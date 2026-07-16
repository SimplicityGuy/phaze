"""Phase 57.1 SPIKE -- de-risk the two load-bearing safety properties before the
production write path is locked (PLAN 57.1-01; consumed by Plan 04).

This module is a SPIKE harness. It touches NO production module under
``src/phaze/`` -- it exercises the EXISTING ``put_analysis`` replace path to
prove crash-mid-run idempotency.

Phase 101 (phaze-bo3p.4): the ``transport`` task group (pebble Manager-queue
drainer + child-side httpx comparison) was removed with the pebble ProcessPool
itself -- the exec'd analysis child (services.analysis_exec) superseded that
transport, and its parent/child contract is covered by
tests/analyze/services/test_analysis_exec.py. The surviving group:

* ``idempotent`` -- prove a file killed mid-analysis re-runs cleanly via the
  existing ``put_analysis`` ``file_id``-UQ replace path (PROG-02 / RESEARCH Q3):
  the final ``analysis`` row + ``analysis_window`` set is identical to an
  uninterrupted control run, the file reaches the ``ANALYZED`` derived status exactly once, and
  no duplicate/orphaned window rows remain. The Phase 32 re-enqueue done-predicate
  is state-only, so the partial row cannot mis-drive recovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import func, select

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.file import FileRecord
from phaze.routers.agent_analysis import router as agent_analysis_router
from phaze.tasks.reenqueue import _select_done_analyze_ids


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


# ===========================================================================
# Task 2 -- crash-mid-run idempotency on the put_analysis replace path
#           (group: idempotent; real Postgres via the `session` fixture)
# ===========================================================================


# Canonical "uninterrupted control" payload: the known-good final analysis a
# clean run produces. Five fine windows fully analyzed (sampled=False), plus the
# representative aggregates. The re-run after a crash must land byte-identical.
_CONTROL_PAYLOAD: dict = {
    "bpm": 128.0,
    "musical_key": "C minor",
    "mood": {"energetic": 0.9, "happy": 0.4},
    "style": {"electronic": 0.8, "house": 0.5},
    "danceability": 0.77,
    "fine_windows_analyzed": 5,
    "fine_windows_total": 5,
    "coarse_windows_analyzed": 2,
    "coarse_windows_total": 2,
    "sampled": False,
    "windows": [
        {"tier": "fine", "window_index": 0, "start_sec": 0.0, "end_sec": 30.0, "bpm": 128.0, "musical_key": "C minor"},
        {"tier": "fine", "window_index": 1, "start_sec": 30.0, "end_sec": 60.0, "bpm": 127.0, "musical_key": "C minor"},
        {"tier": "fine", "window_index": 2, "start_sec": 60.0, "end_sec": 90.0, "bpm": 129.0, "musical_key": "G major"},
        {"tier": "fine", "window_index": 3, "start_sec": 90.0, "end_sec": 120.0, "bpm": 128.0, "musical_key": "C minor"},
        {"tier": "fine", "window_index": 4, "start_sec": 120.0, "end_sec": 150.0, "bpm": 130.0, "musical_key": "A minor"},
        {"tier": "coarse", "window_index": 0, "start_sec": 0.0, "end_sec": 180.0, "mood": "energetic", "style": "electronic", "danceability": 0.77},
        {"tier": "coarse", "window_index": 1, "start_sec": 180.0, "end_sec": 360.0, "mood": "happy", "style": "house", "danceability": 0.71},
    ],
}


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Smoke app wiring ONLY the agent_analysis router (mirrors test_agent_analysis.py)."""
    app = FastAPI(title="spike-smoke", version="test")
    app.include_router(agent_analysis_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _make_client(session: AsyncSession, token: str) -> AsyncClient:
    app = _make_smoke_app(session)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"})


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    """Seed a FileRecord so the AnalysisResult.file_id FK is satisfiable."""
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
            file_size=4096,
        )
    )
    await session.commit()
    return file_id


async def _window_keys(session: AsyncSession, file_id: uuid.UUID) -> list[tuple[str, int]]:
    """Return the (tier, window_index) keys for a file's analysis_window rows, sorted."""
    session.expire_all()
    rows = (await session.execute(select(AnalysisWindow.tier, AnalysisWindow.window_index).where(AnalysisWindow.file_id == file_id))).all()
    return sorted((tier, idx) for tier, idx in rows)


async def _analysis_aggregates(session: AsyncSession, file_id: uuid.UUID) -> tuple:
    """Return the comparable aggregate columns of a file's analysis row."""
    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    return (
        row.bpm,
        row.musical_key,
        row.mood,
        row.style,
        row.fine_windows_analyzed,
        row.fine_windows_total,
        row.coarse_windows_analyzed,
        row.coarse_windows_total,
        row.sampled,
    )


async def _analyze_is_done(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """True iff the recovery done-predicate classifies the file as analyze-domain-complete.

    Phase 80 (READ-03) cut ``_select_done_analyze_ids`` over from a ``FileRecord.state`` read to the
    ledger-scoped ``domain_completed_clause(ANALYZE)`` derivation, so it now takes the fids to bind (a
    single-element list is enough for this single-file probe). A crash-partial analysis row has
    ``analysis_completed_at`` NULL (not done, not failed) -> not domain-complete; a completed rerun
    stamps ``analysis_completed_at`` -> done.
    """
    session.expire_all()
    done_ids = set((await session.scalars(_select_done_analyze_ids([file_id]))).all())
    return file_id in done_ids


@pytest.mark.asyncio
async def test_idempotent_crash_midrun_rerun_matches_control(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A file killed mid-analysis re-runs to a state byte-identical to an uninterrupted control.

    Asserts the four must_have truths:
      (a) final analysis aggregates == the uninterrupted-control values (file_id-UQ
          on_conflict_do_update overwrote the partial row);
      (b) the analysis_window set == the control set, matched by (tier, window_index),
          with NO duplicate and NO orphaned rows (delete-then-insert replace ran);
      (c) the file reaches ANALYZED (derived) exactly once (one analysis row, terminal state);
      (d) the state-only re-enqueue done-predicate classifies the file as done ONLY
          after the terminal re-run.
    """
    agent, raw_token = seed_test_agent

    # --- The CONTROL: a fresh file, single clean put_analysis (uninterrupted). ---
    control_id = await _seed_file(session, agent.id)
    async with _make_client(session, raw_token) as ac:
        resp = await ac.put(f"/api/internal/agent/analysis/{control_id}", json=_CONTROL_PAYLOAD)
    assert resp.status_code == 200, resp.text

    # --- The INTERRUPTED file: a non-terminal file carrying LEFTOVER partial state. ---
    crashed_id = await _seed_file(session, agent.id)
    # Mimic "previous completed-then-killed": a partial aggregate row (NULL bpm,
    # analyzed < total) + 3 stale fine windows, written WITHOUT flipping state.
    session.add(
        AnalysisResult(
            id=uuid.uuid4(),
            file_id=crashed_id,
            bpm=None,
            fine_windows_analyzed=3,
            fine_windows_total=10,
            sampled=True,
        )
    )
    for idx in range(3):
        session.add(
            AnalysisWindow(
                id=uuid.uuid4(), file_id=crashed_id, tier="fine", window_index=idx, start_sec=idx * 30.0, end_sec=(idx + 1) * 30.0, bpm=99.0
            )
        )
    await session.commit()

    # (d) BEFORE the re-run the file is non-terminal -> NOT analyze-done.
    assert await _analyze_is_done(session, crashed_id) is False
    # Leftover partial state is present pre-re-run (the thing the replace must overwrite).
    assert await _window_keys(session, crashed_id) == [("fine", 0), ("fine", 1), ("fine", 2)]

    # --- The clean RE-RUN: Phase 32 re-enqueues -> put_analysis with the good payload. ---
    async with _make_client(session, raw_token) as ac:
        resp = await ac.put(f"/api/internal/agent/analysis/{crashed_id}", json=_CONTROL_PAYLOAD)
    assert resp.status_code == 200, resp.text

    # (a) aggregates identical to the uninterrupted control run.
    assert await _analysis_aggregates(session, crashed_id) == await _analysis_aggregates(session, control_id)
    # The partial-row markers were overwritten (NULL bpm -> control bpm; sampled True -> False).
    crashed_aggs = await _analysis_aggregates(session, crashed_id)
    assert crashed_aggs[0] == 128.0  # bpm
    assert crashed_aggs[8] is False  # sampled

    # (b) window set identical to control, by (tier, window_index); no dup, no orphan.
    control_keys = await _window_keys(session, control_id)
    crashed_keys = await _window_keys(session, crashed_id)
    assert crashed_keys == control_keys
    assert len(crashed_keys) == len(_CONTROL_PAYLOAD["windows"])  # exactly the control count, no orphans
    assert len(crashed_keys) == len(set(crashed_keys))  # no duplicate (tier, window_index)
    # The three stale windows (bpm=99.0) are gone -- replace, not append.
    session.expire_all()
    stale = (
        await session.execute(
            select(func.count()).select_from(AnalysisWindow).where(AnalysisWindow.file_id == crashed_id, AnalysisWindow.bpm == 99.0)
        )
    ).scalar_one()
    assert stale == 0

    # (c) Phase 90 (D-09): completion derives from analysis_completed_at (not files.state), and there is
    #     exactly one analysis row (file_id UQ -> never duplicated).
    session.expire_all()
    analysis_rows = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == crashed_id))).scalars().all()
    assert len(analysis_rows) == 1
    assert analysis_rows[0].analysis_completed_at is not None

    # (d) AFTER the terminal re-run the file is analyze-done.
    assert await _analyze_is_done(session, crashed_id) is True


@pytest.mark.asyncio
async def test_idempotent_repeat_rerun_no_duplicate_windows(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """Replaying the identical put_analysis twice leaves the SAME single row + window set.

    Locks the counter/idempotency claim from a second angle: a double re-run (e.g.
    a retried job) does not duplicate analysis_window rows nor create a second
    analysis row -- the file_id-UQ upsert + delete-then-insert replace are
    idempotent under replay.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)

    async with _make_client(session, raw_token) as ac:
        first = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_CONTROL_PAYLOAD)
        second = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_CONTROL_PAYLOAD)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    keys = await _window_keys(session, file_id)
    assert len(keys) == len(_CONTROL_PAYLOAD["windows"])
    assert len(keys) == len(set(keys))  # no duplicates after replay

    session.expire_all()
    analysis_count = (await session.execute(select(func.count()).select_from(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert analysis_count == 1
