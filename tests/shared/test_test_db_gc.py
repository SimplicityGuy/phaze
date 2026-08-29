"""Behaviour tests for ``just test-db-gc`` (phaze-robzi.2) -- the STOCK half of the epic.

``test-db-reclaim --apply`` (phaze-robzi.1) now drops a freed seat's own two databases in the same
operation that frees its Redis index, going forward. It cannot reach a database orphaned BEFORE
that fix existed: once a seat was reclaimed under the old contract, its Redis registry entry -- the
only thing that ever named ``phaze_<seat>_test`` / ``phaze_<seat>_migrations_test`` -- was gone, and
nothing pointed at those two databases again. 652 such databases (6974 MB) were measured on the
shared harness on 2026-08-25, with the only tool able to remove them being a full ``test-db-down``
container teardown -- the exact instrument CLAUDE.md records destroying 89 per-worktree databases
and the Redis registry mid-round on 2026-07-29.

``scripts/test-db-gc.sh`` closes the stock: a broad sweep requiring THREE independent signals
(operator decision 2026-08-25, quoted verbatim: "Unregistered + no connections + age floor
(Recommended)" -- the "(Recommended)" suffix is the assistant's framing, not the operator's words;
durable record: bead phaze-robzi.2):

  1. UNREGISTERED -- no seat in the Redis registry names this database.
  2. NO BACKENDS -- zero Postgres client backends connected to it right now.
  3. PAST AN AGE FLOOR -- default 24h, overridable. Covers the race the first two signals cannot: a
     seat mid-provision that has created its databases but not yet reached the Redis ``allocate``
     call that registers them.

**Why these tests use real Redis and Postgres, on the same discipline as
``tests/shared/test_redis_seat_registry.py``.** The three-signal classification and the age-clock
arithmetic are properties of real evidence, not of anything mockable. Every test drives
``scripts/test-db-gc.sh`` -- the same entry point the justfile recipe calls -- against THROWAWAY
containers this module starts and removes itself, never the shared ``phaze-test-db`` /
``phaze-test-redis`` harness that this bead exists to make safe to reap.

**The age clock, measured rather than assumed (phaze-robzi.2, 2026-08-28, against
``postgres:18-alpine`` in a throwaway container).** ``pg_database`` carries no creation timestamp
column at all. ``pg_stat_file`` on the database's own directory (``base/<oid>``) DOES return a row,
but its ``creation`` field came back NULL on this image -- not available at all here. Its
``modification`` field IS populated and moves on real writes: measured immediately after ``CREATE
DATABASE``, then again 5 seconds later after ``CREATE TABLE`` + ``INSERT`` + ``CHECKPOINT`` inside
that database, ``modification`` jumped forward by exactly that gap. So ``scripts/test-db-gc.sh``
uses ``modification`` as a "time since last touched" clock, not a creation clock -- see its header
for why that is the SAFER choice for this purpose, not merely the only available one. These tests
exercise the age floor by backdating a database's directory mtime with ``touch -t`` from inside the
container (busybox's ``touch`` does not understand GNU's ``-d '2 hours ago'`` relative-date syntax
-- see ``_age_database``'s own docstring for the measurement), rather than waiting out a real 24h
floor.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "test-db-gc.sh"
_JUSTFILE = _REPO_ROOT / "justfile"

_REGISTRY_KEY = "phaze:test:redis-db-index"

_PG_IMAGE = "postgres:18-alpine"

# A container name nothing will ever answer to -- the "asked, and nothing answered" state, distinct
# from omitting the flag entirely (this script's flags are both mandatory, so there is no "omitted"
# state to distinguish, unlike redis-seat-registry.sh's optional --pg-container).
_ABSENT_CONTAINER = "phaze-gc-test-no-such-container"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607 - literal argv, test-generated container name


def _docker_usable() -> bool:
    return shutil.which("docker") is not None and _docker("info").returncode == 0


pytestmark = pytest.mark.skipif(not _docker_usable(), reason="needs a working docker daemon to start throwaway Redis/Postgres containers")


@pytest.fixture(scope="module")
def throwaway_redis() -> Iterator[str]:
    """A private Redis for this module alone -- never the shared ``phaze-test-redis``."""
    container = f"phaze-gc-test-redis-{uuid4().hex[:10]}"
    started = _docker("run", "-d", "--name", container, "redis:7-alpine", "redis-server", "--databases", "16")
    if started.returncode != 0:
        pytest.skip(f"could not start a throwaway Redis: {started.stderr.strip()}")
    try:
        for _ in range(60):
            ping = _docker("exec", container, "redis-cli", "PING")
            if ping.returncode == 0 and "PONG" in ping.stdout:
                break
            time.sleep(0.25)
        else:
            pytest.skip("throwaway Redis never became ready")
        yield container
    finally:
        _docker("rm", "-f", container)


@pytest.fixture(scope="module")
def throwaway_postgres() -> Iterator[str]:
    """A private Postgres for this module alone -- never the shared ``phaze-test-db``.

    Matches the image and startup discipline of the L2 fixture in
    ``tests/shared/test_redis_seat_registry.py``: dropping and creating ``phaze%`` databases in
    bulk is precisely the operation that must never happen against the container every other
    concurrent worktree shares.
    """
    container = f"phaze-gc-test-pg-{uuid4().hex[:10]}"
    started = _docker(
        "run", "-d", "--name", container, "-e", "POSTGRES_USER=phaze", "-e", "POSTGRES_PASSWORD=phaze", "-e", "POSTGRES_DB=postgres", _PG_IMAGE
    )
    if started.returncode != 0:
        pytest.skip(f"could not start a throwaway Postgres: {started.stderr.strip()}")
    try:
        for _ in range(160):
            ready = _docker("exec", container, "pg_isready", "-U", "phaze", "-d", "postgres")
            if ready.returncode == 0 and _docker("exec", container, "psql", "-U", "phaze", "-d", "postgres", "-tAc", "select 1").returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.skip("throwaway Postgres never became ready")
        yield container
    finally:
        _docker("rm", "-f", container)


def _psql(container: str, sql: str, *, database: str = "postgres") -> str:
    result = _docker("exec", container, "psql", "-U", "phaze", "-d", database, "-tAc", sql)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def postgres(throwaway_postgres: str) -> Iterator[str]:
    """Hand each test an empty Postgres: no ``phaze%`` databases and no backends left attached."""
    yield throwaway_postgres
    for row in _psql(throwaway_postgres, "select datname from pg_database where datname like 'phaze%'").splitlines():
        database = row.strip()
        if not database:
            continue
        _psql(
            throwaway_postgres,
            "select pg_terminate_backend(pid) from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid()",
            database=database,
        )
        _psql(throwaway_postgres, f'drop database if exists "{database}"')


@pytest.fixture
def registry(throwaway_redis: str) -> Iterator[str]:
    """Hand each test an empty registry, the container reused for speed."""
    _docker("exec", throwaway_redis, "redis-cli", "-n", "0", "FLUSHALL")
    yield throwaway_redis
    _docker("exec", throwaway_redis, "redis-cli", "-n", "0", "FLUSHALL")


def _register_seat(container: str, seat: str, index: int = 1) -> None:
    """Put a seat straight into the registry -- this module tests ``test-db-gc.sh``, not allocation."""
    _docker("exec", container, "redis-cli", "-n", "0", "HSET", _REGISTRY_KEY, seat, str(index))


def _database_names(container: str) -> set[str]:
    return {row.strip() for row in _psql(container, "select datname from pg_database where datname like 'phaze%'").splitlines() if row.strip()}


def _create_database(container: str, name: str) -> None:
    _psql(container, f'create database "{name}"')


def _age_database(container: str, name: str, hours: int) -> None:
    """Backdate ``name``'s directory ``modification`` time, the age-gc.sh's age clock reads.

    ``pg_stat_file``'s ``modification`` is a real filesystem mtime (see the module docstring's
    measurement), so the natural way to make a database look old without waiting out the real
    floor is to backdate its own data directory's mtime from inside the container -- the identical
    filesystem-level operation the production accumulation mechanism performs implicitly by doing
    nothing to an abandoned database for a long time.

    ``postgres:18-alpine`` ships busybox ``touch``/``date``, which do NOT understand GNU's
    relative-date extension (``touch -d '2 hours ago'`` fails there with "invalid date" -- checked
    directly against this image while writing this test). Busybox's ``touch -t`` DOES accept a
    precomputed ``[[CC]YY]MMDDhhmm[.ss]`` timestamp, so the "N hours ago" arithmetic is done here,
    on the host, in UTC (the image carries no TZ, matching ``pg_stat_file``'s own UTC-normalized
    timestamptz), and only the literal target time crosses into the container.

    ``PGDATA`` on this image is versioned (``/var/lib/postgresql/18/docker``), NOT the commonly
    assumed ``/var/lib/postgresql/data`` -- checked directly against this image while writing this
    test, since a hardcoded guess silently touched a nonexistent path and failed loudly at the
    ``touch`` call rather than the ``select``. ``pg_stat_file`` itself never needs this: it resolves
    its relative argument against the server's own ``PGDATA``, whatever that is.
    """
    oid = _psql(container, f"select oid from pg_database where datname = '{name}'")  # noqa: S608 - test literal, throwaway container
    assert oid, f"database {name!r} does not exist"
    data_directory = _psql(container, "show data_directory")
    target = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)).strftime("%Y%m%d%H%M.%S")
    touched = _docker("exec", container, "touch", "-t", target, f"{data_directory}/base/{oid}")
    assert touched.returncode == 0, touched.stderr


_BACKENDS_ON_THIS_DATABASE = (
    "select count(*) from pg_stat_activity where backend_type = 'client backend' and datname = current_database() and pid <> pg_backend_pid()"
)


def _attach_backend(container: str, database: str) -> None:
    """Hold a client backend open on ``database`` -- the "no backends" signal, as a real connection."""
    attached = _docker("exec", "-d", container, "psql", "-U", "phaze", "-d", database, "-c", "select pg_sleep(600)")
    assert attached.returncode == 0, attached.stderr
    for _ in range(80):
        if int(_psql(container, _BACKENDS_ON_THIS_DATABASE, database=database)) >= 1:
            return
        time.sleep(0.25)
    pytest.skip(f"could not attach a Postgres backend to {database}")


def _run(pg: str, redis: str, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    assert _SCRIPT.is_file(), f"gc script missing: {_SCRIPT}"
    return subprocess.run(  # noqa: S603 - fixed, in-repo executable; every argument is a test literal or a throwaway container name
        [str(_SCRIPT), "--pg-container", pg, "--redis-container", redis, *extra],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _stale_database(postgres: str, name: str, *, hours: int = 48) -> None:
    """A database that is unregistered, backend-free, and past the default 24h floor by default."""
    _create_database(postgres, name)
    _age_database(postgres, name, hours)


# ---------------------------------------------------------------------------------------------
# The three signals, combined and each in isolation
# ---------------------------------------------------------------------------------------------


def test_apply_drops_a_database_that_is_unregistered_backend_free_and_past_the_age_floor(postgres: str, registry: str) -> None:
    """Criterion 1: all three signals agreeing -> the dry run names it, --apply drops it."""
    _stale_database(postgres, "phaze_orphan_a1b2c3d4_test")
    _stale_database(postgres, "phaze_orphan_a1b2c3d4_migrations_test")

    preview = _run(postgres, registry)
    assert preview.returncode == 0, preview.stderr
    assert "phaze_orphan_a1b2c3d4_test" in preview.stdout
    assert "phaze_orphan_a1b2c3d4_migrations_test" in preview.stdout
    assert {"phaze_orphan_a1b2c3d4_test", "phaze_orphan_a1b2c3d4_migrations_test"} <= _database_names(postgres), "a dry run must drop nothing"

    applied = _run(postgres, registry, "--apply")
    assert applied.returncode == 0, applied.stderr
    remaining = _database_names(postgres)
    assert "phaze_orphan_a1b2c3d4_test" not in remaining
    assert "phaze_orphan_a1b2c3d4_migrations_test" not in remaining


def test_a_registered_seats_databases_are_never_candidates(postgres: str, registry: str) -> None:
    """Signal 1 alone protects: a live registry entry keeps its databases, however old they look."""
    _stale_database(postgres, "phaze_liveseat_ffeeffee_test")
    _stale_database(postgres, "phaze_liveseat_ffeeffee_migrations_test")
    _register_seat(registry, "liveseat_ffeeffee")

    applied = _run(postgres, registry, "--apply")

    assert applied.returncode == 0, applied.stderr
    assert {"phaze_liveseat_ffeeffee_test", "phaze_liveseat_ffeeffee_migrations_test"} <= _database_names(postgres), (
        "a registered seat's databases must survive regardless of age"
    )


def test_a_database_with_a_live_backend_is_never_a_candidate(postgres: str, registry: str) -> None:
    """Signal 2 alone protects: a connected backend keeps the database, however old and unregistered."""
    _stale_database(postgres, "phaze_busyorphan_11223344_test")
    _attach_backend(postgres, "phaze_busyorphan_11223344_test")

    applied = _run(postgres, registry, "--apply")

    assert applied.returncode == 0, applied.stderr
    assert "phaze_busyorphan_11223344_test" in _database_names(postgres), "a database with a live backend must survive"


def test_a_database_younger_than_the_age_floor_is_never_a_candidate(postgres: str, registry: str) -> None:
    """Signal 3 alone protects: this is the race the age floor exists to close (a seat mid-provision).

    Unregistered and backend-free are BOTH true here -- exactly the state a seat's databases are in
    for the brief window between ``CREATE DATABASE`` and the Redis ``allocate`` call that registers
    them. Without the floor, a sweep racing that window would drop a database seconds old.
    """
    _create_database(postgres, "phaze_freshseat_55667788_test")  # no _age_database call: freshly created, mtime is "now"

    applied = _run(postgres, registry, "--apply")

    assert applied.returncode == 0, applied.stderr
    assert "phaze_freshseat_55667788_test" in _database_names(postgres), "a database younger than the age floor must survive"


def test_the_age_floor_is_overridable(postgres: str, registry: str) -> None:
    """The default is 24h; an operator who wants a tighter or looser floor can ask for one."""
    _stale_database(postgres, "phaze_middling_99887766_test", hours=2)

    default_floor = _run(postgres, registry, "--apply")
    assert "phaze_middling_99887766_test" in _database_names(postgres), "2h old must survive the default 24h floor"

    tight_floor = _run(postgres, registry, "--age-floor-hours", "1", "--apply")
    assert default_floor.returncode == 0, default_floor.stderr
    assert tight_floor.returncode == 0, tight_floor.stderr
    assert "phaze_middling_99887766_test" not in _database_names(postgres), "2h old must be dropped once the floor is lowered to 1h"


# ---------------------------------------------------------------------------------------------
# The shared canonical pair -- never a candidate, no matter what
# ---------------------------------------------------------------------------------------------


def test_the_shared_canonical_pair_is_never_a_candidate(postgres: str, registry: str) -> None:
    """Criterion 3: ``phaze_test`` / ``phaze_migrations_test`` must survive unconditionally.

    Both are created here with the actual accumulation shape a real orphan check would see: no
    registry seat names ``phaze_test`` (it is not a per-seat database at all), no backend, and old
    -- exactly the profile the three signals alone would otherwise reclaim.
    """
    _stale_database(postgres, "phaze_test")
    _stale_database(postgres, "phaze_migrations_test")

    applied = _run(postgres, registry, "--apply")

    assert applied.returncode == 0, applied.stderr
    assert {"phaze_test", "phaze_migrations_test"} <= _database_names(postgres), "the shared canonical pair must never be dropped"


# ---------------------------------------------------------------------------------------------
# Fail closed on missing evidence
# ---------------------------------------------------------------------------------------------


def test_refuses_outright_when_postgres_is_unreachable(registry: str) -> None:
    """Unknown backend evidence must never be read as safe to drop (mirrors require_postgres_evidence)."""
    result = _run(_ABSENT_CONTAINER, registry, "--apply")

    assert result.returncode == 1
    assert "not reachable" in result.stderr


def test_refuses_outright_when_the_redis_registry_is_unreachable(postgres: str) -> None:
    """Unknown registration status must never be read as unregistered."""
    _stale_database(postgres, "phaze_shouldsurvive_aabbccdd_test")

    result = _run(postgres, _ABSENT_CONTAINER, "--apply")

    assert result.returncode == 1
    assert "not reachable" in result.stderr
    assert "phaze_shouldsurvive_aabbccdd_test" in _database_names(postgres), "a refused sweep must drop nothing"


# ---------------------------------------------------------------------------------------------
# Never a container, never a stop/rm/create/run
# ---------------------------------------------------------------------------------------------


def test_no_container_is_stopped_removed_or_recreated_on_any_path(postgres: str, registry: str, tmp_path: Path) -> None:
    """Criterion 4, ASSERTED via a full docker-verb audit trail from a real --apply run that drops."""
    _stale_database(postgres, "phaze_audited_gc_test")
    real_docker = shutil.which("docker")
    assert real_docker is not None

    log = tmp_path / "docker-subcommands.log"
    bin_dir = tmp_path / "shim"
    bin_dir.mkdir()
    shim = bin_dir / "docker"
    shim.write_text(
        f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$1" >>{shlex.quote(str(log))}\nexec {shlex.quote(real_docker)} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    applied = _run(postgres, registry, "--apply", env=env)

    assert applied.returncode == 0, applied.stderr
    assert "phaze_audited_gc_test" not in _database_names(postgres), "the drop must actually have run for this assertion to mean anything"
    subcommands = {line.strip() for line in log.read_text(encoding="utf-8").splitlines() if line.strip()}
    assert subcommands, "the shim must have recorded at least one docker call"
    assert subcommands == {"exec"}, f"test-db-gc.sh must only ever `docker exec`, never stop/rm/create/run: saw {subcommands}"


def test_the_script_source_never_uses_a_docker_verb_other_than_exec() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for verb in ("stop", "rm", "create", "run"):
        assert f"docker {verb}" not in source, f"scripts/test-db-gc.sh must never call `docker {verb}`"


# ---------------------------------------------------------------------------------------------
# Wiring -- the recipe an operator actually reaches for
# ---------------------------------------------------------------------------------------------


def _recipe_body(name: str) -> str:
    justfile = _JUSTFILE.read_text(encoding="utf-8")
    _, _, after = justfile.partition(f"\n{name}")
    assert after, f"could not locate the `{name}` recipe in the justfile"
    return after.split("\n[doc(", 1)[0]


def test_the_gc_recipe_exists_and_is_a_dry_run_by_default() -> None:
    body = _recipe_body("test-db-gc *flags:")
    assert "scripts/test-db-gc.sh" in body
    assert "--apply" not in body.split("{{flags}}")[0], "the recipe must not force --apply itself"


def test_the_gc_recipe_never_tears_anything_down() -> None:
    body = _recipe_body("test-db-gc *flags:")
    assert "docker rm" not in body
    assert "docker stop" not in body
    assert "test-db-down" not in body
