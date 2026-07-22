"""Integration tests for tracklists router."""

from datetime import date, datetime
from pathlib import Path
import re
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.schemas.agent_tasks import ScanLiveSetPayload
from phaze.services.tracklist_scraper import ScrapedTracklist, TracklistSearchResult
from tests._queue_fakes import install_fake_queues, seed_active_agent


def _make_file(original_path: str = "/music/test.mp3", file_type: str = "mp3") -> FileRecord:
    """Create a test FileRecord."""
    filename = original_path.rsplit("/", 1)[-1]
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash="a" * 64,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type=file_type,
        file_size=1000,
    )


def _make_executed_proposal(file_id: uuid.UUID) -> RenameProposal:
    """Seed an ``executed`` RenameProposal so ``applied()`` (READ-05/D-01) admits the file.

    The cue-version guards now read ``await is_applied(session, fr.id)`` (an executed proposal),
    NOT a scalar ``fr.state``. Fixtures that expect a CUE badge must carry an executed
    proposal; the file is left at ``state='moved'`` so the badge proves the guard reads the proposal.
    """
    return RenameProposal(
        id=uuid.uuid4(),
        file_id=file_id,
        proposed_filename="applied.mp3",
        status=ProposalStatus.EXECUTED,
    )


def _make_tracklist(
    file_id: uuid.UUID | None = None,
    external_id: str | None = None,
    match_confidence: int | None = None,
    auto_linked: bool = False,
    source: str = "1001tracklists",
    status: str = "approved",
) -> Tracklist:
    """Create a test Tracklist."""
    return Tracklist(
        id=uuid.uuid4(),
        external_id=external_id or f"tl-{uuid.uuid4().hex[:8]}",
        source_url=f"https://www.1001tracklists.com/tracklist/{uuid.uuid4().hex[:8]}/test.html",
        file_id=file_id,
        match_confidence=match_confidence,
        auto_linked=auto_linked,
        artist="Test Artist",
        event="Test Festival",
        date=date(2024, 4, 14),
        source=source,
        status=status,
    )


@pytest.mark.asyncio
async def test_list_tracklists_returns_html(session: AsyncSession, client: AsyncClient) -> None:
    """Phase 57 (SHELL-05): a plain GET /tracklists/ 302-redirects into the shell.

    The "Tracklists" page heading + stats header are full-page chrome that move to the
    tracklist workspace node (a Phase-57 placeholder; real content lands in 58-61). The
    in-page HX list partial stays usable (covered by the with-data / filter tests below).
    """
    response = await client.get("/tracklists/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tracklist"


@pytest.mark.asyncio
async def test_list_tracklists_empty_state(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ with no data shows empty state message."""
    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "hasn't run yet" in response.text.lower() or "hasn&#x27;t run yet" in response.text.lower()


@pytest.mark.asyncio
async def test_list_tracklists_with_data(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ with tracklists shows card content."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=85)
    session.add(tl)
    await session.flush()

    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Test Artist" in response.text
    assert "Test Festival" in response.text


@pytest.mark.asyncio
async def test_list_tracklists_filter_matched(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/?filter=matched returns only matched tracklists."""
    file = _make_file()
    session.add(file)
    await session.flush()

    matched = _make_tracklist(file_id=file.id, match_confidence=90, external_id="matched-1")
    unmatched = _make_tracklist(external_id="unmatched-1")
    session.add_all([matched, unmatched])
    await session.flush()

    response = await client.get("/tracklists/?filter=matched", headers={"HX-Request": "true"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_tracklists_filter_unmatched(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/?filter=unmatched returns only unmatched tracklists."""
    response = await client.get("/tracklists/?filter=unmatched", headers={"HX-Request": "true"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_tracklists_htmx_returns_partial(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ with HX-Request header returns partial (no html tag)."""
    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text


@pytest.mark.asyncio
async def test_get_tracks(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/{id}/tracks returns track detail partial."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version = TracklistVersion(
        id=uuid.uuid4(),
        tracklist_id=tl.id,
        version_number=1,
    )
    session.add(version)
    await session.flush()

    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Track Artist",
        title="Track Title",
        label="Test Label",
        timestamp="00:00",
        is_mashup=False,
    )
    session.add(track)
    await session.flush()

    response = await client.get(f"/tracklists/{tl.id}/tracks")
    assert response.status_code == 200
    assert "Track Artist" in response.text
    assert "Track Title" in response.text


@pytest.mark.asyncio
async def test_unlink_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/unlink removes file linkage."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=90)
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/unlink")
    assert response.status_code == 200

    await session.refresh(tl)
    assert tl.file_id is None
    assert tl.match_confidence is None


@pytest.mark.asyncio
async def test_undo_link(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/undo-link removes auto-link."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=95, auto_linked=True)
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/undo-link")
    assert response.status_code == 200

    await session.refresh(tl)
    assert tl.file_id is None
    assert tl.auto_linked is False


@pytest.mark.asyncio
async def test_navigation_contains_tracklists_link(session: AsyncSession, client: AsyncClient) -> None:
    """Phase 57 (SHELL-03/05): the legacy top-nav Tracklists link is replaced by the DAG rail.

    Plan 57-03 retired the base.html tab-bar (the rail node ``hx-get="/s/tracklist"`` is the
    new nav affordance), so a plain GET /tracklists/ 302-redirects into the shell rather than
    rendering a nav bar with ``href="/tracklists/"``.
    """
    response = await client.get("/tracklists/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tracklist"


@pytest.mark.asyncio
async def test_stats_header_values(session: AsyncSession, client: AsyncClient) -> None:
    """Phase 57 (SHELL-05): the tracklists stats header moved to the shell workspace node.

    The "Total Tracklists"/"Matched"/"Unmatched" stats header is full-page chrome rendered
    by the tracklist workspace node (a Phase-57 placeholder; real content lands in 58-61),
    so a plain GET /tracklists/ now 302-redirects into the shell.
    """
    response = await client.get("/tracklists/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tracklist"


@pytest.mark.asyncio
async def test_scan_tab(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan returns scan tab HTML with file list."""
    file1 = _make_file(original_path="/music/live-set-1.mp3", file_type="mp3")
    file2 = _make_file(original_path="/music/live-set-2.m4a", file_type="m4a")
    session.add_all([file1, file2])
    await session.flush()

    response = await client.get("/tracklists/scan")
    assert response.status_code == 200
    assert "Scan Live Sets" in response.text
    assert "live-set-1.mp3" in response.text
    assert "live-set-2.m4a" in response.text


@pytest.mark.asyncio
async def test_scan_tab_excludes_already_scanned(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan excludes files that already have fingerprint tracklists."""
    file1 = _make_file(original_path="/music/already-done.mp3", file_type="mp3")
    file2 = _make_file(original_path="/music/fresh-file.mp3", file_type="mp3")
    session.add_all([file1, file2])
    await session.flush()

    # Link file1 to a fingerprint-sourced tracklist
    tl = _make_tracklist(file_id=file1.id, external_id="fp-scanned", source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    response = await client.get("/tracklists/scan")
    assert response.status_code == 200
    assert "fresh-file.mp3" in response.text
    assert "already-done.mp3" not in response.text


@pytest.mark.asyncio
async def test_scan_tab_empty_state(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan with no unscanned files shows empty state."""
    response = await client.get("/tracklists/scan")
    assert response.status_code == 200
    assert "No unscanned files" in response.text


def _scan_panel_file_ids(body: str) -> list[str]:
    """Extract every ``file_ids`` checkbox ``value`` (a file uuid) IN RENDER ORDER.

    ``scan_tab.html`` renders one ``<input name="file_ids" value="{{ file.id }}">`` per row, so this
    is the one place the per-file identity survives on a page where several rows can share the exact
    SAME ``original_filename`` (the whole point of the tiebreaker regression below -- filename text
    alone cannot distinguish two tied rows, but the checkbox value can).
    """
    return re.findall(r'name="file_ids"\s+value="([0-9a-fA-F-]{36})"', body)


@pytest.mark.asyncio
async def test_scan_tab_pagination_stable_across_a_filename_tie_boundary(session: AsyncSession, client: AsyncClient) -> None:
    """A large tied ``original_filename`` block spanning several page boundaries is never skipped or repeated.

    ``original_filename`` carries no uniqueness constraint. Seeds 19 files with distinct,
    alphabetically-earlier filenames (deterministically filling the first 19 slots of page 1),
    followed by 80 files that all share the exact SAME ``original_filename`` (an explicit tie,
    never a clock/insertion race) sorting after all 19 -- a tie block spanning page 1 through
    page 5 (``page_size`` is 20; 99 rows total).

    Without a unique tiebreaker, ``ORDER BY filename LIMIT 20 OFFSET N`` for each page is a
    SEPARATE query, and Postgres's executor picks a different internal sort bound per page
    (page 1 only needs the top 20 of 99 rows; page 5 needs the top 99 of 99) -- these differently
    bounded partial sorts are NOT required to break a 80-way tie the same way, so a tied file can
    land on two pages while another tied file lands on none. This is the CLASSIC unstable-
    pagination failure mode, empirically confirmed against this exact data shape (verified below
    to reproduce reliably, not a one-in-a-million flake): reverting this test's tiebreaker (see
    the sibling raw-SQL probe used to size this dataset) drops one tied row from the page sweep
    while duplicating another, every run.

    Regression guard for phaze-rgxg: the assertions below -- that every page returns file ids
    disjoint from every other page, and that the union across ALL pages is exactly the seeded set
    -- are verified to FAIL when the ``, FileRecord.id`` tiebreaker in
    ``routers/tracklists.py``'s ``scan_tab`` is reverted.
    """
    page_size = 20
    tie_filename = "tied-live-set.mp3"
    distinct_files = [_make_file(original_path=f"/music/aaa-{i:02d}.mp3", file_type="mp3") for i in range(19)]
    tied_files = [_make_file(original_path=f"/music/irrelevant-path-{i}.mp3", file_type="mp3") for i in range(80)]
    for f in tied_files:
        f.original_filename = tie_filename  # identical sort key, explicit -- not a path/insertion accident
    all_files = distinct_files + tied_files
    session.add_all(all_files)
    await session.flush()
    all_ids = {str(f.id) for f in all_files}
    assert len(all_ids) == 99
    total_pages = -(-len(all_files) // page_size)  # ceil division -- 5 pages of 20

    seen_per_page: list[set[str]] = []
    for page in range(1, total_pages + 1):
        response = await client.get(f"/tracklists/scan?page={page}")
        assert response.status_code == 200
        seen_per_page.append(set(_scan_panel_file_ids(response.text)))

    # No id may appear on more than one page -- a repeat proves the tiebreaker is missing.
    for i, page_i in enumerate(seen_per_page):
        for j, page_j in enumerate(seen_per_page):
            if i != j:
                assert page_i.isdisjoint(page_j), f"a tied file appeared on both page {i + 1} and page {j + 1}"

    # The union across every page must be exactly the seeded set -- a gap proves a tied file was
    # skipped between pages.
    union_ids: set[str] = set()
    for page_ids in seen_per_page:
        union_ids |= page_ids
    assert union_ids == all_ids, "the union of all pages must be exactly the seeded file set"
    assert sum(len(p) for p in seen_per_page) == len(all_ids), "a tied file was skipped or duplicated across the page sweep"


def _tracklist_card_ids(body: str) -> list[str]:
    """Extract every ``id="tracklist-{uuid}"`` card wrapper IN RENDER ORDER (tracklist_card.html)."""
    return re.findall(r'id="tracklist-([0-9a-fA-F-]{36})"', body)


def _seed_tied_tracklists(session: AsyncSession, *, n_distinct: int, n_tied: int, tied_at: object) -> set[uuid.UUID]:
    """Seed ``n_distinct`` uniquely-ranked tracklists followed by an ``n_tied``-way tie block.

    Distinct rows carry strictly descending ``match_confidence`` (200 down), all ABOVE the tied
    block's confidence (50), so they deterministically fill the front of the order. The tied block
    shares one EXPLICIT ``match_confidence`` AND one EXPLICIT ``created_at`` (a tie on the FULL
    compound sort key, never a clock/insertion race) -- the exact shape ``list_tracklists`` /
    ``_render_tracklist_list`` order by. Returns every seeded id.
    """
    rows: list[Tracklist] = []
    for i in range(n_distinct):
        t = Tracklist(id=uuid.uuid4(), external_id=f"filler-{uuid.uuid4().hex[:8]}", source_url="https://x/filler", match_confidence=200 - i)
        t.created_at = tied_at  # type: ignore[assignment]
        rows.append(t)
    for _ in range(n_tied):
        t = Tracklist(id=uuid.uuid4(), external_id=f"tied-{uuid.uuid4().hex[:8]}", source_url="https://x/tied", match_confidence=50)
        t.created_at = tied_at  # type: ignore[assignment]
        rows.append(t)
    session.add_all(rows)
    return {t.id for t in rows}


@pytest.mark.asyncio
async def test_list_tracklists_pagination_stable_across_a_confidence_and_created_at_tie_boundary(session: AsyncSession, client: AsyncClient) -> None:
    """A large (match_confidence, created_at) tie block spanning several pages is never skipped or repeated.

    Drives ``GET /tracklists/`` (``list_tracklists``) directly. Both ``match_confidence`` and
    ``created_at`` are non-unique, so two tracklists can tie on the FULL compound sort key; without
    a unique tiebreaker, ``ORDER BY match_confidence DESC NULLS LAST, created_at DESC LIMIT 20
    OFFSET N`` for each page is a separate query that Postgres is free to plan (and break the tie)
    differently per page -- the same unstable-pagination failure mode as ``scan_tab`` above,
    empirically confirmed to reproduce reliably at this scale (19 distinct + 80 tied = 99 rows, 5
    pages of 20): reverting the ``Tracklist.id`` tiebreaker drops one tied row from the page sweep
    while duplicating another, every run.

    Regression guard for phaze-rgxg: the assertions below -- every page's ids disjoint from every
    other page, and the union across ALL pages exactly the seeded set -- are verified to FAIL when
    the ``, Tracklist.id.desc()`` tiebreaker in ``routers/tracklists.py``'s ``list_tracklists`` is
    reverted.
    """
    page_size = 20
    tied_at = datetime(2026, 7, 20, 12, 0, 0)  # naive on purpose (created_at is TIMESTAMP WITHOUT TZ)
    all_ids = {str(i) for i in _seed_tied_tracklists(session, n_distinct=19, n_tied=80, tied_at=tied_at)}
    await session.commit()
    assert len(all_ids) == 99
    total_pages = -(-len(all_ids) // page_size)  # ceil division -- 5 pages of 20

    seen_per_page: list[set[str]] = []
    for page in range(1, total_pages + 1):
        response = await client.get(f"/tracklists/?filter=all&page={page}", headers={"HX-Request": "true"})
        assert response.status_code == 200
        seen_per_page.append(set(_tracklist_card_ids(response.text)))

    for i, page_i in enumerate(seen_per_page):
        for j, page_j in enumerate(seen_per_page):
            if i != j:
                assert page_i.isdisjoint(page_j), f"a tied tracklist appeared on both page {i + 1} and page {j + 1}"

    union_ids: set[str] = set()
    for page_ids in seen_per_page:
        union_ids |= page_ids
    assert union_ids == all_ids, "the union of all pages must be exactly the seeded tracklist set"
    assert sum(len(p) for p in seen_per_page) == len(all_ids), "a tied tracklist was skipped or duplicated across the page sweep"


@pytest.mark.asyncio
async def test_render_tracklist_list_page_one_matches_direct_list_tracklists_page_one(session: AsyncSession, client: AsyncClient) -> None:
    """``_render_tracklist_list`` (the POST-mutation re-render helper) renders the SAME page 1 as ``list_tracklists``.

    Every real caller of ``_render_tracklist_list`` (link/unlink/rescrape/undo-link) renders page 1
    with the SAME ``ORDER BY match_confidence DESC NULLS LAST, created_at DESC`` as ``list_tracklists``
    -- it is a second call SITE for the identical statement, not a second query shape, so this test
    drives it through a REAL POST endpoint (``POST /tracklists/{id}/unlink`` with a nonexistent
    ``tracklist_id`` -- a safe no-op mutation that still unconditionally re-renders the list) rather
    than duplicating the full multi-page sweep above.

    Regression guard for phaze-rgxg: with the tied dataset from the sibling test above, this
    asserts the ``unlink`` response's page-1 card ids are EXACTLY the ``list_tracklists`` GET's
    page-1 card ids -- reverting the ``, Tracklist.id.desc()`` tiebreaker on EITHER call site makes
    this comparison flaky (both sides independently susceptible to a different tie-break per call),
    so it is verified to fail when the tiebreaker in ``_render_tracklist_list`` is reverted.
    """
    tied_at = datetime(2026, 7, 20, 12, 0, 0)
    _seed_tied_tracklists(session, n_distinct=19, n_tied=80, tied_at=tied_at)
    await session.commit()

    get_response = await client.get("/tracklists/?filter=all&page=1", headers={"HX-Request": "true"})
    assert get_response.status_code == 200
    get_page1_ids = _tracklist_card_ids(get_response.text)
    assert len(get_page1_ids) == 20

    post_response = await client.post(f"/tracklists/{uuid.uuid4()}/unlink")
    assert post_response.status_code == 200
    post_page1_ids = _tracklist_card_ids(post_response.text)

    assert post_page1_ids == get_page1_ids, "_render_tracklist_list's page 1 must match list_tracklists' page 1 exactly, in order"


# ---------------------------------------------------------------------------
# GET /tracklists/scan response shape (phaze-xc84)
#
# The handler owes a DOCUMENT SHAPE decision, and routers/response_shape.py owns it: rule 1 bans
# branching on the raw ``HX-Request`` header, rule 2 says a history restore is a full-document
# request even though it carries that header. Before the fix the handler made no decision at all --
# it returned the bare scan_tab fragment unconditionally, so a plain browser navigation got a
# chrome-less page with no base.html and no nav.
#
# The same predicate drives ``is_hx``, which scan_tab.html gates its ``#scan-panel`` wrapper on.
# Getting it backwards yields either ZERO ``#scan-panel`` (htmx swap finds no target) or TWO
# (duplicate id -- the class already on record in phaze-gzrd / op6f / 7j50 / 5p43), so every case
# below asserts the exact count, never merely "present".
# ---------------------------------------------------------------------------


def _count_scan_panel(body: str) -> int:
    """Count ``id="scan-panel"`` occurrences -- the invariant is EXACTLY one on every shape."""
    return body.count('id="scan-panel"')


@pytest.mark.asyncio
async def test_scan_tab_plain_navigation_returns_full_document(session: AsyncSession, client: AsyncClient) -> None:
    """A plain browser GET /tracklists/scan returns a FULL document, chrome included (phaze-xc84).

    Before the fix this returned the bare partial: no ``<html>``, no nav, and -- because base.html
    never loaded -- no htmx, so the 'Scan Selected Files' button fell back to a native form submit
    and silently enqueued nothing.

    Asserts the CHROME, not merely a 200; the buggy handler returned 200 too.
    """
    file1 = _make_file(original_path="/music/plain-nav.mp3", file_type="mp3")
    session.add(file1)
    await session.flush()

    response = await client.get("/tracklists/scan")
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a plain navigation must return a full document, not a fragment"
    assert 'aria-label="Main navigation"' in body, "the app nav must be present on a plain navigation"
    assert "plain-nav.mp3" in body, "the scan content itself must still render inside the full page"
    assert _count_scan_panel(body) == 1, "the full page must carry exactly one #scan-panel wrapper"


@pytest.mark.asyncio
async def test_scan_tab_htmx_swap_returns_fragment(session: AsyncSession, client: AsyncClient) -> None:
    """A live htmx swap gets the chrome-less, wrapper-less fragment (phaze-xc84).

    Guards the other direction: the fix must not turn every htmx request into a full page. The
    fragment is swapped INTO the existing ``#scan-panel``, so it must not carry its own -- a second
    one would duplicate the id in the live document.
    """
    file1 = _make_file(original_path="/music/hx-swap.mp3", file_type="mp3")
    session.add(file1)
    await session.flush()

    response = await client.get("/tracklists/scan", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower(), "a live htmx swap must get a fragment, not a full document"
    assert "hx-swap.mp3" in body
    assert _count_scan_panel(body) == 0, "the fragment swaps INTO #scan-panel and must not carry a second one"


@pytest.mark.asyncio
async def test_scan_tab_history_restore_returns_full_document(session: AsyncSession, client: AsyncClient) -> None:
    """A history-restore GET returns the FULL document (phaze-xc84, response_shape rule 2).

    The scan pagination buttons hx-get ``/tracklists/scan?page=N``. On a history-cache miss htmx
    re-fetches with BOTH ``HX-Request`` and ``HX-History-Restore-Request`` set, IGNORES hx-target,
    and swaps the response into ``<body>``. A fragment here replaces the whole page -- and, because
    the old template suppressed its wrapper whenever ``HX-Request`` was set, the resulting document
    contained ZERO ``#scan-panel``, so every later swap on that page had no target at all.

    This is the case a raw ``HX-Request`` check gets wrong, which is why the handler composes
    ``wants_fragment`` instead.
    """
    file1 = _make_file(original_path="/music/restore.mp3", file_type="mp3")
    session.add(file1)
    await session.flush()

    response = await client.get(
        "/tracklists/scan",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a history restore must return a full document, not a fragment"
    assert 'aria-label="Main navigation"' in body, "the app nav must survive a history restore"
    assert _count_scan_panel(body) == 1, "a restored document must carry exactly one #scan-panel to swap into"


@pytest.mark.asyncio
async def test_scan_tab_restore_header_alone_returns_full_document(session: AsyncSession, client: AsyncClient) -> None:
    """The restore header DOMINATES even without ``HX-Request`` (response_shape rule 2).

    ``wants_fragment`` is deliberately ``is_htmx_request and not is_history_restore``; this pins the
    fourth shape the contract enumerates so a future refactor cannot quietly drop the ``not``.
    """
    response = await client.get("/tracklists/scan", headers={"HX-History-Restore-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower()
    assert _count_scan_panel(body) == 1


def test_scan_progress_cta_has_no_dead_alpine_refs() -> None:
    """The scan-completion CTA references no nonexistent Alpine vars (phaze-xc84 item b).

    ``@click="showScan = false; activeTab = 'proposed'"`` named two variables that do not exist in
    the v7 shell, so the handler was a silent no-op; it also hx-targeted ``#tracklists-list``, which
    is absent from the scan surface this fragment renders into. Both are gone -- the plain href
    navigates for real.
    """
    source = (Path(__file__).parents[3] / "src/phaze/templates/tracklists/partials/scan_progress.html").read_text()
    # Strip Jinja comments first: the markup is what ships to the browser, and the {# #} block
    # above the CTA necessarily NAMES the dead identifiers in order to explain their removal.
    template = re.sub(r"\{#.*?#\}", "", source, flags=re.DOTALL)
    assert "showScan" not in template, "showScan does not exist in the v7 shell -- the @click was a no-op"
    assert "activeTab" not in template, "activeTab does not exist in the v7 shell -- the @click was a no-op"
    assert 'hx-target="#tracklists-list"' not in template, "the CTA renders inside #scan-panel, where #tracklists-list is absent"
    assert 'href="/tracklists/?filter=proposed&page=1"' in template, "the CTA must still navigate to the proposed list"


@pytest.mark.asyncio
async def test_trigger_scan(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/scan enqueues a full ScanLiveSetPayload onto the agent queue."""
    await seed_active_agent(session, "nox")
    _controller, task_router = install_fake_queues(client)

    file = _make_file()
    session.add(file)
    await session.flush()

    response = await client.post("/tracklists/scan", data={"file_ids": [str(file.id)]})
    assert response.status_code == 200
    assert "Scanning..." in response.text

    # scan_live_set captured on the per-agent queue, never the controller.
    agent_queue = task_router.queues["nox-meta"]
    assert agent_queue.name == "phaze-agent-nox-meta"
    assert len(agent_queue.captured) == 1
    task_name, payload = agent_queue.captured[0]
    assert task_name == "scan_live_set"
    assert payload["file_id"] == str(file.id)
    assert payload["original_path"] == file.original_path
    assert payload["agent_id"] == "nox"
    # The enqueued payload must validate against the strict ScanLiveSetPayload so the
    # worker no longer dead-letters it (the v4.0.8 payload-incident class).
    assert ScanLiveSetPayload.model_validate(payload)

    # The progress partial's poll URL carries agent_id so the status poll targets
    # the same per-agent queue.
    assert "agent_id=nox" in response.text


@pytest.mark.asyncio
async def test_trigger_scan_skips_file_id_without_record(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/scan skips a file_id with no FileRecord -- nothing enqueued, no dead-letter."""
    await seed_active_agent(session, "nox")
    _controller, task_router = install_fake_queues(client)

    missing_id = str(uuid.uuid4())
    response = await client.post("/tracklists/scan", data={"file_ids": [missing_id]})
    assert response.status_code == 200

    # No FileRecord for the submitted id -> nothing enqueued for it.
    assert task_router.queues["nox-meta"].captured == []


@pytest.mark.asyncio
async def test_trigger_scan_skips_malformed_file_id(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/scan skips a non-UUID file_id -- no 500, nothing enqueued."""
    await seed_active_agent(session, "nox")
    _controller, task_router = install_fake_queues(client)

    response = await client.post("/tracklists/scan", data={"file_ids": ["not-a-uuid"]})
    assert response.status_code == 200

    # A malformed id is dropped before the DB query, never enqueued, never a 500.
    assert task_router.queues["nox-meta"].captured == []


@pytest.mark.asyncio
async def test_trigger_scan_no_active_agent(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/scan with zero active agents enqueues nothing, shows empty-state."""
    # Only the conftest legacy agent exists (last_seen_at is None -> excluded).
    _controller, task_router = install_fake_queues(client)

    file_id = str(uuid.uuid4())
    response = await client.post("/tracklists/scan", data={"file_ids": [file_id]})
    assert response.status_code == 200
    assert "No active agent" in response.text
    # Nothing enqueued anywhere.
    assert task_router.queues == {}
    assert task_router.queue_for_calls == []


@pytest.mark.asyncio
async def test_proposed_filter(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/?filter=proposed returns only proposed tracklists."""
    file = _make_file()
    session.add(file)
    await session.flush()

    proposed = _make_tracklist(file_id=file.id, external_id="fp-proposed", source="fingerprint", status="proposed")
    approved = _make_tracklist(external_id="tl-approved", source="1001tracklists", status="approved")
    session.add_all([proposed, approved])
    await session.flush()

    response = await client.get("/tracklists/?filter=proposed", headers={"HX-Request": "true"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_inline_edit_get(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/tracks/{id}/edit/{field} returns input HTML."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Original Artist",
        title="Original Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    response = await client.get(f"/tracklists/tracks/{track.id}/edit/artist")
    assert response.status_code == 200
    assert 'name="artist"' in response.text
    assert "Original Artist" in response.text


@pytest.mark.asyncio
async def test_inline_edit_save(session: AsyncSession, client: AsyncClient) -> None:
    """PUT /tracklists/tracks/{id}/edit/{field} updates field and returns display HTML."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Old Artist",
        title="Old Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    response = await client.put(f"/tracklists/tracks/{track.id}/edit/artist", data={"artist": "New Artist"})
    assert response.status_code == 200
    assert "New Artist" in response.text
    assert "hx-get" in response.text

    await session.refresh(track)
    assert track.artist == "New Artist"


@pytest.mark.asyncio
async def test_inline_edit_invalid_field(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/tracks/{id}/edit/{field} returns 400 for invalid field name."""
    track_id = uuid.uuid4()
    response = await client.get(f"/tracklists/tracks/{track_id}/edit/invalid_field")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_delete_track(session: AsyncSession, client: AsyncClient) -> None:
    """DELETE /tracklists/tracks/{id} removes track row."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Delete Me",
        title="Delete Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    # Regression (phaze-dyvt/phaze-5fc2): a matched track carries DiscogsLink children whose FK has no
    # ON DELETE. Before the fix, deleting the track raised IntegrityError -> unhandled 500. The endpoint
    # must clear the referencing links first.
    dl = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="999",
        discogs_artist="A",
        discogs_title="T",
        confidence=0.9,
        status="candidate",
    )
    session.add(dl)
    await session.flush()

    response = await client.delete(f"/tracklists/tracks/{track.id}")
    assert response.status_code == 200
    assert response.text == ""

    remaining_links = await session.execute(select(func.count(DiscogsLink.id)).where(DiscogsLink.track_id == track.id))
    assert remaining_links.scalar_one() == 0, "the matched track's DiscogsLink children are removed with it"


@pytest.mark.asyncio
async def test_approve_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/approve changes status to approved."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/approve")
    assert response.status_code == 200

    await session.refresh(tl)
    assert tl.status == "approved"


@pytest.mark.asyncio
async def test_reject_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/reject changes status to rejected."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/reject")
    assert response.status_code == 200

    await session.refresh(tl)
    assert tl.status == "rejected"


@pytest.mark.asyncio
async def test_bulk_reject_low_confidence(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/reject-low removes tracks below threshold."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    high_conf = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Good",
        title="Good Track",
        confidence=95.0,
    )
    low_conf = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=2,
        artist="Bad",
        title="Bad Track",
        confidence=30.0,
    )
    session.add_all([high_conf, low_conf])
    await session.flush()

    # Regression (phaze-5fc2): a matched low-confidence track carries a DiscogsLink child (FK has no
    # ON DELETE, Core bulk delete fires no cascade). Before the fix ONE such row raised IntegrityError,
    # rolling back the single-statement bulk delete and rendering reject-low wholly inoperable on any
    # matched tracklist. Match Discogs creates candidate links confidence-indifferently, so a low-conf
    # track can absolutely carry one.
    dl = DiscogsLink(
        id=uuid.uuid4(),
        track_id=low_conf.id,
        discogs_release_id="777",
        discogs_artist="Bad Discogs",
        discogs_title="Bad Discogs Title",
        confidence=0.3,
        status="candidate",
    )
    session.add(dl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/reject-low?threshold=50")
    assert response.status_code == 200
    assert "Good" in response.text
    assert "Bad" not in response.text

    # The low-confidence track AND its referencing link are both gone; the high-confidence track survives.
    remaining_links = await session.execute(select(func.count(DiscogsLink.id)).where(DiscogsLink.track_id == low_conf.id))
    assert remaining_links.scalar_one() == 0, "referencing DiscogsLink rows are cleared before the bulk track delete"
    remaining_tracks = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == version.id))
    assert remaining_tracks.scalar_one() == 1, "only the high-confidence track survives"


@pytest.mark.asyncio
async def test_fingerprint_tracks_use_fingerprint_template(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/{id}/tracks returns fingerprint template for fingerprint source."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="FP Artist",
        title="FP Title",
        confidence=88.0,
    )
    session.add(track)
    await session.flush()

    response = await client.get(f"/tracklists/{tl.id}/tracks")
    assert response.status_code == 200
    # Fingerprint template includes confidence badges and inline edit
    assert "FP Artist" in response.text
    assert "hx-get" in response.text  # inline edit wiring
    assert "hx-delete" in response.text  # delete button


@pytest.mark.asyncio
async def test_stats_include_proposed(session: AsyncSession, client: AsyncClient) -> None:
    """Stats dict includes proposed count."""
    file = _make_file()
    session.add(file)
    await session.flush()

    proposed = _make_tracklist(file_id=file.id, external_id="fp-stats", source="fingerprint", status="proposed")
    approved = _make_tracklist(external_id="tl-stats-appr", source="1001tracklists", status="approved")
    session.add_all([proposed, approved])
    await session.flush()

    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Proposed" in response.text


# --- Scan status / progress ---


@pytest.mark.asyncio
async def test_scan_status_all_complete(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status polls the per-agent queue and reports completion."""
    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.COMPLETE
    mock_job.result = {"status": "scanned", "filename": "test.mp3"}

    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("nox", "meta").job = AsyncMock(return_value=mock_job)

    response = await client.get("/tracklists/scan/status?job_ids=job-1&agent_id=nox")
    assert response.status_code == 200
    # The poll resolved the per-agent queue via task_router.queue_for("nox").
    assert "nox" in task_router.queue_for_calls
    assert "Scan complete" in response.text


@pytest.mark.asyncio
async def test_scan_status_with_error(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status reports errors from failed jobs on the per-agent queue.

    Mirrors REAL SAQ semantics (regression for the crashed-scan-reported-as-clean bug):
    a FAILED job's ``result`` is always ``None`` -- SAQ only populates ``result`` from the
    task's return value on COMPLETE (``saq/job.py``) -- and the failure detail lives in
    ``job.error`` (the traceback string). The ``result={"status": "error", ...}`` shape this
    test used to fake is never produced by ``scan_live_set`` (it only ever returns
    ``no_matches``/``scanned``; failures raise and become ``Status.FAILED``), so faking it
    would let the pre-fix ``isinstance(job.result, dict)`` branch pass without exercising the
    real bug. Pre-fix, this test fails: the dead ``result_data.get("status") == "error"``
    branch is unreachable against ``result=None``, so the job silently counts as a clean
    completion ("Scan complete. No matching tracks found...") instead of surfacing the crash.
    """
    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.FAILED
    mock_job.result = None
    mock_job.error = "RuntimeError: fingerprint sidecar unreachable\nTraceback (most recent call last): ..."
    mock_job.kwargs = {"file_id": str(uuid.uuid4()), "original_path": "/music/bad.mp3", "agent_id": "nox"}

    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("nox", "meta").job = AsyncMock(return_value=mock_job)

    response = await client.get("/tracklists/scan/status?job_ids=job-1&agent_id=nox")
    assert response.status_code == 200
    # The crash is surfaced explicitly -- NOT folded into a clean "Scan complete" message.
    assert "/music/bad.mp3" in response.text
    assert "Scan failed" in response.text
    assert "No matching tracks found" not in response.text


@pytest.mark.asyncio
async def test_scan_status_failed_job_falls_back_to_job_error(session: AsyncSession, client: AsyncClient) -> None:
    """A FAILED job with no ``original_path`` in kwargs falls back to ``job.error``'s first line."""
    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.FAILED
    mock_job.result = None
    mock_job.error = "ConnectionError: fingerprint service refused connection\nTraceback: ..."
    mock_job.kwargs = {}

    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("nox", "meta").job = AsyncMock(return_value=mock_job)

    response = await client.get("/tracklists/scan/status?job_ids=job-1&agent_id=nox")
    assert response.status_code == 200
    assert "ConnectionError: fingerprint service refused connection" in response.text


@pytest.mark.asyncio
async def test_scan_status_mixed_complete_and_failed(session: AsyncSession, client: AsyncClient) -> None:
    """A batch with one COMPLETE (scanned) job and one FAILED job reports both: a
    created tracklist AND a surfaced crash, with neither masking the other."""
    from saq import Status

    complete_job = MagicMock()
    complete_job.status = Status.COMPLETE
    complete_job.result = {"status": "scanned", "file_id": "abc"}

    failed_job = MagicMock()
    failed_job.status = Status.FAILED
    failed_job.result = None
    failed_job.error = "RuntimeError: fingerprint sidecar unreachable"
    failed_job.kwargs = {"original_path": "/music/crashed.mp3"}

    _controller, task_router = install_fake_queues(client)
    fake_queue = task_router.queue_for("nox", "meta")
    fake_queue.job = AsyncMock(side_effect=[complete_job, failed_job])

    response = await client.get("/tracklists/scan/status?job_ids=job-1,job-2&agent_id=nox")
    assert response.status_code == 200
    assert "tracklist(s) created" in response.text
    assert "/music/crashed.mp3" in response.text
    assert "Scan failed" in response.text


@pytest.mark.asyncio
async def test_scan_status_job_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status handles a missing job gracefully (counts complete)."""
    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("nox", "meta").job = AsyncMock(return_value=None)

    response = await client.get("/tracklists/scan/status?job_ids=missing-job&agent_id=nox")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_scan_status_job_pending(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/scan/status handles pending jobs (no result yet)."""
    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.QUEUED
    mock_job.result = None

    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("nox", "meta").job = AsyncMock(return_value=mock_job)

    response = await client.get("/tracklists/scan/status?job_ids=pending-job&agent_id=nox")
    assert response.status_code == 200
    # Job still queued -> not done -> poll partial keeps emitting agent_id.
    assert "agent_id=nox" in response.text


# --- HARD-03 (AR-30-03 / Phase-30 REVIEW IN-01): agent_id boundary validation ---
# A malformed agent_id must 422 at the HTTP boundary instead of a silently-empty
# 200 poll. Pattern + max_length mirror the Agent.id DB CHECK (models/agent.py:36)
# and the CLI AGENT_ID_RE (cli/__init__.py:44).


@pytest.mark.asyncio
async def test_scan_status_malformed_agent_id_returns_422(session: AsyncSession, client: AsyncClient) -> None:
    """HARD-03: a malformed agent_id -> 422 (was a silent empty 200 poll)."""
    response = await client.get("/tracklists/scan/status?job_ids=job-1&agent_id=Bad_ID!")
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_scan_status_well_formed_agent_id_passes_validation(session: AsyncSession, client: AsyncClient) -> None:
    """HARD-03: a well-formed agent_id still reaches the handler (not a 422)."""
    from saq import Status

    mock_job = MagicMock()
    mock_job.status = Status.COMPLETE
    mock_job.result = {"status": "scanned", "filename": "test.mp3"}

    _controller, task_router = install_fake_queues(client)
    task_router.queue_for("test-agent-01", "meta").job = AsyncMock(return_value=mock_job)

    response = await client.get("/tracklists/scan/status?job_ids=job-1&agent_id=test-agent-01")
    assert response.status_code != 422
    assert response.status_code == 200, response.text


# --- Link / rescrape / search endpoints ---


@pytest.mark.asyncio
async def test_link_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/link sets file_id and confidence."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    response = await client.post(
        f"/tracklists/{tl.id}/link",
        data={"file_id": str(file.id), "confidence": 85},
    )
    assert response.status_code == 200

    await session.refresh(tl)
    assert tl.file_id == file.id
    assert tl.match_confidence == 85


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [0, 100])
async def test_link_tracklist_accepts_the_domain_boundary(session: AsyncSession, client: AsyncClient, confidence: int) -> None:
    """phaze-k5ac: 0 and 100 are exactly the 0-100 match-score domain boundary -- must be ACCEPTED."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    response = await client.post(
        f"/tracklists/{tl.id}/link",
        data={"file_id": str(file.id), "confidence": confidence},
    )
    assert response.status_code == 200, response.text

    await session.refresh(tl)
    assert tl.match_confidence == confidence


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [-1, 101, 3000000000])
async def test_link_tracklist_rejects_confidence_outside_the_domain(session: AsyncSession, client: AsyncClient, confidence: int) -> None:
    """phaze-k5ac: confidence outside 0-100 -- including an int32-overflowing value -- is a 422, not a 500.

    3000000000 (> int32 max 2147483647) previously reached `tracklist.match_confidence = confidence`
    and raised Postgres NumericValueOutOfRange, unhandled, on commit. Asserting `match_confidence` is
    left untouched proves the rejection happens before the assignment/commit ever runs.
    """
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    response = await client.post(
        f"/tracklists/{tl.id}/link",
        data={"file_id": str(file.id), "confidence": confidence},
    )
    assert response.status_code == 422, response.text

    await session.refresh(tl)
    assert tl.match_confidence is None, "a rejected (422) link POST must not mutate match_confidence"
    assert tl.file_id is None, "a rejected (422) link POST must not mutate file_id either"


@pytest.mark.asyncio
async def test_link_tracklist_stale_file_id_returns_clean_error_not_500(session: AsyncSession, client: AsyncClient) -> None:
    """phaze-29bv: a well-formed but non-existent file_id (stale panel / deleted FileRecord) is a
    clean 4xx, not an unhandled 500 from a ForeignKeyViolation at commit.

    The FK write on Tracklist.file_id was previously unvalidated, so a dead-but-well-formed UUID
    reached session.commit() and raised asyncpg ForeignKeyViolationError, poisoning the request
    transaction. The handler must resolve the FileRecord first and branch cleanly.
    """
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    dead_file_id = uuid.uuid4()  # well-formed UUID with no FileRecord row
    response = await client.post(
        f"/tracklists/{tl.id}/link",
        data={"file_id": str(dead_file_id), "confidence": 85},
    )
    assert response.status_code == 404, response.text

    # The link never landed: no poisoned commit, tracklist left unlinked.
    await session.refresh(tl)
    assert tl.file_id is None
    assert tl.match_confidence is None


@pytest.mark.asyncio
async def test_link_search_result_stale_file_id_skips_scrape_and_returns_clean_error(session: AsyncSession, client: AsyncClient) -> None:
    """phaze-x4vi: a dead file_id is rejected BEFORE the expensive scrape, so no scraped work is
    discarded and the commit never FK-violates into a 500.

    Previously the handler scraped (up to 30s), stored the tracklist in the same uncommitted
    transaction, then set the unvalidated file_id and committed -- a dead file_id raised
    ForeignKeyViolation AND rolled back the scrape, so every retry re-hit the rate-limited site.
    """
    dead_file_id = uuid.uuid4()  # well-formed UUID with no FileRecord row
    url = "https://www.1001tracklists.com/tracklist/better1/x.html"

    with patch("phaze.routers.tracklists.TracklistScraper") as mock_scraper_cls:
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.scrape_tracklist = AsyncMock()
        mock_scraper.close = AsyncMock()

        response = await client.post(
            "/tracklists/link-result",
            data={"file_id": str(dead_file_id), "external_id": "better1", "url": url},
        )

    assert response.status_code == 404, response.text
    # The scrape is skipped entirely -- no wasted network round trip, nothing to discard.
    mock_scraper.scrape_tracklist.assert_not_awaited()
    # No tracklist row was created for the rejected request.
    result = await session.execute(select(Tracklist).where(Tracklist.external_id == "better1"))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_link_search_result_releases_connection_before_scrape(session: AsyncSession, client: AsyncClient) -> None:
    """phaze-gkow: the pooled DB connection is released BEFORE scrape_tracklist()'s rate-limit sleep + HTTP.

    Pre-gkow the handler held the injected session's open transaction across scrape_tracklist()
    (~2-35s: a 2-5s rate-limit sleep then a 30s-timeout POST), pinning one PgBouncer SESSION-mode
    pooled connection idle-in-transaction and draining the capped pool. The handler must commit the
    read transaction (releasing the connection) BEFORE the scrape, so we spy that a commit is recorded
    strictly BEFORE scrape_tracklist runs -- while the scraped tracklist still lands linked.
    """
    file = _make_file(original_path="/music/Real Artist - Live @ Coachella 2024.04.14.mp3")
    session.add(file)
    await session.flush()

    scraped = ScrapedTracklist(
        external_id="better1",
        title="Real Artist @ Coachella",
        artist="Real Artist",
        event="Coachella",
        date="2024-04-14",
        tracks=[],
        source_url="https://www.1001tracklists.com/tracklist/better1/x.html",
    )

    order: list[str] = []
    real_commit = AsyncSession.commit

    async def _spy_commit(self: AsyncSession) -> None:
        await real_commit(self)
        order.append("commit")

    async def _scrape_recording(_url: str) -> ScrapedTracklist:
        order.append("scrape")
        return scraped

    with (
        patch("phaze.routers.tracklists.TracklistScraper") as mock_scraper_cls,
        patch.object(AsyncSession, "commit", _spy_commit),
    ):
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.scrape_tracklist = _scrape_recording
        mock_scraper.close = AsyncMock()

        response = await client.post(
            "/tracklists/link-result",
            data={"file_id": str(file.id), "external_id": "better1", "url": scraped.source_url},
        )

    assert response.status_code == 200, response.text
    # A commit (releasing the read txn / pooled connection) precedes the network scrape.
    assert "scrape" in order
    assert order.index("commit") < order.index("scrape")
    # The scrape still lands, stored and linked to the file.
    result = await session.execute(select(Tracklist).where(Tracklist.external_id == "better1"))
    selected = result.scalar_one_or_none()
    assert selected is not None
    assert selected.file_id == file.id


@pytest.mark.asyncio
async def test_link_search_result_stale_file_during_scrape_keeps_scraped_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """phaze-gkow: a file deleted DURING the scrape yields a clean 404 without discarding the scrape.

    With the connection released across the scrape, the file can vanish between the up-front validation
    and the linkage write. The scrape is stored+committed FIRST, so the stale-file 404 leaves the freshly
    scraped tracklist persisted (unlinked) -- never discarded -- and never FK-violates into a 500.
    """
    file = _make_file(original_path="/music/Real Artist - Live @ Coachella 2024.04.14.mp3")
    session.add(file)
    await session.flush()
    file_id = file.id

    scraped = ScrapedTracklist(
        external_id="better1",
        title="Real Artist @ Coachella",
        artist="Real Artist",
        event="Coachella",
        date="2024-04-14",
        tracks=[],
        source_url="https://www.1001tracklists.com/tracklist/better1/x.html",
    )

    async def _scrape_then_delete_file(_url: str) -> ScrapedTracklist:
        # Simulate a concurrent scan deleting the file while the (connection-free) scrape runs.
        await session.delete(await session.get(FileRecord, file_id))
        await session.commit()
        return scraped

    with patch("phaze.routers.tracklists.TracklistScraper") as mock_scraper_cls:
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.scrape_tracklist = _scrape_then_delete_file
        mock_scraper.close = AsyncMock()

        response = await client.post(
            "/tracklists/link-result",
            data={"file_id": str(file_id), "external_id": "better1", "url": scraped.source_url},
        )

    assert response.status_code == 404, response.text
    # The scraped tracklist is persisted (unlinked), never discarded.
    result = await session.execute(select(Tracklist).where(Tracklist.external_id == "better1"))
    stored = result.scalar_one_or_none()
    assert stored is not None
    assert stored.file_id is None


@pytest.mark.asyncio
async def test_rescrape_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/rescrape enqueues scrape job."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    controller_queue, task_router = install_fake_queues(client)

    response = await client.post(f"/tracklists/{tl.id}/rescrape")
    assert response.status_code == 200
    # Controller task lands on the controller queue, never a per-agent queue.
    assert controller_queue.captured == [("scrape_and_store_tracklist", {"tracklist_id": str(tl.id)})]
    assert task_router.queues == {}


@pytest.mark.asyncio
async def test_rescrape_tracklist_has_candidates_in_context(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/rescrape includes has_candidates when candidates exist."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    # Create a version and track
    version = TracklistVersion(
        id=uuid.uuid4(),
        tracklist_id=tl.id,
        version_number=1,
    )
    session.add(version)
    await session.flush()

    tl.latest_version_id = version.id
    await session.flush()

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Track Artist",
        title="Track Title",
    )
    session.add(track)
    await session.flush()

    # Create a candidate DiscogsLink for the track
    dl = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="12345",
        discogs_artist="Discogs Artist",
        discogs_title="Discogs Title",
        confidence=0.85,
        status="candidate",
    )
    session.add(dl)
    await session.flush()

    install_fake_queues(client)

    response = await client.post(f"/tracklists/{tl.id}/rescrape")
    assert response.status_code == 200
    # The bulk-link button text should appear when has_candidates is True
    assert "Bulk" in response.text


@pytest.mark.asyncio
async def test_manual_search(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/search enqueues a search job onto the controller queue."""
    controller_queue, task_router = install_fake_queues(client)

    file_id = uuid.uuid4()
    response = await client.post(f"/tracklists/search?file_id={file_id}")
    assert response.status_code == 200
    assert controller_queue.captured == [("search_tracklist", {"file_id": str(file_id)})]
    assert task_router.queues == {}


@pytest.mark.asyncio
async def test_search_better_match_no_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/{id}/search with non-existent tracklist returns empty results."""
    fake_id = uuid.uuid4()
    response = await client.get(f"/tracklists/{fake_id}/search")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_search_better_match_scores_against_file_not_tracklist(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-ldal): score results against the FILE's context, not the tracklist's own
    artist. Before the fix, compute_match_confidence was called with file_artist=tracklist.artist,
    which collapses to a fuzzy artist self-comparison and produces a near-100 score for ANY
    same-artist-as-the-tracklist result, regardless of whether it actually matches the file.
    """
    file = _make_file(original_path="/music/Real Artist - Live @ Coachella 2024.04.14.mp3")
    session.add(file)
    await session.flush()

    # The already-linked tracklist has a DIFFERENT artist than the file -- if the scorer still
    # compares results to tl.artist (the bug), the result below would score low, not 100.
    tl = _make_tracklist(file_id=file.id, match_confidence=40)
    tl.artist = "Totally Different Artist"
    session.add(tl)
    await session.flush()

    search_result = TracklistSearchResult(
        external_id="better1",
        title="Real Artist @ Coachella",
        url="https://www.1001tracklists.com/tracklist/better1/x.html",
        artist="Real Artist",
        date="2024-04-14",
    )

    with patch("phaze.routers.tracklists.TracklistScraper") as mock_scraper_cls:
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.search = AsyncMock(return_value=[search_result])
        mock_scraper.close = AsyncMock()

        response = await client.get(f"/tracklists/{tl.id}/search")

    assert response.status_code == 200
    # Real Artist == Real Artist (the file's own parsed artist) -> exact match, 100%.
    assert "100%" in response.text
    # The selected result's own identity travels through the form so link-result can
    # resolve/scrape ITS content -- not the path tracklist_id.
    assert 'name="external_id" value="better1"' in response.text
    assert f'name="url" value="{search_result.url}"' in response.text


@pytest.mark.asyncio
async def test_search_better_match_releases_connection_before_scrape(session: AsyncSession, client: AsyncClient) -> None:
    """phaze-ctrl: the pooled DB connection is released BEFORE scraper.search()'s rate-limit sleep + HTTP.

    Pre-ctrl the handler held the injected session (an open implicit read transaction, pinning one
    PgBouncer SESSION-mode upstream connection) across scraper.search() -- ~2-35s of pure network I/O.
    A handful of concurrent searches (or a slow/unreachable upstream) drained the capped pool and 500'd
    /health + normal page loads. The handler must commit the read (releasing the connection) BEFORE the
    scrape, so we spy that a session commit is recorded strictly BEFORE search() runs.
    """
    file = _make_file(original_path="/music/Real Artist - Live @ Coachella 2024.04.14.mp3")
    session.add(file)
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=40)
    tl.artist = "Real Artist"
    tl.event = "Coachella"
    session.add(tl)
    await session.flush()

    order: list[str] = []
    real_commit = AsyncSession.commit

    async def _spy_commit(self: AsyncSession) -> None:
        await real_commit(self)
        order.append("commit")

    async def _search_recording(_query: str) -> list[TracklistSearchResult]:
        order.append("search")
        return []

    with (
        patch("phaze.routers.tracklists.TracklistScraper") as mock_scraper_cls,
        patch.object(AsyncSession, "commit", _spy_commit),
    ):
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.search = _search_recording
        mock_scraper.close = AsyncMock()

        response = await client.get(f"/tracklists/{tl.id}/search")

    assert response.status_code == 200
    # A commit (releasing the read txn / pooled connection) precedes the network scrape.
    assert "search" in order
    assert order.index("commit") < order.index("search")


@pytest.mark.asyncio
async def test_link_search_result_links_selected_not_original(session: AsyncSession, client: AsyncClient) -> None:
    """Regression (phaze-ldal): linking a search RESULT must persist and link THAT result's own
    content -- not silently re-link the original tracklist (searched from) and stamp the
    selected result's confidence onto it.
    """
    file = _make_file(original_path="/music/Real Artist - Live @ Coachella 2024.04.14.mp3")
    session.add(file)
    await session.flush()

    original = _make_tracklist(file_id=file.id, match_confidence=40, external_id="original-tl")
    original.artist = "Totally Different Artist"
    session.add(original)
    await session.flush()

    scraped = ScrapedTracklist(
        external_id="better1",
        title="Real Artist @ Coachella",
        artist="Real Artist",
        event="Coachella",
        date="2024-04-14",
        tracks=[],
        source_url="https://www.1001tracklists.com/tracklist/better1/x.html",
    )

    with patch("phaze.routers.tracklists.TracklistScraper") as mock_scraper_cls:
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.scrape_tracklist = AsyncMock(return_value=scraped)
        mock_scraper.close = AsyncMock()

        response = await client.post(
            "/tracklists/link-result",
            data={"file_id": str(file.id), "external_id": "better1", "url": scraped.source_url},
        )

    assert response.status_code == 200
    mock_scraper.scrape_tracklist.assert_awaited_once_with(scraped.source_url)

    # The ORIGINAL tracklist is untouched: same file link, same (low) confidence it had before.
    await session.refresh(original)
    assert original.file_id == file.id
    assert original.match_confidence == 40

    # The SELECTED result is persisted as its own row, linked to the file with an honest score
    # computed against the file's actual context (not a tracklist-vs-itself comparison).
    result = await session.execute(select(Tracklist).where(Tracklist.external_id == "better1"))
    selected = result.scalar_one_or_none()
    assert selected is not None
    assert selected.file_id == file.id
    assert selected.match_confidence == 100
    assert selected.artist == "Real Artist"


@pytest.mark.asyncio
async def test_link_search_result_reuses_existing_tracklist_row(session: AsyncSession, client: AsyncClient) -> None:
    """Linking a result whose external_id already exists (e.g. previously scraped) resolves the
    existing row instead of re-scraping -- and still leaves the original tracklist alone.
    """
    file = _make_file(original_path="/music/Real Artist - Live @ Coachella 2024.04.14.mp3")
    session.add(file)
    await session.flush()

    original = _make_tracklist(file_id=file.id, match_confidence=40, external_id="original-tl")
    session.add(original)

    existing = _make_tracklist(external_id="already-known", match_confidence=None)
    existing.artist = "Real Artist"
    existing.event = "Coachella"
    existing.date = date(2024, 4, 14)
    session.add(existing)
    await session.flush()

    with patch("phaze.routers.tracklists.TracklistScraper") as mock_scraper_cls:
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.scrape_tracklist = AsyncMock()
        mock_scraper.close = AsyncMock()

        response = await client.post(
            "/tracklists/link-result",
            data={"file_id": str(file.id), "external_id": "already-known", "url": existing.source_url},
        )

    assert response.status_code == 200
    mock_scraper.scrape_tracklist.assert_not_awaited()

    await session.refresh(existing)
    assert existing.file_id == file.id
    assert existing.match_confidence == 100

    await session.refresh(original)
    assert original.file_id == file.id
    assert original.match_confidence == 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malicious_url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint, wrong host + scheme
        "https://internal.local/admin",  # arbitrary internal host
        "https://www.1001tracklists.com.evil.com/tracklist/x/y.html",  # startswith-style bypass attempt
        "https://evilwww.1001tracklists.com/tracklist/x/y.html",  # lookalike host
        "ftp://www.1001tracklists.com/tracklist/x/y.html",  # right host, disallowed scheme
    ],
)
async def test_link_search_result_rejects_url_outside_scraper_host(session: AsyncSession, client: AsyncClient, malicious_url: str) -> None:
    """Security regression: the client-supplied ``url`` must never be handed to httpx unless it
    is scoped to the scraper's own host -- otherwise a tampered request turns this endpoint into
    an SSRF proxy that fetches arbitrary internal/external URLs on the server's behalf.
    """
    file = _make_file()
    session.add(file)
    await session.flush()

    with patch("phaze.routers.tracklists.TracklistScraper") as mock_scraper_cls:
        mock_scraper = mock_scraper_cls.return_value
        mock_scraper.scrape_tracklist = AsyncMock()
        mock_scraper.close = AsyncMock()

        response = await client.post(
            "/tracklists/link-result",
            data={"file_id": str(file.id), "external_id": "attacker-picked", "url": malicious_url},
        )

    assert response.status_code == 400
    mock_scraper.scrape_tracklist.assert_not_awaited()

    result = await session.execute(select(Tracklist).where(Tracklist.external_id == "attacker-picked"))
    assert result.scalar_one_or_none() is None


# --- Error branches ---


@pytest.mark.asyncio
async def test_approve_tracklist_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/approve returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/tracklists/{fake_id}/approve")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reject_tracklist_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/reject returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/tracklists/{fake_id}/reject")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reject_low_confidence_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/reject-low returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/tracklists/{fake_id}/reject-low?threshold=50")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_track_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """DELETE /tracklists/tracks/{id} returns 404 for non-existent track."""
    fake_id = uuid.uuid4()
    response = await client.delete(f"/tracklists/tracks/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_edit_track_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/tracks/{id}/edit/{field} returns 404 for non-existent track."""
    fake_id = uuid.uuid4()
    response = await client.get(f"/tracklists/tracks/{fake_id}/edit/artist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_save_track_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """PUT /tracklists/tracks/{id}/edit/{field} returns 404 for non-existent track."""
    fake_id = uuid.uuid4()
    response = await client.put(f"/tracklists/tracks/{fake_id}/edit/artist", data={"artist": "New"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_save_track_invalid_field(session: AsyncSession, client: AsyncClient) -> None:
    """PUT /tracklists/tracks/{id}/edit/{field} returns 400 for invalid field."""
    fake_id = uuid.uuid4()
    response = await client.put(f"/tracklists/tracks/{fake_id}/edit/invalid_field", data={"invalid_field": "x"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_save_track_timestamp_overwidth_rejected_value_preserved(session: AsyncSession, client: AsyncClient) -> None:
    """phaze-81au: an over-width timestamp edit must 422, not 500, and must not silently discard it.

    ``TracklistTrack.timestamp`` is ``String(20)`` (models/tracklist.py). Before the fix, ``setattr`` +
    ``commit`` handed Postgres a 27-char value, which raised an unhandled ``StringDataRightTruncation``
    -> HTTP 500 and left the operator's edit lost. The fix rejects at the boundary (wire_bounds rule 4)
    -- BEFORE the DB is touched -- and re-renders the same edit input with the typed value preserved
    (not blanked) plus an inline reason, because this is an operator-facing inline edit, not the agent
    wire path.
    """
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Artist",
        title="Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    overwidth = "2024-06-15 22:34:56.789 UTC"  # 27 chars, from the bead's own failure scenario
    assert len(overwidth) > 20

    response = await client.put(f"/tracklists/tracks/{track.id}/edit/timestamp", data={"timestamp": overwidth})

    assert response.status_code == 422
    # The operator's typed value survives in the re-rendered input -- not blanked, not the stale DB value.
    assert overwidth in response.text
    assert 'name="timestamp"' in response.text
    assert "hx-put" in response.text  # still editable -- the input round-trips, it is not a dead end
    # An inline reason is present so the operator learns WHY, without leaving this surface.
    assert "20 char" in response.text.lower()

    await session.refresh(track)
    assert track.timestamp == "00:00"  # untouched -- the rejected edit never reached the DB


@pytest.mark.asyncio
async def test_save_track_timestamp_at_column_width_boundary_saves(session: AsyncSession, client: AsyncClient) -> None:
    """A timestamp exactly 20 chars (the column width) is accepted, not off-by-one rejected."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Artist",
        title="Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    exactly_20 = "1" * 20
    response = await client.put(f"/tracklists/tracks/{track.id}/edit/timestamp", data={"timestamp": exactly_20})

    assert response.status_code == 200
    await session.refresh(track)
    assert track.timestamp == exactly_20


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["artist", "title", "timestamp"])
async def test_save_track_field_empty_persists_null_not_empty_string(session: AsyncSession, client: AsyncClient, field: str) -> None:
    """phaze-jsl9: clearing an inline edit must persist NULL, not "" -- CUE eligibility keys on
    ``timestamp.is_not(None)``, so a "" value would silently stay "eligible" forever.
    """
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Artist",
        title="Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    response = await client.put(f"/tracklists/tracks/{track.id}/edit/{field}", data={field: ""})
    assert response.status_code == 200
    assert "-" in response.text  # display coalesces the absent value

    await session.refresh(track)
    assert getattr(track, field) is None


@pytest.mark.asyncio
async def test_save_track_field_whitespace_only_persists_null(session: AsyncSession, client: AsyncClient) -> None:
    """A whitespace-only edit is treated the same as an empty one -- stripped to NULL."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Artist",
        title="Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    response = await client.put(f"/tracklists/tracks/{track.id}/edit/artist", data={"artist": "   "})
    assert response.status_code == 200

    await session.refresh(track)
    assert track.artist is None


@pytest.mark.asyncio
@pytest.mark.parametrize("garbage", ["abc", "12:xx", "1:2:3:4", "~5:00", "[1:02:03]", "05:24*"])
async def test_save_track_timestamp_unparseable_rejected_value_preserved(session: AsyncSession, client: AsyncClient, garbage: str) -> None:
    """phaze-jsl9: garbage that passes the length guard must still 422, not silently commit a
    non-NULL value that parse_timestamp_string can never parse.
    """
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Artist",
        title="Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    response = await client.put(f"/tracklists/tracks/{track.id}/edit/timestamp", data={"timestamp": garbage})

    assert response.status_code == 422
    assert garbage in response.text
    assert 'name="timestamp"' in response.text
    assert "hx-put" in response.text

    await session.refresh(track)
    assert track.timestamp == "00:00"  # untouched -- the rejected edit never reached the DB


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", ["01:02:03", "12:34", "90.5", "0"])
async def test_save_track_timestamp_valid_formats_accepted(session: AsyncSession, client: AsyncClient, valid: str) -> None:
    """Every format parse_timestamp_string documents (HH:MM:SS / MM:SS / float-seconds) still saves."""
    tl = _make_tracklist(source="fingerprint", status="proposed")
    session.add(tl)
    await session.flush()

    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    tl.latest_version_id = version.id

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist="Artist",
        title="Title",
        timestamp="00:00",
    )
    session.add(track)
    await session.flush()

    response = await client.put(f"/tracklists/tracks/{track.id}/edit/timestamp", data={"timestamp": valid})

    assert response.status_code == 200
    await session.refresh(track)
    assert track.timestamp == valid


# --- Discogs matching endpoints ---


def _make_version_with_tracks(session: AsyncSession, tl: Tracklist, num_tracks: int = 2) -> tuple[TracklistVersion, list[TracklistTrack]]:
    """Create a version with tracks for a tracklist. Call session.flush() after."""
    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    tracks = []
    for i in range(num_tracks):
        track = TracklistTrack(
            id=uuid.uuid4(),
            version_id=version.id,
            position=i + 1,
            artist=f"Artist {i + 1}",
            title=f"Title {i + 1}",
            timestamp=f"0{i}:00",
        )
        tracks.append(track)
    return version, tracks


@pytest.mark.asyncio
async def test_match_discogs_enqueues_task(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/match-discogs enqueues SAQ task and returns card."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    controller_queue, task_router = install_fake_queues(client)

    response = await client.post(f"/tracklists/{tl.id}/match-discogs")
    assert response.status_code == 200
    assert controller_queue.captured == [("match_tracklist_to_discogs", {"tracklist_id": str(tl.id)})]
    assert task_router.queues == {}


@pytest.mark.asyncio
async def test_match_discogs_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/match-discogs returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    install_fake_queues(client)

    response = await client.post(f"/tracklists/{fake_id}/match-discogs")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_discogs_candidates(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/{tl_id}/tracks/{t_id}/discogs returns candidate rows."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    track = tracks[0]
    link1 = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-12345",
        discogs_artist="deadmau5",
        discogs_title="Strobe",
        discogs_label="mau5trap",
        discogs_year=2009,
        confidence=87.0,
        status="candidate",
    )
    link2 = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-67890",
        discogs_artist="deadmau5",
        discogs_title="Strobe (Radio Edit)",
        confidence=72.0,
        status="candidate",
    )
    session.add_all([link1, link2])
    await session.flush()

    response = await client.get(f"/tracklists/{tl.id}/tracks/{track.id}/discogs")
    assert response.status_code == 200
    assert "deadmau5" in response.text
    assert "Strobe" in response.text
    assert "Accept Match" in response.text
    assert "Dismiss Match" in response.text


@pytest.mark.asyncio
async def test_get_discogs_candidates_empty(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/{tl_id}/tracks/{t_id}/discogs returns empty state when no candidates."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    track_id = uuid.uuid4()
    response = await client.get(f"/tracklists/{tl.id}/tracks/{track_id}/discogs")
    assert response.status_code == 200
    assert "No Discogs candidates" in response.text


@pytest.mark.asyncio
async def test_accept_discogs_link(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/discogs-links/{id}/accept sets accepted and dismisses siblings."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()

    track = tracks[0]
    link1 = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-111",
        discogs_artist="Artist A",
        discogs_title="Title A",
        confidence=90.0,
        status="candidate",
    )
    link2 = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-222",
        discogs_artist="Artist B",
        discogs_title="Title B",
        confidence=75.0,
        status="candidate",
    )
    session.add_all([link1, link2])
    await session.flush()

    response = await client.post(f"/tracklists/discogs-links/{link1.id}/accept")
    assert response.status_code == 200
    assert "Linked" in response.text

    await session.refresh(link1)
    await session.refresh(link2)
    assert link1.status == "accepted"
    assert link2.status == "dismissed"


@pytest.mark.asyncio
async def test_accept_discogs_link_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/discogs-links/{id}/accept returns 404 for non-existent link."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/tracklists/discogs-links/{fake_id}/accept")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_accept_discogs_link_handles_concurrent_candidate_delete(session: AsyncSession, client: AsyncClient) -> None:
    """phaze-xdu1: a candidate deleted by match_tracklist_to_discogs mid-accept yields a friendly 409, not a 500.

    When the match task's short candidate-swap transaction commits between the accept handler's SELECT
    and its write, the ORM UPDATE matches 0 rows and raises StaleDataError. The handler must roll back
    and re-render the current candidates rather than let StaleDataError escape as an unhandled 500 that
    silently loses the operator's click. We simulate the stale write by making commit raise StaleDataError.
    """
    from sqlalchemy.orm.exc import StaleDataError

    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()

    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-race",
        discogs_artist="Race Artist",
        discogs_title="Race Title",
        confidence=90.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    with patch.object(AsyncSession, "commit", new=AsyncMock(side_effect=StaleDataError("expected to update 1 row(s); 0 were matched"))):
        response = await client.post(f"/tracklists/discogs-links/{link.id}/accept")

    # Friendly 'candidates changed, refresh' re-render -- NOT an unhandled 500.
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_dismiss_discogs_link_handles_concurrent_candidate_delete(session: AsyncSession, client: AsyncClient) -> None:
    """phaze-xdu1: a candidate deleted by the match task mid-dismiss yields a friendly 409, not a 500."""
    from sqlalchemy.orm.exc import StaleDataError

    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()

    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-race2",
        discogs_artist="Race Artist",
        discogs_title="Race Title",
        confidence=60.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    with patch.object(AsyncSession, "commit", new=AsyncMock(side_effect=StaleDataError("expected to update 1 row(s); 0 were matched"))):
        response = await client.delete(f"/tracklists/discogs-links/{link.id}")

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_dismiss_discogs_link(session: AsyncSession, client: AsyncClient) -> None:
    """DELETE /tracklists/discogs-links/{id} sets status to dismissed."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()

    track = tracks[0]
    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-333",
        discogs_artist="Dismiss Me",
        discogs_title="Gone",
        confidence=60.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    response = await client.delete(f"/tracklists/discogs-links/{link.id}")
    assert response.status_code == 200
    # The dismissed link should not appear
    assert "Dismiss Me" not in response.text
    assert "No Discogs candidates" in response.text

    await session.refresh(link)
    assert link.status == "dismissed"


@pytest.mark.asyncio
async def test_dismiss_discogs_link_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """DELETE /tracklists/discogs-links/{id} returns 404 for non-existent link."""
    fake_id = uuid.uuid4()
    response = await client.delete(f"/tracklists/discogs-links/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_bulk_link_discogs(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/bulk-link accepts top candidate per track."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=2)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    # Track 1: two candidates
    link1a = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-a1",
        discogs_artist="A1",
        discogs_title="T1",
        confidence=95.0,
        status="candidate",
    )
    link1b = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-a2",
        discogs_artist="A2",
        discogs_title="T2",
        confidence=70.0,
        status="candidate",
    )
    # Track 2: one candidate
    link2a = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[1].id,
        discogs_release_id="r-b1",
        discogs_artist="B1",
        discogs_title="T3",
        confidence=80.0,
        status="candidate",
    )
    session.add_all([link1a, link1b, link2a])
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/bulk-link")
    assert response.status_code == 200

    await session.refresh(link1a)
    await session.refresh(link1b)
    await session.refresh(link2a)
    assert link1a.status == "accepted"
    assert link1b.status == "dismissed"
    assert link2a.status == "accepted"


@pytest.mark.asyncio
async def test_bulk_link_discogs_dismisses_preexisting_accepted_link(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/bulk-link never leaves two accepted DiscogsLinks for one track (D-07).

    Regression for phaze-gl1k: a track that already has an accepted link (e.g. from a prior
    individual accept, preserved by match_tracklist_to_discogs re-matching) must be folded into
    the same accept/dismiss pass as freshly loaded candidates, not left untouched alongside a new
    accepted row.
    """
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    track = tracks[0]
    # A pre-existing accepted link (not status=='candidate', so the old candidate-only query
    # would never see it).
    preexisting_accepted = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-old",
        discogs_artist="Old",
        discogs_title="Accepted",
        confidence=50.0,
        status="accepted",
    )
    # A freshly re-matched candidate with higher confidence.
    new_candidate = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id="r-new",
        discogs_artist="New",
        discogs_title="Candidate",
        confidence=95.0,
        status="candidate",
    )
    session.add_all([preexisting_accepted, new_candidate])
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/bulk-link")
    assert response.status_code == 200

    await session.refresh(preexisting_accepted)
    await session.refresh(new_candidate)

    statuses = {preexisting_accepted.status, new_candidate.status}
    accepted_count = sum(1 for s in (preexisting_accepted.status, new_candidate.status) if s == "accepted")
    assert accepted_count == 1, f"expected exactly one accepted link, got statuses={statuses}"
    # The higher-confidence link wins.
    assert new_candidate.status == "accepted"
    assert preexisting_accepted.status == "dismissed"


@pytest.mark.asyncio
async def test_bulk_link_discogs_no_candidates(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/bulk-link with no candidates returns gracefully."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    response = await client.post(f"/tracklists/{tl.id}/bulk-link")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_bulk_link_discogs_not_found(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/bulk-link returns 404 for non-existent tracklist."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/tracklists/{fake_id}/bulk-link")
    assert response.status_code == 404


# --- has_candidates and _cue_version wiring tests ---


@pytest.mark.asyncio
async def test_match_discogs_returns_has_candidates(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/match-discogs includes has_candidates when candidates exist."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    # Pre-create a candidate link
    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-test",
        discogs_artist="Test",
        discogs_title="Test Track",
        confidence=90.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    install_fake_queues(client)

    response = await client.post(f"/tracklists/{tl.id}/match-discogs")
    assert response.status_code == 200
    assert "Bulk-link All" in response.text


@pytest.mark.asyncio
async def test_approve_tracklist_has_candidates(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/approve includes Bulk-link button when candidates exist."""
    tl = _make_tracklist(status="proposed")
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-appr",
        discogs_artist="A",
        discogs_title="T",
        confidence=85.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/approve")
    assert response.status_code == 200
    assert "Bulk-link All" in response.text


@pytest.mark.asyncio
async def test_approve_tracklist_no_candidates_no_bulk_button(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/approve without candidates does not show Bulk-link button."""
    tl = _make_tracklist(status="proposed")
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/approve")
    assert response.status_code == 200
    assert "Bulk-link All" not in response.text


@pytest.mark.asyncio
async def test_undo_link_preserves_cue_version(session: AsyncSession, client: AsyncClient) -> None:
    """POST /tracklists/{id}/undo-link list response includes CUE version badge for other tracklists."""
    # READ-05/D-01: applied-ness comes from an executed proposal, not file.state (kept at 'moved').
    file1 = _make_file(original_path="/music/set1.mp3")
    file2 = _make_file(original_path="/music/set2.mp3")
    session.add_all([file1, file2])
    await session.flush()
    session.add_all([_make_executed_proposal(file1.id), _make_executed_proposal(file2.id)])
    await session.flush()

    # Tracklist to undo-link
    tl1 = _make_tracklist(file_id=file1.id, match_confidence=90, auto_linked=True, external_id="undo-cue-1")
    # Tracklist that should keep CUE badge
    tl2 = _make_tracklist(file_id=file2.id, match_confidence=95, external_id="undo-cue-2", status="approved")
    session.add_all([tl1, tl2])
    await session.flush()

    with patch("phaze.routers.tracklists._get_cue_version", return_value=2):
        response = await client.post(f"/tracklists/{tl1.id}/undo-link")
    assert response.status_code == 200
    assert "CUE v" in response.text


@pytest.mark.asyncio
async def test_list_tracklists_has_candidates_in_list(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ (HTMX) shows Bulk-link button when candidates exist."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-list",
        discogs_artist="List",
        discogs_title="Track",
        confidence=88.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Bulk-link All" in response.text


@pytest.mark.asyncio
async def test_render_tracklist_list_no_version_no_candidates(session: AsyncSession, client: AsyncClient) -> None:
    """Undo-link with tracklist lacking latest_version_id sets _has_candidates=False."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=90, auto_linked=True)
    tl.latest_version_id = None
    session.add(tl)
    await session.flush()

    response = await client.post(f"/tracklists/{tl.id}/undo-link")
    assert response.status_code == 200
    assert "Bulk-link All" not in response.text


@pytest.mark.asyncio
async def test_render_tracklist_list_approved_non_executed_cue_zero(session: AsyncSession, client: AsyncClient) -> None:
    """Undo-link list view shows cue_version=0 for approved tracklist with non-EXECUTED file."""
    file_exec = _make_file(original_path="/music/exec.mp3")
    file_disc = _make_file(original_path="/music/disc.mp3")
    session.add_all([file_exec, file_disc])
    await session.flush()

    # Tracklist to undo
    tl1 = _make_tracklist(file_id=file_exec.id, match_confidence=90, auto_linked=True, external_id="cue-zero-1")
    # Approved tracklist with non-EXECUTED file — should get _cue_version=0
    tl2 = _make_tracklist(file_id=file_disc.id, match_confidence=95, external_id="cue-zero-2", status="approved")
    session.add_all([tl1, tl2])
    await session.flush()

    response = await client.post(f"/tracklists/{tl1.id}/undo-link")
    assert response.status_code == 200
    # tl2 is approved with non-EXECUTED file, so no CUE badge
    assert "CUE v" not in response.text


@pytest.mark.asyncio
async def test_render_tracklist_list_cue_version_not_approved(session: AsyncSession, client: AsyncClient) -> None:
    """Undo-link list view shows cue_version=0 for non-approved tracklist."""
    file = _make_file()
    session.add(file)
    await session.flush()

    tl1 = _make_tracklist(file_id=file.id, match_confidence=90, auto_linked=True, external_id="cue-na-1")
    # Proposed (not approved) tracklist with EXECUTED file — should get _cue_version=0
    tl2 = _make_tracklist(external_id="cue-na-2", status="proposed")
    session.add_all([tl1, tl2])
    await session.flush()

    response = await client.post(f"/tracklists/{tl1.id}/undo-link")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_tracklists_cue_version_executed(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ shows CUE badge for approved tracklist with an applied (executed-proposal) file.

    READ-05/D-01: the cue-version guard reads ``is_applied`` (an executed proposal), so the file is
    left at ``state='moved'`` and made applied via an executed proposal -- proving the badge derives
    from ``proposals.status``, not ``files.state``.
    """
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(_make_executed_proposal(file.id))
    await session.flush()

    tl = _make_tracklist(file_id=file.id, match_confidence=90, status="approved")
    session.add(tl)
    await session.flush()

    with patch("phaze.routers.tracklists._get_cue_version", return_value=3):
        response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "CUE v3" in response.text


@pytest.mark.asyncio
async def test_list_tracklists_has_candidates_full_page(session: AsyncSession, client: AsyncClient) -> None:
    """GET /tracklists/ (full page, no HTMX) shows Bulk-link button when candidates exist."""
    tl = _make_tracklist()
    session.add(tl)
    await session.flush()

    version, tracks = _make_version_with_tracks(session, tl, num_tracks=1)
    session.add(version)
    session.add_all(tracks)
    await session.flush()
    tl.latest_version_id = version.id

    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=tracks[0].id,
        discogs_release_id="r-full",
        discogs_artist="Full",
        discogs_title="Page",
        confidence=85.0,
        status="candidate",
    )
    session.add(link)
    await session.flush()

    response = await client.get("/tracklists/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Bulk-link All" in response.text


# ---------------------------------------------------------------------------
# ``/tracklists/`` history-restore response shape (phaze-64uy) -- HYGIENE, not a live defect.
#
# This handler branched on the raw ``HX-Request`` header, which routers/response_shape.py rule 1
# bans outright. But NOTHING in the template corpus pushes a ``/tracklists/`` URL into history
# (tracklists/partials/pagination.html issues the GET WITHOUT hx-push-url), so no history restore can currently REACH this handler and the raw check was not
# reachable-broken the way shell.py / proposals.py / duplicates.py / admin_agents.py were.
#
# It is converted, and pinned here, so that adding ``hx-push-url`` to these controls later cannot
# silently re-introduce the defect: the shape would already be correct on the day the URL starts
# entering history.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracklists_history_restore_does_not_return_a_fragment(client: AsyncClient) -> None:
    """A history-restore GET ``/tracklists/`` falls through to the shell redirect, not the fragment.

    Asserts the SHAPE, not merely a 200 -- before the fix this returned a 200 fragment, which htmx
    would have swapped into ``<body>``, replacing the whole page.
    """
    response = await client.get("/tracklists/?filter=all", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert response.status_code == 302, "a restore must not be answered with a chrome-less 200 fragment"
    assert response.headers["location"] == "/s/tracklist"


@pytest.mark.asyncio
async def test_tracklists_history_restore_resolves_to_a_full_document(client: AsyncClient) -> None:
    """Following that redirect yields a FULL document with chrome intact."""
    response = await client.get(
        "/tracklists/?filter=all",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a history restore must resolve to a full document"
    assert 'aria-label="Pipeline navigation"' in body, "the page chrome must be present after a restore"


@pytest.mark.asyncio
async def test_tracklists_live_htmx_swap_still_returns_the_fragment(client: AsyncClient) -> None:
    """The other direction: an ordinary htmx swap must still get the chrome-less fragment."""
    response = await client.get("/tracklists/?filter=all", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower(), "a live htmx swap must get a fragment, not a full document"


@pytest.mark.asyncio
async def test_tracklists_restore_header_alone_does_not_return_a_fragment(client: AsyncClient) -> None:
    """The restore header dominates even without ``HX-Request`` (response_shape rule 2)."""
    response = await client.get("/tracklists/?filter=all", headers={"HX-History-Restore-Request": "true"})
    assert response.status_code == 302
    assert response.headers["location"] == "/s/tracklist"


@pytest.mark.asyncio
async def test_tracklists_live_swap_carries_no_duplicate_list_wrapper(client: AsyncClient) -> None:
    """The live swap fragment carries ZERO ``#tracklists-list`` wrappers (phaze-64uy).

    ``tracklist_list.html`` used to compute its own ``is_hx`` from
    ``request.headers.get('HX-Request')`` -- the exact expression response_shape rule 1 bans, and
    the exact defect phaze-xc84 had already found in the sibling ``scan_tab.html``. The handler now
    supplies the flag as ``wants_fragment(request)``, so ONE predicate drives both the response
    shape and the wrapper gate and they can no longer disagree.

    The fragment is swapped INTO the existing ``#tracklists-list``, so a second copy would duplicate
    the id in the live document and strand every later swap on the stale outer element (the class
    already on record in phaze-gzrd / op6f / 7j50 / 5p43).
    """
    response = await client.get("/tracklists/?filter=all", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert response.text.count('id="tracklists-list"') == 0, "the fragment swaps INTO #tracklists-list and must not carry a second one"
