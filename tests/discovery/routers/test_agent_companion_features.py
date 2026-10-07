"""Authenticated contract of POST /api/internal/agent/companion-features (phaze-osy6j).

Storage semantics (stamp verdicts, cross-agent isolation, re-reports) are covered against Postgres in
``tests/discovery/services/test_companion_content.py`` and end to end in
``tests/discovery/test_companion_features_producers.py``; this module pins the HTTP edge.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from typing import TYPE_CHECKING, Any

from httpx import AsyncClient
import pytest

from phaze.models.file import FileRecord


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_ROUTE = "/api/internal/agent/companion-features"
_CONTENT = b'FILE "set.mp3" MP3\r\n'


def _record(path: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "original_path": path,
        "fingerprint": hashlib.sha256(_CONTENT).hexdigest(),
        "encoding": "ascii",
        "byte_size": len(_CONTENT),
        "truncated": False,
        "media_references": [{"name": "set.mp3", "source": "cue_file"}],
        "reference_count": 1,
        "is_tracklist": False,
        "content_junk_class": None,
        "folder_media": ["set.mp3"],
        "folder_media_count": 1,
        "extractor_version": 1,
    }
    record.update(overrides)
    return record


async def _companion(session: AsyncSession, agent_id: str, path: str) -> FileRecord:
    row = FileRecord(
        agent_id=agent_id,
        sha256_hash=hashlib.sha256(_CONTENT).hexdigest(),
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type="cue",
        file_size=len(_CONTENT),
    )
    session.add(row)
    await session.commit()
    return row


async def test_the_owner_stores_and_a_replay_rewrites_the_same_row(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    agent, _token = seed_test_agent
    await _companion(session, agent.id, "/test/music/rel/set.cue")
    body = {"features": [_record("/test/music/rel/set.cue"), _record("/test/music/rel/unknown.cue")]}

    first = await authenticated_client.post(_ROUTE, json=body)
    second = await authenticated_client.post(_ROUTE, json=body)

    assert first.status_code == 200, first.text
    assert first.json() == {"agent_id": agent.id, "stored": 1, "unknown": 1}
    assert second.json() == first.json()


async def test_missing_and_revoked_tokens_are_refused(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    agent, _token = seed_test_agent
    body = {"features": [_record("/test/music/rel/set.cue")]}
    async with AsyncClient(transport=authenticated_client._transport, base_url="http://test") as anonymous:
        missing = await anonymous.post(_ROUTE, json=body)
    agent.revoked_at = datetime.now(UTC)
    await session.commit()
    revoked = await authenticated_client.post(_ROUTE, json=body)

    assert missing.status_code == 401
    assert revoked.status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"fingerprint": "NOT-A-DIGEST" * 5 + "0000"},
        {"encoding": "cp1251"},
        {"content_junk_class": "known_stamp"},  # control-side only: an agent can never claim it
        {"agent_id": "someone-else"},  # AUTH-01: the agent comes from the token, never the body
        {"extractor_version": 0},
        {"media_references": [{"name": "x.mp3", "source": "guess"}]},
    ],
)
async def test_malformed_records_are_rejected_at_the_edge(authenticated_client: AsyncClient, overrides: dict[str, Any]) -> None:
    response = await authenticated_client.post(_ROUTE, json={"features": [_record("/test/music/rel/set.cue", **overrides)]})

    assert response.status_code == 422, response.text


async def test_an_empty_chunk_is_rejected(authenticated_client: AsyncClient) -> None:
    assert (await authenticated_client.post(_ROUTE, json={"features": []})).status_code == 422
