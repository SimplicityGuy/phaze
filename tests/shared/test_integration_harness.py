"""Ownership and validation-grade guards for the dedicated integration harness."""

import os
from pathlib import Path
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "scripts" / "integration-test-harness.sh"


def _owner_id() -> str:
    cksum = shutil.which("cksum")
    assert cksum is not None
    result = subprocess.run(  # noqa: S603 - resolved platform utility mirrors the harness
        [cksum],
        input=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.split()[0]


def _fake_docker(tmp_path: Path) -> Path:
    fake = tmp_path / "docker"
    fake.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "${1:-}" = inspect ] && [ "${2:-}" = -f ]; then
  case "$3" in
    *run-id*) printf '%s\\n' "$FAKE_RUN_ID" ;;
    *owner*) printf '%s\\n' "$FAKE_OWNER" ;;
    *pid*) printf '%s\\n' "$FAKE_PID" ;;
  esac
fi
exit 0
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _cleanup(tmp_path: Path, *, owner: str, pid: int) -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "docker.log"
    env = {
        **os.environ,
        "DOCKER_BIN": str(_fake_docker(tmp_path)),
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_RUN_ID": "run_42",
        "FAKE_OWNER": owner,
        "FAKE_PID": str(pid),
    }
    result = subprocess.run(  # noqa: S603 - fixed in-repo executable with a synthetic run ID
        [str(HARNESS), "cleanup", "run_42"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, log.read_text(encoding="utf-8")


def test_cleanup_removes_only_the_selected_owned_stale_run(tmp_path: Path) -> None:
    result, log = _cleanup(tmp_path, owner=_owner_id(), pid=999_999_999)

    assert result.returncode == 0, result.stderr
    assert "rm -f phaze-integration-test-db-run_42 phaze-integration-test-redis-run_42" in log
    assert "phaze-test-db" not in log
    assert "phaze-test-redis" not in log


def test_cleanup_refuses_a_live_run(tmp_path: Path) -> None:
    result, log = _cleanup(tmp_path, owner=_owner_id(), pid=os.getpid())

    assert result.returncode != 0
    assert "still active" in result.stderr
    assert "rm -f" not in log


def test_cleanup_refuses_another_worktrees_run(tmp_path: Path) -> None:
    result, log = _cleanup(tmp_path, owner="different-owner", pid=999_999_999)

    assert result.returncode != 0
    assert "another worktree" in result.stderr
    assert "rm -f" not in log


def test_readiness_waits_for_the_final_tcp_server() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    assert "pg_isready -h 127.0.0.1 -U phaze -d phaze_test" in source


def test_each_run_labels_unique_postgres_and_redis_containers_and_uses_the_real_gate() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    assert 'run_id="${PHAZE_INTEGRATION_RUN_ID:-$$_${RANDOM}}"' in source
    assert "com.phaze.integration.run-id" in source
    assert 'db_container="${pg_prefix}-${run_id}"' in source
    assert 'redis_container="${redis_prefix}-${run_id}"' in source
    assert '"$just_bin" test-cov' in source
    assert "pytest tests/ -q" not in source


def test_dedicated_redis_db_zero_cannot_be_shared_between_runs() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    assert '-p "$redis_publish" redis:7-alpine' in source
    assert 'PHAZE_REDIS_URL="redis://localhost:${redis_port}/0"' in source
    assert "every run owns a separate Redis container" in source
