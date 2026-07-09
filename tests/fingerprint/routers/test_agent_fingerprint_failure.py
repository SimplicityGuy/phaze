"""FAIL-04 (Phase 81, D-18): the fingerprint failure asymmetry is a regression lock, not a new writer.

`report_fingerprint_failed` persists NO `fingerprint_results` row -- it only clears the
`fingerprint_file:<file_id>` ledger row. The durable, auto-retryable per-engine failure marker is
the `fingerprint_results.status='failed'` row already written by `put_fingerprint`. A synthetic
`engine='_task'` sentinel row would poison the two aliased per-engine outer-joins at
`services/pipeline.py:939-940` that feed `_trackid_engine_badge` (`services/pipeline.py:864`), so this
suite asserts no synthetic-engine row is EVER written and that a FAILED fingerprint stays eligible
(FAILURE_IS_TERMINAL[fingerprint] = False -- ELIG-04).

Uses the inline smoke-app + authed-agent client pattern from test_agent_fingerprint.py; parallel-safe
and independent of main.py wiring. Must pass in the `fingerprint` bucket in isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import func as sa_func, select

from phaze.database import get_session
from phaze.enums.stage import Stage, Status, eligible, resolve_status
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_fingerprint import router as agent_fingerprint_router
from phaze.services.scheduling_ledger import upsert_ledger_entry


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


# The synthetic sentinel that MUST NEVER be written -- it would poison the two aliased per-engine
# joins at services/pipeline.py:939-940 (audfprint / panako) and _trackid_engine_badge at :864.
_SENTINEL_ENGINE = "_task"


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that wires the agent_fingerprint router (mirrors test_agent_fingerprint.py)."""
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
            state=FileState.DISCOVERED,
        )
    )
    await session.commit()
    return file_id


async def _seed_ledger(session: AsyncSession, key: str, file_id: uuid.UUID) -> None:
    await upsert_ledger_entry(session, key=key, function="fingerprint_file", kwargs={"file_id": str(file_id)})
    await session.commit()


async def _ledger_present(session: AsyncSession, key: str) -> bool:
    session.expire_all()
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none()
    return row is not None


async def _fingerprint_row_count(session: AsyncSession, file_id: uuid.UUID) -> int:
    session.expire_all()
    return (await session.execute(select(sa_func.count()).select_from(FingerprintResult).where(FingerprintResult.file_id == file_id))).scalar_one()


# ---------------------------------------------------------------------------
# (a) report_fingerprint_failed persists NO fingerprint_results row -- ledger-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_failed_persists_no_fingerprint_row(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """FAIL-04/D-18: the row count is identical before and after report_fingerprint_failed; only the ledger clears.

    Seeds ONE real per-engine failed row (via put_fingerprint) so the count is non-zero, proving the
    terminal ack neither adds NOR removes a `fingerprint_results` row -- it is a pure ledger clear.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    key = f"fingerprint_file:{file_id}"

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        # A real per-engine failed row is the durable marker (written by put_fingerprint, not the ack).
        w = await ac.put(f"/api/internal/agent/fingerprints/{file_id}/audfprint", json={"status": "failed", "error_message": "boom"})
        assert w.status_code == 200, w.text

    # Seed the ledger AFTER put_fingerprint: a successful engine PUT clears the fingerprint_file ledger
    # row, so we re-seed it here to prove report_fingerprint_failed is what clears it in this scenario.
    await _seed_ledger(session, key, file_id)
    count_before = await _fingerprint_row_count(session, file_id)
    assert count_before == 1
    assert await _ledger_present(session, key)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/fingerprints/{file_id}/failed")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is True
    count_after = await _fingerprint_row_count(session, file_id)
    assert count_after == count_before, "report_fingerprint_failed must persist NO fingerprint_results row (D-18)"
    assert not await _ledger_present(session, key), "the terminal ack's SOLE persistent effect is clearing the ledger row"


@pytest.mark.asyncio
async def test_report_failed_writes_nothing_when_no_prior_rows(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """With zero prior fingerprint rows, report_fingerprint_failed leaves the count at zero (no synthetic marker)."""
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    assert await _fingerprint_row_count(session, file_id) == 0

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/fingerprints/{file_id}/failed")

    assert r.status_code == 200, r.text
    assert await _fingerprint_row_count(session, file_id) == 0, "no fingerprint_results row is created by the terminal ack"


# ---------------------------------------------------------------------------
# (b) a status='failed' per-engine row keeps the file eligible (auto-retryable, ELIG-04)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_fingerprint_row_stays_eligible(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A per-engine status='failed' row (the durable marker) keeps eligible(FINGERPRINT) True (ELIG-04).

    End-to-end: put_fingerprint writes the failed row, resolve_status derives Status.FAILED from the
    persisted engine status, and eligible() confirms a failed fingerprint is NOT terminal -- it stays
    auto-retryable (FAILURE_IS_TERMINAL[fingerprint] = False), unlike analyze.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        w = await ac.put(f"/api/internal/agent/fingerprints/{file_id}/audfprint", json={"status": "failed", "error_message": "boom"})
        assert w.status_code == 200, w.text

    session.expire_all()
    engine_statuses = list((await session.execute(select(FingerprintResult.status).where(FingerprintResult.file_id == file_id))).scalars().all())
    assert engine_statuses == ["failed"]

    derived = resolve_status(Stage.FINGERPRINT, {"engine_statuses": engine_statuses, "inflight": False})
    assert derived is Status.FAILED
    assert eligible({Stage.FINGERPRINT: derived}, Stage.FINGERPRINT) is True


def test_failed_fingerprint_is_eligible_pure() -> None:
    """DB-free lock of the ELIG-04 predicate: a FAILED fingerprint stays eligible for auto-retry."""
    assert eligible({Stage.FINGERPRINT: Status.FAILED}, Stage.FINGERPRINT) is True


# ---------------------------------------------------------------------------
# (c) no synthetic engine='_task' sentinel is ever written -- the aliased per-engine joins stay unpoisoned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_failed_writes_no_sentinel_engine_row(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """T-81-04-01: no engine='_task' (or any synthetic sentinel) row exists after report_fingerprint_failed.

    A synthetic engine row would poison the two aliased per-engine outer-joins at
    services/pipeline.py:939-940 (audfprint / panako) feeding _trackid_engine_badge -- so the ack must
    never write one. Seeds two REAL per-engine rows first, then asserts the ack leaves exactly those
    two and introduces no sentinel engine.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        await ac.put(f"/api/internal/agent/fingerprints/{file_id}/audfprint", json={"status": "failed", "error_message": "boom"})
        await ac.put(f"/api/internal/agent/fingerprints/{file_id}/panako", json={"status": "success"})
        r = await ac.post(f"/api/internal/agent/fingerprints/{file_id}/failed")

    assert r.status_code == 200, r.text

    session.expire_all()
    engines = set((await session.execute(select(FingerprintResult.engine).where(FingerprintResult.file_id == file_id))).scalars().all())
    assert engines == {"audfprint", "panako"}, "only real per-engine rows exist; the aliased joins stay unpoisoned"

    sentinel_count = (
        await session.execute(select(sa_func.count()).select_from(FingerprintResult).where(FingerprintResult.engine == _SENTINEL_ENGINE))
    ).scalar_one()
    assert sentinel_count == 0, f"no synthetic engine='{_SENTINEL_ENGINE}' sentinel row may exist (T-81-04-01)"
