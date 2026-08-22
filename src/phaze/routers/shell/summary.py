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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select

from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.services.backends import derive_cloud_hold_reason, get_analysis_activity_counts, get_analysis_live_count
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
)
from phaze.services.proposal_queries import ProposalStats, get_proposal_stats
from phaze.services.stage_status import done_clause


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _stage_count(bucket: dict[str, int | None], key: str) -> int:
    """One stage-progress bucket count, treating a missing key or a NULL value alike as 0.

    The buckets come straight from the stage-progress aggregates, where a stage with no rows
    yields NULL rather than 0 and an absent stage yields no key at all. Both mean "none", so the
    coercion is the same everywhere -- and repeating ``int(bucket.get(k) or 0)`` at every read
    site is how the two spellings drift apart.
    """
    return int(bucket.get(key) or 0)


def _summary_stage_status(stage: dict[str, int | None]) -> dict[str, str | bool]:
    """Map an authoritative stage-progress bucket to the shared status vocabulary."""
    total = _stage_count(stage, "total")
    done = _stage_count(stage, "done")
    skipped = _stage_count(stage, "skipped")
    failed = _stage_count(stage, "failed")
    in_flight = _stage_count(stage, "in_flight")
    if failed:
        return {"label": "failed", "tone": "danger", "icon": "✕", "pulse": False}
    if in_flight:
        return {"label": "in flight", "tone": "accent", "icon": "●", "pulse": True}
    if total and done + skipped >= total:
        return {"label": "complete", "tone": "success", "icon": "✓", "pulse": False}
    if total:
        return {"label": "not started", "tone": "neutral", "icon": "—", "pulse": False}
    return {"label": "empty", "tone": "neutral", "icon": "—", "pulse": False}


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


def _stage_ready_to_start_work(bucket: dict[str, int | None], paused: bool) -> bool:
    """True when a stage has queued work, is not paused, and nothing is in flight yet."""
    return bool(_stage_count(bucket, "not_started") and not paused and not _stage_count(bucket, "in_flight"))


def _stage_paused_with_pending_work(bucket: dict[str, int | None], paused: bool) -> bool:
    """True when a paused stage still has not-started or in-flight work waiting behind it."""
    return bool(paused and (_stage_count(bucket, "not_started") or _stage_count(bucket, "in_flight")))


def _flow_readiness_status(count: int, ready_label: str) -> dict[str, str | bool]:
    """The Review/Apply flow tiles share one status shape: a ready pill when ``count`` is
    non-zero, else the same "caught up" pill every DAG-progress tile falls back to.
    """
    if count:
        return {"label": ready_label, "tone": "attention", "icon": "!", "pulse": False}
    return {"label": "caught up", "tone": "success", "icon": "✓", "pulse": False}


def _flow_dag_nodes(stage_progress: dict[str, dict[str, int | None]], total_files: int) -> list[dict[str, Any]]:
    """The four progress-bucket flow tiles: Discover, Metadata, Analyze, Propose."""
    metadata = stage_progress["metadata"]
    analyze = stage_progress["analyze"]
    proposals = stage_progress["proposals"]
    return [
        {
            "name": "Discover",
            "href": "/s/discover",
            "done": total_files,
            "total": total_files,
            "status": _summary_stage_status(stage_progress["discovery"]),
        },
        {
            "name": "Metadata",
            "href": "/s/metadata",
            "done": int(metadata["done"] or 0),
            "total": int(metadata["total"] or 0),
            "status": _summary_stage_status(metadata),
        },
        {
            "name": "Analyze",
            "href": "/s/analyze",
            "done": int(analyze["done"] or 0),
            "total": int(analyze["total"] or 0),
            "status": _summary_stage_status(analyze),
        },
        {
            "name": "Propose",
            "href": "/s/propose",
            "done": int(proposals["done"] or 0),
            "total": int(proposals["total"] or 0),
            "status": _summary_stage_status(proposals),
        },
    ]


def _flow_review_apply_nodes(execute: dict[str, int | None], proposal_pending: int, proposal_approved: int) -> list[dict[str, Any]]:
    """The two decision-driven flow tiles: Review (pending proposals) and Apply (approved)."""
    return [
        {
            "name": "Review",
            "href": "/s/rename",
            "done": None,
            "total": proposal_pending,
            "detail": f"{proposal_pending} awaiting decision",
            "status": _flow_readiness_status(proposal_pending, "attention"),
        },
        {
            "name": "Apply",
            "href": "/s/apply",
            "done": int(execute["done"] or 0),
            "total": proposal_approved,
            "detail": f"{proposal_approved} approved now",
            "status": _flow_readiness_status(proposal_approved, "ready"),
        },
    ]


def _build_summary_flow(
    stage_progress: dict[str, dict[str, int | None]], total_files: int, proposal_pending: int, proposal_approved: int
) -> list[dict[str, Any]]:
    """Build the six-node DAG flow strip (Discover through Apply) for the Summary overview."""
    return _flow_dag_nodes(stage_progress, total_files) + _flow_review_apply_nodes(stage_progress["execute"], proposal_pending, proposal_approved)


def _summary_failure_totals(metadata: dict[str, int | None], analyze: dict[str, int | None], orphan_counts: dict[str, int]) -> tuple[int, int, int]:
    """Return ``(metadata_failed, analyze_failed, orphan_total)`` -- shared by the attention
    queue and the ``is_degraded`` flag so neither can drift from the other's count.
    """
    metadata_failed = _stage_count(metadata, "failed")
    analyze_failed = _stage_count(analyze, "failed")
    orphan_total = int(orphan_counts.get("metadata", 0)) + int(orphan_counts.get("analyze", 0))
    return metadata_failed, analyze_failed, orphan_total


def _attention_item(priority: int, title: str, detail: str, href: str, action: str, tone: str = "attention") -> dict[str, str | int]:
    return {"priority": priority, "title": title, "detail": detail, "href": href, "action": action, "tone": tone}


def _awaiting_cloud_target(hold_reason: str) -> tuple[str, str]:
    """Resolve the (href, action) pair that best explains an awaiting-cloud hold reason."""
    if hold_reason in {"cloud routing disabled", "held — cloud routing paused (force-local)"}:
        return "/s/operations", "Open Routing"
    if hold_reason in {"held — no cloud backend reachable", "held — no fileserver agent online"} or hold_reason.startswith(
        "held — all lanes at capacity"
    ):
        return "/s/agents", "Inspect Compute"
    return "/s/analyze", "Open Analyze"


def _failure_attention_items(metadata_failed: int, analyze_failed: int, orphan_total: int, stalled_analyses: int) -> list[dict[str, str | int]]:
    """Attention items for terminal failures and orphaned work (priorities 10/11/20)."""
    items: list[dict[str, str | int]] = []
    if metadata_failed:
        items.append(
            _attention_item(10, "Metadata failures", f"{metadata_failed} file(s) need inspection or retry.", "/s/metadata", "Open Metadata", "danger")
        )
    if analyze_failed:
        stalled_detail = f"; {stalled_analyses} stopped by the progress watchdog" if stalled_analyses else ""
        items.append(
            _attention_item(
                11, "Analysis failures", f"{analyze_failed} file(s) reached terminal failure{stalled_detail}.", "/s/analyze", "Open Analyze", "danger"
            )
        )
    if orphan_total:
        items.append(
            _attention_item(
                20, "Orphaned work", f"{orphan_total} scheduled file(s) have no live job or domain result.", "/s/discover", "Open Recovery"
            )
        )
    return items


def _capacity_attention_items(
    metadata: dict[str, int | None], analyze: dict[str, int | None], inputs: SummaryOverviewInputs
) -> list[dict[str, str | int]]:
    """Attention items for cloud/compute capacity and stage pauses (priorities 30-50)."""
    items: list[dict[str, str | int]] = []
    if inputs.inadmissible_count:
        items.append(
            _attention_item(
                30,
                "Cloud jobs blocked by configuration",
                f"{inputs.inadmissible_count} active cloud job(s) are Inadmissible.",
                "/s/agents",
                "Inspect Compute",
            )
        )
    if inputs.awaiting_cloud_count:
        hold_reason = inputs.awaiting_hold_reason or "reason unavailable"
        href, action = _awaiting_cloud_target(hold_reason)
        items.append(_attention_item(40, "Files awaiting cloud routing", f"{inputs.awaiting_cloud_count} file(s): {hold_reason}.", href, action))
    if inputs.queued_behind_quota_count:
        items.append(
            _attention_item(
                41,
                "Cloud quota wait",
                f"{inputs.queued_behind_quota_count} submitted cloud job(s) are waiting for cluster quota.",
                "/s/agents",
                "Inspect Compute",
            )
        )
    for priority, stage_name, stage_label, stage_bucket in (
        (45, "metadata", "Metadata enrichment", metadata),
        (46, "analyze", "Audio analysis", analyze),
    ):
        if _stage_paused_with_pending_work(stage_bucket, inputs.stage_paused[stage_name]):
            items.append(
                _attention_item(
                    priority,
                    f"{stage_label} is paused",
                    f"{_stage_count(stage_bucket, 'not_started')} not started; {_stage_count(stage_bucket, 'in_flight')} still marked in flight.",
                    f"/s/{stage_name}",
                    f"Open {stage_label}",
                )
            )
    if inputs.active_fileservers == 0:
        items.append(
            _attention_item(
                50,
                "No file-server agent available",
                "Discovery and file-owned work cannot be dispatched until an agent checks in.",
                "/s/agents",
                "Configure Agents",
            )
        )
    return items


def _build_attention_items(
    metadata: dict[str, int | None],
    analyze: dict[str, int | None],
    failures: tuple[int, int, int],
    inputs: SummaryOverviewInputs,
) -> list[dict[str, str | int]]:
    """Assemble the priority-sorted attention-queue entries for the Summary overview."""
    metadata_failed, analyze_failed, orphan_total = failures
    items = _failure_attention_items(metadata_failed, analyze_failed, orphan_total, inputs.stalled_analyses) + _capacity_attention_items(
        metadata, analyze, inputs
    )
    items.sort(key=lambda item: int(item["priority"]))
    return items


def _recommended_when_attention_or_empty(attention: list[dict[str, str | int]], total_files: int) -> dict[str, str] | None:
    """The highest-priority attention item, or the empty-collection prompt -- else ``None``."""
    if attention:
        first = attention[0]
        return {"title": first["title"], "detail": first["detail"], "href": first["href"], "action": first["action"], "tone": first["tone"]}  # type: ignore[dict-item]
    if total_files == 0:
        return {
            "title": "Discover the collection",
            "detail": "Run a scan from a configured file-server agent to populate the collection.",
            "href": "/s/discover",
            "action": "Open Discover",
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
            "action": "Open Metadata",
            "tone": "accent",
        }
    if _stage_ready_to_start_work(analyze, stage_paused["analyze"]):
        return {
            "title": "Start audio analysis",
            "detail": "Analysis can run independently of metadata enrichment.",
            "href": "/s/analyze",
            "action": "Open Analyze",
            "tone": "accent",
        }
    if _stage_paused_with_pending_work(metadata, stage_paused["metadata"]):
        return {
            "title": "Metadata enrichment is paused",
            "detail": "Review the pause before resuming metadata work.",
            "href": "/s/metadata",
            "action": "Open Metadata",
            "tone": "attention",
        }
    if _stage_paused_with_pending_work(analyze, stage_paused["analyze"]):
        return {
            "title": "Audio analysis is paused",
            "detail": "Review the pause before resuming analysis work.",
            "href": "/s/analyze",
            "action": "Open Analyze",
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
            "action": f"Open {active_stage.title()}",
            "tone": "accent",
        }
    if int(proposals["done"] or 0) < int(proposals["total"] or 0):
        return {
            "title": "Generate proposals",
            "detail": "Files with completed metadata and analysis are ready for proposal generation.",
            "href": "/s/propose",
            "action": "Open Propose",
            "tone": "accent",
        }
    if proposal_pending:
        return {
            "title": "Review proposed changes",
            "detail": f"{proposal_pending} proposal(s) await an operator decision.",
            "href": "/s/rename",
            "action": "Open Review",
            "tone": "attention",
        }
    if proposal_approved:
        return {
            "title": "Execute approved changes",
            "detail": f"{proposal_approved} approved proposal(s) are ready to apply.",
            "href": "/s/apply",
            "action": "Open Apply",
            "tone": "attention",
        }
    return {
        "title": "Pipeline caught up",
        "detail": "No failures, recovery candidates, pending reviews, or approved changes need action.",
        "href": "/s/files",
        "action": "Browse Files",
        "tone": "success",
    }


def _derive_recommended_action(
    attention: list[dict[str, str | int]],
    total_files: int,
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
    """Derive display-only overview state without introducing new pipeline semantics."""
    metadata = stage_progress["metadata"]
    analyze = stage_progress["analyze"]
    total_files = int(stage_progress["discovery"]["done"] or 0)

    flow = _build_summary_flow(stage_progress, total_files, inputs.proposal_pending, inputs.proposal_approved)
    failures = _summary_failure_totals(metadata, analyze, inputs.orphan_counts)
    attention = _build_attention_items(metadata, analyze, failures, inputs)
    recommended = _derive_recommended_action(
        attention, total_files, stage_progress, inputs.proposal_pending, inputs.proposal_approved, inputs.stage_paused
    )
    metadata_failed, analyze_failed, orphan_total = failures

    return {
        "total_files": total_files,
        "resolved_enrichment": inputs.enriched_count,
        "proposal_pending": inputs.proposal_pending,
        "proposal_approved": inputs.proposal_approved,
        "flow": flow,
        "attention": attention,
        "recommended": recommended,
        "recent": {
            "live": inputs.analysis_live,
            "today": inputs.analyses_today,
            "lifetime": inputs.analyses_lifetime,
        },
        "is_empty": total_files == 0,
        "is_partially_configured": inputs.active_fileservers == 0,
        "is_degraded": bool(metadata_failed or analyze_failed or orphan_total or inputs.inadmissible_count or inputs.awaiting_cloud_count),
    }


async def _get_summary_aggregates(session: AsyncSession) -> dict[str, int | None]:
    """Read the enriched-file intersection and active file-server count in one statement."""
    enriched = select(func.count(FileRecord.id)).where(done_clause(Stage.METADATA), done_clause(Stage.ANALYZE)).scalar_subquery()
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
    """Read independent Summary sources in own sessions under the shared stats fan-out cap."""
    fanout = _stats_fanout()
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
            get_stage_progress(session),
            _read_in_own_session(
                fanout,
                get_proposal_stats,
                ProposalStats(total=0, pending=0, approved=0, executed=0, rejected=0, failed=0, avg_confidence=None),
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
    )
    return {"summary": _derive_summary_overview(stage_progress, inputs)}
