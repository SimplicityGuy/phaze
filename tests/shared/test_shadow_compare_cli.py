"""Hermetic (DB-free) unit cells for the Phase 79 shadow-compare CLI (`phaze.cli.shadow_compare`).

The DB-backed CLI-exit contract (exit 1 on hard divergence / 0 on clean) is covered by the real-PG
cells in ``tests/integration/test_shadow_compare.py``; those always pass ``--database-url`` and so never
exercise the argparse-validation, DSN-redaction, or default-session branches. These cells cover exactly
those pure branches with no Postgres, no network, and a monkeypatched core -- keeping the module above
the 90% per-module coverage floor while staying in the DB-free ``shared`` bucket.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

import phaze.cli.shadow_compare as cli


def test_non_negative_int_accepts_zero_and_positive() -> None:
    assert cli._non_negative_int("0") == 0
    assert cli._non_negative_int("20") == 20


def test_non_negative_int_rejects_negative() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=">= 0"):
        cli._non_negative_int("-1")


def test_parse_dsn_or_exit_returns_masking_url_for_valid_dsn() -> None:
    url = cli._parse_dsn_or_exit("postgresql+asyncpg://user:secretpw@dbhost:5432/phaze_test")
    # A SQLAlchemy URL masks the password in str()/repr() -- so the parsed object is safe to surface.
    assert "secretpw" not in str(url)
    assert url.host == "dbhost"
    assert url.database == "phaze_test"


def test_parse_dsn_or_exit_redacts_on_unparseable_dsn() -> None:
    # An unparseable DSN must raise SystemExit WITHOUT echoing the original (password-bearing) string.
    with pytest.raises(SystemExit) as exc:
        cli._parse_dsn_or_exit("postgresql://user:secretpw@:not-an-int-port/db")
    assert "secretpw" not in str(exc.value)
    assert "invalid --database-url" in str(exc.value)


def test_safe_target_renders_host_db_only() -> None:
    url = make_url("postgresql+asyncpg://user:secretpw@dbhost:5432/phaze_test")
    rendered = cli._safe_target(url)
    assert rendered == "dbhost/phaze_test"
    assert "secretpw" not in rendered


def test_build_parser_defaults_and_flags() -> None:
    parser = cli._build_parser()
    args = parser.parse_args([])
    assert args.sample_cap == 20
    assert args.verbose is False
    assert args.database_url is None
    args2 = parser.parse_args(["--sample-cap", "5", "--verbose", "--database-url", "postgresql://h/d"])
    assert (args2.sample_cap, args2.verbose, args2.database_url) == (5, True, "postgresql://h/d")


def test_main_default_session_path_no_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main([])` (no --database-url) drives the None branch -> default `async_session`, DB-free."""

    class _FakeSession:
        pass

    class _FakeSessionCM:
        async def __aenter__(self) -> _FakeSession:
            return _FakeSession()

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    seen: dict[str, object] = {}

    def _fake_async_session() -> _FakeSessionCM:
        return _FakeSessionCM()

    async def _fake_run(session: object, *, sample_cap: int, verbose: bool) -> object:
        seen["sample_cap"] = sample_cap
        seen["verbose"] = verbose
        return SimpleNamespace(hard_fail_total=0, render=lambda **_kw: "OK")

    monkeypatch.setattr(cli, "async_session", _fake_async_session)
    monkeypatch.setattr(cli, "run_shadow_compare", _fake_run)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    assert cli.main([]) == 0
    assert seen == {"sample_cap": 20, "verbose": False}


def test_main_returns_one_on_hard_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main([])` returns 1 when the (monkeypatched) core reports hard_fail_total > 0 (D-05)."""

    class _FakeSessionCM:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    async def _fake_run(session: object, *, sample_cap: int, verbose: bool) -> object:
        return SimpleNamespace(hard_fail_total=3, render=lambda **_kw: "diverged")

    monkeypatch.setattr(cli, "async_session", lambda: _FakeSessionCM())
    monkeypatch.setattr(cli, "run_shadow_compare", _fake_run)
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    assert cli.main([]) == 1
