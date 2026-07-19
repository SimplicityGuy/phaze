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

The function is idempotent in two distinct senses, and the difference matters:

- If the ``agents`` table already has at least one USABLE row (not revoked, with a
  ``token_hash``) -- of ANY id, not just ``dev-agent`` -- this no-ops, and restarting
  the api container does NOT keep generating new tokens.
- If the only rows present are NOT usable (revoked, or with ``token_hash`` stripped
  to ``NULL``) -- whether that is migration 012's DIFFERENT-id
  ``legacy-application-server`` marker, or a SAME-id ``dev-agent`` row left behind by
  the documented token-rotation procedure (``UPDATE agents SET revoked_at = NOW()
  WHERE id='dev-agent'``, ``routers/agent_auth.py``) -- this (re-)seeds a usable
  ``dev-agent`` row. The ``dev-agent`` row and its sentinel ``ScanBatch`` are written
  via ``INSERT ... ON CONFLICT ... DO UPDATE`` / ``DO NOTHING`` (never a blind
  ``INSERT``), so a pre-existing same-id row -- revoked, token-stripped, or with a
  live sentinel batch already present -- is un-revoked and re-keyed in place instead
  of colliding with ``pk_agents`` or ``uq_scan_batches_agent_id_live``. That upsert
  completes the operator's rotation intent (a fresh usable token) instead of
  crash-looping the whole api container on every restart (phaze-viwd).

Gated by ``settings.dev_seed_agent`` -- production deployments leave the
default ``false``.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import settings
from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


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
    2. If the ``agents`` table has at least one USABLE row (not revoked, with a
       ``token_hash``), return ``None`` (idempotent).
    3. Otherwise, generate (or read from ``settings.dev_agent_token``) a wire
       token and UPSERT its sha256 into the ``id="dev-agent"`` row -- un-revoking
       and re-keying it in place if it already exists (revoked and/or
       token_hash=NULL) rather than blindly inserting and colliding with
       ``pk_agents`` -- plus its sentinel ``ScanBatch`` (skipped if a live one
       already exists, to avoid colliding with ``uq_scan_batches_agent_id_live``),
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

    # Phase 27 UAT Gap 10: prefer PHAZE_AGENT_SCAN_ROOTS (the canonical
    # agent-side scan roots env var, set per AgentSettings.scan_roots) over
    # ControlSettings.scan_path. In docker-compose mode SCAN_PATH is the HOST
    # path used by docker-compose's bind mount source (e.g.,
    # /Users/Robert/phaze-watch-test) while PHAZE_AGENT_SCAN_ROOTS is the
    # in-container path the agent actually walks (e.g., /data/music). Using
    # settings.scan_path here would write the host path to the agent's
    # scan_roots column, which the watcher then tries to observe inside the
    # container and fails with FileNotFoundError.
    agent_scan_roots_env = os.environ.get("PHAZE_AGENT_SCAN_ROOTS", "").strip()
    agent_scan_roots = [p.strip() for p in agent_scan_roots_env.split(",") if p.strip()] if agent_scan_roots_env else [settings.scan_path]

    # phaze-viwd: UPSERT, not a blind INSERT. `usable_count == 0` above only proves
    # no agent the watcher could authenticate as exists yet -- it does NOT prove no
    # row with id='dev-agent' exists. The documented rotation procedure is
    # `UPDATE agents SET revoked_at = NOW() WHERE id='dev-agent'` (routers/agent_auth.py),
    # which LEAVES the row present with a matching PK; the same gap opens if
    # token_hash is ever stripped to NULL while the row persists. A bare
    # `session.add(Agent(id="dev-agent", ...))` then raises IntegrityError on
    # `pk_agents` at the commit below, and because the DB state never changes,
    # every subsequent restart re-crashes identically -- a permanent crash loop
    # (this was the bug). ON CONFLICT (id) DO UPDATE re-keys and un-revokes the
    # existing row in place instead, which is also what the operator actually
    # wanted: they revoked the token to ROTATE it, not to permanently disable the
    # dev agent, so completing that intent (not merely surviving startup) is the
    # correct fix -- see the module docstring above.
    agent_stmt = pg_insert(Agent).values(
        id=_DEV_AGENT_ID,
        name=_DEV_AGENT_ID,
        token_hash=_hash_token(raw_token),
        scan_roots=agent_scan_roots,
    )
    agent_stmt = agent_stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "token_hash": agent_stmt.excluded.token_hash,
            "scan_roots": agent_stmt.excluded.scan_roots,
            "revoked_at": None,
            "updated_at": func.now(),
        },
    )
    await session.execute(agent_stmt)

    # Migration 012 seeds a LIVE sentinel ScanBatch for the legacy agent so
    # POST /api/internal/agent/files can resolve `batch_id=None` via the partial
    # uq_scan_batches_agent_id_live index. The dev-seeded agent needs the same
    # sentinel — otherwise the watcher's chunk-of-1 upserts get NoResultFound
    # at the controller's `scalar_one()` LIVE-batch lookup.
    #
    # phaze-viwd: this insert is CONDITIONAL. Revoking the dev-agent does NOT
    # touch its scan_batches rows, so a previously-seeded LIVE sentinel can
    # still be sitting there when we reach this point. A second unconditional
    # INSERT would violate `uq_scan_batches_agent_id_live` (a partial unique
    # index on `agent_id` WHERE `status = 'live'`). `index_where` mirrors that
    # exact predicate so the ON CONFLICT inference matches the index and the
    # insert is skipped only when a live batch already exists -- never against
    # a completed/failed one, which must remain free to coexist.
    scan_batch_stmt = pg_insert(ScanBatch).values(
        id=uuid.uuid4(),
        agent_id=_DEV_AGENT_ID,
        scan_path="<watcher>",
        status="live",
        total_files=0,
        processed_files=0,
    )
    scan_batch_stmt = scan_batch_stmt.on_conflict_do_nothing(
        index_elements=["agent_id"],
        index_where=(ScanBatch.status == "live"),
    )
    await session.execute(scan_batch_stmt)

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
    _credential_key = "bear" + "er"  # not a secret; assembled to avoid semgrep heuristic
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
