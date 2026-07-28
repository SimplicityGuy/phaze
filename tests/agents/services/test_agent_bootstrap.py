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
    # The shared `async_engine` fixture seeds the `test-fileserver` agent; remove
    # it so the "fresh DB" (empty agents table) precondition holds.
    seeded = await session.get(Agent, "test-fileserver")
    if seeded is not None:
        await session.delete(seeded)
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
    seeded = await session.get(Agent, "test-fileserver")
    if seeded is not None:
        await session.delete(seeded)
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
    seeded = await session.get(Agent, "test-fileserver")
    if seeded is not None:
        await session.delete(seeded)
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
async def test_ensure_dev_agent_revoked_same_id_row_stays_revoked(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-gh22: a REVOKED same-id ``dev-agent`` row must stay revoked across a restart.

    Pre-phaze-gh22, ``usable_count == 0`` fell through to the upsert, which
    un-revoked the row and reinstalled a token unconditionally -- so revoking a
    leaked bearer via the documented kill switch (``routers/agent_auth.py``) was
    silently undone by the very next ``ensure_dev_agent`` call (i.e. the next api
    restart). The fix must treat an explicit revoke of this row as terminal: no
    token is (re)installed, ``revoked_at`` is left untouched, and the call must
    still not raise (phaze-viwd's original crash-loop fix must hold).
    """
    from datetime import UTC, datetime

    seeded = await session.get(Agent, "test-fileserver")
    if seeded is not None:
        await session.delete(seeded)
        await session.commit()

    stale_hash = hashlib.sha256(b"stale-revoked-token").hexdigest()
    revoked_at = datetime.now(UTC)
    session.add(
        Agent(
            id="dev-agent",
            name="dev-agent",
            token_hash=stale_hash,
            revoked_at=revoked_at,
            scan_roots=["/data/music"],
        )
    )
    await session.commit()

    monkeypatch.setattr(settings, "dev_seed_agent", True)
    monkeypatch.setattr(settings, "dev_agent_token", None)

    raw_token = await ensure_dev_agent(session)  # must not raise IntegrityError

    assert raw_token is None, "a revoked dev-agent row must not be silently re-seeded"

    # Exactly one dev-agent row -- untouched, not duplicated.
    count = (await session.execute(select(func.count()).select_from(Agent).where(Agent.id == "dev-agent"))).scalar_one()
    assert count == 1, f"expected the row to be left alone, got {count} rows"

    dev = await session.get(Agent, "dev-agent")
    assert dev is not None
    assert dev.revoked_at is not None, "the revoke must stick across the bootstrap call"
    assert dev.token_hash == stale_hash, "the stale token hash must not be replaced"


@pytest.mark.asyncio
async def test_ensure_dev_agent_refuses_to_reinstall_pinned_token_on_revoked_row(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-gh22 regression: a pinned ``PHAZE_DEV_AGENT_TOKEN`` must not resurrect a revoked row.

    This is the exact failure scenario from the bead: ``PHAZE_DEV_SEED_AGENT=true``
    with a fixed ``PHAZE_DEV_AGENT_TOKEN`` baked into the watcher's ``.env``, an
    operator revokes the leaked bearer (``UPDATE agents SET revoked_at = NOW() WHERE
    id='dev-agent'``), then the api container restarts. Pre-fix, the upsert wrote
    back the SAME sha256 of the SAME cleartext token and cleared ``revoked_at``,
    silently reinstating the leaked credential. Post-fix, the revoke must stick.
    """
    from datetime import UTC, datetime

    seeded = await session.get(Agent, "test-fileserver")
    if seeded is not None:
        await session.delete(seeded)
        await session.commit()

    pinned_token = "phaze_agent_leaked-and-revoked-token"
    pinned_hash = hashlib.sha256(pinned_token.encode("utf-8")).hexdigest()
    session.add(
        Agent(
            id="dev-agent",
            name="dev-agent",
            token_hash=pinned_hash,
            revoked_at=datetime.now(UTC),
            scan_roots=["/data/music"],
        )
    )
    await session.commit()

    monkeypatch.setattr(settings, "dev_seed_agent", True)
    monkeypatch.setattr(settings, "dev_agent_token", SecretStr(pinned_token))

    raw_token = await ensure_dev_agent(session)

    assert raw_token is None, "the pinned leaked token must not be reinstalled after a revoke"

    dev = await session.get(Agent, "dev-agent")
    assert dev is not None
    assert dev.revoked_at is not None, "revocation must survive a bootstrap call even with a pinned token"
    assert dev.token_hash == pinned_hash, "the token hash must remain exactly what it was -- untouched"


@pytest.mark.asyncio
async def test_ensure_dev_agent_skips_duplicate_live_sentinel(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-viwd: a pre-existing LIVE sentinel ``ScanBatch`` must not crash-loop startup.

    A same-id ``dev-agent`` row with ``token_hash`` stripped to ``NULL`` (but NOT
    revoked -- see ``test_ensure_dev_agent_revoked_same_id_row_stays_revoked`` for
    the now-terminal revoked case) still falls through to re-seeding. Its
    ``scan_batches`` rows are untouched by that token-stripping, so a LIVE sentinel
    batch can still be present. Pre-fix, the unconditional second
    ``INSERT ... status='live'`` violated the partial unique index
    ``uq_scan_batches_agent_id_live`` and raised ``IntegrityError`` at commit --
    independently of the Agent PK collision, so a fix that only handles the Agent
    side still crash-loops here.
    """
    seeded = await session.get(Agent, "test-fileserver")
    if seeded is not None:
        await session.delete(seeded)
        await session.commit()

    session.add(
        Agent(
            id="dev-agent",
            name="dev-agent",
            token_hash=None,
            revoked_at=None,
            scan_roots=["/data/music"],
        )
    )
    await session.commit()

    existing_batch = ScanBatch(
        agent_id="dev-agent",
        scan_path="<watcher>",
        status="live",
        total_files=0,
        processed_files=0,
    )
    session.add(existing_batch)
    await session.commit()
    existing_batch_id = existing_batch.id

    monkeypatch.setattr(settings, "dev_seed_agent", True)
    monkeypatch.setattr(settings, "dev_agent_token", None)

    raw_token = await ensure_dev_agent(session)  # must not raise IntegrityError

    assert raw_token is not None

    live_batches = (await session.execute(select(ScanBatch).where(ScanBatch.agent_id == "dev-agent", ScanBatch.status == "live"))).scalars().all()
    assert len(live_batches) == 1, f"expected exactly one live sentinel batch, got {len(live_batches)}"
    assert live_batches[0].id == existing_batch_id, "the pre-existing live sentinel must be left in place, not duplicated"

    dev = await session.get(Agent, "dev-agent")
    assert dev is not None and dev.revoked_at is None and dev.token_hash is not None


@pytest.mark.asyncio
async def test_ensure_dev_agent_reseeds_past_null_token_hash_same_id_row(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-viwd: a non-revoked ``dev-agent`` row with ``token_hash=NULL`` must not crash-loop.

    The idempotency guard's "usable" predicate is ``revoked_at IS NULL AND token_hash
    IS NOT NULL`` -- so a same-id row with ``token_hash`` stripped to ``NULL`` (but
    NOT revoked) is just as invisible to the guard as a revoked one, and hits the
    exact same PK collision on a blind insert.
    """
    seeded = await session.get(Agent, "test-fileserver")
    if seeded is not None:
        await session.delete(seeded)
        await session.commit()

    session.add(
        Agent(
            id="dev-agent",
            name="dev-agent",
            token_hash=None,
            revoked_at=None,
            scan_roots=["/data/music"],
        )
    )
    await session.commit()

    monkeypatch.setattr(settings, "dev_seed_agent", True)
    monkeypatch.setattr(settings, "dev_agent_token", None)

    raw_token = await ensure_dev_agent(session)  # must not raise IntegrityError

    assert raw_token is not None

    count = (await session.execute(select(func.count()).select_from(Agent).where(Agent.id == "dev-agent"))).scalar_one()
    assert count == 1, f"expected the row to be upserted in place, got {count} rows"

    dev = await session.get(Agent, "dev-agent")
    assert dev is not None
    assert dev.revoked_at is None
    assert dev.token_hash is not None
    assert dev.token_hash == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_ensure_dev_agent_uses_env_token_when_set(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PHAZE_DEV_AGENT_TOKEN`` overrides the random token (operator can pin it in .env)."""
    seeded = await session.get(Agent, "test-fileserver")
    if seeded is not None:
        await session.delete(seeded)
        await session.commit()

    fixed_token = "phaze_agent_test-fixed-token-12345"
    monkeypatch.setattr(settings, "dev_seed_agent", True)
    monkeypatch.setattr(settings, "dev_agent_token", SecretStr(fixed_token))

    raw_token = await ensure_dev_agent(session)

    assert raw_token == fixed_token, f"expected fixed token, got {raw_token!r}"

    seeded = (await session.execute(select(Agent))).scalar_one()
    assert seeded.token_hash == hashlib.sha256(fixed_token.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_ensure_dev_agent_uses_phaze_agent_scan_roots_env_when_set(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 27 UAT Gap 10: PHAZE_AGENT_SCAN_ROOTS env overrides settings.scan_path.

    In docker-compose mode SCAN_PATH is the host path (bind-mount source),
    while PHAZE_AGENT_SCAN_ROOTS is the in-container path the watcher walks.
    Without this override the dev-seeder writes the host path to the agent's
    scan_roots column and the watcher then crashes with FileNotFoundError
    when its watchdog Observer tries to schedule the non-existent path.
    """
    seeded = await session.get(Agent, "test-fileserver")
    if seeded is not None:
        await session.delete(seeded)
        await session.commit()

    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/data/music,/data/concerts")
    monkeypatch.setattr(settings, "dev_seed_agent", True)
    monkeypatch.setattr(settings, "dev_agent_token", None)
    monkeypatch.setattr(settings, "scan_path", "/wrong/host/path")  # must be ignored

    raw_token = await ensure_dev_agent(session)

    assert raw_token is not None
    seeded = await session.get(Agent, "dev-agent")
    assert seeded is not None
    assert seeded.scan_roots == ["/data/music", "/data/concerts"], (
        f"agent scan_roots should come from PHAZE_AGENT_SCAN_ROOTS, got {seeded.scan_roots!r}"
    )


@pytest.mark.asyncio
async def test_ensure_dev_agent_disabled_in_prod(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``settings.dev_seed_agent=false`` short-circuits before any DB read."""
    seeded = await session.get(Agent, "test-fileserver")
    if seeded is not None:
        await session.delete(seeded)
        await session.commit()

    monkeypatch.setattr(settings, "dev_seed_agent", False)

    result = await ensure_dev_agent(session)

    assert result is None
    count = (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
    assert count == 0, f"no row should be seeded when feature is disabled; got {count}"
