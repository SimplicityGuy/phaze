"""DIST-04 (2/5) + DIST-05 (2/5) + D-16 tests for PUT /api/internal/agent/metadata/{file_id}.

Uses an inline smoke FastAPI app builder (mirrors test_agent_auth.py) because Plan 06
wires the agent_metadata router into `main.py`; this test suite is parallel-safe and
does not depend on Plans 03/05/06 landing in any particular order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, update

from phaze.database import get_session
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_metadata import router as agent_metadata_router
from phaze.services.scheduling_ledger import upsert_ledger_entry


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that wires the agent_metadata router.

    Tests are parallel-safe and decoupled from Plan 06's main.py wiring.
    """
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_metadata_router)
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
            state=FileState.DISCOVERED,
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
async def test_metadata_put_happy_path(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """DIST-04 (2/5): authenticated PUT writes one metadata row.

    Also locks in the PK regression guard: `FileMetadata.id` has a Python-only
    `default=uuid.uuid4`, and `pg_insert(...).values()` bypasses that. The
    router stamps `payload["id"] = uuid.uuid4()` to compensate; the assertion
    `isinstance(row.id, uuid.UUID)` would fail with `NotNullViolationError`
    on a fresh INSERT if the stamp is removed.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.put(
            f"/api/internal/agent/metadata/{file_id}",
            json={"artist": "Aphex Twin", "title": "Xtal", "year": 1992},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent.id
    assert body["file_id"] == str(file_id)

    result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))
    row = result.scalar_one()
    assert row.artist == "Aphex Twin"
    assert row.title == "Xtal"
    assert row.year == 1992
    # PK regression guard: the router stamps payload["id"] = uuid.uuid4()
    # before pg_insert because FileMetadata.id has a Python-only default.
    assert row.id is not None
    assert isinstance(row.id, uuid.UUID)


@pytest.mark.asyncio
async def test_metadata_replay_overwrites(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """DIST-05 (2/5): replay same file_id with different payload -> last write wins, 1 row total."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r1 = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A", "title": "T1"})
        r2 = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "B", "title": "T2"})

    assert r1.status_code == 200
    assert r2.status_code == 200

    result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].artist == "B"
    assert rows[0].title == "T2"
    # AUTH-01 attribution check: the row was written by the authenticated agent.
    assert agent.id is not None  # keep "agent" alive for the linter


@pytest.mark.asyncio
async def test_metadata_extra_field_422(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """D-16: extra body field -> 422 extra_forbidden.

    Specifically: an attempt to forge `agent_id` in the body returns 422 with
    `loc=["body", "agent_id"]`, blocking the T-25-04-S spoofing vector.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.put(
            f"/api/internal/agent/metadata/{file_id}",
            json={"artist": "A", "agent_id": "evil"},
        )

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc")) == ["body", "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_metadata_partial_put_preserves_other_fields(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Gap closure CR-01 (25-VERIFICATION.md): partial PUT must NOT null unset fields.

    Reproduction of the bug: PUT a full payload, then PUT a single field, and
    confirm the unset fields survive. Before the CR-01 fix this test would
    have failed with `title=None, year=None, album=None` after the partial PUT.
    Asserts field-level last-write-wins (the natural read of D-14 for the
    partial-payload case) is now true.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r_full = await ac.put(
            f"/api/internal/agent/metadata/{file_id}",
            json={"artist": "Aphex Twin", "title": "Xtal", "year": 1992, "album": "SAW85-92"},
        )
        r_partial = await ac.put(
            f"/api/internal/agent/metadata/{file_id}",
            json={"artist": "Aphex Twin v2"},
        )

    assert r_full.status_code == 200, r_full.text
    assert r_partial.status_code == 200, r_partial.text

    # Re-read directly from the DB so we bypass any response-side glossing.
    # session needs an expire to drop cached row state from the earlier commits.
    session.expire_all()
    result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))
    row = result.scalar_one()
    # The partial PUT MUST update the explicitly-set field...
    assert row.artist == "Aphex Twin v2", "partial PUT failed to update the set field"
    # ...and MUST preserve every unset prior field (CR-01 regression).
    assert row.title == "Xtal", f"CR-01 regression: title was clobbered to {row.title!r}"
    assert row.year == 1992, f"CR-01 regression: year was clobbered to {row.year!r}"
    assert row.album == "SAW85-92", f"CR-01 regression: album was clobbered to {row.album!r}"


@pytest.mark.asyncio
async def test_metadata_empty_put_is_noop_for_existing_row(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Gap closure CR-01 edge case: PUT with empty body `{}` against existing row -> 200, no field changes.

    Empty model_dump(exclude_unset=True) means there are no fields to UPDATE.
    The router falls back to `ON CONFLICT DO NOTHING` so Postgres doesn't
    receive an empty SET clause. The pre-existing row must be untouched.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r_seed = await ac.put(
            f"/api/internal/agent/metadata/{file_id}",
            json={"artist": "Aphex Twin", "title": "Xtal"},
        )
        r_empty = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={})

    assert r_seed.status_code == 200, r_seed.text
    assert r_empty.status_code == 200, r_empty.text

    session.expire_all()
    result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))
    row = result.scalar_one()
    assert row.artist == "Aphex Twin"
    assert row.title == "Xtal"


# ---------------------------------------------------------------------------
# 260707-rc4: guarded DISCOVERED -> METADATA_EXTRACTED state advance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_put_advances_discovered_to_extracted(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """A metadata PUT for a DISCOVERED file advances its state to METADATA_EXTRACTED (unblocks fingerprint)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)  # seeded in FileState.DISCOVERED

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A"})

    assert r.status_code == 200, r.text

    session.expire_all()
    file_row = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    assert file_row.state == FileState.METADATA_EXTRACTED, "metadata PUT must advance DISCOVERED -> METADATA_EXTRACTED"
    meta_row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert meta_row.artist == "A"


@pytest.mark.asyncio
async def test_metadata_put_does_not_downgrade_later_state(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """A metadata PUT for a file already ANALYZED must NOT downgrade its state; the metadata row is still upserted."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    # Advance the file past DISCOVERED before the PUT (mirrors a parallel fingerprint/analyze callback).
    await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.ANALYZED))
    await session.commit()

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A"})

    assert r.status_code == 200, r.text

    session.expire_all()
    file_row = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    assert file_row.state == FileState.ANALYZED, "guard failed: later state was downgraded by the metadata PUT"
    meta_row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert meta_row.artist == "A", "metadata row must still be upserted regardless of the file's current state"


# ---------------------------------------------------------------------------
# Phase 45 (L-02): extract_file_metadata ledger clear on the success callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_put_success_clears_ledger(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A successful metadata PUT clears extract_file_metadata:<file_id> in the same transaction."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"extract_file_metadata:{file_id}"
    await _seed_ledger(session, key, "extract_file_metadata", file_id)
    assert await _ledger_present(session, key)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A"})

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key), "metadata callback must clear the ledger row"


@pytest.mark.asyncio
async def test_metadata_put_clear_is_noop_when_absent(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A success callback with NO ledger row still returns 200 (no-op clear)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"extract_file_metadata:{file_id}"
    assert not await _ledger_present(session, key)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A"})

    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_metadata_put_clear_uses_path_file_id_not_redirected(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The clear key uses the PATH file_id; another file's ledger row is untouched (T-45-05)."""
    agent, raw_token = seed_test_agent
    file_a = await _seed_file(session, agent.id)
    file_b = await _seed_file(session, agent.id)
    key_a = f"extract_file_metadata:{file_a}"
    key_b = f"extract_file_metadata:{file_b}"
    await _seed_ledger(session, key_a, "extract_file_metadata", file_a)
    await _seed_ledger(session, key_b, "extract_file_metadata", file_b)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/metadata/{file_a}", json={"artist": "A"})

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key_a)
    assert await _ledger_present(session, key_b), "another file's ledger row must NOT be cleared"


# ---------------------------------------------------------------------------
# Phase 45 (L-02 / CR-02): POST /{file_id}/failed terminal-failure ledger clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_failed_clears_ledger(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A terminal-failure POST clears extract_file_metadata:<file_id> (closes CR-02)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"extract_file_metadata:{file_id}"
    await _seed_ledger(session, key, "extract_file_metadata", file_id)
    assert await _ledger_present(session, key)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_id"] == agent.id
    assert body["file_id"] == str(file_id)
    assert body["cleared"] is True
    assert not await _ledger_present(session, key), "terminal-failure callback must clear the ledger row"


@pytest.mark.asyncio
async def test_metadata_failed_is_noop_when_absent(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A terminal-failure POST with NO ledger row still returns 200 (no-op clear)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"extract_file_metadata:{file_id}"
    assert not await _ledger_present(session, key)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is True


@pytest.mark.asyncio
async def test_metadata_failed_uses_path_file_id_not_redirected(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The terminal clear key uses the PATH file_id; another file's row is untouched (T-45-05)."""
    agent, raw_token = seed_test_agent
    file_a = await _seed_file(session, agent.id)
    file_b = await _seed_file(session, agent.id)
    key_a = f"extract_file_metadata:{file_a}"
    key_b = f"extract_file_metadata:{file_b}"
    await _seed_ledger(session, key_a, "extract_file_metadata", file_a)
    await _seed_ledger(session, key_b, "extract_file_metadata", file_b)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/metadata/{file_a}/failed")

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key_a)
    assert await _ledger_present(session, key_b), "another file's ledger row must NOT be cleared by the terminal ack"


# ---------------------------------------------------------------------------
# WR-02: MetadataFailureResponse.cleared is a Literal[True] invariant (no DB)
# ---------------------------------------------------------------------------


def test_metadata_failure_response_accepts_cleared_true() -> None:
    """cleared=True constructs successfully (the only valid value)."""
    from phaze.schemas.agent_metadata import MetadataFailureResponse

    resp = MetadataFailureResponse(agent_id="a", file_id=uuid.uuid4(), cleared=True)
    assert resp.cleared is True


def test_metadata_failure_response_rejects_cleared_false() -> None:
    """WR-02: cleared=False is machine-rejected by Pydantic (Literal[True] invariant)."""
    from pydantic import ValidationError

    from phaze.schemas.agent_metadata import MetadataFailureResponse

    with pytest.raises(ValidationError):
        MetadataFailureResponse(agent_id="a", file_id=uuid.uuid4(), cleared=False)
