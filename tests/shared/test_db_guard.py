"""Coverage for the single test-database guard (``tests/db_guard.py``).

The guard's whole value is its FAILURE mode, so that is what these tests pin. Two regressions
are specifically defended against, because both were live defects:

* a wrongly-named database ``skip``ping instead of erroring (phaze-laqf) -- roughly 18
  integration tests vanished from runs that still reported green;
* a DSN defaulting to port 5432 (phaze-osgt), the port the justfile reserves for the
  developer's own database, against which these fixtures create and drop schema.
"""

from __future__ import annotations

import pytest

from tests.db_guard import (
    DEFAULT_TEST_DATABASE_URL,
    NonTestDatabaseError,
    coerce_async_dsn,
    database_name,
    integration_dsns,
    is_test_database_name,
    require_test_database,
    resolve_test_dsn,
)


# --- the predicate: a `test` SEGMENT anywhere, not merely a suffix -------------------------


@pytest.mark.parametrize(
    "name",
    [
        "phaze_test",  # the shared harness
        "phaze_test_m7ya",  # per-worktree SUFFIX form -- the case the old guard rejected
        "phaze_m7ya_test",  # per-worktree INFIX form -- the case the old guard accepted
        "phaze_migrations_test",
        "test_phaze",  # leading segment
        "test",  # the whole name
    ],
)
def test_accepts_every_test_database_naming_shape(name: str) -> None:
    assert is_test_database_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "phaze",  # the dev database
        "phaze_prod",
        "",  # a DSN carrying no database at all
        "testing",  # `test` must be a whole segment...
        "latest",  # ...so a substring must not match
        "contest",
        "protest_db",
    ],
)
def test_rejects_names_without_a_test_segment(name: str) -> None:
    assert not is_test_database_name(name)


# --- the failure mode: ERROR, never skip ---------------------------------------------------


def test_non_test_database_raises_rather_than_skipping() -> None:
    """The central contract of phaze-laqf.

    ``pytest.skip`` raises ``Skipped``, which pytest reports as a non-failure and which is
    indistinguishable from a pass in a summary line. Asserting the raised type is
    ``NonTestDatabaseError`` -- and explicitly NOT ``Skipped`` -- is what stops anyone
    reintroducing the silent variant.
    """
    dsn = "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze"

    with pytest.raises(NonTestDatabaseError) as excinfo:
        require_test_database(dsn, context="TRUNCATE")

    assert not isinstance(excinfo.value, type(pytest.skip.Exception))  # type: ignore[attr-defined]
    message = str(excinfo.value)
    assert "phaze" in message
    assert "TRUNCATE" in message
    # The message must tell the operator how to fix it, and why it is loud.
    assert "TEST_DATABASE_URL" in message
    assert "ERROR rather than a skip" in message


def test_guard_failure_is_not_swallowed_by_a_skip_aware_runner() -> None:
    """A ``NonTestDatabaseError`` must propagate through code that only tolerates skips."""
    with pytest.raises(NonTestDatabaseError):
        try:
            require_test_database("postgresql+asyncpg://u:p@h:5432/phaze_prod", context="integration tests")
        except pytest.skip.Exception:  # pragma: no cover -- must never be taken
            pytest.fail("the guard raised Skipped; a misconfigured database must fail the run")


def test_valid_test_database_returns_its_name() -> None:
    name = require_test_database("postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test_m7ya", context="integration tests")
    assert name == "phaze_test_m7ya"


# --- the default target: 5433, never the dev port ------------------------------------------


def test_default_dsn_targets_the_test_port_not_the_dev_port() -> None:
    """phaze-osgt: bare ``uv run pytest`` must never resolve to 5432.

    5432 is where the justfile says the DEVELOPER's database lives. These fixtures create and
    drop schema, so resolving there is a data-loss shape, not merely a confusing error.
    """
    assert ":5433/" in DEFAULT_TEST_DATABASE_URL
    assert ":5432/" not in DEFAULT_TEST_DATABASE_URL


def test_default_dsn_is_itself_accepted_by_the_guard() -> None:
    """The default must satisfy the guard, or bare pytest would hard-error on a fresh clone."""
    assert require_test_database(DEFAULT_TEST_DATABASE_URL, context="integration tests") == "phaze_test"


def test_resolve_prefers_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5433/phaze_laqf_test")
    # Also proves resolution coerces the libpq form to asyncpg.
    assert resolve_test_dsn() == "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_laqf_test"


def test_resolve_falls_back_to_the_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    assert resolve_test_dsn() == DEFAULT_TEST_DATABASE_URL


def test_empty_env_var_falls_back_rather_than_yielding_an_empty_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """``TEST_DATABASE_URL=`` (exported but blank) must not resolve to a nameless DSN."""
    monkeypatch.setenv("TEST_DATABASE_URL", "")
    assert resolve_test_dsn() == DEFAULT_TEST_DATABASE_URL


# --- the shared DSN pair -------------------------------------------------------------------


def test_integration_dsns_describe_the_same_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_laqf_test")
    monkeypatch.delenv("PHAZE_QUEUE_URL", raising=False)

    broker, sa = integration_dsns()

    assert broker.startswith("postgresql://")  # libpq form for psycopg3
    assert sa.startswith("postgresql+asyncpg://")  # dialect form for SQLAlchemy
    assert database_name(broker) == database_name(sa) == "phaze_laqf_test"


def test_integration_dsns_default_is_a_valid_test_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old copy-pasted fallback named database ``phaze`` -- not a test name at all."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("PHAZE_QUEUE_URL", raising=False)

    broker, sa = integration_dsns()

    assert database_name(broker) == database_name(sa) == "phaze_test"
    assert ":5433/" in broker and ":5433/" in sa
    require_test_database(sa, context="integration tests")  # must not raise


def test_broker_dsn_honours_an_explicit_queue_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@localhost:5433/phaze_broker_test")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test")

    broker, sa = integration_dsns()

    assert database_name(broker) == "phaze_broker_test"
    assert database_name(sa) == "phaze_test"


# --- DSN coercion --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h:5433/d", "postgresql+asyncpg://u:p@h:5433/d"),
        ("postgresql+psycopg2://u:p@h:5433/d", "postgresql+asyncpg://u:p@h:5433/d"),
        ("postgresql+psycopg://u:p@h:5433/d", "postgresql+asyncpg://u:p@h:5433/d"),
        ("postgresql+asyncpg://u:p@h:5433/d", "postgresql+asyncpg://u:p@h:5433/d"),
    ],
)
def test_coerce_async_dsn_normalizes_sync_drivers(given: str, expected: str) -> None:
    assert coerce_async_dsn(given) == expected


def test_database_name_of_a_dsn_without_one_is_empty() -> None:
    assert database_name("postgresql+asyncpg://u:p@h:5433/") == ""
