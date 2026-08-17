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
from phaze.routers.response_shape import DUAL_SHAPE_RESPONSE_HEADERS


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

    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    # Only the failed-metadata row is present; the done + not-started rows are filtered out.
    assert "failedmeta-" in body
    assert "donemeta-" not in body
    assert "plainfile-" not in body


@pytest.mark.asyncio
async def test_failed_cell_pill_carries_the_oob_targetable_id(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-bgz26: the failed cell's pill wrapper carries the SAME id the per-file retry ack OOB-pushes.

    The Files matrix (files_table_view.html) has no self-poll -- the per-row Retry button's ack
    must be able to address this exact cell to clear a stale red pill without a full page reload
    (see tests/analyze/test_retry_affordances.py / tests/metadata/test_retry_affordances.py for the
    ack side). If this wrapper id ever drifts from ``_stage_pill_oob``'s ``files-stage-pill-*``
    namespace, the OOB swap silently does nothing -- htmx targets an id that doesn't exist -- and
    this is the ONLY test that would catch that drift from the render side.
    """
    failed = _make_file("pilloob")
    session.add(failed)
    await session.commit()
    session.add(FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="boom"))
    await session.commit()

    resp = await client.get("/pipeline/files", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    assert f'id="files-stage-pill-metadata-{failed.id}"' in body
    # The per-row Retry button targets the SAME file_id/stage this pill wrapper carries.
    assert f"/pipeline/files/{failed.id}/metadata-failed/retry" in body


@pytest.mark.asyncio
async def test_failed_filter_empty_renders_failed_filter_copy(client: AsyncClient, session: AsyncSession) -> None:
    """A failed filter that matches nothing renders the failed-filter empty-state copy (Copywriting Contract).

    Only a metadata failure is seeded, so filtering ``stage=analyze&bucket=failed`` matches zero rows --
    the empty branch must show "No failed files in Analyze" / "Nothing is stuck in Analyze right now.",
    NOT the unfiltered "No files yet" copy.
    """
    failed = _make_file("failedmeta")
    session.add(failed)
    await session.commit()
    session.add(FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="boom"))
    await session.commit()

    resp = await client.get("/pipeline/files?stage=analyze&bucket=failed", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    assert "No failed files in Analyze" in body
    assert "Nothing is stuck in Analyze right now." in body
    # The unfiltered empty copy must NOT be what renders under an active failed filter.
    assert "No files yet" not in body


@pytest.mark.asyncio
async def test_filter_state_is_url_carried(client: AsyncClient, session: AsyncSession) -> None:
    """The filter bar pushes canonical /s/files state that survives back/forward and reload."""
    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    # The status filter bar is present and pushes filter state into the URL (D-03).
    assert 'id="status-filter-bar"' in body
    assert 'hx-get="/s/files"' in body
    assert 'hx-push-url="true"' in body
    # The active filter axes are reflected as selected options (survives the record slide-in re-render).
    assert '<option value="metadata" selected>' in body
    assert '<option value="failed" selected>' in body


@pytest.mark.asyncio
async def test_pipeline_files_plain_request_redirects_to_shell_preserving_query(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (phaze-uvmcr.2): a plain (non-htmx) GET of the pushed URL redirects to /s/files.

    The filter form and Clear-filter anchor both ``hx-push-url="true"`` the bare
    ``/pipeline/files`` endpoint (D-03's URL-carried-lens idiom). ``"files"`` is a registered shell
    stage reachable with the rail intact at ``/s/files`` (``routers/shell.py`` ``STAGE_PARTIALS``),
    so an F5 reload or a bookmark of the pushed URL no longer renders a second, rail-less full-page
    fork (``pipeline/files.html``, retired by this bead) -- it redirects there instead, carrying the
    query string across so the redirected URL still reads as the one that was bookmarked.
    """
    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/s/files?stage=metadata&bucket=failed"


@pytest.mark.asyncio
async def test_pipeline_files_history_restore_also_redirects_to_shell(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (phaze-uvmcr.2): a history-restore GET also redirects, not just a plain request.

    On a history-cache miss (routine -- htmx's historyCacheSize is 10) htmx re-fetches the pushed
    URL with BOTH ``HX-Request`` and ``HX-History-Restore-Request`` set, ignores hx-target, and
    swaps the response into ``<body>`` (response_shape.py rule 2) -- so it falls on the SAME
    non-fragment branch as a plain request, per ``wants_fragment``, and must redirect too rather
    than regressing to the header-only check phaze-64uy already fixed twice elsewhere.
    """
    resp = await client.get(
        "/pipeline/files?stage=metadata&bucket=failed",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/s/files?stage=metadata&bucket=failed"


@pytest.mark.asyncio
async def test_pipeline_files_plain_request_redirect_lands_on_a_full_shell_document(client: AsyncClient, session: AsyncSession) -> None:
    """The redirect target is a real, navigable page -- following it lands on the full shell, not a fragment."""
    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed", follow_redirects=True)
    assert resp.status_code == 200
    body = resp.text
    assert "<html" in body.lower(), "the redirect target must be a full document, not a fragment"
    assert "<h1" in body, "the page heading must be present"
    assert 'id="files-table-view"' in body, "the swap target itself must be present on the redirected page"


@pytest.mark.asyncio
async def test_pipeline_files_redirect_and_fragment_both_mark_the_response_uncacheable(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-r6e5m (response_shape.py contract rule 6) -- ``/pipeline/files`` forks on request
    headers alone into a 302 redirect OR a fragment, so the browser's URL-only HTTP cache could
    otherwise replay one shape's cached bytes for a request that wanted the other. Both branches
    must carry DUAL_SHAPE_RESPONSE_HEADERS.
    """
    redirect = await client.get("/pipeline/files?stage=metadata&bucket=failed", follow_redirects=False)
    assert redirect.status_code == 302
    for name, value in DUAL_SHAPE_RESPONSE_HEADERS.items():
        assert redirect.headers.get(name) == value, f"redirect branch missing/wrong {name!r}"

    fragment = await client.get("/pipeline/files?stage=metadata&bucket=failed", headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    for name, value in DUAL_SHAPE_RESPONSE_HEADERS.items():
        assert fragment.headers.get(name) == value, f"fragment branch missing/wrong {name!r}"


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
    first interaction. The fix splits host (files_workspace.html, the ONLY host since phaze-uvmcr.2
    retired the pipeline/files.html full-page fork -- a bookmark of GET /pipeline/files now
    redirects there) from fragment (this template, now id-less) -- assert directly against the two
    surfaces that must still carry exactly one id each.
    """
    await make_file()

    full_page = await client.get("/pipeline/files", follow_redirects=True)
    assert full_page.text.count('id="files-table-view"') == 1, "the redirected (bookmark) page must host exactly one #files-table-view"

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

    resp = await client.get("/pipeline/files?page=2&page_size=10", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    # The empty-state copy renders (no rows on this over-paged request)...
    assert "No files yet" in body
    # ...but the pager nav -- specifically an ENABLED Previous control back to page 1 -- must too.
    assert 'aria-label="Files pagination"' in body
    # HTML-attribute-escaped (Jinja autoescape turns `&` into `&amp;` inside the hx-get value).
    assert 'hx-get="/s/files?page=1&amp;page_size=10' in body
    assert 'hx-push-url="true"' in body
    assert ">Previous</button>" in body, "Previous must be an enabled <button>, not the disabled <span>"


@pytest.mark.asyncio
async def test_pager_absent_on_unfiltered_empty_first_page(client: AsyncClient, session: AsyncSession) -> None:
    """No pager renders on a genuinely empty, unpaged corpus (page 1, no next page) -- unchanged behavior."""
    resp = await client.get("/pipeline/files", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text

    assert "No files yet" in body
    assert 'aria-label="Files pagination"' not in body


@pytest.mark.asyncio
async def test_pipeline_files_plain_request_redirect_hosts_record_target(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (phaze-7pjc / phaze-2och), updated for phaze-uvmcr.2: following the redirect a
    reload/bookmark of a filtered /pipeline/files URL now issues must land on a page hosting a
    #record-body target so every row's hx-target="#record-body" resolves.

    files_table_view.html's rows dispatch `record:open` and hx-get into #record-body -- an id
    that only shell/partials/record_host.html declares. Before phaze-uvmcr.2, a direct navigation,
    bookmark, reload, or history-restore of a filtered /pipeline/files URL (phaze-2och's failure
    path, reached via the filter bar's hx-push-url) rendered the now-retired pipeline/files.html,
    which needed its OWN copy of record_host.html because base.html carried none. The redirect
    target, shell.html, already includes record_host.html for every stage (unconditionally, not
    just "files") -- this proves the round trip through the new redirect still lands somewhere the
    row's click/Enter resolves, not that /s/files needed new wiring for it.
    """
    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed", follow_redirects=True)
    assert resp.status_code == 200
    body = resp.text

    assert 'id="record-body"' in body, "the record slide-in swap target must exist on the redirected page"
    assert "record:open.window" in body, "the record:open listener the rows dispatch to must be wired"
    assert "d.target.id === 'record-body'" in body, "the 404 opt-in for GET /record/{id} must be present"


@pytest.mark.asyncio
async def test_pipeline_files_history_restore_redirect_hosts_record_target(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (phaze-2och), updated for phaze-uvmcr.2: a history-restore of a filtered URL
    also redirects (same non-fragment branch, per wants_fragment) and still lands hosting
    #record-body.
    """
    resp = await client.get(
        "/pipeline/files?stage=metadata&bucket=failed",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.text

    assert 'id="record-body"' in body, "history-restore must also land on a page with the record host"


@pytest.mark.asyncio
async def test_reload_after_status_filter_push_url_leaves_rows_clickable(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """Regression (phaze-2och): the operator round-trip the bead describes, end to end.

    On /s/files, `_status_filter_bar.html` hx-push-urls `/pipeline/files?stage=...&bucket=...`
    (D-03's URL-carried lens). A plain reload/bookmark/history-restore of THAT exact pushed URL
    used to serve pipeline/files.html with no #record-body host, so every row's hx-get="/record/
    {id}" hx-target="#record-body" bound to a target that did not exist on the page -- a silent
    htmx:targetError no-op. Since phaze-uvmcr.2 that URL redirects to /s/files instead of rendering
    a second copy of the page. Assert the row's own hx-get and the page's own hx-target both
    survive the round trip: the target the row names must actually be present in the same document.
    """
    file_record = await make_file()

    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed", follow_redirects=True)
    assert resp.status_code == 200
    body = resp.text

    # The row's hx-get is unconditional on any filter match, so a plain unfiltered fetch would
    # normally carry it too -- refetch without the filter to get this seeded file's own row.
    resp = await client.get("/pipeline/files", follow_redirects=True)
    body = resp.text
    assert f'hx-get="/record/{file_record.id}"' in body, "the seeded file's row must still hx-get its record"
    assert 'hx-target="#record-body"' in body, "the row's target must be #record-body, unchanged"
    assert 'id="record-body"' in body, "and #record-body itself must exist in the SAME document the row is in"


@pytest.mark.asyncio
async def test_orphaned_lens_lists_exactly_the_recovery_candidates(client: AsyncClient, session: AsyncSession) -> None:
    """``?stage=analyze&bucket=orphaned`` lists a scheduled-then-lost file and nothing else (phaze-cavai).

    The lens is ``stage_status_case(stage)=='in_flight' AND orphaned_clause(stage)`` — the per-file
    twin of recovery's work set. Seeded: an orphan (ledger row, no live broker key, no analysis), a
    genuinely-running file (ledger row + a live ``saq_jobs`` key), and a bare file. Only the orphan
    may appear. ``saq_jobs`` is SAQ-owned and absent from ``Base.metadata``, so the test pins a
    module-controlled minimal table the same way tests/integration/test_orphan_count.py does.
    """
    from sqlalchemy import text

    from phaze.models.scheduling_ledger import SchedulingLedger

    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.execute(text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"))
    orphan = _make_file("orphanstrand")
    running = _make_file("stillrunning")
    plain = _make_file("bareplain")
    session.add_all([orphan, running, plain])
    await session.commit()
    session.add(SchedulingLedger(key=f"process_file:{orphan.id}", function="process_file", routing="agent", payload={"file_id": str(orphan.id)}))
    session.add(SchedulingLedger(key=f"process_file:{running.id}", function="process_file", routing="agent", payload={"file_id": str(running.id)}))
    await session.commit()
    await session.execute(text("INSERT INTO saq_jobs (key, status) VALUES (:k, 'queued')"), {"k": f"process_file:{running.id}"})
    await session.commit()

    resp = await client.get("/pipeline/files?stage=analyze&bucket=orphaned", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    assert "orphanstrand-" in body
    assert "stillrunning-" not in body
    assert "bareplain-" not in body


@pytest.mark.asyncio
async def test_orphaned_lens_on_a_non_enrich_stage_returns_the_empty_set(client: AsyncClient, session: AsyncSession) -> None:
    """``?stage=propose&bucket=orphaned`` is not a defined concept — empty set, never a 500 (phaze-cavai)."""
    stray = _make_file("strayrow")
    session.add(stray)
    await session.commit()

    resp = await client.get("/pipeline/files?stage=propose&bucket=orphaned", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "strayrow-" not in resp.text
