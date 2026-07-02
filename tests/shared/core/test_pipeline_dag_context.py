"""Tests for the Phase-35 DAG-canvas store extension + per-node router context (35-04).

Two surfaces are covered:

- The ``$store.pipeline`` extension in ``base.html`` (a pure template-text assertion;
  no DB) — every per-node sub-key is registered AND seeded to 0, with the Phase-34 keys
  preserved (``-k store``).
- The per-node router context built by ``_build_dag_context`` + the OOB seed paragraphs
  emitted by ``stats_bar.html`` on the 5s poll (DB-backed, auto-marked ``integration``):
  DB-truth ``done`` from ``get_stage_progress``, the ``completed``-counter degrade-fallback
  (D-02), and the never-500 degrade when the counter source is unavailable (T-35-09).
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_BASE_HTML = Path(__file__).resolve().parent.parent.parent.parent / "src" / "phaze" / "templates" / "base.html"

# Every per-node store sub-key the UI-SPEC "Store extension" mandates (35-UI-SPEC L294).
_NEW_STORE_KEYS = (
    "metadataDone",
    "metadataTotal",
    "fingerprintDone",
    "fingerprintTotal",
    "analyzeDone",
    "analyzeTotal",
    "analyzeActive",
    "tracklistDone",
    "scrapeDone",
    "scrapeTotal",
    "matchDone",
    "matchTotal",
    "proposalsDone",
    "proposalsTotal",
    "approved",
    "executedDone",
    "executedTotal",
    # Phase 38 (38-03): per-stage pause/priority live-state keys (REQ-38-4). One edit drives
    # the store-literal seed test, the int-key context test, AND the OOB-seed test for all 6.
    "metadataPaused",
    "metadataPriority",
    "analyzePaused",
    "analyzePriority",
    "fingerprintPaused",
    "fingerprintPriority",
    # t7k FIX2: per-stage in-flight busy counts (replace the single global agentBusy gate). One
    # edit drives the store-literal seed test, the int-key context test, AND the OOB-seed test.
    "metadataBusy",
    "analyzeBusy",
    "fingerprintBusy",
    # Phase 39 (REQ-39-3): search_tracklist in-flight busy count. Rides the same dag.items()
    # seed/OOB loop so the Search node gate reacts live on every 5s poll. One edit drives the
    # store-literal seed test, the int-key context test, AND the OOB-seed test.
    "searchBusy",
    # Phase 40 (REQ-40-2/REQ-40-3): scan_live_set in-flight busy count ("Scan busy") + online-agent
    # count ("Needs agent" when 0). Both ride the same dag.items() seed/OOB loop so the Fingerprint-
    # Scan node gate reacts live on every 5s poll. One edit drives the store-literal seed test, the
    # int-key context test, AND the OOB-seed test.
    "scanBusy",
    "agentOnline",
    # Phase 41 (REQ-41-3): scrape_and_store_tracklist / match_tracklist_to_discogs in-flight busy
    # counts gating the DAG Scrape/Match trigger nodes. Both ride the same dag.items() seed/OOB loop
    # so the gates react live on every 5s poll. One edit drives the store-literal seed test, the
    # int-key context test, AND the OOB-seed test.
    "scrapeBusy",
    "matchBusy",
    # Phase 58 (58-02, WORK-01): Discover "not yet enriched" derived backlog int (discovered -
    # metadataExtracted, clamped >= 0). One edit drives the store-literal seed test, the int-key
    # context test, AND the OOB-seed test -- it rides the same dag.items() seed/OOB loop.
    "notYetEnriched",
)

# The Phase-34 keys the existing button :disabled gating reads — must NOT be removed.
_PRESERVED_STORE_KEYS = ("discovered", "analyzed", "metadataExtracted", "agentBusy", "controllerBusy")


def _store_literal() -> str:
    """Return the ``Alpine.store('pipeline', { ... });`` object-literal text from base.html."""
    text = _BASE_HTML.read_text(encoding="utf-8")
    match = re.search(r"Alpine\.store\('pipeline',\s*\{(.*?)\}\s*\);", text, re.DOTALL)
    assert match is not None, "Alpine.store('pipeline', {...}) literal not found in base.html"
    return match.group(1)


# ---------------------------------------------------------------------------
# Task 1: $store.pipeline extension (pure template text — no DB, runs everywhere)
# ---------------------------------------------------------------------------


def test_store_seeds_every_new_per_node_key_to_zero() -> None:
    """Every UI-SPEC per-node sub-key is present in the store literal and seeded to 0."""
    literal = _store_literal()
    for key in _NEW_STORE_KEYS:
        assert re.search(rf"\b{key}\s*:\s*0\b", literal), f"store key '{key}' missing or not seeded to 0"


def test_store_preserves_phase34_keys() -> None:
    """The Phase-34 keys the button gating reads are still seeded (not removed)."""
    literal = _store_literal()
    for key in _PRESERVED_STORE_KEYS:
        assert re.search(rf"\b{key}\s*:\s*0\b", literal), f"Phase-34 store key '{key}' was removed"


def test_store_literal_has_no_undefined_seed() -> None:
    """No store key is left undefined — a sample of the new keys read 0, not undefined."""
    literal = _store_literal()
    for key in ("analyzeActive", "metadataDone", "proposalsTotal", "executedDone"):
        assert f"{key}: 0" in literal


# ---------------------------------------------------------------------------
# Task 2: per-node router context (_build_dag_context) — DB-backed
# ---------------------------------------------------------------------------


class _FallbackRedis:
    """Minimal async Redis double exposing only ``mget`` over a seeded ``store`` dict.

    Mirrors a non-``decode_responses`` client (returns ``bytes`` per present key) so the
    ``pipeline_counters._to_int`` bytes-decode path is exercised. ``read_counters`` only
    calls ``mget`` on the enqueued + completed key lists, so that is all we implement.
    """

    def __init__(self, store: dict[str, int]) -> None:
        self.store = store

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        return [str(self.store[k]).encode() if k in self.store else None for k in keys]


def _idle_activity() -> dict[str, int]:
    """A zero queue-activity dict (no in-flight work) for _build_dag_context."""
    return {
        "agent_queued": 0,
        "agent_active": 0,
        "controller_queued": 0,
        "controller_active": 0,
        "agent_busy": 0,
        "controller_busy": 0,
    }


def _make_file_with_metadata() -> tuple[FileRecord, FileMetadata]:
    """A discovered FileRecord plus a FileMetadata row (drives metadata.done == 1)."""
    uid = uuid.uuid4()
    file_rec = FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.METADATA_EXTRACTED,
    )
    metadata = FileMetadata(file_id=uid, artist="Test", title="Track")
    return file_rec, metadata


@pytest.mark.asyncio
async def test_build_dag_context_carries_every_per_node_key(session: AsyncSession) -> None:
    """_build_dag_context returns a ``dag`` dict carrying every per-node store key."""
    app_state = SimpleNamespace()  # no redis → counters degrade to {} (DB-only)
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    dag = ctx["dag"]
    for key in _NEW_STORE_KEYS:
        assert key in dag, f"context dag missing store key '{key}'"
        assert isinstance(dag[key], int), f"dag['{key}'] must be an int for x-init interpolation"


@pytest.mark.asyncio
async def test_dag_done_comes_from_db_truth(session: AsyncSession) -> None:
    """metadata.done reflects the DB metadata row count (get_stage_progress authority)."""
    file_rec, metadata = _make_file_with_metadata()
    session.add(file_rec)
    session.add(metadata)
    await session.flush()

    app_state = SimpleNamespace()
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    assert ctx["dag"]["metadataDone"] == 1
    assert ctx["dag"]["analyzeActive"] == 0


@pytest.mark.asyncio
async def test_completed_counter_degrade_fallback(session: AsyncSession) -> None:
    """When a node's DB done is 0 AND its mapped completed counter > 0, the counter renders.

    This exercises the maintained ``completed`` counter as a DOCUMENTED degrade-fallback
    (D-02): there are NO metadata rows in the DB (metadata.done == 0), but the maintained
    ``completed`` counter for ``extract_file_metadata`` is 3, so metadataDone falls back to 3.
    """
    fake = _FallbackRedis({"phaze:pipeline:completed:extract_file_metadata": 3})
    app_state = SimpleNamespace(redis=fake)
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    assert ctx["dag"]["metadataDone"] == 3


@pytest.mark.asyncio
async def test_proposals_batch_counter_is_not_a_fallback_done(session: AsyncSession) -> None:
    """WR-03: generate_proposals is a BATCH task, so its completed counter must NOT render as
    proposalsDone (a per-file count). With DB done == 0 and a generate_proposals completed
    counter of 5, proposalsDone stays 0 (DB-truth) rather than the wrong-unit batch count.
    """
    fake = _FallbackRedis({"phaze:pipeline:completed:generate_proposals": 5})
    app_state = SimpleNamespace(redis=fake)
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    assert ctx["dag"]["proposalsDone"] == 0


@pytest.mark.asyncio
async def test_db_truth_wins_over_completed_counter(session: AsyncSession) -> None:
    """When the DB has done > 0, DB-truth wins even if the completed counter differs (D-03)."""
    file_rec, metadata = _make_file_with_metadata()
    session.add(file_rec)
    session.add(metadata)
    await session.flush()

    # completed counter says 99, but the DB has exactly 1 metadata row → DB wins.
    fake = _FallbackRedis({"phaze:pipeline:completed:extract_file_metadata": 99})
    app_state = SimpleNamespace(redis=fake)
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    assert ctx["dag"]["metadataDone"] == 1


@pytest.mark.asyncio
async def test_build_dag_context_never_raises_on_counter_outage(session: AsyncSession) -> None:
    """A counter source that raises degrades to DB-truth — _build_dag_context never raises."""

    class _ExplodingRedis:
        async def mget(self, keys: list[str]) -> list[bytes | None]:  # noqa: ARG002
            raise RuntimeError("redis down")

    app_state = SimpleNamespace(redis=_ExplodingRedis())
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    # Empty DB + dead counters → all-zero, but the function returned cleanly (no 500).
    assert ctx["dag"]["metadataDone"] == 0


@pytest.mark.asyncio
async def test_build_dag_context_degrades_to_default_stage_controls(session: AsyncSession) -> None:
    """The per-stage pause/priority keys flow through as the Phase-37 defaults when the
    control table is unreadable/empty (T-38-DEGRADE).

    ``get_stage_controls`` mirrors the ``_safe_count`` / ``get_queue_activity`` discipline:
    on ANY failure (a missing ``pipeline_stage_control`` table in the unit test DB, a DB
    hiccup) it returns ``paused=False, priority=50`` for every stage and never raises into
    the 5s poll. ``_build_dag_context`` coerces those defaults to ``int`` (``0`` / ``50``)
    so the all-ints ``x-init`` invariant holds (Pitfall 3 / T-35-11).
    """
    app_state = SimpleNamespace()
    from phaze.routers.pipeline import _build_dag_context

    ctx = await _build_dag_context(app_state, session, _idle_activity())
    dag = ctx["dag"]
    assert dag["metadataPaused"] == 0
    assert dag["metadataPriority"] == 50
    assert isinstance(dag["metadataPaused"], int)
    assert isinstance(dag["metadataPriority"], int)


@pytest.mark.asyncio
async def test_get_stage_controls_degrades_on_db_error() -> None:
    """get_stage_controls returns the 3-stage defaults and never raises when the SELECT fails.

    This proves the actual T-38-DEGRADE mitigation (the except branch: warn → guarded rollback
    → defaults) against a session whose ``execute`` raises — a missing ``pipeline_stage_control``
    table or a DB hiccup. The DB-backed ``_build_dag_context`` degrade test above only exercises
    the empty-table happy path (zero rows → defaults), so this fake-session test is what actually
    covers the never-500 rollback branch that keeps the 5s poll alive.
    """
    from phaze.services.pipeline import get_stage_controls

    rolled_back = False

    class _ExplodingSession:
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError('relation "pipeline_stage_control" does not exist')

        async def rollback(self) -> None:
            nonlocal rolled_back
            rolled_back = True

    controls = await get_stage_controls(_ExplodingSession())  # type: ignore[arg-type]
    assert rolled_back is True
    assert controls == {s: {"paused": False, "priority": 50} for s in ("metadata", "analyze", "fingerprint")}


@pytest.mark.asyncio
async def test_get_stage_controls_overlays_present_rows(session: AsyncSession) -> None:
    """A present control row overlays its paused/priority onto the defaults; absent stages keep
    the defaults. Proves the happy-path overlay loop in get_stage_controls."""
    from phaze.models.pipeline_stage_control import PipelineStageControl
    from phaze.services.pipeline import get_stage_controls

    session.add(PipelineStageControl(stage="analyze", paused=True, priority=20))
    await session.flush()

    controls = await get_stage_controls(session)
    assert controls["analyze"] == {"paused": True, "priority": 20}
    assert controls["metadata"] == {"paused": False, "priority": 50}
    assert controls["fingerprint"] == {"paused": False, "priority": 50}


# ---------------------------------------------------------------------------
# Task 2: HTTP render — OOB seeds on the poll + dashboard full-page never-500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_poll_emits_oob_seed_for_every_node_key(client: AsyncClient) -> None:
    """GET /pipeline/stats emits an OOB x-init seed paragraph for every per-node store key."""
    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    body = response.text
    for key in _NEW_STORE_KEYS:
        assert f'id="dag-seed-{key}"' in body, f"missing OOB seed id for store key '{key}'"
        assert f"$store.pipeline.{key} =" in body, f"missing x-init store write for '{key}'"
    # The seeds are OOB-swapped (only honored during an HTMX swap, i.e. the poll).
    assert body.count('hx-swap-oob="true"') >= len(_NEW_STORE_KEYS)


@pytest.mark.asyncio
async def test_stats_poll_degrades_to_200_without_counter_source(client: AsyncClient) -> None:
    """The 5s poll never 500s when the Redis counter source is absent (T-35-09)."""
    # The test client skips the lifespan, so app.state.redis / controller_queue are absent;
    # the counter read degrades to {} and the poll still renders 200 from DB-truth.
    response = await client.get("/pipeline/stats")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Phase 50 (50-07, D-09): the bounded cloud-window count cards — PUSHING ("Staged
# pushing") + PUSHED ("Analyzing cloud") — must ride BOTH the dashboard full-page
# context and the 5s stats-poll context, degrade-safe (never 500 the poll).
# ---------------------------------------------------------------------------


def _window_file(i: int, state: FileState) -> FileRecord:
    """A FileRecord seed in the given state (unique hash/path per ``i``)."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=f"w{i:063d}"[:64],
        original_path=f"/music/win{i}.mp3",
        original_filename=f"win{i}.mp3",
        current_path=f"/music/win{i}.mp3",
        file_type="mp3",
        file_size=1000,
        state=state,
    )


async def _capture_context(client: AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str) -> dict[str, object]:
    """GET ``path`` while capturing the TemplateResponse context the router builds.

    Patches the router's ``templates.TemplateResponse`` to record the ``context`` dict and
    return a trivial 200 — decoupling the context-wiring assertion from template rendering
    (the partials may not yet be included). Mirrors the OOB-contract id-on-both-renders test.
    """
    from starlette.responses import HTMLResponse

    from phaze.routers import pipeline as pipeline_router, shell as shell_router

    captured: dict[str, object] = {}

    def _spy(**kwargs: object) -> HTMLResponse:
        captured.update(kwargs.get("context", {}))  # type: ignore[arg-type]
        return HTMLResponse("ok")

    # CUT-02 (Phase 62): /pipeline/ is now a pure redirect; the DAG dashboard *context*
    # (built by the shared build_dashboard_context) is consumed by the shell Analyze render
    # (/s/analyze). /pipeline/stats still renders through the pipeline router. Patch BOTH
    # routers' templates so this helper captures context regardless of which path is under test.
    monkeypatch.setattr(pipeline_router.templates, "TemplateResponse", _spy)
    monkeypatch.setattr(shell_router.templates, "TemplateResponse", _spy)
    response = await client.get(path, headers={"HX-Request": "true"})
    assert response.status_code == 200
    return captured


@pytest.mark.asyncio
async def test_dashboard_context_carries_window_counts(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /pipeline/ context carries pushing_count (PUSHING) + analyzing_cloud_count (PUSHED)."""
    session.add_all(
        [
            _window_file(1, FileState.PUSHING),
            _window_file(2, FileState.PUSHING),
            _window_file(3, FileState.PUSHED),
        ]
    )
    await session.commit()

    ctx = await _capture_context(client, monkeypatch, "/s/analyze")
    assert ctx["pushing_count"] == 2
    assert ctx["analyzing_cloud_count"] == 1


@pytest.mark.asyncio
async def test_stats_poll_context_carries_window_counts(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /pipeline/stats context re-pushes pushing_count + analyzing_cloud_count on the 5s poll."""
    session.add_all(
        [
            _window_file(4, FileState.PUSHING),
            _window_file(5, FileState.PUSHED),
            _window_file(6, FileState.PUSHED),
        ]
    )
    await session.commit()

    ctx = await _capture_context(client, monkeypatch, "/pipeline/stats")
    assert ctx["pushing_count"] == 1
    assert ctx["analyzing_cloud_count"] == 2


@pytest.mark.asyncio
async def test_window_counts_present_in_both_contexts_when_empty(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both keys are ALWAYS present (default 0) on the dashboard AND the poll — never missing."""
    dash = await _capture_context(client, monkeypatch, "/s/analyze")
    poll = await _capture_context(client, monkeypatch, "/pipeline/stats")
    for ctx in (dash, poll):
        assert ctx["pushing_count"] == 0
        assert ctx["analyzing_cloud_count"] == 0
