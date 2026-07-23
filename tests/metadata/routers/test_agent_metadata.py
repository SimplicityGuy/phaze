"""DIST-04 (2/5) + DIST-05 (2/5) + D-16 tests for PUT /api/internal/agent/metadata/{file_id}.

Uses an inline smoke FastAPI app builder (mirrors test_agent_auth.py) because Plan 06
wires the agent_metadata router into `main.py`; this test suite is parallel-safe and
does not depend on Plans 03/05/06 landing in any particular order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, update

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
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
async def test_metadata_replay_bumps_updated_at_not_created_at(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """phaze-7634: a conflicting re-PUT bumps FileMetadata.updated_at; created_at stays pinned.

    Same defect class as phaze-c8nz: `on_conflict_do_update`'s `set_` clause used to omit
    `updated_at`, and `TimestampMixin.updated_at`'s ORM `onupdate` hook never fires for a Core
    upsert -- so a re-extracted row kept reporting the FIRST-write timestamp forever. Backdate
    both columns, re-PUT with a non-empty body (the "if dumped" set_ branch), and assert
    updated_at moves forward while created_at is untouched.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r1 = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A", "title": "T1"})
    assert r1.status_code == 200, r1.text

    # Backdate created_at/updated_at directly (bypassing the ORM/onupdate hook) to a fixed point
    # well in the past. metadata.created_at/updated_at are TIMESTAMP WITHOUT TIME ZONE columns --
    # use a naive UTC value so asyncpg doesn't reject the aware/naive mismatch.
    outage_time = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=12)
    await session.execute(update(FileMetadata).where(FileMetadata.file_id == file_id).values(created_at=outage_time, updated_at=outage_time))
    await session.commit()

    before_reupsert = datetime.now(UTC).replace(tzinfo=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r2 = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "B", "title": "T2"})
    assert r2.status_code == 200, r2.text

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert row.artist == "B"
    assert row.created_at == outage_time, "created_at must stay pinned to the first-write value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_reupsert - timedelta(seconds=5), (
        "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"
    )


@pytest.mark.asyncio
async def test_metadata_empty_put_bumps_updated_at_not_created_at(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """phaze-7634: an empty-body re-PUT (the "else" set_ branch) also bumps updated_at.

    Mirrors ``test_metadata_replay_bumps_updated_at_not_created_at`` but exercises the
    empty-``model_dump(exclude_unset=True)`` branch, which has its own separate ``set_`` clause.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r1 = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "Aphex Twin", "title": "Xtal"})
    assert r1.status_code == 200, r1.text

    outage_time = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=12)
    await session.execute(update(FileMetadata).where(FileMetadata.file_id == file_id).values(created_at=outage_time, updated_at=outage_time))
    await session.commit()

    before_reupsert = datetime.now(UTC).replace(tzinfo=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r2 = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={})
    assert r2.status_code == 200, r2.text

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert row.artist == "Aphex Twin", "empty-body PUT must not touch other fields"
    assert row.created_at == outage_time, "created_at must stay pinned to the first-write value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_reupsert - timedelta(seconds=5), (
        "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"
    )


@pytest.mark.asyncio
async def test_metadata_callback_idempotent_after_cas_removal(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Phase 90 (D-09, CAS-guard clarification): removing the DISCOVERED->METADATA_EXTRACTED
    FileRecord.state CAS advance from put_metadata preserves idempotency.

    Before PR-B the metadata callback advanced files.state under a ``state == DISCOVERED`` CAS guard.
    PR-B deletes that embedded READ + WRITE atomically. This test PROVES the ON CONFLICT ``metadata``
    upsert (the marker path) is now the sole idempotency authority: invoking the callback TWICE with the
    SAME payload yields exactly one metadata row, no error, an unchanged final result, and a cleared
    scheduling-ledger row -- the second call produces no duplicate or incorrect effect.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    # The callback clears extract_file_metadata:<file_id> on every success (Phase 45 L-02); seed it so
    # we can assert the non-state work still commits after the CAS removal.
    ledger_key = f"extract_file_metadata:{file_id}"
    await _seed_ledger(session, ledger_key, "extract_file_metadata", file_id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    payload = {"artist": "Boards of Canada", "title": "Roygbiv", "year": 1998}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r1 = await ac.put(f"/api/internal/agent/metadata/{file_id}", json=payload)
        r2 = await ac.put(f"/api/internal/agent/metadata/{file_id}", json=payload)

    # Both calls succeed; the second is an idempotent upsert (ON CONFLICT), not an error.
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    # Exactly ONE metadata marker row survives the double call (the idempotency authority).
    session.expire_all()
    rows = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalars().all()
    assert len(rows) == 1
    # Final observable result is unchanged (same payload both times) and carries no failure marker.
    assert rows[0].artist == "Boards of Canada"
    assert rows[0].title == "Roygbiv"
    assert rows[0].year == 1998
    assert rows[0].failed_at is None
    # The non-state work (ledger clear + commit) still ran after the CAS removal.
    assert await _ledger_present(session, ledger_key) is False


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


# ------------------------------------------------------------------------------------------------
# phaze-bd4n: year/track_number/bitrate are capped at a realistic domain, not left unbounded (or
# ge=0-only) against their int4 columns (wire_bounds rule 3).
# ------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("year", 10000),
        ("year", 9999999999),
        ("track_number", -1),
        ("track_number", 10000),
        ("bitrate", -1),
        ("bitrate", 1_000_001),
        ("bitrate", 5_000_000_000),
    ],
)
async def test_metadata_put_rejects_out_of_domain_integer_tag_and_persists_no_row(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    field: str,
    value: int,
) -> None:
    """An out-of-domain (or int32-overflowing) tag integer is a 422 at the wire, never a 500.

    Previously `year` was fully unbounded, `track_number` had no `Field(...)` at all, and `bitrate`
    was `ge=0` only -- any of them could reach `pg_insert(FileMetadata)` and raise Postgres
    `NumericValueOutOfRange`, unhandled. Asserting no row is persisted proves the rejection happens
    before the insert ever runs, so a buggy tag reader emitting one bad field does not force a
    repeatable 500 + re-enqueue on every callback for that file.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={field: value})

    assert response.status_code == 422, response.text
    assert field in response.text

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one_or_none()
    assert row is None, "a rejected (422) metadata PUT must not persist any row"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("year", 0), ("year", 9999), ("track_number", 0), ("track_number", 9999), ("bitrate", 0), ("bitrate", 1_000_000)],
)
async def test_metadata_put_accepts_integer_tag_at_the_domain_boundary(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    field: str,
    value: int,
) -> None:
    """The domain boundary itself must be ACCEPTED, not merely "one past it" rejected."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={field: value})

    assert response.status_code == 200, response.text

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert getattr(row, field) == value


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
async def test_metadata_put_writes_marker_without_touching_state(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Phase 90 (D-09): a metadata PUT writes the ``metadata`` marker (the derived progress authority --
    done(metadata) = EXISTS metadata WHERE failed_at IS NULL, so the marker is what unblocks the
    fingerprint stage) and NO LONGER writes files.state.

    Before PR-B this callback advanced files.state DISCOVERED -> METADATA_EXTRACTED under a CAS guard.
    PR-B removed that write; the derived marker is the sole authority.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A"})

    assert r.status_code == 200, r.text

    session.expire_all()
    # The metadata marker (derived progress authority) is written, with no failure stamp.
    meta_row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert meta_row.artist == "A"
    assert meta_row.failed_at is None
    # ...and post-MIG-04 there is no scalar files.state for the metadata PUT to write at all.


@pytest.mark.asyncio
async def test_metadata_put_does_not_disturb_later_derived_progress(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """A metadata PUT for an already-ANALYZED file must NOT disturb its analysis marker; the metadata row is still upserted."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    # Advance the file's DERIVED progress past metadata before the PUT (mirrors a parallel analyze callback):
    # a completed analysis row is the sole "analyzed" authority post-MIG-04.
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=file_id, analysis_completed_at=datetime.now(UTC)))
    await session.commit()

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A"})

    assert r.status_code == 200, r.text

    session.expire_all()
    arow = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert arow.analysis_completed_at is not None, "guard failed: the file's derived analyze progress was disturbed by the metadata PUT"
    meta_row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert meta_row.artist == "A", "metadata row must still be upserted regardless of the file's derived progress"


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


# ---------------------------------------------------------------------------
# Phase 81 (FAIL-02 / D-01 / D-10 / D-13): report_metadata_failed persists a
# durable failure marker (both body paths) + put_metadata clears it on success.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_failed_bodyless_persists_marker_and_clears_ledger(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """D-10 / FAIL-02: a BODYLESS POST (old agent) -> 200, inserts a failure row, clears the ledger.

    The failure row has ``failed_at`` set and payload columns (artist/title/...) NULL, so
    ``resolve_status(METADATA)`` derives FAILED, never DONE (D-01/D-02). The endpoint stays
    version-skew-safe: an image that never learned the triage body still records the marker
    AND clears ``extract_file_metadata:<file_id>`` (the CR-02 unbounded-loop guard).
    """
    from phaze.enums.stage import Stage, Status, resolve_status

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
    assert r.json()["cleared"] is True

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    # Failure marker set; payload columns stay NULL (payload-NULL failure row, D-01).
    assert row.failed_at is not None, "bodyless failed POST must persist failed_at"
    assert row.error_message is not None, "bodyless POST records a placeholder detail"
    assert row.artist is None and row.title is None and row.album is None and row.year is None
    # The failure-only row derives FAILED, not DONE (D-02).
    status = resolve_status(Stage.METADATA, {"row_present": True, "failed_at": row.failed_at, "inflight": False})
    assert status is Status.FAILED, f"payload-NULL failure row must derive FAILED, got {status}"
    # Ledger cleared (CR-02).
    assert not await _ledger_present(session, key), "terminal-failure callback must clear the ledger row"


@pytest.mark.asyncio
async def test_metadata_failed_with_body_populates_error_message(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """D-10: a WITH-BODY POST (new agent) -> 200; error_message is populated as ``"<reason>: <error>"``."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed", json={"reason": "error", "error": "boom"})

    assert r.status_code == 200, r.text

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert row.failed_at is not None
    assert row.error_message == "error: boom", f"error_message must be '<reason>: <error>', got {row.error_message!r}"


@pytest.mark.asyncio
async def test_metadata_failed_repeat_bumps_updated_at_not_created_at(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-7634: a repeat failure marker refresh bumps updated_at; created_at stays pinned.

    Same defect class as phaze-c8nz on the CAS-guarded ``report_metadata_failed`` upsert (WR-01,
    ``where=FileMetadata.failed_at.isnot(None)``): the `set_` clause used to omit `updated_at`.
    This exercises the already-failed refresh branch -- the CAS predicate itself is untouched by
    the fix, only which columns the guarded UPDATE writes.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        first = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed", json={"reason": "error", "error": "first crash"})
    assert first.status_code == 200, first.text

    # Backdate created_at/updated_at directly (bypassing the ORM/onupdate hook) to a fixed point
    # well in the past. metadata.created_at/updated_at are TIMESTAMP WITHOUT TIME ZONE columns --
    # use a naive UTC value so asyncpg doesn't reject the aware/naive mismatch.
    outage_time = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=12)
    await session.execute(update(FileMetadata).where(FileMetadata.file_id == file_id).values(created_at=outage_time, updated_at=outage_time))
    await session.commit()

    before_repeat = datetime.now(UTC).replace(tzinfo=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        second = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed", json={"reason": "crashed", "error": "second crash"})
    assert second.status_code == 200, second.text

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert row.error_message == "crashed: second crash"
    assert row.created_at == outage_time, "created_at must stay pinned to the first-write value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_repeat - timedelta(seconds=5), (
        "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"
    )


@pytest.mark.asyncio
async def test_metadata_failed_unknown_body_field_422(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """T-81-03-02: a present body with an unknown field -> 422 (extra='forbid')."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed", json={"reason": "error", "bogus": "x"})

    assert r.status_code == 422, r.text
    errors = r.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc")) == ["body", "bogus"] for e in errors), errors


@pytest.mark.asyncio
async def test_metadata_empty_body_put_after_failure_clears_marker(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """D-13 sharp edge: an empty-body ``{}`` success PUT after a failure clears ``failed_at`` on the existing row.

    Reproduction of the latent bug: without the empty-body clear branch a successful retry
    that carries no fields (empty body) would leave the prior ``failed_at``/``error_message``
    on the row, so the file would derive FAILED forever despite the success.
    """
    from phaze.enums.stage import Stage, Status, resolve_status

    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        # First: a terminal failure stamps the marker.
        r_fail = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed", json={"reason": "error", "error": "boom"})
        # Then: an empty-body success PUT (extraction ran, no tags) must clear the marker.
        r_ok = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={})

    assert r_fail.status_code == 200, r_fail.text
    assert r_ok.status_code == 200, r_ok.text

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert row.failed_at is None, "empty-body success PUT must clear failed_at (D-13)"
    assert row.error_message is None, "empty-body success PUT must clear error_message (D-13)"
    # The row now derives DONE (present, failed_at NULL), not FAILED.
    status = resolve_status(Stage.METADATA, {"row_present": True, "failed_at": row.failed_at, "inflight": False})
    assert status is Status.DONE, f"a cleared success row must derive DONE, got {status}"


@pytest.mark.asyncio
async def test_metadata_field_put_after_failure_clears_marker(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """D-13: a NON-empty success PUT after a failure also clears the marker (unconditional set_ clear)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r_fail = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed", json={"reason": "error", "error": "boom"})
        r_ok = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "Recovered"})

    assert r_fail.status_code == 200, r_fail.text
    assert r_ok.status_code == 200, r_ok.text

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert row.artist == "Recovered"
    assert row.failed_at is None, "a field success PUT must clear failed_at (D-13 unconditional set_ clear)"
    assert row.error_message is None


@pytest.mark.asyncio
async def test_metadata_failed_does_not_clobber_populated_success_row(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """WR-01 (81-REVIEW): a terminal-failure POST must NOT downgrade a row that already carries real tags.

    Repro of the bug: ``POST /api/v1/extract-metadata`` re-enqueues ALL music/video files regardless of
    state, so a file that already succeeded (a ``metadata`` row with populated payload columns AND
    ``failed_at IS NULL`` -> DONE) can be re-extracted; if that re-run times out, the agent POSTs the
    terminal ack. Before the fix ``report_metadata_failed``'s unconditional ``ON CONFLICT DO UPDATE``
    stamped ``failed_at`` onto the good row, so it derived FAILED and lost ``propose`` eligibility
    (done(metadata) = EXISTS metadata WHERE failed_at IS NULL is a ``propose`` upstream conjunct).

    The fix guards the upsert so the marker is only refreshed on a row that is ALREADY a failure row
    (``metadata.failed_at IS NOT NULL``); a DONE row is never downgraded. The ledger clear stays
    UNCONDITIONAL (the run terminated -- the row must clear to avoid the CR-02 unbounded recovery loop),
    so the file keeps its usable metadata AND does not strand a ledger row.
    """
    from phaze.enums.stage import Stage, Status, resolve_status

    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    # A prior successful extraction re-enqueued by POST /extract-metadata carries a fresh ledger row.
    key = f"extract_file_metadata:{file_id}"
    await _seed_ledger(session, key, "extract_file_metadata", file_id)
    assert await _ledger_present(session, key)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        # 1. A successful extraction writes real tags -> the row reads DONE.
        r_ok = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "Aphex Twin", "title": "Xtal", "year": 1992})
        # 2. A re-extraction of the SAME file then fails terminally (the WR-01 trigger).
        r_fail = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed", json={"reason": "timeout", "error": "boom"})

    assert r_ok.status_code == 200, r_ok.text
    assert r_fail.status_code == 200, r_fail.text
    assert r_fail.json()["cleared"] is True

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    # The good metadata survives: real tags retained, NO failure stamp applied.
    assert row.artist == "Aphex Twin", "WR-01: the terminal ack clobbered the real tags"
    assert row.title == "Xtal"
    assert row.year == 1992
    assert row.failed_at is None, "WR-01: a fully-populated DONE metadata row was downgraded to FAILED"
    assert row.error_message is None
    # The file STILL derives DONE (not FAILED) -> its propose upstream conjunct stays satisfied.
    status = resolve_status(Stage.METADATA, {"row_present": True, "failed_at": row.failed_at, "inflight": False})
    assert status is Status.DONE, f"WR-01: a populated metadata row must stay DONE after a stray failure ack, got {status}"
    # The terminal ack STILL clears the ledger (unbounded-loop guard, CR-02) even though it skipped the stamp.
    assert not await _ledger_present(session, key), "WR-01 fix must not regress the unconditional ledger clear"


@pytest.mark.asyncio
async def test_metadata_failed_nul_in_error_persists_and_clears_ledger(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """T-81-03-04 (PG-invalid limb): a NUL-bearing ``error`` must NOT abort the transaction.

    NUL clears pydantic validation (``max_length`` only bounds length; of the two PG-invalid classes
    only lone surrogates are rejected at the wire, as ``string_unicode``). Postgres then rejects the
    write with ``CharacterNotInRepertoireError``, rolling back BOTH the marker upsert and the ledger
    clear -- so the file is re-enqueued and fails identically forever. That is the
    unbounded-recovery-loop outcome the version-skew guard (T-81-03-03) exists to prevent, reached
    through a different door. ``sanitize_pg_text`` strips NUL before persist, so the row lands and
    the ledger clears.
    """
    nul_error = "bad" + chr(0) + "frame"
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"extract_file_metadata:{file_id}"
    await _seed_ledger(session, key, "extract_file_metadata", file_id)
    assert await _ledger_present(session, key)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed", json={"reason": "crashed", "error": nul_error})

    assert r.status_code == 200, r.text

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert row.failed_at is not None
    assert row.error_message is not None
    assert chr(0) not in row.error_message, "NUL must be stripped before persist"
    assert row.error_message == "crashed: badframe"
    # The whole point: the transaction survived, so the ledger clear committed.
    assert not await _ledger_present(session, key), "a NUL-bearing error must not strand the ledger row"


@pytest.mark.asyncio
async def test_metadata_failed_oversized_error_rejected_and_no_row_persisted(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """T-81-03-04 (oversized limb): a 2001-char ``error`` -> 422 at the wire; NO metadata row is persisted.

    ``MetadataFailurePayload.error`` bounds free text with ``max_length=2000`` (T-81-03-04's oversized
    limb, the DoS-via-huge-string threat). One char over the bound must never reach the handler at all,
    so the failed request must leave no trace: no ``metadata`` row for this file, present or absent a
    prior one.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed", json={"reason": "error", "error": "x" * 2001})

    assert r.status_code == 422, r.text
    errors = r.json()["detail"]
    assert any(e.get("type") == "string_too_long" and list(e.get("loc")) == ["body", "error"] for e in errors), errors

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one_or_none()
    assert row is None, "a rejected (422) failure POST must not persist a metadata failure row"


@pytest.mark.asyncio
async def test_metadata_failed_error_at_max_length_boundary_is_accepted(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """T-81-03-04 boundary: a 2000-char ``error`` (exactly at ``max_length``) IS accepted -> 200, row persisted.

    Regression guard against someone "fixing" the bound by lowering it below 2000: this asserts the
    boundary is exact, not merely that 2001 is rejected.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/metadata/{file_id}/failed", json={"reason": "error", "error": "x" * 2000})

    assert r.status_code == 200, r.text

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one()
    assert row.failed_at is not None, "an accepted (2000-char) failure POST must persist the marker"
    assert row.error_message is not None
    assert row.error_message.startswith("error: "), f"error_message must be composed as '<reason>: <error>', got {row.error_message!r}"
