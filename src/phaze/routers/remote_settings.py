"""Global provider Settings and durable Tracklist queue controls. GET never sends requests."""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import select
import structlog

from phaze.models.remote_lookup_batch import RemoteLookupBatch
from phaze.routers.record import templates
from phaze.routers.request_guards import require_same_origin_form
from phaze.services.remote_lookup import RemoteLookup
from phaze.services.remote_lookup_batches import batch_status, create_batch
from phaze.services.remote_policy import ProviderPolicy


router = APIRouter(tags=["admin"])
logger = structlog.get_logger(__name__)


def service(request: Request) -> RemoteLookup:
    value = getattr(request.app.state, "remote_lookup", None)
    if value is None:
        raise HTTPException(503, "Remote lookup is not configured")
    return cast("RemoteLookup", value)


async def render_settings(request: Request, message: str | None = None, *, failed: bool = False) -> HTMLResponse:
    statuses = await service(request).provider_status()
    return templates.TemplateResponse(
        request=request, name="admin/partials/_remote_providers.html", context={"providers": statuses, "message": message, "failed": failed}
    )


@router.get("/admin/remote-providers", response_class=HTMLResponse)
async def provider_settings(request: Request) -> HTMLResponse:
    return await render_settings(request)


@router.post("/admin/remote-providers/{provider_id}/policy", response_class=HTMLResponse, dependencies=[Depends(require_same_origin_form)])
async def set_provider_policy(
    request: Request,
    provider_id: Annotated[str, Path(max_length=64, pattern=r"^[a-z][a-z0-9_]*$")],
    min_interval_seconds: Annotated[float, Form(gt=0, le=3600)],
    max_requests_per_run: Annotated[int, Form(gt=0, le=256)],
    enabled: Annotated[bool, Form()] = False,
) -> HTMLResponse:
    try:
        value = ProviderPolicy(enabled=enabled, min_interval_seconds=min_interval_seconds, max_requests_per_run=max_requests_per_run)
        await service(request).policy.set(provider_id, value, actor="admin")
    except (ValueError, ValidationError) as exc:
        logger.warning("remote_policy.rejected", provider_id=provider_id, actor="admin", error_type=type(exc).__name__)
        return await render_settings(request, str(exc), failed=True)
    return await render_settings(
        request,
        "Saved globally. API and workers read this policy before their next request; requests already sent finish. Quotas and stops are preserved.",
    )


@router.post("/admin/remote-providers/{provider_id}/clear-stop", response_class=HTMLResponse, dependencies=[Depends(require_same_origin_form)])
async def clear_provider_stop(
    request: Request,
    provider_id: Annotated[str, Path(max_length=64, pattern=r"^[a-z][a-z0-9_]*$")],
    reason: Annotated[str, Form(min_length=1, max_length=512)],
) -> HTMLResponse:
    try:
        await service(request).clear_stop(provider_id, operator="admin", reason=reason)
    except ValueError as exc:
        return await render_settings(request, str(exc), failed=True)
    return await render_settings(request, "Stop cleared with an audit record. Reservations and Retry-After remain in force.")


async def render_queue(request: Request, message: str | None = None) -> HTMLResponse:
    lookup = service(request)
    async with lookup._sessions() as session:
        ids = list(await session.scalars(select(RemoteLookupBatch.id).order_by(RemoteLookupBatch.created_at.desc()).limit(5)))
    batches = [await batch_status(lookup._sessions, batch_id) for batch_id in ids]
    providers = await lookup.provider_status()
    return templates.TemplateResponse(
        request=request, name="pipeline/partials/_remote_lookup_queue.html", context={"providers": providers, "batches": batches, "message": message}
    )


@router.get("/pipeline/remote-lookup-queue", response_class=HTMLResponse)
async def lookup_queue(request: Request) -> HTMLResponse:
    return await render_queue(request)


@router.post("/pipeline/remote-lookup-queue", response_class=HTMLResponse, dependencies=[Depends(require_same_origin_form)])
async def queue_lookup(request: Request) -> HTMLResponse:
    lookup = service(request)
    batch_id = await create_batch(lookup._sessions, lookup)
    message = "Batch saved. Expansion and requests run asynchronously; nothing is selected automatically."
    try:
        await request.app.state.controller_queue.enqueue("expand_remote_lookup_batch", batch_id=str(batch_id), key=f"remote-expand:{batch_id}:start")
    except Exception:
        message = "Batch saved, but queue delivery failed. The controller recovery pass will resume this batch."
    return await render_queue(request, message)
