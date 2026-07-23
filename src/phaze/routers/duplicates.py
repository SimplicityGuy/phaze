"""Duplicate resolution UI router -- serves the duplicate review workflow pages."""

import json
from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.routers.request_guards import parse_json_array_payload
from phaze.services.dedup import (
    find_duplicate_group_by_hash,
    find_duplicate_groups_by_hashes,
    get_duplicate_stats,
    resolve_group,
    score_group,
    undo_resolve,
)
from phaze.services.review import build_dupe_group_card


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


@router.get("/", response_class=RedirectResponse)
async def list_duplicates() -> RedirectResponse:
    """SHELL-05 (D-03): resolve a legacy ``/duplicates/`` bookmark into the v7.0 shell.

    phaze-y4s6: this used to also serve an in-page HX-filtered/paginated group list (rendering
    ``duplicates/partials/group_list.html``, composed of ``group_card.html`` + ``pagination.html``).
    The live v7.0 Dedupe workspace (``pipeline/partials/dedupe_workspace.html``) renders its
    ``dupe_group`` cards inline via ``services/review.get_dedupe_groups`` with no pagination and
    never hx-gets this bare path -- there was no live caller left to preserve an HX-filter branch
    for. The dead list/pagination templates were deleted outright.
    """
    return RedirectResponse(url="/s/dedupe", status_code=302)


@router.get("/{group_hash}/compare", response_class=HTMLResponse)
async def compare_group(
    request: Request,
    group_hash: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the comparison table partial for a single duplicate group."""
    # phaze-m7ya: a LOOKUP by hash, not a paged read. This used to fetch a hardcoded first 1000
    # groups and linear-scan them, so any group past that arbitrary boundary answered "Group not
    # found" forever while the list page still offered it a Compare button.
    group = await find_duplicate_group_by_hash(session, group_hash)
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
    # `services/` builds and flushes but never commits (caller-owned transaction). `get_session`
    # yields the session WITHOUT committing, so the router must — otherwise the marker and the
    # dual-written state are rolled back on session close and the HTMX partial reports a resolve
    # that never happened. Matches routers/tags.py:369, routers/tracklists.py.
    await session.commit()
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
    """Undo a group resolution, restoring files to previous states.

    phaze-wkqk: ``file_states`` is a raw client string, so the parse is guarded (untrusted-input
    contract rule 1, ``routers/request_guards.py``) -- a stale tab replaying a truncated payload
    gets a 422, not the unhandled ``JSONDecodeError`` 500 this used to raise. Rule 2's element half
    lives in ``undo_resolve``, which drops entries that are not dicts.
    """
    parsed_states = parse_json_array_payload(file_states, field="file_states")
    await undo_resolve(session, parsed_states)
    await session.commit()  # `get_session` does not commit; without this the undo is rolled back.

    # Re-fetch group data after undo
    # phaze-m7ya: keyed lookup, same reason as compare_group -- the capped scan silently dropped the
    # restored card (group=None) for any group outside the first 1000, so undo appeared to erase it.
    group = await find_duplicate_group_by_hash(session, group_hash)
    dupe_group_card = None
    if group:
        score_group(group)
        # phaze-be1j: the restored group must swap back into the Dedupe workspace shell as a live
        # _dupe_group.html card (build_dupe_group_card's shape), not the legacy group_card.html row.
        dupe_group_card = build_dupe_group_card(group)

    stats = await get_duplicate_stats(session)

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/undo_response.html",
        context={
            "request": request,
            "group": dupe_group_card,
            "group_hash": group_hash,
            "stats": stats,
        },
    )


@router.post("/resolve-all", response_class=HTMLResponse)
async def bulk_resolve(
    request: Request,
    group_hashes: list[str] = Form(default_factory=list),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Bulk-resolve exactly the duplicate groups the operator was shown.

    Resolves the operator-submitted ``group_hashes`` (the sha256 hashes rendered on the page/workspace
    at the time of the click) rather than re-deriving "the current page" via a fresh LIMIT/OFFSET query.
    That re-derivation is what let a never-reviewed group get auto-resolved: an unordered paginated query
    can select a different set of hashes than what was displayed, and even with stable ordering the set
    can still drift if another resolve committed between the page render and this POST (phaze-81bu).
    """
    groups = await find_duplicate_groups_by_hashes(session, group_hashes)

    all_file_states: list[dict[str, Any]] = []
    resolved_groups = 0
    for group in groups:
        if not group["files"]:
            # Already fully resolved (e.g. by a concurrent request) between display and this POST.
            continue
        score_group(group)
        canonical_id = uuid.UUID(group["canonical_id"])
        _count, file_states = await resolve_group(session, group["sha256_hash"], canonical_id)
        all_file_states.extend(file_states)
        resolved_groups += 1

    await session.commit()  # `get_session` does not commit; without this every resolve is rolled back.
    stats = await get_duplicate_stats(session)

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/bulk_resolve_response.html",
        context={
            "request": request,
            "stats": stats,
            "resolved_groups": resolved_groups,
            "all_file_states": json.dumps(all_file_states),
            # phaze-wgse: the SUBMITTED hashes (not just the ones actually resolved this pass), so the
            # response can OOB-remove every #dupe-group-{hash} card the operator was shown -- including
            # one a concurrent request already resolved between render and this POST (the `continue`
            # branch above), which is stale and must disappear too, not just the ones this call wrote.
            "group_hashes": group_hashes,
        },
    )


@router.post("/undo-all", response_class=HTMLResponse)
async def bulk_undo(
    request: Request,
    file_states: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Undo a bulk resolution, restoring all files.

    phaze-be1j: the toast's "Undo All" now targets the Dedupe workspace's persistent
    ``#dedupe-bulk-response`` status div (``innerHTML``) -- the old ``#duplicates-list`` target
    (and the full ``group_list.html`` legacy accordion-page response built for it) no longer
    exists in the v7 shell, so the request never fired. Bulk resolve doesn't OOB-touch individual
    group cards either (R-2), so there's no per-card DOM state to restore here -- just confirm
    the undo landed.

    phaze-wkqk: same guarded parse as ``undo_resolve_endpoint`` -- see the untrusted-input contract
    in ``routers/request_guards.py``.
    """
    parsed_states = parse_json_array_payload(file_states, field="file_states")
    restored_count = await undo_resolve(session, parsed_states)
    await session.commit()  # `get_session` does not commit; without this the bulk undo is rolled back.

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/bulk_undo_response.html",
        context={
            "request": request,
            "restored_count": restored_count,
        },
    )
