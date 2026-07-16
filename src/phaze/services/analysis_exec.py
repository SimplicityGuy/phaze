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
  total)`` (guarded — a callback error never kills the pump); the terminal
  ``result``/``error`` line decides the outcome.
- Frame every child stderr line into structlog (``analysis_child_output``) — this is
  where essentia's C++ banners land after the child's fd 1 → fd 2 re-route, closing
  the banner-capture TODO deferred from Phase 100.
- Kill the child on timeout or cancellation (``proc.kill()`` + ``await proc.wait()``)
  so no orphan analysis process survives its parent's interest.

Error contract (chosen to slot into the lanes' existing terminal handling):
- inner timeout      → ``TimeoutError``  (what the pebble pool raised on SIGKILL —
  ``process_file`` maps it to the ``"timeout"`` failure reason unchanged)
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


def _build_argv(
    file_path: str,
    models_dir: str,
    *,
    fine_window_sec: int | None,
    coarse_window_sec: int | None,
    fine_min_sec: int | None,
    fine_cap: int | None,
    coarse_cap: int | None,
) -> list[str]:
    """Fixed list argv for the child. Only provided windowing overrides become flags,
    so ``analyze_file``'s own defaults stay authoritative for absent ones."""
    argv = [sys.executable, "-m", _CHILD_MODULE, file_path, "--models-dir", models_dir]
    for flag, value in (
        ("--fine-window-sec", fine_window_sec),
        ("--coarse-window-sec", coarse_window_sec),
        ("--fine-min-sec", fine_min_sec),
        ("--fine-cap", fine_cap),
        ("--coarse-cap", coarse_cap),
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
    fine_cap: int | None = None,
    coarse_cap: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run one windowed analysis in the child CLI; return the ``analyze_file`` dict.

    ``progress_cb(analyzed, total)`` is invoked on the event loop per protocol
    progress line (guarded: an exception inside it is logged and swallowed, mirroring
    the lanes' progress-never-fails-the-job contract). ``timeout=None`` delegates
    wall-clock bounding to the caller's environment (the pod's Kueue deadline).
    """
    argv = _build_argv(
        file_path,
        models_dir,
        fine_window_sec=fine_window_sec,
        coarse_window_sec=coarse_window_sec,
        fine_min_sec=fine_min_sec,
        fine_cap=fine_cap,
        coarse_cap=coarse_cap,
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

    def _safe_progress(analyzed: int, total: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(analyzed, total)
        except Exception:  # the lanes' contract: a progress error never fails the analysis
            log.debug("analysis_progress_cb_error", file=file_path)

    async def _pump_stdout() -> None:
        nonlocal result, error_message
        async for raw in child_stdout:
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
            kind = message.get("type")
            if kind == "progress":
                _safe_progress(int(message.get("analyzed", 0)), int(message.get("total", 0)))
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
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            stderr_tail.append(line[:_STDERR_LINE_MAX])
            log.info("analysis_child_output", line=line[:_STDERR_LINE_MAX])

    async def _drive() -> int:
        await asyncio.gather(_pump_stdout(), _pump_stderr())
        return await proc.wait()

    try:
        returncode = await asyncio.wait_for(_drive(), timeout=timeout) if timeout is not None else await _drive()
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        msg = f"analysis child timed out after {timeout}s"
        raise TimeoutError(msg) from None
    except asyncio.CancelledError:
        # The caller lost interest (SAQ job cancellation / pod teardown): reap the
        # child so no orphan essentia process keeps burning CPU, then propagate.
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
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
