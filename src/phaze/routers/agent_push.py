"""POST /api/internal/agent/push/{file_id}/{pushed,mismatch} -- control-side push callbacks (Phase 50, Plan 50-05).

The Postgres-free file-server / compute agents cannot mutate ``FileRecord`` or the
scheduling ledger directly (RESEARCH §Critical Finding 1), so the ``push_file``
transport reports its outcome through these token-authed internal-API callbacks --
the control plane is the only place with the ORM to read ``sha256_hash``, resolve the
per-agent compute queue, and read the scratch directory off ``ControlSettings``.

Mirrors ``agent_analysis.py`` (``put_analysis`` / ``report_analysis_failed``):

- ``/pushed``   (D-01 intent within the Postgres-free boundary): in ONE transaction
  flip ``PUSHING -> PUSHED``, clear the ``push_file:<id>`` ledger row, and enqueue
  exactly one ``process_file`` job on the COMPUTE queue carrying the ORM-pinned
  ``expected_sha256`` (D-11) and a ``<active_compute_scratch_dir>/<file_id>.<ext>``
  ``scratch_path``. With no compute agent online this is a clean 200 hold (never a
  500): the file stays ``PUSHING`` with its ledger row, so the staging cron /
  recovery re-drives it once a compute agent appears.

- ``/mismatch`` (D-12 integrity re-drive loop): increment the ``push_attempt`` counter
  living in the ``push_file`` ledger payload JSONB (Pitfall 4). Under
  ``push_max_attempts`` re-enqueue ``push_file`` on the FILESERVER queue while the file
  stays ``PUSHING`` (the slot is retained, Open-Q1); at/over the cap SPILL the file back
  to ``FileState.AWAITING_CLOUD`` with its cloud budget marked spent and clear the ledger
  row in one transaction (Phase 69, SCHED-03/D-04) so the next drain tick routes the file
  to local instead of looping forever (T-50-loop) -- ANALYSIS_FAILED comes only from a
  local analysis failure.

AUTH-01 discipline: ``file_id`` always travels on the URL PATH and the agent identity
comes from the token dependency -- never from a request body (the agent client sends
no body for either callback).
"""

from typing import TYPE_CHECKING, Annotated, Any, cast
import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_push import PushedResponse, PushMismatchResponse
from phaze.schemas.agent_tasks import PushFilePayload
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.scheduling_ledger import clear_ledger_entry
from phaze.tasks.push import PUSH_FILE_SAQ_TIMEOUT_SEC


if TYPE_CHECKING:
    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/internal/agent/push", tags=["agent-internal"])


@router.post("/{file_id}/pushed", status_code=status.HTTP_200_OK, response_model=PushedResponse)
async def report_pushed(
    file_id: uuid.UUID,
    request: Request,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PushedResponse:
    """Record a successful rsync push: ``PUSHING -> PUSHED`` + ledger clear + ``process_file`` enqueue.

    One committed transaction (mirrors ``put_analysis``'s state-update + ``clear_ledger_entry``
    idiom). ``expected_sha256`` is read CONTROL-SIDE from ``FileRecord.sha256_hash`` (D-11) -- the
    untrusted agent never supplies it -- and ``scratch_path`` is built from
    ``ControlSettings.active_compute_scratch_dir`` (the control-side mirror of the compute agent's
    ``cloud_scratch_dir``). ``file_id`` is the PATH value only; ``agent`` comes from the token
    dependency (AUTH-01).

    No compute agent online -> a clean 200 hold (NOT a 500): nothing is enqueued and the file is
    left ``PUSHING`` with its ledger row so the staging cron / recovery re-drives it later.
    """
    settings = cast("ControlSettings", get_settings())

    # Load the file first: the pinned payload needs sha256_hash + file_type (D-11). Reading before
    # the state flip is fine -- both fields are immutable here and untouched by the UPDATE below.
    file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()

    # Gate on an online compute agent BEFORE mutating anything: with none available this is a clean
    # hold (D-02) -- no state change, no ledger clear, no enqueue, no 500.
    try:
        compute_agent = await select_active_agent(session, kind="compute")
    except NoActiveAgentError:
        logger.warning("report_pushed held: no compute agent online", file_id=str(file_id), agent_id=agent.id)
        return PushedResponse(file_id=file_id)

    # One transaction: PUSHING -> PUSHED, clear the push ledger row, enqueue compute analysis.
    # WR-02: guard the transition on the CURRENT state being PUSHING so a duplicate/late callback
    # (e.g. a push_file SAQ retry after its first callback already committed) is an idempotent no-op
    # instead of clobbering an already-advanced file (ANALYZED/...) back to PUSHED and re-enqueuing
    # process_file against a scratch copy the first run already deleted (which would re-trigger the
    # CR-01 stranding). Only when the row actually transitioned do we clear the ledger + enqueue.
    # An UPDATE returns a CursorResult at runtime (exposing rowcount); the async stubs type it as
    # the base Result, so cast to read the affected-row count (mirrors services/scan_deletion.py).
    res = cast(
        "CursorResult[Any]",
        await session.execute(
            update(FileRecord).where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING).values(state=FileState.PUSHED)
        ),
    )
    if res.rowcount == 0:
        # Already advanced past PUSHING: a clean idempotent 200, no ledger clear, no re-enqueue.
        await session.commit()
        logger.info(
            "report_pushed: idempotent no-op (file no longer PUSHING)",
            file_id=str(file_id),
            agent_id=agent.id,
        )
        return PushedResponse(file_id=file_id)
    await clear_ledger_entry(session, f"push_file:{file_id}")

    # D-08: terminalize compute's cloud_job row (SUBMITTED -> SUCCEEDED) in the SAME transaction as the
    # PUSHING -> PUSHED flip. ComputeAgentBackend.dispatch wrote this row (backend_id set, s3_key NULL,
    # SUBMITTED) when the file was staged; the /pushed callback is compute's reconcile path (§4.2), so
    # terminalizing it here drains in_flight_count(compute) so the file leaves the backend's per-backend
    # in-flight window as its FileState flips (Phase 69, D-05). Gated behind the WR-02 rowcount != 0
    # guard above -- a duplicate/late callback (rowcount == 0) returned early and writes NOTHING here.
    await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.SUCCEEDED.value))

    compute_queue = request.app.state.task_router.queue_for(compute_agent.id)
    # TRANSITIONAL — Phase 68: registry-derived reduction accessor (removed with the Backend protocol).
    scratch_path = f"{settings.active_compute_scratch_dir}/{file_id}.{file.file_type}"
    await enqueue_process_file(
        compute_queue,
        file,
        compute_agent.id,
        settings.models_path,
        expected_sha256=file.sha256_hash,
        scratch_path=scratch_path,
    )
    await session.commit()

    logger.info(
        "report_pushed: file -> PUSHED + process_file enqueued",
        file_id=str(file_id),
        agent_id=agent.id,
        compute_agent_id=compute_agent.id,
    )
    return PushedResponse(file_id=file_id)


@router.post("/{file_id}/mismatch", status_code=status.HTTP_200_OK, response_model=PushMismatchResponse)
async def report_push_mismatch(
    file_id: uuid.UUID,
    request: Request,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PushMismatchResponse:
    """Record a post-transfer sha256 mismatch: attempt-capped re-drive, or terminal failure (D-12).

    The ``push_attempt`` counter lives in the ``push_file:<file_id>`` ledger payload JSONB
    (migration-free, Pitfall 4). Read it (default 0) and increment:

    - ``attempt + 1 > push_max_attempts`` -> SPILL to ``FileState.AWAITING_CLOUD`` + cloud_job FAILED
      with ``attempts`` marked spent + ``clear_ledger_entry`` in one transaction (Phase 69, SCHED-03/D-04):
      the file falls to local on the next drain tick instead of re-pushing forever (T-50-loop). The
      terminal ``ANALYSIS_FAILED`` now comes only from a local analysis failure.
    - otherwise -> re-enqueue ``push_file`` on the FILESERVER queue (the rsync initiator) keeping
      the file ``PUSHING`` (the slot is retained, Open-Q1), and stamp the incremented
      ``push_attempt`` back onto the ledger row. The deterministic ``push_file:<id>`` key dedups a
      still-live push. With no fileserver online the file is left ``PUSHING`` for the staging cron /
      recovery to re-drive.

    ``file_id`` is the PATH value only; ``agent`` from the token dependency (AUTH-01).
    """
    settings = cast("ControlSettings", get_settings())
    ledger_key = f"push_file:{file_id}"

    # The push_attempt counter rides the ledger payload JSONB (Pitfall 4); default 0 when absent.
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == ledger_key))).scalar_one_or_none()
    current_attempt = 0
    if row is not None and isinstance(row.payload, dict):
        current_attempt = int(row.payload.get("push_attempt", 0) or 0)
    next_attempt = current_attempt + 1

    # Over the cap: SPILL back to AWAITING_CLOUD + ledger clear, one transaction (Phase 69, SCHED-03/D-04).
    if next_attempt > settings.push_max_attempts:
        # SCHED-03/D-04: a compute push that exhausts its push_max_attempts re-drives no longer HARD-fails.
        # Spill the file back to AWAITING_CLOUD so the next release_awaiting_cloud drain tick can route it
        # to a lower-rank backend -- and, because this backend's cloud budget is now exhausted, to LOCAL.
        # ANALYSIS_FAILED comes ONLY from a local analysis failure; every cloud-failure path spills to local.
        await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.AWAITING_CLOUD))
        # Terminalize compute's cloud_job row (SUBMITTED -> FAILED) in the SAME transaction, mirroring the
        # /pushed success path (SUCCEEDED at L127): FAILED drains the row from the D-10 in-flight set so
        # in_flight_count(compute) stays honest and the backend's cap slot is released (Phase 69, D-05).
        # Mark the total-cloud budget SPENT (attempts >= cloud_submit_max_attempts): select_backend reads
        # cloud_job.attempts to exclude cloud, so this stamp forces the next drain tick onto local (D-04).
        # A no-op for non-compute files (no cloud_job row -> 0 rows affected) and idempotent.
        await session.execute(
            update(CloudJob)
            .where(CloudJob.file_id == file_id)
            .values(status=CloudJobStatus.FAILED.value, attempts=settings.cloud_submit_max_attempts)
        )
        await clear_ledger_entry(session, ledger_key)
        await session.commit()
        logger.warning(
            "report_push_mismatch: push cap reached -> spill to AWAITING_CLOUD (routes to local)",
            file_id=str(file_id),
            agent_id=agent.id,
            attempt=next_attempt,
            cap=settings.push_max_attempts,
        )
        return PushMismatchResponse(file_id=file_id, cleared=True)

    # Under the cap: re-drive push_file on the FILESERVER queue, keeping the PUSHING slot (Open-Q1).
    file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    try:
        fileserver_agent = await select_active_agent(session, kind="fileserver")
    except NoActiveAgentError:
        # No fileserver online: leave the file PUSHING for the staging cron / recovery to re-drive.
        logger.warning(
            "report_push_mismatch held: no fileserver agent online",
            file_id=str(file_id),
            agent_id=agent.id,
            attempt=next_attempt,
        )
        await session.commit()
        return PushMismatchResponse(file_id=file_id, cleared=False)

    fileserver_queue = request.app.state.task_router.queue_for(fileserver_agent.id)
    payload = PushFilePayload(
        file_id=file.id,
        original_path=file.original_path,
        file_type=file.file_type,
        agent_id=fileserver_agent.id,
    )
    dumped = payload.model_dump(mode="json")
    await fileserver_queue.connect()
    # Deterministic key collapses a still-live push to a no-op (the control-side before_enqueue hook
    # also derives it from file_id); passing it explicitly keeps the dedup contract clear here.
    # WR-03: stamp the explicit SAQ job-net timeout (strictly above the agent's asyncio outer guard)
    # so a re-driven push has the same deterministic inner<outer<net kill ordering as the staged one.
    await fileserver_queue.enqueue("push_file", key=ledger_key, timeout=PUSH_FILE_SAQ_TIMEOUT_SEC, **dumped)

    # Persist the incremented attempt counter in the ledger payload. The control-side before_enqueue
    # hook upserts the row with the fresh PushFilePayload kwargs (no push_attempt) in its own session,
    # so stamp push_attempt back on AFTER the enqueue -- this UPDATE is the source of truth for the
    # counter. The file stays PUSHING (the slot is retained); no FileRecord state change.
    merged: dict[str, Any] = {**dumped, "push_attempt": next_attempt}
    await session.execute(update(SchedulingLedger).where(SchedulingLedger.key == ledger_key).values(payload=merged))
    await session.commit()

    logger.info(
        "report_push_mismatch: re-driving push_file (slot retained)",
        file_id=str(file_id),
        agent_id=agent.id,
        attempt=next_attempt,
        fileserver_agent_id=fileserver_agent.id,
    )
    return PushMismatchResponse(file_id=file_id, cleared=False)
