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

NULL-GUARD FOR A CONCURRENTLY-DELETED FileRecord (request_guards.py rule 3, phaze-zdej): both
callbacks load ``FileRecord`` on a ``file_id`` a PREVIOUS request named, and a scan-deletion
(``DELETE /pipeline/scans/{batch_id}`` -> ``delete_scan_cascade``) can remove that row -- and its
``cloud_job`` sidecar, cascade-deleted in the same transaction -- while a file is still mid-rsync.
Both handlers use ``scalar_one_or_none()`` and branch explicitly to the same clean 200 hold used
for "no attributed compute backend": no state change, no ledger clear, no enqueue.
"""

from typing import TYPE_CHECKING, Annotated, Any, cast
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_push import PushedResponse, PushMismatchResponse
from phaze.schemas.agent_tasks import PushFilePayload
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.backends import hold_awaiting_cloud, resolve_compute_backend
from phaze.services.enqueue_router import NoActiveAgentError, lane_for_task, select_active_agent
from phaze.services.scheduling_ledger import clear_ledger_entry
from phaze.tasks.push import PUSH_FILE_SAQ_RETRIES, push_file_saq_timeout_sec


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
    untrusted agent never supplies it -- and the compute queue + ``scratch_path`` are resolved from the
    file's RECORDED ``cloud_job.backend_id`` via ``resolve_compute_backend`` (D-06 record-don't-rederive),
    NOT ``select_active_agent(kind="compute")`` / ``active_compute_scratch_dir`` (Pitfall 4): the
    terminalization, scratch dir, and process_file routing all attribute to the agent this file was
    dispatched to (MCOMP-06). ``file_id`` is the PATH value only; ``agent`` comes from the token
    dependency (AUTH-01).

    No attributed compute backend (no ``cloud_job``, or an operator-removed ``backend_id``) -> a clean
    200 hold (NOT a 500): nothing is enqueued and the file is left ``PUSHING`` with its ledger row so the
    staging cron / recovery re-drives it later.
    """
    settings = cast("ControlSettings", get_settings())

    # Load the file first: the pinned payload needs sha256_hash + file_type (D-11). Reading before
    # the state flip is fine -- both fields are immutable here and untouched by the UPDATE below.
    file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()

    # D-06 (record-don't-rederive, Pitfall 4): resolve the file's OWN compute backend from the RECORDED
    # cloud_job.backend_id -- NOT select_active_agent(kind="compute"). The scratch dir, the process_file
    # target queue, AND the cloud_job terminalization must all attribute to the agent this file was
    # dispatched to (MCOMP-06 no-cross-attribution), so route off the backend ComputeAgentBackend.dispatch
    # stamped on this file's cloud_job, never "the active compute agent". The /pushed reporter is the
    # FILESERVER agent (tasks/push.py runs report_pushed after rsync), so there is NO reporter==agent_ref
    # gate here (that D-07 gate belongs on /mismatch, whose reporter IS the compute agent).
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    backend = resolve_compute_backend(settings, cloud_job.backend_id if cloud_job else None)
    # No attributed compute backend (no cloud_job, or an operator-removed / unattributed backend_id ->
    # resolve returns None): a clean 200 hold (NOT a 500) mirroring the old no-compute-agent hold -- no
    # state change, no ledger clear, no enqueue. The file stays PUSHING with its ledger row so the staging
    # cron / recovery re-drives it once the backend is resolvable.
    if backend is None:
        logger.warning("report_pushed held: no attributed compute backend", file_id=str(file_id), agent_id=agent.id)
        return PushedResponse(file_id=file_id)
    # agent_ref / scratch_dir are Optional at the ComputeBackend type level but guaranteed non-empty at
    # construction by _require_dispatch_fields, so narrow for mypy (same discipline as backends._destination).
    agent_ref = cast("str", backend.agent_ref)
    scratch_dir = cast("str", backend.scratch_dir)

    # One transaction: terminalize compute's cloud_job row + PUSHING -> PUSHED dual-write, clear the push
    # ledger row, enqueue compute analysis.
    # SC#1/D-12: the CAS anchor is cloud_job.status == 'submitted' (compute's single in-flight status), NOT
    # FileRecord.state == PUSHING -- collapsing the guard onto the sidecar as the single CAS domain and
    # removing the last FileRecord.state ROUTING read here before Phase 90 drops the column. This single CAS
    # replaces BOTH the old FileRecord PUSHING->PUSHED guard AND the unconditional cloud_job SUCCEEDED write:
    # it terminalizes compute's row SUBMITTED->SUCCEEDED (D-08), draining it from the D-10 in-flight set so
    # in_flight_count(compute) stays honest as the file advances (Phase 69, D-05). Anchoring on the
    # 'submitted' literal is safe even though kueue's lifecycle also transits SUBMITTED: resolve_compute_backend
    # returns None for a kueue backend_id and the handler already returned the 200 hold above, so a kueue file
    # never reaches this CAS -- no defensive backend-kind check is added (D-12). WR-02: a duplicate/late /pushed
    # (a push_file SAQ retry after its first callback committed) whose cloud_job already advanced past
    # 'submitted' matches 0 rows -> idempotent no-op: NO FileRecord write, NO ledger clear, NO second
    # process_file enqueue (which would clobber an already-ANALYZED file to PUSHED and re-trigger CR-01
    # stranding). An UPDATE returns a CursorResult at runtime (exposing rowcount); the async stubs type it as
    # the base Result, so cast to read the affected-row count (mirrors services/scan_deletion.py).
    res = cast(
        "CursorResult[Any]",
        await session.execute(
            update(CloudJob)
            .where(CloudJob.file_id == file_id, CloudJob.status == CloudJobStatus.SUBMITTED.value)
            .values(status=CloudJobStatus.SUCCEEDED.value)
        ),
    )
    if res.rowcount == 0:
        # Already advanced past 'submitted': a clean idempotent 200, no ledger clear, no re-enqueue.
        await session.commit()
        logger.info(
            "report_pushed: idempotent no-op (cloud_job no longer 'submitted')",
            file_id=str(file_id),
            agent_id=agent.id,
        )
        return PushedResponse(file_id=file_id)
    # rowcount != 0: gate the ledger clear + process_file enqueue behind the cloud_job CAS.
    # Phase 90 (D-09): the former PUSHED FileRecord.state dual-write was removed; the cloud_job
    # sidecar (terminalized SUBMITTED -> SUCCEEDED above) is the sole derived authority PR-A reads.
    await clear_ledger_entry(session, f"push_file:{file_id}")

    # D-06: route process_file to the RECORDED backend's agent_ref queue with its scratch_dir. The
    # transitional settings.active_compute_scratch_dir reduction accessor was DELETED in Phase 73
    # (MCOMP-03); scratch resolution is per-agent off the recorded backend.
    compute_queue = request.app.state.task_router.queue_for(agent_ref, lane_for_task("process_file"))
    scratch_path = f"{scratch_dir}/{file_id}.{file.file_type}"
    await enqueue_process_file(
        compute_queue,
        file,
        agent_ref,
        settings.models_path,
        expected_sha256=file.sha256_hash,
        scratch_path=scratch_path,
    )
    await session.commit()

    logger.info(
        "report_pushed: file -> PUSHED + process_file enqueued",
        file_id=str(file_id),
        agent_id=agent.id,
        backend_id=backend.id,
        compute_agent_id=agent_ref,
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

    D-07 (T-73-07) reporter authorization runs FIRST: the /mismatch reporter IS the compute agent running
    ``process_file``, so its ``agent.id`` must equal the file's RECORDED ``cloud_job.backend_id`` backend's
    ``agent_ref`` (resolved via ``resolve_compute_backend``). A wrong/stale/duplicate compute agent is
    rejected 403 with nothing terminalized (reject-don't-terminalize; never re-stamp ``backend_id`` from the
    token). The under-cap re-drive then stamps that backend's destination onto the ``PushFilePayload``
    (Landmine 1) -- never a destination-less push; an unattributed file (no backend) holds instead.

    The ``push_attempt`` counter lives in the ``push_file:<file_id>`` ledger payload JSONB
    (migration-free, Pitfall 4). Read it (default 0) and increment:

    - ``attempt + 1 > push_max_attempts`` -> SPILL to ``FileState.AWAITING_CLOUD`` via a CAS on
      ``cloud_job.status == 'submitted'`` (SC#1/D-12: the sidecar is the single CAS domain, no
      ``FileRecord.state`` routing read) that re-stamps the row ``submitted -> awaiting`` with ``attempts``
      marked spent (D-03), + ``clear_ledger_entry`` in one transaction (Phase 69, SCHED-03/D-04): the file
      falls to local on the next drain tick instead of re-pushing forever (T-50-loop). The terminal
      ``ANALYSIS_FAILED`` now comes only from a local analysis failure. A late/duplicate /mismatch on a
      file whose cloud_job already advanced past ``submitted`` matches 0 rows and cannot clobber it.
    - otherwise -> re-enqueue ``push_file`` on the FILESERVER queue (the rsync initiator) keeping
      the file ``PUSHING`` (the slot is retained, Open-Q1), and stamp the incremented
      ``push_attempt`` back onto the ledger row. The deterministic ``push_file:<id>`` key dedups a
      still-live push. With no fileserver online the file is left ``PUSHING`` for the staging cron /
      recovery to re-drive.

    ``file_id`` is the PATH value only; ``agent`` from the token dependency (AUTH-01).
    """
    settings = cast("ControlSettings", get_settings())
    ledger_key = f"push_file:{file_id}"

    # D-06 + D-07 (record-don't-rederive + reporter authorization, T-73-07): resolve the file's OWN compute
    # backend from the RECORDED cloud_job.backend_id, then verify the REPORTING agent is that backend's
    # dispatched agent. Unlike /pushed (reported by the FILESERVER), /mismatch is reported by the COMPUTE
    # agent running process_file (tasks/functions.py), so agent.id MUST equal backend.agent_ref. A
    # wrong/stale/duplicate compute agent is rejected 403 BEFORE any mutation -- the file is NOT
    # terminalized/spilled/re-driven (reject-don't-terminalize). NEVER re-stamp backend_id from the token:
    # that would invert record-don't-rederive and let a spoofing reporter mis-attribute another agent's file.
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    backend = resolve_compute_backend(settings, cloud_job.backend_id if cloud_job else None)
    if backend is not None and agent.id != backend.agent_ref:
        logger.warning(
            "report_push_mismatch rejected: reporting agent is not the dispatched compute agent",
            file_id=str(file_id),
            reporter=agent.id,
            expected=backend.agent_ref,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="reporting agent is not the dispatched compute agent")

    # The push_attempt counter rides the ledger payload JSONB (Pitfall 4); default 0 when absent.
    # D-05 (AR-73-02 / T-73-13 / WR-04): serialize the read->+1->write-back so two concurrent /mismatch
    # for one file (SAQ retries, N compute reporters) can't both read the same counter and lose an
    # increment, silently letting the file exceed its bounded push budget.
    #
    # We take a transaction-scoped ADVISORY lock keyed by the ledger key -- NOT `.with_for_update()` on
    # this row. A row lock here would self-deadlock: the under-cap path re-enqueues `push_file` at the
    # `fileserver_queue.enqueue(...)` call BELOW while this transaction is still open, and `push_file` is a
    # registered before_enqueue key-builder, so the `apply_deterministic_key` WRITE hook opens its OWN
    # session on the same pool and upserts THIS SAME ledger row (ON CONFLICT DO UPDATE). That nested write
    # would block on a row lock we hold, and we can't commit to release it until the enqueue returns --
    # a hang with no statement_timeout to break it and no Postgres deadlock cycle to detect. The advisory
    # lock lives in a different lock space than the hook's row lock, so the hook's upsert never blocks on
    # it; a second concurrent /mismatch for the same key waits on the advisory lock until we commit, so the
    # RMW is still serialized and each increment is applied exactly once (cap still trips at the boundary).
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(ledger_key))))
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
        #
        # SC#1/D-12 anchor swap: the CAS guard now keys on cloud_job.status == 'submitted' (compute's single
        # in-flight status), NOT FileRecord.state == PUSHING -- collapsing the guard onto the sidecar as the
        # single CAS domain and removing the last FileRecord.state ROUTING read before Phase 90 drops the
        # column. D-03: this SAME CAS re-stamps the row submitted -> awaiting (was a separate FAILED write) so
        # the hard shadow invariant AWAITING_CLOUD => status='awaiting' holds, keeping attempts SPENT
        # (>= cloud_submit_max_attempts) so select_backend excludes cloud and routes the file to local (D-04).
        # A duplicate/late /mismatch (SAQ retry) -- or a stale/removed-backend reporter that skipped the D-07
        # gate -- on a file whose cloud_job already advanced past 'submitted' (SUCCEEDED/RUNNING/reaped) matches
        # 0 rows and CANNOT clobber it back to AWAITING_CLOUD (T-83-PUSH-CLOBBER).
        #
        # D-01/D-02: route the spill re-stamp through the SINGLE awaiting writer
        # (services.backends.hold_awaiting_cloud) instead of an inline CAS. Its spill branch keys on
        # expect_status=('submitted',) and re-stamps submitted -> awaiting with attempts SPENT (D-03),
        # returning False (a full no-op) on the 0-row advanced-file case. NO clear_cloud_phase: the push
        # spill must NOT touch cloud_phase (D-12 -- only the s3 spill clears it).
        #
        # NULL-GUARD: the helper's CAS dereferences file.id, so load the FileRecord (none is loaded in this
        # branch today). An absent file (unreachable in practice -- cloud_job.file_id FKs files.id) takes the
        # FULL no-op below (cleared=False), identical to a CAS miss; passing None would raise AttributeError
        # where the old disconnected update(FileRecord) silently matched 0 rows. No 404 here: the over-cap
        # spill is an agent callback and a 404 would change the response contract.
        file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
        cleared = file is not None and await hold_awaiting_cloud(
            session,
            file,
            attempts=settings.cloud_submit_max_attempts,
            expect_status=(CloudJobStatus.SUBMITTED.value,),
        )
        if not cleared:
            # cloud_job no longer 'submitted' (already advanced, reaped, or a non-compute file with no row):
            # idempotent FULL no-op. No cloud_job write, no FileRecord write, no ledger clear.
            await session.commit()
            logger.info(
                "report_push_mismatch: idempotent no-op (cloud_job no longer 'submitted', over-cap spill skipped)",
                file_id=str(file_id),
                agent_id=agent.id,
            )
            return PushMismatchResponse(file_id=file_id, cleared=False)
        # cleared (helper CAS hit): gate the ledger clear behind the cloud_job CAS. The submitted ->
        # awaiting re-stamp above already drained the row from the D-10 in-flight set so
        # in_flight_count(compute) stays honest and the backend's cap slot is released (Phase 69, D-05);
        # 'awaiting' is not in IN_FLIGHT (D-03).
        # Phase 90 (D-09): the former AWAITING_CLOUD FileRecord.state dual-write was removed; the
        # cloud_job sidecar re-stamped to 'awaiting' by hold_awaiting_cloud is the sole derived authority.
        await clear_ledger_entry(session, ledger_key)
        await session.commit()
        logger.warning(
            "report_push_mismatch: push cap reached -> cloud_job re-stamped to awaiting + spill to AWAITING_CLOUD (routes to local)",
            file_id=str(file_id),
            agent_id=agent.id,
            attempt=next_attempt,
            cap=settings.push_max_attempts,
        )
        return PushMismatchResponse(file_id=file_id, cleared=True)

    # Under the cap: re-drive push_file on the FILESERVER queue, keeping the PUSHING slot (Open-Q1).
    # Landmine 1: the re-drive MUST carry the recorded destination -- never a destination-less payload
    # (the fileserver would rsync to a null remote spec). An unattributed file (no cloud_job / an
    # operator-removed backend_id -> backend is None) has no destination to stamp, so it cannot be
    # re-driven: hold it PUSHING for the staging cron / recovery (mirrors the no-fileserver hold below)
    # rather than enqueue a destination-less push.
    if backend is None:
        logger.warning(
            "report_push_mismatch held: no attributed compute backend to re-stamp the push destination",
            file_id=str(file_id),
            agent_id=agent.id,
            attempt=next_attempt,
        )
        await session.commit()
        return PushMismatchResponse(file_id=file_id, cleared=False)

    # NULL-GUARD (phaze-zdej, request_guards.py rule 3 -- copy the over-cap branch's shape): a
    # concurrent scan-deletion can remove this FileRecord (and cascade-delete the SAME cloud_job row
    # this handler already read above) in the window between the cloud_job SELECT and this SELECT.
    # `backend` was resolved from the in-memory cloud_job read before that deletion could have
    # committed, so reaching here with a vanished FileRecord is a genuine race, not a contradiction.
    # scalar_one() would raise NoResultFound -> unhandled 500; there is nothing left to re-drive a
    # push for, so hold cleanly (mirrors the "no fileserver online" hold below) instead of crashing.
    file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
    if file is None:
        logger.warning(
            "report_push_mismatch held: file record no longer exists (concurrent scan-deletion)",
            file_id=str(file_id),
            agent_id=agent.id,
            attempt=next_attempt,
        )
        await session.commit()
        return PushMismatchResponse(file_id=file_id, cleared=False)

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

    fileserver_queue = request.app.state.task_router.queue_for(fileserver_agent.id, lane_for_task("push_file"))
    # Landmine 1: stamp the RECORDED backend's destination onto the re-driven payload (backend is non-None
    # here -- either the reporter passed the D-07 gate or no gate applied and the backend-None hold above
    # already returned). The destination is the compute backend to push TO; agent_id is the FILESERVER
    # that initiates the rsync (the push origin), unchanged.
    payload = PushFilePayload(
        file_id=file.id,
        original_path=file.original_path,
        file_type=file.file_type,
        agent_id=fileserver_agent.id,
        dest_host=backend.push_host,
        dest_scratch_dir=backend.scratch_dir,
        dest_ssh_user=backend.ssh_user,
    )
    dumped = payload.model_dump(mode="json")
    await fileserver_queue.connect()
    # Deterministic key collapses a still-live push to a no-op (the control-side before_enqueue hook
    # also derives it from file_id); passing it explicitly keeps the dedup contract clear here.
    # WR-03: stamp the explicit SAQ job-net timeout (strictly above the agent's asyncio outer guard)
    # so a re-driven push has the same deterministic inner<outer<net kill ordering as the staged one.
    # phaze-2qpn: size-scaled + retries, matching the staged-path enqueue.
    await fileserver_queue.enqueue(
        "push_file", key=ledger_key, timeout=push_file_saq_timeout_sec(file.file_size), retries=PUSH_FILE_SAQ_RETRIES, **dumped
    )

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
