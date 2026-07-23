"""UI-02 / D-03: the failure/status filter lens over the single paginated files table (87-05).

The filter is "just another lens" on the ONE canonical ``GET /pipeline/files`` list -- there is no
separate failures page. These tests pin, against a real operator ``client``:

* ``?stage=metadata&bucket=failed`` returns ONLY files whose derived metadata bucket is ``failed`` --
  a done-metadata file and a plain (not-started) file are both excluded (UI-02 failure visibility);
* when a failed filter matches nothing, the failed-filter empty-state copy renders (Copywriting
  Contract: "No failed files in {stage}" / "Nothing is stuck in {stage} right now.");
* the filter bar carries URL state -- the swap target hx-gets ``/pipeline/files`` with ``hx-push-url``.

Uses the plain operator ``client`` + ``session`` fixtures (tests/conftest.py). The whole
``tests/integration/`` package is auto-marked ``integration``. The route derives each row's buckets
via the correlated ``stage_status_case`` columns (Plan 04), so the seed markers below (a metadata
``failed_at`` row vs. a metadata payload row) drive the buckets under test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _make_file(marker: str) -> FileRecord:
    """A FileRecord whose current_path carries a distinctive ``marker`` so we can assert row presence."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{marker}-{uid.hex}.mp3",
        original_filename=f"{marker}-{uid.hex}.mp3",
        current_path=f"/music/{marker}-{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )


@pytest.mark.asyncio
async def test_metadata_failed_filter_returns_only_failed_rows(client: AsyncClient, session: AsyncSession) -> None:
    """``?stage=metadata&bucket=failed`` renders ONLY the failed-metadata row (UI-02).

    A metadata ``failed_at`` row derives bucket ``failed``; a metadata payload row derives ``done``;
    a bare file derives ``not_started``. Only the first must appear in the filtered table.
    """
    failed = _make_file("failedmeta")
    done = _make_file("donemeta")
    plain = _make_file("plainfile")
    session.add_all([failed, done, plain])
    await session.commit()
    # failed metadata: failed_at set, payload NULL -> done(metadata) derives FAILED.
    session.add(FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="boom"))
    # done metadata: real payload, failed_at NULL -> derives DONE.
    session.add(FileMetadata(file_id=done.id, artist="Real", title="Track"))
    await session.commit()

    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed")
    assert resp.status_code == 200
    body = resp.text

    # Only the failed-metadata row is present; the done + not-started rows are filtered out.
    assert "failedmeta-" in body
    assert "donemeta-" not in body
    assert "plainfile-" not in body


@pytest.mark.asyncio
async def test_failed_filter_empty_renders_failed_filter_copy(client: AsyncClient, session: AsyncSession) -> None:
    """A failed filter that matches nothing renders the failed-filter empty-state copy (Copywriting Contract).

    Only a metadata failure is seeded, so filtering ``stage=fingerprint&bucket=failed`` matches zero rows --
    the empty branch must show "No failed files in Fingerprint" / "Nothing is stuck in Fingerprint right now.",
    NOT the unfiltered "No files yet" copy.
    """
    failed = _make_file("failedmeta")
    session.add(failed)
    await session.commit()
    session.add(FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="boom"))
    await session.commit()

    resp = await client.get("/pipeline/files?stage=fingerprint&bucket=failed")
    assert resp.status_code == 200
    body = resp.text

    assert "No failed files in Fingerprint" in body
    assert "Nothing is stuck in Fingerprint right now." in body
    # The unfiltered empty copy must NOT be what renders under an active failed filter.
    assert "No files yet" not in body


@pytest.mark.asyncio
async def test_filter_state_is_url_carried(client: AsyncClient, session: AsyncSession) -> None:
    """The filter bar carries URL state: it hx-gets /pipeline/files with hx-push-url (survives back/forward)."""
    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed")
    assert resp.status_code == 200
    body = resp.text

    # The status filter bar is present and pushes filter state into the URL (D-03).
    assert 'id="status-filter-bar"' in body
    assert 'hx-get="/pipeline/files"' in body
    assert 'hx-push-url="true"' in body
    # The active filter axes are reflected as selected options (survives the record slide-in re-render).
    assert '<option value="metadata" selected>' in body
    assert '<option value="failed" selected>' in body


@pytest.mark.asyncio
async def test_pipeline_files_plain_request_returns_full_page(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (phaze-p7ox): a plain (non-htmx) GET of the pushed URL returns the FULL page.

    The filter form and Clear-filter anchor both ``hx-push-url="true"`` the bare
    ``/pipeline/files`` endpoint (D-03's URL-carried-lens idiom). Before the fix, this handler
    unconditionally returned the chrome-less ``files_table_view.html`` fragment -- no
    ``<html>``, no CSS, no htmx, no Alpine -- so an F5 reload or a bookmark of a filtered view
    rendered a broken, unstyled page.
    """
    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed")
    assert resp.status_code == 200
    body = resp.text
    assert "<html" in body.lower(), "a plain request must return a full document, not a fragment"
    assert "<h1" in body, "the page heading must be present"
    assert 'id="files-table-view"' in body, "the swap target itself must be present in the full page"
    # The filter selection still round-trips through the full-page render.
    assert '<option value="metadata" selected>' in body
    assert '<option value="failed" selected>' in body


@pytest.mark.asyncio
async def test_pipeline_files_history_restore_returns_full_page(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (phaze-p7ox): a history-restore GET returns the FULL page, chrome included.

    On a history-cache miss (routine -- htmx's historyCacheSize is 10) htmx re-fetches the pushed
    URL with BOTH ``HX-Request`` and ``HX-History-Restore-Request`` set, ignores hx-target, and
    swaps the response into ``<body>`` (response_shape.py rule 2). A fragment here replaces the
    whole page with an orphaned filter bar + table and no way out but a manual reload.
    """
    resp = await client.get(
        "/pipeline/files?stage=metadata&bucket=failed",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<html" in body.lower(), "a history restore must return a full document, not a fragment"
    assert "<h1" in body, "the <h1> page heading must survive a history restore"
    assert 'id="files-table-view"' in body, "the swap target itself must be present in the full page"


@pytest.mark.asyncio
async def test_pipeline_files_live_htmx_swap_still_returns_the_fragment(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (phaze-p7ox): an ordinary htmx swap (no restore header) still gets the
    chrome-less fragment -- the fix must not turn every live filter/pagination swap into a full page.
    """
    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    assert "<html" not in body.lower(), "a live htmx swap must get a fragment, not a full document"
    # phaze-mrhq: the fragment itself must NOT carry id="files-table-view" -- that id belongs to the
    # HOST (files_workspace.html / pipeline/files.html) it is swapped innerHTML into. Before the fix
    # this fragment's own root div carried the same id, so every swap nested a duplicate
    # #files-table-view inside the host it was replacing (see test_files_fragment_carries_no_duplicate_id).
    assert 'id="files-table-view"' not in body, "the swap-target fragment must not re-carry the host's id (would nest a duplicate)"


@pytest.mark.asyncio
async def test_files_fragment_carries_no_duplicate_id(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """Regression (phaze-mrhq): the live filter/sort/pager fragment must not self-nest #files-table-view.

    files_table_view.html used to be BOTH the host div AND the swap-target fragment: it carried
    ``id="files-table-view"`` itself, and GET /pipeline/files (a live htmx swap) returned this same
    template. Every filter/sort/Prev/Next click therefore swapped a fresh id-bearing div innerHTML
    into the existing one, leaving ``#files-table-view > #files-table-view`` in the DOM after the
    first interaction. The fix splits host (files_workspace.html for /s/files, an inline wrapper in
    pipeline/files.html for the bookmark path) from fragment (this template, now id-less) -- assert
    directly against the two surfaces that must still carry exactly one id each.
    """
    await make_file()

    full_page = await client.get("/pipeline/files")
    assert full_page.text.count('id="files-table-view"') == 1, "the full (bookmark) page must host exactly one #files-table-view"

    workspace = await client.get("/s/files")
    assert workspace.text.count('id="files-table-view"') == 1, "the /s/files workspace must host exactly one #files-table-view"

    live_fragment = await client.get("/pipeline/files", headers={"HX-Request": "true"})
    assert 'id="files-table-view"' not in live_fragment.text, "the live filter/sort/pager fragment must carry no id of its own"


@pytest.mark.asyncio
async def test_over_paged_empty_view_still_shows_previous_control(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (phaze-3db8): an over-paged empty render must still carry the Previous control.

    The pagination ``<nav>`` used to sit inside the rows-present branch, so a page N > 1 that
    returns zero rows (``get_files_page``/``clamp_page``'s documented "a page PAST the end is not
    clamped -- it simply yields an empty page" contract) rendered only the empty-state copy with no
    way back to page 1 short of a manual reload. One file with page_size=MIN_PAGE_SIZE (10) makes
    page 2 genuinely past the end while still being reachable via the normal Prev/Next flow.
    """
    session.add(_make_file("onlyfile"))
    await session.commit()

    resp = await client.get("/pipeline/files?page=2&page_size=10")
    assert resp.status_code == 200
    body = resp.text

    # The empty-state copy renders (no rows on this over-paged request)...
    assert "No files yet" in body
    # ...but the pager nav -- specifically an ENABLED Previous control back to page 1 -- must too.
    assert 'aria-label="Files pagination"' in body
    # HTML-attribute-escaped (Jinja autoescape turns `&` into `&amp;` inside the hx-get value).
    assert 'hx-get="/pipeline/files?page=1&amp;page_size=10' in body
    assert ">Previous</button>" in body, "Previous must be an enabled <button>, not the disabled <span>"


@pytest.mark.asyncio
async def test_pager_absent_on_unfiltered_empty_first_page(client: AsyncClient, session: AsyncSession) -> None:
    """No pager renders on a genuinely empty, unpaged corpus (page 1, no next page) -- unchanged behavior."""
    resp = await client.get("/pipeline/files")
    assert resp.status_code == 200
    body = resp.text

    assert "No files yet" in body
    assert 'aria-label="Files pagination"' not in body
