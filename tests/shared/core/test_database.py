"""Unit tests for phaze.database.run_migrations (Phase 27 UAT Gap 2).

Background
----------
On a fresh docker compose stack the api lifespan never ran ``alembic upgrade
head`` -- it only executed ``SELECT 1``. The watcher then failed because the
``agents`` table did not exist. Gap 2's fix wires ``run_migrations`` into the
lifespan startup, gated by ``settings.auto_migrate``.

These tests verify the runner is idempotent and respects the config gate
without hitting a real database -- the integration-level "fresh DB ->
migrations run" assertion lives in ``test_main_lifespan.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import phaze.database as db


@pytest.mark.asyncio
async def test_run_migrations_invokes_alembic_upgrade_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_migrations`` must call alembic's upgrade(...'head') exactly once."""
    monkeypatch.setattr(db.settings, "auto_migrate", True)

    upgrade_calls: list[tuple[object, str]] = []

    def _fake_upgrade(cfg: object, revision: str) -> None:
        upgrade_calls.append((cfg, revision))

    monkeypatch.setattr(db.command, "upgrade", _fake_upgrade)

    await db.run_migrations()

    assert len(upgrade_calls) == 1, f"expected 1 upgrade call, got {len(upgrade_calls)}"
    _cfg, revision = upgrade_calls[0]
    assert revision == "head", f"expected revision='head', got {revision!r}"


@pytest.mark.asyncio
async def test_run_migrations_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ``run_migrations`` twice succeeds (alembic upgrade head is a no-op when at head).

    The "actually at head" no-op behaviour is alembic's contract; what we verify
    here is that calling our wrapper twice does not raise and produces two
    independent upgrade invocations.
    """
    monkeypatch.setattr(db.settings, "auto_migrate", True)

    upgrade_calls: list[tuple[object, str]] = []

    def _fake_upgrade(cfg: object, revision: str) -> None:
        upgrade_calls.append((cfg, revision))

    monkeypatch.setattr(db.command, "upgrade", _fake_upgrade)

    await db.run_migrations()
    await db.run_migrations()

    assert len(upgrade_calls) == 2, "second call should also dispatch to alembic"


@pytest.mark.asyncio
async def test_run_migrations_escapes_percent_in_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A percent-encoded credential in ``settings.database_url`` must not crash ``run_migrations`` (phaze-7oya).

    ``_run_upgrade_head_sync`` builds a ConfigParser-backed Alembic ``Config`` and stores
    ``settings.database_url`` into it via ``set_main_option`` BEFORE calling
    ``command.upgrade`` (mocked here so no real DB/env.py is touched) -- so this exercises the
    escaping at the ``database.py`` call site directly, independent of alembic/env.py's own
    (separately tested) escaping. A real Postgres password with a URL-reserved character (e.g.
    ``p@ss`` -> ``p%40ss``) produces exactly this literal ``%`` shape. Pre-fix, ``set_main_option``
    itself raises ``ValueError('invalid interpolation syntax')`` before ``command.upgrade`` is
    ever reached.
    """
    monkeypatch.setattr(db.settings, "auto_migrate", True)
    monkeypatch.setattr(db.settings, "database_url", "postgresql+asyncpg://phaze:s3cret%40home@db:5432/phaze")

    upgrade_calls: list[tuple[object, str]] = []

    def _fake_upgrade(cfg: object, revision: str) -> None:
        upgrade_calls.append((cfg, revision))

    monkeypatch.setattr(db.command, "upgrade", _fake_upgrade)

    await db.run_migrations()  # must not raise

    assert len(upgrade_calls) == 1
    cfg, revision = upgrade_calls[0]
    assert revision == "head"
    assert cfg.get_main_option("sqlalchemy.url") == "postgresql+asyncpg://phaze:s3cret%40home@db:5432/phaze", (
        "escaped round-trip must deinterpolate back to the original literal URL"
    )


@pytest.mark.asyncio
async def test_run_migrations_skips_when_auto_migrate_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """``settings.auto_migrate=false`` must short-circuit before invoking alembic."""
    monkeypatch.setattr(db.settings, "auto_migrate", False)

    fake_upgrade = MagicMock()
    monkeypatch.setattr(db.command, "upgrade", fake_upgrade)

    await db.run_migrations()

    fake_upgrade.assert_not_called()


def test_engine_pool_hygiene_sourced_from_config() -> None:
    """quick-260707-ryn: the module-level api engine builds its pool from the config knobs.

    Leans phaze's PgBouncer session-mode server-connection footprint (the ~55-cap deadlock):
    reduced pool_size/max_overflow + pre_ping (drop dead server conns) + recycle (free idle
    server slots) + a bounded acquire timeout (fail fast rather than hang /health). The live
    AsyncAdaptedQueuePool exposes size()/_max_overflow/_timeout/_recycle/_pre_ping; assert on
    them directly (no module reload -- create_async_engine is lazy, so this opens no socket).
    """
    pool = db.engine.pool
    assert pool.size() == 5
    assert pool._max_overflow == 5
    assert pool._timeout == 10
    assert pool._recycle == 1800
    assert pool._pre_ping is True
