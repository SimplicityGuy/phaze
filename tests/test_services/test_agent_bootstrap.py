"""Unit tests for phaze.services.agent_bootstrap (Phase 27 UAT Gap 3).

Background
----------
Migration 012 seeds the ``legacy-application-server`` agent ONLY when migrating
from a populated v3.0 ``files`` table. On a fresh DB no agent exists, so the
watcher's ``/whoami`` call hits 403 and the watcher container restart-loops.

``ensure_dev_agent(session)`` seeds a single ``dev-agent`` row on the first
boot of a fresh DB. The behaviour is gated by ``settings.dev_seed_agent`` (so
production deployments never trigger it) and idempotent (re-runs no-op when
the table already has a row).

These tests use the ``session`` fixture (a real Postgres session via the
shared conftest), so they verify the behaviour end-to-end against a real
database -- including the SHA-256 token hash storage.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from pydantic import SecretStr
import pytest
from sqlalchemy import func, select

from phaze.config import settings
from phaze.models.agent import LEGACY_AGENT_ID, Agent
from phaze.models.scan_batch import ScanBatch
from phaze.services.agent_bootstrap import ensure_dev_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_ensure_dev_agent_seeds_when_table_empty(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty agents table + dev_seed_agent=true -> exactly one agent row created."""
    # The shared `async_engine` fixture seeds LEGACY_AGENT_ID; remove it so the
    # "fresh DB" precondition holds.
    legacy = await session.get(Agent, LEGACY_AGENT_ID)
    if legacy is not None:
        await session.delete(legacy)
        await session.commit()

    monkeypatch.setattr(settings, "dev_seed_agent", True)
    monkeypatch.setattr(settings, "dev_agent_token", None)

    raw_token = await ensure_dev_agent(session)

    assert raw_token is not None, "ensure_dev_agent should return the seeded token"
    assert raw_token.startswith(settings.agent_token_prefix), f"token prefix wrong: {raw_token!r}"

    count = (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
    assert count == 1, f"expected exactly one agent post-seed, got {count}"

    seeded = (await session.execute(select(Agent))).scalar_one()
    assert seeded.id == "dev-agent"
    # Token is stored as sha256 -- verify by recomputing.
    expected_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert seeded.token_hash == expected_hash, "stored hash does not match sha256(token)"

    # Phase 27 UAT Gap 9: a LIVE-sentinel ScanBatch must accompany the agent
    # so the controller's POST /files batch_id resolution finds a target row.
    # Without this, watcher chunk-of-1 upserts crash with NoResultFound.
    live = (await session.execute(select(ScanBatch).where(ScanBatch.agent_id == "dev-agent", ScanBatch.status == "live"))).scalar_one()
    assert live.scan_path == "<watcher>", "LIVE sentinel must use the canonical scan_path marker"


@pytest.mark.asyncio
async def test_ensure_dev_agent_noop_when_usable_agent_exists(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing USABLE agent (non-revoked, with token_hash) blocks seeding."""
    # Replace conftest's tokenless legacy seed with a usable agent under test
    legacy = await session.get(Agent, LEGACY_AGENT_ID)
    if legacy is not None:
        await session.delete(legacy)
        await session.commit()
    session.add(Agent(id="some-real-agent", name="some-real-agent", token_hash="fakehash" * 8, scan_roots=["/data/music"]))
    await session.commit()

    monkeypatch.setattr(settings, "dev_seed_agent", True)
    monkeypatch.setattr(settings, "dev_agent_token", None)

    before_count = (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
    result = await ensure_dev_agent(session)

    assert result is None, "ensure_dev_agent must return None when a usable agent already exists"
    after_count = (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
    assert after_count == before_count, f"unexpected new rows: before={before_count} after={after_count}"


@pytest.mark.asyncio
async def test_ensure_dev_agent_seeds_past_revoked_legacy_marker(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migration 012's revoked legacy-application-server row must NOT block dev seeding.

    Migration 012 inserts a `legacy-application-server` agent with `revoked_at=NOW()`
    and `token_hash=NULL` as a marker — that row cannot authenticate, so the
    dev-seeder must still seed a usable `dev-agent` row past it. A naive
    `count(*) > 0` check would no-op here and leave the watcher unable to authenticate.
    """
    from datetime import UTC, datetime

    # Replace the tokenless test fixture legacy with the production-shaped revoked legacy
    legacy = await session.get(Agent, LEGACY_AGENT_ID)
    if legacy is not None:
        await session.delete(legacy)
        await session.commit()
    session.add(
        Agent(
            id=LEGACY_AGENT_ID,
            name=LEGACY_AGENT_ID,
            token_hash=None,
            revoked_at=datetime.now(UTC),
            scan_roots=["/data/music"],
        )
    )
    await session.commit()

    monkeypatch.setattr(settings, "dev_seed_agent", True)
    monkeypatch.setattr(settings, "dev_agent_token", None)

    raw_token = await ensure_dev_agent(session)

    assert raw_token is not None, "must seed past a revoked/tokenless legacy marker"
    # Now table holds both: the revoked legacy marker AND the new dev-agent
    count = (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
    assert count == 2, f"expected revoked legacy + new dev-agent, got count={count}"
    dev = await session.get(Agent, "dev-agent")
    assert dev is not None and dev.token_hash is not None and dev.revoked_at is None


@pytest.mark.asyncio
async def test_ensure_dev_agent_uses_env_token_when_set(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PHAZE_DEV_AGENT_TOKEN`` overrides the random token (operator can pin it in .env)."""
    legacy = await session.get(Agent, LEGACY_AGENT_ID)
    if legacy is not None:
        await session.delete(legacy)
        await session.commit()

    fixed_token = "phaze_agent_test-fixed-token-12345"
    monkeypatch.setattr(settings, "dev_seed_agent", True)
    monkeypatch.setattr(settings, "dev_agent_token", SecretStr(fixed_token))

    raw_token = await ensure_dev_agent(session)

    assert raw_token == fixed_token, f"expected fixed token, got {raw_token!r}"

    seeded = (await session.execute(select(Agent))).scalar_one()
    assert seeded.token_hash == hashlib.sha256(fixed_token.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_ensure_dev_agent_disabled_in_prod(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``settings.dev_seed_agent=false`` short-circuits before any DB read."""
    legacy = await session.get(Agent, LEGACY_AGENT_ID)
    if legacy is not None:
        await session.delete(legacy)
        await session.commit()

    monkeypatch.setattr(settings, "dev_seed_agent", False)

    result = await ensure_dev_agent(session)

    assert result is None
    count = (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
    assert count == 0, f"no row should be seeded when feature is disabled; got {count}"
