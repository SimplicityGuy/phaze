"""Guards for ``scripts/harness-scram-iterations.sh`` (phaze-1dc8g).

The NullPool test engine opens a fresh asyncpg connection per DB test, ~3,500 per full suite, and
asyncpg derives the SCRAM key in a pure-Python loop whose length is the ROLE's stored iteration
count. At Postgres's default 4096 that was ~12 ms of a ~17-18 ms connect (measured 2026-09-24 from
the macOS host against the 5433 harness); at one iteration the same connect took ~5 ms. The script
re-hashes the harness role once, and these tests hold three things in place:

* both harness entry points still call it -- ``just test-db`` for the local 5433 container and the
  CI matrix job for its ``services.postgres`` container -- so the saving cannot silently regress;
* against a real throwaway ``postgres:18-alpine``, it leaves the role at one iteration, keeps the
  password ``phaze`` working over real SCRAM, and is a no-op the second time;
* it refuses any container that is not the ephemeral harness BEFORE touching Postgres, because
  re-setting a password to ``phaze`` is a no-op only where ``phaze`` already is the password.

Every container here is started and removed by this module -- never the shared ``phaze-test-db``.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "harness-scram-iterations.sh"
_JUSTFILE = _REPO_ROOT / "justfile"
_TESTS_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "tests.yml"

_PG_IMAGE = "postgres:18-alpine"
_HARNESS_ENV = ("POSTGRES_USER=phaze", "POSTGRES_PASSWORD=phaze", "POSTGRES_DB=phaze_test")


def _recipe_body(name: str) -> str:
    """The body of one justfile recipe: from its header line to the next unindented line."""
    lines = _JUSTFILE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{name}:")
    body = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        body.append(line)
    return "\n".join(body)


def test_just_test_db_rehashes_the_harness_role() -> None:
    body = _recipe_body("test-db")
    assert 'bash scripts/harness-scram-iterations.sh "$container"' in body
    assert 'container="{{test_db_container}}"' in body


def test_the_ci_matrix_job_rehashes_its_service_role() -> None:
    workflow = _TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "PG_SERVICE_CONTAINER: ${{ job.services.postgres.id }}" in workflow
    assert 'run: bash scripts/harness-scram-iterations.sh "${PG_SERVICE_CONTAINER}"' in workflow


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607 - literal argv, test-generated container name


def _docker_usable() -> bool:
    return shutil.which("docker") is not None and _docker("info").returncode == 0


needs_docker = pytest.mark.skipif(not _docker_usable(), reason="needs a working docker daemon to start throwaway Postgres containers")


def _run_script(container: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(_SCRIPT), container], capture_output=True, text=True, check=False)  # noqa: S603, S607 - literal argv


def _env_flags(env: tuple[str, ...]) -> list[str]:
    return [flag for pair in env for flag in ("-e", pair)]


@pytest.fixture(scope="module")
def harness_postgres() -> Iterator[str]:
    """A throwaway Postgres configured exactly like the harness ``just test-db`` starts."""
    container = f"phaze-scram-test-pg-{uuid4().hex[:10]}"
    started = _docker("run", "-d", "--name", container, *_env_flags(_HARNESS_ENV), _PG_IMAGE)
    if started.returncode != 0:
        pytest.skip(f"could not start a throwaway Postgres: {started.stderr.strip()}")
    try:
        # Probe over TCP: the image's first-boot TEMPORARY server listens on the unix socket only
        # (see `_postgres_ready` in tests/shared/test_test_db_gc.py).
        for _ in range(160):
            if _docker("exec", container, "psql", "-h", "127.0.0.1", "-U", "phaze", "-d", "postgres", "-tAc", "select 1").returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.skip("throwaway Postgres never became ready")
        yield container
    finally:
        _docker("rm", "-f", container)


def _stored_verifier(container: str) -> str:
    result = _docker("exec", container, "psql", "-U", "phaze", "-d", "postgres", "-tAc", "SELECT rolpassword FROM pg_authid WHERE rolname = 'phaze'")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _scram_login(container: str, password: str) -> subprocess.CompletedProcess[str]:
    """Log in over the container's own network address, which its pg_hba routes to scram-sha-256.

    127.0.0.1 and the unix socket are ``trust`` in this image, so they would accept ANY password
    and prove nothing about the verifier.
    """
    login = 'psql -h "$(hostname -i | cut -d" " -f1)" -U phaze -d postgres -tAc "select 1"'
    return _docker("exec", "-e", f"PGPASSWORD={password}", container, "sh", "-c", login)


@needs_docker
def test_the_role_ends_at_one_iteration_with_the_password_unchanged(harness_postgres: str) -> None:
    assert _stored_verifier(harness_postgres).startswith("SCRAM-SHA-256$4096:"), "precondition: the image hashes at the default 4096"

    result = _run_script(harness_postgres)

    assert result.returncode == 0, result.stderr
    assert _stored_verifier(harness_postgres).startswith("SCRAM-SHA-256$1:")
    assert _scram_login(harness_postgres, "phaze").returncode == 0
    # Negative control: the SCRAM path really is checking the password.
    assert _scram_login(harness_postgres, "not-the-password").returncode != 0


@needs_docker
def test_a_second_run_changes_nothing(harness_postgres: str) -> None:
    assert _run_script(harness_postgres).returncode == 0
    before = _stored_verifier(harness_postgres)

    result = _run_script(harness_postgres)

    assert result.returncode == 0, result.stderr
    assert "already hashed" in result.stdout
    # A re-hash would mint a fresh salt, so an unchanged verifier proves no ALTER ran.
    assert _stored_verifier(harness_postgres) == before


@needs_docker
@pytest.mark.parametrize(
    "env",
    [
        pytest.param(("POSTGRES_USER=phaze", "POSTGRES_PASSWORD=phaze", "POSTGRES_DB=phaze"), id="the-dev-compose-database"),
        pytest.param(("POSTGRES_USER=phaze", "POSTGRES_PASSWORD=a-real-secret", "POSTGRES_DB=phaze_test"), id="a-different-password"),
        pytest.param(("POSTGRES_USER=admin", "POSTGRES_PASSWORD=phaze", "POSTGRES_DB=phaze_test"), id="a-different-user"),
    ],
)
def test_a_container_that_is_not_the_harness_is_refused_before_postgres_is_touched(env: tuple[str, ...]) -> None:
    # Created, never started: had the script got as far as `docker exec`, docker would have
    # answered "is not running" instead of the script's own refusal.
    container = f"phaze-scram-test-refuse-{uuid4().hex[:10]}"
    created = _docker("create", "--name", container, *_env_flags(env), _PG_IMAGE)
    if created.returncode != 0:
        pytest.skip(f"could not create a throwaway container: {created.stderr.strip()}")
    try:
        result = _run_script(container)
    finally:
        _docker("rm", "-f", container)

    assert result.returncode == 1
    assert "is not the ephemeral test harness" in result.stderr
    assert "is not running" not in result.stderr
