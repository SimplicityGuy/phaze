"""Contract tests for PUT /api/internal/agent/analysis/{file_id} (Phase 26 D-26).

Mirrors `tests/test_routers/test_agent_metadata.py` exactly: smoke-app pattern,
seed FileRecord for FK satisfaction, expire_all to bypass session cache between
PUTs. Covers happy path, idempotent replay, partial-PUT field-level LWW (CR-01
invariant), empty-body no-op for existing rows, first-PUT-with-empty-body
creates a row, 422 on extra fields (D-16 / AUTH-01 spoof block), and the auth
401/403 surface from `Depends(get_authenticated_agent)`.
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
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers import agent_analysis as agent_analysis_module
from phaze.routers.agent_analysis import router as agent_analysis_router
from phaze.services.scheduling_ledger import upsert_ledger_entry


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that wires the agent_analysis router.

    Tests are parallel-safe and decoupled from Plan 12's main.py wiring.
    """
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_analysis_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _make_client(session: AsyncSession, token: str | None = None) -> AsyncClient:
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    """Seed a FileRecord so AnalysisResult.file_id FK (files.id) is satisfied."""
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
            file_size=1024,
        )
    )
    await session.commit()
    return file_id


async def _seed_ledger(session: AsyncSession, key: str, function: str, file_id: uuid.UUID) -> None:
    """Seed a scheduling-ledger row so the callback's clear has something to remove."""
    await upsert_ledger_entry(session, key=key, function=function, kwargs={"file_id": str(file_id)})
    await session.commit()


async def _ledger_present(session: AsyncSession, key: str) -> bool:
    session.expire_all()
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none()
    return row is not None


async def _seed_cloud_job(session: AsyncSession, file_id: uuid.UUID, status: CloudJobStatus) -> None:
    """Seed a cloud_job row at a chosen status (D-14 reaper tests).

    `staging_bucket` is left NULL so the callback's `_delete_staged_object_if_cloud` guard
    short-circuits with zero S3 calls -- the reaper behaviour is what the tests exercise, not staging.
    """
    session.add(CloudJob(file_id=file_id, status=status.value))
    await session.commit()


async def _cloud_job_present(session: AsyncSession, file_id: uuid.UUID) -> bool:
    session.expire_all()
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    return row is not None


@pytest.mark.asyncio
async def test_analysis_put_happy_path(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """PUT with full body creates AnalysisResult row and returns 200."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={
                "bpm": 128.5,
                "musical_key": "C# minor",
                "mood": {"happy": 0.7, "energetic": 0.8},
                "style": {"electronic": 0.9, "house": 0.6},
                "danceability": 0.85,
                "energy": 0.92,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent.id
    assert body["file_id"] == str(file_id)

    result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    row = result.scalar_one()
    assert row.bpm == 128.5
    assert row.musical_key == "C# minor"
    # PK regression guard: AnalysisResult.id has a Python-only default, the router
    # must stamp `payload["id"] = uuid.uuid4()` so pg_insert doesn't fail NOT NULL.
    assert row.id is not None
    assert isinstance(row.id, uuid.UUID)
    # Storage conversion: dict[str, float] -> summary string for mood/style (String(50) columns).
    assert row.mood is not None
    assert "energetic=0.80" in row.mood
    assert "happy=0.70" in row.mood
    assert row.style is not None
    assert "electronic=0.90" in row.style
    assert "house=0.60" in row.style
    # Overflow funnel: wire fields without dedicated columns land in `features` JSONB.
    assert row.features is not None
    assert row.features.get("danceability") == 0.85
    assert row.features.get("energy") == 0.92


@pytest.mark.asyncio
async def test_analysis_put_replay_idempotent(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """PUT twice with same payload -> 1 row in DB (D-26 idempotent upsert)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    payload = {"bpm": 120.0, "musical_key": "G major"}

    async with _make_client(session, raw_token) as ac:
        r1 = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=payload)
        r2 = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=payload)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    session.expire_all()
    result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].bpm == 120.0
    assert rows[0].musical_key == "G major"


@pytest.mark.asyncio
async def test_analysis_put_replay_bumps_updated_at_not_created_at(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """phaze-7634: a conflicting re-PUT bumps AnalysisResult.updated_at; created_at stays pinned.

    Same defect class as phaze-c8nz: `on_conflict_do_update`'s `set_` clause used to omit
    `updated_at`, and `TimestampMixin.updated_at`'s ORM `onupdate` hook never fires for a Core
    upsert -- so a re-analyzed row kept reporting the FIRST-write timestamp forever. Backdate
    both columns, re-PUT, and assert updated_at moves forward while created_at is untouched.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r1 = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 120.0})
    assert r1.status_code == 200, r1.text

    # Backdate created_at/updated_at directly (bypassing the ORM/onupdate hook) to a fixed point
    # well in the past. analysis.created_at/updated_at are TIMESTAMP WITHOUT TIME ZONE columns --
    # use a naive UTC value so asyncpg doesn't reject the aware/naive mismatch.
    outage_time = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=12)
    await session.execute(update(AnalysisResult).where(AnalysisResult.file_id == file_id).values(created_at=outage_time, updated_at=outage_time))
    await session.commit()

    before_reupsert = datetime.now(UTC).replace(tzinfo=None)

    async with _make_client(session, raw_token) as ac:
        r2 = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 125.0})
    assert r2.status_code == 200, r2.text

    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.bpm == 125.0
    assert row.created_at == outage_time, "created_at must stay pinned to the first-write value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_reupsert - timedelta(seconds=5), (
        "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"
    )


@pytest.mark.asyncio
async def test_analysis_partial_put_preserves_other_fields(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """CR-01 invariant: partial PUT only updates the fields the caller set."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r_full = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={
                "bpm": 128.0,
                "musical_key": "A minor",
                "danceability": 0.8,
                "energy": 0.9,
            },
        )
        r_partial = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"bpm": 130.0},  # only bpm
        )

    assert r_full.status_code == 200, r_full.text
    assert r_partial.status_code == 200, r_partial.text

    session.expire_all()
    result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    row = result.scalar_one()
    assert row.bpm == 130.0, "partial PUT failed to update bpm"
    assert row.musical_key == "A minor", f"CR-01 regression: musical_key was clobbered to {row.musical_key!r}"
    # CR-01 invariant: `features` JSONB (carrying funneled danceability/energy) preserved unchanged.
    assert row.features is not None, "CR-01 regression: features JSONB was wiped by partial PUT"
    assert row.features.get("danceability") == 0.8, f"CR-01 regression: danceability was clobbered to {row.features.get('danceability')!r}"
    assert row.features.get("energy") == 0.9, f"CR-01 regression: energy was clobbered to {row.features.get('energy')!r}"


@pytest.mark.asyncio
async def test_analysis_empty_put_is_noop_for_existing_row(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Empty body PUT against existing row: 200 + row preserved (ON CONFLICT DO NOTHING branch)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r_seed = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"bpm": 100.0, "musical_key": "F"},
        )
        r_empty = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={})

    assert r_seed.status_code == 200, r_seed.text
    assert r_empty.status_code == 200, r_empty.text

    session.expire_all()
    result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    row = result.scalar_one()
    assert row.bpm == 100.0
    assert row.musical_key == "F"


@pytest.mark.asyncio
async def test_analysis_first_put_with_empty_body_creates_row(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Empty body PUT on a brand-new file_id creates a row with all fields NULL."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={})

    assert r.status_code == 200, r.text
    session.expire_all()
    result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    row = result.scalar_one()
    assert row.bpm is None
    assert row.musical_key is None
    assert row.mood is None
    assert row.style is None
    assert row.features is None  # no overflow fields set -> features stays NULL


def _window(tier: str, idx: int, start: float, end: float, **extra: object) -> dict[str, object]:
    """Build a window dict for a PUT body."""
    return {"tier": tier, "window_index": idx, "start_sec": start, "end_sec": end, **extra}


async def _window_rows(session: AsyncSession, file_id: uuid.UUID) -> list[AnalysisWindow]:
    session.expire_all()
    result = await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_id).order_by(AnalysisWindow.window_index))
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_analysis_window_idempotent_insert_then_replace(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """PUT [w0,w1] -> 2 rows; re-PUT [w0',w1',w2'] -> exactly 3 rows (idempotent replace, no duplicates)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r1 = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={
                "bpm": 120.0,
                "windows": [
                    _window("fine", 0, 0.0, 30.0, bpm=120.0, musical_key="C major"),
                    _window("fine", 1, 30.0, 60.0, bpm=122.0, musical_key="A minor"),
                ],
            },
        )
        assert r1.status_code == 200, r1.text
        rows1 = await _window_rows(session, file_id)
        assert len(rows1) == 2

        r2 = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={
                "windows": [
                    _window("fine", 0, 0.0, 30.0, bpm=121.0),
                    _window("fine", 1, 30.0, 60.0, bpm=123.0),
                    _window("coarse", 0, 0.0, 180.0, mood="calm"),
                ],
            },
        )

    assert r2.status_code == 200, r2.text
    rows2 = await _window_rows(session, file_id)
    assert len(rows2) == 3, "re-PUT must REPLACE windows, not append duplicates"
    assert rows2[0].bpm == 121.0


@pytest.mark.asyncio
async def test_analysis_window_idempotent_partial_put_preserves_windows(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """PUT with windows omitted (None) leaves existing windows untouched (partial-PUT)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r_seed = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"windows": [_window("fine", 0, 0.0, 30.0, bpm=120.0)]},
        )
        assert r_seed.status_code == 200, r_seed.text
        # Aggregate-only PUT: no `windows` key at all.
        r_partial = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 130.0})

    assert r_partial.status_code == 200, r_partial.text
    rows = await _window_rows(session, file_id)
    assert len(rows) == 1, "aggregate-only PUT must NOT wipe existing windows"
    assert rows[0].bpm == 120.0


@pytest.mark.asyncio
async def test_analysis_window_idempotent_empty_list_deletes(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """PUT with windows=[] deletes all windows for the file (explicit empty replace)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r_seed = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"windows": [_window("fine", 0, 0.0, 30.0, bpm=120.0), _window("fine", 1, 30.0, 60.0, bpm=122.0)]},
        )
        assert r_seed.status_code == 200, r_seed.text
        r_empty = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"windows": []})

    assert r_empty.status_code == 200, r_empty.text
    rows = await _window_rows(session, file_id)
    assert len(rows) == 0, "windows=[] must delete all window rows for the file"


@pytest.mark.asyncio
async def test_analysis_window_idempotent_delete_scoped_to_path_file_id(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """The DELETE is scoped strictly to the PATH file_id (no cross-file deletion, AUTH-01)."""
    agent, raw_token = seed_test_agent
    file_a = await _seed_file(session, agent.id)
    file_b = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        await ac.put(
            f"/api/internal/agent/analysis/{file_a}",
            json={"windows": [_window("fine", 0, 0.0, 30.0, bpm=120.0)]},
        )
        await ac.put(
            f"/api/internal/agent/analysis/{file_b}",
            json={"windows": [_window("fine", 0, 0.0, 30.0, bpm=99.0)]},
        )
        # Re-PUT file_a; file_b's windows must be untouched.
        await ac.put(
            f"/api/internal/agent/analysis/{file_a}",
            json={"windows": [_window("fine", 0, 0.0, 30.0, bpm=121.0)]},
        )

    rows_b = await _window_rows(session, file_b)
    assert len(rows_b) == 1, "cross-file deletion: file_b windows must survive a PUT to file_a"
    assert rows_b[0].bpm == 99.0


# phaze-syxv: 2,731 rows x 12 bind parameters = 32,772 > the 32,767 the PostgreSQL Bind message can
# carry in an int16 count, so an UNCHUNKED insert raises asyncpg
# `InterfaceError: the number of query arguments cannot exceed 32767` from here up. 2,800 is just
# past that break AND is roughly the fine-window count of a real 24h recording at 30s windows -- the
# figure the schema's own comment cites as realistic. These tests FAIL on the pre-fix code.
_BIND_LIMIT_BREAK_ROWS = 2800


@pytest.mark.asyncio
async def test_analysis_window_insert_exceeds_pg_bind_parameter_limit(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """A window set past the int16 bind-parameter limit persists in full (phaze-syxv regression).

    Before the fix this was TERMINAL data loss, not a transient 500: the PUT passed schema
    validation (well under ``max_length=50000``), the single multi-row VALUES raised, and because
    ``FAILURE_IS_TERMINAL[ANALYZE]`` the analyze stage was marked permanently FAILED -- discarding
    hours of essentia CPU, with every retry reproducing the same deterministic error.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    windows = [_window("fine", i, i * 30.0, (i + 1) * 30.0, bpm=120.0 + (i % 7), musical_key="C major") for i in range(_BIND_LIMIT_BREAK_ROWS)]

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 120.0, "windows": windows})

    assert response.status_code == 200, response.text
    rows = await _window_rows(session, file_id)
    assert len(rows) == _BIND_LIMIT_BREAK_ROWS, "every window must persist -- chunking must not drop or truncate rows"
    # Chunk boundaries are where a naive split silently loses or duplicates rows, so pin the ends
    # and the count of distinct indices rather than only the total.
    assert rows[0].window_index == 0
    assert rows[-1].window_index == _BIND_LIMIT_BREAK_ROWS - 1
    assert len({r.window_index for r in rows}) == _BIND_LIMIT_BREAK_ROWS, "no duplicated window_index across chunk boundaries"


@pytest.mark.asyncio
async def test_analysis_window_chunked_insert_is_atomic_on_mid_write_failure(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure BETWEEN chunks commits nothing -- the window replace stays all-or-nothing (phaze-syxv).

    Chunking splits one statement into several, which would be a regression if the chunks could
    land independently: a file holding half its windows reads as a complete analysis and is worse
    than a clean failure. All chunks share ``put_analysis``'s single transaction, so a mid-write
    raise leaves ZERO windows committed.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    real_chunk_rows = agent_analysis_module.chunk_rows

    def _explode_after_first_chunk(rows: object) -> object:
        """Yield the first chunk (so rows really are written), then fail like a dropped connection."""
        for index, chunk in enumerate(real_chunk_rows(rows)):  # type: ignore[arg-type]
            if index > 0:
                raise RuntimeError("simulated mid-write failure")
            yield chunk

    monkeypatch.setattr(agent_analysis_module, "chunk_rows", _explode_after_first_chunk)

    windows = [_window("fine", i, i * 30.0, (i + 1) * 30.0, bpm=120.0) for i in range(_BIND_LIMIT_BREAK_ROWS)]

    async with _make_client(session, raw_token) as ac:
        with pytest.raises(RuntimeError, match="simulated mid-write failure"):
            await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 120.0, "windows": windows})

    # The route never reached its single `session.commit()`, so unwinding the transaction must
    # discard the chunks that DID execute.
    await session.rollback()
    rows = await _window_rows(session, file_id)
    assert rows == [], "a mid-write failure must leave NO partially-written window set committed"


@pytest.mark.asyncio
async def test_analysis_put_advances_state_and_persists_coverage_columns(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Phase 43 / Phase 90 (D-09): a non-empty PUT marks analysis complete (analysis_completed_at, the
    derived 'analyzed' authority -- the files.state write was removed) and lands coverage in dedicated columns."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={
                "bpm": 128.0,
                "fine_windows_analyzed": 10,
                "fine_windows_total": 40,
                "coarse_windows_analyzed": 2,
                "coarse_windows_total": 8,
                "sampled": True,
            },
        )

    assert response.status_code == 200, response.text
    session.expire_all()

    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    # Phase 90 (D-09): done(analyze) derives from analysis_completed_at, not files.state.
    assert row.analysis_completed_at is not None, "non-empty PUT must stamp the derived 'analyzed' completion marker"
    # Coverage landed in dedicated columns (Pitfall 3 -- NOT the features JSONB).
    assert row.fine_windows_analyzed == 10
    assert row.fine_windows_total == 40
    assert row.coarse_windows_analyzed == 2
    assert row.coarse_windows_total == 8
    assert row.sampled is True
    # features must stay NULL -- no coverage field leaked into the JSONB overflow.
    assert row.features is None, "coverage fields must not funnel into features JSONB"


@pytest.mark.asyncio
async def test_analysis_empty_put_does_not_advance_state(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Phase 43: an empty-body PUT ({}) is a no-op -- state stays as it was (DISCOVERED)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={})

    assert r.status_code == 200, r.text
    session.expire_all()
    # Derived non-advance: an empty PUT writes no completed analysis row.
    arow = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()
    assert arow is None or arow.analysis_completed_at is None, "empty PUT must NOT complete analysis"


@pytest.mark.asyncio
async def test_analysis_put_stamps_completed_at(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Phase 57.1 KEY RISK: a non-empty PUT stamps analysis_completed_at (the completion discriminator).

    The discriminator is set in the SAME dumped-guarded txn that writes the completed analysis row, so a
    completed analysis row is distinguishable from an in-flight partial row (which leaves it NULL).
    It is server-set via func.now() -- never wire-set -- so a client cannot forge completion.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"bpm": 128.0, "fine_windows_analyzed": 40, "fine_windows_total": 40},
        )

    assert response.status_code == 200, response.text
    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.analysis_completed_at is not None, "a completed (non-empty) PUT must stamp analysis_completed_at"


@pytest.mark.asyncio
async def test_analysis_empty_put_leaves_completed_at_null(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Phase 57.1: an empty-body PUT ({}) is a no-op -- no completion, so analysis_completed_at stays NULL.

    This mirrors the ANALYZED-flip guard: `dumped` is falsy for an empty body, so the completion
    branch (state flip + completed_at stamp) is skipped. A partial in-flight row therefore never
    looks completed.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={})

    assert r.status_code == 200, r.text
    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.analysis_completed_at is None, "an empty PUT (no completion) must leave analysis_completed_at NULL"


@pytest.mark.asyncio
async def test_analysis_completed_at_not_wire_settable(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Phase 57.1 (T-57.1-12): analysis_completed_at is server-set only -- a client cannot forge it.

    The wire payload uses extra='forbid', so a body carrying analysis_completed_at is a 422. This
    locks the spoofing mitigation: completion cannot be claimed by the agent, only stamped by func.now().
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"bpm": 120.0, "analysis_completed_at": "2026-01-01T00:00:00Z"},
        )

    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_analysis_extra_field_422(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """D-16: extra='forbid' rejects unknown fields (AUTH-01 -- no agent_id forgery)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        response = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"bpm": 120.0, "agent_id": "spoofed-agent"},
        )

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc")) == ["body", "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_analysis_put_overwidth_musical_key_422s_without_persisting(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-ty0o: an over-width ``musical_key`` is rejected 422 BEFORE the pg_insert runs.

    ``analysis_results.musical_key`` is ``String(10)``. Pre-fix, an unbounded string reaching
    Postgres over that width raises ``StringDataRightTruncation``, aborting the transaction --
    since the analyze stage is FAILURE_IS_TERMINAL (Phase 43), a repeatable over-width value from a
    misbehaving upstream would 500 every retry rather than degrade cleanly. 11 chars is the
    smallest over-width value; real essentia ``KeyExtractor`` output never exceeds 8 chars
    (`f"{key} {scale}"`), so this is a malformed/adversarial value, not a realistic one.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"musical_key": "x" * 11})

    assert r.status_code == 422, r.text
    assert "musical_key" in r.text
    assert "string_too_long" in r.text, r.text

    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()
    assert row is None, "a rejected (422) PUT must not persist any AnalysisResult row"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "over_width"),
    [
        ("musical_key", "x" * 11),
        ("mood", "x" * 51),
        ("style", "x" * 51),
    ],
)
async def test_analysis_window_overwidth_field_422s_without_persisting(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    field: str,
    over_width: str,
) -> None:
    """phaze-ty0o: an over-width window ``musical_key``/``mood``/``style`` is rejected 422 first.

    ``analysis_windows.musical_key`` is ``String(10)``; ``.mood``/``.style`` are ``String(50)``. Same
    FAILURE_IS_TERMINAL exposure as the aggregate ``musical_key`` field above -- an unhandled
    ``StringDataRightTruncation`` would abort the transaction on a stage the agent cannot cleanly
    retry past.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(
            f"/api/internal/agent/analysis/{file_id}",
            json={"windows": [_window("fine" if field == "musical_key" else "coarse", 0, 0.0, 30.0, **{field: over_width})]},
        )

    assert r.status_code == 422, r.text
    assert field in r.text
    assert "string_too_long" in r.text, r.text

    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()
    assert row is None, "a rejected (422) PUT must not persist any AnalysisResult row"
    rows = await _window_rows(session, file_id)
    assert not rows, "a rejected (422) PUT must not persist any AnalysisWindow row"


# ---------------------------------------------------------------------------
# Phase 57.1 (Plan 03): AnalysisProgressPayload schema validation
# ---------------------------------------------------------------------------


def test_progress_schema_rejects_extra_key() -> None:
    """AnalysisProgressPayload uses extra='forbid' -- a ride-along agent_id/file_id is a ValidationError (AUTH-01, T-57.1-02)."""
    from pydantic import ValidationError

    from phaze.schemas.agent_analysis import AnalysisProgressPayload

    with pytest.raises(ValidationError):
        AnalysisProgressPayload(fine_windows_analyzed=1, fine_windows_total=40, agent_id="spoofed-agent")  # type: ignore[call-arg]


def test_progress_schema_rejects_negative_count() -> None:
    """AnalysisProgressPayload counts are ge=0 -- a negative count is a ValidationError."""
    from pydantic import ValidationError

    from phaze.schemas.agent_analysis import AnalysisProgressPayload

    with pytest.raises(ValidationError):
        AnalysisProgressPayload(fine_windows_analyzed=-1, fine_windows_total=40)


def test_progress_schema_rejects_int32_overflowing_count() -> None:
    """phaze-01gh: a count >= 2^31 would overflow analysis_results' int4 counter columns -- reject at 422."""
    from pydantic import ValidationError

    from phaze.schemas.agent_analysis import AnalysisProgressPayload

    with pytest.raises(ValidationError) as exc_info:
        AnalysisProgressPayload(fine_windows_analyzed=1, fine_windows_total=2147483648)

    assert any(e.get("type") == "less_than_equal" for e in exc_info.value.errors())


def test_progress_schema_requires_both_counts() -> None:
    """Both fine counts are REQUIRED (no default) -- a progress POST always carries both."""
    from pydantic import ValidationError

    from phaze.schemas.agent_analysis import AnalysisProgressPayload

    with pytest.raises(ValidationError):
        AnalysisProgressPayload(fine_windows_total=40)  # type: ignore[call-arg]


def test_progress_schema_response_exposes_agent_and_file_id() -> None:
    """AnalysisProgressResponse mirrors AnalysisWriteResponse: {agent_id, file_id}."""
    from phaze.schemas.agent_analysis import AnalysisProgressResponse

    fid = uuid.uuid4()
    resp = AnalysisProgressResponse(agent_id="a1", file_id=fid)
    assert resp.agent_id == "a1"
    assert resp.file_id == fid


# ---------------------------------------------------------------------------
# Phase 57.1 (Plan 03): POST /{file_id}/progress -- counter-only sibling handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_post_start_then_bump_single_row_advancing(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """START(0,N) then bump(k,N) upserts ONE analysis row with fine_windows_analyzed advancing (idempotent counter)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r_start = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/progress",
            json={"fine_windows_analyzed": 0, "fine_windows_total": 40},
        )
        assert r_start.status_code == 200, r_start.text
        body = r_start.json()
        assert body["agent_id"] == agent.id
        assert body["file_id"] == str(file_id)

        r_bump = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/progress",
            json={"fine_windows_analyzed": 17, "fine_windows_total": 40},
        )
        assert r_bump.status_code == 200, r_bump.text

    session.expire_all()
    rows = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalars().all()
    assert len(rows) == 1, "progress POSTs must upsert ONE row per file_id, never append"
    assert rows[0].fine_windows_analyzed == 17, "counter must advance to the latest bump"
    assert rows[0].fine_windows_total == 40


@pytest.mark.asyncio
async def test_progress_post_bump_bumps_updated_at_not_created_at(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-7634: a progress-counter bump (conflicting re-upsert) bumps updated_at; created_at stays pinned.

    Same defect class as phaze-c8nz on the counter-only progress upsert (a SIBLING statement
    to put_analysis's, per the module docstring): `on_conflict_do_update`'s `set_` clause used
    to omit `updated_at`. Backdate both columns after START, then bump, and assert updated_at
    moves forward while created_at is untouched.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r_start = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/progress",
            json={"fine_windows_analyzed": 0, "fine_windows_total": 40},
        )
    assert r_start.status_code == 200, r_start.text

    outage_time = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=12)
    await session.execute(update(AnalysisResult).where(AnalysisResult.file_id == file_id).values(created_at=outage_time, updated_at=outage_time))
    await session.commit()

    before_bump = datetime.now(UTC).replace(tzinfo=None)

    async with _make_client(session, raw_token) as ac:
        r_bump = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/progress",
            json={"fine_windows_analyzed": 17, "fine_windows_total": 40},
        )
    assert r_bump.status_code == 200, r_bump.text

    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.fine_windows_analyzed == 17
    assert row.created_at == outage_time, "created_at must stay pinned to the first-write value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_bump - timedelta(seconds=5), (
        "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"
    )


@pytest.mark.asyncio
async def test_progress_post_has_no_completion_side_effects(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """A progress POST writes ONLY the counts: state unchanged, no windows, ledger intact, completed_at NULL (T-57.1-03)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"process_file:{file_id}"
    await _seed_ledger(session, key, "process_file", file_id)
    assert await _ledger_present(session, key)

    async with _make_client(session, raw_token) as ac:
        r = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/progress",
            json={"fine_windows_analyzed": 5, "fine_windows_total": 40},
        )

    assert r.status_code == 200, r.text
    session.expire_all()

    # Derived analyze status NEVER advances on a progress POST (KEY RISK -- completion stays solely on put_analysis).
    # analysis_completed_at stays NULL -> the convergence gate keeps the partial row out of proposals.
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.analysis_completed_at is None, "progress POST must leave analysis_completed_at NULL"

    # No analysis_window detail rows written (D-01 -- detail stays atomic at completion).
    assert len(await _window_rows(session, file_id)) == 0, "progress POST must NOT write analysis_window rows"

    # Scheduling ledger NOT cleared (completion-only side effect).
    assert await _ledger_present(session, key), "progress POST must NOT clear the scheduling ledger"


@pytest.mark.asyncio
async def test_progress_post_missing_auth_returns_401(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """No Authorization header on the progress endpoint -> 401 (HTTPBearer auto_error)."""
    agent, _ = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, token=None) as ac:
        r = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/progress",
            json={"fine_windows_analyzed": 0, "fine_windows_total": 40},
        )

    assert r.status_code == 401


@pytest.mark.asyncio
async def test_progress_post_forged_body_key_422(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """extra='forbid' rejects a body smuggling agent_id/file_id (AUTH-01, T-57.1-02)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/progress",
            json={"fine_windows_analyzed": 0, "fine_windows_total": 40, "agent_id": "spoofed-agent"},
        )

    assert r.status_code == 422
    errors = r.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc")) == ["body", "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_progress_post_overflowing_count_422s_without_stranding_the_ledger(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-01gh: an int32-overflowing progress count is rejected 422 before the pg_insert / ledger touch.

    Bare unhandled 500 in the pre-fix code (post_analysis_progress has no ledger-clear step of its
    own to skip, but the same DataError -> 500 -> no clean signal for the agent to stop retrying with
    the same bad count applies). Asserting no AnalysisResult row is written proves the rejection
    happens before any transaction opens.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/progress",
            json={"fine_windows_analyzed": 0, "fine_windows_total": 2147483648},
        )

    assert r.status_code == 422, r.text
    assert "fine_windows_total" in r.text

    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()
    assert row is None, "a rejected (422) progress POST must not persist any AnalysisResult row"


@pytest.mark.asyncio
async def test_analysis_failed_sets_marker(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """POST /{file_id}/failed stamps the derived analyze-failure marker (analysis.failed_at, the sole
    authority after Phase 90 D-09 removed the files.state = ANALYSIS_FAILED write) and echoes agent_id/file_id."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        response = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/failed",
            json={"reason": "timeout", "error": "killed after 7200s"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent.id
    assert body["file_id"] == str(file_id)

    session.expire_all()
    # Phase 90 (D-09): failed_clause(Stage.ANALYZE) derives from analysis.failed_at, not files.state.
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert row.failed_at is not None
    assert row.analysis_completed_at is None, "a failure marker must not also read as complete (XOR CHECK)"


@pytest.mark.asyncio
async def test_analysis_failed_bad_reason_422(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A reason outside the Literal set returns 422 and does NOT change state."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        response = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/failed",
            json={"reason": "kaboom"},
        )

    assert response.status_code == 422
    session.expire_all()
    # Derived non-advance: a 422 body writes no failure marker.
    arow = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()
    assert arow is None or arow.failed_at is None, "a 422 body must not write a failure marker"


@pytest.mark.asyncio
async def test_analysis_failed_extra_field_422(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """extra='forbid' rejects an attempt to smuggle agent_id in the failure body (AUTH-01)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        response = await ac.post(
            f"/api/internal/agent/analysis/{file_id}/failed",
            json={"reason": "error", "agent_id": "spoofed-agent"},
        )

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc")) == ["body", "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_analysis_failed_missing_auth_returns_401(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """No Authorization header on the failure endpoint -> 401."""
    agent, _ = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, token=None) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout"})

    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analysis_missing_auth_returns_401(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """No Authorization header -> 401 (HTTPBearer auto_error)."""
    agent, _ = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, token=None) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 120.0})

    assert r.status_code == 401


@pytest.mark.asyncio
async def test_analysis_unknown_token_returns_403(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """Well-formed bearer with unknown hash -> 403 (auth dep doesn't leak agent existence)."""
    agent, _ = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, token="phaze_agent_unknown-token-1234") as ac:  # noqa: S106 -- test fixture, not a real secret
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 120.0})

    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Phase 45 (L-02): agent-stage scheduling-ledger clear on the control-side callbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analysis_put_success_clears_ledger(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A successful PUT clears process_file:<file_id> in the same transaction as ANALYZED."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"process_file:{file_id}"
    await _seed_ledger(session, key, "process_file", file_id)
    assert await _ledger_present(session, key)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 128.0})

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key), "successful analyze callback must clear the ledger row"


@pytest.mark.asyncio
async def test_analysis_put_overflowing_coverage_count_422s_without_stranding_the_ledger(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-01gh: an int32-overflowing coverage count is rejected 422 BEFORE the pg_insert runs.

    In the pre-fix code, ``fine_windows_total=2147483648`` reached ``pg_insert(AnalysisResult)`` and
    Postgres raised ``NumericValueOutOfRange``, aborting the transaction BEFORE `clear_ledger_entry`
    (agent_analysis.py) ran -- leaving ``process_file:<file_id>`` in the ledger, which the recovery
    sweep re-enqueues, re-running the SAME failing analysis forever (a poison loop). The schema bound
    now rejects the value before the handler body -- and therefore before any transaction -- ever
    runs, so the pre-existing ledger row is left EXACTLY as it was (not cleared, not corrupted) for a
    future corrected retry to clear normally, and no partial/bad AnalysisResult row is ever written.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"process_file:{file_id}"
    await _seed_ledger(session, key, "process_file", file_id)
    assert await _ledger_present(session, key)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"fine_windows_total": 2147483648})

    assert r.status_code == 422, r.text
    assert "fine_windows_total" in r.text
    assert await _ledger_present(session, key), "a rejected (422) PUT must not strand or clear the ledger row"

    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()
    assert row is None, "a rejected (422) PUT must not persist any AnalysisResult row"


@pytest.mark.asyncio
async def test_analysis_failed_clears_ledger_poison_case(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """POST /{file_id}/failed clears process_file:<file_id> -- locked decision #1, the poison case."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"process_file:{file_id}"
    await _seed_ledger(session, key, "process_file", file_id)
    assert await _ledger_present(session, key)

    async with _make_client(session, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout"})

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key), "terminal-failure callback must clear the ledger row (no recovery re-queue)"


@pytest.mark.asyncio
async def test_analysis_put_clear_is_noop_when_ledger_absent(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A success callback with NO ledger row (e.g. a re-delivered callback) still returns 200."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"process_file:{file_id}"
    assert not await _ledger_present(session, key)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 128.0})

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key)


@pytest.mark.asyncio
async def test_analysis_put_clear_uses_path_file_id_not_redirected(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The clear key uses the PATH file_id; another file's ledger row is untouched (T-45-05)."""
    agent, raw_token = seed_test_agent
    file_a = await _seed_file(session, agent.id)
    file_b = await _seed_file(session, agent.id)
    key_a = f"process_file:{file_a}"
    key_b = f"process_file:{file_b}"
    await _seed_ledger(session, key_a, "process_file", file_a)
    await _seed_ledger(session, key_b, "process_file", file_b)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_a}", json={"bpm": 128.0})

    assert r.status_code == 200, r.text
    assert not await _ledger_present(session, key_a), "the PATH file_a ledger row must be cleared"
    assert await _ledger_present(session, key_b), "another file's ledger row must NOT be redirected/cleared"


# ---------------------------------------------------------------------------
# Phase 83 (D-14): awaiting-cloud_job reaper at both analyze-terminal seams
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analysis_put_reaps_awaiting_cloud_job(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A successful PUT reaps the file's inert `awaiting` cloud_job hold-over row (D-14)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, CloudJobStatus.AWAITING)
    assert await _cloud_job_present(session, file_id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file_id}", json={"bpm": 128.0})

    assert r.status_code == 200, r.text
    assert not await _cloud_job_present(session, file_id), "successful analyze callback must reap the awaiting cloud_job row"


@pytest.mark.asyncio
async def test_analysis_failed_reaps_awaiting_cloud_job(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A terminal-failure POST reaps the file's inert `awaiting` cloud_job hold-over row (D-14)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, CloudJobStatus.AWAITING)
    assert await _cloud_job_present(session, file_id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout"})

    assert r.status_code == 200, r.text
    assert not await _cloud_job_present(session, file_id), "terminal-failure callback must reap the awaiting cloud_job row"


@pytest.mark.asyncio
async def test_analysis_failed_leaves_succeeded_cloud_job(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A SUCCEEDED cloud_job row (cloud-analyzed file) is NOT touched by the terminal reaper (D-14)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, CloudJobStatus.SUCCEEDED)
    assert await _cloud_job_present(session, file_id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout"})

    assert r.status_code == 200, r.text
    assert await _cloud_job_present(session, file_id), "the status='awaiting' filter must leave a SUCCEEDED cloud_job row in place"


@pytest.mark.asyncio
async def test_analysis_failed_leaves_running_cloud_job(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A RUNNING cloud_job row (cloud-analyzed file) is NOT touched by the terminal reaper (D-14)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    await _seed_cloud_job(session, file_id, CloudJobStatus.RUNNING)
    assert await _cloud_job_present(session, file_id)

    async with _make_client(session, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout"})

    assert r.status_code == 200, r.text
    assert await _cloud_job_present(session, file_id), "the status='awaiting' filter must leave a RUNNING cloud_job row in place"
