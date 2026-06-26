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
  ``expected_sha256`` (D-11) and a ``<compute_scratch_dir>/<file_id>.<ext>``
  ``scratch_path``. With no compute agent online this is a clean 200 hold (never a
  500): the file stays ``PUSHING`` with its ledger row, so the staging cron /
  recovery re-drives it once a compute agent appears.

AUTH-01 discipline: ``file_id`` always travels on the URL PATH and the agent identity
comes from the token dependency -- never from a request body (the agent client sends
no body for either callback).
"""

from typing import TYPE_CHECKING, Annotated, cast
import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_push import PushedResponse
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.scheduling_ledger import clear_ledger_entry


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
    ``ControlSettings.compute_scratch_dir`` (the control-side mirror of the compute agent's
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
    await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.PUSHED))
    await clear_ledger_entry(session, f"push_file:{file_id}")

    compute_queue = request.app.state.task_router.queue_for(compute_agent.id)
    scratch_path = f"{settings.compute_scratch_dir}/{file_id}.{file.file_type}"
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
