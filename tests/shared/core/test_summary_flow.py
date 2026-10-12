"""phaze-g9gua: the Summary flow's edges, bottleneck and per-read availability.

Pure tests drive ``_derive_summary_overview`` directly. The DB-backed tests inject failures at the
STATEMENT level -- a query against a relation that does not exist, executed by the real degrade
wrappers against the real per-test Postgres -- so every fallback below is the production fallback
(the real SAVEPOINT rollback, the real legacy zero, the real generic marker), not a mock of the
availability seam.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, literal_column, select, table

from phaze.models.analysis import AnalysisResult
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.shell import summary as summary_mod
from phaze.routers.shell.summary import SummaryOverviewInputs, _build_flow_edges, _derive_bottleneck, _derive_summary_overview
from phaze.services.pipeline import buckets as buckets_mod, stages as stages_mod
from phaze.services.pipeline.common import capture_read_degradations, mark_read_degraded, measure_read
from tests.shared.routers.pipeline._shared import _reset_saq_jobs_minimal


if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


# Every source label get_stage_progress records when handed a sink.
STAGE_PROGRESS_SOURCES = (
    "music_video_total",
    "tracklist_total",
    "discovery",
    "proposals_total",
    "metadata_bucket",
    "analyze_bucket",
    "tracklist",
    "match",
    "proposals",
    "execute",
    "execute_total",
)


def _bucket(size: int, /, **counts: int | None) -> dict[str, int | None]:
    return {"not_started": size, "in_flight": 0, "orphaned": 0, "done": 0, "skipped": 0, "failed": 0, "missing": 0, "total": size} | counts


def _progress(total: int = 4, **nodes: dict[str, int | None]) -> dict[str, dict[str, int | None]]:
    return {
        "discovery": {"done": total, "total": total},
        "metadata": _bucket(total),
        "analyze": _bucket(total),
        "tracklist": {"done": 0, "total": None},
        "match": {"done": 0, "total": 0},
        "proposals": {"done": 0, "total": 0},
        "execute": {"done": 0, "total": 0},
    } | nodes


def _inputs(**overrides: Any) -> SummaryOverviewInputs:
    base = SummaryOverviewInputs(
        proposal_pending=0,
        proposal_approved=0,
        active_fileservers=1,
        orphan_counts={"metadata": 0, "analyze": 0},
        stalled_analyses=0,
        inadmissible_count=0,
        awaiting_cloud_count=0,
        awaiting_hold_reason=None,
        queued_behind_quota_count=0,
        stage_paused={"metadata": False, "analyze": False},
        enriched_count=0,
        analysis_live=0,
        analyses_today=0,
        analyses_lifetime=0,
    )
    return replace(base, **overrides)


def _healthy_sink(**failed: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    return dict.fromkeys((*STAGE_PROGRESS_SOURCES, "proposal_stats"), ()) | failed


def _node(overview: dict[str, Any], name: str) -> dict[str, Any]:
    return next(node for node in overview["flow"] if node["name"] == name)


def _edge(overview: dict[str, Any], edge_id: str) -> dict[str, Any]:
    return next(edge for edge in overview["flow_edges"] if edge["id"] == edge_id)


# --------------------------------------------------------------------------------------------
# Pure derivation
# --------------------------------------------------------------------------------------------


def test_empty_archive_edges_are_measured_zeros_and_the_bottleneck_is_discovery() -> None:
    overview = _derive_summary_overview(_progress(total=0), _inputs())

    assert [edge["value"] for edge in overview["flow_edges"]] == [0] * 7
    assert {edge["state"] for edge in overview["flow_edges"]} == {"empty"}
    assert all(edge["width"] == 1.0 for edge in overview["flow_edges"]), "a measured zero keeps a visible hairline, never no width"
    assert overview["total_files"] == 0
    assert overview["is_empty"] is True
    assert overview["bottleneck"] == {
        "source": "recommended",
        "id": None,
        "node": "Discover",
        "title": "Discover the collection",
        "detail": overview["recommended"]["detail"],
        "tone": "accent",
    }


def test_degraded_reads_stay_none_never_zero() -> None:
    progress = _progress(
        discovery={"done": None, "total": None},
        analyze=_bucket(4, not_started=None, in_flight=None, orphaned=None, done=None, skipped=None, failed=None, missing=None, total=None),
        proposals={"done": 2, "total": None},
        execute={"done": 1, "total": None},
        metadata=_bucket(4, not_started=0, done=4),
    )
    overview = _derive_summary_overview(progress, _inputs(enriched_count=None))

    analyze = _node(overview, "Analyze")
    assert (analyze["done"], analyze["total"]) == (None, None)
    assert analyze["status"]["label"] == "unavailable"
    assert analyze["status"]["failed"] is None, "an unmeasured failure count is not a measured zero"
    assert _node(overview, "Metadata")["done"] == 4, "an independently measured sibling stays known"
    assert _node(overview, "Propose")["done"] == 2, "the measured numerator survives its unmeasured denominator"
    assert _node(overview, "Propose")["total"] is None
    for edge_id in ("discover-metadata", "discover-analyze", "analyze-join", "join-propose", "review-apply"):
        edge = _edge(overview, edge_id)
        assert (edge["value"], edge["width"], edge["state"]) == (None, None, "unknown"), edge_id
    assert _edge(overview, "metadata-join")["value"] == 4
    assert _edge(overview, "propose-review")["value"] == 2
    assert overview["total_files"] is None
    assert overview["is_empty"] is False, "an unmeasured collection is not an empty one"
    assert overview["recommended"]["title"] != "Discover the collection"


def test_paused_stage_with_pending_work_outlines_that_stage_as_the_top_attention_item() -> None:
    progress = _progress(metadata=_bucket(4, not_started=4), analyze=_bucket(4, not_started=0, done=4))
    overview = _derive_summary_overview(progress, _inputs(stage_paused={"metadata": True, "analyze": False}))

    top = overview["attention"][0]
    assert top["title"] == "Metadata enrichment is paused"
    assert overview["bottleneck"]["source"] == "attention"
    assert overview["bottleneck"]["id"] == top["id"]
    assert overview["bottleneck"]["title"] == top["title"]
    assert overview["bottleneck"]["node"] == "Metadata"


def test_failure_dominated_state_takes_the_top_attention_item_not_an_independent_ranking() -> None:
    # Analyze carries far more failures than Metadata, but the shared policy ranks Metadata failures
    # first (priority 10 vs 11): the callout must follow it, not re-rank by size.
    progress = _progress(
        total=100,
        metadata=_bucket(100, not_started=0, done=98, failed=2),
        analyze=_bucket(100, not_started=0, done=10, failed=90),
    )
    overview = _derive_summary_overview(progress, _inputs(orphan_counts={"metadata": 0, "analyze": 3}))

    assert [item["title"] for item in overview["attention"]][:3] == ["Metadata failures", "Analysis failures", "Orphaned work"]
    assert overview["bottleneck"]["id"] == overview["attention"][0]["id"] == "metadata-failed"
    assert overview["bottleneck"]["node"] == "Metadata"
    assert overview["recommended"]["href"] == "#needs-attention-title"


def test_pipeline_wide_attention_outlines_no_stage() -> None:
    overview = _derive_summary_overview(_progress(), _inputs(active_fileservers=0))
    assert overview["attention"][0]["id"] == "fileserver-unavailable"
    assert overview["bottleneck"]["id"] == "fileserver-unavailable"
    assert overview["bottleneck"]["node"] is None


def test_join_uses_the_convergence_set_and_review_apply_counts_distinct_approved_files() -> None:
    progress = _progress(
        total=10,
        metadata=_bucket(8, not_started=0, done=8),
        analyze=_bucket(8, not_started=2, done=6),
        proposals={"done": 3, "total": 5},
        execute={"done": 1, "total": 2},
    )
    overview = _derive_summary_overview(progress, _inputs(proposal_pending=4, proposal_approved=7))

    # Neither min(8, 6) nor 8 + 6: the measured convergence set.
    assert _edge(overview, "join-propose")["value"] == 5
    # Distinct approved FILES (execute.total), not the 7 approved proposal ROWS.
    assert _edge(overview, "review-apply")["value"] == 2
    apply = _node(overview, "Execute")
    assert apply["detail"] == "7 approved now"
    assert apply["done"] == 1
    widths = {edge["id"]: edge["width"] for edge in overview["flow_edges"]}
    assert widths["discover-metadata"] == 14.0, "the widest measured edge takes the full proxy width"
    assert widths["discover-metadata"] > widths["metadata-join"] > widths["analyze-join"] > widths["join-propose"] > widths["review-apply"]


def test_bottleneck_follows_the_recommendation_when_nothing_needs_attention() -> None:
    complete = _bucket(4, not_started=0, done=4)
    progress = _progress(metadata=complete, analyze=complete, proposals={"done": 4, "total": 4})

    review = _derive_summary_overview(progress, _inputs(proposal_pending=2))
    assert review["attention"] == []
    assert review["bottleneck"]["node"] == "Review"
    assert review["bottleneck"]["title"] == review["recommended"]["title"]

    caught_up = _derive_summary_overview(progress, _inputs())
    assert caught_up["recommended"]["title"] == "Pipeline caught up"
    assert caught_up["bottleneck"]["node"] is None


def test_bottleneck_on_unknown_attention_is_the_unavailable_recommendation() -> None:
    attention = [{"id": "metadata-failed", "scope": "metadata", "title": "Metadata failures", "detail": "x", "tone": "danger"}]
    unavailable = {
        "title": "Inspect unavailable status",
        "detail": "A pipeline measurement is unavailable.",
        "href": "#needs-attention-title",
        "tone": "neutral",
    }
    bottleneck = _derive_bottleneck(attention, unavailable, attention_known=False)  # type: ignore[arg-type]
    assert bottleneck["source"] == "unavailable"
    assert bottleneck["node"] is None
    assert bottleneck["title"] == "Inspect unavailable status"


def test_edge_definitions_are_documented_with_the_music_video_unit_mismatch() -> None:
    doc = _build_flow_edges.__doc__ or ""
    assert "files holding" in doc
    assert "UNIT MISMATCH" in doc
    assert "music/video" in doc
    assert "proposals.total" in doc


def test_sink_absent_source_is_unknown_and_measured_zero_stays_zero() -> None:
    sink = _healthy_sink()
    del sink["execute_total"]
    overview = _derive_summary_overview(_progress(total=0), _inputs(flow_read_failures=sink))
    assert _edge(overview, "review-apply")["state"] == "unknown", "a source the snapshot never measured is unknown, not healthy"
    assert _edge(overview, "discover-metadata")["value"] == 0
    assert _edge(overview, "discover-metadata")["state"] == "empty"


def test_sink_bucket_failure_and_split_only_failure_are_narrowed_correctly() -> None:
    progress = _progress(analyze=_bucket(4, not_started=1, in_flight=2, done=1), metadata=_bucket(4, not_started=0, done=4))

    bucket_failed = _derive_summary_overview(progress, _inputs(flow_read_failures=_healthy_sink(analyze_bucket=("stage_bucket",))))
    assert _node(bucket_failed, "Analyze")["done"] is None
    assert _node(bucket_failed, "Metadata")["done"] == 4

    split_only = _derive_summary_overview(progress, _inputs(flow_read_failures=_healthy_sink(analyze_bucket=("stage_orphan_split",))))
    analyze = _node(split_only, "Analyze")
    assert analyze["done"] == 1, "the split failing does not erase the already-measured done count"
    assert analyze["status"]["label"] == "activity unavailable"
    assert _edge(split_only, "analyze-join")["value"] == 1


def test_metadata_unavailable_flag_is_honoured_without_a_marker() -> None:
    progress = _progress(metadata=_bucket(4, not_started=0, done=0) | {"available": 0})
    overview = _derive_summary_overview(progress, _inputs(flow_read_failures=_healthy_sink()))
    assert _node(overview, "Metadata")["done"] is None
    assert _edge(overview, "metadata-join")["state"] == "unknown"


def test_unmeasured_proposal_stats_never_read_as_caught_up() -> None:
    overview = _derive_summary_overview(_progress(), _inputs(flow_read_failures=_healthy_sink(proposal_stats=("source_session",))))
    review = _node(overview, "Review")
    assert review["status"]["label"] == "unavailable"
    assert review["detail"] == "— awaiting decision"
    assert overview["proposal_pending"] is None
    assert overview["proposal_approved"] is None


# --------------------------------------------------------------------------------------------
# measure_read: attribution and replay
# --------------------------------------------------------------------------------------------


async def test_measure_read_attributes_concurrent_generic_markers_and_replays_them() -> None:
    sink: dict[str, tuple[str, ...]] = {}

    async def failing() -> int:
        await asyncio.sleep(0)
        mark_read_degraded("source_session")
        return 0

    async def healthy() -> int:
        await asyncio.sleep(0)
        return 3

    with capture_read_degradations() as outer:
        assert await asyncio.gather(measure_read(sink, "analyze_bucket", failing()), measure_read(sink, "metadata_bucket", healthy())) == [0, 3]
    assert sink == {"analyze_bucket": ("source_session",), "metadata_bucket": ()}
    assert outer == ["source_session"], "the enclosing snapshot collector still sees the replayed failure"


async def test_measure_read_leaves_a_raising_source_unmeasured() -> None:
    sink: dict[str, tuple[str, ...]] = {}

    async def raising() -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await measure_read(sink, "execute", raising())
    assert "execute" not in sink


# --------------------------------------------------------------------------------------------
# Real failure injection against Postgres
# --------------------------------------------------------------------------------------------

_FAULTS: ContextVar[frozenset[str]] = ContextVar("summary_flow_faults", default=frozenset())


def _missing_relation() -> Any:
    return select(func.count()).select_from(table("phaze_g9gua_missing_relation"))


@pytest.fixture
def inject_faults(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Route the named reads onto statements Postgres rejects, inside the REAL degrade wrappers.

    The fault set is a ContextVar, so a fault set inside one task stays in that task (and the
    gather children it spawns) -- which is what lets two concurrent snapshots differ.
    """
    real_count = stages_mod._safe_count
    real_bucket_stmt = buckets_mod._stage_bucket_stmt
    real_orphaned = buckets_mod.orphaned_clause
    real_stats = summary_mod.get_proposal_stats

    async def count(session: AsyncSession, stmt: Any, *, node: str) -> int:
        return await real_count(session, _missing_relation() if node in _FAULTS.get() else stmt, node=node)

    def bucket_stmt(stage: Any) -> Any:
        if f"{stage.value}_bucket" in _FAULTS.get():
            return select(literal_column("'done'"), func.count()).select_from(table("phaze_g9gua_missing_relation"))
        return real_bucket_stmt(stage)

    def orphaned(stage: Any) -> Any:
        if f"{stage.value}_split" in _FAULTS.get():
            return literal_column("phaze_g9gua_missing_column") > 0
        return real_orphaned(stage)

    async def stats(session: AsyncSession) -> Any:
        if "proposal_stats" in _FAULTS.get():
            async with session.begin_nested():
                await session.execute(_missing_relation())
        return await real_stats(session)

    monkeypatch.setattr(stages_mod, "_safe_count", count)
    monkeypatch.setattr(buckets_mod, "_stage_bucket_stmt", bucket_stmt)
    monkeypatch.setattr(buckets_mod, "orphaned_clause", orphaned)
    monkeypatch.setattr(summary_mod, "get_proposal_stats", stats)
    # Keep the non-flow attention inputs measured, so the only unknowns are the injected ones.
    lane = {"id": "cloud-a", "kind": "kueue", "available": True, "in_flight": 0, "cap": 4}
    monkeypatch.setattr(summary_mod, "get_backend_lane_snapshot", AsyncMock(return_value=[lane]))
    monkeypatch.setattr(summary_mod, "_get_summary_aggregates", AsyncMock(return_value={"enriched": 1, "active_fileservers": 1}))
    monkeypatch.setattr(summary_mod, "get_analysis_live_count", AsyncMock(return_value=0))
    monkeypatch.setattr(summary_mod, "get_cached_stage_orphan_counts", lambda: {"metadata": 0, "analyze": 0})
    monkeypatch.setattr(summary_mod, "stage_orphan_counts_known", lambda: True)
    yield


async def _seed(session: AsyncSession, make_file: Any) -> None:
    """Two music files: one fully enriched, one with metadata done and analysis in flight."""
    await _reset_saq_jobs_minimal(session)
    settled = await make_file(original_filename="settled.mp3")
    running = await make_file(original_filename="running.mp3")
    session.add_all(
        [
            FileMetadata(file_id=settled.id),
            AnalysisResult(file_id=settled.id, analysis_completed_at=datetime.now(UTC)),
            FileMetadata(file_id=running.id),
            SchedulingLedger(key=f"process_file:{running.id}", function="process_file", routing="agent", payload={"file_id": str(running.id)}),
        ]
    )
    await session.commit()


async def _summary_under(faults: set[str], session: AsyncSession) -> dict[str, Any]:
    _FAULTS.set(frozenset(faults))
    return (await summary_mod._build_summary_context(SimpleNamespace(), session))["summary"]


@pytest.mark.usefixtures("inject_faults")
async def test_stage_progress_sink_attributes_each_real_failure_and_keeps_the_legacy_dict(session: AsyncSession, make_file: Any) -> None:
    await _seed(session, make_file)
    healthy_sink: dict[str, tuple[str, ...]] = {}
    legacy = await stages_mod.get_stage_progress(session)
    assert await stages_mod.get_stage_progress(session, read_failures=healthy_sink) == legacy, "the sink never changes the legacy dict"
    assert healthy_sink == dict.fromkeys(STAGE_PROGRESS_SOURCES, ())

    _FAULTS.set(frozenset({"proposals_total", "analyze_bucket", "execute_total"}))
    try:
        sink: dict[str, tuple[str, ...]] = {}
        with capture_read_degradations() as outer:
            degraded = await stages_mod.get_stage_progress(session, read_failures=sink)
    finally:
        _FAULTS.set(frozenset())

    assert sink["proposals_total"] == ("proposals_total",)
    assert sink["execute_total"] == ("execute_total",)
    assert sink["analyze_bucket"] == ("stage_bucket",), "the generic bucket marker is owned by the Analyze read"
    assert sink["metadata_bucket"] == (), "the concurrent Metadata bucket read did not fail"
    assert all(sink[source] == () for source in STAGE_PROGRESS_SOURCES if source not in {"proposals_total", "execute_total", "analyze_bucket"})
    assert sorted(outer) == ["execute_total", "proposals_total", "stage_bucket"], "every failure is replayed to the snapshot collector"
    # Legacy fallbacks are untouched: zeros, same keys, healthy siblings intact.
    assert degraded["proposals"] == {"done": legacy["proposals"]["done"], "total": 0}
    assert degraded["execute"]["total"] == 0
    assert degraded["analyze"]["done"] == 0
    assert degraded["metadata"] == legacy["metadata"]
    assert degraded.keys() == legacy.keys()


@pytest.mark.parametrize(
    ("faults", "expect"),
    [
        ({"analyze_bucket"}, {"Analyze": None, "analyze-join": None, "metadata-join": 2}),
        ({"proposals_total"}, {"Propose": 0, "join-propose": None, "Propose.total": None}),
        ({"execute_total"}, {"review-apply": None, "Execute": 0}),
        ({"proposal_stats"}, {"Review.status": "unavailable", "Execute.detail": "— approved now"}),
        ({"analyze_split"}, {"Analyze": 1, "Analyze.status": "activity unavailable", "analyze-join": 1}),
        ({"discovery"}, {"Discover": None, "total_files": None}),
    ],
)
@pytest.mark.usefixtures("inject_faults")
async def test_real_failures_reach_the_flow_as_unknown_and_the_callout_follows_the_override(
    session: AsyncSession, make_file: Any, faults: set[str], expect: dict[str, Any]
) -> None:
    await _seed(session, make_file)
    healthy = await _summary_under(set(), session)
    assert healthy["attention_known"] is True
    assert _node(healthy, "Analyze")["done"] == 1
    assert _node(healthy, "Analyze")["status"]["label"] in {"partial", "in flight"}

    overview = await _summary_under(faults, session)

    for key, value in expect.items():
        if key == "total_files":
            assert overview["total_files"] is value
        elif "-" in key:
            edge = _edge(overview, key)
            assert edge["value"] == value, key
            if value is None:
                assert (edge["width"], edge["state"]) == (None, "unknown")
        elif key.endswith(".status"):
            assert _node(overview, key.split(".")[0])["status"]["label"] == value
        elif key.endswith(".total"):
            assert _node(overview, key.split(".")[0])["total"] is value
        elif key.endswith(".detail"):
            assert _node(overview, key.split(".")[0])["detail"] == value
        else:
            assert _node(overview, key)["done"] == value, key
    assert overview["attention_known"] is False
    assert overview["recommended"]["title"] == "Inspect unavailable status"
    assert overview["bottleneck"] == {
        "source": "unavailable",
        "id": None,
        "node": None,
        "title": "Inspect unavailable status",
        "detail": overview["recommended"]["detail"],
        "tone": "neutral",
    }


@pytest.mark.usefixtures("inject_faults")
async def test_concurrent_snapshots_never_share_a_failure(session: AsyncSession, make_file: Any) -> None:
    await _seed(session, make_file)
    faulted, healthy = await asyncio.gather(_summary_under({"analyze_bucket"}, session), _summary_under(set(), session))

    assert _node(faulted, "Analyze")["done"] is None
    assert faulted["attention_known"] is False
    assert _node(healthy, "Analyze")["done"] == 1
    assert healthy["attention_known"] is True
    assert healthy["recommended"]["title"] != "Inspect unavailable status"
    assert healthy["bottleneck"]["source"] != "unavailable"


@pytest.mark.usefixtures("inject_faults")
async def test_healthy_empty_archive_keeps_measured_zeros(session: AsyncSession) -> None:
    await _reset_saq_jobs_minimal(session)
    overview = await _summary_under(set(), session)
    assert overview["attention_known"] is True
    assert overview["total_files"] == 0
    assert [edge["value"] for edge in overview["flow_edges"]] == [0] * 7
    assert {edge["state"] for edge in overview["flow_edges"]} == {"empty"}
    assert overview["bottleneck"]["node"] == "Discover"
