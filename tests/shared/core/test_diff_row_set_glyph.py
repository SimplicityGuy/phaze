"""phaze-x1qr3.9: the set glyph on the Changes Review row (`_diff_row.html`), both facets.

Mirrors ``test_diff_row_tooltip_contract.py``'s real-consumer idiom (ADR-0012 rule 3): every
assertion reads the RENDERED HTML the browser would actually receive, never the Python row dict
``services/review.py`` builds. A file WITH a ``SetProfile`` row renders the glyph (the ONE shared
``ui/primitives.html`` ``set_glyph`` macro every glyph surface uses, at the row-scale 10px
``h-2.5`` height); a file WITHOUT one renders no placeholder at all -- ``data-set-glyph`` /
``data-set-glyph-empty`` are both absent, not merely one of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event

from phaze.models.set_profile import SetProfile


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

    from phaze.models.file import FileRecord
    from phaze.models.proposal import RenameProposal


def _glyph_cells() -> list[dict[str, int | float | None]]:
    return [{"camelot_number": 8, "energy": 0.2}, {"camelot_number": 9, "energy": 0.8}]


# Rename facet (`rename-row`)


@pytest.mark.asyncio
async def test_changes_review_rename_row_renders_the_glyph_when_a_profile_exists(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    proposal = await seed_pending_proposal(0.95, original_filename="glyph-rename.mp3")
    session.add(SetProfile(file_id=proposal.file_id, glyph=_glyph_cells()))
    await session.commit()
    # `seed_pending_proposal` manually assigns `proposal.file = file` (a fixture convenience, not a
    # real query), which leaves that identity-mapped `file` looking already-loaded under this
    # `expire_on_commit=False` session -- SQLAlchemy then skips re-querying `RenameProposal.file`
    # for the request below and never cascades the nested `set_profile` selectinload onto it. Expire
    # everything so the request's own query (the real, production code path) does the loading.
    session.expire_all()

    body = (await client.get("/s/rename?status=all")).text

    assert f'id="rename-row-{proposal.id}"' in body
    assert "data-set-glyph" in body and "data-set-glyph-empty" not in body
    assert body.count("<rect") == len(_glyph_cells())


@pytest.mark.asyncio
async def test_changes_review_rename_row_renders_no_glyph_without_a_profile(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """No ``SetProfile`` row at all -- an empty cell, not the macro's own "No coarse windows" text.

    ``_diff_row.html`` never calls ``set_glyph`` for this row (the ``profile is defined and
    profile`` guard is falsy), so neither ``data-set-glyph`` NOR ``data-set-glyph-empty`` appears --
    unlike the record page, which always calls the macro and gets the placeholder text.
    """
    proposal = await seed_pending_proposal(0.95, original_filename="no-glyph-rename.mp3")

    body = (await client.get("/s/rename?status=all")).text

    assert f'id="rename-row-{proposal.id}"' in body
    assert "data-set-glyph" not in body
    assert "No coarse windows" not in body


# Tag-write facet (`tagwrite-row`)


@pytest.mark.asyncio
async def test_changes_review_tagwrite_row_renders_the_glyph_when_a_profile_exists(
    client: AsyncClient,
    session: AsyncSession,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, object]]],
) -> None:
    file, _md = await seed_executed_file_with_metadata(
        original_filename="Glyph Artist - Glyph Title.mp3", artist=None, title=None, album="Keep Album"
    )
    session.add(SetProfile(file_id=file.id, glyph=_glyph_cells()))
    await session.commit()
    session.expire_all()

    body = (await client.get("/s/rename?status=all")).text

    assert f'id="tagwrite-row-{file.id}"' in body
    assert "data-set-glyph" in body and "data-set-glyph-empty" not in body


@pytest.mark.asyncio
async def test_changes_review_tagwrite_row_renders_no_glyph_without_a_profile(
    client: AsyncClient,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, object]]],
) -> None:
    file, _md = await seed_executed_file_with_metadata(
        original_filename="No Glyph Artist - No Glyph Title.mp3", artist=None, title=None, album="Keep Album"
    )

    body = (await client.get("/s/rename?status=all")).text

    assert f'id="tagwrite-row-{file.id}"' in body
    assert "data-set-glyph" not in body


# No N+1: one `set_profile` SELECT for the whole page, never one per row.


@pytest.mark.asyncio
async def test_changes_review_rename_rows_load_profiles_in_one_query_not_per_row(
    client: AsyncClient,
    session: AsyncSession,
    _db_connection: AsyncConnection,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Three rows, each with a distinct ``SetProfile`` -- the eager load stays ONE statement."""
    proposals = [await seed_pending_proposal(0.95, original_filename=f"glyph-n1-{i}.mp3") for i in range(3)]
    for p in proposals:
        session.add(SetProfile(file_id=p.file_id, glyph=_glyph_cells()))
    await session.commit()
    session.expire_all()

    captured: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, parameters: object, context: object, executemany: bool) -> None:
        captured.append(statement)

    sync_conn = _db_connection.sync_connection
    event.listen(sync_conn, "before_cursor_execute", _capture)
    try:
        response = await client.get("/s/rename?status=all")
    finally:
        event.remove(sync_conn, "before_cursor_execute", _capture)

    assert response.status_code == 200
    assert "data-set-glyph" in response.text

    set_profile_selects = [s for s in captured if "set_profile" in s.lower() and "select" in s.lower()]
    assert len(set_profile_selects) == 1, f"expected exactly one set_profile SELECT for the whole page, saw: {set_profile_selects}"
