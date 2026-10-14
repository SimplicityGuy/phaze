"""The Summary overview: its stage/flow/attention derivations and the context builder on top.

Split out of the single-module ``routers/shell.py`` by phaze-bk9el.16. Every function here moved
VERBATIM; nothing was rewritten, merged or reordered. The group is the one the repowise split plan
called ``context``: the pure derivations (``_derive_summary_overview`` and its helpers, which take
plain dicts and return plain dicts) plus the two IO functions that feed them
(``_get_summary_aggregates``, ``_build_summary_context``).

PATCH TARGETS MOVED WITH THE CODE. ``_build_summary_context`` resolves ``get_stage_progress``,
``_stats_fanout``, ``get_proposal_stats`` and the rest through THIS module's globals, so a test
that substitutes them patches ``phaze.routers.shell.summary.<name>`` -- not the package. That is a
consequence of moving the function, not a change to it; the package still re-exports every public
name so ordinary importers are unaffected.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select

from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.services.attention import analysis_attention_known, derive_attention_items
from phaze.services.backends import derive_cloud_hold_reason, get_analysis_activity_counts, get_analysis_live_count, get_backend_lane_snapshot
from phaze.services.pipeline import (
    _read_in_own_session,
    _stats_fanout,
    get_analysis_stalled_count,
    get_awaiting_cloud_count,
    get_cached_stage_orphan_counts,
    get_cloud_phase_counts,
    get_inadmissible_count,
    get_stage_controls,
    get_stage_progress,
    stage_orphan_counts_known,
)
from phaze.services.pipeline.common import capture_read_degradations, measure_read
from phaze.services.proposal_queries import ProposalStats, get_proposal_stats
from phaze.services.stage_status import done_clause, inflight_clause
from phaze.utils.humanize import format_count
from phaze.web.template_globals import PAGE_NAMES, open_label


if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession


def _stage_count(bucket: dict[str, int | None], key: str) -> int:
    """One stage-progress bucket count, treating a missing key or a NULL value alike as 0.

    The buckets come straight from the stage-progress aggregates, where a stage with no rows
    yields NULL rather than 0 and an absent stage yields no key at all. Both mean "none", so the
    coercion is the same everywhere -- and repeating ``int(bucket.get(k) or 0)`` at every read
    site is how the two spellings drift apart.
    """
    return int(bucket.get(key) or 0)


def _summary_stage_status(stage: dict[str, int | None]) -> dict[str, str | bool | int]:
    """Map an authoritative stage-progress bucket to the shared status vocabulary.

    phaze-6ak0q: the stage's overall state is derived from its ACTIVITY and COMPLETION, never from a
    nonzero failure count. A stage with 5 analyses executing is "in flight" and one at 104156/104247
    with 91 terminal failures is "complete" (every file has settled) -- failures are reported
    separately in ``failed`` (rendered as its own badge) so they stay visible without overriding
    what the stage is doing. Only a stage whose every settled file failed reads "failed" overall.

    phaze-a51b2: an IDLE stage with some (not all) files settled successfully reads "partial", never
    "not started" -- that label is reserved for a stage where nothing has succeeded yet.
    """
    total = _stage_count(stage, "total")
    done = _stage_count(stage, "done")
    skipped = _stage_count(stage, "skipped")
    failed = _stage_count(stage, "failed")
    # phaze-227l5: files gone from disk are SETTLED (nothing is waiting on them) but are not failures.
    missing = _stage_count(stage, "missing")
    in_flight = _stage_count(stage, "in_flight")
    if in_flight:
        state: dict[str, str | bool | int] = {"label": "in flight", "tone": "accent", "icon": "●", "pulse": True}
    elif total and done + skipped + failed + missing >= total:
        if done + skipped:
            state = {"label": "complete", "tone": "success", "icon": "✓", "pulse": False}
        else:
            state = {"label": "failed" if failed else "missing", "tone": "danger", "icon": "✕", "pulse": False}
    elif total and done + skipped:
        # Idle but unfinished: some files settled successfully, the rest are waiting. Not "not started" (a51b2).
        state = {"label": "partial", "tone": "slate", "icon": "◐", "pulse": False}
    elif total:
        state = {"label": "not started", "tone": "neutral", "icon": "—", "pulse": False}
    else:
        state = {"label": "empty", "tone": "neutral", "icon": "—", "pulse": False}
    state["failed"] = failed
    state["missing"] = missing
    return state


@dataclass(frozen=True, slots=True)
class SummaryOverviewInputs:
    """Every non-``stage_progress`` input :func:`_derive_summary_overview` needs.

    A structured argument replacing what was a 15-parameter keyword-only signature
    (phaze-w84zt): the individual reads are still fanned out independently in
    :func:`_build_summary_context` (each keeps its own degrade-safe default), this is
    only the shape they are handed to the derivation in. ``stage_progress`` stays a
    separate positional argument because it is itself a dict of buckets keyed by stage
    name, not a flat scalar field.
    """

    proposal_pending: int
    proposal_approved: int
    active_fileservers: int | None
    orphan_counts: dict[str, int]
    stalled_analyses: int
    inadmissible_count: int
    awaiting_cloud_count: int
    awaiting_hold_reason: str | None
    queued_behind_quota_count: int
    stage_paused: dict[str, bool]
    enriched_count: int | None
    analysis_live: int | None
    analyses_today: int | None
    analyses_lifetime: int | None
    lanes: list[dict[str, Any]] = field(default_factory=list)
    # phaze-g9gua: the per-source fallbacks this snapshot's flow reads took (see _FlowMeasurements).
    # None -- every pure caller -- trusts the supplied numbers; the real Summary read always supplies it.
    flow_read_failures: Mapping[str, tuple[str, ...]] | None = None


def _stage_ready_to_start_work(bucket: dict[str, int | None], paused: bool) -> bool:
    """True when a stage has queued work, is not paused, and nothing is in flight yet."""
    return bool(_stage_count(bucket, "not_started") and not paused and not _stage_count(bucket, "in_flight"))


def _stage_paused_with_pending_work(bucket: dict[str, int | None], paused: bool) -> bool:
    """True when a paused stage still has not-started or in-flight work waiting behind it."""
    return bool(paused and (_stage_count(bucket, "not_started") or _stage_count(bucket, "in_flight")))


_UNAVAILABLE_STATUS: dict[str, str | bool | int | None] = {
    "label": "unavailable",
    "tone": "neutral",
    "icon": "—",
    "pulse": False,
    "failed": None,
    "missing": None,
}

# A bucket read whose ONLY fallback was the orphan split still measured every bucket: the split
# alone failed, so in_flight still holds its orphans and the two cannot be told apart.
_ORPHAN_SPLIT_ONLY = frozenset({"stage_orphan_split"})


@dataclass(frozen=True, slots=True)
class _FlowMeasurements:
    """Which Summary flow sources this snapshot actually measured (phaze-g9gua).

    ``failures`` is the per-source map :func:`get_stage_progress` and :func:`measure_read` fill in.
    ``None`` -- a pure caller that supplied no map -- trusts the values it was handed, so an explicit
    ``None`` value is the only unknown. With a map, a source is measured only when it is PRESENT with
    no fallbacks: an absent source was never measured, so it is unknown, never assumed healthy.
    """

    failures: Mapping[str, tuple[str, ...]] | None

    def measured(self, source: str) -> bool:
        return self.failures is None or self.failures.get(source) == ()

    def buckets_measured(self, source: str) -> bool:
        if self.failures is None:
            return True
        taken = self.failures.get(source)
        return taken is not None and set(taken) <= _ORPHAN_SPLIT_ONLY


def _count_or_none(value: int | None, measured: bool = True) -> int | None:
    """A count that is ``None`` when its read was not measured -- never the fallback zero."""
    return int(value) if measured and value is not None else None


def _flow_bucket(bucket: dict[str, int | None], measurements: _FlowMeasurements, source: str) -> dict[str, int | None]:
    """An enrich-stage bucket with every unmeasured count as ``None``.

    A failed bucket read (or a snapshot reporting ``available`` false) makes every count unknown.
    An orphan-split-only failure keeps the measured buckets and makes only ``in_flight`` and
    ``orphaned`` unknown; ``unsettled`` -- their sum, which the bucket query itself measured --
    stays known so the node can still say whether any work is unsettled.
    """
    known = measurements.buckets_measured(source) and bool(bucket.get("available", 1))
    split_known = known and measurements.measured(source)
    view = {key: _count_or_none(bucket.get(key, 0), known) for key in ("not_started", "done", "skipped", "failed", "missing")}
    in_flight = _count_or_none(bucket.get("in_flight", 0), known)
    orphaned = _count_or_none(bucket.get("orphaned", 0), known)
    view["unsettled"] = None if in_flight is None or orphaned is None else in_flight + orphaned
    view["in_flight"] = in_flight if split_known else None
    view["orphaned"] = orphaned if split_known else None
    return view


def _flow_stage_status(view: dict[str, int | None], total: int | None) -> dict[str, str | bool | int | None]:
    """:func:`_summary_stage_status` over a measured view, or an explicit unavailable status.

    Settled counts the node cannot see, or a denominator it cannot see, never read as a stage
    state: the node says "unavailable" and keeps whichever failed/missing counts WERE measured.
    """
    settled = [view.get(key, 0) for key in ("done", "skipped", "failed", "missing", "not_started")]
    unsettled = view.get("unsettled", 0)
    if total is None or unsettled is None or any(count is None for count in settled):
        return _UNAVAILABLE_STATUS | {"failed": view.get("failed"), "missing": view.get("missing")}
    if unsettled and view.get("in_flight") is None:
        # The orphan split failed with unsettled work: running and orphaned cannot be told apart.
        return _UNAVAILABLE_STATUS | {"label": "activity unavailable", "failed": view.get("failed"), "missing": view.get("missing")}
    known: dict[str, int | None] = {key: value for key, value in view.items() if value is not None}
    return dict(_summary_stage_status({**known, "in_flight": view.get("in_flight") or 0, "total": total}))


def _flow_readiness_status(count: int | None, ready_label: str) -> dict[str, str | bool | int | None]:
    """The Review/Apply flow tiles share one status shape: a ready pill when ``count`` is
    non-zero, else the same "caught up" pill every DAG-progress tile falls back to -- and an
    unavailable pill when the count was never measured, never a caught-up claim from a fallback 0.
    """
    if count is None:
        return dict(_UNAVAILABLE_STATUS)
    if count:
        return {"label": ready_label, "tone": "attention", "icon": "!", "pulse": False, "failed": 0, "missing": 0}
    return {"label": "caught up", "tone": "success", "icon": "✓", "pulse": False, "failed": 0, "missing": 0}


@dataclass(frozen=True, slots=True)
class _FlowValues:
    """Every flow number with its availability already applied: ``None`` means not measured."""

    discovered: int | None
    metadata_total: int | None
    analyze_total: int | None
    metadata: dict[str, int | None]
    analyze: dict[str, int | None]
    convergence: int | None
    proposed: int | None
    executed: int | None
    approved_files: int | None
    pending_rows: int | None
    approved_rows: int | None


def _flow_values(
    stage_progress: dict[str, dict[str, int | None]],
    proposal_pending: int | None,
    proposal_approved: int | None,
    failures: Mapping[str, tuple[str, ...]] | None,
) -> _FlowValues:
    """Resolve each flow number against the source that measured it (phaze-g9gua)."""
    measured = _FlowMeasurements(failures)
    proposals = stage_progress["proposals"]
    execute = stage_progress["execute"]
    return _FlowValues(
        discovered=_count_or_none(stage_progress["discovery"].get("done"), measured.measured("discovery")),
        metadata_total=_count_or_none(stage_progress["metadata"].get("total"), measured.measured("music_video_total")),
        analyze_total=_count_or_none(stage_progress["analyze"].get("total"), measured.measured("music_video_total")),
        metadata=_flow_bucket(stage_progress["metadata"], measured, "metadata_bucket"),
        analyze=_flow_bucket(stage_progress["analyze"], measured, "analyze_bucket"),
        convergence=_count_or_none(proposals.get("total"), measured.measured("proposals_total")),
        proposed=_count_or_none(proposals.get("done"), measured.measured("proposals")),
        executed=_count_or_none(execute.get("done"), measured.measured("execute")),
        approved_files=_count_or_none(execute.get("total"), measured.measured("execute_total")),
        pending_rows=_count_or_none(proposal_pending, measured.measured("proposal_stats")),
        approved_rows=_count_or_none(proposal_approved, measured.measured("proposal_stats")),
    )


def flow_counts(values: _FlowValues) -> dict[str, int | None]:
    """Every figure the Summary flow card, board and text list print, keyed by the store name they bind to.

    phaze-2dv18: ONE definition feeds both the server-rendered first paint (``summary.flow_counts``) and the
    5s ``/pipeline/stats`` poll (``$store.pipeline.summaryFlow``), so a card cannot hold a figure the poll has
    already moved past. ``None`` is an unmeasured figure and renders as an em-dash, never as a zero.
    """
    return {
        "discovered": values.discovered,
        "metadataDone": values.metadata["done"],
        "metadataTotal": values.metadata_total,
        "analyzeDone": values.analyze["done"],
        "analyzeTotal": values.analyze_total,
        "convergence": values.convergence,
        "proposedDone": values.proposed,
        "approvedFiles": values.approved_files,
        "pendingRows": values.pending_rows,
        "approvedRows": values.approved_rows,
        "executed": values.executed,
    }


def build_flow_counts(
    stage_progress: dict[str, dict[str, int | None]],
    proposal_pending: int | None,
    proposal_approved: int | None,
    failures: Mapping[str, tuple[str, ...]] | None,
) -> dict[str, int | None]:
    """:func:`flow_counts` over raw reads -- the entry point the shell poll uses."""
    return flow_counts(_flow_values(stage_progress, proposal_pending, proposal_approved, failures))


def _flow_dag_nodes(values: _FlowValues) -> list[dict[str, Any]]:
    """The four progress-bucket flow tiles: Discover, Metadata, Analyze, Propose."""
    return [
        {
            "name": "Discover",
            "href": "/s/discover",
            "done": values.discovered,
            "total": values.discovered,
            "store_keys": {"done": "discovered", "total": "discovered"},
            "status": _flow_stage_status({"done": values.discovered}, values.discovered),
        },
        {
            "name": "Metadata",
            "href": "/s/metadata",
            "done": values.metadata["done"],
            "total": values.metadata_total,
            "store_keys": {"done": "metadataDone", "total": "metadataTotal"},
            "status": _flow_stage_status(values.metadata, values.metadata_total),
        },
        {
            "name": "Analyze",
            "href": "/s/analyze",
            "done": values.analyze["done"],
            "total": values.analyze_total,
            "store_keys": {"done": "analyzeDone", "total": "analyzeTotal"},
            "status": _flow_stage_status(values.analyze, values.analyze_total),
        },
        {
            "name": "Propose",
            "href": "/s/propose",
            "done": values.proposed,
            "total": values.convergence,
            "store_keys": {"done": "proposedDone", "total": "convergence"},
            "status": _flow_stage_status({"done": values.proposed}, values.convergence),
        },
    ]


def _flow_review_apply_nodes(values: _FlowValues) -> list[dict[str, Any]]:
    """The two decision-driven flow tiles: Review (pending proposals) and Execute (approved)."""
    return [
        {
            "name": "Review",
            "href": "/s/rename",
            "done": None,
            "total": values.pending_rows,
            "store_keys": {"done": None, "total": "pendingRows"},
            "detail_suffix": "awaiting decision",
            "detail": f"{format_count(values.pending_rows)} awaiting decision",
            "status": _flow_readiness_status(values.pending_rows, "attention"),
        },
        {
            "name": PAGE_NAMES["apply"],
            "href": "/s/apply",
            "done": values.executed,
            "total": values.approved_rows,
            "store_keys": {"done": "executed", "total": "approvedRows"},
            "detail_suffix": "approved now",
            "detail": f"{format_count(values.approved_rows)} approved now",
            "status": _flow_readiness_status(values.approved_rows, "ready"),
        },
    ]


def _build_summary_flow(
    stage_progress: dict[str, dict[str, int | None]],
    total_files: int | None,
    proposal_pending: int | None,
    proposal_approved: int | None,
    *,
    flow_read_failures: Mapping[str, tuple[str, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Build the six-node DAG flow strip (Discover through Execute) for the Summary overview.

    ``total_files`` is accepted for signature stability; the Discover node reads the discovery
    count itself so it carries that read's availability rather than a pre-coerced integer.
    """
    del total_files
    values = _flow_values(stage_progress, proposal_pending, proposal_approved, flow_read_failures)
    return _flow_dag_nodes(values) + _flow_review_apply_nodes(values)


# Proxy edge widths, in SVG user units. A measured zero gets a visible hairline of its own -- an
# unmeasured edge gets NO width at all (the template draws it dashed), so neither can pass for the other.
_EDGE_ZERO_WIDTH = 1.0
_EDGE_MIN_WIDTH = 2.0
_EDGE_MAX_WIDTH = 14.0


def _build_flow_edges(values: _FlowValues) -> list[dict[str, Any]]:
    """The flow board's edges: each one is the files holding its SOURCE stage's output.

    Every edge's ``value`` counts files holding the output of the stage it leaves:

    - ``discover-metadata`` / ``discover-analyze`` -- files discovered (``discovery.done``), every
      file type. UNIT MISMATCH: Metadata and Analyze only ever take the music/video subset of these
      (their denominator is ``music_video_total``), so these two edges are wider than the work they
      feed; the edge says what Discover produced, not what enrichment will consume.
    - ``metadata-join`` / ``analyze-join`` -- files holding a done metadata / analysis result
      (``metadata.done`` / ``analyze.done``), music/video files only.
    - ``join-propose`` -- the convergence set: files whose metadata AND analysis are both done and
      neither in flight (``proposals.total``, the exact set proposal generation batches from). It is
      never reconstructed from the two parents -- not their minimum, sum or an invented intersection.
    - ``propose-review`` -- files in the convergence set holding a proposal (``proposals.done``).
    - ``review-apply`` -- DISTINCT files with an approved proposal awaiting execution
      (``execute.total``). This is a file count, not ``ProposalStats.approved`` (proposal rows,
      shown as "approved now" on the Execute node), and neither is the convergence count.

    ``width`` is a proxy scaled linearly against the widest measured edge. A measured zero has the
    hairline :data:`_EDGE_ZERO_WIDTH` and ``state`` ``"empty"``; an unmeasured edge keeps
    ``value`` and ``width`` ``None`` and ``state`` ``"unknown"`` -- it is never drawn as zero.
    """
    specs = (
        ("discover-metadata", "Discover", "Metadata", values.discovered, "files discovered", "discovered"),
        ("discover-analyze", "Discover", "Analyze", values.discovered, "files discovered", "discovered"),
        ("metadata-join", "Metadata", "Propose", values.metadata["done"], "files with metadata", "metadataDone"),
        ("analyze-join", "Analyze", "Propose", values.analyze["done"], "files analyzed", "analyzeDone"),
        ("join-propose", "Metadata + Analyze", "Propose", values.convergence, "files with both, ready to propose", "convergence"),
        ("propose-review", "Propose", "Review", values.proposed, "files with a proposal", "proposedDone"),
        ("review-apply", "Review", PAGE_NAMES["apply"], values.approved_files, "files approved to execute", "approvedFiles"),
    )
    widest = max((spec[3] for spec in specs if spec[3]), default=0)
    edges = []
    for edge_id, source, target, value, unit, key in specs:
        if value is None:
            width, state = None, "unknown"
        elif value == 0:
            width, state = _EDGE_ZERO_WIDTH, "empty"
        else:
            width, state = round(_EDGE_MIN_WIDTH + (_EDGE_MAX_WIDTH - _EDGE_MIN_WIDTH) * value / widest, 1), "flow"
        edges.append({"id": edge_id, "source": source, "target": target, "value": value, "key": key, "unit": unit, "width": width, "state": state})
    return edges


# The flow node an attention item's scope, or a recommendation's destination, points at.
_ATTENTION_SCOPE_NODE = {"metadata": "Metadata", "analyze": "Analyze"}
_RECOMMENDED_HREF_NODE = {
    "/s/discover": "Discover",
    "/s/metadata": "Metadata",
    "/s/analyze": "Analyze",
    "/s/propose": "Propose",
    "/s/rename": "Review",
    "/s/apply": PAGE_NAMES["apply"],
}


def _derive_bottleneck(attention: list[dict[str, str | int]], recommended: dict[str, str], *, attention_known: bool = True) -> dict[str, Any]:
    """The one flow callout, taken from the SAME signals as Needs Attention and the recommendation.

    It ranks nothing itself. Unmeasured attention -> the unavailable recommendation, no node;
    otherwise the TOP attention item verbatim (its node from its scope -- a pipeline-wide item
    outlines none); otherwise the recommendation and the node it opens. "Pipeline caught up"
    opens no stage, so it outlines none. Call it on the FINAL state, after the outer
    ``attention_known`` override, so it can never disagree with what the page shows.
    """
    if not attention_known:
        return {"source": "unavailable", "id": None, "node": None, "title": recommended["title"], "detail": recommended["detail"], "tone": "neutral"}
    if attention:
        top = attention[0]
        return {
            "source": "attention",
            "id": top["id"],
            "node": _ATTENTION_SCOPE_NODE.get(str(top.get("scope", ""))),
            "title": top["title"],
            "detail": top["detail"],
            "tone": top["tone"],
        }
    return {
        "source": "recommended",
        "id": None,
        "node": _RECOMMENDED_HREF_NODE.get(recommended["href"]),
        "title": recommended["title"],
        "detail": recommended["detail"],
        "tone": recommended["tone"],
    }


def _summary_failure_totals(metadata: dict[str, int | None], analyze: dict[str, int | None], orphan_counts: dict[str, int]) -> tuple[int, int, int]:
    """Return ``(metadata_failed, analyze_failed, orphan_total)`` -- shared by the attention
    queue and the ``is_degraded`` flag so neither can drift from the other's count.
    """
    metadata_failed = _stage_count(metadata, "failed")
    analyze_failed = _stage_count(analyze, "failed")
    orphan_total = int(orphan_counts.get("metadata", 0)) + int(orphan_counts.get("analyze", 0))
    return metadata_failed, analyze_failed, orphan_total


def _build_attention_items(
    metadata: dict[str, int | None],
    analyze: dict[str, int | None],
    failures: tuple[int, int, int],
    inputs: SummaryOverviewInputs,
) -> list[dict[str, str | int]]:
    """Compatibility wrapper over the shared attention policy; items and hrefs remain identical."""
    return derive_attention_items(metadata, analyze, failures, inputs)


def _recommended_when_attention_or_empty(attention: list[dict[str, str | int]], total_files: int | None) -> dict[str, str] | None:
    """Point to the sole incident list, or offer the empty-collection prompt -- else ``None``.

    Only a MEASURED empty collection is empty: an unmeasured discovery count (``None``) is not.
    """
    if attention:
        return {
            "title": "Review pipeline incidents",
            "detail": "Inspect the causes and recovery actions in Needs Attention below.",
            "href": "#needs-attention-title",
            "action": "Review incidents",
            "tone": "attention",
        }
    if total_files == 0:
        return {
            "title": "Discover the collection",
            "detail": "Run a scan from a configured file-server agent to populate the collection.",
            "href": "/s/discover",
            "action": open_label("discover"),
            "tone": "accent",
        }
    return None


def _recommended_when_stage_actionable(
    metadata: dict[str, int | None], analyze: dict[str, int | None], stage_paused: dict[str, bool]
) -> dict[str, str] | None:
    """A stage that can be started or a paused stage with pending work -- else ``None``."""
    if _stage_ready_to_start_work(metadata, stage_paused["metadata"]):
        return {
            "title": "Start metadata enrichment",
            "detail": "Metadata and analysis are independent and can run in parallel.",
            "href": "/s/metadata",
            "action": open_label("metadata"),
            "tone": "accent",
        }
    if _stage_ready_to_start_work(analyze, stage_paused["analyze"]):
        return {
            "title": "Start audio analysis",
            "detail": "Analysis can run independently of metadata enrichment.",
            "href": "/s/analyze",
            "action": open_label("analyze"),
            "tone": "accent",
        }
    if _stage_paused_with_pending_work(metadata, stage_paused["metadata"]):
        return {
            "title": "Metadata enrichment is paused",
            "detail": "Review the pause before resuming metadata work.",
            "href": "/s/metadata",
            "action": open_label("metadata"),
            "tone": "attention",
        }
    if _stage_paused_with_pending_work(analyze, stage_paused["analyze"]):
        return {
            "title": "Audio analysis is paused",
            "detail": "Review the pause before resuming analysis work.",
            "href": "/s/analyze",
            "action": open_label("analyze"),
            "tone": "attention",
        }
    return None


def _recommended_default(
    metadata: dict[str, int | None],
    analyze: dict[str, int | None],
    proposals: dict[str, int | None],
    proposal_pending: int,
    proposal_approved: int,
) -> dict[str, str]:
    """The fallback recommendation chain once nothing above needs attention: in-flight work,
    then propose/review/execute in pipeline order, then the caught-up terminal state.
    """
    if _stage_count(metadata, "in_flight") or _stage_count(analyze, "in_flight"):
        active_stage = "metadata" if _stage_count(metadata, "in_flight") else "analyze"
        return {
            "title": "Enrichment is in progress",
            "detail": "Current work is already running; monitor it before starting the next dependent stage.",
            "href": f"/s/{active_stage}",
            "action": open_label(active_stage),
            "tone": "accent",
        }
    if int(proposals["done"] or 0) < int(proposals["total"] or 0):
        return {
            "title": "Generate proposals",
            "detail": "Files with completed metadata and analysis are ready for proposal generation.",
            "href": "/s/propose",
            "action": open_label("propose"),
            "tone": "accent",
        }
    if proposal_pending:
        return {
            "title": "Review proposed changes",
            "detail": f"{format_count(proposal_pending)} proposal(s) await an operator decision.",
            "href": "/s/rename",
            "action": open_label("rename"),
            "tone": "attention",
        }
    if proposal_approved:
        return {
            "title": "Execute approved changes",
            "detail": f"{proposal_approved} approved proposal(s) are ready to apply.",
            "href": "/s/apply",
            "action": open_label("apply"),
            "tone": "attention",
        }
    return {
        "title": "Pipeline caught up",
        "detail": "No failures, recovery candidates, pending reviews, or approved changes need action.",
        "href": "/s/files",
        "action": open_label("files"),
        "tone": "success",
    }


def _derive_recommended_action(
    attention: list[dict[str, str | int]],
    total_files: int | None,
    stage_progress: dict[str, dict[str, int | None]],
    proposal_pending: int,
    proposal_approved: int,
    stage_paused: dict[str, bool],
) -> dict[str, str]:
    """Pick the single "what to do next" card -- first match wins, same priority order as
    the original elif chain: an attention item or empty collection, then an actionable or
    paused stage, then the pipeline-order fallback.
    """
    metadata = stage_progress["metadata"]
    analyze = stage_progress["analyze"]
    proposals = stage_progress["proposals"]
    return (
        _recommended_when_attention_or_empty(attention, total_files)
        or _recommended_when_stage_actionable(metadata, analyze, stage_paused)
        or _recommended_default(metadata, analyze, proposals, proposal_pending, proposal_approved)
    )


def _derive_summary_overview(stage_progress: dict[str, dict[str, int | None]], inputs: SummaryOverviewInputs) -> dict[str, Any]:
    """Derive display-only overview state without introducing new pipeline semantics.

    phaze-g9gua: the flow, its edges and the displayed totals carry each read's availability --
    an unmeasured count is ``None``, never the read's fallback zero -- while attention and the
    recommendation keep their legacy inputs. ``bottleneck`` is derived from that attention and
    recommendation (:func:`_derive_bottleneck`); :func:`_build_summary_context` re-derives it after
    its unknown-attention override.
    """
    metadata = stage_progress["metadata"]
    analyze = stage_progress["analyze"]
    values = _flow_values(stage_progress, inputs.proposal_pending, inputs.proposal_approved, inputs.flow_read_failures)
    total_files = values.discovered

    flow = _flow_dag_nodes(values) + _flow_review_apply_nodes(values)
    failures = _summary_failure_totals(metadata, analyze, inputs.orphan_counts)
    attention = _build_attention_items(metadata, analyze, failures, inputs)
    recommended = _derive_recommended_action(
        attention, total_files, stage_progress, inputs.proposal_pending, inputs.proposal_approved, inputs.stage_paused
    )
    return {
        "total_files": total_files,
        "resolved_enrichment": inputs.enriched_count,
        "proposal_pending": values.pending_rows,
        "proposal_approved": values.approved_rows,
        "flow": flow,
        "flow_counts": flow_counts(values),
        "flow_edges": _build_flow_edges(values),
        "attention": attention,
        "recommended": recommended,
        "bottleneck": _derive_bottleneck(attention, recommended),
        "recent": {
            "live": inputs.analysis_live,
            "today": inputs.analyses_today,
            "lifetime": inputs.analyses_lifetime,
        },
        "is_empty": total_files == 0,
        "is_partially_configured": inputs.active_fileservers == 0,
        "is_degraded": bool(attention),
    }


async def _get_summary_aggregates(session: AsyncSession) -> dict[str, int | None]:
    """Read the enriched-file intersection and active file-server count in one statement.

    phaze-mvq8z.17: ``enriched`` must be the intersection of the SAME "done" bucket
    :func:`~phaze.services.pipeline.get_stage_progress` reports for the Analyze/Metadata DAG
    tiles -- not bare ``done_clause``. ``stage_status_case``'s ladder puts ``in_flight`` ABOVE
    ``done`` (``services/stage_status.py``), so a file mid-re-analysis (dashboard "Run Analysis",
    the reanalysis backfill, or a reboot re-enqueue -- no ``process_file`` producer clears
    ``AnalysisResult.analysis_completed_at``) still carries its STALE completion timestamp
    alongside a live ``scheduling_ledger`` row: bare ``done_clause(ANALYZE)`` stays True for it,
    but the analyze bucket's "done" count correctly excludes it. Without the ``~inflight_clause``
    conjuncts here, FULLY ENRICHED could read HIGHER than the Analyze/Metadata "done" tiles it is
    supposed to be a subset of, in a single quiescent snapshot -- not merely a cross-session
    timing artefact. This mirrors the fix phaze-3542b already applied to
    ``_proposal_pending_clauses`` for the SAME reason; see that predicate's comment for the
    ledger-leak cost this conjunct accepts (self-clears via ``ledger_reaper`` within ~5 minutes).
    """
    enriched = (
        select(func.count(FileRecord.id))
        .where(
            done_clause(Stage.METADATA),
            ~inflight_clause(Stage.METADATA),
            done_clause(Stage.ANALYZE),
            ~inflight_clause(Stage.ANALYZE),
        )
        .scalar_subquery()
    )
    active_fileservers = (
        select(func.count(Agent.id)).where(Agent.kind == "fileserver", Agent.revoked_at.is_(None), Agent.last_seen_at.is_not(None)).scalar_subquery()
    )
    try:
        async with session.begin_nested():
            row = (await session.execute(select(enriched, active_fileservers))).one()
    except Exception:
        # Broad by design: this is a single aggregate SELECT feeding the Summary overview's
        # "enriched" / "active file-servers" tiles. ANY failure here (driver error, pool
        # exhaustion, transient network blip) degrades to None-valued tiles rather than 500
        # the whole overview render -- the same degrade-safe contract every other Summary
        # source in _build_summary_context follows.
        return {"enriched": None, "active_fileservers": None}
    return {
        "enriched": int(row[0] or 0),
        "active_fileservers": int(row[1] or 0),
    }


async def _build_summary_context(app_state: Any, session: AsyncSession) -> dict[str, Any]:
    """Keep Summary's legacy values while exposing whether its attention snapshot was measured."""
    with capture_read_degradations() as failures:
        context = await _read_summary_context(app_state, session)
    context["summary"]["attention_known"] = context["summary"]["attention_known"] and not failures
    if not context["summary"]["attention_known"]:
        context["summary"]["recommended"] = {
            "title": "Inspect unavailable status",
            "detail": "A pipeline measurement is unavailable. Inspect current sources before declaring the pipeline caught up.",
            "href": "#needs-attention-title",
            "action": "Inspect status",
            "tone": "neutral",
        }
    # phaze-g9gua: derived from the FINAL attention state, so the flow callout can never keep a
    # caught-up or stage bottleneck the override above has just withdrawn.
    context["summary"]["bottleneck"] = _derive_bottleneck(
        context["summary"]["attention"], context["summary"]["recommended"], attention_known=context["summary"]["attention_known"]
    )
    return context


async def _read_summary_context(app_state: Any, session: AsyncSession) -> dict[str, Any]:
    """Read independent Summary sources in own sessions under the shared stats fan-out cap."""
    fanout = _stats_fanout()
    # One map per snapshot: each flow read records its own fallbacks under its own source label, so a
    # concurrent sibling's failure -- or another Summary render's -- is never attributed to it.
    flow_read_failures: dict[str, tuple[str, ...]] = {}
    (
        stage_progress,
        proposal_stats,
        stalled_analyses,
        inadmissible_count,
        awaiting_cloud_count,
        cloud_phases,
        stage_controls,
        aggregates,
        analysis_live,
        analysis_activity,
    ) = cast(
        "tuple[dict[str, dict[str, int | None]], ProposalStats, int, int, int, dict[str, int], dict[str, dict[str, int | bool]], dict[str, int | None], int | None, dict[str, int | None]]",
        await asyncio.gather(
            # get_stage_progress already owns its bounded per-node sessions; wrapping it would hold
            # a semaphore slot while its children wait for that same semaphore.
            get_stage_progress(session, read_failures=flow_read_failures),
            measure_read(
                flow_read_failures,
                "proposal_stats",
                _read_in_own_session(
                    fanout,
                    get_proposal_stats,
                    ProposalStats(total=0, pending=0, approved=0, executed=0, rejected=0, failed=0, avg_confidence=None),
                ),
            ),
            _read_in_own_session(fanout, get_analysis_stalled_count, 0),
            _read_in_own_session(fanout, get_inadmissible_count, 0),
            _read_in_own_session(fanout, get_awaiting_cloud_count, 0),
            _read_in_own_session(
                fanout,
                get_cloud_phase_counts,
                {"queued_behind_quota": 0, "admitted": 0, "running": 0, "finished": 0},
            ),
            _read_in_own_session(
                fanout,
                get_stage_controls,
                {"metadata": {"paused": False, "priority": 50}, "analyze": {"paused": False, "priority": 50}},
            ),
            _read_in_own_session(
                fanout,
                _get_summary_aggregates,
                cast("dict[str, int | None]", {"enriched": None, "active_fileservers": None}),
            ),
            _read_in_own_session(fanout, lambda own_session: get_analysis_live_count(own_session, app_state), None),
            _read_in_own_session(
                fanout,
                get_analysis_activity_counts,
                cast("dict[str, int | None]", {"today": None, "lifetime": None}),
            ),
        ),
    )
    awaiting_hold_reason = await _read_in_own_session(fanout, derive_cloud_hold_reason, "held") if awaiting_cloud_count else None
    lanes = await _read_in_own_session(
        fanout, lambda own_session: get_backend_lane_snapshot(own_session, app_state), cast("list[dict[str, Any]]", [])
    )
    inputs = SummaryOverviewInputs(
        proposal_pending=proposal_stats.pending,
        proposal_approved=proposal_stats.approved,
        active_fileservers=aggregates["active_fileservers"],
        orphan_counts=get_cached_stage_orphan_counts(),
        stalled_analyses=stalled_analyses,
        inadmissible_count=inadmissible_count,
        awaiting_cloud_count=awaiting_cloud_count,
        awaiting_hold_reason=awaiting_hold_reason,
        queued_behind_quota_count=cloud_phases["queued_behind_quota"],
        stage_paused={stage: bool(stage_controls[stage]["paused"]) for stage in ("metadata", "analyze")},
        enriched_count=aggregates["enriched"],
        analysis_live=analysis_live,
        analyses_today=analysis_activity["today"],
        analyses_lifetime=analysis_activity["lifetime"],
        lanes=lanes,
        flow_read_failures=flow_read_failures,
    )
    overview = _derive_summary_overview(stage_progress, inputs)
    overview["attention_known"] = (
        aggregates["active_fileservers"] is not None
        and all(bool(stage_progress[stage].get("available", 1)) for stage in ("metadata", "analyze"))
        and stage_orphan_counts_known()
        and analysis_attention_known(lanes, awaiting_cloud_count, awaiting_hold_reason, aggregates["active_fileservers"])
    )
    return {"summary": overview}
