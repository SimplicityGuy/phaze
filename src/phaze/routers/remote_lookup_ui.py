"""The recording page's provider-neutral look-up panel: native HTMX forms over the remote look-up service.

Every action is an explicit operator POST. ``GET`` only reads what is stored (``services.remote_lookup_view``): it
never issues a lookup, a load or any provider call, and the panel loads lazily so the record page itself needs no
remote service. These routes call the service layer directly rather than the JSON API in
``routers/remote_lookups.py``: the panel must re-render from stored state after every action (one partial, so the
first render and the post-action render cannot drift), and the JSON API returns models, not a view. The JSON API
stays the programmatic surface and is still a non-UI prefix in tests/shared/core/test_route_reachability.py.

Operator access and the server-owned audit identity follow routers/local_sources.py: a same-origin browser form,
and the actor is ``admin``, never a form field.
"""

from dataclasses import dataclass
from typing import Annotated, Any
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.database import get_session
from phaze.models.provider_source import ProviderRecordingCandidate
from phaze.routers.record import templates
from phaze.routers.request_guards import require_same_origin_form
from phaze.schemas.remote_lookup import LoadCommand, LoadResult, LookupCommand, LookupResult, OperatorOutcome, ProviderStatus, RemoteSourceDecision
from phaze.services.companion_view import validation_message
from phaze.services.remote_lookup import RemoteLookup, decide_remote_candidate
from phaze.services.remote_lookup_view import build_remote_panel


router = APIRouter(tags=["admin"])
logger = structlog.get_logger(__name__)
# The server-owned audit identity, as routers/local_sources.py and routers/remote_lookups.py record it.
_ACTOR = "admin"
_OPTIONAL_FORM_FIELDS = ("expected_selection_token", "expected_revision", "reason", "text_section_ordinal")


@dataclass(frozen=True)
class PanelNotice:
    """What the last action said, rendered as given: an outcome's status, code and message are never rewritten."""

    outcome: OperatorOutcome | None = None
    message: str | None = None
    failed: bool = False
    provider_name: str | None = None
    lookup: LookupResult | None = None
    load: LoadResult | None = None


def _service(request: Request) -> RemoteLookup | None:
    service: RemoteLookup | None = getattr(request.app.state, "remote_lookup", None)
    return service


async def _statuses(service: RemoteLookup | None) -> tuple[ProviderStatus, ...]:
    return await service.provider_status() if service is not None else ()


async def _render(
    request: Request, session: AsyncSession, file_id: uuid.UUID, notice: PanelNotice | None = None, *, status_code: int = 200
) -> HTMLResponse:
    service = _service(request)
    panel = await build_remote_panel(session, await _statuses(service), file_id, configured=service is not None)
    if panel is None:
        return HTMLResponse('<p role="alert" class="text-sm text-danger">This recording no longer exists.</p>', status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="record/remote/_panel.html",
        context={"request": request, "panel": panel, "notice": notice, "file_id": file_id},
        status_code=status_code,
    )


def _failure(error: ValueError) -> PanelNotice:
    return PanelNotice(message=validation_message(error) if isinstance(error, ValidationError) else str(error), failed=True)


async def _form(request: Request, *allowed: str) -> dict[str, Any]:
    """Only the named native-form fields; blank optional fields are absent, extras are ignored, not trusted."""
    raw = await request.form()
    values: dict[str, Any] = {}
    for key in allowed:
        value = raw.get(key)
        if isinstance(value, str) and not (value.strip() == "" and key in _OPTIONAL_FORM_FIELDS):
            values[key] = value
    return values


@router.get("/files/{file_id}/remote-lookup", response_class=HTMLResponse)
async def remote_lookup_panel(request: Request, file_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]) -> HTMLResponse:
    """Read-only: providers, stored candidates and loaded versions. Issues no lookup and no provider call."""
    return await _render(request, session, file_id)


@router.post("/files/{file_id}/remote-lookup/lookup", response_class=HTMLResponse, dependencies=[Depends(require_same_origin_form)])
async def remote_lookup_action(request: Request, file_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]) -> HTMLResponse:
    """The explicit operator action that asks one provider for candidates. They are held for review, never selected."""
    service = _service(request)
    if service is None:
        return await _render(request, session, file_id, PanelNotice(message="Remote look-up is not configured.", failed=True))
    try:
        command = LookupCommand.model_validate({**await _form(request, "provider_id"), "media_id": file_id})
        result = await service.lookup(command)
    except ValueError as exc:
        logger.info("remote_lookup_ui_rejected", reason=str(exc))
        return await _render(request, session, file_id, _failure(exc))
    names = {status.provider_id: status.display_name for status in await _statuses(service)}
    return await _render(request, session, file_id, PanelNotice(outcome=result.outcome, provider_name=names.get(result.provider_id), lookup=result))


@router.post("/files/{file_id}/remote-lookup/load", response_class=HTMLResponse, dependencies=[Depends(require_same_origin_form)])
async def remote_load_action(request: Request, file_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]) -> HTMLResponse:
    """The explicit operator action that loads one held candidate as a pending version. Selection is separate."""
    service = _service(request)
    if service is None:
        return await _render(request, session, file_id, PanelNotice(message="Remote look-up is not configured.", failed=True))
    try:
        command = LoadCommand.model_validate(await _form(request, "candidate_id"))
        row = await session.get(ProviderRecordingCandidate, command.candidate_id)
        if row is None or row.media_id != file_id:
            raise ValueError("Only a candidate held for this recording can be loaded")
        result = await service.load(command)
    except ValueError as exc:
        logger.info("remote_load_ui_rejected", reason=str(exc))
        return await _render(request, session, file_id, _failure(exc))
    return await _render(request, session, file_id, PanelNotice(outcome=result.outcome, load=result))


@router.post("/files/{file_id}/remote-lookup/decision", response_class=HTMLResponse, dependencies=[Depends(require_same_origin_form)])
async def remote_decision_action(request: Request, file_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]) -> HTMLResponse:
    """The explicit operator select, reject or reopen. Compare-and-set tokens come from the form; the actor does not."""
    try:
        values = await _form(
            request,
            "observation_id",
            "decision_id",
            "action",
            "expected_selection_token",
            "expected_revision",
            "expected_media_sha256",
            "accept_conflict",
            "text_section_ordinal",
            "accept_text_warnings",
            "reason",
        )
        values.setdefault("expected_revision", None)
        values["accept_conflict"] = values.get("accept_conflict") == "true"
        # Section mapping is carried only when the operator made a choice, so an unsectioned decision is unchanged.
        if values.pop("accept_text_warnings", None) == "true":
            values["accept_text_warnings"] = True
        command = RemoteSourceDecision.model_validate({**values, "media_id": file_id})
        async with session.begin_nested():
            await decide_remote_candidate(session, command, actor=_ACTOR)
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        logger.info("remote_decision_ui_rejected", reason=str(exc))
        return await _render(request, session, file_id, _failure(exc))
    logger.info("remote_decided", media_id=str(file_id), decision_id=str(command.decision_id), action=command.action)
    message = {
        "select": "Tracklist selected for this recording.",
        "reject": "Candidate rejected. It will not be proposed again unless you reopen it.",
        "reopen": "Candidate reopened and returned to review.",
    }[command.action]
    return await _render(request, session, file_id, PanelNotice(message=message))
