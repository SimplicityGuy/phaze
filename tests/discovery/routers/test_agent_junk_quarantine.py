"""Authenticated contract of PATCH /api/internal/agent/junk-quarantine/{review_id} (phaze-lwuf6).

What a report changes is covered against Postgres in ``tests/discovery/services/test_junk_quarantine.py``;
this module pins the HTTP edge: ownership from the token, the body contract, idempotency.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from typing import TYPE_CHECKING
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import select

from phaze.models.agent import Agent
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_ROUTE = "/api/internal/agent/junk-quarantine"
_SHA = hashlib.sha256(b"junk").hexdigest()


async def _executing(session: AsyncSession, agent_id: str, path: str = "/test/music/rel/site.nfo") -> CompanionJunkReview:
    row = CompanionJunkReview(
        agent_id=agent_id,
        original_path=path,
        sha256_hash=_SHA,
        file_type="nfo",
        file_size=4,
        reason="known_stamp",
        content_group=_SHA,
        status="executing",
        decided_at=datetime.now(UTC),
    )
    session.add(row)
    session.add(
        FileRecord(
            agent_id=agent_id, sha256_hash=_SHA, original_path=path, original_filename="site.nfo", current_path=path, file_type="nfo", file_size=4
        )
    )
    await session.commit()
    return row


async def test_a_confirmed_move_is_recorded_once(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    agent, _token = seed_test_agent
    review = await _executing(session, agent.id)
    body = {"status": "quarantined", "destination_path": "/test/music/.phaze-quarantine/rel/site.nfo"}

    first = await authenticated_client.patch(f"{_ROUTE}/{review.id}", json=body)
    second = await authenticated_client.patch(f"{_ROUTE}/{review.id}", json={**body, "replayed": True})

    assert first.status_code == 200, first.text
    assert first.json() == {"review_id": str(review.id), "status": "quarantined", "applied": True, "retired_files": 1}
    assert second.json() == {"review_id": str(review.id), "status": "quarantined", "applied": False, "retired_files": 0}
    assert (await session.execute(select(FileRecord.id))).all() == []
    assert (await session.execute(select(CompanionJunkReview.status))).scalars().all() == ["quarantined"]


async def test_a_failure_is_recorded_with_its_reason(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    agent, _token = seed_test_agent
    review = await _executing(session, agent.id)

    response = await authenticated_client.patch(f"{_ROUTE}/{review.id}", json={"status": "failed", "error_message": "already exists"})

    assert response.json() == {"review_id": str(review.id), "status": "failed", "applied": True, "retired_files": 0}
    row = (await session.execute(select(CompanionJunkReview).execution_options(populate_existing=True))).scalar_one()
    assert (row.status, row.error_message) == ("failed", "already exists")


async def test_another_agents_row_and_an_unknown_row_are_both_404(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """The token owns the row or nothing: a foreign row reads exactly like a missing one."""
    session.add(Agent(id="someone-else", name="Someone Else", token_hash="c" * 64, scan_roots=["/test/music"]))
    await session.flush()
    foreign = await _executing(session, "someone-else")
    body = {"status": "failed", "error_message": "x"}

    foreign_response = await authenticated_client.patch(f"{_ROUTE}/{foreign.id}", json=body)
    unknown_response = await authenticated_client.patch(f"{_ROUTE}/{uuid.uuid4()}", json=body)

    assert (foreign_response.status_code, unknown_response.status_code) == (404, 404)
    assert foreign_response.json() == unknown_response.json()
    assert (await session.execute(select(CompanionJunkReview.status))).scalars().all() == ["executing"]


@pytest.mark.parametrize(
    "body",
    [
        {"status": "quarantined"},  # a success names its destination
        {"status": "quarantined", "destination_path": "/d", "error_message": "x"},
        {"status": "failed"},  # a failure says why
        {"status": "failed", "error_message": "x", "destination_path": "/d"},
        {"status": "failed", "error_message": "x", "replayed": True},
        {"status": "executing", "destination_path": "/d"},  # only terminal outcomes
        {"status": "failed", "error_message": "x" * 2001},
        {"status": "failed", "error_message": "x", "agent_id": "forged"},  # extra="forbid"
    ],
)
async def test_a_malformed_report_is_rejected(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession, body: dict[str, object]
) -> None:
    agent, _token = seed_test_agent
    review = await _executing(session, agent.id)

    response = await authenticated_client.patch(f"{_ROUTE}/{review.id}", json=body)

    assert response.status_code == 422, response.text
    assert (await session.execute(select(CompanionJunkReview.status))).scalars().all() == ["executing"]


async def test_a_missing_token_is_refused(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    agent, _token = seed_test_agent
    review = await _executing(session, agent.id)

    async with AsyncClient(transport=authenticated_client._transport, base_url="http://test") as anonymous:
        response = await anonymous.patch(f"{_ROUTE}/{review.id}", json={"status": "failed", "error_message": "x"})

    assert response.status_code == 401
    assert (await session.execute(select(CompanionJunkReview.status))).scalars().all() == ["executing"]
