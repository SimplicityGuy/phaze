"""Ownership and validation-grade guards for the dedicated integration harness."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import pytest


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
    """A logging stand-in for docker that answers every call the harness makes.

    ``inspect -f`` answers with the FAKE_* labels; ``ps`` prints FAKE_PS_ROWS (the ``clean`` sweep's
    listing); ``port`` hands back a fixed host port; ``exec`` succeeds and prints ``1`` so
    ``ensure-pg-database.sh`` sees the database as present. It is installed both as DOCKER_BIN and
    as ``docker`` on PATH, because ``ensure-pg-database.sh`` calls plain ``docker``.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "docker"
    fake.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "${1:-}" in
  inspect)
    if [ "${2:-}" = -f ]; then
      case "$3" in
        *run-id*) printf '%s\\n' "${FAKE_RUN_ID:-}" ;;
        *owner*) printf '%s\\n' "${FAKE_OWNER:-}" ;;
        *pid*) printf '%s\\n' "${FAKE_PID:-}" ;;
      esac
    fi
    ;;
  ps) printf '%b' "${FAKE_PS_ROWS:-}" ;;
  port) printf '127.0.0.1:55432\\n' ;;
  exec) printf '1\\n' ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _fake_just(tmp_path: Path) -> Path:
    """Stands in for ``just test-cov``: FAKE_SUITE=pass|fail|hang. A hung suite records its own pid."""
    fake = tmp_path / "bin" / "just"
    fake.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$$" > "$FAKE_SUITE_PID"
case "${FAKE_SUITE:-pass}" in
  pass) exit 0 ;;
  fail) exit 3 ;;
  hang)
    # `wait` on a background sleep, so a TERM is handled now rather than after the sleep.
    # The sleep is killed too: an orphaned sleep would hold the harness's stdout pipe open.
    sleep 60 &
    sleeper=$!
    trap 'kill "$sleeper"; exit 143' TERM
    wait "$sleeper"
    printf 'finished\\n' >> "$FAKE_SUITE_PID"
    exit 0
    ;;
esac
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    fake_docker = _fake_docker(tmp_path)
    fake_just = _fake_just(tmp_path)
    return {
        **os.environ,
        "PATH": f"{fake_docker.parent}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_BIN": str(fake_docker),
        "JUST_BIN": str(fake_just),
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_SUITE_PID": str(tmp_path / "suite.pid"),
        **extra,
    }


def _clean(tmp_path: Path, **extra: str) -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "docker.log"
    log.touch()
    result = subprocess.run(  # noqa: S603 - fixed in-repo executable against a fake docker
        [str(HARNESS), "clean"],
        cwd=REPO_ROOT,
        env=_env(tmp_path, **extra),
        check=False,
        capture_output=True,
        text=True,
    )
    return result, log.read_text(encoding="utf-8")


def _removals(log: str) -> list[str]:
    return [line for line in log.splitlines() if line.startswith("rm -f ")]


def test_clean_removes_only_this_checkouts_stale_runs(tmp_path: Path) -> None:
    owner = _owner_id()
    result, log = _clean(
        tmp_path,
        FAKE_PS_ROWS=f"run_42 {owner}\\nrun_42 {owner}\\nrun_77 different-owner\\n",
        FAKE_RUN_ID="run_42",
        FAKE_OWNER=owner,
        FAKE_PID="999999999",
    )

    assert result.returncode == 0, result.stderr
    assert _removals(log) == ["rm -f phaze-integration-test-db-run_42 phaze-integration-test-valkey-run_42"]
    assert "Leaving integration run run_77: another worktree owns it." in result.stdout
    assert "removed 1 stale run(s); left 0 active and 1 foreign run(s) alone" in result.stdout
    assert "phaze-test-db " not in log
    assert "phaze-test-valkey" not in log


def test_clean_refuses_a_live_run_and_keeps_sweeping(tmp_path: Path) -> None:
    owner = _owner_id()
    result, log = _clean(
        tmp_path,
        FAKE_PS_ROWS=f"run_42 {owner}\\n",
        FAKE_RUN_ID="run_42",
        FAKE_OWNER=owner,
        FAKE_PID=str(os.getpid()),
    )

    assert result.returncode == 0, result.stderr
    assert "still active" in result.stderr
    assert _removals(log) == []
    assert "left 1 active and 0 foreign run(s) alone" in result.stdout


def test_clean_with_nothing_to_do_says_so(tmp_path: Path) -> None:
    result, log = _clean(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _removals(log) == []
    assert "removed 0 stale run(s)" in result.stdout


def test_clean_takes_no_run_id(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed in-repo executable against a fake docker
        [str(HARNESS), "clean", "run_42"], cwd=REPO_ROOT, env=_env(tmp_path), check=False, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "usage" in result.stderr


def _start_run(tmp_path: Path, suite: str) -> subprocess.Popen[str]:
    env = _env(tmp_path, FAKE_SUITE=suite, PHAZE_INTEGRATION_RUN_ID="run_9", FAKE_RUN_ID="run_9", FAKE_OWNER=_owner_id())
    (tmp_path / "docker.log").touch()
    return subprocess.Popen(  # noqa: S603 - fixed in-repo executable against a fake docker and a fake just
        [str(HARNESS), "run"], cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


_RUN_9_REMOVAL = "rm -f phaze-integration-test-db-run_9 phaze-integration-test-valkey-run_9"


@pytest.mark.parametrize(("suite", "expected_exit"), [("pass", 0), ("fail", 3)])
def test_a_run_tears_down_its_own_containers_however_the_suite_ends(tmp_path: Path, suite: str, expected_exit: int) -> None:
    process = _start_run(tmp_path, suite)
    _stdout, stderr = process.communicate(timeout=60)
    log = (tmp_path / "docker.log").read_text(encoding="utf-8")

    assert process.returncode == expected_exit, stderr
    assert _removals(log) == [_RUN_9_REMOVAL]


@pytest.mark.parametrize(("signum", "expected_exit"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_an_interrupted_run_stops_the_suite_and_tears_down(tmp_path: Path, signum: signal.Signals, expected_exit: int) -> None:
    process = _start_run(tmp_path, "hang")
    suite_pid_file = tmp_path / "suite.pid"
    deadline = time.monotonic() + 30
    while not suite_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert suite_pid_file.exists(), "the fake suite never started"
    suite_pid = int(suite_pid_file.read_text(encoding="utf-8").split()[0])

    process.send_signal(signum)
    _stdout, stderr = process.communicate(timeout=30)
    log = (tmp_path / "docker.log").read_text(encoding="utf-8")

    assert process.returncode == expected_exit, stderr
    assert _removals(log) == [_RUN_9_REMOVAL]
    assert "finished" not in suite_pid_file.read_text(encoding="utf-8"), "the suite ran to completion instead of being stopped"
    with pytest.raises(ProcessLookupError):
        os.kill(suite_pid, 0)


def test_readiness_waits_for_the_final_tcp_server() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    assert "pg_isready -h 127.0.0.1 -U phaze -d phaze_test" in source


def test_each_run_labels_unique_postgres_and_valkey_containers_and_uses_the_real_gate() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    assert 'run_id="${PHAZE_INTEGRATION_RUN_ID:-$$_${RANDOM}}"' in source
    assert "com.phaze.integration.run-id" in source
    assert 'db_container="${pg_prefix}-${run_id}"' in source
    assert 'redis_container="${redis_prefix}-${run_id}"' in source
    assert '"$just_bin" test-cov' in source
    assert "pytest tests/ -q" not in source


def test_dedicated_redis_db_zero_cannot_be_shared_between_runs() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    assert '-p "$redis_publish" "$redis_image"' in source
    assert 'PHAZE_REDIS_URL="redis://localhost:${redis_port}/0"' in source
    assert "every run owns a separate Valkey container" in source
