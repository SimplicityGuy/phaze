"""``phaze backfill set-projection`` (phaze-x1qr3.3): backfills energy/camelot/mood_scores +
set_profile from already-stored ``analysis_window`` JSONB, with no re-analysis.

DB-backed (unlike ``tests/analyze/cli/test_backfill.py``'s pure-wrapper split for the sibling
``reenqueue-incomplete-analyses`` command): the acceptance bar itself is about corpus behaviour
(fills 3 files, no-ops on a second run, re-fills only stale rows), which a stubbed service layer
cannot demonstrate. ``cli.async_session`` is monkeypatched to yield the real per-test ``session``
fixture (mirrors ``tests/analyze/cli/test_backfill.py``'s ``_stub_io`` shape, minus the stub) so the
CLI wrapper's own ``async with async_session() as session:`` reaches the real Postgres session the
seeded rows live in.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import select

from phaze import cli
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisWindow
from phaze.models.file import FileRecord
from phaze.models.set_profile import SetProfile
from phaze.services.set_projection import CAMELOT_TABLE
from phaze.services.set_projection_writer import CURRENT_PROJECTION_VERSION
from tests.analyze._real_result import real_analysis_result


_VALID_CAMELOT_CODES = frozenset(CAMELOT_TABLE.values())
"""See the sibling module docstring in test_analysis_persist_projection.py: the backfill must
never copy a raw stored `musical_key` string into `camelot` either."""


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
            file_size=4096,
        )
    )
    await session.flush()
    return file_id


async def _seed_windows(session: AsyncSession, file_id: uuid.UUID, *, fine_only: bool) -> None:
    """Seed real-shaped ``AnalysisWindow`` rows for ``file_id`` from the A7 real-payload fixture,
    exactly as they'd already exist from a PRIOR (pre-projection) analysis -- no ``energy`` /
    ``camelot`` / ``mood_scores``, matching every row this bead's backfill is meant to fill in."""
    result = real_analysis_result()
    windows = result["windows"]
    if fine_only:
        windows = [w for w in windows if w["tier"] == "fine"]
    for w in windows:
        session.add(
            AnalysisWindow(
                id=uuid.uuid4(),
                file_id=file_id,
                tier=w["tier"],
                window_index=w["window_index"],
                start_sec=w["start_sec"],
                end_sec=w["end_sec"],
                bpm=w.get("bpm"),
                musical_key=w.get("musical_key"),
                mood=w.get("mood"),
                style=w.get("style"),
                danceability=w.get("danceability"),
                features=w.get("features"),
            )
        )
    await session.flush()


@asynccontextmanager
async def _session_ctx(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield session


async def test_backfill_fills_a_corpus_is_idempotent_and_refills_only_stale_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], session: AsyncSession
) -> None:
    """The full acceptance bar in one seat: fills 3 files (one fine-only), no-ops on a re-run,
    and re-fills only the one row whose ``projection_version`` is later made stale."""
    monkeypatch.setattr(cli, "async_session", lambda: _session_ctx(session))

    agent_id = await _seed_agent(session)
    file_a = await _seed_file(session, agent_id=agent_id, sha256_hash="a" * 64)
    file_b = await _seed_file(session, agent_id=agent_id, sha256_hash="b" * 64)
    file_fine_only = await _seed_file(session, agent_id=agent_id, sha256_hash="c" * 64)
    await _seed_windows(session, file_a, fine_only=False)
    await _seed_windows(session, file_b, fine_only=False)
    await _seed_windows(session, file_fine_only, fine_only=True)
    await session.commit()

    exit_code = await cli._run_backfill_set_projection()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "3 file(s) scanned" in out
    assert "3 file(s) projected" in out
    assert "0 file(s) skipped (no windows)" in out
    assert "0 file(s) failed" in out

    session.expire_all()
    profiles = {
        row.file_id: row
        for row in (await session.execute(select(SetProfile).where(SetProfile.file_id.in_([file_a, file_b, file_fine_only])))).scalars()
    }
    assert set(profiles) == {file_a, file_b, file_fine_only}
    for file_id in (file_a, file_b):
        assert profiles[file_id].projection_version == CURRENT_PROJECTION_VERSION
        assert profiles[file_id].mean_vector is not None
        assert profiles[file_id].arc is not None
    # The fine-only file: camelot_modal is knowable from fine windows alone; mean_vector/arc/glyph
    # are NOT -- there are no coarse windows to derive them from (the design's "sources naming the
    # missing tier" case).
    fine_only_profile = profiles[file_fine_only]
    assert fine_only_profile.projection_version == CURRENT_PROJECTION_VERSION
    assert fine_only_profile.camelot_modal is not None
    assert fine_only_profile.mean_vector is None
    assert fine_only_profile.arc is None
    assert fine_only_profile.glyph is None

    windows_a = (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == file_a))).scalars().all()
    coarse_a = [w for w in windows_a if w.tier == "coarse"]
    fine_a = [w for w in windows_a if w.tier == "fine"]
    assert all(w.energy is not None and w.mood_scores is not None for w in coarse_a)
    assert all(w.camelot is None for w in coarse_a)
    assert all(w.camelot is not None for w in fine_a if w.musical_key is not None)
    # Reviewer finding (phaze-x1qr3.2): the backfill must never copy a raw `musical_key` string
    # into `camelot` -- only a table value or NULL, matching what `camelot_code` can produce.
    assert all(w.camelot in _VALID_CAMELOT_CODES for w in fine_a if w.camelot is not None)

    # Second run: every profile is already current -> a genuine no-op.
    exit_code_2 = await cli._run_backfill_set_projection()
    assert exit_code_2 == 0
    out_2 = capsys.readouterr().out
    assert "0 file(s) scanned" in out_2
    assert "0 file(s) projected" in out_2

    # Make ONE file's profile stale (simulates a projection_version bump) and confirm the third
    # run re-fills only that one row, never the two already-current ones.
    stale_profile = (await session.execute(select(SetProfile).where(SetProfile.file_id == file_b))).scalar_one()
    stale_profile.projection_version = 0
    await session.commit()

    exit_code_3 = await cli._run_backfill_set_projection()
    assert exit_code_3 == 0
    out_3 = capsys.readouterr().out
    assert "1 file(s) scanned" in out_3
    assert "1 file(s) projected" in out_3

    session.expire_all()
    refreshed = (await session.execute(select(SetProfile).where(SetProfile.file_id == file_b))).scalar_one()
    assert refreshed.projection_version == CURRENT_PROJECTION_VERSION


async def test_backfill_reports_zero_scanned_on_an_empty_corpus(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], session: AsyncSession
) -> None:
    """No files with windows at all -> a clean zero-count report, exit 0."""
    monkeypatch.setattr(cli, "async_session", lambda: _session_ctx(session))

    exit_code = await cli._run_backfill_set_projection()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "0 file(s) scanned" in out
    assert "0 file(s) failed" in out


def test_build_parser_accepts_backfill_set_projection() -> None:
    """``phaze backfill set-projection`` parses to the expected namespace (mirrors the sibling
    ``reenqueue-incomplete-analyses`` parser-acceptance test in ``tests/analyze/cli/test_backfill.py``)."""
    parser = cli._build_parser()

    args = parser.parse_args(["backfill", "set-projection"])

    assert args.group == "backfill"
    assert args.backfill_command == "set-projection"


def test_main_backfill_set_projection_dispatches_to_the_async_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main(["backfill", "set-projection"])`` reaches the async runner and returns its exit code."""
    calls: list[bool] = []

    async def _fake_runner() -> int:
        calls.append(True)
        return 0

    monkeypatch.setattr(cli, "_run_backfill_set_projection", _fake_runner)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    assert cli.main(["backfill", "set-projection"]) == 0
    assert calls == [True]


def test_main_backfill_set_projection_propagates_nonzero_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_runner() -> int:
        return 1

    monkeypatch.setattr(cli, "_run_backfill_set_projection", _fake_runner)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    assert cli.main(["backfill", "set-projection"]) == 1
