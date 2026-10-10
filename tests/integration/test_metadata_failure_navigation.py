"""Failure inspection uses the dashboard's media population, independently of extraction eligibility."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

from bs4 import BeautifulSoup
import pytest

from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.pipeline import get_metadata_selection_summary, get_metadata_status_snapshot


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _file(name: str, *, missing: bool = False) -> FileRecord:
    file_id = uuid.uuid4()
    return FileRecord(
        id=file_id,
        agent_id="test-fileserver",
        sha256_hash=file_id.hex,
        original_path=f"/synthetic/{name}",
        original_filename=name,
        current_path=f"/synthetic/{name}",
        file_type=name.rsplit(".", 1)[1],
        file_size=1000,
        missing_at=datetime.now(UTC) if missing else None,
    )


@pytest.mark.asyncio
async def test_failure_attention_reaches_resolved_duplicate_with_no_eligible_files(client: AsyncClient, session: AsyncSession) -> None:
    failed, keeper = _file("resolved-failure.mp3"), _file("successful-keeper.mp3")
    session.add_all([failed, keeper])
    await session.flush()
    session.add_all(
        [
            FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="Synthetic extraction failure"),
            FileMetadata(file_id=keeper.id, title="Keeper"),
            DedupResolution(file_id=failed.id, canonical_file_id=keeper.id),
        ]
    )
    await session.commit()
    assert (await get_metadata_status_snapshot(session)).failed == 1
    assert (await get_metadata_selection_summary(session)).eligible_count == 0

    summary = await client.get("/s/summary")
    assert summary.status_code == 200
    summary_link = BeautifulSoup(summary.text, "html.parser").find("a", string="Inspect failed files")
    assert summary_link is not None
    href = str(summary_link["href"])
    assert href == "/s/files?stage=metadata&bucket=failed&scope=media"
    metadata = await client.get("/s/metadata")
    metadata_link = BeautifulSoup(metadata.text, "html.parser").find("a", string="Inspect failed files")
    assert metadata_link is not None and metadata_link["href"] == href
    assert "Resolved duplicates" in metadata.text
    inspected = await client.get(href)
    assert inspected.status_code == 200 and "resolved-failure.mp3" in inspected.text
    assert "successful-keeper.mp3" not in inspected.text
    assert f"/record/{failed.id}" in inspected.text
    assert "Music and video" in inspected.text


@pytest.mark.asyncio
async def test_media_failure_lens_matches_counts_and_preserves_general_history(client: AsyncClient, session: AsyncSession) -> None:
    failed = _file("normal-failure.mp3")
    resolved = _file("resolved-failure.mp4")
    missing = _file("missing-failure.mp3", missing=True)
    companion = _file("companion-failure.nfo")
    done = _file("successful-keeper.mp3")
    session.add_all([failed, resolved, missing, companion, done])
    await session.flush()
    session.add_all([FileMetadata(file_id=file.id, failed_at=datetime.now(UTC)) for file in (failed, resolved, missing, companion)])
    session.add_all([FileMetadata(file_id=done.id, title="Keeper"), DedupResolution(file_id=resolved.id, canonical_file_id=done.id)])
    await session.commit()
    snapshot = await get_metadata_status_snapshot(session)
    assert (snapshot.failed, snapshot.missing) == (2, 1)
    assert (await get_metadata_selection_summary(session)).eligible_count == 1

    href = "/s/files?stage=metadata&bucket=failed&scope=media"
    for endpoint, headers in (
        (href, {}),
        (href, {"HX-Request": "true", "HX-Target": "files-table-view"}),
        (href.replace("/s/files", "/pipeline/files"), {"HX-Request": "true"}),
    ):
        response = await client.get(endpoint, headers=headers)
        assert response.status_code == 200
        assert "normal-failure.mp3" in response.text and "resolved-failure.mp4" in response.text
        assert all(name not in response.text for name in ("missing-failure.mp3", "companion-failure.nfo", "successful-keeper.mp3"))
        soup = BeautifulSoup(response.text, "html.parser")
        assert soup.select('input[name="scope"][value="media"]')
        sort_links = soup.select('th [hx-get*="sort="]')
        assert sort_links and all("scope=media" in str(link["hx-get"]) for link in sort_links)
    history = await client.get("/s/files?stage=metadata&bucket=failed")
    assert "companion-failure.nfo" in history.text and "missing-failure.mp3" not in history.text
    general = await client.get("/s/files")
    assert "missing-failure.mp3" in general.text and "companion-failure.nfo" in general.text


@pytest.mark.asyncio
async def test_media_scope_survives_paging_sort_and_filter_navigation(client: AsyncClient, session: AsyncSession) -> None:
    files = [_file(f"failed-{index:02}.mp3") for index in range(12)]
    session.add_all(files)
    await session.flush()
    session.add_all([FileMetadata(file_id=file.id, failed_at=datetime.now(UTC)) for file in files])
    await session.commit()
    response = await client.get("/s/files?stage=metadata&bucket=failed&scope=media&page_size=10&sort=file&order=desc")
    soup = BeautifulSoup(response.text, "html.parser")
    next_link = soup.find(attrs={"hx-get": lambda value: value and "page=2" in value})
    assert next_link is not None
    href = str(next_link["hx-get"])
    assert "scope=media" in href and "sort=file" in href and "order=desc" in href
    next_page = await client.get(href, headers={"HX-Request": "true", "HX-Target": "files-table-view"})
    assert next_page.status_code == 200 and "failed-00.mp3" in next_page.text and "failed-01.mp3" in next_page.text
    assert "failed-11.mp3" not in next_page.text
    assert BeautifulSoup(next_page.text, "html.parser").select('input[name="scope"][value="media"]')


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/s/files", "/pipeline/files"])
async def test_unknown_scope_cannot_silently_widen_failure_attention(client: AsyncClient, endpoint: str) -> None:
    response = await client.get(f"{endpoint}?stage=metadata&bucket=failed&scope=unknown", headers={"HX-Request": "true"})
    assert response.status_code == 422
