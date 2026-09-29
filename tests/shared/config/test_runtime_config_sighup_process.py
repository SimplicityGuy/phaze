"""Real-process SIGHUP delivery test (phaze-mvq8z.5 acceptance): "A real-process test sends
SIGHUP to a running worker subprocess and observes the reload log line."

``_sighup_worker_harness.py`` is launched as a genuinely separate OS process (mirroring
``tests/analyze/core/test_analysis_child.py``'s real-subprocess pattern) and installs the SAME
production ``phaze.runtime_config_triggers.install_sighup_handler`` the api lifespan, control
worker startup, and agent worker startup all call -- so ``os.kill(pid, SIGHUP)`` here exercises
the real trigger, not a stand-in. ``test_runtime_config_triggers.py`` covers the debounce/
content-hash directory-watch half, and the handler's wiring in-process, at unit-test speed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest

from tests._child_process_budget import CHILD_PROCESS_HANG_GUARD_SEC


if TYPE_CHECKING:
    from collections.abc import Iterator


# -m module form, mirroring tests/analyze/core/test_analysis_child.py's real-subprocess idiom:
# every argv element below is a STRING LITERAL (ruff's S603 only trusts subprocess input built
# entirely from literals + sys.executable, not a Path-derived variable), and running it as a
# package module (rather than a bare script path) means it needs no sys.path surgery -- `tests`
# is already an ordinary package (tests/__init__.py exists) and `-m` adds `cwd` to sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TIMEOUT_SEC = 15.0


class _LiveProcess:
    """A running subprocess whose stdout is drained by a background daemon thread.

    The reader thread never blocks the test thread: it just appends lines to a
    lock-protected buffer, and callers POLL that buffer with a deadline. A blocking
    ``readline()`` call directly on the test thread was tried and rejected -- it has no way to
    honor a timeout once it is inside the call, which risks hanging the test itself rather than
    just failing it.
    """

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._lock:
                self._lines.append(line)

    def _snapshot(self) -> str:
        with self._lock:
            return "".join(self._lines)

    def wait_for_count(self, needle: str, count: int, *, timeout: float = _TIMEOUT_SEC) -> str:
        """Block (via short polls, never a blocking read) until ``needle`` appears >= ``count`` times."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self._snapshot()
            if text.count(needle) >= count:
                return text
            if self.proc.poll() is not None:
                pytest.fail(f"harness exited early (code={self.proc.returncode}) before {needle!r} x{count}. Output so far:\n{text}")
            time.sleep(0.02)
        pytest.fail(f"timed out after {timeout}s waiting for {needle!r} x{count}. Output so far:\n{self._snapshot()}")
        raise AssertionError  # unreachable; pytest.fail raises, this satisfies "missing return"

    def wait_for(self, needle: str, *, timeout: float = _TIMEOUT_SEC) -> str:
        return self.wait_for_count(needle, 1, timeout=timeout)

    def count(self, needle: str) -> int:
        return self._snapshot().count(needle)


@pytest.fixture
def harness() -> Iterator[_LiveProcess]:
    proc = subprocess.Popen(  # trusted input: literal sys.executable + our own module, no shell
        [sys.executable, "-m", "tests.shared.config._sighup_worker_harness"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered
        cwd=_REPO_ROOT,
    )
    live = _LiveProcess(proc)
    try:
        live.wait_for("HARNESS_READY")
        yield live
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=CHILD_PROCESS_HANG_GUARD_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=CHILD_PROCESS_HANG_GUARD_SEC)


def test_sighup_installs_successfully_on_a_real_process(harness: _LiveProcess) -> None:
    assert "HARNESS_READY installed=True" in harness._snapshot()


def test_sighup_triggers_exactly_one_reload_and_the_worker_keeps_running(harness: _LiveProcess) -> None:
    os.kill(harness.proc.pid, signal.SIGHUP)
    output = harness.wait_for('"event": "phaze.runtime_config reload"')

    reload_lines = [line for line in output.splitlines() if '"event": "phaze.runtime_config reload"' in line]
    assert len(reload_lines) == 1, f"expected exactly one reload line, got {len(reload_lines)}:\n{output}"
    payload = json.loads(reload_lines[0])
    assert payload["source"] == "sighup"
    assert payload["outcome"] in ("applied", "unchanged")  # never raised into the signal handler

    # The worker is still alive -- SIGHUP triggered a reload, not a shutdown.
    assert harness.proc.poll() is None


def test_a_second_sighup_produces_a_second_independent_reload(harness: _LiveProcess) -> None:
    """The handler is not a one-shot: repeated signals keep triggering repeated reloads."""
    os.kill(harness.proc.pid, signal.SIGHUP)
    harness.wait_for_count('"source": "sighup"', 1)

    os.kill(harness.proc.pid, signal.SIGHUP)
    harness.wait_for_count('"source": "sighup"', 2)

    assert harness.count('"source": "sighup"') == 2
    assert harness.proc.poll() is None
