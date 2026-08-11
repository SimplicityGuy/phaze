"""Shared async subprocess driver for essentia analysis (Phase 101, phaze-bo3p.2).

The single parent-side entry BOTH lanes call — the one-shot pod (``phaze.job_runner``)
and the SAQ worker (``phaze.tasks.functions``) — to run ``analyze_file`` in a real
child process (``python -m phaze.analysis_child``). Because essentia's C++ holds the
GIL of the process it runs in, moving it out of the parent keeps the parent's asyncio
event loop free: ``progress_cb`` fires ON the loop as protocol lines arrive, so
progress POSTs go out mid-analysis (the OBS-03 fix for the 0→100% bar jump).

Responsibilities:
- Spawn the child with a fixed list argv (never a shell — the push.py convention;
  S603/B603-clean) and PIPE stdout/stderr.
- Parse the stdout JSONL protocol: ``progress`` lines → ``progress_cb(analyzed,
  total)`` (guarded — a callback error never kills the pump); ``heartbeat`` lines →
  the stall watchdog (and ``heartbeat_cb``); the terminal ``result``/``error`` line
  decides the outcome.
- Frame every child stderr line into structlog (``analysis_child_output``) — this is
  where essentia's C++ banners land after the child's fd 1 → fd 2 re-route, closing
  the banner-capture TODO deferred from Phase 100.
- Kill the child on stall, cancellation, or ANY other exceptional exit
  (``proc.kill()`` + ``await proc.wait()``) so no orphan analysis process survives
  its parent's interest.

D-08 DECISION RECORD — LIVENESS IS PROGRESS-BASED, NEVER WALL-CLOCK (phaze-w55w1)
---------------------------------------------------------------------------------
This driver used to take a ``timeout`` and SIGKILL the child at
``analysis_inner_timeout_sec`` (6 600 s) on the SAQ lane, with no bound at all on the
burst lane. Both were wrong for the same reason, and the repo has the incident to prove
it: ``phaze-1b39`` imposed a 3 h ``activeDeadlineSeconds`` and SIGTERM'd legitimate 2-6
hour concert-set analyses, burned ``cloud_submit_max_attempts``, and stalled the whole
burst lane (2026-07-28). **A wall clock cannot distinguish a long analysis from a hang.**
Exhaustive analysis (ADR-0007 §7) makes that fatal rather than merely wasteful: a
multi-hour set now legitimately runs for many hours, so any elapsed-time bound low enough
to catch a hang is also low enough to kill real work.

The replacement is a STALL watchdog. The child heartbeats every unit of progress it makes
— fine and coarse window completions, chunk decode boundaries, each coarse model sweep —
and this driver kills it only when NO such line has arrived for ``stall_timeout`` seconds.
A job that keeps producing windows runs to completion however long that takes; a job that
produces nothing dies in bounded time with a real, storable reason. The threshold is
``analysis_stall_timeout_sec`` (config), sized against the longest legitimately silent
stretch — one chunk's decode on a 12-hour file — not against total analysis time.

Any child output resets the deadline, not just protocol lines: a child mid-essentia-banner
is demonstrably alive, and treating an unparsed stderr line as silence would kill a
healthy job for a cosmetic reason.

Error contract (chosen to slot into the lanes' existing terminal handling):
- stall              → :class:`AnalysisStalledError`, a ``TimeoutError`` subclass, so the
  lanes' existing ``except TimeoutError`` terminal handling ("timeout" failure reason, no
  blind re-run) applies unchanged while the stored message says stall, not elapsed time
- child crash / nonzero exit / malformed protocol → :class:`AnalysisSubprocessError`
  carrying the exit code and a stderr tail (replaces pebble's ``ProcessExpired``)
- cancellation       → re-raised ``asyncio.CancelledError`` after the child is reaped

This module imports neither essentia nor the DB — it stays inside the pod's
import boundary (tests/shared/core/test_task_split.py).
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import json
import sys
from typing import TYPE_CHECKING, Any

import structlog


if TYPE_CHECKING:
    from collections.abc import Callable


log = structlog.get_logger(__name__)


_CHILD_MODULE = "phaze.analysis_child"
# StreamReader line limit for BOTH pipes. The terminal result line carries the full
# windows payload (fine + coarse windows incl. per-window features), which can run to
# megabytes for a long file — far past asyncio's 64 KiB default readline limit.
_STREAM_LIMIT = 1 << 25  # 32 MiB
# How many trailing child-stderr lines ride an AnalysisSubprocessError for diagnosis.
_STDERR_TAIL_LINES = 20
_STDERR_LINE_MAX = 500
# How often the stall watchdog wakes to compare "now" against the last activity stamp.
# Bounded so a very short stall_timeout (tests) is still enforced promptly, and capped as a
# fraction of the threshold so a production-sized threshold costs a handful of wakeups an hour.
_WATCHDOG_MIN_TICK_S = 0.05
_WATCHDOG_TICK_FRACTION = 0.1


class AnalysisSubprocessError(RuntimeError):
    """Terminal analysis-child failure: crash, nonzero exit, or malformed protocol.

    Carries ``exit_code`` (None when the child never exited cleanly under our watch)
    and ``stderr_tail`` — the last framed stderr lines — so the lane's failure report
    can say WHAT the child printed as it died without re-running anything.
    """

    def __init__(self, message: str, *, exit_code: int | None = None, stderr_tail: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class AnalysisStalledError(TimeoutError):
    """The analysis child stopped reporting progress and was killed (phaze-w55w1).

    Subclasses ``TimeoutError`` on purpose: both lanes already route ``TimeoutError`` to
    their terminal, non-retried failure handling ("no blind re-run of a file that could not
    finish", T-43-08), and that disposition is still exactly right for a stalled child. What
    changes is the EVIDENCE, not the handling — ``str(exc)`` names the stall threshold and
    the last stage the child reported, so the stored ``error_message`` says why the file
    failed instead of merely how long it ran.
    """

    def __init__(self, message: str, *, stall_timeout: float, last_stage: str | None = None) -> None:
        super().__init__(message)
        self.stall_timeout = stall_timeout
        self.last_stage = last_stage


def _build_argv(
    file_path: str,
    models_dir: str,
    *,
    fine_window_sec: int | None,
    coarse_window_sec: int | None,
    fine_min_sec: int | None,
) -> list[str]:
    """Fixed list argv for the child. Only provided windowing overrides become flags,
    so ``analyze_file``'s own defaults stay authoritative for absent ones."""
    argv = [sys.executable, "-m", _CHILD_MODULE, file_path, "--models-dir", models_dir]
    for flag, value in (
        ("--fine-window-sec", fine_window_sec),
        ("--coarse-window-sec", coarse_window_sec),
        ("--fine-min-sec", fine_min_sec),
    ):
        if value is not None:
            argv.extend((flag, str(value)))
    return argv


async def run_analysis_subprocess(
    file_path: str,
    models_dir: str,
    *,
    fine_window_sec: int | None = None,
    coarse_window_sec: int | None = None,
    fine_min_sec: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    stall_timeout: float | None = None,
) -> dict[str, Any]:
    """Run one exhaustive analysis in the child CLI; return the ``analyze_file`` dict.

    ``progress_cb(analyzed, total)`` is invoked on the event loop per protocol
    progress line (guarded: an exception inside it is logged and swallowed, mirroring
    the lanes' progress-never-fails-the-job contract). ``heartbeat_cb(stage, done, total)``
    is the same contract for the liveness channel — lanes use it to touch their own
    supervisor (e.g. the SAQ job heartbeat) without inventing a second protocol.

    ``stall_timeout`` bounds SILENCE, not runtime (D-08): the child is killed only after
    that many seconds with no output of any kind, and a job that keeps reporting progress
    runs to completion no matter how long it takes. ``stall_timeout=None`` disables the
    watchdog entirely — no bound at all, which is what the pre-phaze-w55w1 burst lane had
    and is kept only for callers that genuinely supervise the child by other means.
    """
    argv = _build_argv(
        file_path,
        models_dir,
        fine_window_sec=fine_window_sec,
        coarse_window_sec=coarse_window_sec,
        fine_min_sec=fine_min_sec,
    )
    try:
        # Fixed list argv, no shell (push.py convention): neither ruff S603 nor bandit
        # B603 flags create_subprocess_exec with a list argv.
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
    except FileNotFoundError as exc:
        # sys.executable vanished out from under us — a broken venv, not a job failure.
        msg = f"analysis child interpreter {sys.executable!r} not found"
        raise RuntimeError(msg) from exc

    if proc.stdout is None or proc.stderr is None:  # pragma: no cover - PIPEs requested above
        proc.kill()
        msg = "analysis child spawned without stdout/stderr pipes"
        raise RuntimeError(msg)
    child_stdout = proc.stdout
    child_stderr = proc.stderr

    result: dict[str, Any] | None = None
    error_message: str | None = None
    stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    loop = asyncio.get_running_loop()
    last_activity = loop.time()
    last_stage: str | None = None

    def _touch() -> None:
        # ANY child output proves liveness (D-08) -- protocol lines and essentia's own
        # stderr banners alike. Called from both pumps.
        nonlocal last_activity
        last_activity = loop.time()

    def _safe_progress(analyzed: int, total: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(analyzed, total)
        except Exception:  # the lanes' contract: a progress error never fails the analysis
            log.debug("analysis_progress_cb_error", file=file_path)

    def _safe_heartbeat(stage: str, done: int, total: int) -> None:
        if heartbeat_cb is None:
            return
        try:
            heartbeat_cb(stage, done, total)
        except Exception:  # same contract as progress: liveness reporting never fails the job
            log.debug("analysis_heartbeat_cb_error", file=file_path)

    async def _pump_stdout() -> None:
        nonlocal result, error_message, last_stage
        async for raw in child_stdout:
            _touch()
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                # After the child's fd re-route nothing but protocol should reach this
                # pipe; garbage is logged (bounded) and skipped — the terminal
                # result/error line still decides the outcome.
                log.warning("analysis_child_protocol_garbage", line=line[:_STDERR_LINE_MAX])
                continue
            if not isinstance(message, dict):
                # Valid JSON but not a protocol object ("null", "42", "[1]"): same
                # garbage discipline as non-JSON lines. Calling .get on these raised
                # and orphaned the child (phaze-702y).
                log.warning("analysis_child_protocol_garbage", line=line[:_STDERR_LINE_MAX])
                continue
            kind = message.get("type")
            if kind == "progress":
                _safe_progress(int(message.get("analyzed", 0)), int(message.get("total", 0)))
            elif kind == "heartbeat":
                last_stage = str(message.get("stage", ""))
                _safe_heartbeat(last_stage, int(message.get("done", 0)), int(message.get("total", 0)))
            elif kind == "result" and isinstance(message.get("result"), dict):
                result = message["result"]
            elif kind == "error":
                error_message = str(message.get("message", "unknown analysis child error"))
            else:
                log.warning("analysis_child_protocol_garbage", line=line[:_STDERR_LINE_MAX])

    async def _pump_stderr() -> None:
        # essentia's C++ banners (and any stray child prints) arrive here via the
        # child's fd 1 → fd 2 re-route; frame each line as a structured log event so
        # the pod console shows them as attributed child output, never as app lines.
        async for raw in child_stderr:
            _touch()
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            stderr_tail.append(line[:_STDERR_LINE_MAX])
            log.info("analysis_child_output", line=line[:_STDERR_LINE_MAX])

    async def _drive() -> int:
        await asyncio.gather(_pump_stdout(), _pump_stderr())
        return await proc.wait()

    async def _kill_and_reap() -> None:
        if proc.returncode is None:
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()

    async def _watchdog(threshold: float) -> None:
        """Sleep-and-compare stall detector (D-08). Returns only when the child has gone
        ``threshold`` seconds without ANY output; the caller then kills and reports."""
        tick = max(_WATCHDOG_MIN_TICK_S, threshold * _WATCHDOG_TICK_FRACTION)
        while True:
            idle = loop.time() - last_activity
            if idle >= threshold:
                return
            # Floored at the minimum tick so a deadline a hair away cannot busy-spin the loop.
            await asyncio.sleep(max(_WATCHDOG_MIN_TICK_S, min(tick, threshold - idle)))

    async def _settle(*tasks: asyncio.Future[Any]) -> None:
        """Cancel every unfinished task and AWAIT them all before returning.

        Awaiting is the load-bearing half. A cancelled-but-unawaited task is still scheduled:
        it resumes on a later loop iteration, and until it does, the pumps it owns keep running
        -- reading a child this function's caller is about to reap, and invoking ``progress_cb``
        / ``heartbeat_cb`` for a job nobody is waiting on any more. On the SAQ lane those
        callbacks touch the job's heartbeat, so an orphaned pump reports a dead analysis as
        alive. ``return_exceptions=True`` because a pump exception on the way out must not
        displace the outcome (stall, cancellation) that is already propagating.
        """
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _drive_with_watchdog(threshold: float) -> int:
        drive = asyncio.ensure_future(_drive())
        watch = asyncio.ensure_future(_watchdog(threshold))
        try:
            done, _pending = await asyncio.wait({drive, watch}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            # The CALLER was cancelled while we were waiting (SAQ job cancellation, pod
            # teardown). `asyncio.wait` does not cancel what it was waiting on, so BOTH tasks
            # -- crucially `drive`, which owns the pumps -- must be settled here or they outlive
            # the reap. Cancelling only the watchdog (the original shape) left the pumps live.
            await _settle(drive, watch)
            raise
        await _settle(watch)
        if drive in done:
            return drive.result()
        # The watchdog fired: the child is silent past the threshold. Settle the pumps and
        # let the caller's handler kill + reap, so there is exactly one kill path.
        await _settle(drive)
        raise AnalysisStalledError(
            f"analysis child stalled: no progress for {threshold}s (last stage: {last_stage or 'none reported'})",
            stall_timeout=threshold,
            last_stage=last_stage,
        )

    try:
        returncode = await (_drive_with_watchdog(stall_timeout) if stall_timeout is not None else _drive())
    except AnalysisStalledError:
        await _kill_and_reap()
        log.warning("analysis_child_stalled", file=file_path, stall_timeout=stall_timeout, last_stage=last_stage)
        raise
    except asyncio.CancelledError:
        # The caller lost interest (SAQ job cancellation / pod teardown): reap the
        # child so no orphan essentia process keeps burning CPU, then propagate.
        await _kill_and_reap()
        raise
    except BaseException:
        # phaze-702y: ANY other escape from the pump/drive — e.g. a readline ValueError
        # when a single protocol line exceeds _STREAM_LIMIT — must honor the same
        # no-orphan contract as timeout/cancellation before it propagates.
        await _kill_and_reap()
        raise

    if returncode == 0 and result is not None:
        return result

    tail = tuple(stderr_tail)
    if error_message is not None:
        msg = f"analysis child failed (exit {returncode}): {error_message}"
    elif returncode != 0:
        msg = f"analysis child exited {returncode} without a result"
    else:
        msg = "analysis child exited 0 without a result line (malformed protocol)"
    raise AnalysisSubprocessError(msg, exit_code=returncode, stderr_tail=tail)
