"""Integration tests for unified search UI endpoints."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def create_searchable_file(
    session: AsyncSession,
    *,
    original_filename: str = "deadmau5 - Strobe.mp3",
    artist: str | None = "deadmau5",
    genre: str | None = "progressive house",
    bpm: float | None = 128.0,
) -> FileRecord:
    """Create FileRecord + FileMetadata + AnalysisResult for search testing."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{original_filename}",
        original_filename=original_filename,
        current_path=f"/music/{original_filename}",
        file_type="music",
        file_size=5_000_000,
    )
    session.add(file_record)
    await session.flush()

    metadata = FileMetadata(
        id=uuid.uuid4(),
        file_id=file_id,
        artist=artist,
        title=original_filename.rsplit(".", 1)[0],
        genre=genre,
    )
    session.add(metadata)
    await session.flush()

    analysis = AnalysisResult(
        id=uuid.uuid4(),
        file_id=file_id,
        bpm=bpm,
    )
    session.add(analysis)
    await session.commit()
    return file_record


async def create_searchable_tracklist(
    session: AsyncSession,
    *,
    artist: str = "deadmau5",
    event: str = "Coachella 2024",
    status: str = "approved",
    tracklist_date: date | None = None,
) -> Tracklist:
    """Create Tracklist for search testing."""
    tracklist = Tracklist(
        id=uuid.uuid4(),
        external_id=f"tl-{uuid.uuid4().hex[:8]}",
        source_url=f"https://1001tracklists.com/{uuid.uuid4().hex[:8]}",
        artist=artist,
        event=event,
        status=status,
        date=tracklist_date,
        source="1001tracklists",
    )
    session.add(tracklist)
    await session.commit()
    return tracklist


@pytest.mark.asyncio
async def test_search_page_loads(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05 / D-04): /search is renamed to the ⌘K command palette.

    A plain GET /search/ 302-redirects to the shell root with ``?palette=1`` (the shell
    Alpine reads it to auto-open the palette). The legacy search landing page (heading +
    summary counts + filter panel) is retired by the rename; the live results path survives
    via the HX fragment (test_search_with_query_returns_results covers it).
    """
    response = await client.get("/search/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/?palette=1"


@pytest.mark.asyncio
async def test_search_with_query_returns_results(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=deadmau5 returns 200 with grouped ⌘K palette rows (v7.0 RECORD-02).

    The flat results table was superseded by the grouped command-palette listbox: matching files
    now render as ``role="option"`` rows under the Files group instead of table rows.
    """
    await create_searchable_file(session, original_filename="deadmau5 - Strobe.mp3", artist="deadmau5")
    response = await client.get("/search/", params={"q": "deadmau5"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "deadmau5" in response.text
    assert 'role="option"' in response.text
    assert "Files" in response.text


@pytest.mark.asyncio
async def test_search_returns_file_and_tracklist_results(client: AsyncClient, session: AsyncSession) -> None:
    """Results contain both File and Tracklist type badges."""
    await create_searchable_file(session, original_filename="deadmau5 - Strobe.mp3", artist="deadmau5")
    await create_searchable_tracklist(session, artist="deadmau5", event="deadmau5 Coachella 2024")
    response = await client.get("/search/", params={"q": "deadmau5"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-400" in response.text  # File badge
    assert "bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400" in response.text  # Tracklist badge


@pytest.mark.asyncio
async def test_search_no_results_message(client: AsyncClient) -> None:
    """GET /search/?q=nonexistent returns No results found message (D-07)."""
    response = await client.get("/search/", params={"q": "xyznonexistent123"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "No results found" in response.text
    assert "xyznonexistent123" in response.text


@pytest.mark.asyncio
async def test_search_htmx_partial(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=test with HX-Request header returns partial (no base.html wrapper)."""
    await create_searchable_file(session, original_filename="test track.mp3", artist="test")
    response = await client.get("/search/", params={"q": "test"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text


@pytest.mark.asyncio
async def test_search_artist_filter(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=Strobe&artist=deadmau5 narrows results."""
    await create_searchable_file(session, original_filename="deadmau5 - Strobe.mp3", artist="deadmau5")
    await create_searchable_file(session, original_filename="Daft Punk - Strobe Remix.mp3", artist="Daft Punk")
    response = await client.get("/search/", params={"q": "Strobe", "artist": "deadmau5"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "deadmau5" in response.text


@pytest.mark.asyncio
async def test_search_bpm_filter(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=deadmau5&bpm_min=120&bpm_max=130 narrows results."""
    await create_searchable_file(session, original_filename="deadmau5 - Strobe.mp3", artist="deadmau5", bpm=128.0)
    await create_searchable_file(session, original_filename="deadmau5 - Raise Your Weapon.mp3", artist="deadmau5", bpm=140.0)
    response = await client.get("/search/", params={"q": "deadmau5", "bpm_min": "120", "bpm_max": "130"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Strobe" in response.text


@pytest.mark.asyncio
async def test_search_date_filter_narrows_results(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-z3tx: GET /search/?q=...&date_from=...&date_to=... actually WORKS end to end.

    Before the fix, date_from/date_to were declared `str`, forwarded unparsed against
    Tracklist.date (a Date column) / FileRecord.created_at (a DateTime column), and EVERY value
    500'd -- q alone -> 200, q+date_from -> 500, q+date_to -> 500 (reproduced in the bead). This
    proves the feature is not merely non-crashing but actually filters: a valid ISO date string
    parses to a real `date` at the boundary and narrows to the in-range tracklist.
    """
    today = date.today()
    await create_searchable_tracklist(session, artist="DJ Recent", event="Recent Fest", tracklist_date=today)
    await create_searchable_tracklist(session, artist="DJ Old", event="Old Fest", tracklist_date=today - timedelta(days=365))
    response = await client.get(
        "/search/",
        params={
            "q": "fest",
            "date_from": (today - timedelta(days=30)).isoformat(),
            "date_to": (today + timedelta(days=1)).isoformat(),
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "Recent Fest" in response.text
    assert "Old Fest" not in response.text


@pytest.mark.asyncio
async def test_search_date_filter_rejects_invalid_date(client: AsyncClient) -> None:
    """An unparseable date_from is a clean 422 at the boundary, never a 500 (wire_bounds rule 4/6)."""
    response = await client.get("/search/", params={"q": "deadmau5", "date_from": "not-a-date"}, headers={"HX-Request": "true"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_ignores_removed_file_state_param(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 90 (PR-A, D-11): the ``file_state`` facet is removed; a stale/legacy param is harmlessly ignored.

    The endpoint no longer declares a ``file_state`` Query param, so a bookmarked ⌘K URL carrying it must
    NOT 422 -- FastAPI ignores the unknown param and the palette renders the full (un-narrowed) match set.
    """
    await create_searchable_file(session, original_filename="deadmau5 - Strobe.mp3")
    await create_searchable_file(session, original_filename="deadmau5 - FML.mp3")
    response = await client.get("/search/", params={"q": "deadmau5", "file_state": "approved"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    # The removed facet no longer narrows -- BOTH deadmau5 files render under the palette Files group.
    assert "Strobe" in response.text
    assert "FML" in response.text


@pytest.mark.asyncio
async def test_search_pagination(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=track&page_size=25 renders the matched files as palette rows (v7.0 RECORD-02).

    The ⌘K command palette is a top-N surface — the old paginated ``Showing X-Y of Z`` footer was
    retired with the flat table. The page/page_size params still bound the underlying search(),
    so the matched files surface as ``role="option"`` rows under the Files group.
    """
    for i in range(30):
        await create_searchable_file(
            session,
            original_filename=f"track {i:03d}.mp3",
            artist=f"artist {i:03d}",
        )
    response = await client.get("/search/", params={"q": "track", "page": "1", "page_size": "25"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'role="option"' in response.text
    assert "Files" in response.text


@pytest.mark.asyncio
async def test_search_nav_tab_first(client: AsyncClient) -> None:
    """Phase 57 (SHELL-03/05): the legacy search/pipeline nav tabs are gone (DAG rail).

    Plan 57-03 retired the base.html tab-bar, so there is no nav-tab ordering to assert; a
    plain GET /search/ now 302-redirects to the ⌘K palette (/?palette=1).
    """
    response = await client.get("/search/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/?palette=1"


@pytest.mark.asyncio
async def test_search_filter_panel_collapsed(client: AsyncClient) -> None:
    """Phase 57 (SHELL-05 / D-04): the legacy search filter panel is retired by the ⌘K rename.

    The collapsible filter panel was search-page chrome; the page is replaced by the command
    palette, so a plain GET /search/ now 302-redirects to /?palette=1.
    """
    response = await client.get("/search/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/?palette=1"


# ---------------------------------------------------------------------------
# Discogs search integration (DISC-03)
# ---------------------------------------------------------------------------


async def create_searchable_discogs_link(
    session: AsyncSession,
    *,
    discogs_artist: str = "Daft Punk",
    discogs_title: str = "Random Access Memories",
    discogs_year: int = 2013,
    status: str = "accepted",
) -> DiscogsLink:
    """Create a DiscogsLink with required parent chain for search testing."""
    tracklist = Tracklist(
        id=uuid.uuid4(),
        external_id=f"tl-{uuid.uuid4().hex[:8]}",
        source_url=f"https://1001tracklists.com/{uuid.uuid4().hex[:8]}",
        artist=discogs_artist,
        event="Test Event",
        status="approved",
        source="1001tracklists",
    )
    session.add(tracklist)
    await session.flush()

    version = TracklistVersion(
        id=uuid.uuid4(),
        tracklist_id=tracklist.id,
        version_number=1,
    )
    session.add(version)
    await session.flush()

    track = TracklistTrack(
        id=uuid.uuid4(),
        version_id=version.id,
        position=1,
        artist=discogs_artist,
        title=discogs_title,
    )
    session.add(track)
    await session.flush()

    link = DiscogsLink(
        id=uuid.uuid4(),
        track_id=track.id,
        discogs_release_id=f"r-{uuid.uuid4().hex[:8]}",
        discogs_artist=discogs_artist,
        discogs_title=discogs_title,
        discogs_label="Test Label",
        discogs_year=discogs_year,
        confidence=90.0,
        status=status,
    )
    session.add(link)
    await session.commit()
    return link


@pytest.mark.asyncio
async def test_search_returns_discogs_results(client: AsyncClient, session: AsyncSession) -> None:
    """GET /search/?q=daft+punk returns Discogs results with discogs_release type."""
    await create_searchable_discogs_link(session, discogs_artist="Daft Punk", discogs_title="Random Access Memories")
    response = await client.get("/search/", params={"q": "daft punk"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Discogs" in response.text


@pytest.mark.asyncio
async def test_search_discogs_purple_pill(client: AsyncClient, session: AsyncSession) -> None:
    """Discogs results render with purple pill badge."""
    await create_searchable_discogs_link(session, discogs_artist="Bonobo", discogs_title="Migration")
    response = await client.get("/search/", params={"q": "bonobo"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "bg-purple-100 dark:bg-purple-950 text-purple-700 dark:text-purple-400" in response.text


@pytest.mark.asyncio
async def test_search_three_entity_types(client: AsyncClient, session: AsyncSession) -> None:
    """Search results contain File (blue), Tracklist (green), and Discogs (purple) badges."""
    await create_searchable_file(session, original_filename="bonobo - migration.mp3", artist="bonobo")
    await create_searchable_tracklist(session, artist="bonobo", event="bonobo Coachella 2024")
    await create_searchable_discogs_link(session, discogs_artist="bonobo", discogs_title="migration")
    response = await client.get("/search/", params={"q": "bonobo"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-400" in response.text  # File
    assert "bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400" in response.text  # Tracklist
    assert "bg-purple-100 dark:bg-purple-950 text-purple-700 dark:text-purple-400" in response.text  # Discogs


@pytest.mark.asyncio
async def test_summary_counts_include_discogs(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05 / D-04): the search landing summary-counts moved to the ⌘K palette.

    The empty-query landing page (with its Discogs-link summary count) is retired by the
    search → ⌘K rename, so a plain GET /search/ now 302-redirects to /?palette=1. The
    discogs-result rendering itself is covered by test_search_returns_discogs_results.
    """
    response = await client.get("/search/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/?palette=1"


# ---------------------------------------------------------------------------
# ``/search/`` history-restore response shape (phaze-64uy) -- HYGIENE, not a live defect.
#
# This handler branched on the raw ``HX-Request`` header, which routers/response_shape.py rule 1
# bans outright. But NOTHING in the template corpus pushes a ``/search/`` URL into history
# (shell/partials/cmdk_modal.html and search/partials/palette_results.html both issue the GET without hx-push-url), so no history restore can currently REACH this handler and the raw check was not
# reachable-broken the way shell.py / proposals.py / duplicates.py / admin_agents.py were.
#
# It is converted, and pinned here, so that adding ``hx-push-url`` to these controls later cannot
# silently re-introduce the defect: the shape would already be correct on the day the URL starts
# entering history.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_history_restore_does_not_return_a_fragment(client: AsyncClient) -> None:
    """A history-restore GET ``/search/`` falls through to the shell redirect, not the fragment.

    Asserts the SHAPE, not merely a 200 -- before the fix this returned a 200 fragment, which htmx
    would have swapped into ``<body>``, replacing the whole page.
    """
    response = await client.get("/search/?q=test", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert response.status_code == 302, "a restore must not be answered with a chrome-less 200 fragment"
    assert response.headers["location"] == "/?palette=1"


@pytest.mark.asyncio
async def test_search_history_restore_resolves_to_a_full_document(client: AsyncClient) -> None:
    """Following that redirect yields a FULL document with chrome intact."""
    response = await client.get(
        "/search/?q=test",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a history restore must resolve to a full document"
    assert 'aria-label="Pipeline navigation"' in body, "the page chrome must be present after a restore"


@pytest.mark.asyncio
async def test_search_live_htmx_swap_still_returns_the_fragment(client: AsyncClient) -> None:
    """The other direction: an ordinary htmx swap must still get the chrome-less fragment."""
    response = await client.get("/search/?q=test", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower(), "a live htmx swap must get a fragment, not a full document"


@pytest.mark.asyncio
async def test_search_restore_header_alone_does_not_return_a_fragment(client: AsyncClient) -> None:
    """The restore header dominates even without ``HX-Request`` (response_shape rule 2)."""
    response = await client.get("/search/?q=test", headers={"HX-History-Restore-Request": "true"})
    assert response.status_code == 302
    assert response.headers["location"] == "/?palette=1"
