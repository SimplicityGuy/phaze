"""Guard: an Alembic migration that ADDs a column must ANALYZE or backfill its table (phaze-gx8p2).

Follow-up from ``phaze-3agnm`` / migration ``068``. ``ALTER TABLE ... ADD COLUMN`` writes no rows,
so it does not count toward ``n_mod_since_analyze``. On a table that sees no further writes, the
new column never gets ``pg_stats`` and the planner falls back to default selectivity for any
predicate on it -- measured on host-prod: ``metadata.failed_at IS NULL`` was estimated at 57 rows
against a real 11,428, producing per-row nested loops on every ``/s/analyze`` render. Migration
``068`` fixed ``metadata`` and ``cloud_job`` once, for the columns known to be stat-less at the
time. Nothing stopped the NEXT ``ADD COLUMN`` from repeating the defect -- this guard is that stop.

Operator decision 2026-09-24 (dispatch session ``a9c86d1b``, ``AskUserQuestion``). Question as put:
"phaze-gx8p2, how to stop the next ALTER-added filter column from having no statistics?" Answer as
given (selected option label): "Guard test (Recommended)". Scope, as recorded on the bead: a
``tests/shared`` guard that flags any Alembic migration adding a column without an ANALYZE of that
table or a backfill UPDATE (the backfill leaves autoanalyze a reason to fire). Durable record: bead
``phaze-gx8p2``.

THE RULE. Every ``op.add_column(table, ...)`` call inside a migration's ``upgrade()`` must be
matched, in the SAME file's ``upgrade()``, by one of:

* an ``op.execute(...)`` (or ``op.execute(sa.text(...))``) whose SQL text is an ``ANALYZE`` of that
  table (``ANALYZE metadata`` / ``ANALYZE public.metadata``), the pattern migration ``068`` set; or
* an ``op.execute(...)`` whose SQL text is an ``UPDATE`` of that table -- a backfill leaves rows
  modified, which counts toward ``n_mod_since_analyze`` and gives autoanalyze a reason to fire on
  its own, so an explicit ``ANALYZE`` is not required on top of it; or
* a module-level ``ANALYZE_EXEMPT_TABLES`` dict naming that table with a reason (a call-site
  exemption, auditable exactly where the add_column lives, rather than a list buried in this test
  -- the CLAUDE.md branch-coverage section makes the same call for its own exemption flag: "an
  exemption at the call site is auditable; a lenient default is invisible to every bead downstream").

SCOPE IS ``upgrade()`` ONLY, DELIBERATELY. Several migrations here ``add_column`` only inside
``downgrade()`` -- reverting a column DROP, e.g. ``060_drop_analysis_sampled.py`` and
``066_drop_analysis_window_camelot.py``. That is not a forward deploy path adding a stat-less
column to a live database; it is what running a migration backward already accepts responsibility
for. Scanning ``downgrade()`` too would flag both as violations for a column that no live upgrade
path ever adds without the table already carrying whatever statistics it had before the drop.

WHY AST RATHER THAN REGEX. A regex scan of "ADD COLUMN" text cannot resolve ``op.add_column(_TABLE,
...)`` back to the string ``_TABLE`` was assigned to at module scope -- and every migration here
that names its target table via a module constant does exactly that (``_TABLE = "cloud_job"``).
``ast`` gives real scoping: which calls are inside ``upgrade()`` versus ``downgrade()``, and which
``Name`` resolves to which string literal.

WHAT THIS DOES NOT CATCH. A table name built at runtime (an f-string, a variable computed inside
``upgrade()`` rather than assigned as a bare module-level string constant) will not resolve and the
``add_column`` call is silently skipped for that table -- no migration in this repo does this today
(``_resolve_table_arg`` only follows a literal or a module-level string constant), so nothing is
currently invisible to it, but a future migration built that way would need a plainer form or its
own exemption. Likewise a backfill or ANALYZE issued through ``op.get_bind().execute(...)`` rather
than ``op.execute(...)`` is not recognized; no migration in this repo backfills that way today (the
few ``get_bind().execute`` call sites are read-only ``SELECT`` checks), so this is a known, currently
empty gap rather than a silent one.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import NamedTuple


REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"

# The module-level constant name a migration may define to exempt one of its own added-to tables.
# Shape: ``ANALYZE_EXEMPT_TABLES = {"table_name": "reason"}``.
EXEMPT_CONST_NAME = "ANALYZE_EXEMPT_TABLES"

# The identifier immediately following ANALYZE / UPDATE, optionally schema-qualified with
# ``public.``. Matches ``ANALYZE metadata``, ``ANALYZE public.metadata``, ``ANALYZE VERBOSE
# metadata``, ``UPDATE scan_batches SET ...``, ``UPDATE analysis AS a SET ...``.
_ANALYZE_TABLE_RE = re.compile(r"\banalyze\s+(?:verbose\s+)?(?:public\.)?([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
_UPDATE_TABLE_RE = re.compile(r"\bupdate\s+(?:only\s+)?(?:public\.)?([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)


class AddColumnFinding(NamedTuple):
    """One ``op.add_column`` call found inside a migration's ``upgrade()``."""

    file: str
    table: str


def _call_attr_name(node: ast.Call) -> str | None:
    """The attribute/function name of a call: ``op.add_column(...)`` -> ``"add_column"``."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _string_constants(module: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` (or annotated) assignments -- resolves ``_TABLE`` aliases."""
    consts: dict[str, str] = {}
    for node in module.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str):
            consts[target.id] = value.value
    return consts


def _exempt_tables(module: ast.Module, consts: dict[str, str]) -> dict[str, str]:
    """The module-level ``ANALYZE_EXEMPT_TABLES`` dict, if present: ``{table: reason}``.

    Keys are commonly the same module-level alias (``_TABLE``) ``add_column`` itself resolves
    through -- ``ast.literal_eval`` alone cannot evaluate a dict keyed by a ``Name`` node, so keys
    and values are each resolved individually rather than handing the whole dict literal to it.
    """
    for node in module.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if not (isinstance(target, ast.Name) and target.id == EXEMPT_CONST_NAME and isinstance(value, ast.Dict)):
            continue
        exempt: dict[str, str] = {}
        for key_node, val_node in zip(value.keys, value.values, strict=True):
            if key_node is None or val_node is None:
                continue
            table: str | None = None
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                table = key_node.value
            elif isinstance(key_node, ast.Name):
                table = consts.get(key_node.id)
            if table is None:
                continue
            try:
                reason = ast.literal_eval(val_node)
            except ValueError:
                continue
            if isinstance(reason, str) and reason.strip():
                exempt[table] = reason
        return exempt
    return {}


def _resolve_table_arg(call: ast.Call, consts: dict[str, str]) -> str | None:
    """The table name an ``op.add_column`` / similar call targets: a literal, or a resolved alias."""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return consts.get(arg.id)
    return None


def _sql_text(node: ast.expr) -> str | None:
    """Best-effort literal SQL text passed to ``op.execute(...)``.

    Handles a bare (possibly multi-line, implicitly-concatenated) string literal and the
    ``sa.text("...")`` / ``text("...")`` wrapper both are used in this repo's migrations.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and _call_attr_name(node) == "text" and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _find_function(module: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def scan_migration_text(text: str, filename: str = "<migration>") -> tuple[list[AddColumnFinding], list[str]]:
    """Scan one migration module's source for ``upgrade()`` ``add_column`` calls and their coverage.

    Returns ``(findings, violations)``: every ``add_column`` call found in ``upgrade()`` (whatever
    its table's coverage), and a human-readable violation string for each one whose table has
    neither a same-file ANALYZE, a same-file backfill UPDATE, nor an ``ANALYZE_EXEMPT_TABLES`` entry.
    """
    module = ast.parse(text, filename=filename)
    consts = _string_constants(module)
    exempt = _exempt_tables(module, consts)
    upgrade = _find_function(module, "upgrade")
    if upgrade is None:
        return [], []

    added_tables: list[str] = []
    covered_tables: set[str] = set()

    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        name = _call_attr_name(node)
        if name == "add_column":
            table = _resolve_table_arg(node, consts)
            if table is not None:
                added_tables.append(table)
        elif name == "execute" and node.args:
            sql = _sql_text(node.args[0])
            if sql is None:
                continue
            covered_tables.update(m.group(1) for m in _ANALYZE_TABLE_RE.finditer(sql))
            covered_tables.update(m.group(1) for m in _UPDATE_TABLE_RE.finditer(sql))

    findings = [AddColumnFinding(file=filename, table=table) for table in added_tables]
    violations = [
        f"{filename}: op.add_column({table!r}, ...) in upgrade() has no ANALYZE/backfill UPDATE of "
        f"{table!r} in this migration and no {EXEMPT_CONST_NAME} entry for it"
        for table in added_tables
        if table not in covered_tables and table not in exempt
    ]
    return findings, violations


def scan_repo_migrations(versions_dir: Path = VERSIONS_DIR) -> tuple[int, int, list[str]]:
    """Scan every real migration file under ``versions_dir``.

    Returns ``(files_scanned, add_column_calls_found, violations)`` -- the first two exist so
    callers can assert the guard did not pass vacuously (no files, or no add_column calls, found).
    """
    files = sorted(p for p in versions_dir.glob("*.py") if p.name != "__init__.py")
    violations: list[str] = []
    total_added = 0
    for path in files:
        findings, file_violations = scan_migration_text(path.read_text(encoding="utf-8"), filename=path.name)
        total_added += len(findings)
        violations.extend(file_violations)
    return len(files), total_added, violations


# ---------------------------------------------------------------------------------------------
# Guard: the real migration tree
# ---------------------------------------------------------------------------------------------


def test_the_guard_scans_a_nonzero_number_of_real_migrations_and_add_column_calls() -> None:
    """Scope check: the guard must not pass vacuously by finding nothing to check.

    A directory that resolved empty, or a corpus with no ``add_column`` call at all, would make
    every other assertion in this module trivially true for the wrong reason.
    """
    files_scanned, add_column_calls, _ = scan_repo_migrations()
    assert files_scanned > 0, f"no migration files found under {VERSIONS_DIR} -- the guard would pass vacuously"
    assert add_column_calls > 0, "no op.add_column call found in any migration's upgrade() -- the guard would pass vacuously"


def test_every_real_migration_that_adds_a_column_analyzes_backfills_or_is_exempt() -> None:
    """The actual guard: every ``add_column`` in every real migration is covered or exempt.

    A red here names the offending file and table directly in the assertion message -- fix it by
    adding an ``ANALYZE``/backfill ``UPDATE`` of that table to the same migration's ``upgrade()``,
    or by adding a reasoned ``ANALYZE_EXEMPT_TABLES`` entry.
    """
    _, _, violations = scan_repo_migrations()
    assert violations == [], f"migrations adding a column with no ANALYZE/backfill/exemption: {violations}"


def test_the_real_064_migration_is_recognized_as_covered_by_its_own_backfill_update() -> None:
    """Positive control against a REAL file: 064's ``UPDATE scan_batches ...`` covers its add_column."""
    path = VERSIONS_DIR / "064_orphan_companion_diagnostics.py"
    findings, violations = scan_migration_text(path.read_text(encoding="utf-8"), filename=path.name)
    assert findings == [AddColumnFinding(file=path.name, table="scan_batches")]
    assert violations == []


def test_the_real_068_migration_analyzes_the_tables_065_and_054_left_stat_less() -> None:
    """Positive control: 068's ``op.execute("ANALYZE public.cloud_job")`` is parsed as covering cloud_job."""
    path = VERSIONS_DIR / "068_analyze_stat_less_filter_columns.py"
    module = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    upgrade = _find_function(module, "upgrade")
    assert upgrade is not None
    covered: set[str] = set()
    for node in ast.walk(upgrade):
        if isinstance(node, ast.Call) and _call_attr_name(node) == "execute" and node.args:
            sql = _sql_text(node.args[0])
            if sql:
                covered.update(m.group(1) for m in _ANALYZE_TABLE_RE.finditer(sql))
    assert covered == {"metadata", "cloud_job"}


# ---------------------------------------------------------------------------------------------
# Mutation tests: synthetic migrations, proving the guard can go RED as well as green
# ---------------------------------------------------------------------------------------------

_VIOLATION_NO_COVERAGE = '''\
"""Synthetic: adds a column with no ANALYZE, no backfill, and no exemption."""

import sqlalchemy as sa

from alembic import op

revision = "999"
down_revision = "998"


def upgrade() -> None:
    op.add_column("widgets", sa.Column("gizmo", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("widgets", "gizmo")
'''

_COMPLIANT_VIA_ANALYZE = '''\
"""Synthetic: adds a column and ANALYZEs the same table."""

import sqlalchemy as sa

from alembic import op

revision = "999"
down_revision = "998"

_TABLE = "widgets"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("gizmo", sa.Text(), nullable=True))
    op.execute("ANALYZE public.widgets")


def downgrade() -> None:
    op.drop_column(_TABLE, "gizmo")
'''

_COMPLIANT_VIA_BACKFILL = '''\
"""Synthetic: adds a column and backfills it with an UPDATE of the same table."""

import sqlalchemy as sa

from alembic import op

revision = "999"
down_revision = "998"


def upgrade() -> None:
    op.add_column("widgets", sa.Column("gizmo", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE widgets SET gizmo = 'backfilled' WHERE gizmo IS NULL"))


def downgrade() -> None:
    op.drop_column("widgets", "gizmo")
'''

_COMPLIANT_VIA_EXEMPTION = '''\
"""Synthetic: adds a column with no ANALYZE/backfill, but carries a reasoned exemption."""

import sqlalchemy as sa

from alembic import op

revision = "999"
down_revision = "998"

_TABLE = "widgets"

ANALYZE_EXEMPT_TABLES = {
    _TABLE: "widgets is a synthetic high-write fixture table; this entry exists only to pin the guard's exemption path.",
}


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("gizmo", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "gizmo")
'''

_EXEMPTION_WITH_BLANK_REASON_DOES_NOT_COUNT = '''\
"""Synthetic: an exemption entry whose reason is blank must not silently exempt anything."""

import sqlalchemy as sa

from alembic import op

revision = "999"
down_revision = "998"

ANALYZE_EXEMPT_TABLES = {
    "widgets": "   ",
}


def upgrade() -> None:
    op.add_column("widgets", sa.Column("gizmo", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("widgets", "gizmo")
'''

_ADD_COLUMN_ONLY_IN_DOWNGRADE = '''\
"""Synthetic: mirrors 060/066 -- add_column reverts a DROP and lives only in downgrade()."""

import sqlalchemy as sa

from alembic import op

revision = "999"
down_revision = "998"


def upgrade() -> None:
    op.drop_column("widgets", "gizmo")


def downgrade() -> None:
    op.add_column("widgets", sa.Column("gizmo", sa.Text(), nullable=True))
'''


def test_synthetic_add_column_with_no_coverage_is_a_violation() -> None:
    """Mutation: no ANALYZE, no backfill, no exemption -- the guard goes RED."""
    findings, violations = scan_migration_text(_VIOLATION_NO_COVERAGE, filename="999_synthetic_violation.py")
    assert findings == [AddColumnFinding(file="999_synthetic_violation.py", table="widgets")]
    assert len(violations) == 1
    assert "widgets" in violations[0]
    assert "999_synthetic_violation.py" in violations[0]


def test_synthetic_add_column_covered_by_analyze_is_compliant() -> None:
    """Mutation: an ANALYZE of the added-to table in the same upgrade() clears the violation."""
    findings, violations = scan_migration_text(_COMPLIANT_VIA_ANALYZE, filename="999_synthetic_analyze.py")
    assert findings == [AddColumnFinding(file="999_synthetic_analyze.py", table="widgets")]
    assert violations == []


def test_synthetic_add_column_covered_by_backfill_update_is_compliant() -> None:
    """Mutation: a backfill UPDATE of the added-to table in the same upgrade() clears the violation."""
    findings, violations = scan_migration_text(_COMPLIANT_VIA_BACKFILL, filename="999_synthetic_backfill.py")
    assert findings == [AddColumnFinding(file="999_synthetic_backfill.py", table="widgets")]
    assert violations == []


def test_synthetic_add_column_with_reasoned_exemption_is_compliant() -> None:
    """Mutation: a reasoned ANALYZE_EXEMPT_TABLES entry for the table clears the violation."""
    findings, violations = scan_migration_text(_COMPLIANT_VIA_EXEMPTION, filename="999_synthetic_exempt.py")
    assert findings == [AddColumnFinding(file="999_synthetic_exempt.py", table="widgets")]
    assert violations == []


def test_synthetic_exemption_with_a_blank_reason_does_not_count() -> None:
    """Mutation: an exemption entry present in name only (blank reason) must not suppress the guard."""
    findings, violations = scan_migration_text(_EXEMPTION_WITH_BLANK_REASON_DOES_NOT_COUNT, filename="999_synthetic_blank.py")
    assert findings == [AddColumnFinding(file="999_synthetic_blank.py", table="widgets")]
    assert len(violations) == 1


def test_synthetic_add_column_only_in_downgrade_is_not_flagged() -> None:
    """Mutation: an add_column reverting a drop, living only in downgrade(), is out of scope."""
    findings, violations = scan_migration_text(_ADD_COLUMN_ONLY_IN_DOWNGRADE, filename="999_synthetic_downgrade_only.py")
    assert findings == []
    assert violations == []
