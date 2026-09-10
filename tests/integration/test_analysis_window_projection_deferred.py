"""phaze-5ergb: the tracklist swap routes and the record page/poster must not pay for
``AnalysisWindow.features``.

``routers/pipeline/tracklists.py``'s ``_track_segments_for`` and ``routers/record.py``'s
``build_file_record_context`` both load every ``AnalysisWindow`` row for a file to compute a
few medians (``build_track_segments`` / ``build_analysis_timeline_context`` /
``build_harmonic_journey`` -- tier/start/end/bpm/camelot/energy/mood_scores only). Neither ever
reads the ``features`` column: it is the ~5 KB per-coarse-window JSONB the narrow projection
columns exist specifically so no viewer surface has to decode (see the ``AnalysisWindow``
docstring in ``models/analysis.py``). A 12 h set carries ~240 coarse windows, so an undeferred
load transfers and decodes 1-2 MB on every single-file button click.

The capture technique is ``tests/integration/test_files_set_glyph.py`` /
``tests/integration/test_record_similarity_queries.py``'s -- a ``before_cursor_execute``
listener on the test's own connection. Asserted at the SQL layer, not the rendered HTML,
because that is the layer where the defect lives: the fragment already renders the right
numbers whether or not ``features`` came over the wire.

Deferring a column on an ASYNC session is only safe if nothing downstream ever touches the
attribute -- an accidental lazy load on a deferred column raises ``MissingGreenlet`` on an
async session rather than quietly fetching it. So every test below also asserts no FOLLOW-UP
``SELECT ... features`` fires, which is what would happen if some consumer secretly needed it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event

from tests.shared.test_record_track_segments import _seed_set_with_tracklist_and_windows


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession


pytestmark = pytest.mark.integration


async def _capture_get(
    client: AsyncClient,
    connection: AsyncConnection,
    url: str,
    *,
    htmx: bool = False,
) -> list[str]:
    """GET ``url``, returning every statement the app issued serving it."""
    statements: list[str] = []

    def _capture(conn, cursor, statement: str, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    sync_conn = connection.sync_connection
    event.listen(sync_conn, "before_cursor_execute", _capture)
    try:
        response = await client.get(url, headers={"HX-Request": "true"} if htmx else None)
    finally:
        event.remove(sync_conn, "before_cursor_execute", _capture)
    assert response.status_code == 200
    return statements


async def _capture_post(client: AsyncClient, connection: AsyncConnection, url: str) -> list[str]:
    """POST ``url``, returning every statement the app issued serving it."""
    statements: list[str] = []

    def _capture(conn, cursor, statement: str, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    sync_conn = connection.sync_connection
    event.listen(sync_conn, "before_cursor_execute", _capture)
    try:
        response = await client.post(url)
    finally:
        event.remove(sync_conn, "before_cursor_execute", _capture)
    assert response.status_code == 200
    return statements


def _window_selects(statements: list[str]) -> list[str]:
    return [s for s in statements if "select" in s.lower() and "analysis_window" in s.lower()]


def _features_selects(statements: list[str]) -> list[str]:
    """Any statement naming ``analysis_window``'s ``features`` column -- the initial scan (must
    select none of these) or a lazy-load follow-up (would mean deferring it was unsafe).

    Scoped to ``analysis_window`` specifically: ``AnalysisResult`` (the 1:1 ``analysis`` table)
    carries its OWN unrelated ``features`` column that these routes legitimately select in full
    -- out of this bead's scope -- so a bare ``"features" in statement`` would false-positive on
    every request.
    """
    return [s for s in statements if "analysis_window" in s.lower() and "features" in s.lower()]


@pytest.mark.asyncio
async def test_record_page_window_scan_never_selects_features(
    client: AsyncClient,
    session: AsyncSession,
    make_file,
    _db_connection: AsyncConnection,  # type: ignore[no-untyped-def]
) -> None:
    file_rec = await _seed_set_with_tracklist_and_windows(make_file, session)

    statements = await _capture_get(client, _db_connection, f"/files/{file_rec.id}")

    window_selects = _window_selects(statements)
    assert window_selects, "expected the page to issue an analysis_window SELECT"
    assert _features_selects(statements) == [], f"no statement may name analysis_window.features, saw: {_features_selects(statements)}"
    # The projection columns the join actually reads are still there -- proving this is a
    # narrowed projection, not an accidentally-empty query.
    scan = window_selects[0].lower()
    for column in ("tier", "start_sec", "end_sec", "bpm", "camelot", "energy", "mood_scores"):
        assert f"analysis_window.{column}" in scan, f"the scan must still carry {column}"


@pytest.mark.asyncio
async def test_record_drawer_window_scan_never_selects_features(
    client: AsyncClient,
    session: AsyncSession,
    make_file,
    _db_connection: AsyncConnection,  # type: ignore[no-untyped-def]
) -> None:
    file_rec = await _seed_set_with_tracklist_and_windows(make_file, session)

    statements = await _capture_get(client, _db_connection, f"/record/{file_rec.id}", htmx=True)

    assert _window_selects(statements), "expected the drawer to issue an analysis_window SELECT"
    assert _features_selects(statements) == [], f"no statement may name analysis_window.features, saw: {_features_selects(statements)}"


@pytest.mark.asyncio
async def test_poster_window_scan_never_selects_features(
    client: AsyncClient,
    session: AsyncSession,
    make_file,
    _db_connection: AsyncConnection,  # type: ignore[no-untyped-def]
) -> None:
    file_rec = await _seed_set_with_tracklist_and_windows(make_file, session)

    statements = await _capture_get(client, _db_connection, f"/files/{file_rec.id}/poster.svg")

    assert _window_selects(statements), "expected the poster to issue an analysis_window SELECT"
    assert _features_selects(statements) == [], f"no statement may name analysis_window.features, saw: {_features_selects(statements)}"


@pytest.mark.asyncio
async def test_prioritize_swap_window_scan_never_selects_features(
    client: AsyncClient,
    session: AsyncSession,
    make_file,
    _db_connection: AsyncConnection,  # type: ignore[no-untyped-def]
) -> None:
    """POST .../prioritize re-renders the same tracklist fragment via ``_track_segments_for``."""
    file_rec = await _seed_set_with_tracklist_and_windows(make_file, session)

    statements = await _capture_post(client, _db_connection, f"/pipeline/tracklists/{file_rec.id}/prioritize")

    assert _window_selects(statements), "expected the swap to issue an analysis_window SELECT"
    assert _features_selects(statements) == [], f"no statement may name analysis_window.features, saw: {_features_selects(statements)}"


@pytest.mark.asyncio
async def test_refresh_swap_window_scan_never_selects_features(
    client: AsyncClient,
    session: AsyncSession,
    make_file,
    _db_connection: AsyncConnection,  # type: ignore[no-untyped-def]
) -> None:
    file_rec = await _seed_set_with_tracklist_and_windows(make_file, session)

    statements = await _capture_post(client, _db_connection, f"/pipeline/tracklists/{file_rec.id}/refresh")

    assert _window_selects(statements), "expected the swap to issue an analysis_window SELECT"
    assert _features_selects(statements) == [], f"no statement may name analysis_window.features, saw: {_features_selects(statements)}"


@pytest.mark.asyncio
async def test_unprioritize_swap_window_scan_never_selects_features(
    client: AsyncClient,
    session: AsyncSession,
    make_file,
    _db_connection: AsyncConnection,  # type: ignore[no-untyped-def]
) -> None:
    file_rec = await _seed_set_with_tracklist_and_windows(make_file, session)

    statements = await _capture_post(client, _db_connection, f"/pipeline/tracklists/{file_rec.id}/unprioritize")

    assert _window_selects(statements), "expected the swap to issue an analysis_window SELECT"
    assert _features_selects(statements) == [], f"no statement may name analysis_window.features, saw: {_features_selects(statements)}"
