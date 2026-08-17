"""Pipeline orchestration router -- trigger endpoints and dashboard UI.

phaze-oau1o: this was one 3,233-line module. It is now a package whose submodules are split by
ROUTE FAMILY, re-exported here so the split is invisible to every importer:
``from phaze.routers.pipeline import router`` (main.py), ``FILES_SORT`` (routers/shell.py) and
``build_dashboard_context`` (routers/shell.py) all resolve exactly as before.

**There is still exactly ONE ``router``.** It is created in :mod:`._common` and every submodule
decorates onto that same object, so importing this package mounts all 30 routes at their unchanged
paths. Submodules are imported below purely for that side effect; the import ORDER is the route
REGISTRATION order. No two routes in this package share a matchable path shape (no ``/pipeline/{x}``
wildcard exists at any position where a literal sibling could be shadowed), so the order is not
load-bearing -- but keep it grouped as-is rather than alphabetised, and re-check that claim before
adding any path-parameter segment directly under ``/pipeline/``.

**Patch targets moved (the one thing this split can break).** A name a submodule imported is bound
in THAT submodule's namespace, so ``patch("phaze.routers.pipeline.get_backend_lane_snapshot")`` no
longer reaches the code under test -- it must name the owning submodule, e.g.
``phaze.routers.pipeline.dashboard.get_backend_lane_snapshot``. Third-party and service imports are
deliberately NOT re-exported here so that a stale target of that form raises ``AttributeError``
instead of silently patching a name nothing reads. The re-exports below are only names this package
DEFINES; patching one of those on the facade is still a silent no-op, so patch the submodule.
"""

from __future__ import annotations

from phaze.routers.pipeline._common import (
    _ENRICH_STAGE_LABELS,
    _NO_ACTIVE_AGENT_MESSAGE,
    TEMPLATES_DIR,
    _background_tasks,
    _stage_pill_oob,
    logger,
    router,
    templates,
)
from phaze.routers.pipeline.analysis import (
    _analysis_file_ids_scope,
    _enqueue_analysis_jobs,
    _ledger_keys_scope,
    _retry_analysis_group,
    _route_discovered_by_duration,
    _scheduling_ledger_cas_delete_stmt,
    retry_analysis_failed,
    retry_analysis_failed_file,
    trigger_analysis,
    trigger_analysis_ui,
)
from phaze.routers.pipeline.backfill import (
    trigger_backfill_cloud,
)
from phaze.routers.pipeline.dashboard import (
    _NODE_COMPLETED_FNS,
    _build_dag_context,
    _derive_stats,
    _read_pipeline_counters,
    _reconciled_done,
    build_dashboard_context,
    dashboard,
    pipeline_stats_partial,
)
from phaze.routers.pipeline.extraction import (
    _enqueue_extraction_jobs,
    retry_metadata_failed,
    retry_metadata_failed_file,
    trigger_extraction_ui,
    trigger_metadata_extraction,
)
from phaze.routers.pipeline.files import (
    _PENDING_SORTS,
    _PENDING_STAGES,
    _VALID_BUCKETS,
    FILES_SORT,
    TRACKLIST_SETS_SORT,
    analyze_files_fragment,
    pending_files_fragment,
    pipeline_files,
    tracklist_sets_fragment,
)
from phaze.routers.pipeline.lanes import (
    lane_detail,
)
from phaze.routers.pipeline.proposals import (
    _enqueue_proposal_jobs,
    trigger_proposals,
    trigger_proposals_ui,
)
from phaze.routers.pipeline.recovery import (
    _recovery_state,
    _run_recovery,
    recover_status_ui,
    trigger_recover_ui,
)
from phaze.routers.pipeline.skip import (
    _STAGE_TRACE_LABELS,
    _eligibility_trace_context,
    _force_skip_file_exists,
    _force_skip_no_op_toast,
    _has_approved_proposal,
    _one_stage_scalars,
    eligibility_trace,
    force_skip_stage,
)
from phaze.routers.pipeline.tracklists import (
    _enqueue_match_jobs,
    _render_drain_status,
    arm_tracklist_drain_ui,
    disarm_tracklist_drain_ui,
    prioritize_tracklist_lookup_ui,
    refresh_tracklist_lookup_ui,
    run_tracklist_drain_ui,
    tracklist_drain_status_ui,
    trigger_match_tracklists_ui,
    unprioritize_tracklist_lookup_ui,
)


__all__ = [
    "FILES_SORT",
    "TEMPLATES_DIR",
    "TRACKLIST_SETS_SORT",
    "_ENRICH_STAGE_LABELS",
    "_NODE_COMPLETED_FNS",
    "_NO_ACTIVE_AGENT_MESSAGE",
    "_PENDING_SORTS",
    "_PENDING_STAGES",
    "_STAGE_TRACE_LABELS",
    "_VALID_BUCKETS",
    "_analysis_file_ids_scope",
    "_background_tasks",
    "_build_dag_context",
    "_derive_stats",
    "_eligibility_trace_context",
    "_enqueue_analysis_jobs",
    "_enqueue_extraction_jobs",
    "_enqueue_match_jobs",
    "_enqueue_proposal_jobs",
    "_force_skip_file_exists",
    "_force_skip_no_op_toast",
    "_has_approved_proposal",
    "_ledger_keys_scope",
    "_one_stage_scalars",
    "_read_pipeline_counters",
    "_reconciled_done",
    "_recovery_state",
    "_render_drain_status",
    "_retry_analysis_group",
    "_route_discovered_by_duration",
    "_run_recovery",
    "_scheduling_ledger_cas_delete_stmt",
    "_stage_pill_oob",
    "analyze_files_fragment",
    "arm_tracklist_drain_ui",
    "build_dashboard_context",
    "dashboard",
    "disarm_tracklist_drain_ui",
    "eligibility_trace",
    "force_skip_stage",
    "lane_detail",
    "logger",
    "pending_files_fragment",
    "pipeline_files",
    "pipeline_stats_partial",
    "prioritize_tracklist_lookup_ui",
    "recover_status_ui",
    "refresh_tracklist_lookup_ui",
    "retry_analysis_failed",
    "retry_analysis_failed_file",
    "retry_metadata_failed",
    "retry_metadata_failed_file",
    "router",
    "run_tracklist_drain_ui",
    "templates",
    "tracklist_drain_status_ui",
    "tracklist_sets_fragment",
    "trigger_analysis",
    "trigger_analysis_ui",
    "trigger_backfill_cloud",
    "trigger_extraction_ui",
    "trigger_match_tracklists_ui",
    "trigger_metadata_extraction",
    "trigger_proposals",
    "trigger_proposals_ui",
    "trigger_recover_ui",
    "unprioritize_tracklist_lookup_ui",
]
