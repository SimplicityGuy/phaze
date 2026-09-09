"""phaze-x1qr3.9: the set glyph in the Files table's File column cell.

``get_files_page`` eager-loads ``FileRecord.set_profile`` (a ``selectinload``, never a per-row
query -- see ``services/pipeline/files.py``); ``files_table_view.html`` renders the glyph inline
in the File cell -- NOT as a new column -- so a file without a ``SetProfile`` row renders that
``<td>`` byte-identical to before this bead (no placeholder), and the row's column count and
overall shape are unaffected either way. A file WITH one gets the glyph inline after the path.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import event

from phaze.models.file import FileRecord
from phaze.models.set_profile import SetProfile


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession


pytestmark = pytest.mark.integration


def _make_file(marker: str) -> FileRecord:
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


def _glyph_cells() -> list[dict[str, int | float | None]]:
    return [{"camelot_number": 8, "energy": 0.2}, {"camelot_number": 9, "energy": 0.8}]


def _row_html(body: str, row_id: str) -> str:
    """The exact ``<tr id="{row_id}" ...>...</tr>`` block for one Files table row."""
    start = body.index(f'<tr id="{row_id}"')
    end = body.index("</tr>", start) + len("</tr>")
    return body[start:end]


@pytest.mark.asyncio
async def test_file_without_a_profile_renders_no_glyph_and_the_same_column_count(client: AsyncClient, session: AsyncSession) -> None:
    plain = _make_file("noglyphfiles")
    session.add(plain)
    await session.commit()

    resp = await client.get("/pipeline/files", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    row = _row_html(resp.text, "files-row-1")

    assert "noglyphfiles-" in row
    assert "data-set-glyph" not in row
    # 9 columns: File, Type, 5 stage cells, Current state, Details -- unchanged by this bead.
    assert len(re.findall(r"<td\b", row)) == 9


@pytest.mark.asyncio
async def test_file_with_a_profile_renders_the_glyph_at_the_same_column_count(client: AsyncClient, session: AsyncSession) -> None:
    with_profile = _make_file("glyphfiles")
    session.add(with_profile)
    await session.commit()
    session.add(SetProfile(file_id=with_profile.id, glyph=_glyph_cells()))
    await session.commit()

    resp = await client.get("/pipeline/files", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    row = _row_html(resp.text, "files-row-1")

    assert "glyphfiles-" in row
    assert "data-set-glyph" in row and "data-set-glyph-empty" not in row
    assert row.count("<rect") == len(_glyph_cells())
    # Same column count as the profile-less row -- the glyph rides in the File cell, not a new column.
    assert len(re.findall(r"<td\b", row)) == 9


@pytest.mark.asyncio
async def test_files_page_loads_profiles_in_one_query_not_per_row(
    client: AsyncClient, session: AsyncSession, _db_connection: AsyncConnection
) -> None:
    """Three rows, each with a distinct ``SetProfile`` -- the eager load stays ONE statement."""
    files = [_make_file(f"nplus1-{i}") for i in range(3)]
    session.add_all(files)
    await session.commit()
    for f in files:
        session.add(SetProfile(file_id=f.id, glyph=_glyph_cells()))
    await session.commit()

    captured: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, parameters: object, context: object, executemany: bool) -> None:
        captured.append(statement)

    sync_conn = _db_connection.sync_connection
    event.listen(sync_conn, "before_cursor_execute", _capture)
    try:
        resp = await client.get("/pipeline/files", headers={"HX-Request": "true"})
    finally:
        event.remove(sync_conn, "before_cursor_execute", _capture)

    assert resp.status_code == 200
    assert resp.text.count("data-set-glyph role=") == len(files)

    set_profile_selects = [s for s in captured if "set_profile" in s.lower() and "select" in s.lower()]
    assert len(set_profile_selects) == 1, f"expected exactly one set_profile SELECT for the whole page, saw: {set_profile_selects}"
