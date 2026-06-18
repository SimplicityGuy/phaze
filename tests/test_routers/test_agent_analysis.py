"""Contract tests for PUT /api/internal/agent/analysis/{file_id} (Phase 26 D-26).

Mirrors `tests/test_routers/test_agent_metadata.py` exactly: smoke-app pattern,
seed FileRecord for FK satisfaction, expire_all to bypass session cache between
PUTs. Covers happy path, idempotent replay, partial-PUT field-level LWW (CR-01
invariant), empty-body no-op for existing rows, first-PUT-with-empty-body
creates a row, 422 on extra fields (D-16 / AUTH-01 spoof block), and the auth
401/403 surface from `Depends(get_authenticated_agent)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.file import FileRecord, FileState
from phaze.routers.agent_analysis import router as agent_analysis_router


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
            state=FileState.DISCOVERED,
        )
    )
    await session.commit()
    return file_id


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


@pytest.mark.asyncio
async def test_analysis_put_advances_state_and_persists_coverage_columns(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Phase 43: a non-empty PUT advances files.state to 'analyzed' and lands coverage in dedicated columns (not features)."""
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

    file_row = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    assert file_row.state == FileState.ANALYZED, "non-empty PUT must advance state to analyzed"

    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
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
    file_row = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    assert file_row.state == FileState.DISCOVERED, "empty PUT must NOT advance state"


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
async def test_analysis_failed_sets_state(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """POST /{file_id}/failed advances files.state to 'analysis_failed' and echoes agent_id/file_id."""
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
    file_row = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    assert file_row.state == FileState.ANALYSIS_FAILED


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
    file_row = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    assert file_row.state == FileState.DISCOVERED, "a 422 body must not advance state"


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
