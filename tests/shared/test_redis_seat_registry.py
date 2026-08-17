"""Behaviour tests for the Redis logical-DB seat registry behind ``just test-db-for`` (phaze-68wky).

``just test-db-for <name>`` gives each worktree ("seat") an exclusive Redis logical database,
because two redis-backed test modules run global ``scan_iter``+``delete`` sweeps in fixture setup
AND teardown — on a shared logical database one seat's fixture deletes another seat's live keys
mid-test (phaze-fwo7, guarded by :mod:`tests.shared.test_redis_worktree_isolation`).

**The defect these tests pin.** Allocation used to be ``INCR phaze:test:redis-db-counter``: a
monotonic counter with no reclaim. Every seat that ever ran ``test-db-for`` burned an index
forever, so the counter walked past the 64-database cap and then refused every new seat — indices
68, 73, 74 and 80 were observed live, mostly held by worktrees that no longer existed. Refusing
was right (wrapping onto an occupied index restores phaze-fwo7 silently), but the only remedies
the error offered were ``just test-db-down`` variants, and ``test-db-down`` removes the ONE
Postgres+Redis pair every concurrent worktree shares — the phaze-ieqg incident, where a mid-round
teardown destroyed 89 per-worktree databases and this registry under five in-flight suites. Five
independent fleet reports (hq-6hb, hq-8zr, hq-el5, hq-ew2, hq-ij4) hit that wall, and each invented
a different workaround; one of them dropped Redis isolation altogether, i.e. straight back into
phaze-fwo7.

**Why these tests use a real Redis — and, for L2, a real Postgres.** The properties that regressed
are properties of the
*allocator's arithmetic and its evidence*, not of anything mockable: which index a seat is handed,
whether a released one comes back, and whether a seat still in use is protected. A fake would
re-implement exactly the logic under test. So each test drives ``scripts/redis-seat-registry.sh``
— the same entry point the justfile recipe calls — against a **throwaway** container this module
starts and removes itself. It has a random name, publishes **no port at all** (everything goes
through ``docker exec``), and is configured with a deliberately tiny 6-database space so
exhaustion is reachable in four allocations. It is never the shared ``phaze-test-redis`` on 6380;
touching that is what this bead exists to make unnecessary. The L2 tests start a throwaway
**Postgres** under the same discipline, for the same reason: L2 is the guard that stands between a
sweep and a running suite, and demonstrating it needs a real backend on a real database — which is
the one thing that must not happen on the shared ``phaze-test-db``.

The liveness rules the sweep applies are documented in the script's header. Restated here because
these tests are the executable form of them: a seat is IN USE while a Redis client is connected to
its logical DB (L1), while a Postgres backend is on its database (L2 — pytest holds one for the
whole session), or while its lease is unexpired (L3). Two conditions override L3 but never L1/L2:
its origin worktree is gone (O1), or its index is past the cap so no client could be using it (O2).
An entry with no lease stamp predates this mechanism, has genuinely unknown age, and is left alone
unless ``--include-unstamped`` is passed — see
:func:`test_unstamped_legacy_entries_are_left_alone_by_default`.

L2 in particular has three states, not two, and collapsing them is phaze-gmkua: evidence
*obtained* (an answer, empty or not), evidence *unavailable* (a container was named and did not
answer — which says nothing about any seat), and evidence *waived* by ``--no-postgres-check``. Only
the first is a finding.
"""

from __future__ import annotations

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


# tests/shared/test_redis_seat_registry.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "redis-seat-registry.sh"
_JUSTFILE = _REPO_ROOT / "justfile"

# 6 logical databases: DB 0 is the registry, so seats get 1..5 and the exhaustion path is four
# allocations away instead of sixty-three.
_CAPACITY = 6
_ALLOCATABLE = _CAPACITY - 1

_REGISTRY_KEY = "phaze:test:redis-db-index"
_SEEN_KEY = "phaze:test:redis-db-seen"
_ORIGIN_KEY = "phaze:test:redis-db-origin"
# The old monotonic counter's key, now the persisted all-time high-water mark (phaze-08sww).
_HIGHWATER_KEY = "phaze:test:redis-db-counter"

# A container name nothing will ever answer to, for the "L2 evidence was asked for and did not
# arrive" state. Distinct from passing no --pg-container at all, which is the operator declining to
# ask — telling those two apart is the whole of phaze-gmkua defect B.
_ABSENT_PG_CONTAINER = "phaze-seatreg-test-no-such-postgres"

# Matches docker-compose/CI (CLAUDE.md pins the harness to postgres:18-alpine), so the catalogue
# view these tests read is the one the script meets in production.
_PG_IMAGE = "postgres:18-alpine"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    """Every docker invocation in this module, in one place.

    Single entry point so the `S603`/`S607` waiver is stated once rather than at seven call sites:
    the executable is a literal, the arguments are test literals and a container name this module
    generated, and nothing here is shell-interpolated.
    """
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607 - literal argv, test-generated container name


def _docker_usable() -> bool:
    return shutil.which("docker") is not None and _docker("info").returncode == 0


pytestmark = pytest.mark.skipif(not _docker_usable(), reason="needs a working docker daemon to start a throwaway Redis")


@pytest.fixture(scope="module")
def throwaway_redis() -> Iterator[str]:
    """A private Redis container for this module alone.

    Deliberately NOT the shared ``phaze-test-redis``: a test that allocated, released and swept
    entries on the harness every other worktree depends on would be the very hazard this bead is
    about. No port is published, so it cannot collide with 6380 (or anything else) even if two
    worktrees run this module at the same time.
    """
    container = f"phaze-seatreg-test-{uuid4().hex[:10]}"
    started = _docker("run", "-d", "--name", container, "redis:7-alpine", "redis-server", "--databases", str(_CAPACITY))
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
    """A private Postgres for the L2 tests, held to the same discipline as the Redis one.

    L2 is the STRONGEST liveness signal the registry has and the only one that protects a running
    pytest: a suite holds a session-level advisory-lock connection for its whole run and frequently
    holds no Redis connection at all, so L1 and L3 can both look idle while a suite is mid-test.
    Exercising it needs a real backend on a real database — but never on the shared
    ``phaze-test-db``, where attaching sleeping backends and dropping ``phaze%`` databases is the
    2026-07-29 incident in miniature.
    """
    container = f"phaze-seatreg-pg-{uuid4().hex[:10]}"
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


@pytest.fixture
def postgres(throwaway_postgres: str) -> Iterator[str]:
    """Hand each test an empty Postgres: no ``phaze%`` databases and no backends left attached."""
    yield throwaway_postgres
    _psql(throwaway_postgres, "select pg_terminate_backend(pid) from pg_stat_activity where datname like 'phaze%'")
    for database in _psql(throwaway_postgres, "select datname from pg_database where datname like 'phaze%'").splitlines():
        if database.strip():
            _psql(throwaway_postgres, f'drop database if exists "{database.strip()}"')


def _psql(container: str, sql: str, *, database: str = "postgres") -> str:
    result = _docker("exec", container, "psql", "-U", "phaze", "-d", database, "-tAc", sql)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _seat_database(container: str, database: str) -> None:
    _psql(container, f'create database "{database}"')


def _attach_backend(container: str, database: str) -> None:
    """Hold a client backend open on ``database`` — the L2 signal, as a real connection.

    ``pg_sleep`` stands in for what pytest actually does: hold one connection open, doing nothing
    visible, for the whole session. That is the case no connection-to-Redis check can see.
    """
    attached = _docker("exec", "-d", container, "psql", "-U", "phaze", "-d", database, "-c", "select pg_sleep(600)")
    assert attached.returncode == 0, attached.stderr
    counted = f"select count(*) from pg_stat_activity where backend_type = 'client backend' and datname = '{database}'"  # noqa: S608 - test-literal database name, throwaway container
    for _ in range(80):
        if int(_psql(container, counted)) >= 1:
            return
        time.sleep(0.25)
    pytest.skip(f"could not attach a Postgres backend to {database}")


@pytest.fixture
def registry(throwaway_redis: str) -> Iterator[str]:
    """Hand each test an empty registry AND no parked clients; the container is reused for speed.

    Disconnecting matters as much as flushing: the L1 liveness signal is a connected client, so a
    ``BLPOP`` left parked by an earlier test would keep a later test's seat looking live and the
    sweep would correctly refuse to touch it — a false red that says nothing about the code.
    """
    _reset(throwaway_redis)
    yield throwaway_redis
    _reset(throwaway_redis)


def _reset(container: str) -> None:
    _redis(container, "CLIENT", "KILL", "TYPE", "normal")  # SKIPME defaults to yes, so this connection survives
    for _ in range(40):
        if "cmd=blpop" not in _redis(container, "CLIENT", "LIST"):
            break
        time.sleep(0.1)
    _redis(container, "FLUSHALL")


def _redis(container: str, *args: str) -> str:
    result = _docker("exec", container, "redis-cli", *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _registry_cli(container: str, *args: str) -> str:
    return _redis(container, "-n", "0", *args)


def _run(container: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Drive the real script, with the same arguments the justfile recipe passes it."""
    assert _SCRIPT.is_file(), f"seat registry script missing: {_SCRIPT}"
    return subprocess.run(  # noqa: S603 - fixed, in-repo executable; every argument is a test literal
        [str(_SCRIPT), args[0], "--redis-container", container, *args[1:]],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _interleave_at_client_list(tmp_path: Path, action: str, *, occurrence: int = 2) -> dict[str, str]:
    """Env that makes the script run ``action`` just before its ``occurrence``-th ``CLIENT LIST``.

    The window phaze-r311e lives in is *inside* one ``reclaim --apply`` run: between the single
    classification pass and the destruction that acts on its snapshot. Nothing in the script's
    interface exposes that moment, and a sleep-and-hope test would prove nothing about it. So this
    borrows the one thing the script demonstrably does at exactly that boundary — it reaches Redis
    through ``docker`` — and puts a shim on ``PATH`` that forwards every call to the real binary
    but fires ``action`` first on the Nth match.

    ``CLIENT LIST`` call 1 is classification's L1 read; call 2 is the re-verification the apply
    loop performs immediately before destroying a seat. Firing between them lands the interference
    precisely in the gap, deterministically, with no timing at all — and against the real script
    rather than a re-implementation of it. ``action`` runs with the ORIGINAL ``PATH``, so its own
    ``docker`` calls reach the real binary and cannot re-enter the shim.
    """
    real_docker = shutil.which("docker")
    assert real_docker is not None, "the module-level skip should have caught this"

    action_script = tmp_path / "interleaved-action.sh"
    action_script.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\nexport PATH={shlex.quote(os.environ['PATH'])}\n{action}\n", encoding="utf-8")
    action_script.chmod(0o755)

    counter = tmp_path / "client-list-calls"
    bin_dir = tmp_path / "shim-bin"
    bin_dir.mkdir()
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'case " $* " in\n'
        '  *" CLIENT LIST "*)\n'
        f"    seen=$(( $(cat {shlex.quote(str(counter))} 2>/dev/null || echo 0) + 1 ))\n"
        f'    printf %s "$seen" >{shlex.quote(str(counter))}\n'
        f'    if [ "$seen" = "{occurrence}" ]; then {shlex.quote(str(action_script))} >&2; fi\n'
        "    ;;\n"
        "esac\n"
        f'exec {shlex.quote(real_docker)} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _allocate(container: str, seat: str, *, origin: str | None = None, capacity: int = _CAPACITY) -> subprocess.CompletedProcess[str]:
    extra = ["--origin", origin] if origin is not None else []
    return _run(container, "allocate", "--seat", seat, "--capacity", str(capacity), *extra)


def _index_of(result: subprocess.CompletedProcess[str]) -> int:
    """The allocator prints the index on stdout and nothing else; humans get stderr."""
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def _allocated_seats(container: str) -> dict[str, str]:
    flat = _registry_cli(container, "HGETALL", _REGISTRY_KEY).splitlines()
    return dict(zip(flat[::2], flat[1::2], strict=True))


def _blocking_client(container: str, index: int) -> None:
    """Park a client on logical DB ``index`` — the L1 liveness signal, as a real connection."""
    parked = _docker("exec", "-d", container, "sh", "-c", f"redis-cli -n {index} BLPOP phaze-seatreg-test-idle 0")
    assert parked.returncode == 0, parked.stderr
    for _ in range(40):
        if f"db={index}" in _redis(container, "CLIENT", "LIST"):
            return
        time.sleep(0.1)
    pytest.skip("could not park a client on the throwaway Redis")


# ---------------------------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------------------------


def test_allocation_is_idempotent_for_one_seat(registry: str, tmp_path: Path) -> None:
    """Re-running for the same seat returns the SAME index and burns no new one.

    CLAUDE.md states this outright and every agent recipe relies on it — re-running ``test-db-for``
    to recover the exports for an existing worktree must not mint a second seat, which would strand
    the first index and orphan its keys. Under the old monotonic counter the ``HGET`` short-circuit
    provided this; the replacement has to provide it too, and from the same registry key.
    """
    first = _index_of(_allocate(registry, "seat_alpha", origin=str(tmp_path)))
    second = _index_of(_allocate(registry, "seat_alpha", origin=str(tmp_path)))
    third = _index_of(_allocate(registry, "seat_alpha", origin=str(tmp_path)))

    assert first == second == third
    assert 1 <= first < _CAPACITY, "DB 0 holds the registry and must never be handed to a seat"
    assert _allocated_seats(registry) == {"seat_alpha": str(first)}


def test_distinct_seats_never_share_an_index(registry: str, tmp_path: Path) -> None:
    """Two seats sharing one logical DB IS phaze-fwo7. Fill the space and check for duplicates."""
    indices = [_index_of(_allocate(registry, f"seat_{n}", origin=str(tmp_path))) for n in range(_ALLOCATABLE)]

    assert sorted(indices) == list(range(1, _CAPACITY)), "the allocatable space is 1..capacity-1, each handed out once"
    assert len(set(indices)) == len(indices)


def test_exhaustion_fails_loudly_and_never_wraps(registry: str, tmp_path: Path) -> None:
    """Past capacity the allocator refuses. A wrap would silently restore cross-seat interference.

    This is the one behaviour of the original design that was RIGHT, and the reclaim work must not
    trade it away: the fix for a full registry is to free a seat, never to double one up.
    """
    for n in range(_ALLOCATABLE):
        _index_of(_allocate(registry, f"seat_{n}", origin=str(tmp_path)))
    before = _allocated_seats(registry)

    overflow = _allocate(registry, "seat_overflow", origin=str(tmp_path))

    assert overflow.returncode == 3
    assert _allocated_seats(registry) == before, "a refused allocation must not touch the registry"


def test_exhaustion_names_the_non_destructive_remedy_first(registry: str, tmp_path: Path) -> None:
    """The error must lead with reclaim, and must not offer ``test-db-down`` as the fix.

    This is the whole reported defect, not a cosmetic point about wording. Five agents read this
    message, found only ``just test-db-down`` on offer, and went around it — one of them by
    dropping Redis isolation entirely. A remedy an operator must not run is not a remedy.
    """
    for n in range(_ALLOCATABLE):
        _allocate(registry, f"seat_{n}", origin=str(tmp_path))
    message = _allocate(registry, "seat_overflow", origin=str(tmp_path)).stderr

    assert "just test-db-reclaim" in message
    assert "just test-db-release" in message

    reclaim_at = message.index("just test-db-reclaim")
    resize_at = message.index("PHAZE_TEST_REDIS_DATABASES")
    assert reclaim_at < resize_at, "reclaiming must be offered before the remedy that recreates the container"

    teardown_lines = [line for line in message.splitlines() if "test-db-down" in line]
    assert teardown_lines, "the message should still name test-db-down — to warn AGAINST it"
    assert all("do NOT" in line or "phaze-ieqg" in line for line in teardown_lines), f"test-db-down must not be offered as a remedy: {teardown_lines}"


def test_a_freed_index_is_handed_out_again(registry: str, tmp_path: Path) -> None:
    """The reclaim property itself: once every index is held, releasing one unblocks a new seat.

    Under the monotonic counter this was impossible — the counter never looked at what was free,
    only at what it had already issued.
    """
    held = {seat: _index_of(_allocate(registry, seat, origin=str(tmp_path))) for seat in (f"seat_{n}" for n in range(_ALLOCATABLE))}
    assert _allocate(registry, "seat_new", origin=str(tmp_path)).returncode == 3

    released = _run(registry, "release", "--seat", "seat_2", "--capacity", str(_CAPACITY))
    assert released.returncode == 0, released.stderr

    assert _index_of(_allocate(registry, "seat_new", origin=str(tmp_path))) == held["seat_2"]


def test_a_never_used_index_is_preferred_over_a_recycled_one(registry: str, tmp_path: Path) -> None:
    """Recycling is the last resort, not the first — and releasing the TOP seat must not change that.

    Reclaim can only be as good as its evidence, and one hazard survives every check: a shell that
    still has a stale ``PHAZE_REDIS_URL`` exported keeps writing to its old index after the
    registry hands it back. Handing out untouched indices first means that hazard is only taken
    when the space genuinely demands it.

    This test used to release index 1, comfortably BELOW the high-water mark, and so passed against
    an allocator that derived the mark from the currently registered entries — where freeing the
    top seat collapses it and the very next allocation recycles an index that was in use seconds
    ago (phaze-08sww). Releasing the top index is the case that tells the two apart, and it is also
    the realistic one: a sweep frees the seats that are done, and the newest seats are the ones a
    stale export is most likely to still name.
    """
    for n in range(3):
        _allocate(registry, f"seat_{n}", origin=str(tmp_path))
    _run(registry, "release", "--seat", "seat_2", "--capacity", str(_CAPACITY))

    assert _index_of(_allocate(registry, "seat_fresh", origin=str(tmp_path))) == 4, "freeing the TOP index must not collapse the high-water mark"

    assert _index_of(_allocate(registry, "seat_filler", origin=str(tmp_path))) == 5
    assert _index_of(_allocate(registry, "seat_recycler", origin=str(tmp_path))) == 3, "with nothing untouched left, the freed index is reused"


def test_releasing_the_top_seat_of_a_pre_existing_registry_still_raises_the_mark(registry: str, tmp_path: Path) -> None:
    """The mark has to be raised BEFORE the entry is freed, because freeing erases the evidence.

    A registry written before this key existed carries no mark at all, and the entries themselves
    are the only record of what has been handed out. Reading them at allocation time is not enough:
    by then ``release`` has already deleted the one that mattered, and the allocator sees a mark of
    2 where the truth is 3. So both freeing paths raise the mark first (phaze-08sww).
    """
    for seat, index in (("seat_legacy_a", "1"), ("seat_legacy_b", "2"), ("seat_legacy_c", "3")):
        _registry_cli(registry, "HSET", _REGISTRY_KEY, seat, index)
    assert _registry_cli(registry, "GET", _HIGHWATER_KEY) == "", "the fixture starts with no mark, as a pre-existing container would"

    released = _run(registry, "release", "--seat", "seat_legacy_c", "--capacity", str(_CAPACITY))
    assert released.returncode == 0, released.stderr

    assert _index_of(_allocate(registry, "seat_next", origin=str(tmp_path))) == 4, "index 3 was handed out; the next seat must not be handed it back"


def test_an_out_of_range_allocation_is_repaired_in_place(registry: str, tmp_path: Path) -> None:
    """The 68/73/74/80 allocations the old counter minted are moved back into range.

    Redis refuses ``SELECT`` past its database count, so such a seat is already broken rather than
    live — repointing it cannot disturb anything, and leaving it alone would keep the worktree that
    owns it permanently unable to run the suite.
    """
    _registry_cli(registry, "HSET", _REGISTRY_KEY, "seat_wild", "68")

    repaired = _allocate(registry, "seat_wild", origin=str(tmp_path))

    assert _index_of(repaired) < _CAPACITY
    assert "out-of-range" in repaired.stderr


def test_a_recycled_index_is_cleared_before_handover(registry: str, tmp_path: Path) -> None:
    """A new seat must never inherit the previous holder's keys.

    Foreign keys inside a supposedly private database is phaze-fwo7 seen from the other side: the
    sweeps and keyspace-counting assertions in the redis-backed modules cannot tell them from
    their own.

    The space is filled first so that recycling is genuinely forced. This test used to allocate one
    seat, drop it, and allocate again — which only recycled because the high-water mark collapsed
    with the entry, i.e. because of the phaze-08sww defect. With the mark persisted, an untouched
    index is preferred and the handover under test never happened.
    """
    held = {seat: _index_of(_allocate(registry, seat, origin=str(tmp_path))) for seat in (f"seat_{n}" for n in range(_ALLOCATABLE))}
    index = held["seat_2"]
    _redis(registry, "-n", str(index), "SET", "exec:leftover", "stale-value")
    _registry_cli(registry, "HDEL", _REGISTRY_KEY, "seat_2")

    reused = _index_of(_allocate(registry, "seat_second", origin=str(tmp_path)))

    assert reused == index, "with nothing untouched left, the freed index is the one handed over"
    assert _redis(registry, "-n", str(index), "DBSIZE") == "0"


# ---------------------------------------------------------------------------------------------
# Release — the non-destructive path that did not exist
# ---------------------------------------------------------------------------------------------


def test_release_frees_the_index_and_clears_only_that_seats_keys(registry: str, tmp_path: Path) -> None:
    """Release touches one logical DB and the registry fields. Nothing else, and no container."""
    mine = _index_of(_allocate(registry, "seat_mine", origin=str(tmp_path)))
    theirs = _index_of(_allocate(registry, "seat_theirs", origin=str(tmp_path)))
    _redis(registry, "-n", str(mine), "SET", "exec:mine", "x")
    _redis(registry, "-n", str(theirs), "SET", "exec:theirs", "x")

    released = _run(registry, "release", "--seat", "seat_mine", "--capacity", str(_CAPACITY))

    assert released.returncode == 0, released.stderr
    assert _allocated_seats(registry) == {"seat_theirs": str(theirs)}
    assert _redis(registry, "-n", str(mine), "DBSIZE") == "0"
    assert _redis(registry, "-n", str(theirs), "DBSIZE") == "1", "another seat's keys are none of release's business"
    assert _registry_cli(registry, "HGET", _SEEN_KEY, "seat_mine") == ""
    assert _registry_cli(registry, "HGET", _ORIGIN_KEY, "seat_mine") == ""


def test_release_of_an_unknown_seat_is_a_no_op(registry: str) -> None:
    """Releasing a seat that holds nothing succeeds quietly — the recipe is safe to re-run."""
    result = _run(registry, "release", "--seat", "seat_absent", "--capacity", str(_CAPACITY))

    assert result.returncode == 0, result.stderr
    assert "nothing to release" in result.stdout


def test_release_of_a_corrupt_registry_value_does_not_read_it_as_a_regex(registry: str, tmp_path: Path) -> None:
    """A registry VALUE is data, and ``release`` used to hand it straight to a ``grep`` (phaze-nbfuc).

    ``classify_seats`` has always validated the value with ``^[0-9]+$`` before using it as an index;
    ``cmd_release`` did not. So a value of ``.*`` reached ``seat_is_redis_live``'s ``grep -qx`` as a
    REGEX, matched every ``CLIENT LIST`` line — including the registry's own connection, which is
    always there — and the seat reported permanently in use. It could not be released without
    ``--force``, which is a state no operator would diagnose from the message they got.

    It fails safe, so this is P3 rather than a data-loss bug: it refuses to free rather than freeing
    something live. But an unreleasable seat is exactly the exhaustion this whole registry exists to
    make recoverable.

    ``seat_bystander`` holds a real index with a real connection: the corrupt entry must not become
    a blanket refusal in the other direction either, and releasing it must not disturb the seat that
    the runaway regex used to match.
    """
    bystander = _index_of(_allocate(registry, "seat_bystander", origin=str(tmp_path)))
    _blocking_client(registry, bystander)
    _registry_cli(registry, "HSET", _REGISTRY_KEY, "seat_corrupt", ".*")

    released = _run(registry, "release", "--seat", "seat_corrupt", "--capacity", str(_CAPACITY))

    assert released.returncode == 0, released.stderr
    assert "corrupt registry value" in released.stderr, f"the operator has to be told what was wrong: {released.stderr}"
    assert ".*" in released.stderr
    assert _allocated_seats(registry) == {"seat_bystander": str(bystander)}, "the corrupt entry goes, the live seat stays"


def test_release_of_a_corrupt_registry_value_never_touches_logical_db_zero(registry: str, tmp_path: Path) -> None:
    """The sanitized index is 0, and 0 is the registry's own database — it must not be flushed.

    ``free_seat`` has always guarded its L1 check with ``-ge 1`` for this reason; the release path
    now sanitizes the same way, so the guard has to be there too. Flushing DB 0 would delete the
    entire registry, which is a far worse outcome than the unreleasable seat this fixes.
    """
    _allocate(registry, "seat_other", origin=str(tmp_path))
    _registry_cli(registry, "HSET", _REGISTRY_KEY, "seat_corrupt", "not-an-index")

    released = _run(registry, "release", "--seat", "seat_corrupt", "--capacity", str(_CAPACITY))

    assert released.returncode == 0, released.stderr
    assert "seat_other" in _allocated_seats(registry), "the registry itself must survive; DB 0 is where it lives"
    assert _registry_cli(registry, "HGET", _REGISTRY_KEY, "seat_corrupt") == ""
    assert "logical DB 0" not in released.stdout, f"nothing was freed from DB 0, so nothing should say so: {released.stdout}"


def test_release_refuses_while_a_client_is_connected(registry: str, tmp_path: Path) -> None:
    """L1: a connected client means a suite is live in that seat. Refuse, and say why."""
    index = _index_of(_allocate(registry, "seat_busy", origin=str(tmp_path)))
    _blocking_client(registry, index)

    refused = _run(registry, "release", "--seat", "seat_busy", "--capacity", str(_CAPACITY))

    assert refused.returncode == 4
    assert "connected" in refused.stderr
    assert _allocated_seats(registry) == {"seat_busy": str(index)}

    forced = _run(registry, "release", "--seat", "seat_busy", "--capacity", str(_CAPACITY), "--force")
    assert forced.returncode == 0, forced.stderr
    assert _allocated_seats(registry) == {}


# ---------------------------------------------------------------------------------------------
# Reclaim — the sweep, and what it refuses to do
# ---------------------------------------------------------------------------------------------


def _reclaim(container: str, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _run(container, "reclaim", "--capacity", str(_CAPACITY), "--no-postgres-check", *extra, env=env)


def _expire_lease(container: str, seat: str) -> None:
    """Backdate a seat's lease stamp well past the default 72h window."""
    _registry_cli(container, "HSET", _SEEN_KEY, seat, "100")


def test_reclaim_is_a_dry_run_until_apply(registry: str, tmp_path: Path) -> None:
    """The sweep reports before it acts. A destructive default is how phaze-ieqg happened."""
    index = _index_of(_allocate(registry, "seat_old", origin=str(tmp_path)))
    _expire_lease(registry, "seat_old")

    mark_before = _registry_cli(registry, "GET", _HIGHWATER_KEY)

    preview = _reclaim(registry)

    assert preview.returncode == 0, preview.stderr
    assert "would reclaim" in preview.stdout
    assert _allocated_seats(registry) == {"seat_old": str(index)}, "a dry run must change nothing"
    assert _registry_cli(registry, "GET", _HIGHWATER_KEY) == mark_before, "not even the high-water mark — a dry run means a dry run"

    applied = _reclaim(registry, "--apply")
    assert applied.returncode == 0, applied.stderr
    assert _allocated_seats(registry) == {}


def test_reclaim_frees_a_seat_whose_worktree_is_gone(registry: str, tmp_path: Path) -> None:
    """O1: the recorded origin directory no longer exists, so the seat cannot come back.

    This is the common case behind the wild indices — worktrees are removed when a bead lands, and
    nothing ever told the registry.
    """
    gone = tmp_path / "removed-worktree"
    gone.mkdir()
    _allocate(registry, "seat_gone", origin=str(gone))
    _allocate(registry, "seat_here", origin=str(tmp_path))
    gone.rmdir()

    applied = _reclaim(registry, "--apply")

    assert list(_allocated_seats(registry)) == ["seat_here"]
    assert "no longer exists" in applied.stdout


def test_reclaim_leaves_a_seat_with_a_live_lease_alone(registry: str, tmp_path: Path) -> None:
    """L3: a seat claimed recently is left alone even though nothing is connected to it.

    This is the case no connection-based check can see, and getting it wrong would be worse than
    the exhaustion it fixes: a developer between runs would have their index handed to someone else
    while their shell still exports it.
    """
    index = _index_of(_allocate(registry, "seat_idle", origin=str(tmp_path)))

    applied = _reclaim(registry, "--apply")

    assert _allocated_seats(registry) == {"seat_idle": str(index)}
    assert "left 1 in-use seat(s) alone" in applied.stdout


def test_reclaim_leaves_a_seat_with_a_connected_client_alone(registry: str, tmp_path: Path) -> None:
    """L1 beats an expired lease: evidence of use always outranks evidence of age."""
    index = _index_of(_allocate(registry, "seat_running", origin=str(tmp_path)))
    _expire_lease(registry, "seat_running")
    _blocking_client(registry, index)

    applied = _reclaim(registry, "--apply")

    assert _allocated_seats(registry) == {"seat_running": str(index)}, applied.stdout


def test_reclaim_leaves_a_live_seat_alone_even_when_its_worktree_is_gone(registry: str, tmp_path: Path) -> None:
    """O1 does not override L1 either. A suite running out of a deleted directory is still running."""
    gone = tmp_path / "deleted-mid-run"
    gone.mkdir()
    index = _index_of(_allocate(registry, "seat_zombie", origin=str(gone)))
    gone.rmdir()
    _blocking_client(registry, index)

    _reclaim(registry, "--apply")

    assert _allocated_seats(registry) == {"seat_zombie": str(index)}


def test_unstamped_legacy_entries_are_left_alone_by_default(registry: str) -> None:
    """An entry allocated before lease stamping has unknown age — and unknown is not stale.

    Backfilling a plausible age would be exactly the invented liveness test this design refuses;
    the honest move is to report them, leave them, and let each one stamp itself the next time its
    worktree runs ``test-db-for``. ``--include-unstamped`` clears the backlog deliberately.
    """
    _registry_cli(registry, "HSET", _REGISTRY_KEY, "seat_legacy", "3")

    default_sweep = _reclaim(registry, "--apply")
    assert _allocated_seats(registry) == {"seat_legacy": "3"}
    assert "predate lease stamping" in default_sweep.stdout

    opted_in = _reclaim(registry, "--apply", "--include-unstamped")
    assert opted_in.returncode == 0, opted_in.stderr
    assert _allocated_seats(registry) == {}


def test_a_sweep_does_not_make_the_next_allocation_recycle(registry: str, tmp_path: Path) -> None:
    """The high-water preference must survive the sweep, which is the moment it is needed most.

    ``--apply`` frees the seats an operator has just decided are done, so the indices it hands back
    are the ones a shell somewhere is most likely to still have exported. Deriving the mark from
    the surviving entries made it collapse to zero exactly then, and the next ``test-db-for`` was
    handed an index that had been in use seconds earlier (phaze-08sww).
    """
    for n in range(3):
        _allocate(registry, f"seat_{n}", origin=str(tmp_path))
        _expire_lease(registry, f"seat_{n}")

    swept = _reclaim(registry, "--apply")
    assert _allocated_seats(registry) == {}, swept.stdout

    assert _index_of(_allocate(registry, "seat_next", origin=str(tmp_path))) == 4, "an emptied registry must not reset the all-time mark"


def test_a_sweep_of_a_pre_existing_registry_still_raises_the_mark(registry: str, tmp_path: Path) -> None:
    """The sweep half of the same ordering rule, on a registry that carries no mark of its own.

    Entries written before this key existed are the only evidence that their indices were ever
    handed out, and ``--apply`` deletes all of them in one pass. Raising the mark from the snapshot
    before the first deletion is what stops the very next ``test-db-for`` from starting again at 1.
    """
    for seat, index in (("seat_legacy_a", "1"), ("seat_legacy_b", "2"), ("seat_legacy_c", "3")):
        _registry_cli(registry, "HSET", _REGISTRY_KEY, seat, index)

    swept = _reclaim(registry, "--apply", "--include-unstamped")
    assert _allocated_seats(registry) == {}, swept.stdout

    assert _index_of(_allocate(registry, "seat_next", origin=str(tmp_path))) == 4, "the swept indices were handed out and must not be first in line"


def test_reclaim_refuses_without_postgres_evidence(registry: str, tmp_path: Path) -> None:
    """L2 is mandatory: unknown must not be read as free.

    pytest holds a Postgres connection for its whole session and often none to Redis, so without
    that evidence a running suite looks idle. The sweep would then clear its keys mid-run and
    produce the branch-unrelated red that phaze-ieqg cost a round to disprove.
    """
    _allocate(registry, "seat_any", origin=str(tmp_path))

    blind = _run(registry, "reclaim", "--capacity", str(_CAPACITY), "--apply")

    assert blind.returncode == 1
    assert "--no-postgres-check" in blind.stderr
    assert _allocated_seats(registry) != {}, "a refused sweep must free nothing"


def test_reclaim_refuses_when_the_named_postgres_does_not_answer(registry: str, tmp_path: Path) -> None:
    """An unreachable Postgres is not evidence of an idle seat (phaze-gmkua defect B).

    The liveness query used to end ``2>/dev/null || true``, so a container that never answered
    returned an EMPTY database list — byte-identical to "no seat has a backend attached". A sweep
    would then read every running suite as idle while reporting nothing unusual.
    """
    _allocate(registry, "seat_any", origin=str(tmp_path))

    blind = _run(registry, "reclaim", "--capacity", str(_CAPACITY), "--pg-container", _ABSENT_PG_CONTAINER, "--apply")

    assert blind.returncode == 1
    assert _allocated_seats(registry) != {}, "a refused sweep must free nothing"


def test_no_postgres_check_warns_even_when_pg_container_is_also_passed(registry: str, tmp_path: Path) -> None:
    """The warning is gated on the EVIDENCE, not on which flags were typed (phaze-gmkua defect A).

    The old gate was ``--no-postgres-check AND no --pg-container``. But the justfile recipe always
    passes ``--pg-container``, so ``just test-db-reclaim --no-postgres-check`` — the only shape an
    operator ever types — swept with L2 missing and printed nothing at all. The tests never caught
    it because they only ever exercised the bare form.
    """
    _allocate(registry, "seat_old", origin=str(tmp_path))
    _expire_lease(registry, "seat_old")

    justfile_shape = _run(registry, "reclaim", "--capacity", str(_CAPACITY), "--pg-container", _ABSENT_PG_CONTAINER, "--no-postgres-check", "--apply")

    assert justfile_shape.returncode == 0, justfile_shape.stderr
    assert "⚠️" in justfile_shape.stderr, f"a sweep with L2 missing must say so: {justfile_shape.stderr}"
    assert "--no-postgres-check" in justfile_shape.stderr
    assert _allocated_seats(registry) == {}, "the waiver still lets the sweep proceed on L1/L3/O1/O2"


def test_list_reports_an_unreachable_postgres_instead_of_calling_seats_stale(registry: str, tmp_path: Path) -> None:
    """``list`` must not launder a missing L2 into a stale verdict either.

    ``test-db-seats`` is what an operator reads before deciding to sweep. If a Postgres that never
    answered made every seat look reclaimable, the recommendation it prints would be built on the
    absence of evidence rather than on evidence of absence.
    """
    gone = tmp_path / "vanished"
    gone.mkdir()
    _allocate(registry, "seat_gone", origin=str(gone))
    gone.rmdir()

    listing = _run(registry, "list", "--capacity", str(_CAPACITY), "--pg-container", _ABSENT_PG_CONTAINER)

    assert listing.returncode == 0, listing.stderr
    assert "did not answer" in listing.stderr
    row = next(line for line in listing.stdout.splitlines() if "seat_gone" in line)
    assert "in-use" in row, f"O1 must not fire while L2 is unknown: {row}"
    assert "unknown is never read as free" in row


def test_a_seat_reclaimed_mid_sweep_survives_the_sweep(registry: str, tmp_path: Path) -> None:
    """The TOCTOU that made the sweep able to destroy a live seat (phaze-r311e).

    ``reclaim`` classifies every seat ONCE and then destroys them from that snapshot — 5.46s for a
    five-seat sweep, 20s+ on a realistic registry. An agent running ``just test-db-for`` inside
    that window is handed its existing index back and its lease refreshed, correctly and
    atomically; the sweep then flushed the database anyway and freed the index for the next
    allocate. Two seats on one logical DB is phaze-fwo7, reached from a snapshot that was merely
    stale rather than wrong.

    The re-claim here happens at exactly that point — after classification, before destruction —
    and is the same command an agent would type.
    """
    index = _index_of(_allocate(registry, "seat_racer", origin=str(tmp_path)))
    _expire_lease(registry, "seat_racer")
    _redis(registry, "-n", str(index), "SET", "exec:in-flight", "x")
    reclaim_it = (
        f"{shlex.quote(str(_SCRIPT))} allocate --redis-container {shlex.quote(registry)} "
        f"--seat seat_racer --capacity {_CAPACITY} --origin {shlex.quote(str(tmp_path))} >/dev/null"
    )

    swept = _reclaim(registry, "--apply", env=_interleave_at_client_list(tmp_path, reclaim_it))

    assert swept.returncode == 0, swept.stderr
    assert _allocated_seats(registry) == {"seat_racer": str(index)}, swept.stdout
    assert _redis(registry, "-n", str(index), "DBSIZE") == "1", "the re-claimed seat's keys must survive"
    assert "re-claimed since classification" in swept.stdout


def test_a_seat_that_connects_mid_sweep_survives_the_sweep(registry: str, tmp_path: Path) -> None:
    """L1 is re-read immediately before destruction, not only at classification.

    The lease-stamp comparison catches a seat that re-ran ``test-db-for``. This catches the other
    half: a suite that was merely idle when the sweep started and opened its Redis connection
    while the sweep was still walking the registry.
    """
    index = _index_of(_allocate(registry, "seat_waker", origin=str(tmp_path)))
    _expire_lease(registry, "seat_waker")
    connect = (
        f"docker exec -d {shlex.quote(registry)} sh -c 'redis-cli -n {index} BLPOP phaze-seatreg-test-idle 0'\n"
        "for _ in $(seq 1 40); do\n"
        f"  if docker exec {shlex.quote(registry)} redis-cli CLIENT LIST | grep -q ' db={index} '; then exit 0; fi\n"
        "  sleep 0.1\n"
        "done\n"
        "exit 1\n"
    )

    swept = _reclaim(registry, "--apply", env=_interleave_at_client_list(tmp_path, connect))

    assert swept.returncode == 0, swept.stderr
    assert _allocated_seats(registry) == {"seat_waker": str(index)}, swept.stdout
    assert "connected to DB" in swept.stdout


# ---------------------------------------------------------------------------------------------
# L2 — the strongest signal, against a real Postgres (phaze-esmn3)
#
# Every reclaim test above passes --no-postgres-check, so until this section existed the branch
# that protects a RUNNING SUITE was the one branch nothing exercised. That gap is also what let
# phaze-gmkua hide: the justfile's real argument shape was never covered either.
# ---------------------------------------------------------------------------------------------


def _reclaim_with_postgres(container: str, pg: str, *extra: str) -> subprocess.CompletedProcess[str]:
    """The justfile's real argument shape: --pg-container present, no --no-postgres-check."""
    return _run(container, "reclaim", "--capacity", str(_CAPACITY), "--pg-container", pg, *extra)


def _stale_seat(container: str, seat: str, tmp_path: Path) -> int:
    """A seat that L1, L3 and O1 all agree is reclaimable, so only L2 can save it."""
    gone = tmp_path / f"worktree-{seat}"
    gone.mkdir()
    index = _index_of(_allocate(container, seat, origin=str(gone)))
    gone.rmdir()
    _expire_lease(container, seat)
    return index


def test_reclaim_refuses_to_free_a_seat_with_a_live_postgres_backend(registry: str, postgres: str, tmp_path: Path) -> None:
    """The guard that stands between a sweep and a running suite, exercised for real.

    pytest holds one Postgres connection for its entire session and often none to Redis, so a suite
    that is merely thinking looks idle to L1 and — after 72 hours, or the moment its worktree is
    removed — to L3 and O1 as well. L2 is the only thing left, and it was never under test.
    """
    index = _stale_seat(registry, "seat_pytest", tmp_path)
    _seat_database(postgres, "phaze_seat_pytest_test")
    _attach_backend(postgres, "phaze_seat_pytest_test")
    _redis(registry, "-n", str(index), "SET", "exec:mid-run", "x")

    swept = _reclaim_with_postgres(registry, postgres, "--apply")

    assert swept.returncode == 0, swept.stderr
    assert _allocated_seats(registry) == {"seat_pytest": str(index)}, swept.stdout
    assert _redis(registry, "-n", str(index), "DBSIZE") == "1", "a running suite's keys must survive the sweep"
    assert "left 1 in-use seat(s) alone" in swept.stdout


def test_the_migrations_database_counts_as_evidence_too(registry: str, postgres: str, tmp_path: Path) -> None:
    """``test-db-for`` hands out a PAIR of databases, so both must be watched.

    A migrations run holds its connection on the second one only. Matching just ``phaze_<seat>_test``
    would read that seat as idle.
    """
    index = _stale_seat(registry, "seat_migrating", tmp_path)
    _seat_database(postgres, "phaze_seat_migrating_migrations_test")
    _attach_backend(postgres, "phaze_seat_migrating_migrations_test")

    swept = _reclaim_with_postgres(registry, postgres, "--apply")

    assert _allocated_seats(registry) == {"seat_migrating": str(index)}, swept.stdout


def test_l2_protects_only_the_seat_it_names(registry: str, postgres: str, tmp_path: Path) -> None:
    """The matcher must be exact, or L2 becomes a blanket refusal that frees nothing.

    ``seat_bus`` is a strict prefix of ``seat_busy`` and ``seat_busy_extra`` extends it, so a
    substring match would let one live suite pin two idle seats. A guard that never releases
    anything is how the registry filled up in the first place.
    """
    busy = _stale_seat(registry, "seat_busy", tmp_path)
    _stale_seat(registry, "seat_bus", tmp_path)
    _stale_seat(registry, "seat_busy_extra", tmp_path)
    for seat in ("seat_busy", "seat_bus", "seat_busy_extra"):
        _seat_database(postgres, f"phaze_{seat}_test")
    _attach_backend(postgres, "phaze_seat_busy_test")

    swept = _reclaim_with_postgres(registry, postgres, "--apply")

    assert _allocated_seats(registry) == {"seat_busy": str(busy)}, swept.stdout


def test_the_justfile_shape_still_consults_postgres_under_no_postgres_check(registry: str, postgres: str, tmp_path: Path) -> None:
    """``--no-postgres-check`` waives the REFUSAL, not evidence that is there for the taking.

    ``just test-db-reclaim --no-postgres-check`` always passes ``--pg-container`` too, because the
    recipe does. A reachable Postgres therefore still protects a running suite on that path — and
    the sweep says which of the two it did, because the operator asked for one and got the other.
    """
    index = _stale_seat(registry, "seat_waived", tmp_path)
    _seat_database(postgres, "phaze_seat_waived_test")
    _attach_backend(postgres, "phaze_seat_waived_test")

    swept = _run(registry, "reclaim", "--capacity", str(_CAPACITY), "--pg-container", postgres, "--no-postgres-check", "--apply")

    assert swept.returncode == 0, swept.stderr
    assert _allocated_seats(registry) == {"seat_waived": str(index)}, swept.stdout
    assert "--no-postgres-check" in swept.stderr, "the waiver must be reported whether or not it changed the outcome"
    assert "⚠️" not in swept.stderr, f"nothing was missing, so nothing should be warned about: {swept.stderr}"


def test_release_refuses_while_a_postgres_backend_is_attached(registry: str, postgres: str, tmp_path: Path) -> None:
    """``release`` names one seat, but L2 still outranks the operator's assertion — until --force."""
    index = _index_of(_allocate(registry, "seat_running", origin=str(tmp_path)))
    _seat_database(postgres, "phaze_seat_running_test")
    _attach_backend(postgres, "phaze_seat_running_test")

    refused = _run(registry, "release", "--seat", "seat_running", "--capacity", str(_CAPACITY), "--pg-container", postgres)

    assert refused.returncode == 4
    assert "Postgres" in refused.stderr
    assert _allocated_seats(registry) == {"seat_running": str(index)}

    forced = _run(registry, "release", "--seat", "seat_running", "--capacity", str(_CAPACITY), "--pg-container", postgres, "--force")
    assert forced.returncode == 0, forced.stderr
    assert _allocated_seats(registry) == {}


def test_list_reports_the_evidence_behind_every_verdict(registry: str, tmp_path: Path) -> None:
    """``just test-db-seats`` exists so a full registry reads as a list of seats, not a dead end."""
    _allocate(registry, "seat_live", origin=str(tmp_path))
    gone = tmp_path / "vanished"
    gone.mkdir()
    _allocate(registry, "seat_dead", origin=str(gone))
    gone.rmdir()
    _registry_cli(registry, "HSET", _REGISTRY_KEY, "seat_legacy", "5")
    before = _allocated_seats(registry)

    listing = _run(registry, "list", "--capacity", str(_CAPACITY))

    assert listing.returncode == 0, listing.stderr
    assert "seat_live" in listing.stdout
    assert "in-use" in listing.stdout
    assert "stale" in listing.stdout
    assert "unknown" in listing.stdout
    assert _allocated_seats(registry) == before, "list is read-only"


# ---------------------------------------------------------------------------------------------
# Wiring — the recipes an operator actually reaches for
# ---------------------------------------------------------------------------------------------


def _recipe_body(name: str) -> str:
    """The text of one justfile recipe, from its signature to the next `[doc(` attribute."""
    justfile = _JUSTFILE.read_text(encoding="utf-8")
    _, _, after = justfile.partition(f"\n{name}")
    assert after, f"could not locate the `{name}` recipe in the justfile"
    return after.split("\n[doc(", 1)[0]


def _recipe_commands(name: str) -> str:
    """The same recipe with comment lines stripped.

    These recipes discuss ``test-db-down`` and the old ``INCR`` counter at length — that is the
    rationale a reader needs at the moment they are choosing between them. Assertions about what a
    recipe *does* must therefore read the commands, not the prose, or every explanation becomes a
    test failure and the pressure is to delete the explanation.
    """
    return "\n".join(line for line in _recipe_body(name).splitlines() if not line.strip().startswith("#"))


def test_test_db_for_delegates_allocation_to_the_registry_script() -> None:
    """The recipe must not carry a second, drifting copy of the allocator.

    ``derive-seat-name.sh`` and ``ensure-pg-database.sh`` already established this: the recipe
    orchestrates, the scripts decide, and the scripts are what the tests can drive.
    """
    body = _recipe_commands("test-db-for name:")

    assert "scripts/redis-seat-registry.sh allocate" in body
    assert "INCR" not in body, "the monotonic counter is the defect; it must not survive in the recipe"


def test_the_non_destructive_recipes_exist_and_do_not_tear_anything_down() -> None:
    """``test-db-release`` / ``test-db-reclaim`` / ``test-db-seats`` are the point of the bead.

    Their absence is what left ``test-db-down`` as the only offered remedy, so a regression that
    deleted them would restore the reported defect in full.
    """
    for recipe, subcommand in (
        ("test-db-release name *flags:", "release"),
        ("test-db-reclaim *flags:", "reclaim"),
        ("test-db-seats:", "list"),
    ):
        body = _recipe_commands(recipe)
        assert f"scripts/redis-seat-registry.sh {subcommand}" in body
        assert "docker rm" not in body, f"`{recipe}` must never remove a container"
        assert "test-db-down" not in body, f"`{recipe}` is the alternative to test-db-down, not a wrapper for it"


def test_the_teardown_guards_point_at_reclaim_instead() -> None:
    """Both ``docker rm`` paths on the shared harness must name the non-destructive route.

    An operator who reaches ``test-db-down`` or the logical-DB resize because the registry filled
    up is one step from the phaze-ieqg incident; that is the moment to say ``test-db-reclaim``.
    """
    for recipe in ("test-db-down:", "test-db:"):
        body = _recipe_body(recipe)
        assert "test-db-reclaim" in body, f"`{recipe}`'s guard should offer reclaim before a teardown"
