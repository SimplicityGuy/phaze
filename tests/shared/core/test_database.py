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
async def test_run_migrations_skips_when_auto_migrate_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """``settings.auto_migrate=false`` must short-circuit before invoking alembic."""
    monkeypatch.setattr(db.settings, "auto_migrate", False)

    fake_upgrade = MagicMock()
    monkeypatch.setattr(db.command, "upgrade", fake_upgrade)

    await db.run_migrations()

    fake_upgrade.assert_not_called()
