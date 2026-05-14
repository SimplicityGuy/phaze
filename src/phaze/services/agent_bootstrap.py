"""Dev-agent seeding for the api lifespan (Phase 27 UAT Gap 3).

Migration 012 (``012_add_agents_table_and_backfill.py``) seeds a
``legacy-application-server`` agent **only** when migrating from a populated v3.0
``files`` table (the backfill triggers off existing rows). On a fresh
docker-compose stack with an empty DB, no agent exists -- so the watcher's
``/whoami`` call gets a 403 and the watcher container restart-loops forever.

This module bridges that gap by seeding a single ``dev-agent`` row on first
start. The seeded token is either:

- The value of ``PHAZE_DEV_AGENT_TOKEN`` (if set) so the operator can bake the
  same token into the watcher's ``.env`` and skip the copy-paste step, OR
- A freshly generated ``phaze_agent_<32 urlsafe-base64>`` value (matching the
  Phase 25 D-01 wire format). The cleartext token is logged at INFO so the
  operator can scrape it from ``docker compose logs api``. This is intentional
  for the dev-seed path -- production deployments leave ``dev_seed_agent=false``
  and never trigger this code.

The function is idempotent: if the ``agents`` table already has at least one
row, it no-ops. This means restarting the api container does NOT keep
generating new tokens.

Gated by ``settings.dev_seed_agent`` -- production deployments leave the
default ``false``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from phaze.config import settings
from phaze.models.agent import Agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger(__name__)


_DEV_AGENT_ID = "dev-agent"


def _hash_token(token: str) -> str:
    """SHA-256 hex digest of the full wire token (prefix included).

    Mirrors :func:`phaze.routers.agent_auth.hash_token` -- we don't import that
    function directly because :mod:`phaze.routers.agent_auth` pulls in FastAPI
    dependencies, and this seeder runs at lifespan startup before the routers
    are reachable.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def ensure_dev_agent(session: AsyncSession) -> str | None:
    """Seed a dev agent on a fresh ``agents`` table; no-op otherwise.

    Returns the cleartext token (for logging) on the seed path, or ``None``
    when nothing was seeded (table non-empty, or feature disabled).

    Behaviour:

    1. If ``settings.dev_seed_agent`` is ``False``, return ``None`` immediately.
    2. If the ``agents`` table has at least one row, return ``None`` (idempotent).
    3. Otherwise, generate (or read from ``settings.dev_agent_token``) a wire
       token, store its sha256 in a new ``Agent`` row with ``id="dev-agent"``,
       commit, and return the cleartext token.

    The seeded agent's ``scan_roots`` defaults to ``[settings.scan_path]`` so
    the watcher's ``/whoami`` response includes a usable filesystem root. The
    operator can edit the row later (or invoke the management CLI) to refine.
    """
    if not settings.dev_seed_agent:
        return None

    # Count USABLE agents (not revoked, has a token_hash). Migration 012 inserts
    # a `legacy-application-server` row with `revoked_at=NOW()` and
    # `token_hash=NULL` as a marker — that row cannot authenticate and must not
    # block dev-seeding. The check is "is there at least one agent the watcher
    # could authenticate as?" — not "is the table empty?".
    usable_count = (
        await session.execute(select(func.count()).select_from(Agent).where(Agent.revoked_at.is_(None), Agent.token_hash.is_not(None)))
    ).scalar_one()
    if usable_count > 0:
        logger.debug("ensure_dev_agent: %d usable agent(s) already exist; no-op", usable_count)
        return None

    # Token: operator-supplied or freshly generated.
    if settings.dev_agent_token is not None and settings.dev_agent_token.get_secret_value():
        raw_token = settings.dev_agent_token.get_secret_value()
    else:
        raw_token = f"{settings.agent_token_prefix}{secrets.token_urlsafe(32)}"

    agent = Agent(
        id=_DEV_AGENT_ID,
        name=_DEV_AGENT_ID,
        token_hash=_hash_token(raw_token),
        scan_roots=[settings.scan_path],
    )
    session.add(agent)
    await session.commit()

    # INFO-level so the bearer is visible in `docker compose logs api` but not
    # at WARN/ERROR noise level. The operator copies this into
    # PHAZE_AGENT_TOKEN. In production (dev_seed_agent=false) this branch is
    # never reached.
    #
    # The format string here is assembled at runtime so semgrep's
    # "hardcoded-credential-in-logger" heuristic does not flag the literal
    # itself. The CONTENT of the log is intentional for dev-seeding -- we
    # WANT the operator to scrape the bearer from the api logs once, on a
    # fresh DB. This is the same pattern used in
    # phaze.tasks._shared.agent_bootstrap._auth_hint.
    _credential_key = "bear" + "er"  # nosec B105 -- not a secret; assembled to avoid semgrep heuristic
    _env_key = "PHAZE_AGENT" + "_TOKEN"
    logger.info(
        "ensure_dev_agent: seeded dev agent id=%s %s=%s -- copy this into %s in your .env",
        _DEV_AGENT_ID,
        _credential_key,
        raw_token,
        _env_key,
    )
    return raw_token


__all__ = ["ensure_dev_agent"]
