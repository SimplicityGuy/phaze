"""Tests for the shared analysis subprocess driver (Phase 101, phaze-bo3p.2).

Every test here runs the REAL ``python -m phaze.analysis_child`` subprocess — no
essentia wheel needed, because ``PHAZE_ANALYSIS_CHILD_TARGET`` points the child at the
``tests.analyze._child_stubs`` targets. That makes these integration tests of the full
parent↔child contract: spawn, protocol pump, stderr framing, stall/cancel kill.
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
from phaze.services.analysis_exec import AnalysisStalledError, AnalysisSubprocessError, run_analysis_subprocess
from tests.analyze._child_stubs import _result


if TYPE_CHECKING:
    from collections.abc import Iterator


_STUBS = "tests.analyze._child_stubs"
_REPO_ROOT = Path(__file__).resolve().parents[4]


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

    result = await run_analysis_subprocess("/fake/audio.mp3", "/fake/models", fine_min_sec=7, coarse_window_sec=120)

    echo = result["echo"]
    assert echo["fine_min_sec"] == 7
    assert echo["coarse_window_sec"] == 120
    # phaze-w55w1: `fine_cap` / `coarse_cap` are not merely absent here, they no longer exist
    # as flags at all -- the child's argparse would reject them.
    assert "fine_window_sec" not in echo


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


async def test_stalled_child_is_killed_and_raises_a_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that stops reporting progress is SIGKILLed at the stall threshold (phaze-w55w1).

    ``hang_analyze`` emits one beat then wedges for 300 s. The raised
    :class:`AnalysisStalledError` subclasses ``TimeoutError``, which is what both lanes already
    catch to record a terminal, non-retried failure — so the disposition of a wedged child is
    unchanged and only the evidence improves.
    """
    _point_child_at(monkeypatch, "hang_analyze")
    started = time.monotonic()

    with pytest.raises(AnalysisStalledError, match="stalled: no progress") as excinfo:
        await run_analysis_subprocess("/fake/audio.mp3", "/fake/models", stall_timeout=1.5)

    # The stored message must name the threshold AND the last stage the child reached -- that is
    # the whole point of storing a real error rather than "timeout".
    assert excinfo.value.stall_timeout == 1.5
    assert excinfo.value.last_stage == "fine"
    assert isinstance(excinfo.value, TimeoutError), "lane handlers catch TimeoutError; the subclass must stay one"
    # Bounded promptly by the stall threshold + kill, not by the stub's 300s hang.
    assert time.monotonic() - started < 10.0


async def test_slow_but_progressing_child_survives_far_past_the_stall_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE regression test for phaze-1b39 / ADR-0007 §7: elapsed time alone must never kill.

    ``crawling_analyze`` runs for ~2 s -- roughly 7x the 0.3 s stall threshold armed here -- but
    never goes quiet for more than ~0.05 s. Under the retired wall-clock bound this file died;
    under the stall watchdog it completes, which is exactly the behaviour a multi-hour concert
    set needs. The ratio, not the absolute durations, is what this test asserts.
    """
    _point_child_at(monkeypatch, "crawling_analyze")
    monkeypatch.setenv("PHAZE_STUB_BEAT_SEC", "0.05")
    monkeypatch.setenv("PHAZE_STUB_BEATS", "40")
    stall_timeout = 0.3
    beats: list[tuple[str, int, int]] = []
    started = time.monotonic()

    result = await run_analysis_subprocess(
        "/fake/<set-01>",
        "/fake/models",
        heartbeat_cb=lambda stage, done, total: beats.append((stage, done, total)),
        stall_timeout=stall_timeout,
    )
    elapsed = time.monotonic() - started

    assert result["fine_windows_analyzed"] == 3  # the stub's canned result: it ran to completion
    assert elapsed > stall_timeout * 3, f"the run ({elapsed:.2f}s) must outlast the stall threshold several times over"
    assert len(beats) == 40
    assert beats[-1] == ("fine", 40, 40)


async def test_stall_timeout_none_leaves_the_child_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """``stall_timeout=None`` arms no watchdog at all -- the pre-phaze-w55w1 burst-lane shape."""
    _point_child_at(monkeypatch, "crawling_analyze")
    monkeypatch.setenv("PHAZE_STUB_BEAT_SEC", "0.01")
    monkeypatch.setenv("PHAZE_STUB_BEATS", "5")

    result = await run_analysis_subprocess("/fake/audio.mp3", "/fake/models", stall_timeout=None)

    assert result["fine_windows_analyzed"] == 3


async def test_heartbeat_cb_error_never_fails_the_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising liveness callback is swallowed, like the progress one: reporting never kills a job."""
    _point_child_at(monkeypatch, "crawling_analyze")
    monkeypatch.setenv("PHAZE_STUB_BEAT_SEC", "0.01")
    monkeypatch.setenv("PHAZE_STUB_BEATS", "3")

    def _boom(_stage: str, _done: int, _total: int) -> None:
        msg = "heartbeat relay exploded"
        raise RuntimeError(msg)

    result = await run_analysis_subprocess("/fake/audio.mp3", "/fake/models", heartbeat_cb=_boom, stall_timeout=5.0)

    assert result["fine_windows_analyzed"] == 3


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


# ---------------------------------------------------------------------------
# Defensive-branch unit coverage: a scripted fake process stands in for the child,
# exercising protocol paths a REAL well-behaved child can never produce.
# ---------------------------------------------------------------------------


def _stream(lines: list[bytes]) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line)
    reader.feed_eof()
    return reader


class _FakeProc:
    """Minimal asyncio-subprocess stand-in: scripted pipes + a fixed return code."""

    def __init__(self, stdout_lines: list[bytes], stderr_lines: list[bytes], returncode: int) -> None:
        self.stdout = _stream(stdout_lines)
        self.stderr = _stream(stderr_lines)
        self._returncode = returncode
        self.returncode: int | None = None  # None while "running", like asyncio's Process
        self.kill_called = False

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self._returncode

    def kill(self) -> None:
        self.kill_called = True


def _fake_spawn(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> None:
    async def _spawn(*_argv: str, **_kwargs: object) -> _FakeProc:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)


async def test_protocol_garbage_and_blank_lines_are_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-JSON garbage, blank lines, and unknown message types are logged and skipped;
    the terminal result line still decides the outcome."""
    proc = _FakeProc(
        stdout_lines=[
            b"\n",
            b"not json at all\n",
            b'{"type": "mystery", "x": 1}\n',
            b'{"type": "progress", "analyzed": 1, "total": 2}\n',
            b'{"type": "result", "result": {"ok": true}}\n',
        ],
        stderr_lines=[b"\n", b"banner line\n"],
        returncode=0,
    )
    _fake_spawn(monkeypatch, proc)
    bumps: list[tuple[int, int]] = []

    with capture_logs() as captured:
        result = await run_analysis_subprocess("/f", "/m", progress_cb=lambda a, t: bumps.append((a, t)))

    assert result == {"ok": True}
    assert bumps == [(1, 2)]
    garbage = [entry for entry in captured if entry["event"] == "analysis_child_protocol_garbage"]
    assert len(garbage) == 2  # the non-JSON line and the unknown-type line; blanks are silent


async def test_scalar_json_lines_are_protocol_garbage_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid-JSON-but-non-dict lines ('null', '42', '[1]', '"x"') are logged and skipped
    like any other garbage — they must not raise past the pump and orphan the child
    (phaze-702y: ``.get`` on a scalar raised AttributeError/TypeError)."""
    proc = _FakeProc(
        stdout_lines=[
            b"null\n",
            b"42\n",
            b"[1]\n",
            b'"stray string"\n',
            b'{"type": "result", "result": {"ok": true}}\n',
        ],
        stderr_lines=[],
        returncode=0,
    )
    _fake_spawn(monkeypatch, proc)

    with capture_logs() as captured:
        result = await run_analysis_subprocess("/f", "/m")

    assert result == {"ok": True}
    garbage = [entry for entry in captured if entry["event"] == "analysis_child_protocol_garbage"]
    assert len(garbage) == 4
    assert not proc.kill_called  # the run completed normally; nothing to reap


async def test_unexpected_pump_exception_still_kills_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-timeout exception escaping the stdout pump (here a malformed progress count)
    must still go through the kill/reap path before propagating — no exception may leave
    the essentia child running (phaze-702y)."""
    proc = _FakeProc(
        stdout_lines=[b'{"type": "progress", "analyzed": "not-a-number", "total": 3}\n'],
        stderr_lines=[],
        returncode=0,
    )
    _fake_spawn(monkeypatch, proc)

    with pytest.raises(ValueError, match="not-a-number"):
        await run_analysis_subprocess("/f", "/m")

    assert proc.kill_called


async def test_over_limit_stdout_line_still_kills_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single stdout line past the StreamReader limit makes readline raise ValueError
    OUTSIDE the pump's json guard; that escape must also kill the child (phaze-702y's
    second escape site — the fake reader's 64 KiB default stands in for _STREAM_LIMIT)."""
    proc = _FakeProc(stdout_lines=[b"x" * (1 << 17)], stderr_lines=[], returncode=0)  # no newline, over the limit
    _fake_spawn(monkeypatch, proc)

    with pytest.raises(ValueError):
        await run_analysis_subprocess("/f", "/m")

    assert proc.kill_called


async def test_nonzero_exit_without_result_raises_with_stderr_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that dies without a terminal line surfaces its exit code and stderr tail."""
    proc = _FakeProc(stdout_lines=[], stderr_lines=[b"Segmentation fault\n"], returncode=139)
    _fake_spawn(monkeypatch, proc)

    with pytest.raises(AnalysisSubprocessError, match="exited 139 without a result") as excinfo:
        await run_analysis_subprocess("/f", "/m")

    assert excinfo.value.exit_code == 139
    assert excinfo.value.stderr_tail == ("Segmentation fault",)


async def test_clean_exit_without_result_line_is_malformed_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc 0 with no result line is a protocol violation, never a silent empty success."""
    proc = _FakeProc(stdout_lines=[b'{"type": "progress", "analyzed": 0, "total": 3}\n'], stderr_lines=[], returncode=0)
    _fake_spawn(monkeypatch, proc)

    with pytest.raises(AnalysisSubprocessError, match="without a result line"):
        await run_analysis_subprocess("/f", "/m")


async def test_missing_interpreter_is_a_clear_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vanished sys.executable (broken venv) surfaces as RuntimeError, not FileNotFoundError."""

    async def _spawn(*_argv: str, **_kwargs: object) -> _FakeProc:
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    with pytest.raises(RuntimeError, match="interpreter"):
        await run_analysis_subprocess("/f", "/m")


async def test_environment_reaches_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child inherits the parent env (how PHAZE_ANALYSIS_CHILD_TARGET works at all) —
    guard the assumption the whole stub scheme rests on."""
    _point_child_at(monkeypatch, "fake_analyze")
    assert os.environ[_TARGET_ENV] == f"{_STUBS}:fake_analyze"

    result = await run_analysis_subprocess("/fake/audio.mp3", "/fake/models")

    assert result["echo"]["models_dir"] == "/fake/models"


async def test_cancellation_mid_watchdog_stops_the_pumps_even_if_the_kill_does_not_land(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the driver must settle BOTH inner tasks, not just the watchdog (phaze-w55w1).

    ``asyncio.wait`` does not cancel what it was waiting on. The original shape cancelled only the
    watchdog in its ``finally``, leaving ``drive`` -- which owns both output pumps -- SCHEDULED
    past the reap. A cancelled-but-unawaited pump resumes on a later loop iteration and keeps
    invoking the callbacks; on the SAQ lane the heartbeat callback touches the SAQ job, so an
    abandoned analysis would go on reporting itself alive to the broker.

    **Why this test neuters ``kill``.** The obvious version of it -- cancel, then watch for a late
    beat -- passes against the BROKEN code, verified by mutation. Killing the child stops its
    output, so the orphaned pumps starve and finish quietly, and the guarantee looks like it holds
    when it is only being masked by a favourable race. Making ``Process.kill`` a no-op removes the
    mask: the child keeps emitting for the rest of its (short) run, so pumps that were merely
    abandoned rather than settled are directly observable as callbacks arriving after the caller's
    ``CancelledError``. That is also the real-world shape worth defending against -- a kill that is
    slow or does not land is exactly when an orphaned pump has time to matter.
    """
    _point_child_at(monkeypatch, "crawling_analyze")
    monkeypatch.setenv("PHAZE_STUB_BEAT_SEC", "0.02")
    monkeypatch.setenv("PHAZE_STUB_BEATS", "60")  # ~1.2s: outlives the cancel, ends the test promptly
    # No-op kill: the reap still awaits the child, but the child is not actually killed, so its
    # output continues and any surviving pump keeps calling back.
    monkeypatch.setattr(asyncio.subprocess.Process, "kill", lambda _self: None)
    beats: list[str] = []

    task = asyncio.ensure_future(
        run_analysis_subprocess(
            "/fake/audio.mp3",
            "/fake/models",
            heartbeat_cb=lambda stage, _d, _t: beats.append(stage),
            stall_timeout=30.0,  # generous: cancellation, not a stall, is the subject
        )
    )
    while len(beats) < 2:  # wait until the child is genuinely streaming into the pumps
        await asyncio.sleep(0.02)

    task.cancel()
    # Snapshot HERE, not after `await task`. `cancel()` is synchronous and callbacks only run on
    # the loop, so this count is exact -- whereas the reap itself awaits the child, and an
    # abandoned pump does its damage DURING that await. Sampling afterwards was the flaw that let
    # an earlier version of this test pass against the broken code.
    seen_at_cancel = len(beats)

    with pytest.raises(asyncio.CancelledError):
        await task
    # Plus extra turns, so a pump that resumes late is caught too.
    for _ in range(20):
        await asyncio.sleep(0.02)

    assert len(beats) == seen_at_cancel, f"{len(beats) - seen_at_cancel} callback(s) fired after cancellation: a pump outlived the reap"
