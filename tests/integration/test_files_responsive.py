"""Responsive and accessible interaction contracts for the Files workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _make_file(path: str = "/music/example.mp3") -> FileRecord:
    file_id = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=file_id.hex,
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type="mp3",
        file_size=1000,
    )


@pytest.mark.asyncio
async def test_rows_use_explicit_details_controls_instead_of_row_button_semantics(client: AsyncClient, session: AsyncSession) -> None:
    file = _make_file()
    session.add(file)
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    row_start = body[body.index('<tr id="files-row-1"') : body.index(">", body.index('<tr id="files-row-1"'))]

    assert "hx-get" not in row_start
    assert "tabindex" not in row_start
    assert "cursor-pointer" not in row_start
    assert body.count(f'hx-get="/record/{file.id}"') == 2
    assert body.count('aria-label="Open details for example.mp3"') == 2
    assert "@click=\"$dispatch('record:open', { el: $el })\"" in body


@pytest.mark.asyncio
async def test_compact_layout_uses_cards_and_progressive_stage_disclosure(client: AsyncClient, session: AsyncSession) -> None:
    session.add(_make_file())
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text

    assert 'class="hidden md:block"' in body
    assert 'class="grid gap-3 px-4 py-3 md:hidden"' in body
    assert 'class="min-w-0 rounded-lg border' in body
    assert "Stage details</summary>" in body
    assert "hidden whitespace-nowrap px-3 py-2 xl:table-cell" in body
    for label in ("Metadata", "Analyze", "Propose", "Review", "Execute"):
        assert f">{label}<" in body
    for abbreviation in (">Meta<", ">Prop<", ">Appr<", ">Exec<"):
        assert abbreviation not in body


@pytest.mark.asyncio
async def test_current_actionable_state_is_visible_in_table_and_card(client: AsyncClient, session: AsyncSession) -> None:
    session.add(_make_file())
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text

    assert body.count("Current state") >= 3
    assert body.count('aria-label="Metadata: not started"') >= 2


@pytest.mark.asyncio
async def test_compact_sort_controls_preserve_filters_and_push_history(client: AsyncClient, session: AsyncSession) -> None:
    session.add(_make_file())
    await session.commit()

    body = (
        await client.get(
            "/pipeline/files?stage=metadata&bucket=not_started&sort=review&order=desc&page_size=10",
            headers={"HX-Request": "true"},
        )
    ).text
    form = body[body.index('aria-label="Sort files"') : body.index("</form>", body.index('aria-label="Sort files"'))]

    assert 'hx-target="#files-table-view"' in form
    assert 'hx-push-url="true"' in form
    assert 'name="stage" value="metadata"' in form
    assert 'name="bucket" value="not_started"' in form
    assert '<option value="review" selected>Review</option>' in form
    assert '<option value="desc" selected>Descending</option>' in form
