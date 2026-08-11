"""Shared, FastAPI-free producer for ``process_file`` SAQ jobs.

Single source of truth for the deterministic job key, the complete payload, and
the job policy (timeout/retries). BOTH producers -- the dashboard "Run Analysis"
path (``routers/pipeline.py::_enqueue_analysis_jobs``) and the Wave-2 agent-reboot
re-enqueue task -- funnel through this helper so they emit the IDENTICAL key
``process_file:<file_id>``. That lets SAQ's per-queue deterministic-key dedup
collapse a repeat enqueue of an already in-flight file to a clean no-op
(32-CONTEXT "Dedup" decision; 32-RESEARCH §Q4) -- the two paths cannot drift.

Import boundary (32-RESEARCH §Q4): this module MUST stay FastAPI-free. It imports
neither ``fastapi`` nor ``phaze.routers`` -- only stdlib ``uuid`` (annotation-only),
the ``ProcessFilePayload`` schema (a real import because it is constructed),
``phaze.config`` (for the job's heartbeat policy -- role-agnostic, same import the
shared ``before_enqueue`` hook already makes), and ``FileRecord`` (annotation-only).
The annotation-only names live under ``TYPE_CHECKING`` so the reboot task and the
router can both import this without pulling in the web layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from phaze.config import get_settings
from phaze.schemas.agent_tasks import ProcessFilePayload


if TYPE_CHECKING:
    import uuid

    from phaze.models.file import FileRecord


def process_file_job_key(file_id: uuid.UUID) -> str:
    """Return the deterministic SAQ job key ``process_file:<file_id>`` for a file.

    Both producers call this so SAQ's per-queue ``incomplete``-set dedup collapses
    a repeat enqueue of an in-flight file to a no-op (32-RESEARCH §Q4). ``file_id``
    is a server-generated UUID interpolated as a UUID string -- no untrusted
    free-text enters the key (threat T-32-01).
    """
    return f"process_file:{file_id}"


# phaze-ewen: SAQ statuses that mean the deterministic key is held by a job that is NOT genuinely
# in flight -- it is dead or dying and will never process the file, yet its surviving key blocks
# re-enqueue (SAQ's _enqueue upsert only overwrites keys in aborted/complete/failed, never
# 'aborting'). A collision against one of these is BLOCKED, not "already in flight". Mirrors the
# classification the (now-removed, phaze-0jpe) fingerprint-requeue funnel applied to its own
# collisions (phaze-e57w) -- lifted here as the shared home for every ``process_file`` producer.
_BLOCKED_STATUS_VALUES = frozenset({"aborting", "failed", "aborted"})
# Non-terminal statuses: genuinely in flight ONLY if the row is not also stale/stuck.
_LIVE_STATUS_VALUES = frozenset({"new", "deferred", "queued", "active"})


def classify_process_file_collision(job: Any) -> str:
    """Classify a ``process_file`` deterministic-key collision as ``"in_flight"`` or ``"blocked"``.

    Every ``enqueue_process_file`` caller that DISCARDS a ``None`` return treats a collision as
    "already in flight" unconditionally -- but SAQ's ``_enqueue`` upsert only overwrites a
    conflicting key whose status is in ``('aborted', 'complete', 'failed')``; ``'aborting'`` is
    NOT in that allowlist, and neither is a claimed-but-worker-dead ``active`` row past its
    timeout/heartbeat. Both hold the key forever without ever processing the file.

    ``job`` is the existing row looked up via ``queue.job(key)`` (or ``None`` if it could not be
    found). A dead/dying status (``aborting``/``failed``/``aborted``) is BLOCKED; a live status
    (``queued``/``active``/...) is BLOCKED only when the row is also :attr:`saq.Job.stuck` (past
    its timeout/heartbeat -- a stale claimed row that will never make progress on its own),
    otherwise it is genuinely ``in_flight``. An unlookupable / unknown-status row degrades to
    ``in_flight`` (benign) rather than crying wolf.
    """
    if job is None:
        return "in_flight"
    status = getattr(job, "status", None)
    sval = getattr(status, "value", status)
    if sval in _BLOCKED_STATUS_VALUES:
        return "blocked"
    if sval in _LIVE_STATUS_VALUES and bool(getattr(job, "stuck", False)):
        return "blocked"
    return "in_flight"


async def enqueue_process_file(
    queue: Any,
    file: FileRecord,
    agent_id: str,
    models_path: str,
    *,
    expected_sha256: str | None = None,
    scratch_path: str | None = None,
) -> Any:
    """Enqueue ONE ``process_file`` job with the deterministic key + full payload + policy.

    Builds a COMPLETE ``ProcessFilePayload`` (the five required fields: the FileRecord's
    ``id`` / ``original_path`` / ``file_type`` plus the resolved ``agent_id`` and
    ``models_path``, plus the optional Phase-50 ``expected_sha256`` / ``scratch_path``
    cloud-push fields, both of which default ``None``) and serializes it via
    ``model_dump(mode="json")`` so the UUID round-trips as a string and the agent worker's
    ``ProcessFilePayload.model_validate`` (``extra="forbid"``) accepts it. Mirrors the working
    ``agent_files.py`` pattern -- the pre-Phase-30 bug enqueued only ``file_id`` and
    dead-lettered every job.

    Returns whatever ``queue.enqueue`` returns: a ``saq.Job`` normally, or ``None``
    when SAQ deduped the deterministic key (the file is already in-flight) -- so the
    Wave-2 reboot loop can count a ``None`` as a dedup skip.
    """
    payload = ProcessFilePayload(
        file_id=file.id,
        original_path=file.original_path,
        file_type=file.file_type,
        agent_id=agent_id,
        models_path=models_path,
        # Phase 50 (D-11): pin the pushed scratch copy + control-side expected sha256 for a cloud
        # file. Keyword-only + trailing + default None so the bulk local producer (_enqueue_analysis_jobs)
        # that passes neither stays byte-identical under extra="forbid"; when set, the worker reads/
        # verifies/cleans up the ephemeral scratch copy instead of original_path.
        expected_sha256=expected_sha256,
        scratch_path=scratch_path,
    )
    # Phase 36: the PostgresQueue broker pool is built ``open=False`` and, unlike the old
    # redis-backed Queue, does NOT auto-connect on first enqueue. ``connect()`` is idempotent
    # (guarded by ``self._connected``) so this is a no-op after the first call. This path is
    # reached non-routed too (reboot re-enqueue, integration tests), so opening here covers
    # every process_file producer regardless of how the queue was obtained.
    await queue.connect()
    return await queue.enqueue(
        "process_file",
        # Deterministic key so a re-trigger (or the Wave-2 reboot re-enqueue) of an
        # already in-flight file dedups to a no-op (SAQ incomplete-set; 32-RESEARCH §Q4).
        key=process_file_job_key(file.id),
        # phaze-w55w1: NO wall-clock net. Phase 43 pinned timeout=7200 as an outer bound under the
        # (removed) 6600s inner SIGKILL. Exhaustive analysis (ADR-0007 §7) makes any elapsed-time
        # bound wrong: a multi-hour concert set legitimately runs past both numbers, and a wall
        # clock cannot tell that apart from a hang -- phaze-1b39 is the incident where trying
        # killed real work. `timeout=0` is SAQ's documented "disabled", already used by
        # `scan_directory` for the same reason (a full SHA-256 walk legitimately runs 1-2h) and
        # already understood by both key reapers, which EXCLUDE `timeout: 0` rows by design
        # (tasks/_saq_reap.py, phaze-mllxc).
        timeout=0,
        # ...and the liveness bound that REPLACES it, on SAQ's own progress primitive. `Job.stuck`
        # is `(timeout and age > timeout) or (heartbeat and now - touched > heartbeat)`, so with
        # the wall clock disabled a job is stuck exactly when it has stopped being touched. The
        # agent lane touches it (`tasks/functions.py`) off the analysis child's heartbeat channel,
        # so a job that keeps completing windows is never swept and one that goes silent is swept
        # in bounded time -- which also keeps `classify_process_file_collision`'s `job.stuck` read
        # honest, now on a progress signal instead of an elapsed one.
        #
        # DERIVED, not equal to the inner stall threshold: this outer deadline watches a strictly
        # narrower signal than the child's own watchdog does, so it needs real slack or it will
        # sweep jobs the watchdog is correctly holding healthy. See
        # `config.BaseSettings.analysis_job_heartbeat_sec` for the full reasoning.
        heartbeat=get_settings().analysis_job_heartbeat_sec,
        # retries=2 (NOT 1): apply_project_job_defaults (tasks/_shared/queue_defaults.py)
        # only fills jobs still at the SAQ default retries==1, clobbering it to
        # worker_max_retries(4). retries=2 is honored and stays in the locked 1-2 band,
        # killing the 4x re-analysis churn from the long-file incident (RESEARCH Pitfall 2).
        retries=2,
        **payload.model_dump(mode="json"),
    )
