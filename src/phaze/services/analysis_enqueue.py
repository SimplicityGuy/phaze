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
the ``ProcessFilePayload`` schema (a real import because it is constructed), and
``FileRecord`` (annotation-only). The annotation-only names live under
``TYPE_CHECKING`` so the reboot task and the router can both import this without
pulling in the web layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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


async def enqueue_process_file(queue: Any, file: FileRecord, agent_id: str, models_path: str) -> Any:
    """Enqueue ONE ``process_file`` job with the deterministic key + full payload + policy.

    Builds a COMPLETE ``ProcessFilePayload`` (all five fields: the FileRecord's
    ``id`` / ``original_path`` / ``file_type`` plus the resolved ``agent_id`` and
    ``models_path``) and serializes it via ``model_dump(mode="json")`` so the UUID
    round-trips as a string and the agent worker's ``ProcessFilePayload.model_validate``
    (``extra="forbid"``) accepts it. Mirrors the working ``agent_files.py`` pattern --
    the pre-Phase-30 bug enqueued only ``file_id`` and dead-lettered every job.

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
    )
    return await queue.enqueue(
        "process_file",
        # Deterministic key so a re-trigger (or the Wave-2 reboot re-enqueue) of an
        # already in-flight file dedups to a no-op (SAQ incomplete-set; 32-RESEARCH §Q4).
        key=process_file_job_key(file.id),
        # 4h bounded: exceeds the longest legit set (~3h) yet lets SAQ reclaim a
        # dead/restarted worker's job (spike 31-01 + restart-resilience). Hardcoded
        # like pipeline_scans.py.
        timeout=14400,
        # retries=2 (NOT 1): apply_project_job_defaults (tasks/_shared/queue_defaults.py)
        # only fills jobs still at the SAQ default retries==1, clobbering it to
        # worker_max_retries(4). retries=2 is honored and stays in the locked 1-2 band,
        # killing the 4x re-analysis churn from the long-file incident (RESEARCH Pitfall 2).
        retries=2,
        **payload.model_dump(mode="json"),
    )
