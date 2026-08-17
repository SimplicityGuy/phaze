"""Pipeline orchestration service -- stage counts and file queries.

This package was ONE 2,695-line module until phaze-vsqpr. It was never knotted (max cyclomatic
complexity 9, max nesting 4); it was undifferentiated bulk -- ~65 read-model query functions in one
flat namespace, which repowise measured as the widest co-change scatter in the repo (94 distinct
files). The split below draws domain boundaries through that flat namespace. It is a PURE MOVE: not
one statement of behaviour changed, and every function landed in its new module byte-identical.

**This module is a facade.** It re-exports the COMPLETE surface the single module used to expose --
public names AND the private helpers that other packages, scripts and tests already import -- so
every existing ``from phaze.services.pipeline import <name>`` keeps working untouched. Adding a name
to a submodule is not enough; export it here too, or you have created a second, partial surface.

Where things live:

- :mod:`~phaze.services.pipeline.common` -- the corpus scope, the cloud double-dispatch guard, and
  the degrade-safe COUNT wrapper everything else composes against.
- :mod:`~phaze.services.pipeline.buckets` -- the derived five-bucket per-stage counts + the D-01a
  ``orphaned`` carve-out, corpus-wide and per-agent.
- :mod:`~phaze.services.pipeline.jobs` -- read-only probes of the SAQ-owned ``saq_jobs`` table.
- :mod:`~phaze.services.pipeline.reconciliation` -- scanned / deduped / unique reconciliation.
- :mod:`~phaze.services.pipeline.agents` -- live per-agent and fleet activity reads.
- :mod:`~phaze.services.pipeline.stages` -- the stage-progress fan-out and the stage controls.
- :mod:`~phaze.services.pipeline.orphans` -- the orphan/stuck counts and their off-request cache.
- :mod:`~phaze.services.pipeline.analyze` -- the Analyze workspace read model.
- :mod:`~phaze.services.pipeline.cloud` -- the cloud lane read model.
- :mod:`~phaze.services.pipeline.failures` -- the terminal analyze-failure bucket.
- :mod:`~phaze.services.pipeline.pending` -- the per-stage pending sets (enqueue + render twins).
- :mod:`~phaze.services.pipeline.proposals` -- the proposal convergence gate.
- :mod:`~phaze.services.pipeline.tracklists` -- the tracklist / match read model.
- :mod:`~phaze.services.pipeline.files` -- the scannable per-file overview.

**Four names are deliberately NOT re-exported** -- ``stages._STATS_FANOUT``,
``stages._STATS_FANOUT_CACHE``, ``orphans._orphan_cache`` and ``orphans._orphan_cache_expires_at``.
All four are module globals that get REBOUND at runtime (the fan-out test override; the orphan
cache's whole-dict rebind). A re-export binds the value once at import and would then never track
the rebinding, so ``pipeline._orphan_cache`` would silently report a stale dict and a
``monkeypatch.setattr("phaze.services.pipeline._STATS_FANOUT", ...)`` would silently do nothing.
Omitting them turns both into a loud ``AttributeError`` instead. Patch and read them on their OWN
module (``phaze.services.pipeline.stages`` / ``phaze.services.pipeline.orphans``).

The import order below is dependency order; it is what keeps the intra-package imports acyclic.
"""

from __future__ import annotations

from phaze.services.pipeline.agents import (
    _AGENT_RECENT_SCANS_N,
    count_active_agents,
    get_agent_lane_depths,
    get_agent_recent_scans,
    get_queue_activity,
    queue_progress_percent,
)
from phaze.services.pipeline.analyze import (
    _ANALYZE_ACTIVE_CLOUD_STATUSES,
    _ANALYZE_COMPLETIONS_WINDOW,
    ANALYZE_FILTER_ALL,
    ANALYZE_FILTER_AWAITING,
    ANALYZE_FILTER_COMPLETED,
    ANALYZE_FILTER_FAILED,
    ANALYZE_FILTER_IN_FLIGHT,
    ANALYZE_FILTERS,
    AnalyzeFilesPage,
    _analyze_active_where,
    _analyze_files_select,
    _analyze_status_where,
    _project_analyze_rows,
    analyze_lanes_content_hash,
    derive_file_lane,
    get_analyze_files_page,
    get_analyze_working_set,
)
from phaze.services.pipeline.buckets import (
    ORPHANED_BUCKET,
    StageBucketSnapshot,
    _agent_stage_buckets,
    _empty_buckets,
    _metadata_status_stmt,
    _safe_bucket_counts,
    _safe_bucket_snapshot,
    _safe_orphan_split,
    _stage_bucket_stmt,
)
from phaze.services.pipeline.cloud import (
    _backfill_candidates_stmt,
    _cloud_window_clauses,
    _kueue_backend_ids,
    count_backfill_candidates,
    get_awaiting_cloud_count,
    get_backfill_candidates,
    get_cloud_phase_counts,
    get_cloud_staging_candidates,
    get_inadmissible_count,
    get_pushed_count,
    get_pushing_count,
)
from phaze.services.pipeline.common import (
    _ACTIVE_CLOUD_STATUSES,
    _BUSY_FUNCTION_TO_STAGE,
    MUSIC_VIDEO_TYPES,
    _safe_count,
)
from phaze.services.pipeline.failures import (
    _STALL_ERROR_PREFIX,
    get_analysis_failed_count,
    get_analysis_failed_files,
    get_analysis_stalled_count,
)
from phaze.services.pipeline.files import (
    _FILES_PAGE_STAGES,
    _ORPHAN_DETAIL_STAGES,
    FilesPage,
    FilesPageRow,
    _files_page_stmt,
    get_file_orphan_details,
    get_file_stage_buckets,
    get_files_page,
)
from phaze.services.pipeline.jobs import (
    _INFLIGHT_COUNT_SQL,
    _LIVE_KEYS_SQL,
    _MATCH_BUSY_FUNCTION,
    _PROPOSALS_BUSY_FUNCTION,
    _STAGE_BUSY_SQL,
    count_inflight_jobs,
    get_live_job_keys,
    get_match_busy_count,
    get_proposal_busy_count,
    get_stage_busy_counts,
)
from phaze.services.pipeline.orphans import (
    _ORPHAN_TTL_SECONDS,
    _compute_stage_orphan_counts,
    get_cached_stage_orphan_counts,
    get_stage_orphan_counts,
    refresh_stage_orphan_counts,
)
from phaze.services.pipeline.pending import (
    MetadataActivitySummary,
    MetadataSelectionSummary,
    MetadataStatusSnapshot,
    PendingFilesPage,
    _metadata_activity_stmt,
    _metadata_pending_stmt,
    _pending_page_stmt,
    get_discovered_files_with_duration,
    get_metadata_activity_summary,
    get_metadata_failed_files,
    get_metadata_pending_files,
    get_metadata_selection_summary,
    get_metadata_status_snapshot,
    get_pending_files_page,
)
from phaze.services.pipeline.proposals import (
    _proposal_pending_clauses,
    count_proposal_pending_files,
    get_proposal_pending_batches,
)
from phaze.services.pipeline.reconciliation import (
    deduped_count,
    get_agent_reconciliations,
    get_global_reconciliation,
    get_scanned_total,
)
from phaze.services.pipeline.stages import (
    _DEFAULT_CONTROLS,
    _STAGE_ACTIVITY_SQL,
    _STATS_FANOUT_CAP,
    StageActivitySnapshot,
    _empty_stage_activity,
    _read_in_own_session,
    _stats_fanout,
    get_stage_activity_counts,
    get_stage_activity_snapshot,
    get_stage_controls,
    get_stage_progress,
)
from phaze.services.pipeline.tracklists import (
    _tracklist_sets_page_stmt,
    get_match_pending_tracklists,
    get_tracklist_sets_page,
    get_untracked_files,
)


__all__ = [
    "ANALYZE_FILTERS",
    "ANALYZE_FILTER_ALL",
    "ANALYZE_FILTER_AWAITING",
    "ANALYZE_FILTER_COMPLETED",
    "ANALYZE_FILTER_FAILED",
    "ANALYZE_FILTER_IN_FLIGHT",
    "MUSIC_VIDEO_TYPES",
    "ORPHANED_BUCKET",
    "_ACTIVE_CLOUD_STATUSES",
    "_AGENT_RECENT_SCANS_N",
    "_ANALYZE_ACTIVE_CLOUD_STATUSES",
    "_ANALYZE_COMPLETIONS_WINDOW",
    "_BUSY_FUNCTION_TO_STAGE",
    "_DEFAULT_CONTROLS",
    "_FILES_PAGE_STAGES",
    "_INFLIGHT_COUNT_SQL",
    "_LIVE_KEYS_SQL",
    "_MATCH_BUSY_FUNCTION",
    "_ORPHAN_DETAIL_STAGES",
    "_ORPHAN_TTL_SECONDS",
    "_PROPOSALS_BUSY_FUNCTION",
    "_STAGE_ACTIVITY_SQL",
    "_STAGE_BUSY_SQL",
    "_STALL_ERROR_PREFIX",
    "_STATS_FANOUT_CAP",
    "AnalyzeFilesPage",
    "FilesPage",
    "FilesPageRow",
    "MetadataActivitySummary",
    "MetadataSelectionSummary",
    "MetadataStatusSnapshot",
    "PendingFilesPage",
    "StageActivitySnapshot",
    "StageBucketSnapshot",
    "_agent_stage_buckets",
    "_analyze_active_where",
    "_analyze_files_select",
    "_analyze_status_where",
    "_backfill_candidates_stmt",
    "_cloud_window_clauses",
    "_compute_stage_orphan_counts",
    "_empty_buckets",
    "_empty_stage_activity",
    "_files_page_stmt",
    "_kueue_backend_ids",
    "_metadata_activity_stmt",
    "_metadata_pending_stmt",
    "_metadata_status_stmt",
    "_pending_page_stmt",
    "_project_analyze_rows",
    "_proposal_pending_clauses",
    "_read_in_own_session",
    "_safe_bucket_counts",
    "_safe_bucket_snapshot",
    "_safe_count",
    "_safe_orphan_split",
    "_stage_bucket_stmt",
    "_stats_fanout",
    "_tracklist_sets_page_stmt",
    "analyze_lanes_content_hash",
    "count_active_agents",
    "count_backfill_candidates",
    "count_inflight_jobs",
    "count_proposal_pending_files",
    "deduped_count",
    "derive_file_lane",
    "get_agent_lane_depths",
    "get_agent_recent_scans",
    "get_agent_reconciliations",
    "get_analysis_failed_count",
    "get_analysis_failed_files",
    "get_analysis_stalled_count",
    "get_analyze_files_page",
    "get_analyze_working_set",
    "get_awaiting_cloud_count",
    "get_backfill_candidates",
    "get_cached_stage_orphan_counts",
    "get_cloud_phase_counts",
    "get_cloud_staging_candidates",
    "get_discovered_files_with_duration",
    "get_file_orphan_details",
    "get_file_stage_buckets",
    "get_files_page",
    "get_global_reconciliation",
    "get_inadmissible_count",
    "get_live_job_keys",
    "get_match_busy_count",
    "get_match_pending_tracklists",
    "get_metadata_activity_summary",
    "get_metadata_failed_files",
    "get_metadata_pending_files",
    "get_metadata_selection_summary",
    "get_metadata_status_snapshot",
    "get_pending_files_page",
    "get_proposal_busy_count",
    "get_proposal_pending_batches",
    "get_pushed_count",
    "get_pushing_count",
    "get_queue_activity",
    "get_scanned_total",
    "get_stage_activity_counts",
    "get_stage_activity_snapshot",
    "get_stage_busy_counts",
    "get_stage_controls",
    "get_stage_orphan_counts",
    "get_stage_progress",
    "get_tracklist_sets_page",
    "get_untracked_files",
    "queue_progress_percent",
    "refresh_stage_orphan_counts",
]
