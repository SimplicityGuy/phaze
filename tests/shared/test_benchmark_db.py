"""Behavioral tests for the reusable benchmark database lifecycle."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "benchmark-db.sh"


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    call_log = tmp_path / "docker-calls"
    executable = tmp_path / "docker"
    executable.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '<%s>' "$@" >>"$CALL_LOG"
printf '\n' >>"$CALL_LOG"
if [[ "${1:-}" == inspect && "${2:-}" != -f ]]; then exit "${FAKE_EXISTS:-0}"; fi
if [[ "${1:-}" == inspect && "${3:-}" == '{{.Config.Image}}' ]]; then printf '%s\n' "${FAKE_IMAGE:-postgres:18-alpine}"; exit; fi
if [[ "${1:-}" == inspect && "${3:-}" == *HostIp* ]]; then printf '%s\n' "${FAKE_BIND_IP:-127.0.0.1}"; exit; fi
if [[ "${1:-}" == inspect && "${3:-}" == *HostPort* ]]; then printf '%s\n' "${FAKE_PORT:-5545}"; exit; fi
if [[ "${1:-}" == inspect && "${3:-}" == '{{.State.Running}}' ]]; then printf '%s\n' "${FAKE_RUNNING:-true}"; exit; fi
if [[ "${1:-}" == exec ]]; then exit 0; fi
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, call_log


def _run_up(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    docker, call_log = _fake_docker(tmp_path)
    env = os.environ | {"CALL_LOG": str(call_log), "DOCKER_BIN": str(docker)} | overrides
    result = subprocess.run(  # noqa: S603 - fixed in-repository script and controlled fake executable
        [
            "/bin/bash",
            SCRIPT,
            "up",
            "phaze-benchmark-db",
            "5545",
            "phaze_benchmark",
            "postgres:18-alpine",
            "256m",
            "127.0.0.1",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, call_log


def test_reused_container_with_wrong_image_fails_before_start(tmp_path: Path) -> None:
    result, call_log = _run_up(tmp_path, FAKE_IMAGE="postgres:17-alpine", FAKE_RUNNING="false")
    assert result.returncode != 0
    assert "uses image postgres:17-alpine; expected postgres:18-alpine" in result.stderr
    assert "<start>" not in call_log.read_text(encoding="utf-8")


def test_reused_container_with_wrong_port_fails_before_start(tmp_path: Path) -> None:
    result, call_log = _run_up(tmp_path, FAKE_PORT="9999", FAKE_RUNNING="false")
    assert result.returncode != 0
    assert "publishes port 9999; expected 5545" in result.stderr
    assert "<start>" not in call_log.read_text(encoding="utf-8")


def test_matching_stopped_container_is_verified_started_and_readied(tmp_path: Path) -> None:
    result, call_log = _run_up(tmp_path, FAKE_RUNNING="false")
    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "<start><phaze-benchmark-db>" in calls
    assert "<exec><phaze-benchmark-db><pg_isready><-h><127.0.0.1>" in calls


def test_invalid_port_fails_before_docker_is_called(tmp_path: Path) -> None:
    docker, call_log = _fake_docker(tmp_path)
    result = subprocess.run(  # noqa: S603 - fixed in-repository script and controlled fake executable
        ["/bin/bash", SCRIPT, "up", "phaze-benchmark-db", "bad", "phaze_benchmark", "postgres:18-alpine", "256m", "127.0.0.1"],
        cwd=REPO_ROOT,
        env=os.environ | {"CALL_LOG": str(call_log), "DOCKER_BIN": str(docker)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "port must be an integer" in result.stderr
    assert not call_log.exists()
