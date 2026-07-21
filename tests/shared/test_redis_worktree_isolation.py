"""Redis worktree-isolation guard (phaze-fwo7).

``just test-db-for <name>`` isolated Postgres per worktree and said nothing about Redis, so every
concurrent seat landed on the same logical database, ``redis://localhost:6380/0``. That broke
parallel runs two ways:

* **Destructively** — three modules run a global ``scan_iter``+``delete`` sweep over ``exec:*``,
  ``exec_progress_req:*`` and ``tracklist_req:*`` in fixture setup AND teardown, so one seat's
  fixture deleted another seat's live keys mid-test.
* **Observationally** — assertions in ``test_execution_dispatch.py`` counted the *global* keyspace
  (``len(exec_keys) == 1``), so any concurrent seat holding an ``exec:*`` key failed them.

The observed symptom was a full-suite failure indistinguishable from a real regression, which
passed on isolated re-run. That is the expensive shape: it teaches reviewers to wave red away.

**What this module guards, and why it is these two things.** A test asserting that a sweep on DB
``n`` cannot reach DB ``m`` would assert a property of Redis, not of this repo — it could never
fail and would be worthless. The two things that genuinely regressed, and can regress again, are
both *configuration*:

1. A new redis-backed test module hardcodes a database index instead of honouring
   ``PHAZE_REDIS_URL``, silently opting itself back into the shared database.
2. ``just test-db-for`` stops emitting the ``PHAZE_REDIS_URL`` export, restoring the original trap
   where an engineer isolates Postgres and never learns Redis needs isolating too.

Both are checked against the real files. ``test_meta_guard_flags_*`` prove neither check is
vacuously green.

Reproducing the original defect (documented, not automated — a genuine two-process race does not
belong in the suite)::

    just test-db-for seat-a          # -> PHAZE_REDIS_URL=redis://localhost:6380/1
    just test-db-for seat-b          # -> PHAZE_REDIS_URL=redis://localhost:6380/2

    # BEFORE the fix, both seats used /0. Simulate that shared-database state:
    docker exec phaze-test-redis redis-cli -n 0 SET exec:victim live-data
    PHAZE_REDIS_URL=redis://localhost:6380/0 uv run pytest \
        tests/review/routers/test_execution_dispatch.py::test_dispatch_summary_in_redis_hash
    docker exec phaze-test-redis redis-cli -n 0 EXISTS exec:victim    # -> 0, DESTROYED

    # AFTER the fix, the seats hold different logical databases:
    docker exec phaze-test-redis redis-cli -n 1 SET exec:victim live-data
    PHAZE_REDIS_URL=redis://localhost:6380/2 uv run pytest \
        tests/review/routers/test_execution_dispatch.py::test_dispatch_summary_in_redis_hash
    docker exec phaze-test-redis redis-cli -n 1 EXISTS exec:victim    # -> 1, survived
"""

from __future__ import annotations

from pathlib import Path
import re


_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"
_JUSTFILE = _REPO_ROOT / "justfile"

# Matches a literal test-Redis DSN, e.g. "redis://localhost:6380/0". Port 6380 is the ephemeral
# test Redis (6379 is a developer's own Redis, and unit tests that merely parse a DSN without
# connecting legitimately use 6379 — those are not worktree-shared and so are out of scope).
# The host must START with an alphanumeric so documentation prose that elides the host
# ("Override via PHAZE_REDIS_URL=redis://...:6380/0") is not mistaken for a hardcoded DSN.
_TEST_REDIS_DSN = re.compile(r"redis://[A-Za-z0-9][A-Za-z0-9._-]*:6380/\d+")

# The only acceptable shape for such a literal: an env-var default, so an exported PHAZE_REDIS_URL
# always wins. `os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")` is fine; a bare
# assignment is not.
_ENV_DEFAULTED = re.compile(r"os\.environ\.get\(\s*[\"']PHAZE_REDIS_URL[\"']\s*,\s*[\"']redis://[A-Za-z0-9][A-Za-z0-9._-]*:6380/\d+[\"']\s*\)")


def _redis_dsn_offenders(text: str) -> list[str]:
    """Return test-Redis DSN literals in ``text`` that are NOT PHAZE_REDIS_URL env defaults.

    Works by blanking every well-formed ``os.environ.get("PHAZE_REDIS_URL", ...)`` default first,
    then reporting whatever 6380 DSN literals remain.
    """
    return _TEST_REDIS_DSN.findall(_ENV_DEFAULTED.sub("", text))


def test_test_redis_dsns_are_always_env_var_defaults() -> None:
    """Every hardcoded test-Redis DSN under tests/ must be a PHAZE_REDIS_URL fallback.

    A module that hardcodes the DSN outright ignores the per-worktree export and rejoins the
    shared database, reintroducing the cross-seat interference this guard exists to prevent.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if path == Path(__file__).resolve():
            continue
        found = _redis_dsn_offenders(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(_REPO_ROOT))] = found

    assert not offenders, (
        "test-Redis DSNs must be written as os.environ.get('PHAZE_REDIS_URL', '<dsn>') so the "
        f"per-worktree export from `just test-db-for <name>` takes effect. Offenders: {offenders}"
    )


def test_test_db_for_emits_a_redis_url_export() -> None:
    """`just test-db-for` must hand the engineer a PHAZE_REDIS_URL alongside the Postgres exports.

    The original defect was not that Redis isolation was impossible — it was that the recipe which
    taught the isolation workflow only ever mentioned Postgres. A fix requiring a separate,
    easily-forgotten second step would not be a fix.
    """
    justfile = _JUSTFILE.read_text(encoding="utf-8")
    recipe = justfile.split("test-db-for name:", 1)
    assert len(recipe) == 2, "could not locate the `test-db-for` recipe in the justfile"
    body = recipe[1].split("\n[doc(", 1)[0]

    assert "PHAZE_REDIS_URL" in body, "`just test-db-for` must print a PHAZE_REDIS_URL export, not just the Postgres ones"
    # Allocation must come from the atomic registry, not a hash of the name: `hash(name) % 16`
    # collides ~35% of the time across 8 seats, which would restore the bug intermittently.
    assert "INCR" in body and "HSETNX" in body, "Redis DB allocation must use the atomic INCR/HSETNX registry, not a hash-and-hope scheme"


def test_meta_guard_flags_a_hardcoded_dsn() -> None:
    """The DSN check is not vacuously green: a bare literal is reported, an env default is not."""
    assert _redis_dsn_offenders('_REDIS_URL = "redis://localhost:6380/0"') == ["redis://localhost:6380/0"]
    assert _redis_dsn_offenders('_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")') == []
