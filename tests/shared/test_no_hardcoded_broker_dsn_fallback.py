"""Guard: no test module may fall back to port 5432 when deriving a broker/test DSN (phaze-yxklc).

``tests/db_guard.py`` documents a single contract: an unset ``TEST_DATABASE_URL`` defaults to the
5433 ephemeral harness (``phaze_test``), precisely so a bare ``uv run pytest`` is safe. 5432 is the
port the justfile reserves for the DEVELOPER's own database (justfile:4-5).

WHAT THIS COST, measured 2026-08-10
------------------------------------

Eight test modules independently derived a SAQ/psycopg broker DSN with a copy-pasted expression::

    os.environ.get("PHAZE_QUEUE_URL") or os.environ.get(
        "TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze"
    ).replace(...)

That fallback bypasses ``tests/db_guard.py`` entirely -- it is not a ``TEST_DATABASE_URL`` default,
it is a SEPARATE, independently-hardcoded default naming the wrong port. With ``TEST_DATABASE_URL``
unset (a bare ``uv run pytest``, no ``just test-db-for`` seat exported) these modules aimed straight
at 5432. Measured impact: 5 failures across two modules as psycopg pools spent 30s timing out against
nothing on 5432 -- and on a machine where 5432 DOES hold a live ``phaze`` database, SAQ's ``init_db()``
would have created its tables in the developer's real data, the exact live-data-shape ``db_guard``
exists to prevent.

THE FIX, THE RULE
------------------

All eight sites now derive their DSN pair through ``tests.db_guard.integration_dsns()``, which starts
from the guard's safe 5433 default. This module is the regression lock: any ``tests/**.py`` file that
reintroduces an ``os.environ.get(...)`` call carrying a literal ``5432`` fallback fails the build.

Deliberately a source scan rather than a runtime assertion, for the same reason
``tests/shared/test_cluster_wide_catalog_scoping.py`` scans source: the defect is invisible unless the
environment variable happens to be unset when the module is collected, so a runtime check would pass
in exactly the conditions where the bug is dormant.
"""

from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOT = REPO_ROOT / "tests"

# This module's own docstring/pinning tests deliberately quote the offending shape (to prove the
# regex still recognises it) -- exclude it from the scan, the same way a spell-checker excludes its
# own dictionary of known-misspelled words.
_SELF = Path(__file__).resolve()

# `os.environ.get(...)` where the argument list carries a literal `5432` -- the exact shape of the
# eight historical offenders. `[^)]*` matches across a wrapped multi-line call (character classes
# match newlines unless explicitly excluded), and deliberately stops at the FIRST `)`: every offending
# site closed the `.get(...)` call immediately after the default string literal, before any `.replace(`.
_FALLBACK_5432_RE = re.compile(r"os\.environ\.get\([^)]*5432[^)]*\)")

# db_guard.py itself is the one legitimate place a 5433/5432 *comparison* lives (its own regression
# test, and the module explaining why 5432 must never be a default) -- it contains no `.get(...,
# ...5432...)` call, so excluding it isn't necessary, but scanning it is exactly the point: if someone
# "simplifies" db_guard's own resolver back to a 5432 default, this guard must catch that too.


def _sources() -> list[Path]:
    return sorted(path for path in SCANNED_ROOT.rglob("*.py") if path.resolve() != _SELF)


def _offending_files() -> list[str]:
    """Return test modules whose ``os.environ.get(...)`` calls carry a literal port-5432 fallback."""
    offenders = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        if _FALLBACK_5432_RE.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    return offenders


def test_no_test_module_hardcodes_a_5432_fallback_dsn() -> None:
    """Every DSN-deriving ``os.environ.get`` call must resolve through ``tests.db_guard``, not inline.

    Failure here means some module will silently aim at the developer's own database (or hang for
    30s against nothing) whenever ``TEST_DATABASE_URL`` is unset. Fix it by deriving the DSN pair
    through ``tests.db_guard.integration_dsns()`` instead of hand-rolling the fallback::

        from tests.db_guard import integration_dsns

        BROKER_DSN, SA_DSN = integration_dsns()
    """
    offenders = _offending_files()
    assert offenders == [], (
        "these modules hardcode a port-5432 fallback DSN, bypassing tests/db_guard.py's safe 5433 "
        f"default: {offenders}. Derive the DSN via tests.db_guard.integration_dsns() instead."
    )


def test_the_detector_recognises_the_shape_it_is_meant_to_catch() -> None:
    """Pin the regex against the exact historical offender (single- and multi-line) and its fix.

    Stops a later "simplification" of the pattern from silently disarming the guard.
    """
    single_line = (
        'BROKER_DSN = os.environ.get("PHAZE_QUEUE_URL") or os.environ.get('
        '"TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze").replace('
        '"postgresql+asyncpg://", "postgresql://")'
    )
    wrapped = (
        '_BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get(\n'
        '    "TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze"\n'
        ')).replace("postgresql+asyncpg://", "postgresql://")'
    )
    fixed = "BROKER_DSN, SA_DSN = integration_dsns()"
    unrelated_5432_literal = 'queue_url="postgresql://u:p@h:5432/d"'

    assert _FALLBACK_5432_RE.search(single_line) is not None
    assert _FALLBACK_5432_RE.search(wrapped) is not None
    assert _FALLBACK_5432_RE.search(fixed) is None
    assert _FALLBACK_5432_RE.search(unrelated_5432_literal) is None, (
        "a bare 5432 literal with no enclosing os.environ.get(...) call must not be flagged -- "
        "explicit test DSNs (e.g. monkeypatch.setenv literals) are not the fallback-default defect"
    )


def test_the_scan_actually_reaches_the_known_offender_shape() -> None:
    """The detector must be looking at real files -- an over-tight regex would pass vacuously.

    Confirms at least one currently-clean module that HISTORICALLY carried the offending shape is
    in the scanned set, so a regex that stops matching anything real would be caught by CI drift
    rather than silently reporting green forever.
    """
    known_fixed_sites = (
        "tests/agents/services/test_agent_task_router.py",
        "tests/integration/test_pg_queue_priority.py",
        "tests/integration/test_pg_dedup.py",
        "tests/integration/test_stage_status_equivalence.py",
        "tests/integration/test_files_page.py",
        "tests/integration/test_lifespan_orphan_task.py",
        "tests/analyze/tasks/test_recovery.py",
        "tests/analyze/tasks/test_ledger_backfill.py",
    )
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _sources()}
    for site in known_fixed_sites:
        assert site in scanned, f"{site} is no longer scanned -- the guard's coverage silently shrank"
