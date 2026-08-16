"""Responsive and accessible interaction contracts for the Files workspace."""

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


@pytest.mark.asyncio
async def test_canonical_files_route_serves_live_fragment_and_restorable_filtered_shell(client: AsyncClient, session: AsyncSession) -> None:
    failed = _make_file("/music/failed.mp3")
    healthy = _make_file("/music/healthy.mp3")
    session.add_all([failed, healthy])
    await session.commit()
    session.add(FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="boom"))
    await session.commit()
    url = "/s/files?stage=metadata&bucket=failed&sort=file&order=desc&page_size=10"

    fragment = await client.get(url, headers={"HX-Request": "true", "HX-Target": "files-table-view"})
    assert fragment.status_code == 200
    assert "<html" not in fragment.text.lower()
    assert "/music/failed.mp3" in fragment.text
    assert "/music/healthy.mp3" not in fragment.text
    assert 'hx-get="/s/files' in fragment.text

    restored = await client.get(url, headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert restored.status_code == 200
    assert "<html" in restored.text.lower()
    assert 'aria-label="Pipeline navigation"' in restored.text
    assert "/music/failed.mp3" in restored.text
    assert "/music/healthy.mp3" not in restored.text
    assert '<option value="metadata" selected>' in restored.text
    assert '<option value="failed" selected>' in restored.text


@pytest.mark.asyncio
async def test_canonical_files_route_restores_sorted_page(client: AsyncClient, session: AsyncSession) -> None:
    for index in range(12):
        session.add(_make_file(f"/music/file-{index:02d}.mp3"))
    await session.commit()
    url = "/s/files?page=2&page_size=10&sort=file&order=asc"

    fragment = await client.get(url, headers={"HX-Request": "true", "HX-Target": "files-table-view"})
    restored = await client.get(url, headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})

    for body in (fragment.text, restored.text):
        assert "/music/file-10.mp3" in body
        assert "/music/file-11.mp3" in body
        assert "/music/file-00.mp3" not in body
        assert "Page 2" in body
    assert "<html" not in fragment.text.lower()
    assert "<html" in restored.text.lower()
