"""Duplicate resolution UI router -- serves the duplicate review workflow pages."""

import json
from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.services.dedup import (
    count_duplicate_groups,
    find_duplicate_groups_with_metadata,
    get_duplicate_stats,
    resolve_group,
    score_group,
    undo_resolve,
)
from phaze.services.proposal_queries import Pagination


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _filesizeformat(value: int | float) -> str:
    """Convert bytes to human-readable file size string."""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


templates.env.filters["filesizeformat"] = _filesizeformat

router = APIRouter(prefix="/duplicates", tags=["duplicates"])


def _compute_best_values(group: dict[str, Any]) -> dict[str, str]:
    """Compute which file has the best value for each numeric column.

    Returns {column_name: file_id} for columns where a best exists.
    For file_size: smallest is best.
    For bitrate: highest is best.
    For duration: longest is best.
    For tag_filled: most is best.
    """
    files = group["files"]
    best: dict[str, str] = {}

    # file_size: smallest is best
    valid = [(f["id"], f["file_size"]) for f in files if f.get("file_size") is not None]
    if valid and len({v for _, v in valid}) > 1:
        best["file_size"] = min(valid, key=lambda x: x[1])[0]

    # bitrate: highest is best
    valid = [(f["id"], f["bitrate"]) for f in files if f.get("bitrate") is not None and f["bitrate"] > 0]
    if valid and len({v for _, v in valid}) > 1:
        best["bitrate"] = max(valid, key=lambda x: x[1])[0]

    # duration: longest is best
    valid = [(f["id"], f["duration"]) for f in files if f.get("duration") is not None and f["duration"] > 0]
    if valid and len({v for _, v in valid}) > 1:
        best["duration"] = max(valid, key=lambda x: x[1])[0]

    # tag_filled: most is best
    valid = [(f["id"], f["tag_filled"]) for f in files if f.get("tag_filled") is not None]
    if valid and len({v for _, v in valid}) > 1:
        best["tag_filled"] = max(valid, key=lambda x: x[1])[0]

    return best


@router.get("/", response_class=HTMLResponse)
async def list_duplicates(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=10, le=100),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the duplicate groups list page, or an HTMX group list fragment."""
    offset = (page - 1) * page_size
    groups = await find_duplicate_groups_with_metadata(session, limit=page_size, offset=offset)
    stats = await get_duplicate_stats(session)
    total = await count_duplicate_groups(session)

    for group in groups:
        score_group(group)

    pagination = Pagination(page=page, page_size=page_size, total=total)

    context: dict[str, Any] = {
        "request": request,
        "groups": groups,
        "stats": stats,
        "pagination": pagination,
        "current_page": "duplicates",
    }

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request=request, name="duplicates/partials/group_list.html", context=context)

    return templates.TemplateResponse(request=request, name="duplicates/list.html", context=context)


@router.get("/{group_hash}/compare", response_class=HTMLResponse)
async def compare_group(
    request: Request,
    group_hash: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the comparison table partial for a single duplicate group."""
    groups = await find_duplicate_groups_with_metadata(session, limit=1000, offset=0)

    # Find the specific group by hash
    group = next((g for g in groups if g["sha256_hash"] == group_hash), None)
    if group is None:
        return HTMLResponse(content="<p class='text-sm text-gray-500'>Group not found.</p>", status_code=200)

    score_group(group)
    best_values = _compute_best_values(group)

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/comparison_table.html",
        context={
            "request": request,
            "group": group,
            "best_values": best_values,
        },
    )


@router.post("/{group_hash}/resolve", response_class=HTMLResponse)
async def resolve_group_endpoint(
    request: Request,
    group_hash: str,
    canonical_id: uuid.UUID = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Resolve a duplicate group by marking non-canonical files."""
    resolved_count, resolved_file_states = await resolve_group(session, group_hash, canonical_id)
    stats = await get_duplicate_stats(session)

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/resolve_response.html",
        context={
            "request": request,
            "stats": stats,
            "group_hash": group_hash,
            "resolved_count": resolved_count,
            "resolved_file_states": json.dumps(resolved_file_states),
            "canonical_id": str(canonical_id),
        },
    )


@router.post("/{group_hash}/undo", response_class=HTMLResponse)
async def undo_resolve_endpoint(
    request: Request,
    group_hash: str,
    file_states: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Undo a group resolution, restoring files to previous states."""
    parsed_states = json.loads(file_states)
    await undo_resolve(session, parsed_states)

    # Re-fetch group data after undo
    groups = await find_duplicate_groups_with_metadata(session, limit=1000, offset=0)
    group = next((g for g in groups if g["sha256_hash"] == group_hash), None)
    if group:
        score_group(group)

    stats = await get_duplicate_stats(session)

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/undo_response.html",
        context={
            "request": request,
            "group": group,
            "stats": stats,
        },
    )


@router.post("/resolve-all", response_class=HTMLResponse)
async def bulk_resolve(
    request: Request,
    page: int = Form(1),
    page_size: int = Form(20),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Bulk-resolve all duplicate groups on the current page."""
    offset = (page - 1) * page_size
    groups = await find_duplicate_groups_with_metadata(session, limit=page_size, offset=offset)

    all_file_states: list[dict[str, Any]] = []
    resolved_groups = 0
    for group in groups:
        score_group(group)
        canonical_id = uuid.UUID(group["canonical_id"])
        _count, file_states = await resolve_group(session, group["sha256_hash"], canonical_id)
        all_file_states.extend(file_states)
        resolved_groups += 1

    stats = await get_duplicate_stats(session)

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/bulk_resolve_response.html",
        context={
            "request": request,
            "stats": stats,
            "resolved_groups": resolved_groups,
            "all_file_states": json.dumps(all_file_states),
        },
    )


@router.post("/undo-all", response_class=HTMLResponse)
async def bulk_undo(
    request: Request,
    file_states: str = Form(...),
    page: int = Form(1),
    page_size: int = Form(20),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Undo a bulk resolution, restoring all files."""
    parsed_states = json.loads(file_states)
    await undo_resolve(session, parsed_states)

    # Re-fetch groups for the page
    offset = (page - 1) * page_size
    groups = await find_duplicate_groups_with_metadata(session, limit=page_size, offset=offset)
    for group in groups:
        score_group(group)

    stats = await get_duplicate_stats(session)
    total = await count_duplicate_groups(session)
    pagination = Pagination(page=page, page_size=page_size, total=total)

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/group_list.html",
        context={
            "request": request,
            "groups": groups,
            "stats": stats,
            "pagination": pagination,
            "current_page": "duplicates",
            "oob_stats": True,
        },
    )
