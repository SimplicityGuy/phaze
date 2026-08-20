"""Agent S3-staging callback HTTP boundary.

The file-server agent is Postgres- and SDK-free, so it reports multipart upload success or failure
through these token-authenticated callbacks. The stateful transaction/S3 protocols live in
``services.agent_s3_reports``; this router deliberately retains authentication, request/response
schemas, logging context, and exact HTTP compatibility.
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
    """Complete an upload through the typed service protocol and preserve the agent's HTTP contract."""
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
    """Re-drive or spill a failed upload while preserving idempotent HTTP-200 compatibility."""
    settings = cast("ControlSettings", get_settings())
    try:
        result = await process_upload_failed(session, file_id, settings, request.app.state.task_router)
    except UnknownUploadFileError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown file_id") from exc
    _log_upload_failed_result(result, file_id, agent.id, body.detail)
    return UploadFailedResponse(file_id=file_id, cleared=result.cleared)
