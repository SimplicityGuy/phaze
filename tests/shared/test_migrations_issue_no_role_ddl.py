"""No migration creates, alters or drops a role, or locks the shared role catalog (phaze-euo4a).

Production's app role has no ``CREATEROLE`` and is not a superuser, so any of these fails the upgrade and crash-loops
phaze on deploy: that is what 2026.11.0's migration 095 did. Roles are provisioned by the deployment, before phaze
starts (``docs/analytics-read-only-access.md``). ``tests/integration/test_migrations/test_restricted_app_role.py`` runs
the chain as such a role and is the test that proves it; this guard is the cheap, DB-free tripwire that names the line.

It reads every string constant a migration holds, the SQL it executes included, and skips docstrings, which are prose:
a docstring may explain why a role is NOT created without tripping the guard. Comments never reach the AST.
"""

import ast
from pathlib import Path
import re

import pytest


VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"

# ROLE / USER / GROUP are synonyms in Postgres DDL; pg_authid (and its pg_shadow view) is the shared role catalog.
FORBIDDEN = re.compile(r"\b(?:CREATE|ALTER|DROP)\s+(?:ROLE|USER|GROUP)\b|\bpg_authid\b|\bpg_shadow\b|\bREASSIGN\s+OWNED\b", re.IGNORECASE)


def _docstring_nodes(tree: ast.Module) -> set[int]:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                nodes.add(id(first.value))
    return nodes


def offending_strings(source: str) -> list[tuple[int, str]]:
    """(line, match) for each forbidden statement in a non-docstring string constant of ``source``."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.extend((node.lineno, match.group(0)) for match in FORBIDDEN.finditer(node.value))
    return found


MIGRATIONS = sorted(VERSIONS.glob("*.py"))


def test_the_migrations_are_found() -> None:
    assert len(MIGRATIONS) >= 10, f"expected the migration chain under {VERSIONS}"
    assert any(path.name.startswith("095_") for path in MIGRATIONS)


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda path: path.name)
def test_no_migration_issues_role_ddl(path: Path) -> None:
    offences = offending_strings(path.read_text(encoding="utf-8"))
    assert not offences, (
        f"{path.name} issues role DDL or touches the role catalog at {offences}: production's app role cannot. "
        "Provision roles in the deployment, before phaze starts, and only GRANT to them here (phaze-euo4a)."
    )


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE ROLE analytics_ro LOGIN",
        "create user x",
        "ALTER ROLE analytics_ro SET default_transaction_read_only = on",
        "ALTER\n    USER x PASSWORD 'p'",
        "DROP ROLE analytics_ro",
        "DROP GROUP g",
        "LOCK TABLE pg_catalog.pg_authid IN SHARE UPDATE EXCLUSIVE MODE",
        "REASSIGN OWNED BY a TO b",
    ],
)
def test_the_guard_catches_role_ddl_in_executed_sql(statement: str) -> None:
    """The guard is discriminating: each shape 2026.11.0's 095 used (and its synonyms) is caught in an executed string."""
    source = f'"""Docstring."""\nfrom alembic import op\n\n\ndef upgrade() -> None:\n    op.execute({statement!r})\n'
    assert offending_strings(source), statement


def test_the_guard_ignores_docstrings_and_grants() -> None:
    source = (
        '"""Module prose may say CREATE ROLE is not run here."""\n'
        "from alembic import op\n\n\n"
        "def upgrade() -> None:\n"
        '    """Nor ALTER ROLE."""\n'
        '    op.execute("GRANT SELECT ON analytics_files TO analytics_ro")\n'
        '    op.execute("REVOKE USAGE ON SCHEMA public FROM analytics_ro")\n'
        "    op.execute(\"SELECT 1 FROM pg_roles WHERE rolname = 'analytics_ro'\")\n"
    )
    assert offending_strings(source) == []


def test_the_guard_would_have_caught_the_released_095() -> None:
    """The 2026.11.0 tree's 095 role statements, verbatim in shape: each is reported."""
    released = (
        '"""095."""\n'
        'LOCK_ROLE_CATALOG = "LOCK TABLE pg_catalog.pg_authid IN SHARE UPDATE EXCLUSIVE MODE"\n'
        'ENSURE_ROLE = "DO $$ BEGIN CREATE ROLE analytics_ro LOGIN NOSUPERUSER; END $$"\n'
        'SET_READ_ONLY = "ALTER ROLE analytics_ro SET default_transaction_read_only = on"\n'
        'DROP_ROLE = "DO $$ BEGIN DROP ROLE analytics_ro; END $$"\n'
    )
    assert [match for _, match in offending_strings(released)] == ["pg_authid", "CREATE ROLE", "ALTER ROLE", "DROP ROLE"]
