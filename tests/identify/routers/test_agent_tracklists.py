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
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.routers import agent_tracklists
from phaze.services.scheduling_ledger import upsert_ledger_entry


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")


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


@pytest.mark.integration
async def test_tracklist_owner_failure_releases_lock_then_retry_succeeds(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-dwwj: a mid-handler failure in the owner path RELEASES the req_key lock so a retry works.

    Before the fix, ANY exception after ``SET NX`` (a transient asyncpg blip, deadlock, commit failure)
    returned without caching a response and left the lock held for its full 1h TTL. Every subsequent
    delivery -- including the client's own next retry -- then lost the ``SET NX``, polled, and got a
    409 that agent_client maps to a NEVER-retried error, permanently and silently discarding a matched
    tracklist. The owner path now DELs the lock on failure, so the very next retry re-acquires it.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    request_id = uuid.uuid4()
    req_key = f"{agent_tracklists._REQ_PREFIX}{request_id}"
    payload = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": f"fp-dead-{file_id.hex[:8]}",
        "request_id": str(request_id),
        "tracks": [{"position": 1, "artist": "A", "title": "T1", "timestamp": "00:00:00"}],
    }

    # Simulate a transient failure inside the owner path (after SET NX won the lock).
    calls = {"n": 0}

    async def _boom(*_args: object, **_kwargs: object) -> None:
        calls["n"] += 1
        raise RuntimeError("transient blip in the owner path")

    monkeypatch.setattr(agent_tracklists, "clear_ledger_entry", _boom)

    async with _make_client(session, redis_client, raw_token) as ac:
        with pytest.raises(RuntimeError, match="transient blip"):
            await ac.post("/api/internal/agent/tracklists", json=payload)

    # The core fix: the lock is RELEASED despite the failure (was: held for 1h, stranding all retries).
    assert await redis_client.get(req_key) is None, "owner-path failure must DEL the req_key lock"
    assert calls["n"] == 1

    # The retry now re-acquires the freed lock and succeeds (no permanent 409 dead-lock).
    monkeypatch.undo()
    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post("/api/internal/agent/tracklists", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["track_count"] == 1


@pytest.mark.integration
async def test_tracklist_post_commit_cache_failure_returns_durable_response_not_500(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-42jr: a Redis blip on the POST-COMMIT idempotency cache write must NOT 500.

    The tracklist/version/tracks and the ledger clear are already durably committed when the
    resp_key SET runs. Re-raising there returned a 500, tripping agent_client's tenacity retry to
    re-run the whole owner path with the same request_id -- appending a DUPLICATE TracklistVersion
    (version_number = max+1 sidesteps the UNIQUE constraint). The handler must treat a post-commit
    cache miss as success: return the durable 200, leaving exactly one version.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    request_id = uuid.uuid4()
    ext = f"fp-cachefail-{file_id.hex[:8]}"
    payload = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": ext,
        "request_id": str(request_id),
        "tracks": [{"position": 1, "artist": "A", "title": "T1", "timestamp": "00:00:00"}],
    }

    # Fail ONLY the resp_key cache write (post-commit); the req_key SET NX must still succeed.
    real_set = redis_client.set

    async def _set(name: object, *args: object, **kwargs: object) -> object:
        if str(name).startswith(agent_tracklists._RESP_PREFIX):
            raise RuntimeError("post-commit redis blip")
        return await real_set(name, *args, **kwargs)

    monkeypatch.setattr(redis_client, "set", _set)

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post("/api/internal/agent/tracklists", json=payload)

    assert r.status_code == 200, r.text
    assert r.json()["track_count"] == 1
    assert r.json()["version"] == 1

    # Exactly ONE version committed -- no duplicate from a retry the 500 would have triggered.
    session.expire_all()
    tracklists = (await session.execute(select(Tracklist).where(Tracklist.external_id == ext))).scalars().all()
    assert len(tracklists) == 1
    versions = (await session.execute(select(TracklistVersion).where(TracklistVersion.tracklist_id == tracklists[0].id))).scalars().all()
    assert len(versions) == 1


@pytest.mark.integration
async def test_tracklist_owner_path_cancellation_releases_lock(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-42jr: a CancelledError in the owner path must STILL release the req_key lock.

    CancelledError is a BaseException, so the old ``except Exception`` skipped the release and
    stranded the lock for the full 1h TTL -- every subsequent delivery then lost the SET NX and
    409ed. The release must be BaseException-proof.
    """
    import asyncio

    from phaze.schemas.agent_tracklists import TracklistCreatePayload

    agent, _ = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    request_id = uuid.uuid4()
    req_key = f"{agent_tracklists._REQ_PREFIX}{request_id}"
    body = TracklistCreatePayload(
        file_id=file_id,
        source="fingerprint",
        external_id=f"fp-cancel-{file_id.hex[:8]}",
        request_id=request_id,
        tracks=[{"position": 1, "artist": "A", "title": "T1", "timestamp": "00:00:00"}],  # type: ignore[list-item]
    )

    async def _cancel(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(agent_tracklists, "_run_owner_path", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await agent_tracklists.create_tracklist(body=body, agent=agent, session=session, redis_client=redis_client)

    assert await redis_client.get(req_key) is None, "cancellation must release the owner lock, not strand it for 1h"


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


@pytest.mark.integration
@pytest.mark.parametrize(
    ("field", "payload_patch"),
    [
        ("external_id", {"external_id": "e" * 51}),
        ("timestamp", {"tracks": [{"position": 1, "timestamp": "x" * 21}]}),
    ],
)
async def test_tracklist_over_width_value_422s_without_taking_the_idempotency_lock(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
    field: str,
    payload_patch: dict[str, object],
) -> None:
    """phaze-btlu: an over-width value is rejected 422 by pydantic BEFORE the SET NX EX req_key.

    This is the whole point of fixing at the schema boundary rather than catching
    StringDataRightTruncation downstream (wire_bounds rule 4). Had the value reached Postgres, the
    aborted transaction would surface as an unhandled 500 -- and, because the lock is taken at
    router:100 BEFORE the insert, every retry of the deterministic request_id would then get a
    misleading 409 for the full hour of the TTL. Asserting the key is ABSENT is what proves the
    rejection happened early enough to leave the retry path clean.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    request_id = uuid.uuid4()
    payload: dict[str, object] = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": f"fp-{file_id.hex[:8]}",
        "request_id": str(request_id),
        "tracks": [{"position": 1, "artist": "A", "title": "T1"}],
        **payload_patch,
    }

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post("/api/internal/agent/tracklists", json=payload)

    assert r.status_code == 422, r.text
    assert field in r.text
    assert "string_too_long" in r.text, r.text
    assert await redis_client.get(f"tracklist_req:{request_id}") is None, (
        "the idempotency lock was taken for a payload that failed validation -- the retry is now poisoned"
    )
    assert (await session.execute(select(Tracklist).where(Tracklist.external_id == "e" * 51))).first() is None


@pytest.mark.integration
async def test_tracklist_position_over_int32_422s_without_taking_the_idempotency_lock(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """phaze-p9k7: an int32-overflowing ``position`` is rejected 422 by pydantic BEFORE the SET NX EX req_key.

    Mirrors the phaze-btlu width-cap regression test exactly, but for the int32 numeric bound on
    ``TracklistTrackPayload.position`` (wire_bounds rule 3/4). Had the value reached Postgres, the
    aborted NumericValueOutOfRange transaction would surface as an unhandled 500 -- and, because the
    lock is taken BEFORE the insert, every retry of the deterministic request_id would then get a
    misleading 409 for the full hour of the TTL. Asserting the key is ABSENT is what proves the
    rejection happened early enough to leave the retry path clean.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    request_id = uuid.uuid4()
    payload: dict[str, object] = {
        "file_id": str(file_id),
        "source": "fingerprint",
        "external_id": f"fp-{file_id.hex[:8]}",
        "request_id": str(request_id),
        "tracks": [{"position": 2147483648, "artist": "A", "title": "T1"}],
    }

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post("/api/internal/agent/tracklists", json=payload)

    assert r.status_code == 422, r.text
    assert "position" in r.text
    assert "less_than_equal" in r.text, r.text
    assert await redis_client.get(f"tracklist_req:{request_id}") is None, (
        "the idempotency lock was taken for a payload that failed validation -- the retry is now poisoned"
    )
    assert (await session.execute(select(Tracklist).where(Tracklist.external_id == f"fp-{file_id.hex[:8]}"))).first() is None
