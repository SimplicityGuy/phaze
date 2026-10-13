"""Operator-triggered remote lookup JSON APIs. The only trigger for remote traffic; no page load issues one.

Operator access and the server-owned audit identity follow routers/local_sources.py. The service lives on
``app.state.remote_lookup``, built once per process in the application lifespan. Its consumer is the
provider-neutral lookup and candidate review UI, bead phaze-q6j9w; until that lands no template calls these routes,
which is why ``/api/remote-lookups/`` is a non-UI prefix in tests/shared/core/test_route_reachability.py.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.database import get_session
from phaze.routers.request_guards import require_companion_command_origin
from phaze.schemas.local_source_import import SelectedRecordingSource
from phaze.schemas.remote_lookup import ClearStopCommand, LoadCommand, LoadResult, LookupCommand, LookupResult, ProviderStatus, RemoteSourceDecision
from phaze.services.remote_lookup import RemoteLookup, decide_remote_candidate


router = APIRouter(prefix="/api/remote-lookups", tags=["admin"])
logger = structlog.get_logger(__name__)
# The server-owned audit identity, as routers/local_sources.py records it.
_ACTOR = "admin"


def _service(request: Request) -> RemoteLookup:
    service: RemoteLookup = request.app.state.remote_lookup
    return service


def _conflict(error: ValueError) -> HTTPException:
    return HTTPException(status_code=422 if isinstance(error, ValidationError) else 409, detail=str(error))


@router.get("/providers", response_model=tuple[ProviderStatus, ...])
async def providers(request: Request) -> tuple[ProviderStatus, ...]:
    """Every registered remote provider, enabled or not, with its stop state and stop code."""
    return await _service(request).provider_status()


@router.post("/lookup", response_model=LookupResult, dependencies=[Depends(require_companion_command_origin)])
async def lookup(command: LookupCommand, request: Request) -> LookupResult:
    """Discover candidates for one recording from one provider; they are held for review, never selected."""
    try:
        return await _service(request).lookup(command)
    except ValueError as exc:
        logger.info("remote_lookup_rejected", reason=str(exc))
        raise _conflict(exc) from exc


@router.post("/load", response_model=LoadResult, dependencies=[Depends(require_companion_command_origin)])
async def load(command: LoadCommand, request: Request) -> LoadResult:
    """Load one discovered candidate and import it as a pending version; selection is a separate decision."""
    try:
        return await _service(request).load(command)
    except ValueError as exc:
        logger.info("remote_load_rejected", reason=str(exc))
        raise _conflict(exc) from exc


@router.post("/decision", response_model=SelectedRecordingSource | None, dependencies=[Depends(require_companion_command_origin)])
async def decide(command: RemoteSourceDecision, session: Annotated[AsyncSession, Depends(get_session)]) -> SelectedRecordingSource | None:
    """The explicit operator select or reject of a loaded remote candidate."""
    try:
        async with session.begin_nested():
            result = await decide_remote_candidate(session, command, actor=_ACTOR)
    except ValueError as exc:
        logger.info("remote_decision_rejected", reason=str(exc))
        raise _conflict(exc) from exc
    await session.commit()
    logger.info("remote_decided", media_id=str(command.media_id), decision_id=str(command.decision_id), action=command.action)
    return result


@router.post("/providers/{provider_id}/clear-stop", response_model=ProviderStatus, dependencies=[Depends(require_companion_command_origin)])
async def clear_provider_stop(
    provider_id: Annotated[str, Path(max_length=64, pattern=r"^[a-z][a-z0-9_]*$")], command: ClearStopCommand, request: Request
) -> ProviderStatus:
    """Lift a provider's persistent stop. Records who cleared it and why; the next request is still paced."""
    service = _service(request)
    try:
        await service.clear_stop(provider_id, operator=_ACTOR, reason=command.reason)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return next(status for status in await service.provider_status() if status.provider_id == provider_id)
