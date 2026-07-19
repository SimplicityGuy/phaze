"""Regression tests for the phaze-5462 paging contract (:mod:`phaze.services.pagination`).

Two failure modes are guarded here, because both are SILENT in production:

  1. AN UNBOUNDED LIST. The Analyze workspace shipped 10,132 rows / 12.7 MB inline behind a
     docstring that merely asserted the working set was "naturally bounded". A stuck backlog grew the
     response without limit. ``test_*_bounded_regardless_of_backlog_size`` fails if any of the three
     enrich reads ever again returns more rows than a page.

  2. A NON-UNIQUE ``ORDER BY`` UNDER OFFSET PAGING. Rows that tie on the sort key have NO guaranteed
     relative order, so paging can skip a row entirely or show it twice -- with no error, and
     invisibly to any test that only looks at page 1. The ``_stable_across_pages`` tests walk EVERY
     page over a set engineered so the primary sort key ties on every row, and assert the union is
     exactly the seeded set with no duplicates. These are the tests the five sibling paging beads
     (phaze-39ss / mft5 / hdho) should copy.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.services import pipeline as pipeline_mod
from phaze.services.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MIN_PAGE_SIZE,
    Page,
    clamp_page,
    clamp_page_size,
    paged_stmt,
    split_sentinel,
)
from phaze.services.pipeline import get_analyze_files_page, get_analyze_working_set, get_pending_files_page

from .test_pipeline import _backend_settings, _make_pipeline_file


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# --------------------------------------------------------------------------------------------------
# Pure contract helpers -- no DB.
# --------------------------------------------------------------------------------------------------


def test_paged_stmt_refuses_a_missing_tiebreaker() -> None:
    """Contract rule 4: an ORDER BY with no unique tiebreaker must fail LOUDLY at construction.

    This is the whole point of making ``tiebreaker`` required rather than optional -- the alternative
    failure mode is silent row-skipping in production that no page-1 test would ever catch.
    """
    with pytest.raises(ValueError, match="unique `tiebreaker`"):
        paged_stmt(select(FileRecord), page=1, page_size=10, order_by=(FileRecord.created_at.desc(),), tiebreaker=())


@pytest.mark.parametrize(("given", "expected"), [(-100, 1), (-1, 1), (0, 1), (1, 1), (2, 2), (9999, 9999)])
def test_clamp_page_never_raises(given: int, expected: int) -> None:
    """Contract rule 5: out-of-range pages CLAMP to 1; they never raise and never 422 a render.

    phaze-hpo9 (negative limit/offset validation) is a separate bead, but this is the defined answer
    it must apply -- pinned here so that bead has something to align to.
    """
    assert clamp_page(given) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [(-50, MIN_PAGE_SIZE), (0, MIN_PAGE_SIZE), (1, MIN_PAGE_SIZE), (50, 50), (10_000, MAX_PAGE_SIZE)],
)
def test_clamp_page_size_never_raises(given: int, expected: int) -> None:
    """Contract rule 5: page sizes clamp into [MIN, MAX] -- including negatives (phaze-hpo9's answer)."""
    assert clamp_page_size(given) == expected


def test_split_sentinel_reports_has_next_without_a_count() -> None:
    """Contract rule 2: ``has_next`` comes from the +1 sentinel row, never a COUNT."""
    full, has_next = split_sentinel(list(range(11)), 10)
    assert has_next is True
    assert full == list(range(10)), "the sentinel row is trimmed off the rendered page"

    exact, has_next = split_sentinel(list(range(10)), 10)
    assert has_next is False
    assert exact == list(range(10))


def test_page_has_no_total_field() -> None:
    """Contract rule 2: ``Page`` deliberately exposes no total/page-count -- that would need a COUNT.

    Guards against a well-meaning "page X of Y" feature reintroducing the whole-corpus scan.
    """
    fields = set(Page().__dataclass_fields__)
    assert fields == {"rows", "page", "page_size", "has_next"}
    assert not any(hasattr(Page(), attr) for attr in ("total", "page_count", "total_pages"))


def test_page_pager_affordances() -> None:
    """The Prev/Next affordances the templates bind to (contract rule 2: affordances, not page numbers)."""
    first_of_many = Page(rows=[1], page=1, has_next=True)
    assert first_of_many.has_prev is False
    assert first_of_many.show_pager is True

    middle = Page(rows=[1], page=3, has_next=True)
    assert middle.has_prev is True
    assert middle.show_pager is True

    last = Page(rows=[1], page=4, has_next=False)
    assert last.has_prev is True
    assert last.show_pager is True

    only_page = Page(rows=[1], page=1, has_next=False)
    assert only_page.has_prev is False
    assert only_page.show_pager is False, "a single-page list renders no pager at all"


# --------------------------------------------------------------------------------------------------
# The bound: no enrich list may grow with its backlog.
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_working_set_bounded_regardless_of_backlog_size(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-5462 CORE REGRESSION: a large stuck analyze backlog must NOT grow the response.

    Seeds 40 files that are ALL in the active working set via the disjunct that actually caused the
    production blow-up -- a partial ``analysis`` row (present, not completed, not failed), i.e. the
    failure/stall backlog. Before the fix this branch had NO LIMIT and returned all 40 (and 10,132 in
    prod). Now it returns at most one page, and reports ``has_next`` so the operator can reach the rest.
    """
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _backend_settings())

    backlog = [_make_pipeline_file() for _ in range(40)]
    session.add_all(backlog)
    await session.flush()
    for f in backlog:
        session.add(AnalysisResult(id=uuid.uuid4(), file_id=f.id, fine_windows_analyzed=1, fine_windows_total=10))
    await session.commit()

    page = await get_analyze_working_set(session, page=1, page_size=MIN_PAGE_SIZE)

    assert len(page.rows) <= MIN_PAGE_SIZE, f"the analyze working set is UNBOUNDED again -- {len(page.rows)} rows for a 40-file backlog"
    assert page.has_next is True, "a backlog larger than one page must advertise a next page, not silently truncate"


@pytest.mark.asyncio
async def test_pending_files_page_bounded_regardless_of_backlog_size(session: AsyncSession) -> None:
    """phaze-5462: the metadata/fingerprint workspaces are bounded too.

    The bead assumed these two "already page". They did not -- ``get_metadata_pending_files`` /
    ``get_fingerprint_pending_files`` have no LIMIT and no ORDER BY; those tabs measured a harmless
    ~70 KB purely because their backlogs are empty in production today. This seeds a real backlog and
    asserts the RENDER read stays bounded.
    """
    backlog = [_make_pipeline_file() for _ in range(40)]
    session.add_all(backlog)
    await session.commit()

    for stage in (Stage.METADATA, Stage.FINGERPRINT):
        page = await get_pending_files_page(session, stage, page=1, page_size=MIN_PAGE_SIZE)
        assert len(page.rows) <= MIN_PAGE_SIZE, f"{stage.value} pending render read is unbounded ({len(page.rows)} rows)"


def test_enrich_workspaces_render_no_file_rows_inline() -> None:
    """phaze-5462 acceptance: none of the three enrich workspaces may server-render a file list inline.

    The 12.7 MB Analyze payload came from the workspace template ``{% include %}``-ing the file table
    with the whole working set already in context. All three now ship an EMPTY host div that hx-gets a
    bounded, paged fragment on load. This is a structural guard: it fails if anyone reintroduces an
    inline row loop or include on a landing path, which is precisely how phaze-zqvh's partial fix
    regressed into this bead.
    """
    templates = pathlib.Path("src/phaze/templates/pipeline/partials")
    cases = {
        "analyze_workspace.html": ("analyze-files-view", "/pipeline/analyze-files"),
        "metadata_workspace.html": ("metadata-files-view", "/pipeline/pending-files?stage=metadata"),
        "fingerprint_workspace.html": ("fingerprint-files-view", "/pipeline/pending-files?stage=fingerprint"),
    }
    for name, (host_id, endpoint) in cases.items():
        body = (templates / name).read_text()
        assert f'id="{host_id}"' in body, f"{name} lost its lazy-load host div"
        assert f'hx-get="{endpoint}"' in body, f"{name} no longer lazy-loads its bounded fragment"
        assert 'hx-trigger="load"' in body, f"{name} host div must load the fragment on render"
        assert '{% include "pipeline/partials/_analyze_files.html" %}' not in body, f"{name} re-inlines the file table -- the 12.7 MB regression"
        assert '{% include "pipeline/partials/_file_table.html" %}' not in body, f"{name} re-inlines the file table on the landing path"
        assert "{% for f in" not in body, f"{name} re-introduced an inline per-file row loop"


def test_shell_router_does_not_read_unbounded_pending_sets() -> None:
    """phaze-5462: the shell must not seed the unbounded ``get_*_pending_files`` reads into a render.

    Those two readers stay UNBOUNDED on purpose (contract rule 7 -- they are the ENQUEUE set), so the
    guard is that the RENDER path no longer calls them, not that they gained a LIMIT.
    """
    shell_src = pathlib.Path("src/phaze/routers/shell.py").read_text()
    assert "get_metadata_pending_files(" not in shell_src, "the shell render path must use the bounded get_pending_files_page"
    assert "get_fingerprint_pending_files(" not in shell_src, "the shell render path must use the bounded get_pending_files_page"


def test_bulk_enqueue_still_uses_the_unbounded_pending_set() -> None:
    """Contract rule 7: paging the ENQUEUE set would silently under-enqueue the backlog.

    The inverse guard of the one above -- it must stay true that the bulk triggers read the FULL set.
    A future "consistency" refactor that points EXTRACT ALL at the paged reader would quietly stop
    enqueuing everything past page 1, which is far worse than a long table.
    """
    router_src = pathlib.Path("src/phaze/routers/pipeline.py").read_text()
    assert "get_metadata_pending_files(session)" in router_src, "EXTRACT ALL must enqueue the UNBOUNDED pending set"
    assert "get_fingerprint_pending_files(session)" in router_src, "FINGERPRINT ALL must enqueue the UNBOUNDED pending set"


# --------------------------------------------------------------------------------------------------
# The tiebreaker: paging must be stable when the primary sort key ties.
# --------------------------------------------------------------------------------------------------


async def _walk_all_pages(fetch, page_size: int) -> list[str]:  # type: ignore[no-untyped-def]
    """Walk every page via ``has_next`` and return the concatenated row identity list."""
    seen: list[str] = []
    page_no = 1
    while True:
        page = await fetch(page_no)
        seen.extend(str(getattr(row, "id", None) or row["file_id"]) for row in page.rows)
        if not page.has_next or page_no > 50:
            return seen
        page_no += 1


@pytest.mark.asyncio
async def test_analyze_paging_is_stable_when_the_sort_key_ties(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-5462 CORE REGRESSION (contract rule 4): no row is skipped or duplicated across pages.

    Every seeded file is inserted in ONE transaction, so ``created_at`` -- the primary display sort
    key -- is IDENTICAL on all of them (Postgres timestamp defaults are transaction-time constant).
    That makes the sort key fully degenerate, which is exactly the condition under which a pager
    lacking a unique tiebreaker silently skips and duplicates rows. Walking every page must still
    yield each file EXACTLY once.

    This is the test shape the sibling beads should copy: phaze-39ss (ts_rank), phaze-mft5
    (executed_at) and phaze-hdho (non-deterministic ORDER BY) all have the same degenerate-key defect.
    """
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _backend_settings())

    files = [_make_pipeline_file() for _ in range(25)]
    session.add_all(files)
    await session.flush()
    for f in files:
        session.add(AnalysisResult(id=uuid.uuid4(), file_id=f.id, fine_windows_analyzed=1, fine_windows_total=10))
    await session.commit()

    # Confirm the premise: the sort key really is degenerate, so this test is exercising the tie.
    created = (await session.execute(select(FileRecord.created_at))).scalars().all()
    assert len(set(created)) == 1, "premise failed: created_at is not tied, so this would not exercise the tiebreaker"

    seen = await _walk_all_pages(lambda p: get_analyze_working_set(session, page=p, page_size=MIN_PAGE_SIZE), MIN_PAGE_SIZE)

    expected = {str(f.id) for f in files}
    assert len(seen) == len(set(seen)), (
        f"a row was DUPLICATED across pages -- the ORDER BY lost its unique tiebreaker ({len(seen) - len(set(seen))} dupes)"
    )
    assert set(seen) == expected, f"a row was SKIPPED across pages -- {len(expected - set(seen))} missing of {len(expected)}"


@pytest.mark.asyncio
async def test_analyze_files_page_is_stable_when_the_sort_key_ties(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same degenerate-sort-key walk over the filtered/paged lens (``get_analyze_files_page``)."""
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _backend_settings())

    files = [_make_pipeline_file() for _ in range(25)]
    session.add_all(files)
    await session.flush()
    for f in files:
        session.add(AnalysisResult(id=uuid.uuid4(), file_id=f.id, fine_windows_analyzed=1, fine_windows_total=10))
    await session.commit()

    seen = await _walk_all_pages(
        lambda p: get_analyze_files_page(session, page=p, page_size=MIN_PAGE_SIZE, status="in_flight"),
        MIN_PAGE_SIZE,
    )

    expected = {str(f.id) for f in files}
    assert len(seen) == len(set(seen)), "a row was DUPLICATED across pages of the filtered lens"
    assert set(seen) == expected, "a row was SKIPPED across pages of the filtered lens"


@pytest.mark.asyncio
async def test_pending_files_paging_is_stable_when_the_sort_key_ties(session: AsyncSession) -> None:
    """The same degenerate-sort-key walk over the shared metadata/fingerprint pending pager."""
    files = [_make_pipeline_file() for _ in range(25)]
    session.add_all(files)
    await session.commit()

    seen = await _walk_all_pages(
        lambda p: get_pending_files_page(session, Stage.METADATA, page=p, page_size=MIN_PAGE_SIZE),
        MIN_PAGE_SIZE,
    )

    expected = {str(f.id) for f in files}
    assert len(seen) == len(set(seen)), "a row was DUPLICATED across pages of the pending pager"
    assert set(seen) == expected, "a row was SKIPPED across pages of the pending pager"


@pytest.mark.asyncio
async def test_out_of_range_page_yields_an_empty_page_not_an_error(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract rule 5: a page past the end is a normal EMPTY page, never an exception or a 500."""
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _backend_settings())

    page = await get_analyze_working_set(session, page=9999, page_size=DEFAULT_PAGE_SIZE)
    assert page.rows == []
    assert page.has_next is False

    pending = await get_pending_files_page(session, Stage.METADATA, page=9999)
    assert pending.rows == []
    assert pending.has_next is False
