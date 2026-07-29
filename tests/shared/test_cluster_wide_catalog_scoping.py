"""Guard: every ``pg_locks`` / ``pg_stat_activity`` query must be scoped to its own database (phaze-ieqg).

``TEST_DATABASE_URL`` gives each worktree its own database on ONE shared Postgres cluster. That
isolates table data completely -- and isolates the system catalogues not at all. ``pg_locks`` and
``pg_stat_activity`` are cluster-wide views: they show every seat's backends and every seat's locks,
including seats testing entirely different branches.

WHAT THIS COST, measured 2026-07-29
-----------------------------------

Two full suites running concurrently in separate worktrees, each with its own database and Redis
logical DB (the phaze-fwo7 isolation, correctly applied), both went red on
``tests/integration/test_tag_bulk_write_advisory_lock.py``::

    assert count == 1, "acquiring and releasing on a churning session must leak the lock"
    AssertionError: assert 2 == 1

The count was ``select count(*) from pg_locks where locktype = 'advisory' and classid = ... and
objid = ...`` with no ``database`` predicate, so it counted the OTHER seat's copy of the same
application lock key. Both the ``count == 1`` (leak expected) and ``count == 0`` (leak fixed)
assertions in that module are corrupted the same way -- one reads high, the other reads non-zero.

The same defect in a nastier form sat in three concurrency modules' barrier helper,
``SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted)``: satisfied by ANY blocked backend
anywhere in the cluster, so the barrier returned before the test's own waiter had queued and
everything after it raced. That produces intermittent failures in modules the branch under test
never touched, green on isolated re-run -- the signature that made "the suite is unreliable under
concurrency" look like an unfindable shared surface.

THE RULE
--------

Any module that QUERIES ``pg_locks`` or ``pg_stat_activity`` must qualify the query with
``current_database()``. Prose that merely mentions the view is fine; this checks for the ``FROM``.

Deliberately a source scan rather than a runtime assertion, for the same reason
``tests/shared/test_redis_worktree_isolation.py`` scans source: the defect is invisible on a
single-seat run, so a runtime check would pass in exactly the conditions where the bug is dormant
and only fail on the shared machine where the evidence is hardest to collect.
"""

from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (REPO_ROOT / "tests", REPO_ROOT / "src")

# A real query, not a mention: `FROM pg_locks`, `from pg_stat_activity l`, `join pg_locks`.
_QUERY_RE = re.compile(r"\b(?:from|join)\s+(pg_locks|pg_stat_activity)\b", re.IGNORECASE)

# The one accepted scoping predicate. `current_database()` is used rather than a literal database
# name so the same source works for every worktree's database without templating.
_SCOPE_TOKEN = "current_database()"


def _sources() -> list[Path]:
    return sorted(path for root in SCANNED_ROOTS for path in root.rglob("*.py"))


def _unscoped_query_files() -> list[str]:
    """Return files that query a cluster-wide view without a ``current_database()`` predicate."""
    offenders = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        if _QUERY_RE.search(text) and _SCOPE_TOKEN not in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    return offenders


def test_no_module_queries_a_cluster_wide_view_without_scoping_it() -> None:
    """Every ``FROM pg_locks`` / ``FROM pg_stat_activity`` lives beside a ``current_database()`` filter.

    Failure here means a new query will see other worktrees' backends. Fix it by adding, for an
    advisory-lock count::

        and database = (select oid from pg_database where datname = current_database())

    or, when the lock type may be ``transactionid`` (a row-level ``FOR UPDATE`` waiter), where
    ``pg_locks.database`` is NULL, by joining the waiting backend instead::

        join pg_stat_activity a on a.pid = l.pid where a.datname = current_database()

    ``tests/db_guard.BLOCKED_WAITER_SQL`` is the shared, already-correct form of the second.
    """
    offenders = _unscoped_query_files()
    assert offenders == [], (
        "these modules query a CLUSTER-WIDE catalogue view without restricting it to their own "
        f"database, so they observe other worktrees' seats: {offenders}"
    )


def test_the_scan_actually_reaches_the_known_query_sites() -> None:
    """The detector must be looking at real files -- an over-tight regex would pass vacuously.

    A guard whose pattern silently matches nothing is worse than no guard: it reports green
    forever. Pinned to the two modules that carry these queries today. (The three barrier modules
    are deliberately absent: they now import ``BLOCKED_WAITER_SQL`` rather than inline the SQL,
    which is the outcome ``test_the_three_barrier_modules_share_one_definition`` pins.)
    """
    matched = {str(p.relative_to(REPO_ROOT)) for p in _sources() if _QUERY_RE.search(p.read_text(encoding="utf-8"))}
    for expected in ("tests/db_guard.py", "tests/integration/test_tag_bulk_write_advisory_lock.py"):
        assert expected in matched, f"{expected} queries a cluster-wide view but the detector missed it"


def test_the_detector_recognises_the_shapes_it_is_meant_to_catch() -> None:
    """Both the regex and the scoping token behave on the exact strings this bead removed.

    Pinning the detector against the two historical offenders (and their fixed forms) is what
    stops a later "simplification" of the regex from silently disarming the guard.
    """
    unscoped_barrier = 'text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted)")'
    unscoped_count = "select count(*) from pg_locks where locktype = 'advisory' and classid = :classid"
    prose_only = "``pg_locks`` is cluster-wide -- see the module docstring."

    assert _QUERY_RE.search(unscoped_barrier) and _SCOPE_TOKEN not in unscoped_barrier
    assert _QUERY_RE.search(unscoped_count) and _SCOPE_TOKEN not in unscoped_count
    assert _QUERY_RE.search(prose_only) is None, "a prose mention with no FROM/JOIN must not be flagged"

    scoped_count = (
        "select count(*) from pg_locks where locktype = 'advisory' and classid = :classid "
        "and database = (select oid from pg_database where datname = current_database())"
    )
    assert _QUERY_RE.search(scoped_count) is not None, "the fixed form is still a query"
    assert _SCOPE_TOKEN in scoped_count, "...and it must now satisfy the scoping check"


def test_the_three_barrier_modules_share_one_definition() -> None:
    """The blocked-waiter barrier is imported, not re-typed, in each module that uses it.

    It was three copies of one expression, and all three carried the same bug. One definition in
    ``tests/db_guard`` means the next correction lands everywhere at once -- the argument that
    module's own docstring makes about the test-database predicate.
    """
    for module in (
        "tests/integration/test_stage_pause_resume_lock.py",
        "tests/integration/test_scan_deletion_concurrency.py",
        "tests/integration/test_scan_reaper_concurrency.py",
    ):
        text = (REPO_ROOT / module).read_text(encoding="utf-8")
        assert "from tests.db_guard import BLOCKED_WAITER_SQL" in text, f"{module} must import the shared barrier"
        assert "FROM pg_locks WHERE NOT granted" not in text, f"{module} re-inlined the unscoped barrier"
