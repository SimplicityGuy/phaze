"""Real HTTP refusal contracts with committed, independent-connection reads.

These controls prove database/response behavior, not physical archive deletion.
"""

from collections.abc import AsyncIterator
import re
from unittest.mock import AsyncMock
from urllib.parse import quote
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.dedup_review_plan import DedupReviewPlan
from phaze.models.file import FileRecord
from phaze.routers import duplicates


@pytest_asyncio.fixture
async def committed_duplicates(async_engine: AsyncEngine) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession], list[FileRecord]]]:
    """Own committed synthetic rows; route sessions and observers use different connections."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    files = []
    for group in range(2):
        group_hash = uuid.uuid4().hex * 2
        for member in range(2):
            path = f"/synthetic/{group}/{member}.mp3"
            files.append(
                FileRecord(
                    id=uuid.uuid4(),
                    agent_id="test-fileserver",
                    sha256_hash=group_hash,
                    original_path=path,
                    original_filename=f"{member}.mp3",
                    current_path=path,
                    file_type="mp3",
                    file_size=1000,
                )
            )
    async with factory() as seed:
        seed.add_all(files)
        await seed.commit()

    async def route_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = route_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory, files
    finally:
        async with factory() as cleanup:
            ids = [file.id for file in files]
            await cleanup.execute(delete(DedupResolution).where(DedupResolution.file_id.in_(ids)))
            await cleanup.execute(delete(DedupReviewPlan).where(DedupReviewPlan.group_hash.in_([file.sha256_hash for file in files])))
            await cleanup.execute(delete(FileRecord).where(FileRecord.id.in_(ids)))
            await cleanup.commit()


async def _snapshot(factory: async_sessionmaker[AsyncSession], files: list[FileRecord]) -> dict[str, list[dict]]:
    """Fresh identity map AND connection; include exact durable audit/timestamp values."""
    async with factory() as observer:
        return {
            "files": [
                dict(row)
                for row in (
                    await observer.execute(select(FileRecord.__table__).where(FileRecord.id.in_([file.id for file in files])).order_by(FileRecord.id))
                ).mappings()
            ],
            "plans": [
                dict(row)
                for row in (
                    await observer.execute(
                        select(DedupReviewPlan.__table__)
                        .where(DedupReviewPlan.group_hash.in_([file.sha256_hash for file in files]))
                        .order_by(DedupReviewPlan.id)
                    )
                ).mappings()
            ],
            "markers": [
                dict(row)
                for row in (
                    await observer.execute(
                        select(DedupResolution.__table__).where(DedupResolution.file_id.in_([file.id for file in files])).order_by(DedupResolution.id)
                    )
                ).mappings()
            ],
        }


async def _review(client: AsyncClient, keeper: FileRecord) -> str:
    response = await client.post(f"/duplicates/{keeper.sha256_hash}/review", data={"canonical_id": str(keeper.id)})
    assert response.status_code == 200
    match = re.search(r'name="plan_id"\s+value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _assert_no_success(text: str) -> None:
    assert "Group resolved" not in text
    assert 'name="file_states"' not in text
    assert "/undo" not in text


@pytest.mark.asyncio
async def test_invalid_hash_refuses_valid_plan_without_database_changes(committed_duplicates) -> None:
    client, factory, files = committed_duplicates
    plan_id = await _review(client, files[0])
    before = await _snapshot(factory, files)
    response = await client.post(f"/duplicates/{quote('bad' + chr(0), safe='')}/resolve", data={"plan_id": plan_id})
    assert response.status_code == 200
    assert "Nothing to resolve" in response.text
    _assert_no_success(response.text)
    assert await _snapshot(factory, files) == before
    valid = await client.post(f"/duplicates/{files[0].sha256_hash}/resolve", data={"plan_id": plan_id})
    assert "Group resolved" in valid.text
    assert len((await _snapshot(factory, files))["markers"]) == 1


@pytest.mark.asyncio
async def test_substituted_zero_commit_outcome_is_honest_and_recoverable(committed_duplicates, monkeypatch: pytest.MonkeyPatch) -> None:
    """Substituted route-seam outcome; existing real CAS/Undo tests remain authoritative."""
    client, factory, files = committed_duplicates
    plan_id = await _review(client, files[0])
    before = await _snapshot(factory, files)
    with monkeypatch.context() as patch:
        outcome = AsyncMock(return_value=(0, [], []))
        patch.setattr(duplicates, "commit_review_plans", outcome)
        response = await client.post(f"/duplicates/{files[0].sha256_hash}/resolve", data={"plan_id": plan_id})
        outcome.assert_awaited_once()
    assert response.status_code == 200
    assert "Nothing to resolve" in response.text
    _assert_no_success(response.text)
    assert await _snapshot(factory, files) == before
    valid = await client.post(f"/duplicates/{files[0].sha256_hash}/resolve", data={"plan_id": plan_id})
    assert "Group resolved" in valid.text
    assert len((await _snapshot(factory, files))["markers"]) == 1


@pytest.mark.asyncio
async def test_bulk_stale_plan_rolls_back_earlier_group_and_allows_valid_request(committed_duplicates) -> None:
    client, factory, files = committed_duplicates
    keepers = sorted([files[0], files[2]], key=lambda file: file.sha256_hash)
    plans = [await _review(client, keeper) for keeper in keepers]
    # Corrupt only the last plan's reviewed membership: first group really stages a resolution.
    async with factory() as change:
        stale = await change.get(DedupReviewPlan, uuid.UUID(plans[1]))
        assert stale is not None
        stale.member_ids = [str(keepers[1].id)]
        await change.commit()
    before = await _snapshot(factory, files)
    response = await client.post("/duplicates/resolve-all", data={"plan_ids": plans})
    assert response.status_code == 200
    assert "Review expired" in response.text
    _assert_no_success(response.text)
    assert await _snapshot(factory, files) == before
    valid = await client.post(f"/duplicates/{keepers[0].sha256_hash}/resolve", data={"plan_id": plans[0]})
    assert valid.status_code == 200
    assert "Group resolved" in valid.text
    after = await _snapshot(factory, files)
    assert len(after["markers"]) == 1
    assert sum(plan["committed_at"] is not None for plan in after["plans"]) == 1
