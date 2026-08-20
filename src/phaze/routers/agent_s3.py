"""POST /api/internal/agent/s3/{file_id}/{uploaded,failed} -- control-side S3-staging callbacks (Phase 53, Plan 04).

The file-server agent is Postgres- and SDK-free, so it reports its multipart-upload outcome through
these token-authed internal callbacks and the control plane -- the only side holding the S3
credentials and the ORM -- completes the upload itself (KSTAGE-01/DIST-01, never the agent). The
stateful transaction/S3 protocols live in ``services.agent_s3_reports``, which documents the
incidents behind each ordering; this router deliberately retains ONLY authentication,
request/response schemas, logging context, and exact HTTP compatibility.

Mirrors ``agent_push.py`` (report_pushed / report_push_mismatch). Both routes are 200-by-default:
a duplicate/late callback is an idempotent no-op that still returns 200 (T-53-15), and a re-drive
with no fileserver online -- or against a wedged S3 -- is a clean 200 hold, never a 500 (T-53-19).
The only non-200 exits are the two the service raises for: an unresolvable recorded staging bucket
(409, MKUE-02) and an unknown ``file_id`` on an under-cap re-drive (404, mirroring the
presign-download load, 53-02). Response FIELDS are additive-compatible and unchanged --
``/failed`` still reports ``cleared`` exactly when the terminal spill ran.

AUTH-01 discipline: ``file_id`` always travels on the URL PATH; the agent identity comes from the
token dependency. The request bodies carry NO identity (``extra="forbid"`` on the schemas).
"""

from typing import TYPE_CHECKING, Annotated, cast
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_s3 import UploadedRequest, UploadedResponse, UploadFailedRequest, UploadFailedResponse
from phaze.services.agent_s3_reports import (
    ProtocolOutcome,
    UnknownUploadFileError,
    UnresolvableStagingBucketError,
    UploadedReason,
    UploadedResult,
    UploadFailedReason,
    UploadFailedResult,
    process_upload_failed,
    process_uploaded,
)
from phaze.services.enqueue_router import resolve_queue_for_task


if TYPE_CHECKING:
    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/internal/agent/s3", tags=["agent-internal"])


def _exception_info(exc: BaseException) -> tuple[type[BaseException], BaseException, object]:
    """Build structlog-compatible exception info after a service has returned a caught fault."""
    return type(exc), exc, exc.__traceback__


def _log_uploaded_result(result: UploadedResult, file_id: uuid.UUID, agent_id: str) -> None:
    """Render a typed upload-success outcome with the callback's established log context."""
    if result.cleanup_error is not None:
        logger.warning(
            "report_uploaded: best-effort post-commit multipart abort/object delete failed "
            "(state already committed; the lifecycle TTL backstop is the last resort)",
            file_id=str(file_id),
            agent_id=agent_id,
            exc_info=_exception_info(result.cleanup_error),
        )

    match result.reason:
        case UploadedReason.ABSENT_OR_LATE:
            logger.info("report_uploaded: idempotent no-op (cloud_job absent or not UPLOADING)", file_id=str(file_id), agent_id=agent_id)
        case UploadedReason.EMPTY_PARTS:
            logger.warning(
                "report_uploaded: empty parts list (zero-byte/degenerate upload) -> cloud_job spilled to awaiting (routes local) + cleaned up",
                file_id=str(file_id),
                agent_id=agent_id,
                cleared=result.outcome is ProtocolOutcome.SPILLED,
            )
        case UploadedReason.UPLOAD_ID_CAS_MISS:
            logger.info("report_uploaded: idempotent no-op (lost the flip race)", file_id=str(file_id), agent_id=agent_id)
        case UploadedReason.ENQUEUE_FAILED:
            logger.warning(
                "report_uploaded: cloud_job committed UPLOADED but the post-commit submit_cloud_job "
                "enqueue failed -- file needs a re-triggered submit (control state is durable, not stranded)",
                file_id=str(file_id),
                agent_id=agent_id,
                exc_info=_exception_info(cast("BaseException", result.enqueue_error)),
            )
        case UploadedReason.SUBMIT_ROUTED:
            logger.info("report_uploaded: submit_cloud_job routed", file_id=str(file_id), agent_id=agent_id)
        case UploadedReason.MULTIPART_COMPLETED:
            logger.info("report_uploaded: multipart completed + cloud_job -> UPLOADED", file_id=str(file_id), agent_id=agent_id)


def _log_upload_failed_result(result: UploadFailedResult, file_id: uuid.UUID, agent_id: str, detail: str | None) -> None:
    """Render a typed upload-failure outcome with the callback's established log context."""
    if result.cleanup_error is not None:
        logger.warning(
            "report_upload_failed: best-effort post-commit multipart abort/object delete failed "
            "(state already committed; the lifecycle TTL backstop is the last resort)",
            file_id=str(file_id),
            agent_id=agent_id,
            exc_info=_exception_info(result.cleanup_error),
        )

    match result.reason:
        case UploadFailedReason.OVER_CAP_CAS_MISS:
            logger.info(
                "report_upload_failed: idempotent no-op (cloud_job no longer uploading/uploaded, over-cap spill skipped)",
                file_id=str(file_id),
                agent_id=agent_id,
            )
        case UploadFailedReason.UNDER_CAP_LATE:
            logger.info(
                "report_upload_failed: idempotent no-op (cloud_job absent or not UPLOADING, under-cap re-drive skipped)",
                file_id=str(file_id),
                agent_id=agent_id,
            )
        case UploadFailedReason.NO_AGENT:
            logger.warning(
                "report_upload_failed held: no fileserver agent online",
                file_id=str(file_id),
                agent_id=agent_id,
                attempt=result.attempt,
            )
        case UploadFailedReason.STAGING_UNAVAILABLE:
            logger.warning(
                "report_upload_failed held: redrive_upload could not stage a fresh multipart",
                file_id=str(file_id),
                agent_id=agent_id,
                attempt=result.attempt,
                exc_info=_exception_info(cast("BaseException", result.hold_error)),
            )
        case UploadFailedReason.SPILLED:
            logger.warning(
                "report_upload_failed: re-drive cap reached -> cloud_job re-stamped to awaiting + "
                "cleaned up + spill to AWAITING_CLOUD (routes to local)",
                file_id=str(file_id),
                agent_id=agent_id,
                attempt=result.attempt,
                cap=result.cap,
                detail=detail,
            )
        case UploadFailedReason.REDRIVEN:
            logger.info(
                "report_upload_failed: re-driving upload (slot retained)",
                file_id=str(file_id),
                agent_id=agent_id,
                attempt=result.attempt,
            )


@router.post("/{file_id}/uploaded", status_code=status.HTTP_200_OK, response_model=UploadedResponse)
async def report_uploaded(
    file_id: uuid.UUID,
    body: UploadedRequest,
    request: Request,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UploadedResponse:
    """Record a successful upload: complete the multipart CONTROL-SIDE + ``UPLOADING -> UPLOADED``.

    The control plane (not the agent) completes the multipart upload (KSTAGE-01/DIST-01) using the
    agent-reported ``(part_number, etag)`` pairs, then flips ``cloud_job`` with a rowcount guard so a
    duplicate/late callback is an idempotent 200 no-op that does NOT re-complete the object (T-53-15).
    On the kueue target this is also the post-staging seam that routes ``submit_cloud_job`` (D-01b,
    KROUTE-03/04). ``file_id`` is the PATH value only; ``agent`` comes from the token (AUTH-01).
    """
    settings = cast("ControlSettings", get_settings())
    parts = [(part.part_number, part.etag) for part in body.parts]
    try:
        result = await process_uploaded(session, file_id, parts, settings, request.app.state, resolve_queue_for_task)
    except UnresolvableStagingBucketError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="staged upload has no resolvable staging bucket recorded",
        ) from exc
    _log_uploaded_result(result, file_id, agent.id)
    return UploadedResponse(file_id=file_id)


@router.post("/{file_id}/failed", status_code=status.HTTP_200_OK, response_model=UploadFailedResponse)
async def report_upload_failed(
    file_id: uuid.UUID,
    body: UploadFailedRequest,
    request: Request,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UploadFailedResponse:
    """Record an upload failure: bounded re-drive, or terminal cleanup at the cap (KSTAGE-04).

    The bounded ``s3_upload_attempt`` budget rides ``scheduling_ledger.redrive_attempt`` (phaze-y0j0)
    and its read->+1->write-back is advisory-lock serialized (D-11 / T-83-02) -- see
    ``services.agent_s3_reports`` for both. Under the cap this re-drives and holds the slot (T-53-16);
    at/over it the cloud_job spills to awaiting and the multipart + staged object are cleaned up
    post-commit (T-53-17). ``file_id`` is the PATH value only; ``agent`` from the token (AUTH-01).
    ``body.detail`` is a bounded optional diagnostic that carries no identity.
    """
    settings = cast("ControlSettings", get_settings())
    try:
        result = await process_upload_failed(session, file_id, settings, request.app.state.task_router)
    except UnknownUploadFileError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown file_id") from exc
    _log_upload_failed_result(result, file_id, agent.id, body.detail)
    return UploadFailedResponse(file_id=file_id, cleared=result.cleared)
