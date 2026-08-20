"""Duplicate resolution UI router -- serves the duplicate review workflow pages."""

import json
from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.routers.request_guards import parse_json_array_payload
from phaze.schemas.wire_bounds import INT32_MAX

# phaze-nt8f: `get_duplicate_stats` is deliberately NOT imported here any more. Every resolve /
# undo / bulk-resolve used to call it purely to populate the `#stats-header` OOB fragment, whose
# target id died with duplicates/list.html in the Phase-62 cutover -- three whole-corpus aggregate
# scans per mutation (count_duplicate_groups + the total_files/SUM(file_size) HAVING subquery +
# the SUM(MAX(file_size)) GROUP BY subquery) for a fragment the browser discarded. The service
# itself is kept (services/dedup.py, covered by tests/discovery/services/test_dedup.py): it is the
# read a future Dedupe stats surface would use. Re-import it only alongside a LIVE host for it.
#
# `count_duplicate_groups` (a DIFFERENT function -- the plain corpus-wide GROUP count, not the
# three-query stats bundle above) IS imported: phaze-4iq5t's "Load more" fragment is exactly the
# live host this note asks for before re-adding a whole-corpus scan here.
from phaze.services.dedup import (
    GROUP_PAGE_SIZE,
    count_duplicate_groups,
    find_duplicate_group_by_hash,
    find_duplicate_groups_by_hashes,
    score_group,
    undo_resolve,
)
from phaze.services.dedup_review import InvalidDedupReviewPlanError, commit_review_plans, create_review_plan
from phaze.services.pg_text import contains_pg_invalid_chars
from phaze.services.review import build_dupe_group_card, dedupe_subcount_text, get_dedupe_groups


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


# (column, prefer, positive_only) -- the four numeric columns the duplicate table marks a
# "best" value for, in render order. ``prefer`` is the selector applied to the surviving
# (file_id, value) pairs: ``min`` for file_size (smallest wins), ``max`` for the rest.
# ``positive_only`` additionally drops non-positive values, which bitrate and duration treat
# as "absent" rather than as a genuine low reading.
_BEST_VALUE_COLUMNS: tuple[tuple[str, Any, bool], ...] = (
    ("file_size", min, False),
    ("bitrate", max, True),
    ("duration", max, True),
    ("tag_filled", max, False),
)


def _best_file_for_column(files: list[dict[str, Any]], column: str, prefer: Any, positive_only: bool) -> Any:
    """The id of the file holding the best value for ``column``, or None when there is no best.

    "No best" covers both degenerate cases the table must not mark a winner for: nothing has a
    usable value, and everything that does shares the SAME value (a highlight would then be
    arbitrary among ties).
    """
    valid = [(f["id"], f[column]) for f in files if f.get(column) is not None and (not positive_only or f[column] > 0)]
    if not valid or len({value for _, value in valid}) < 2:
        return None
    return prefer(valid, key=lambda pair: pair[1])[0]


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
    for column, prefer, positive_only in _BEST_VALUE_COLUMNS:
        winner = _best_file_for_column(files, column, prefer, positive_only)
        if winner is not None:
            best[column] = winner
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

    phaze-4iq5t: that "no pagination" description is STILL accurate for this exact bare path (it
    still 302s straight through, unconditionally) but stopped being true of the Dedupe workspace as
    a whole -- ``get_dedupe_groups`` silently rendered only the first ``GROUP_PAGE_SIZE`` groups by
    hash order forever, with no way to see the rest and no signal that more existed. See
    ``load_more_groups`` below, the new live caller pagination was reinstated for.
    """
    return RedirectResponse(url="/s/dedupe", status_code=302)


@router.get("/groups", response_class=HTMLResponse)
async def load_more_groups(
    request: Request,
    # phaze-4iq5t: mirrors companion.py's list_duplicates() bounds -- find_duplicate_groups(_with_metadata)
    # takes a raw limit/offset (not phaze.services.pagination's page/page_size), so this route owns its
    # own ge=/le= guard per wire_bounds rule 8; a negative OFFSET 500s in Postgres rather than clamping.
    offset: int = Query(0, ge=0, le=INT32_MAX),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX "Load more" fragment for the Dedupe workspace: the next GROUP_PAGE_SIZE duplicate groups.

    phaze-4iq5t: reinstates paging for ``services/review.get_dedupe_groups`` -- the offset machinery
    in ``services/dedup.py`` survived the phaze-y4s6/Phase-62 cutover documented in
    ``list_duplicates`` above (that cutover deleted the OLD ``/duplicates/`` list page's pagination
    templates, correctly, because nothing called them any more) but nothing ever called
    ``get_dedupe_groups`` with an offset override afterwards either, so the Dedupe workspace stayed
    silently pinned to page 1 forever. THIS endpoint is the live caller now -- do not delete the
    offset plumbing again on the "no live caller" reasoning without checking for this one first.

    Bounded per request to ``GROUP_PAGE_SIZE`` groups (never an unbounded render, AC3): the primary
    response replaces ``#dedupe-load-more-wrap`` with either the next "Load more" button or nothing
    (exhausted); the new cards, their bulk-review hidden inputs, and the workspace subcount all land
    via the SAME ``hx-swap-oob="beforeend:#<id>"`` / ``hx-swap-oob="true"`` techniques
    ``pipeline/partials/dedupe_workspace.html`` already documents for the bulk-undo response.
    """
    groups = await get_dedupe_groups(session, offset=offset)
    total = await count_duplicate_groups(session)
    rendered_through = offset + len(groups)
    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/_dedupe_group_page.html",
        context={
            "request": request,
            "groups": groups,
            "rendered_through": rendered_through,
            "total_groups": total,
            "page_size": GROUP_PAGE_SIZE,
            "has_more": rendered_through < total,
            "subcount_text": dedupe_subcount_text(rendered_through, total),
        },
    )


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
    #
    # phaze-i0jqu: skip the query entirely if group_hash carries a NUL/lone surrogate PostgreSQL
    # cannot bind in a UTF8 text parameter (asyncpg raises CharacterNotInRepertoireError and aborts
    # the transaction). No group could ever be keyed on such a hash, so it degrades to the SAME
    # "Group not found" answer as a hash that is merely unknown, rather than an unhandled 500.
    group = None if contains_pg_invalid_chars(group_hash) else await find_duplicate_group_by_hash(session, group_hash)
    if group is None:
        return HTMLResponse(content="<p class='text-sm text-muted'>Group not found.</p>", status_code=200)

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


async def _render_resolve_noop(request: Request, session: AsyncSession, group_hash: str) -> HTMLResponse:
    """Render the group's CURRENT (unchanged) state with an honest "nothing changed" toast.

    Shared by every 0-resolved shape ``resolve_group_endpoint`` can hit (phaze-ptzse): an invalid
    group_hash (phaze-i0jqu, never queried below), a stale resubmit of an already-resolved card, a
    concurrent resolve that already claimed the canonical, and a group member deleted mid-flight
    (phaze-a3if1) -- ``resolved_count`` is the single honest signal for all four, so the router does
    not need to distinguish them.

    Re-queries by hash rather than trusting the (possibly stale) group state the request started
    with -- the card must reflect what is actually still in the database, live and re-resolvable, not
    a snapshot from before whatever made this resolve a no-op.
    """
    group = None if contains_pg_invalid_chars(group_hash) else await find_duplicate_group_by_hash(session, group_hash)
    card = None
    if group:
        score_group(group)
        card = build_dupe_group_card(group)

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/resolve_noop_response.html",
        context={"request": request, "group_hash": group_hash, "group": card},
    )


@router.post("/{group_hash}/review", response_class=HTMLResponse)
async def review_group_endpoint(
    request: Request,
    group_hash: str,
    canonical_id: uuid.UUID = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Create an opaque plan for one fully visible group without resolving it."""
    group = None if contains_pg_invalid_chars(group_hash) else await find_duplicate_group_by_hash(session, group_hash)
    if group is None:
        return await _render_resolve_noop(request, session, group_hash)
    score_group(group)
    try:
        plan = create_review_plan(session, group, canonical_id)
    except InvalidDedupReviewPlanError:
        return await _render_resolve_noop(request, session, group_hash)
    await session.commit()
    card = build_dupe_group_card(group)
    keeper = next(file for file in card["files"] if str(file["id"]) == str(canonical_id))
    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/review_response.html",
        context={"request": request, "group": card, "keeper": keeper, "plan_id": plan.id},
    )


@router.post("/{group_hash}/resolve", response_class=HTMLResponse)
async def resolve_group_endpoint(
    request: Request,
    group_hash: str,
    plan_id: uuid.UUID = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Commit exactly one durable reviewed plan; reject stale, replayed, or forged plans."""
    if contains_pg_invalid_chars(group_hash):
        return await _render_resolve_noop(request, session, group_hash)
    try:
        resolved_groups, resolved_file_states, _hashes = await commit_review_plans(session, [plan_id], expected_group_hash=group_hash)
    except InvalidDedupReviewPlanError:
        await session.rollback()
        return await _render_resolve_noop(request, session, group_hash)
    await session.commit()
    if resolved_groups != 1:
        return await _render_resolve_noop(request, session, group_hash)
    resolved_count = len(resolved_file_states)

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/resolve_response.html",
        context={
            "request": request,
            "group_hash": group_hash,
            "resolved_count": resolved_count,
            "resolved_file_states": json.dumps(resolved_file_states),
        },
    )


@router.post("/review-all", response_class=HTMLResponse)
async def review_bulk_resolve(
    request: Request,
    group_hashes: list[str] = Form(default_factory=list),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the highest-quality bulk plan without resolving any duplicate group."""
    safe_hashes = [group_hash for group_hash in group_hashes if not contains_pg_invalid_chars(group_hash)]
    groups = await find_duplicate_groups_by_hashes(session, safe_hashes)
    cards: list[dict[str, Any]] = []
    truncated_count = 0
    for group in groups:
        if group["count"] <= 1:
            continue
        score_group(group)
        try:
            plan = create_review_plan(session, group, uuid.UUID(group["canonical_id"]))
        except InvalidDedupReviewPlanError:
            truncated_count += 1
            continue
        await session.flush()
        card = build_dupe_group_card(group)
        card["plan_id"] = plan.id
        cards.append(card)
    await session.commit()

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/bulk_review.html",
        context={
            "request": request,
            "dedupe_groups": cards,
            "stale_count": len(group_hashes) - len(groups),
            "truncated_count": truncated_count,
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

    phaze-i0jqu: the undo itself never references ``group_hash`` -- it is scoped entirely by the
    (file_id, canonical_id) pairs in ``file_states`` -- so a NUL/lone-surrogate ``group_hash``
    cannot corrupt the undo. It only drives the re-fetch below, which is skipped entirely for an
    invalid hash (no group could ever be keyed on one) rather than binding it into a query and
    raising asyncpg's ``CharacterNotInRepertoireError`` AFTER the undo has already committed.
    """
    parsed_states = parse_json_array_payload(file_states, field="file_states")
    await undo_resolve(session, parsed_states)
    await session.commit()  # `get_session` does not commit; without this the undo is rolled back.

    # Re-fetch group data after undo
    # phaze-m7ya: keyed lookup, same reason as compare_group -- the capped scan silently dropped the
    # restored card (group=None) for any group outside the first 1000, so undo appeared to erase it.
    group = None if contains_pg_invalid_chars(group_hash) else await find_duplicate_group_by_hash(session, group_hash)
    dupe_group_card = None
    if group:
        score_group(group)
        # phaze-be1j: the restored group must swap back into the Dedupe workspace shell as a live
        # _dupe_group.html card (build_dupe_group_card's shape), not the legacy group_card.html row.
        dupe_group_card = build_dupe_group_card(group)

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/undo_response.html",
        context={
            "request": request,
            "group": dupe_group_card,
            "group_hash": group_hash,
        },
    )


@router.post("/resolve-all", response_class=HTMLResponse)
async def bulk_resolve(
    request: Request,
    plan_ids: list[uuid.UUID] = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Atomically commit only opaque, complete, still-current reviewed plans."""
    try:
        resolved_groups, all_file_states, group_hashes = await commit_review_plans(session, plan_ids)
    except InvalidDedupReviewPlanError:
        await session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="duplicates/partials/bulk_review_stale.html",
            context={"request": request},
        )
    await session.commit()

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/bulk_resolve_response.html",
        context={
            "request": request,
            "resolved_groups": resolved_groups,
            "all_file_states": json.dumps(all_file_states),
            "group_hashes": group_hashes,
        },
    )


@router.post("/undo-all", response_class=HTMLResponse)
async def bulk_undo(
    request: Request,
    file_states: str = Form(...),
    group_hashes: list[str] = Form(default_factory=list),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Undo a bulk resolution, restoring all files AND the group cards the resolve removed.

    phaze-be1j: the toast's "Undo All" targets the Dedupe workspace's persistent
    ``#dedupe-bulk-response`` status div (``innerHTML``) -- the old ``#duplicates-list`` target
    (and the full ``group_list.html`` legacy accordion-page response built for it) no longer
    exists in the v7 shell, so the request never fired.

    phaze-dpc5: this used to stop there, on the premise that "bulk resolve doesn't OOB-touch
    individual group cards either (R-2), so there's no per-card DOM state to restore". phaze-wgse
    had since made ``bulk_resolve_response.html`` OOB-DELETE every ``#dupe-group-{hash}`` card the
    operator was shown, and nothing re-renders the Dedupe workspace on its own (the single chrome
    poll only seeds hidden counters; the body is produced by a rail navigation). So the undo
    committed correctly and truthfully reported "N files restored" into a workspace with no cards
    left in it. The response now rebuilds those cards: ``group_hashes`` rides the undo toast (the
    SAME submitted set bulk resolve deleted, phaze-81bu's "act on exactly what the operator was
    shown"), and the restored groups are re-read and OOB-appended to ``#dedupe-group-list``.

    The read is deliberately AFTER the commit: ``find_duplicate_groups_by_hashes`` excludes
    dedup-resolved files, so before the marker DELETE lands it would return the groups still
    collapsed to a single file (or nothing at all) and "restore" empty cards.

    ``group_hashes`` defaults to empty rather than being required: a stale tab whose toast predates
    this change still undoes correctly (markers deleted, honest count), it simply restores no cards
    -- degrading to the old behaviour instead of 422-ing a reversal the operator has 10 seconds to
    click.

    phaze-wkqk: same guarded parse as ``undo_resolve_endpoint`` -- see the untrusted-input contract
    in ``routers/request_guards.py``.

    phaze-i0jqu: a NUL/lone-surrogate hash in ``group_hashes`` is dropped before the re-hydration
    read below, the same as ``bulk_resolve`` -- PostgreSQL cannot bind either, so it would otherwise
    500 the whole undo response on asyncpg's ``CharacterNotInRepertoireError``.

    phaze-cvjby: unlike ``find_duplicate_group_by_hash`` (``count > 1``) and the workspace read's own
    ``HAVING count(id) > 1``, ``find_duplicate_groups_by_hashes`` has no group-size filter -- for a
    hash whose group is still (or is once again) resolved down to one file, it returns that lone
    survivor as a 1-member "group". Every OTHER render source in this router enforces ``count > 1``;
    this loop is the one that didn't, so it could OOB-append a live keeper-select card for a file
    that has no duplicates at all. Skipped here, mirroring ``find_duplicate_group_by_hash``'s own
    invariant, rather than weakening ``find_duplicate_groups_by_hashes`` for its OTHER caller
    (``bulk_resolve``, which tolerates a 1-member group as a harmless ``resolve_group`` no-op).
    """
    parsed_states = parse_json_array_payload(file_states, field="file_states")
    restored_count = await undo_resolve(session, parsed_states)
    await session.commit()  # `get_session` does not commit; without this the bulk undo is rolled back.

    restored_groups: list[dict[str, Any]] = []
    if restored_count and group_hashes:
        safe_hashes = [h for h in group_hashes if not contains_pg_invalid_chars(h)]
        for group in await find_duplicate_groups_by_hashes(session, safe_hashes):
            if group["count"] <= 1:
                continue
            score_group(group)
            restored_groups.append(build_dupe_group_card(group))

    return templates.TemplateResponse(
        request=request,
        name="duplicates/partials/bulk_undo_response.html",
        context={
            "request": request,
            "restored_count": restored_count,
            "dedupe_groups": restored_groups,
        },
    )
