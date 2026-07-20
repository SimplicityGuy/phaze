"""DIST-04 (3/5) + DIST-05 (3/5) tests for PUT /api/internal/agent/fingerprints/{file_id}/{engine}.

Uses an inline smoke FastAPI app builder (mirrors test_agent_auth.py) because Plan 06
wires the agent_fingerprint router into `main.py`; this test suite is parallel-safe
and does not depend on Plans 03/05/06 landing in any particular order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import func as sa_func, select

from phaze.database import get_session
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_fingerprint import router as agent_fingerprint_router
from phaze.services.scheduling_ledger import upsert_ledger_entry


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that wires the agent_fingerprint router."""
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_fingerprint_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="0" * 64,
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=100,
        )
    )
    await session.commit()
    return file_id


async def _seed_ledger(session: AsyncSession, key: str, function: str, file_id: uuid.UUID) -> None:
    await upsert_ledger_entry(session, key=key, function=function, kwargs={"file_id": str(file_id)})
    await session.commit()


async def _ledger_present(session: AsyncSession, key: str) -> bool:
    session.expire_all()
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none()
    return row is not None


@pytest.mark.asyncio
async def test_fingerprint_put_happy_path(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """DIST-04 (3/5): authenticated PUT writes one fingerprint row.

    PK regression guard: `FingerprintResult.id` has a Python-only `default=uuid.uuid4`,
    bypassed by `pg_insert(...).values()`. Router stamps `payload["id"] = uuid.uuid4()`
    explicitly to compensate.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.put(
            f"/api/internal/agent/fingerprints/{file_id}/audfprint",
            json={"status": "completed"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent.id
    assert body["file_id"] == str(file_id)
    assert body["engine"] == "audfprint"

    result = await session.execute(select(FingerprintResult).where(FingerprintResult.file_id == file_id))
    row = result.scalar_one()
    assert row.engine == "audfprint"
    assert row.status == "completed"
    # PK regression guard: the router stamps payload["id"] = uuid.uuid4()
    # before pg_insert because FingerprintResult.id has a Python-only default.
    assert row.id is not None
    assert isinstance(row.id, uuid.UUID)


@pytest.mark.asyncio
async def test_fingerprint_put_unknown_engine_returns_422_not_500(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """phaze-94zs: an engine outside the closed {audfprint, panako} set is a clean 422, never a 500.

    Before the fix, `engine: str` had no constraint, so an over-width value reached
    `fingerprint_results.engine` (String(30)) and Postgres raised StringDataRightTruncation,
    aborting the transaction as an unhandled 500. The engine set is genuinely closed (only the
    two hardcoded adapters ever call this route), so the fix is a `Literal` whitelist -- this
    also rejects a same-width-but-unknown engine, not merely an over-length one.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        over_width = await ac.put(
            f"/api/internal/agent/fingerprints/{file_id}/{'x' * 31}",
            json={"status": "completed"},
        )
        unknown_but_short = await ac.put(
            f"/api/internal/agent/fingerprints/{file_id}/shazam",
            json={"status": "completed"},
        )

    assert over_width.status_code == 422, over_width.text
    assert unknown_but_short.status_code == 422, unknown_but_short.text

    # No row was written for either rejected request.
    count_result = await session.execute(select(sa_func.count()).select_from(FingerprintResult).where(FingerprintResult.file_id == file_id))
    assert count_result.scalar_one() == 0


@pytest.mark.asyncio
async def test_fingerprint_replay_overwrites(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """DIST-05 (3/5): same (file_id, engine) twice -> one row, last write wins."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r1 = await ac.put(f"/api/internal/agent/fingerprints/{file_id}/audfprint", json={"status": "completed"})
        r2 = await ac.put(
            f"/api/internal/agent/fingerprints/{file_id}/audfprint",
            json={"status": "failed", "error_message": "engine crashed"},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200

    count_result = await session.execute(select(sa_func.count()).select_from(FingerprintResult).where(FingerprintResult.file_id == file_id))
    assert count_result.scalar_one() == 1

    result = await session.execute(select(FingerprintResult).where(FingerprintResult.file_id == file_id))
    row = result.scalar_one()
    assert row.status == "failed"
    assert row.error_message == "engine crashed"
    assert agent.id is not None  # keep "agent" alive for the linter


@pytest.mark.asyncio
async def test_fingerprint_two_engines_separate_rows(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """(file_id, engine) composite UQ permits per-engine rows."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r1 = await ac.put(f"/api/internal/agent/fingerprints/{file_id}/audfprint", json={"status": "completed"})
        r2 = await ac.put(f"/api/internal/agent/fingerprints/{file_id}/panako", json={"status": "completed"})

    assert r1.status_code == 200
    assert r2.status_code == 200

    count_result = await session.execute(select(sa_func.count()).select_from(FingerprintResult).where(FingerprintResult.file_id == file_id))
    assert count_result.scalar_one() == 2
    assert agent.id is not None  # keep "agent" alive for the linter


# ---------------------------------------------------------------------------
# Phase 45 (L-02): fingerprint_file ledger clear -- SINGLE key per file (not per engine)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_put_success_clears_ledger(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A successful fingerprint PUT clears fingerprint_file:<file_id> in the same transaction."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"fingerprint_file:{file_id}"
    await _seed_ledger(session, key, "fingerprint_file", file_id)
    assert await _ledger_present(session, key)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/fingerprints/{file_id}/audfprint", json={"status": "completed"})

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key), "fingerprint callback must clear the single-per-file ledger row"


@pytest.mark.asyncio
async def test_fingerprint_second_engine_clear_is_noop(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The ledger key is single-per-file: a second engine PUT clears nothing new and still 200."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"fingerprint_file:{file_id}"
    await _seed_ledger(session, key, "fingerprint_file", file_id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r1 = await ac.put(f"/api/internal/agent/fingerprints/{file_id}/audfprint", json={"status": "completed"})
        # First engine PUT already cleared the single key; the second engine PUT is a clear no-op.
        r2 = await ac.put(f"/api/internal/agent/fingerprints/{file_id}/panako", json={"status": "completed"})

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert not await _ledger_present(session, key)


@pytest.mark.asyncio
async def test_fingerprint_put_clear_uses_path_file_id_not_redirected(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The clear key uses the PATH file_id; another file's ledger row is untouched (T-45-05)."""
    agent, raw_token = seed_test_agent
    file_a = await _seed_file(session, agent.id)
    file_b = await _seed_file(session, agent.id)
    key_a = f"fingerprint_file:{file_a}"
    key_b = f"fingerprint_file:{file_b}"
    await _seed_ledger(session, key_a, "fingerprint_file", file_a)
    await _seed_ledger(session, key_b, "fingerprint_file", file_b)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/fingerprints/{file_a}/audfprint", json={"status": "completed"})

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key_a)
    assert await _ledger_present(session, key_b), "another file's ledger row must NOT be cleared"


# ---------------------------------------------------------------------------
# Phase 45 (L-02 / CR-02): POST /{file_id}/failed terminal-failure ledger clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_failed_clears_ledger(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A terminal-failure POST clears the single-per-file fingerprint_file:<file_id> (closes CR-02)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"fingerprint_file:{file_id}"
    await _seed_ledger(session, key, "fingerprint_file", file_id)
    assert await _ledger_present(session, key)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/fingerprints/{file_id}/failed")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_id"] == agent.id
    assert body["file_id"] == str(file_id)
    assert body["cleared"] is True
    assert not await _ledger_present(session, key), "terminal-failure callback must clear the single-per-file ledger row"


@pytest.mark.asyncio
async def test_fingerprint_failed_is_noop_when_absent(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A terminal-failure POST with NO ledger row still returns 200 (no-op clear)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"fingerprint_file:{file_id}"
    assert not await _ledger_present(session, key)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/fingerprints/{file_id}/failed")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is True


@pytest.mark.asyncio
async def test_fingerprint_failed_uses_path_file_id_not_redirected(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The terminal clear key uses the PATH file_id; another file's row is untouched (T-45-05).

    Also locks the single-per-file key: the /failed path takes NO engine, so the key is
    fingerprint_file:<file_id> regardless of which engine triggered the terminal failure.
    """
    agent, raw_token = seed_test_agent
    file_a = await _seed_file(session, agent.id)
    file_b = await _seed_file(session, agent.id)
    key_a = f"fingerprint_file:{file_a}"
    key_b = f"fingerprint_file:{file_b}"
    await _seed_ledger(session, key_a, "fingerprint_file", file_a)
    await _seed_ledger(session, key_b, "fingerprint_file", file_b)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/fingerprints/{file_a}/failed")

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key_a)
    assert await _ledger_present(session, key_b), "another file's ledger row must NOT be cleared by the terminal ack"


# ---------------------------------------------------------------------------
# WR-02: FingerprintFailureResponse.cleared is a Literal[True] invariant (no DB)
# ---------------------------------------------------------------------------


def test_fingerprint_failure_response_accepts_cleared_true() -> None:
    """cleared=True constructs successfully (the only valid value)."""
    from phaze.schemas.agent_fingerprint import FingerprintFailureResponse

    resp = FingerprintFailureResponse(agent_id="a", file_id=uuid.uuid4(), cleared=True)
    assert resp.cleared is True


def test_fingerprint_failure_response_rejects_cleared_false() -> None:
    """WR-02: cleared=False is machine-rejected by Pydantic (Literal[True] invariant)."""
    from pydantic import ValidationError

    from phaze.schemas.agent_fingerprint import FingerprintFailureResponse

    with pytest.raises(ValidationError):
        FingerprintFailureResponse(agent_id="a", file_id=uuid.uuid4(), cleared=False)
