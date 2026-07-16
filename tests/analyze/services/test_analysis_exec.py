"""Tests for the shared analysis subprocess driver (Phase 101, phaze-bo3p.2).

Every test here runs the REAL ``python -m phaze.analysis_child`` subprocess — no
essentia wheel needed, because ``PHAZE_ANALYSIS_CHILD_TARGET`` points the child at the
``tests.analyze._child_stubs`` targets. That makes these integration tests of the full
parent↔child contract: spawn, protocol pump, stderr framing, timeout/cancel kill.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from phaze.analysis_child import _TARGET_ENV
from phaze.services.analysis_exec import AnalysisSubprocessError, run_analysis_subprocess
from tests.analyze._child_stubs import _result


if TYPE_CHECKING:
    from collections.abc import Iterator


_STUBS = "tests.analyze._child_stubs"
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _run_from_repo_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The child resolves ``tests.analyze._child_stubs`` via ``sys.path[0] == cwd``
    under ``python -m``, so pin the driver's inherited cwd to the repo root."""
    monkeypatch.chdir(_REPO_ROOT)
    yield


def _point_child_at(monkeypatch: pytest.MonkeyPatch, stub: str) -> None:
    monkeypatch.setenv(_TARGET_ENV, f"{_STUBS}:{stub}")


async def test_result_returned_intact_with_mid_run_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """The driver returns the child's result dict verbatim, and progress callbacks fire
    ON the parent loop WHILE the child is still running (the OBS-03 point)."""
    _point_child_at(monkeypatch, "slow_analyze")
    bumps: list[tuple[int, int]] = []
    first_bump_at: list[float] = []

    def _cb(analyzed: int, total: int) -> None:
        if not first_bump_at:
            first_bump_at.append(time.monotonic())
        bumps.append((analyzed, total))

    result = await run_analysis_subprocess("/fake/audio.mp3", "/fake/models", progress_cb=_cb)
    done_at = time.monotonic()

    assert bumps == [(0, 3), (1, 3), (2, 3), (3, 3)]
    # slow_analyze sleeps 0.15s after each bump: the first bump must have been observed
    # well before the child finished — streamed mid-run, not replayed at completion.
    assert done_at - first_bump_at[0] >= 0.4
    expected = json.loads(json.dumps(_result("/fake/audio.mp3", "/fake/models")))
    assert result == expected


async def test_windowing_overrides_forwarded_and_defaults_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only provided windowing kwargs become child flags; absent ones never reach the target."""
    _point_child_at(monkeypatch, "fake_analyze")

    result = await run_analysis_subprocess("/fake/audio.mp3", "/fake/models", fine_cap=7, coarse_window_sec=120)

    echo = result["echo"]
    assert echo["fine_cap"] == 7
    assert echo["coarse_window_sec"] == 120
    for absent in ("fine_window_sec", "fine_min_sec", "coarse_cap"):
        assert absent not in echo


async def test_child_stderr_is_framed_into_log_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw child fd-1/fd-2 output (essentia banners, stray prints) surfaces as
    ``analysis_child_output`` log events — framed, never leaked raw (OBS-03 capture)."""
    _point_child_at(monkeypatch, "noisy_analyze")

    with capture_logs() as captured:
        result = await run_analysis_subprocess("/fake/audio.mp3", "/fake/models")

    assert result["fine_windows_analyzed"] == 3
    framed = [entry["line"] for entry in captured if entry["event"] == "analysis_child_output"]
    assert any("MusicExtractor" in line for line in framed)
    assert any("stray print from the analysis child" in line for line in framed)


async def test_timeout_kills_the_child_and_raises_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged child is SIGKILLed at the inner timeout and surfaces as builtins
    TimeoutError — the same exception the pebble pool raised, so lane handlers keep working."""
    _point_child_at(monkeypatch, "hang_analyze")
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="timed out"):
        await run_analysis_subprocess("/fake/audio.mp3", "/fake/models", timeout=1.5)

    # Bounded promptly by the timeout + kill, not by the stub's 300s hang.
    assert time.monotonic() - started < 10.0


async def test_cancellation_reaps_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling the driver task kills the child before CancelledError propagates."""
    _point_child_at(monkeypatch, "hang_analyze")
    task = asyncio.ensure_future(run_analysis_subprocess("/fake/audio.mp3", "/fake/models"))
    await asyncio.sleep(1.0)  # let the child spawn and wedge

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_child_crash_raises_with_exit_code_and_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising analysis target surfaces as AnalysisSubprocessError with the child's
    error line and nonzero exit code (the ProcessExpired replacement)."""
    _point_child_at(monkeypatch, "crash_analyze")

    with pytest.raises(AnalysisSubprocessError) as excinfo:
        await run_analysis_subprocess("/fake/audio.mp3", "/fake/models")

    assert excinfo.value.exit_code == 1
    assert "RuntimeError: essentia exploded" in str(excinfo.value)


async def test_progress_cb_error_never_fails_the_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising progress callback is swallowed (logged) — the analysis still completes."""
    _point_child_at(monkeypatch, "fake_analyze")

    def _broken_cb(analyzed: int, total: int) -> None:
        msg = "progress consumer bug"
        raise ValueError(msg)

    result = await run_analysis_subprocess("/fake/audio.mp3", "/fake/models", progress_cb=_broken_cb)

    assert result["fine_windows_total"] == 3


async def test_environment_reaches_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child inherits the parent env (how PHAZE_ANALYSIS_CHILD_TARGET works at all) —
    guard the assumption the whole stub scheme rests on."""
    _point_child_at(monkeypatch, "fake_analyze")
    assert os.environ[_TARGET_ENV] == f"{_STUBS}:fake_analyze"

    result = await run_analysis_subprocess("/fake/audio.mp3", "/fake/models")

    assert result["echo"]["models_dir"] == "/fake/models"
