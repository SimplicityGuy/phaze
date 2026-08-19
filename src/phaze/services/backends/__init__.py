"""The internal ``Backend`` protocol + its three re-homed implementations (Phase 68, BACK-01/03).

This is the phase's center of gravity. It houses one ``typing.Protocol`` (design §4.2 shape --
``is_available`` / ``in_flight_count`` / ``dispatch`` / ``reconcile``) and the three implementations
``LocalBackend`` / ``ComputeAgentBackend`` / ``KueueBackend`` that **re-home** the existing staging /
push / submit / reconcile bodies verbatim -- this is a behavior-preserving refactor, NOT a rewrite.

Phase 68 was a **lay-and-prove** phase (D-02): it defined the protocol + the uniform per-backend
``in_flight_count`` substrate and proved its equivalence to the (now Phase-69-retired) global
FileState ``{PUSHING, PUSHED}`` window for the single-backend case. Phase 69 (SCHED-02) then flipped
the drain (``release_awaiting_cloud.stage_cloud_window``) onto these per-backend caps: it snapshots
each backend's ``in_flight_count`` once per tick and enforces the per-backend ``cap``. The protocol
methods are per-backend and unit-tested as such.

Decisions realized here:

* **D-01a** -- the GATE-1 asymmetry lives in per-kind ``is_available``: compute REQUIRES a live compute
  agent; Kueue deliberately probes the cluster with NO compute-agent dependency; local is always up.
* **D-02** -- ``in_flight_count`` is the uniform ``cloud_job``-derived per-backend count; the equivalence
  invariant is the characterization proof that the new substrate matches the old count.
* **D-03** -- ``dispatch`` owns BOTH the ``FileState -> PUSHING`` flip AND the ``cloud_job`` upsert in the
  SAME caller-passed session, before/with the flip, NEVER after a separate commit (Pitfall 4 limbo guard).
* **D-05** -- ``KueueBackend`` calls ``_stage_file_to_s3`` / ``kube_staging`` threaded THIS backend's
  own ``KubeConfig`` + D-06 bucket (Phase 70 MKUE-01/02 retired the ``active_kube`` / ``active_bucket``
  module-global reads: one control plane dispatches to N distinct clusters/buckets).
* **D-07** -- the raise-on-``>1``-non-local guard is Phase-69-retired from :func:`resolve_backends` (N
  non-local backends now resolve; SCHED-01). Phase 70 (MKUE-01) further generalizes
  :func:`resolved_non_local_kind` to return ``"kueue"`` for ANY-kueue registry (N Kueue backends are the
  literal MKUE-01 scenario), retaining the fail-fast only for the ambiguous compute-only ``>1`` case;
  ``cloud_enabled`` stays in config as the registry on/off gate.
* **D-10** -- the in-flight status set is ``{UPLOADING, UPLOADED, SUBMITTED, RUNNING}``.

Cron no-op discipline (T-68-05): ``is_available`` / ``dispatch`` / ``reconcile`` degrade to a clean hold
(return ``False`` / a no-op) on an absent agent or a probe failure -- they never raise out to a cron.
Secret hygiene (T-68-04): this package logs only ``{id, kind, rank, cap}``-level fields, never a
``SecretStr`` / ``*_file`` value or a kube SA token.

Layout (phaze-dr9df)
--------------------

Until 2026-08-17 all of the above lived in ONE 1,543-NLOC ``services/backends.py``: the four-bug-fix
reap loops, the registry resolution, and ~740 lines of read-only lane telemetry that shares nothing
with them but a file. It co-changed with 70 distinct files and carried 29 bug-fix commits. It is now
a package, split along BOTH axes the bug-hunt retrospective named -- per backend kind, and
dispatch / registry / read-path:

=========================== ==================================================================
:mod:`~.base`               ``Backend`` protocol, ``_BaseBackend``, ``IN_FLIGHT`` / ``STAGING``
:mod:`~.admission`          ``hold_awaiting_cloud`` (the single ``'awaiting'`` writer) + the
                            phaze-s5sz parked-``push_file``-enqueue machinery
:mod:`~.local`              ``LocalBackend``
:mod:`~.compute_agent`      ``ComputeAgentBackend`` + its stranded-SUBMITTED reaper
:mod:`~.kueue`              ``KueueBackend`` + its staging reaper and Job/Workload reconcile
:mod:`~.registry`           ``resolve_backends`` / ``resolve_compute_backend`` /
                            ``resolved_non_local_kind``
:mod:`~.lane_detail`        Phase 88 drill-in pane data (identity, depths, recent completions)
:mod:`~.lane_metrics`       phaze-5c6i2 queued / working / processed lane counters
:mod:`~.lane_snapshot`      Phase 71 lane grid, availability probes, derived hold/unreachable
=========================== ==================================================================

**This module is a pure re-export facade and the public import path stays
``phaze.services.backends``.** Every pre-split ``from phaze.services.backends import <name>``
continues to resolve, private names included -- the split deliberately did not force 40 call sites
in 70 co-changing files to churn. Import from here, not from a submodule, unless you are inside the
package.

The one thing a facade cannot preserve is ``monkeypatch.setattr("phaze.services.backends.<name>",
...)`` for a name a SUBMODULE resolves at call time: rebinding it here does not rebind the
submodule's own global. Patch the module that USES it (e.g.
``phaze.services.backends.kueue.get_settings``, ``phaze.services.backends.lane_snapshot
.resolve_backends``). Deferred/function-local importers outside the package
(``tasks.release_awaiting_cloud``, ``tasks.reconcile_cloud_jobs``) import THROUGH this facade at call
time and are unaffected.
"""

from phaze.services.backends.admission import (
    _PENDING_PUSH_FILE_ENQUEUE_KEY,
    _build_push_file_enqueue_kwargs,
    _fold_spent_budget_if_edge,
    _park_push_file_enqueue,
    _PendingPushFileEnqueue,
    _PriorChain,
    drop_pending_push_file_enqueues,
    flush_pending_push_file_enqueues,
    hold_awaiting_cloud,
)
from phaze.services.backends.base import IN_FLIGHT, STAGING, Backend, _BaseBackend
from phaze.services.backends.compute_agent import ComputeAgentBackend
from phaze.services.backends.kueue import KueueBackend
from phaze.services.backends.lane_detail import (
    _CLOUD_RECENT_COMPLETIONS_SQL,
    _LOCAL_RECENT_COMPLETIONS_SQL,
    KUEUE_NO_SAQ_QUEUE_NOTE,
    LANE_RECENT_N,
    NO_FILESERVER_AGENT_NOTE,
    LaneCompletion,
    LaneQueueDepths,
    LaneQueueIdentity,
    _completion_label,
    get_lane_queue_depths,
    get_lane_recent_completions,
    resolve_lane_queue_agent,
)
from phaze.services.backends.lane_metrics import (
    _PROCESSED_WINDOW,
    _cloud_job_succeeded_for_backend,
    _cloud_lane_active,
    _cloud_lane_queued_working,
    _lane_processed_counts,
    _local_lane_queued_working,
    _safe_count_or_none,
    get_analysis_activity_counts,
    get_analyze_queue_totals,
)
from phaze.services.backends.lane_snapshot import (
    _HOLD_REASON_DEGRADED,
    _PROBE_TIMEOUT_SEC,
    _ZERO_ADMISSION,
    _admission_by_backend_id,
    _get_backend_routing_snapshot,
    _kind_of,
    _probe_availability,
    _probe_one,
    derive_cloud_hold_reason,
    derive_localqueue_unreachable,
    get_analysis_live_count,
    get_backend_lane_snapshot,
)
from phaze.services.backends.local import LocalBackend
from phaze.services.backends.registry import resolve_backends, resolve_compute_backend, resolved_non_local_kind


# The complete pre-split public surface, declared rather than implied. `__init__.py` ignores F401
# repo-wide, so this list is the only thing that documents intent -- keep it in sync with the imports
# above when a name is added to (or retired from) any submodule.
__all__ = [
    "IN_FLIGHT",
    "KUEUE_NO_SAQ_QUEUE_NOTE",
    "LANE_RECENT_N",
    "NO_FILESERVER_AGENT_NOTE",
    "STAGING",
    "Backend",
    "ComputeAgentBackend",
    "KueueBackend",
    "LaneCompletion",
    "LaneQueueDepths",
    "LaneQueueIdentity",
    "LocalBackend",
    "_cloud_lane_active",
    "_get_backend_routing_snapshot",
    "derive_cloud_hold_reason",
    "derive_localqueue_unreachable",
    "drop_pending_push_file_enqueues",
    "flush_pending_push_file_enqueues",
    "get_analysis_activity_counts",
    "get_analysis_live_count",
    "get_analyze_queue_totals",
    "get_backend_lane_snapshot",
    "get_lane_queue_depths",
    "get_lane_recent_completions",
    "hold_awaiting_cloud",
    "resolve_backends",
    "resolve_compute_backend",
    "resolve_lane_queue_agent",
    "resolved_non_local_kind",
]
