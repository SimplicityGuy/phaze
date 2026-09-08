"""Unit-level tests for ``services/set_projection_backfill.py`` (phaze-x1qr3.3).

Complements the corpus-level acceptance test (``tests/shared/cli/test_backfill_projection.py``,
which drives the CLI wrapper end to end and covers the resume-by-``projection_version`` branch and
the fine-only case): this module targets the branches a happy-path corpus run cannot reach on its
own -- a file with no windows at all, ONE file's processing raising mid-run, and the periodic
progress-log line -- with real assertions on the reported counts and log events, not bare calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from sqlalchemy import select
import structlog

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisWindow
from phaze.models.file import FileRecord
from phaze.models.set_profile import SetProfile
from phaze.services import set_projection_backfill as backfill_module
from phaze.services.set_projection_backfill import backfill_one_file, run_backfill


if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_agent(session: AsyncSession) -> str:
    agent_id = f"test-agent-{uuid.uuid4().hex[:8]}"
    session.add(Agent(id=agent_id, name=agent_id, token_hash="0" * 64, scan_roots=["/test/music"]))
    await session.flush()
    return agent_id


async def _seed_file(session: AsyncSession, *, agent_id: str, sha256_hash: str) -> uuid.UUID:
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash=sha256_hash,
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.flush()
    return file_id


async def _seed_one_fine_window(session: AsyncSession, file_id: uuid.UUID) -> None:
    """One minimal fine-tier window -- enough for `annotate_window_orm_objects` + `build_profile`
    to run for real without needing the full real-payload fixture (this module is about backfill
    ORCHESTRATION branches, not projection math, which is covered elsewhere against real data)."""
    session.add(
        AnalysisWindow(
            id=uuid.uuid4(),
            file_id=file_id,
            tier="fine",
            window_index=0,
            start_sec=0.0,
            end_sec=30.0,
            bpm=120.0,
            musical_key="A minor",
        )
    )
    await session.flush()


async def test_backfill_one_file_returns_false_and_writes_nothing_when_the_file_has_no_windows(
    session: AsyncSession,
) -> None:
    """A file id with zero `analysis_window` rows is a no-op, not an error -- covers the branch a
    real corpus run structurally cannot reach (`select_files_needing_projection` only ever names
    files that already have at least one window)."""
    file_id = uuid.uuid4()

    result = await backfill_one_file(session, file_id)

    assert result is False
    profile = (await session.execute(select(SetProfile).where(SetProfile.file_id == file_id))).scalar_one_or_none()
    assert profile is None


async def test_run_backfill_isolates_one_files_failure_from_the_rest_of_the_corpus(monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
    """One file's ``backfill_one_file`` raising is caught, rolled back, counted and logged --
    the OTHER file in the same run still gets projected. Real DB state is asserted, not just the
    report: the failed file never gets a ``set_profile`` row and the good file does."""
    agent_id = await _seed_agent(session)
    good_file = await _seed_file(session, agent_id=agent_id, sha256_hash="d" * 64)
    bad_file = await _seed_file(session, agent_id=agent_id, sha256_hash="e" * 64)
    await _seed_one_fine_window(session, good_file)
    await _seed_one_fine_window(session, bad_file)
    await session.commit()

    real_backfill_one_file = backfill_module.backfill_one_file

    async def _flaky(session: AsyncSession, file_id: uuid.UUID) -> bool:
        if file_id == bad_file:
            msg = "synthetic backfill failure"
            raise RuntimeError(msg)
        return await real_backfill_one_file(session, file_id)

    monkeypatch.setattr(backfill_module, "backfill_one_file", _flaky)

    with structlog.testing.capture_logs() as logs:
        report = await run_backfill(session)

    assert report.files_scanned == 2
    assert report.files_projected == 1
    assert report.files_failed == 1

    session.expire_all()
    assert (await session.execute(select(SetProfile).where(SetProfile.file_id == good_file))).scalar_one_or_none() is not None
    assert (await session.execute(select(SetProfile).where(SetProfile.file_id == bad_file))).scalar_one_or_none() is None

    failures = [entry for entry in logs if entry["event"] == "set_projection_backfill_file_failed"]
    assert len(failures) == 1
    assert failures[0]["file_id"] == str(bad_file)
    assert failures[0]["log_level"] == "warning"


async def test_run_backfill_logs_progress_at_the_configured_cadence(session: AsyncSession) -> None:
    """``progress_every=1`` forces the periodic progress line on every scanned file -- otherwise
    unreachable in a small test corpus against the real default (500)."""
    agent_id = await _seed_agent(session)
    file_id = await _seed_file(session, agent_id=agent_id, sha256_hash="f" * 64)
    await _seed_one_fine_window(session, file_id)
    await session.commit()

    with structlog.testing.capture_logs() as logs:
        report = await run_backfill(session, progress_every=1)

    assert report.files_scanned == 1
    progress_events = [entry for entry in logs if entry["event"] == "set_projection_backfill_progress"]
    assert len(progress_events) == 1
    assert progress_events[0]["files_scanned"] == 1
    assert progress_events[0]["files_total"] == 1

    complete_events = [entry for entry in logs if entry["event"] == "set_projection_backfill_complete"]
    assert len(complete_events) == 1
    assert complete_events[0]["files_projected"] == 1
