"""The real browser server lifecycle must reap its owned child after forced shutdown."""

from __future__ import annotations

import signal
import subprocess
import sys
from typing import Any

import pytest

from tests.browser import conftest as browser_harness


@pytest.mark.skipif(sys.platform == "win32", reason="The browser harness uses POSIX process termination")
def test_live_server_reaps_a_child_that_ignores_termination(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Run the actual server generator's teardown against a real signal-resistant child."""
    original_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def start_child(*_args: Any, **_kwargs: Any) -> subprocess.Popen[bytes]:
        child = original_popen(
            [sys.executable, "-u", "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready'); time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        children.append(child)
        return child

    def child_ready(_url: str, process: subprocess.Popen[bytes], _log: Any) -> None:
        assert process.stdout is not None
        assert process.stdout.readline() == b"ready\n"

    # No database is recreated and no uvicorn is needed for this OS-process regression.
    # The two phone cases exercise real database/server boot and request routing separately.
    monkeypatch.setattr(browser_harness, "_recreate_database", lambda _dsn: None)
    monkeypatch.setattr(browser_harness, "_wait_until_serving", child_ready)
    monkeypatch.setattr(browser_harness, "_ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(browser_harness.subprocess, "Popen", start_child)
    server = browser_harness.serve_live_app()
    try:
        next(server)
        assert len(children) == 1 and children[0].returncode is None
        server.close()
        # Inspect returncode before poll(): poll itself can reap a zombie and hide a missing wait.
        assert children[0].returncode == -signal.SIGKILL
    finally:
        server.close()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()
