"""phaze-zb5y9: WHICH record presentation pays the similarity scan, and what that scan carries.

``services/set_similarity.find_similar_sets`` scans ``set_profile`` corpus-wide. Only
``record_page.html``'s sidebar has a slot for the result: the drawer (``record_body.html``,
issued on every Files-table row click) and the poster (``poster.svg``) render nothing from it.
Before this bead all three paid the scan, and the scan hydrated whole ``SetProfile`` entities --
including ``glyph``, the per-window JSONB the sidebar renders and the scoring path never reads.

These assertions are made at the SQL layer rather than against the rendered HTML, because that
is the layer where the defect lived: the drawer's markup was already correct (it showed no
neighbours) while the query behind it scanned the corpus. The capture technique is
``tests/integration/test_files_set_glyph.py``'s -- a ``before_cursor_execute`` listener on the
test's own connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import event

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.set_profile import SetProfile
from phaze.services.set_projection import ARC_POINTS, MOOD_ORDER
from phaze.services.set_similarity import TOP_N


if TYPE_CHECKING:
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession


pytestmark = pytest.mark.integration


_VECTOR = [0.5] * len(MOOD_ORDER)
_ARC = [0.4] * ARC_POINTS
_GLYPH = [{"camelot_number": 8, "energy": 0.5}, {"camelot_number": 9, "energy": 0.6}]


def _make_file(marker: str) -> FileRecord:
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex + uid.hex,
        original_path=f"/music/{marker}-{uid.hex}.mp3",
        original_filename=f"{marker}-{uid.hex}.mp3",
        current_path=f"/music/{marker}-{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )


async def _seed_query_file_and_neighbours(session: AsyncSession, count: int) -> FileRecord:
    """One profiled query file plus ``count`` profiled, scoreable neighbours.

    Every profile carries a ``glyph``, so a scan that hydrates entities really would have to
    read the column the assertions below say it must not.
    """
    query = _make_file("simquery")
    neighbours = [_make_file(f"simpeer{i}") for i in range(count)]
    session.add_all([query, *neighbours])
    await session.commit()
    for record in (query, *neighbours):
        session.add(SetProfile(file_id=record.id, mean_vector=list(_VECTOR), arc=list(_ARC), camelot_modal="8A", glyph=_GLYPH))
        session.add(AnalysisResult(id=uuid.uuid4(), file_id=record.id, bpm=128.0, style="techno", mood="energetic"))
    await session.commit()
    return query


def _candidate_scans(statements: list[str]) -> list[str]:
    """The similarity CANDIDATE scan(s): the only ``set_profile`` read that joins ``files``.

    Deliberately not "any ``set_profile`` SELECT" -- every presentation legitimately reads THIS
    file's own profile by primary key (``routers/record.py``'s ``session.get``) for the facts
    panel and glyph, and the winners' hydration reads ``set_profile`` alone too. The join against
    ``files`` (for the neighbour titles) is what makes the corpus-wide scan distinguishable from
    both.
    """
    return [s for s in statements if "set_profile" in s.lower() and "join files" in s.lower()]


def _set_profile_selects(statements: list[str]) -> list[str]:
    return [s for s in statements if "select" in s.lower() and "set_profile" in s.lower()]


async def _capture_get(
    client: AsyncClient,
    connection: AsyncConnection,
    url: str,
    *,
    htmx: bool = False,
) -> tuple[Response, list[str]]:
    """GET ``url``, returning the response and every statement the app issued serving it."""
    statements: list[str] = []

    def _capture(conn, cursor, statement: str, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    sync_conn = connection.sync_connection
    event.listen(sync_conn, "before_cursor_execute", _capture)
    try:
        response = await client.get(url, headers={"HX-Request": "true"} if htmx else None)
    finally:
        event.remove(sync_conn, "before_cursor_execute", _capture)
    return response, statements


@pytest.mark.asyncio
async def test_the_drawer_issues_no_similarity_candidate_scan(client: AsyncClient, session: AsyncSession, _db_connection: AsyncConnection) -> None:
    """The Files-table row click -- the highest-frequency record request in the product, and the
    one that rendered nothing from the scan it was paying for."""
    query = await _seed_query_file_and_neighbours(session, count=4)

    response, statements = await _capture_get(client, _db_connection, f"/record/{query.id}", htmx=True)

    assert response.status_code == 200
    assert _candidate_scans(statements) == [], f"the drawer must not scan the corpus, saw: {_candidate_scans(statements)}"
    # It still reads THIS file's own profile for the set panel -- the scan is what is gone, not
    # every set_profile read, so this proves the assertion above is about the right query.
    assert _set_profile_selects(statements), "the drawer still reads the file's OWN profile by primary key"


@pytest.mark.asyncio
async def test_the_poster_issues_no_similarity_candidate_scan(client: AsyncClient, session: AsyncSession, _db_connection: AsyncConnection) -> None:
    """The printable poster renders the arc, the wheel and the tracklist -- no neighbour slot."""
    query = await _seed_query_file_and_neighbours(session, count=4)

    response, statements = await _capture_get(client, _db_connection, f"/files/{query.id}/poster.svg")

    assert response.status_code == 200
    assert _candidate_scans(statements) == [], f"the poster must not scan the corpus, saw: {_candidate_scans(statements)}"


@pytest.mark.asyncio
async def test_the_full_page_does_issue_exactly_one_candidate_scan(
    client: AsyncClient, session: AsyncSession, _db_connection: AsyncConnection
) -> None:
    """The control for the two tests above: the presentation that RENDERS neighbours still runs
    the scan, exactly once. Without this, "no scan" would be equally green if the feature had
    simply been deleted."""
    query = await _seed_query_file_and_neighbours(session, count=4)

    response, statements = await _capture_get(client, _db_connection, f"/files/{query.id}")

    assert response.status_code == 200
    assert len(_candidate_scans(statements)) == 1, f"expected one candidate scan for the page, saw: {_candidate_scans(statements)}"
    assert "<li data-similar-set>" in response.text


@pytest.mark.asyncio
async def test_the_candidate_scan_never_selects_the_glyph_column(client: AsyncClient, session: AsyncSession, _db_connection: AsyncConnection) -> None:
    """The scan selects the four scored ``set_profile`` columns and no more.

    ``glyph`` is the expensive one -- ~240 JSONB cells for a 12 h set, decoded for every
    profiled file to render three. Asserting the column list positively (rather than only
    "glyph absent") is what stops a future ``select(SetProfile)`` regression sneaking the whole
    entity back in under a different column name.
    """
    query = await _seed_query_file_and_neighbours(session, count=4)

    _response, statements = await _capture_get(client, _db_connection, f"/files/{query.id}")

    scan = _candidate_scans(statements)[0].lower()
    assert "set_profile.glyph" not in scan
    for scored_column in ("set_profile.file_id", "set_profile.mean_vector", "set_profile.arc", "set_profile.camelot_modal"):
        assert scored_column in scan, f"the scan must still carry {scored_column}"
    for unscored_column in ("set_profile.harmonic_discipline", "set_profile.peak_sec", "set_profile.projection_version"):
        assert unscored_column not in scan, f"the scan must not carry {unscored_column}"


@pytest.mark.asyncio
async def test_the_winners_glyphs_load_in_one_query_bounded_by_top_n(
    client: AsyncClient, session: AsyncSession, _db_connection: AsyncConnection
) -> None:
    """With many more candidates than the page shows, the glyphs still cost ONE query, and that
    query names ``TOP_N`` files -- not one query per winner, and not the corpus."""
    candidates = TOP_N * 4
    query = await _seed_query_file_and_neighbours(session, count=candidates)

    response, statements = await _capture_get(client, _db_connection, f"/files/{query.id}")

    scans = _candidate_scans(statements)
    hydrations = [statement for statement in _set_profile_selects(statements) if statement not in scans and "in (" in statement.lower()]
    assert len(hydrations) == 1, f"expected one IN(...) hydration for the winners, saw: {hydrations}"
    # The glyph the sidebar actually renders came from that one query: TOP_N neighbours, each
    # with the two-cell glyph seeded above.
    body = response.text
    assert body.count("<li data-similar-set>") == TOP_N
    slot_start = body.index("data-similar-sets")
    slot_end = body.index("</ul>", slot_start)
    assert body[slot_start:slot_end].count("<rect") == TOP_N * len(_GLYPH)
