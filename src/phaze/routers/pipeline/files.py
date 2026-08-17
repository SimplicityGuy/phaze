"""Pipeline file matrix + per-stage file fragments."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func

from phaze.database import get_session
from phaze.enums.stage import Stage, Status
from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist
from phaze.routers.column_sort import SortableColumn, SortContract
from phaze.routers.pipeline._common import router, templates
from phaze.routers.response_shape import DUAL_SHAPE_RESPONSE_HEADERS, wants_fragment
from phaze.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MIN_PAGE_SIZE
from phaze.services.pipeline import (
    ANALYZE_FILTERS,
    ORPHANED_BUCKET,
    get_analyze_files_page,
    get_analyze_working_set,
    get_files_page,
    get_pending_files_page,
    get_tracklist_sets_page,
)
from phaze.services.stage_status import stage_status_sort_case


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# phaze-cavai: ORPHANED_BUCKET joins the lens allowlist -- a WHERE-only refinement of in_flight via
# orphaned_clause (services.pipeline._files_page_stmt), never a sixth per-row CASE arm (D-01a).
_VALID_BUCKETS: frozenset[str] = frozenset(s.value for s in Status) | {ORPHANED_BUCKET}


# --- phaze-a6hm.1 sortable-column contracts ------------------------------------------------------
# One SortContract per table, declared at import time so a mis-wired whitelist fails on startup
# rather than degrading every header click (column_sort contract rule 6). Each `expression` is a real
# column bound HERE -- a request `sort=` value is matched against these keys by equality and can
# never name anything else (rule 2). Every `target` is the table's EXISTING host container: this
# feature adds no OOB fragment and no new element id, so it cannot introduce a duplicate one.

# The pending-files fragment is shared machinery keyed by stage; each stage renders its own column
# labels, so each needs its own contract -- labels are how the shared partial recognises a header.
# Only ``metadata`` remains a pending-set workspace (phaze-0jpe removed the fingerprint one), but the
# dict shape is kept because it IS the per-stage seam, not a two-entry convenience.
_PENDING_SORTS: dict[str, SortContract] = {
    "metadata": SortContract(
        endpoint="/pipeline/pending-files",
        target="#metadata-files-view",
        columns=(
            SortableColumn(key="filename", label="File", expression=FileRecord.original_filename),
            SortableColumn(key="file_type", label="Format", expression=FileRecord.file_type),
            SortableColumn(key="file_size", label="Size", expression=FileRecord.file_size),
        ),
        default_key="filename",
    ),
}

# phaze-6not3: sort what is shown -- FILES_SORT's own "sort what you show" precedent (see its comment
# below) was violated on BOTH of these columns. `_tracklist_sets_page_stmt` already outerjoins
# `FileRecord`, so both expressions below can reach it directly (no additional join needed).
#   - "Set" renders `set_name = filename if matched else (artist or event or external_id)`
#     (services/pipeline.py::get_tracklist_sets_page) -- NOT bare `Tracklist.artist`, which put a
#     matched row's audio filename in a column that ordered by a value never shown for that row.
#   - "Tracklist" renders ONLY the two-value `tracklist_state` ("matched"/"candidate") derived from
#     `Tracklist.file_id IS NOT NULL` -- NOT `Tracklist.event`, a column that appears nowhere on
#     screen. `Tracklist.file_id.is_not(None)` groups matched apart from candidate exactly like the
#     rendered state does; a boolean ORDER BY sorts False-then-True (or the reverse on desc), which is
#     the grouping the header promises.
TRACKLIST_SETS_SORT = SortContract(
    endpoint="/pipeline/tracklist-sets",
    target="#tracklist-sets-view",
    columns=(
        SortableColumn(
            key="artist",
            label="Set",
            expression=func.coalesce(FileRecord.original_filename, Tracklist.artist, Tracklist.event, Tracklist.external_id),
        ),
        SortableColumn(key="event", label="Tracklist", expression=Tracklist.file_id.is_not(None)),
    ),
    default_key="artist",
)

# The Files matrix (:func:`pipeline_files`) -- ALL SEVEN rendered columns (phaze-cvn6.1).
#
# `key="file"` orders by the SAME column the File cell renders (`FileRecord.current_path`, the full
# path -- this table has no separate filename-only column), matching the sibling tables' "sort what
# you show" precedent.
#
# phaze-cvn6.1 -- WHY THE STAGE COLUMNS ARE HERE NOW. phaze-a6hm ("make every data table
# sortable") shipped this table with File + Type only; child phaze-a6hm.3 recorded the stage
# columns as "per-page DERIVED stage_status_case CASE expressions, not stable columns a SQL ORDER BY
# can address". That is a GAP, not a regression -- the columns were never sortable and nothing was
# lost -- but the stated reason does not hold: `_files_page_stmt` has ORDERED nothing by them, yet has
# FILTERED by the very same expression (`stage_status_case(stage) == bucket`) since Phase 87, so the
# expression plainly is addressable in SQL. What was really missing was a DEFINED ORDER for an
# enum-like status; alphabetical would interleave `failed` between `done` and `in_flight` and answer
# nothing. `stage_status_sort_case` supplies that order -- see STAGE_STATUS_DISPLAY_ORDER in
# `services/stage_status.py` for the ladder and its cost note.
#
# The five stage labels below MUST stay identical to `_stage_cols` in `files_table_view.html` -- a label is
# how the template recognises a header as sortable, so a typo degrades the header to plain text
# rather than erroring. `tests/integration/test_files_sort.py` asserts the two agree, in both
# directions, so the pair cannot drift silently.
#
# The 6-stage -> 5-pill remap LANDMINE applies to the KEYS too: `Appr` reads the `review` bucket and
# `Exec` reads `apply`. The wire keys are the canonical `Stage` values rather than the header words,
# so the URL names the model's vocabulary and no third naming scheme is invented.
FILES_SORT = SortContract(
    endpoint="/s/files",
    target="#files-table-view",
    columns=(
        SortableColumn(key="file", label="File", expression=FileRecord.current_path),
        SortableColumn(key="type", label="Type", expression=FileRecord.file_type),
        SortableColumn(key="metadata", label="Metadata", expression=stage_status_sort_case(Stage.METADATA)),
        SortableColumn(key="analyze", label="Analyze", expression=stage_status_sort_case(Stage.ANALYZE)),
        SortableColumn(key="propose", label="Propose", expression=stage_status_sort_case(Stage.PROPOSE)),
        SortableColumn(key="review", label="Review", expression=stage_status_sort_case(Stage.REVIEW)),
        SortableColumn(key="apply", label="Execute", expression=stage_status_sort_case(Stage.APPLY)),
    ),
    default_key="file",
)


@router.get("/pipeline/files", response_class=HTMLResponse)
async def pipeline_files(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    stage: str | None = Query(None),
    bucket: str | None = Query(None),
    sort: str | None = Query(None),
    order: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the paginated, per-row-derived files table (UI-01 / D-02).

    The scannable "where's this file at?" overview: each row carries the six-pill stage matrix
    derived per page (never the raw ``files.state`` column string, never a whole-corpus scan per poll
    -- see :func:`phaze.services.pipeline.get_files_page`). The ``stage``+``bucket`` query params are
    validated against the ``Stage`` / ``Status`` allowlists (T-87-14 -- an unknown value degrades to
    an unfiltered page rather than 422-ing the poll) and plumbed through NOW so Plan 05's status-filter
    bar is templates-only. The read is SAVEPOINT degrade-safe at the service layer, so NO router
    try/except -- a DB hiccup renders a safe empty page, never a 500.

    phaze-a6hm.3 sortable-column contract -- see :func:`pending_files_fragment`. ``stage``/``bucket``
    ride ``sort``'s ``view_state`` (contract rule 4) alongside ``page_size``, so a header click keeps
    the operator's filter lens and a Prev/Next click keeps the operator's chosen order via
    :meth:`~phaze.routers.column_sort.SortState.query_state` in the template.

    phaze-p7ox: ``_status_filter_bar.html``'s filter form and Clear-filter anchor both
    ``hx-push-url="true"`` THIS endpoint into the address bar (D-03's URL-carried-lens idiom), but
    this handler used to unconditionally return the chrome-less ``files_table_view.html`` fragment --
    no ``wants_fragment()`` fork, no full-document fallback. Per ``response_shape.py`` rule 2, a
    history-restore (Back/Forward after htmx's 10-entry cache evicts the snapshot -- routine) sets
    ``HX-Request: true`` too but IGNORES ``hx-target`` and swaps into ``<body>``, so the fragment
    replaced the whole page with an orphaned filter bar + table; a plain reload/bookmark of the
    pushed URL (no htmx headers at all) hit the exact same branch and served a raw fragment with no
    ``<html>``, CSS, htmx, or Alpine. A live htmx swap (``wants_fragment`` True) still gets the same
    bare fragment, unchanged.

    phaze-uvmcr.2: anything else -- a plain request or a restore -- used to get a rail-less full
    page (``pipeline/files.html``, its own extends-base.html fork of this same content --
    base.html itself was deleted by phaze-uvmcr.5, its last live caller gone).
    That page was redundant: ``"files"`` is a registered shell stage (``routers/shell.py``
    ``STAGE_PARTIALS``), reachable with the rail intact at ``/s/files``, which composes the SAME
    ``files_table_view.html`` fragment via ``pipeline/partials/files_workspace.html``. So the
    non-fragment branch now REDIRECTS there instead of rendering a second copy of the page, carrying
    the request's query string across (the filter/sort/pager state the URL-carried-lens idiom above
    rides on) so the redirected URL at least reads the same as the one that was bookmarked --
    ``/s/files`` itself is unchanged and does not re-derive a filtered page from it. This is the last
    caller of ``pipeline/files.html``, which phaze-uvmcr.2 deletes alongside this change.
    """
    if not wants_fragment(request):
        # phaze-r6e5m (response_shape.py contract rule 6): this same URL also answers with the
        # fragment below depending on request headers alone, so the redirect must be as
        # browser-uncacheable as the fragment is (see the corresponding note on execution.audit_log).
        query = request.url.query
        return RedirectResponse(url=f"/s/files?{query}" if query else "/s/files", status_code=302, headers=DUAL_SHAPE_RESPONSE_HEADERS)

    stage_enum: Stage | None = None
    if stage:
        try:
            stage_enum = Stage(stage)
        except ValueError:
            stage_enum = None
    bucket_val = bucket if bucket in _VALID_BUCKETS else None
    sort_state = FILES_SORT.resolve(
        sort=sort,
        order=order,
        view_state={"page_size": page_size, "stage": stage_enum.value if stage_enum is not None else None, "bucket": bucket_val},
    )
    files_page = await get_files_page(session, page=page, page_size=page_size, stage=stage_enum, bucket=bucket_val, sort=sort_state)
    context = {
        "files_page": files_page,
        "active_stage": stage_enum.value if stage_enum is not None else None,
        "active_bucket": bucket_val,
        "sort": sort_state,
    }
    return templates.TemplateResponse(
        request=request, name="pipeline/partials/files_table_view.html", context=context, headers=DUAL_SHAPE_RESPONSE_HEADERS
    )


@router.get("/pipeline/analyze-files", response_class=HTMLResponse)
async def analyze_files_fragment(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    status: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the Analyze per-file table fragment: the bounded default working set OR a filtered page (phaze-zqvh.2).

    The status-filter bar in ``_analyze_files.html`` hx-gets this endpoint into ``#analyze-files-view`` --
    the SAME URL-carried-lens idiom as ``/pipeline/files`` / ``_status_filter_bar.html``. ``status`` is
    validated against the ``ANALYZE_FILTERS`` allowlist (T-57-01 / T-87-14: an unknown value NEVER reaches
    a template path or SQL string -- it degrades to the default view, never a 422 into the render):

      * no / unknown ``status`` -> the DEFAULT bounded working-set view (the active-first working set,
        PAGED, plus the LIMIT-ed recent-completions window on the final page), with a pager.
      * a valid ``status`` -> the full analyze-stage listing under that lens, served as a bounded page
        (``get_analyze_files_page``: OFFSET + ``page_size + 1`` sentinel, never a whole-corpus COUNT).

    Both service reads are SAVEPOINT degrade-safe (never 500 the fragment). This endpoint is a SIBLING of
    the 5s ``/pipeline/stats`` poll -- it is NEVER in the poll's OOB fan-out, so the operator's page position
    and filter selection survive every tick (the file grid stays outside the poll, phaze-zqvh.2 acceptance).
    """
    status_val = status if status in ANALYZE_FILTERS else None
    if status_val is None:
        # DEFAULT bounded working-set view (no explicit filter): the active-first set, PAGED, with the
        # completions window appended on the final page. phaze-5462: this branch is now paged like every
        # other -- it used to return the whole unbounded working set.
        working_set = await get_analyze_working_set(session, page=page, page_size=page_size)
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_analyze_files.html",
            context={
                "analyze_rows": working_set.rows,
                "analyze_page": working_set,
                "active_status": None,
            },
        )
    analyze_page = await get_analyze_files_page(session, page=page, page_size=page_size, status=status_val)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_analyze_files.html",
        context={
            "analyze_rows": analyze_page.rows,
            "analyze_page": analyze_page,
            "active_status": status_val,
        },
    )


# phaze-5462: the stage allowlist for the shared pending-files fragment. Validated as a SET so an
# unknown value can NEVER reach SQL or a template path (T-57-01 / T-87-14) -- it degrades to
# "metadata", never a 422 into the render.
_PENDING_STAGES: dict[str, Stage] = {"metadata": Stage.METADATA}


@router.get("/pipeline/pending-files", response_class=HTMLResponse)
async def pending_files_fragment(
    request: Request,
    stage: str = Query("metadata"),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    sort: str | None = Query(None),
    order: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render ONE bounded page of an enrich workspace's pending set (phaze-5462).

    The sibling of :func:`analyze_files_fragment`, on the SAME paging contract
    (:mod:`phaze.services.pagination`): shared page size, OFFSET paging, a ``page_size + 1`` sentinel
    for ``has_next`` (never a whole-corpus COUNT), and the mandatory unique tiebreaker. Both enrich
    enrich workspaces hx-get this on load into their empty host div, so none server-renders a file
    row inline any more.

    ``stage`` is validated against :data:`_PENDING_STAGES` (unknown -> metadata) and is carried into
    the template only as an autoescaped query value -- never a template path (T-57-01).

    NOTE: this is the RENDER read. The EXTRACT ALL button still enqueues the
    UNBOUNDED pending set (paging contract rule 7) -- paging the enqueue would silently drop work.
    """
    stage_key = stage if stage in _PENDING_STAGES else "metadata"
    # phaze-a6hm.1: resolve BEFORE the read. `sort`/`order` are raw wire strings here and whitelisted
    # strings after; the query below never sees the untrusted value. `stage` rides view_state so a
    # header click keeps the operator on their own lens (contract rule 4). `page` deliberately does
    # NOT -- a re-sort returns to page 1.
    sort_state = _PENDING_SORTS[stage_key].resolve(sort=sort, order=order, view_state={"stage": stage_key, "page_size": page_size})
    pending_page = await get_pending_files_page(session, _PENDING_STAGES[stage_key], page=page, page_size=page_size, sort=sort_state)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_pending_files.html",
        context={
            "pending_page": pending_page,
            "stage": stage_key,
            "host_id": f"{stage_key}-files-view",
            "sort": sort_state,
        },
    )


@router.get("/pipeline/tracklist-sets", response_class=HTMLResponse)
async def tracklist_sets_fragment(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    sort: str | None = Query(None),
    order: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render ONE bounded page of the per-set Tracklist coverage table (phaze-1wvb).

    Same paging contract as :func:`pending_files_fragment`. The Tracklist workspace hx-gets this into
    the empty host div BELOW its three step cards; the step cards themselves (and their SEARCH /
    SCRAPE / MATCH ALL triggers, which enqueue the UNBOUNDED pending sets -- rule 7) are untouched
    and still server-rendered by the shell.
    """
    # phaze-a6hm.1 sortable-column contract -- see :func:`pending_files_fragment`.
    sort_state = TRACKLIST_SETS_SORT.resolve(sort=sort, order=order, view_state={"page_size": page_size})
    sets_page = await get_tracklist_sets_page(session, page=page, page_size=page_size, sort=sort_state)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_tracklist_sets.html",
        context={"sets_page": sets_page, "host_id": "tracklist-sets-view", "sort": sort_state},
    )
