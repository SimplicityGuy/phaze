"""Unshimmed public comparison-to-resolution workflow coverage."""

import html
import re
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.dedup_resolution import DedupResolution
from phaze.models.dedup_review_plan import DedupReviewPlan
from phaze.models.file import FileRecord


GROUP_HASH = "c" * 64
_PLAN_RE = re.compile(r'name="plan_id" value="([^"]+)"')
_STATES_RE = re.compile(r'name="file_states"\s+value="([^"]*)"')


def _file(path: str, *, size: int) -> FileRecord:
    return FileRecord(
        id=uuid.uuid4(),
        agent_id="test-fileserver",
        sha256_hash=GROUP_HASH,
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type="flac",
        file_size=size,
    )


@pytest.mark.asyncio
async def test_public_comparison_reviews_confirms_resolves_and_undoes_without_client_shim(
    session: AsyncSession,
    client: AsyncClient,
) -> None:
    """GET compare -> POST review -> POST opaque plan -> server undo, with no direct-write adapter."""
    keeper = _file("/synthetic/keeper.flac", size=2_000)
    duplicate = _file("/synthetic/duplicate.flac", size=1_000)
    session.add_all([keeper, duplicate])
    await session.flush()

    comparison = await client.get(f"/duplicates/{GROUP_HASH}/compare")
    assert comparison.status_code == 200
    assert f'hx-post="/duplicates/{GROUP_HASH}/review"' in comparison.text
    assert f'hx-target="#comparison-group-{GROUP_HASH}"' in comparison.text
    assert f'hx-post="/duplicates/{GROUP_HASH}/resolve"' not in comparison.text
    assert "Review decision" in comparison.text

    reviewed = await client.post(f"/duplicates/{GROUP_HASH}/review", data={"canonical_id": str(keeper.id)})
    assert reviewed.status_code == 200
    plan_match = _PLAN_RE.search(reviewed.text)
    assert plan_match is not None
    plan_id = uuid.UUID(plan_match.group(1))
    plan = await session.get(DedupReviewPlan, plan_id)
    assert plan is not None
    assert plan.canonical_file_id == keeper.id
    assert set(plan.member_ids) == {str(keeper.id), str(duplicate.id)}
    assert plan.committed_at is None
    assert list((await session.execute(select(DedupResolution))).scalars()) == []

    resolved = await client.post(f"/duplicates/{GROUP_HASH}/resolve", data={"plan_id": str(plan_id)})
    assert resolved.status_code == 200
    markers = list((await session.execute(select(DedupResolution))).scalars())
    assert len(markers) == 1
    assert markers[0].file_id == duplicate.id
    assert markers[0].canonical_file_id == keeper.id
    await session.refresh(plan)
    assert plan.committed_at is not None

    states_match = _STATES_RE.search(resolved.text)
    assert states_match is not None
    undone = await client.post(
        f"/duplicates/{GROUP_HASH}/undo",
        data={"file_states": html.unescape(states_match.group(1))},
    )
    assert undone.status_code == 200
    assert list((await session.execute(select(DedupResolution))).scalars()) == []
