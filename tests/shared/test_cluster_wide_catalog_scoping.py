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

PER-OCCURRENCE, NOT PER-FILE (phaze-fmyvq)
-------------------------------------------

The original scan asked two file-wide questions -- does this file contain a ``FROM pg_locks`` /
``FROM pg_stat_activity`` match, AND does ``current_database()`` appear ANYWHERE in the same file
-- so a single scoped query anywhere in a module silently exempted every OTHER query in it. The
fix that closed phaze-esmn3 (an unscoped query added to ``tests/shared/test_redis_seat_registry.py``)
put ``current_database()`` into that file and, without this change, would have made it permanently
exempt: a second unscoped query added there later would have passed the guard vacuously.

The scan now walks every match of the query pattern and requires the scoping token to appear near
THAT occurrence, not merely somewhere in the file. Two refinements make that precise rather than a
guess:

* **Comments and docstrings are excluded from matching.** Several modules describe the historical
  bug or reference a query defined elsewhere in English/SQL-shorthand prose (e.g. db_guard.py's
  ``# ... SELECT 1 FROM pg_locks WHERE NOT granted ...`` explaining why the barrier changed, or a
  test docstring's ``select count(*) from pg_locks ...`` describing an assertion). Those are not
  executable queries and must not be forced to repeat a scoping predicate that lives in the real
  query a few dozen lines away. Detected via ``tokenize`` (``#`` comments) and ``ast`` (bare string
  expression statements -- what a docstring is).
* **A bounded character window, not the whole file, is searched for the scoping token** around
  each surviving occurrence. Every real occurrence in this codebase pairs its query with
  ``current_database()`` within roughly 250 characters (the SQL text is built from at most a
  handful of adjacent string literals or a short multi-line statement); the window is generous
  against that and still nowhere near "anywhere in the file".

**Deliberately, this now parses every scanned file with `ast`/`tokenize` and does not catch
`SyntaxError`/`TokenizeError`.** The old scan was pure regex and could not fail on malformed
input; this one can, and that is intentional rather than an oversight. A ``tests/`` or ``src/``
module this Python version cannot parse is already a broken repository state -- falling back to
a regex-only scan for such a file would risk silently downgrading it to the weaker, file-wide
check this bead exists to remove, in exactly the case where nobody is watching closely enough to
notice. A loud collection-time failure is the correct outcome, not a bug in the guard.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path
import re
import tokenize
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (REPO_ROOT / "tests", REPO_ROOT / "src")
_THIS_FILE = Path(__file__).resolve()

# A real query, not a mention: `FROM pg_locks`, `from pg_stat_activity l`, `join pg_locks`.
_QUERY_RE = re.compile(r"\b(?:from|join)\s+(pg_locks|pg_stat_activity)\b", re.IGNORECASE)

# The one accepted scoping predicate. `current_database()` is used rather than a literal database
# name so the same source works for every worktree's database without templating.
_SCOPE_TOKEN = "current_database()"

# How far from a real occurrence the scoping token may sit and still count as "the same query".
# The largest legitimate distance measured across the current codebase is ~215 characters (a
# multi-line SELECT ... FROM ... WHERE ... AND database = (...current_database())); this leaves
# comfortable headroom without approaching "anywhere in the file".
_WINDOW = 300


def _sources(roots: Sequence[Path] | None = None) -> list[Path]:
    roots = roots if roots is not None else SCANNED_ROOTS
    return sorted(path for root in roots for path in root.rglob("*.py") if root.exists())


def _char_offsets(text: str) -> list[int]:
    """Cumulative character offset at the start of each 1-indexed source line."""
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _prose_ranges(text: str) -> list[tuple[int, int]]:
    """Character ranges that are commentary rather than executable SQL.

    Two shapes: ``#`` comments (via ``tokenize``), and bare string-expression statements --
    docstrings, and any other string literal not assigned or passed to anything (via ``ast``). A
    scan that treated these as real queries would flag the historical-bug explanations in
    ``tests/db_guard.py`` and the docstring in
    ``tests/integration/test_tag_bulk_write_advisory_lock.py``, both of which describe a query in
    prose without being the query itself.

    Raises ``SyntaxError`` (from ``ast.parse``) or ``tokenize.TokenizeError`` uncaught if ``text``
    does not parse under this Python version. Not a fallback case: see the module docstring.
    """
    offsets = _char_offsets(text)

    def to_offset(row: int, col: int) -> int:
        return offsets[row - 1] + col

    ranges: list[tuple[int, int]] = []

    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            ranges.append((to_offset(*tok.start), to_offset(*tok.end)))

    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            assert node.end_lineno is not None
            assert node.end_col_offset is not None
            ranges.append((to_offset(node.lineno, node.col_offset), to_offset(node.end_lineno, node.end_col_offset)))

    return ranges


def _in_any_range(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in ranges)


def _unscoped_occurrences(text: str) -> list[str]:
    """Return a description of each real query occurrence in ``text`` lacking a nearby scoping token.

    "Real" excludes comments and docstrings (see ``_prose_ranges``); "nearby" is a bounded window
    around the occurrence, not the whole text -- the fix for phaze-fmyvq.
    """
    prose = _prose_ranges(text)
    offenders = []
    for match in _QUERY_RE.finditer(text):
        if _in_any_range(match.start(), prose):
            continue
        start = max(0, match.start() - _WINDOW)
        end = min(len(text), match.end() + _WINDOW)
        if _SCOPE_TOKEN not in text[start:end]:
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"line {line_no}: {match.group(0)!r}")
    return offenders


def _unscoped_query_files(roots: Sequence[Path] | None = None) -> list[str]:
    """Return files containing at least one unscoped occurrence of a cluster-wide catalogue query.

    Excludes this guard's own module: its docstring and self-tests deliberately contain example
    SQL fragments, both scoped and unscoped, to pin the detector's behaviour, and those examples
    are not real queries run against the database.
    """
    offenders = []
    for path in _sources(roots):
        if path.resolve() == _THIS_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        if _unscoped_occurrences(text):
            try:
                label = str(path.relative_to(REPO_ROOT))
            except ValueError:
                label = str(path)
            offenders.append(label)
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


def _scoped_query_snippet() -> str:
    """A correctly scoped advisory-lock count, in the exact shape the real code uses.

    Built from plain concatenated literals -- no interpolation -- specifically so this fixture
    text is not itself flagged as a query-construction-via-formatting risk by the security linter.
    """
    return (
        "_SCOPED_SQL = text(\n"
        "    \"select count(*) from pg_locks where locktype = 'advisory' \"\n"
        '    "and database = (select oid from pg_database where datname = current_database())"\n'
        ")\n"
    )


def _unscoped_query_snippet() -> str:
    """The same query with the scoping predicate removed."""
    return "_UNSCOPED_SQL = text(\"select count(*) from pg_locks where locktype = 'advisory'\")\n"


def _filler_lines(count: int = 60) -> str:
    """Enough unrelated comment lines to separate two occurrences well past ``_WINDOW``."""
    return "\n".join(f"# filler line {i} unrelated to either query" for i in range(count))


def test_a_scoped_occurrence_does_not_exempt_an_unscoped_occurrence_in_the_same_text() -> None:
    """Regression for phaze-fmyvq: the guard is per-occurrence, not per-file.

    Before this fix, one ``current_database()`` anywhere in a module satisfied the check for
    every query in it. Here two queries share one text -- one correctly scoped, one not, far
    enough apart that a file-wide substring search would (incorrectly) call the second one
    covered by the first.
    """
    text = "\n".join((_scoped_query_snippet(), _filler_lines(), "", _unscoped_query_snippet()))

    offenders = _unscoped_occurrences(text)

    assert len(offenders) == 1, f"expected exactly the unscoped occurrence to be flagged, got: {offenders}"
    assert "pg_locks" in offenders[0]


def test_a_fixture_module_with_one_scoped_and_one_unscoped_query_fails_the_guard(tmp_path: Path) -> None:
    """The same regression, proven at the ``_unscoped_query_files`` (whole-guard) level.

    A real module on disk, scanned the same way the guard scans ``tests/`` and ``src/`` -- this
    is the acceptance criterion for phaze-fmyvq, not just the lower-level helper.
    """
    fixture = tmp_path / "fixture_cluster_wide_query_module.py"
    fixture.write_text(
        "\n".join(("from sqlalchemy import text", "", _scoped_query_snippet(), _filler_lines(), "", _unscoped_query_snippet())),
        encoding="utf-8",
    )

    offenders = _unscoped_query_files(roots=[tmp_path])

    assert offenders == [str(fixture)], f"the fixture's unscoped query must be flagged, got: {offenders}"


def test_a_fixture_module_where_every_query_is_scoped_passes_the_guard(tmp_path: Path) -> None:
    """The counterpart to the previous test: no false positive when every occurrence is scoped."""
    fixture = tmp_path / "fixture_fully_scoped_module.py"
    fixture.write_text(
        "\n".join(("from sqlalchemy import text", "", _scoped_query_snippet())),
        encoding="utf-8",
    )

    assert _unscoped_query_files(roots=[tmp_path]) == []


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
