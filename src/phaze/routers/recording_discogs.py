"""Explicit selected-track Discogs matching and human review."""

from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.discogs_link import DiscogsLink
from phaze.services.enqueue_router import resolve_queue_for_task
from phaze.services.local_source_import import get_selected_recording_source
from phaze.services.pagination import clamp_page
from phaze.services.selected_discogs import decide_recording_discogs_link, read_recording_discogs_pin


router = APIRouter(prefix="/recording-discogs", tags=["tracklists"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


async def _render(request: Request, session: AsyncSession, media_id: uuid.UUID, *, message: str | None = None) -> HTMLResponse:
    selected = await get_selected_recording_source(session, media_id)
    try:
        page = clamp_page(int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    links = []
    if selected is not None and selected.tracks:
        links = list(
            (
                await session.execute(
                    select(DiscogsLink)
                    .where(
                        DiscogsLink.source_observation_id == selected.observation_id,
                        DiscogsLink.source_track_position.in_([t.position for t in selected.tracks]),
                    )
                    .order_by(DiscogsLink.source_track_position, DiscogsLink.confidence.desc(), DiscogsLink.id)
                    .offset((page - 1) * 50)
                    .limit(51)
                )
            )
            .scalars()
            .all()
        )
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_recording_discogs.html",
        context={"media_id": media_id, "selected": selected, "links": links[:50], "has_next": len(links) > 50, "page": page, "message": message},
    )


@router.get("/{media_id}", response_class=HTMLResponse)
async def recording_discogs_review(request: Request, media_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return await _render(request, session, media_id)


@router.post("/{media_id}/match", response_class=HTMLResponse)
async def match_recording_discogs(
    request: Request,
    media_id: uuid.UUID,
    observation_id: uuid.UUID = Form(...),
    selected_token: uuid.UUID = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        await read_recording_discogs_pin(session, media_id, observation_id, selected_token)
    except ValueError as exc:
        return await _render(request, session, media_id, message=str(exc))
    routed = await resolve_queue_for_task("match_recording_source_to_discogs", request.app.state, session)
    await session.commit()
    await routed.queue.enqueue(
        "match_recording_source_to_discogs",
        media_id=str(media_id),
        expected_observation_id=str(observation_id),
        expected_selection_token=str(selected_token),
    )
    return await _render(request, session, media_id, message="Discogs matching queued for this reviewed source. Refresh to inspect candidates.")


@router.post("/{media_id}/links/{link_id}", response_class=HTMLResponse)
async def decide_recording_discogs(
    request: Request,
    media_id: uuid.UUID,
    link_id: uuid.UUID,
    observation_id: uuid.UUID = Form(...),
    selected_token: uuid.UUID = Form(...),
    action: str = Form(..., max_length=16),  # Small fixed decision vocabulary bounds form input.
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        if action not in {"accept", "dismiss"}:
            raise ValueError("Choose accept or dismiss")
        await decide_recording_discogs_link(
            session, media_id=media_id, observation_id=observation_id, selected_token=selected_token, link_id=link_id, accept=action == "accept"
        )
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        return await _render(request, session, media_id, message=str(exc))
    return await _render(request, session, media_id, message="Discogs decision saved for the immutable selected track.")
