"""Shell-boundary contracts for caller-supplied Just recipe parameters."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
JUST = shutil.which("just")
assert JUST is not None


def _dry_run(recipe: str, *arguments: str) -> str:
    result = subprocess.run(  # noqa: S603 - resolved Just binary with explicit argv
        [JUST, "--dry-run", recipe, *arguments],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout + result.stderr


def test_single_value_recipes_shell_quote_sentinel_inputs() -> None:
    sentinel = "space ' quote; $(touch second-command)"
    cases = {
        "test-file": [sentinel],
        "db-revision": [sentinel],
        "download-models": [sentinel],
        "image-build-arm64": [sentinel],
        "repowise-coverage-ci": [sentinel],
        "integration-test-down": [sentinel],
    }
    for recipe, arguments in cases.items():
        rendered = _dry_run(recipe, *arguments)
        lines = rendered.splitlines()
        assert len(lines) == 1, f"{recipe} rendered a second shell line:\n{rendered}"
        argv = shlex.split(lines[0])
        assert sentinel in argv, f"{recipe} did not preserve the sentinel as one argv entry: {argv}"


def test_whitespace_bearing_paths_and_messages_survive_as_one_argument() -> None:
    path = "tests/path with spaces/test_file.py"
    message = "migration message with spaces"
    assert path in shlex.split(_dry_run("test-file", path))
    assert message in shlex.split(_dry_run("db-revision", message))
    assert path in shlex.split(_dry_run("download-models", path))


def test_invalid_runtime_and_bucket_mode_fail_before_external_commands(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("docker", "uv"):
        executable = fake_bin / name
        executable.write_text(f"#!/usr/bin/env bash\ntouch {shlex.quote(str(marker))}\n", encoding="utf-8")
        executable.chmod(0o755)
    env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

    parity = subprocess.run(  # noqa: S603 - resolved Just binary with explicit argv and controlled PATH
        [JUST, "parity-dump", "image", str(tmp_path), str(tmp_path / "out.json"), "python -c bad"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    bucket = subprocess.run(  # noqa: S603 - resolved Just binary with explicit argv and controlled PATH
        [JUST, "test-bucket", "shared", "tests/shared", "-n auto; bad"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert parity.returncode != 0 and "RUNTIME must be x86 or arm64" in parity.stderr
    assert bucket.returncode != 0 and "MODE must be serial or parallel" in bucket.stderr
    assert not marker.exists()


def test_invalid_seat_name_fails_before_test_db_mutation() -> None:
    result = subprocess.run(  # noqa: S603 - resolved Just binary with explicit invalid test input
        [JUST, "test-db-for", "Bad; docker rm"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid seat name" in result.stderr
    assert "just test-db" not in result.stdout


def test_invalid_tag_fails_before_registry_authentication(tmp_path: Path) -> None:
    marker = tmp_path / "called"
    fake = tmp_path / "external"
    fake.write_text(f"#!/usr/bin/env bash\ntouch {shlex.quote(str(marker))}\n", encoding="utf-8")
    fake.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - fixed in-repository script and controlled fake executable
        ["/bin/bash", REPO_ROOT / "scripts" / "container-images.sh", "push", "api", "bad;tag"],
        cwd=REPO_ROOT,
        env=os.environ | {"DOCKER_BIN": str(fake), "GH_BIN": str(fake)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "image tag must match" in result.stderr
    assert not marker.exists()


def test_invalid_integration_run_id_fails_before_docker(tmp_path: Path) -> None:
    marker = tmp_path / "docker-called"
    fake = tmp_path / "docker"
    fake.write_text(f"#!/usr/bin/env bash\ntouch {shlex.quote(str(marker))}\n", encoding="utf-8")
    fake.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - fixed in-repository script and controlled fake executable
        ["/bin/bash", REPO_ROOT / "scripts" / "integration-test-harness.sh", "cleanup", "bad;run"],
        cwd=REPO_ROOT,
        env=os.environ | {"DOCKER_BIN": str(fake)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "integration run ID must match" in result.stderr
    assert not marker.exists()


def test_invalid_benchmark_counts_fail_before_database_or_python(tmp_path: Path) -> None:
    marker = tmp_path / "uv-called"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(f"#!/usr/bin/env bash\ntouch {shlex.quote(str(marker))}\n", encoding="utf-8")
    fake_uv.chmod(0o755)
    env = os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}"}
    for recipe in ("benchmark-seed", "benchmark-explain", "benchmark-analyze"):
        result = subprocess.run(  # noqa: S603 - resolved Just binary with explicit argv and controlled PATH
            [JUST, recipe, "0; touch bad"], cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=False
        )
        assert result.returncode != 0, recipe
        assert "positive integer" in result.stderr, recipe
    assert not marker.exists()


def test_variadic_flags_are_forwarded_as_discrete_argv(tmp_path: Path) -> None:
    log = tmp_path / "argv"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text('#!/usr/bin/env bash\nprintf \'<%s>\\n\' "$@" >"$ARGV_LOG"\n', encoding="utf-8")
    fake_uv.chmod(0o755)
    sentinel = "branch; $(touch never)"
    result = subprocess.run(  # noqa: S603 - resolved Just binary with explicit argv and controlled PATH
        [JUST, "branch-check", "--base-ref", sentinel],
        cwd=REPO_ROOT,
        env=os.environ | {"ARGV_LOG": str(log), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"<{sentinel}>" in log.read_text(encoding="utf-8")
    assert not (REPO_ROOT / "never").exists()


def test_bucket_paths_and_parallel_mode_become_validated_argv(tmp_path: Path) -> None:
    log = tmp_path / "argv"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        '#!/usr/bin/env bash\nprintf \'coverage=%s\\n\' "${COVERAGE_FILE:-}" >"$ARGV_LOG"\nprintf \'<%s>\\n\' "$@" >>"$ARGV_LOG"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - resolved Just binary with explicit argv and controlled PATH
        [JUST, "test-bucket", "shared_1", "tests/shared tests/agents", "parallel"],
        cwd=REPO_ROOT,
        env=os.environ | {"ARGV_LOG": str(log), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = log.read_text(encoding="utf-8")
    assert "coverage=.coverage.shared_1" in argv
    assert "<tests/shared>" in argv
    assert "<tests/agents>" in argv
    assert "<-n>" in argv and "<auto>" in argv


def test_raw_fragment_boundaries_are_documented_and_minimized() -> None:
    assert "{{flags}}" not in JUSTFILE
    assert "{{PATHS}}" not in JUSTFILE
    assert "{{XDIST}}" not in JUSTFILE
    assert "Trust boundary" in JUSTFILE
    assert '"$@" is never reparsed as shell syntax' in JUSTFILE
