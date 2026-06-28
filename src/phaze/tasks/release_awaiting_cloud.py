"""Control-side bounded cloud-window staging: top the ≤N window up from AWAITING_CLOUD (Phase 50).

THE SINGLE "STAY ONE AHEAD" DRIVER (CLOUDPIPE-01/-05). Phase 49's per-file router HOLDS every
cloud-routed long file in ``FileState.AWAITING_CLOUD`` (it enqueues NOTHING to compute -- that
direct path was removed in Phase 50 so the window cannot be bypassed). This cron is the ONLY thing
that introduces new push work: every ~5 min it tops the in-flight window up to
``cloud_max_in_flight`` by staging ``push_file`` for the oldest held files. Registered as a SINGLE
narrow ``CronJob(stage_cloud_window, "*/5 * * * *")`` on the controller, REPLACING the deprecated
Phase-49 ``release_awaiting_cloud`` drain cron (which drained the WHOLE held set straight to
process_file -- unbounded, and incompatible with the bounded push pipeline).

Window math (RESEARCH §"Stay one ahead"): ``window = COUNT(state IN {PUSHING, PUSHED})`` counted
from COMMITTED FileState truth (D-08, NOT the SAQ ledger); ``slots = cloud_max_in_flight - window``;
if ``slots <= 0`` stage nothing. Otherwise SELECT up to ``slots`` AWAITING_CLOUD files
``ORDER BY created_at ASC`` (FIFO) ``FOR UPDATE SKIP LOCKED``, flip each to ``PUSHING`` and enqueue
``push_file`` on the FILESERVER agent's per-agent queue. The COUNT + SELECT + ``state=PUSHING`` all
happen in ONE transaction so a 144-file backlog can never stage more than ``slots`` at a time
(T-50-scratch-dos) -- the committed PUSHING transition makes the next tick's window count current.

TWO gates, both a clean no-op (NOT a raise -- T-50-cron-raise) when the agent is absent:
  1. COMPUTE agent (the analysis consumer): no compute online -> ``{"staged": 0, "skipped": 0}``,
     files stay AWAITING_CLOUD.
  2. FILESERVER agent (the push initiator -- it owns the media mount and runs rsync): absent during
     a rolling restart -> ``{"staged": 0, "skipped": len(candidates)}``, files stay AWAITING_CLOUD
     and re-stage on a later tick.

A double-tick collapses via the deterministic ``push_file:<id>`` key (SAQ dedups the repeat enqueue
to ``None`` -> counted as skipped); the file still flips to PUSHING (the already-live push job will
land it), so the window stays honest.

CONTROL-ONLY: needs both PostgreSQL (``ctx["async_session"]``) and the per-agent enqueuer
(``ctx["task_router"]``), exactly like ``recover_orphaned_work``. Register ONLY in
``phaze.tasks.controller`` -- never the agent worker (``tests/test_task_split.py`` enforces the
agent role stays Postgres-free). FastAPI-free: imports neither ``fastapi`` nor ``phaze.routers``,
mirroring the ``recover_orphaned_work`` import discipline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import text
import structlog

from phaze.config import get_settings
from phaze.models.file import FileState
from phaze.schemas.agent_tasks import PushFilePayload
from phaze.services.cloud_staging import _stage_file_to_s3
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.pipeline import get_cloud_staging_candidates, get_cloud_window_count
from phaze.tasks.push import PUSH_FILE_SAQ_TIMEOUT_SEC


if TYPE_CHECKING:
    import uuid

    from phaze.models.file import FileRecord


logger = structlog.get_logger(__name__)

# WR-04: a fixed transaction-scoped advisory-lock key that serializes overlapping staging ticks so
# the load-bearing ≤N window cannot be overshot. SAQ does not guarantee non-overlapping cron runs,
# and the window COUNT reads COMMITTED truth -- so two ticks could each read window=0, SKIP LOCKED
# past each other's uncommitted PUSHING flips, and stage up to 2x cloud_max_in_flight. Holding this
# lock across the count+claim makes the second tick block until the first commits, after which it
# sees the committed window. Arbitrary stable constant (phase 50, plan 04); never collides because
# no other code path takes an advisory lock.
_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY = 5_000_504


def push_file_job_key(file_id: uuid.UUID) -> str:
    """Return the deterministic SAQ job key ``push_file:<file_id>`` for a staged push.

    Mirrors ``analysis_enqueue.process_file_job_key``: a double cron tick (or a retried tick) of an
    already-staged file dedups to a no-op via SAQ's per-queue incomplete-set (T-50-double-enqueue).
    ``file_id`` is a server-generated UUID -- no untrusted free-text enters the key.
    """
    return f"push_file:{file_id}"


async def _enqueue_push_file(queue: Any, file: FileRecord, agent_id: str) -> Any:
    """Enqueue ONE ``push_file`` job with the deterministic key + the complete PushFilePayload.

    Builds the four required ``PushFilePayload`` fields (the FileRecord's ``id`` / ``original_path``
    / ``file_type`` plus the resolved fileserver ``agent_id``) and serializes via
    ``model_dump(mode="json")`` so the UUID round-trips as a string under ``extra="forbid"``. Returns
    whatever ``queue.enqueue`` returns -- a ``saq.Job`` normally, or ``None`` when SAQ deduped the
    deterministic key (the file is already being pushed) so the caller counts a ``None`` as skipped.
    """
    payload = PushFilePayload(
        file_id=file.id,
        original_path=file.original_path,
        file_type=file.file_type,
        agent_id=agent_id,
    )
    # Phase 36: the PostgresQueue broker pool is built open=False; connect() is idempotent.
    await queue.connect()
    # WR-03: stamp an explicit SAQ job-net timeout strictly above the agent's asyncio outer guard so
    # a job-net cancellation can never fire before the guard reaps the rsync child. Without this the
    # role default (worker_job_timeout=600) equalled push_timeout_sec and sat BELOW the 630s guard.
    return await queue.enqueue(
        "push_file",
        key=push_file_job_key(file.id),
        timeout=PUSH_FILE_SAQ_TIMEOUT_SEC,
        **payload.model_dump(mode="json"),
    )


async def stage_cloud_window(ctx: dict[str, Any]) -> dict[str, int]:
    """Top the ≤N cloud window up to ``cloud_max_in_flight`` by staging ``push_file`` for held files.

    See the module docstring for the full window math + gate semantics. Returns
    ``{"staged": N, "skipped": M}`` where ``staged`` counts push_file jobs actually enqueued and
    ``skipped`` counts deterministic-key dedup no-ops (or, when the fileserver gate holds, the held
    candidate count). Every early-return path (no compute, window full, no candidates, no fileserver)
    is a clean no-op that leaves the held files in AWAITING_CLOUD for a later tick.
    """
    # cloud_max_in_flight lives on ControlSettings; this cron is registered ONLY on the control
    # worker (PHAZE_ROLE=control), so get_settings() returns ControlSettings here (mirrors the
    # controller.startup llm_model/llm_max_rpm access pattern).
    cfg = get_settings()
    # Phase 55 (D-02): cloud-target gate. 'local' (cloud off) -> clean no-op BEFORE the advisory
    # lock + window logic, so the cron introduces NO new cloud push work. NEVER raise
    # (T-50-cron-raise discipline, matching the GATE 1/2 no-op contract below).
    if cfg.cloud_target == "local":  # type: ignore[attr-defined]
        return {"staged": 0, "skipped": 0}
    max_in_flight = cfg.cloud_max_in_flight  # type: ignore[attr-defined]

    async with ctx["async_session"]() as session:
        # WR-04: serialize overlapping cron ticks. A transaction-scoped advisory lock makes the
        # window count + candidate claim below atomic with respect to a concurrent tick: the second
        # tick blocks here until the first commits, then its get_cloud_window_count sees the
        # committed PUSHING flips and cannot overshoot cloud_max_in_flight. Auto-released at txn end.
        await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})

        # GATE 1: a compute agent (the analysis consumer) must be online -- but ONLY for the a1
        # rsync target whose persistent compute agent drains the per-agent queue. k8s uses ephemeral
        # Kueue pods (no persistent compute agent), so on the k8s branch GATE 1 is SKIPPED -- else
        # every k8s file would wedge in AWAITING_CLOUD forever (Landmine L2). GATE 2 (fileserver)
        # below stays for BOTH targets (the fileserver owns the media mount + runs the S3 upload).
        if cfg.cloud_target == "a1":  # type: ignore[attr-defined]
            try:
                await select_active_agent(session, kind="compute")
            except NoActiveAgentError:
                logger.info("stage_cloud_window no-op: no compute agent online")
                return {"staged": 0, "skipped": 0}

        # Window counted from COMMITTED FileState truth (D-08); compute the free slots.
        window = await get_cloud_window_count(session)
        slots = max_in_flight - window
        if slots <= 0:
            return {"staged": 0, "skipped": 0}

        # FIFO oldest-first candidates, bounded to the free slots, row-locked (one transaction).
        candidates = await get_cloud_staging_candidates(session, slots)
        if not candidates:
            return {"staged": 0, "skipped": 0}

        # GATE 2: a fileserver agent (the push initiator) must be online. Absent during a rolling
        # restart -> clean hold no-op; the locked candidates stay AWAITING_CLOUD (no state change).
        try:
            fileserver_agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            logger.info("stage_cloud_window hold: no fileserver agent online", candidates=len(candidates))
            return {"staged": 0, "skipped": len(candidates)}

        # Phase 55 (D-01a): ONE branch forks on cloud_target inside the SAME window. Both targets
        # reuse the advisory lock + FIFO claim + window/slots math + the SINGLE post-loop commit.
        task_router = ctx["task_router"]
        push_queue = task_router.queue_for(fileserver_agent.id)
        tally = {"staged": 0, "skipped": 0}
        for file in candidates:
            # Flip to PUSHING BEFORE the enqueue dedup outcome is known: a deduped (already-live) push
            # still owns the file, so the window count stays honest on the next tick.
            file.state = FileState.PUSHING
            if cfg.cloud_target == "k8s":  # type: ignore[attr-defined]
                # k8s: stage to S3 via the NO-COMMIT core (L1) -- NEVER the committing public
                # stage_file_to_s3 (a mid-loop commit would release the advisory lock + row locks
                # and re-open the over-stage class). It enqueues s3_upload (not push_file); the
                # window honesty comes from the committed PUSHING flip, identical to a1.
                await _stage_file_to_s3(session, file, task_router)
                tally["staged"] += 1
            else:
                job = await _enqueue_push_file(push_queue, file, fileserver_agent.id)
                if job is None:
                    tally["skipped"] += 1
                else:
                    tally["staged"] += 1
        await session.commit()

    logger.info("stage_cloud_window complete", agent_id=fileserver_agent.id, staged=tally["staged"], skipped=tally["skipped"])
    return tally
