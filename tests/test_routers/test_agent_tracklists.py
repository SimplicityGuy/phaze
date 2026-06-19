"""Integration tests for POST /api/internal/agent/tracklists (Phase 26 D-27)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
import redis.asyncio as redis_async
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.file import FileRecord, FileState
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.routers import agent_tracklists
from phaze.services.scheduling_ledger import upsert_ledger_entry


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6379/0")


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[redis_async.Redis]:
    """Real Redis with decode_responses=True (matches phaze.redis_client.py convention).

    Cleans up `tracklist_req:*` and `tracklist_resp:*` keys after each test so
    reruns do not collide. Uses scan_iter rather than KEYS for memory safety.
    """
    client: redis_async.Redis = redis_async.Redis.from_url(_REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        keys_req = [k async for k in client.scan_iter(match="tracklist_req:*", count=100)]
        keys_resp = [k async for k in client.scan_iter(match="tracklist_resp:*", count=100)]
        if keys_req:
            await client.delete(*keys_req)
        if keys_resp:
            await client.delete(*keys_resp)
        await client.aclose()


def _make_smoke_app(session: AsyncSession, redis_client: redis_async.Redis) -> FastAPI:
    """Build a small FastAPI app wiring the agent_tracklists router + a Redis client."""
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_tracklists.router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.redis = redis_client
    return app


def _make_client(
    session: AsyncSession,
    redis_client: redis_async.Redis,
    token: str | None = None,
) -> AsyncClient:
    app = _make_smoke_app(session, redis_client)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    file_id = uuid.uuid4()
    file_record = FileRecord(
        id=file_id,
        sha256_hash="0" * 64,
        original_path=f"/test/{file_id}.mp3",
        original_filename=f"{file_id}.mp3",
        current_path=f"/test/{file_id}.mp3",
        file_type="mp3",
        file_size=10_000_000,
        state=FileState.DISCOVERED,
        agent_id=agent_id,
    )
    session.add(file_record)
    await session.commit()
    return file_id


async def _seed_ledger(session: AsyncSession, file_id: uuid.UUID) -> str:
    key = f"scan_live_set:{file_id}"
    await upsert_ledger_entry(session, key=key, function="scan_live_set", kwargs={"file_id": str(file_id)})
    await session.commit()
    return key


async def _ledger_present(session: AsyncSession, key: str) -> bool:
    session.expire_all()
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none()
    return row is not None


@pytest.mark.integration
async def test_tracklist_create_happy_path(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """First POST creates Tracklist + Version + N Tracks; returns 200 with counts."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    request_id = uuid.uuid4()
    payload = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": f"fp-{file_id.hex[:12]}",
        "request_id": str(request_id),
        "tracks": [
            {"position": 1, "artist": "Artist A", "title": "Track 1", "timestamp": "00:00:00"},
            {"position": 2, "artist": "Artist B", "title": "Track 2", "timestamp": "00:05:30"},
        ],
    }
    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post("/api/internal/agent/tracklists", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["track_count"] == 2
    assert body["version"] == 1
    tracklist_id = uuid.UUID(body["tracklist_id"])

    # Re-read DB rows; expire to drop any cached state from prior commits.
    session.expire_all()
    tracklist_row = (await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))).scalar_one()
    assert tracklist_row.external_id == payload["external_id"]
    version_rows = (await session.execute(select(TracklistVersion).where(TracklistVersion.tracklist_id == tracklist_id))).scalars().all()
    assert len(version_rows) == 1
    assert version_rows[0].version_number == 1
    track_rows = (await session.execute(select(TracklistTrack).where(TracklistTrack.version_id == version_rows[0].id))).scalars().all()
    assert len(track_rows) == 2


@pytest.mark.integration
async def test_tracklist_idempotent_replay_returns_cached(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """Same request_id -> cached response, no new rows."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    request_id = uuid.uuid4()
    payload = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": f"fp-replay-{file_id.hex[:8]}",
        "request_id": str(request_id),
        "tracks": [{"position": 1, "artist": "A", "title": "T1"}],
    }
    async with _make_client(session, redis_client, raw_token) as ac:
        r1 = await ac.post("/api/internal/agent/tracklists", json=payload)
        r2 = await ac.post("/api/internal/agent/tracklists", json=payload)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json() == r2.json()  # identical cached response

    # Confirm exactly 1 row of each type in DB (no duplicates from replay).
    session.expire_all()
    tracklists = (await session.execute(select(Tracklist).where(Tracklist.external_id == payload["external_id"]))).scalars().all()
    assert len(tracklists) == 1
    versions = (await session.execute(select(TracklistVersion).where(TracklistVersion.tracklist_id == tracklists[0].id))).scalars().all()
    assert len(versions) == 1
    tracks = (await session.execute(select(TracklistTrack).where(TracklistTrack.version_id == versions[0].id))).scalars().all()
    assert len(tracks) == 1


@pytest.mark.integration
async def test_tracklist_replay_with_new_request_id_creates_new_version(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """Same external_id, different request_id -> new TracklistVersion (version_number=2)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    ext = f"fp-newver-{file_id.hex[:8]}"
    payload_a = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": ext,
        "request_id": str(uuid.uuid4()),
        "tracks": [{"position": 1, "artist": "A", "title": "T1"}],
    }
    payload_b = {
        **payload_a,
        "request_id": str(uuid.uuid4()),
        "tracks": [{"position": 1, "artist": "B", "title": "T2"}],
    }
    async with _make_client(session, redis_client, raw_token) as ac:
        r1 = await ac.post("/api/internal/agent/tracklists", json=payload_a)
        r2 = await ac.post("/api/internal/agent/tracklists", json=payload_b)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    body_a = r1.json()
    body_b = r2.json()
    assert body_a["tracklist_id"] == body_b["tracklist_id"]  # same tracklist row
    assert body_a["version"] == 1
    assert body_b["version"] == 2


@pytest.mark.integration
async def test_tracklist_extra_field_422(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """extra='forbid' rejects unknown fields (AUTH-01 -- no agent_id forgery)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    payload = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": "fp-x",
        "request_id": str(uuid.uuid4()),
        "tracks": [{"position": 1, "artist": "A", "title": "T1"}],
        "agent_id": "spoofed",
    }
    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post("/api/internal/agent/tracklists", json=payload)
    assert r.status_code == 422


@pytest.mark.integration
async def test_tracklist_too_many_tracks_422(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """T-26-07-DoS: schema-level Field(max_length=2000) rejects 2001-track payloads."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    payload = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": f"fp-dos-{file_id.hex[:8]}",
        "request_id": str(uuid.uuid4()),
        "tracks": [{"position": i + 1} for i in range(2001)],  # 2001 > max_length=2000
    }
    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post("/api/internal/agent/tracklists", json=payload)
    assert r.status_code == 422
    body_text = r.text.lower()
    assert "max_length" in body_text or "too_long" in body_text or "2000" in r.text or "list_too_long" in body_text, r.text


@pytest.mark.integration
async def test_tracklist_missing_auth_returns_401(
    session: AsyncSession,
    redis_client: redis_async.Redis,
) -> None:
    """Missing Authorization header -> 401 (HTTPBearer auto_error)."""
    async with _make_client(session, redis_client, token=None) as ac:
        r = await ac.post(
            "/api/internal/agent/tracklists",
            json={
                "file_id": str(uuid.uuid4()),
                "source": "fingerprint",
                "external_id": "x",
                "request_id": str(uuid.uuid4()),
                "tracks": [{"position": 1}],
            },
        )
    assert r.status_code == 401


@pytest.mark.integration
async def test_tracklist_unknown_token_returns_403(
    session: AsyncSession,
    redis_client: redis_async.Redis,
) -> None:
    """Well-formed bearer with unknown hash -> 403 (no oracle for agent existence)."""
    async with _make_client(session, redis_client, token="phaze_agent_unknown-token-1234") as ac:  # noqa: S106
        r = await ac.post(
            "/api/internal/agent/tracklists",
            json={
                "file_id": str(uuid.uuid4()),
                "source": "fingerprint",
                "external_id": "x",
                "request_id": str(uuid.uuid4()),
                "tracks": [{"position": 1}],
            },
        )
    assert r.status_code == 403


@pytest.mark.integration
async def test_tracklist_concurrent_writer_returns_409_after_poll_exhaustion(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """Concurrent-writer path: req_key already locked + resp_key never populated -> 409 after poll budget."""
    from phaze.routers.agent_tracklists import _REQ_PREFIX, _TTL_SECONDS

    _agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, _agent.id)
    request_id = uuid.uuid4()

    # Simulate a concurrent writer that has acquired the lock but never written the response.
    await redis_client.set(f"{_REQ_PREFIX}{request_id}", "1", nx=True, ex=_TTL_SECONDS)

    # Reduce the poll budget so the test does not actually wait 500ms.
    import phaze.routers.agent_tracklists as router_mod

    original_max = router_mod._CONCURRENT_POLL_MAX_ATTEMPTS
    original_interval = router_mod._CONCURRENT_POLL_INTERVAL_S
    router_mod._CONCURRENT_POLL_MAX_ATTEMPTS = 2
    router_mod._CONCURRENT_POLL_INTERVAL_S = 0.001
    try:
        async with _make_client(session, redis_client, raw_token) as ac:
            r = await ac.post(
                "/api/internal/agent/tracklists",
                json={
                    "file_id": str(file_id),
                    "source": "fingerprint",
                    "external_id": f"fp-concurrent-{file_id.hex[:8]}",
                    "request_id": str(request_id),
                    "tracks": [{"position": 1}],
                },
            )
    finally:
        router_mod._CONCURRENT_POLL_MAX_ATTEMPTS = original_max
        router_mod._CONCURRENT_POLL_INTERVAL_S = original_interval

    assert r.status_code == 409, r.text
    assert "duplicate in-flight request" in r.text


# ---------------------------------------------------------------------------
# Phase 45 (L-02): scan_live_set scheduling-ledger clear -- match path + terminal-ack endpoint
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_tracklist_create_clears_scan_ledger(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """The owner-path create_tracklist (a MATCH) clears scan_live_set:<file_id> in-transaction."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = await _seed_ledger(session, file_id)
    assert await _ledger_present(session, key)

    payload = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": f"fp-clear-{file_id.hex[:8]}",
        "request_id": str(uuid.uuid4()),
        "tracks": [{"position": 1, "artist": "A", "title": "T1"}],
    }
    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post("/api/internal/agent/tracklists", json=payload)

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key), "match-path create_tracklist must clear the scan ledger row"


@pytest.mark.integration
async def test_tracklist_cached_replay_does_not_clear_again(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """The fast-path/cached replay does NO DB work; a ledger row re-seeded after the first
    delivery survives the cached return (the clear only happens on the owner-path transaction)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = await _seed_ledger(session, file_id)
    payload = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": f"fp-cached-{file_id.hex[:8]}",
        "request_id": str(uuid.uuid4()),
        "tracks": [{"position": 1, "artist": "A", "title": "T1"}],
    }
    async with _make_client(session, redis_client, raw_token) as ac:
        r1 = await ac.post("/api/internal/agent/tracklists", json=payload)
        assert r1.status_code == 200, r1.text
        assert not await _ledger_present(session, key)

        # Re-seed the row, then replay the SAME request_id -> fast-path cached return (no DB work).
        await _seed_ledger(session, file_id)
        r2 = await ac.post("/api/internal/agent/tracklists", json=payload)

    assert r2.status_code == 200, r2.text
    assert r1.json() == r2.json()
    assert await _ledger_present(session, key), "the cached fast-path must NOT clear (no DB work on replay)"


@pytest.mark.integration
async def test_scan_terminal_ack_clears_ledger(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """POST /tracklists/{file_id}/scanned clears scan_live_set:<file_id> and returns 200."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = await _seed_ledger(session, file_id)
    assert await _ledger_present(session, key)

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/tracklists/{file_id}/scanned")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_id"] == str(file_id)
    assert body["cleared"] is True
    assert not await _ledger_present(session, key)


@pytest.mark.integration
async def test_scan_terminal_ack_absent_is_noop(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """Acking with no ledger row present is a clean no-op (still 200) -- a re-delivered ack."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"scan_live_set:{file_id}"
    assert not await _ledger_present(session, key)

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/tracklists/{file_id}/scanned")

    assert r.status_code == 200, r.text


@pytest.mark.integration
async def test_scan_terminal_ack_uses_path_file_id_not_redirected(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """The ack key uses the PATH file_id; another file's ledger row is untouched (T-45-05)."""
    agent, raw_token = seed_test_agent
    file_a = await _seed_file(session, agent.id)
    file_b = await _seed_file(session, agent.id)
    key_a = await _seed_ledger(session, file_a)
    key_b = await _seed_ledger(session, file_b)

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/tracklists/{file_a}/scanned")

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key_a)
    assert await _ledger_present(session, key_b), "another file's ledger row must NOT be cleared"


@pytest.mark.integration
async def test_scan_terminal_ack_requires_auth(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """No Authorization header on the ack endpoint -> 401 (AUTH-01)."""
    agent, _ = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, redis_client, token=None) as ac:
        r = await ac.post(f"/api/internal/agent/tracklists/{file_id}/scanned")

    assert r.status_code == 401
