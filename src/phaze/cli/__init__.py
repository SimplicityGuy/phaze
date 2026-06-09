"""`phaze` management CLI (stdlib argparse, no third-party dependency).

Currently exposes a single command group:

    phaze agents add --id <id> --name <name> --scan-roots /a,/b

`agents add` mints a per-agent bearer token, inserts an `agents` row, and prints
the cleartext token exactly once (it is NOT recoverable afterwards -- only the
sha256 hash is persisted) alongside the derived `phaze-agent-<id>` queue name.

Design notes:
  - The token wire format and hashing are reused verbatim from the HTTP auth
    layer (`phaze.routers.agent_auth.hash_token`); do NOT reimplement sha256.
  - `AGENT_ID_RE` mirrors the `agents.id_charset` CheckConstraint exactly. Ids
    are validated BEFORE any DB access so an invalid id never opens a session.
  - The minted token is the only secret this module handles and is emitted via
    `print()` only -- it is NEVER passed to a logger.
  - Subparsers are used so future `agents` subcommands (list/revoke) slot in
    without restructuring the entry point.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import re
import secrets
import sys
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from phaze.database import async_session
from phaze.logging_config import configure_logging
from phaze.models.agent import Agent
from phaze.routers.agent_auth import hash_token


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


AGENT_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
"""Same charset the `agents.id_charset` CheckConstraint enforces. Do NOT weaken."""

TOKEN_PREFIX = "phaze_agent_"  # noqa: S105  # nosec B105 — public wire prefix, not a secret
"""Wire-token prefix (phase-25 D-01). Hashed prefix-included by `hash_token`."""


def validate_agent_id(agent_id: str) -> None:
    """Raise ``ValueError`` unless ``agent_id`` fully matches :data:`AGENT_ID_RE`."""
    if not AGENT_ID_RE.fullmatch(agent_id):
        msg = (
            f"invalid agent id {agent_id!r}: must match {AGENT_ID_RE.pattern} "
            "(lowercase letters/digits, single hyphens between segments, no "
            "leading/trailing hyphen)"
        )
        raise ValueError(msg)


def validate_scan_roots(scan_roots: list[str]) -> None:
    """Raise ``ValueError`` if any entry is empty or not an absolute path."""
    for root in scan_roots:
        if not root or not Path(root).is_absolute():
            msg = f"invalid scan root {root!r}: every scan root must be an absolute path"
            raise ValueError(msg)


def derive_queue_name(agent_id: str) -> str:
    """Return the SAQ queue name an agent listens on (mirrors agent_worker.py)."""
    return f"phaze-agent-{agent_id}"


async def add_agent(session: AsyncSession, agent_id: str, name: str, scan_roots: list[str]) -> str:
    """Insert an :class:`Agent` row and return the cleartext bearer token.

    The token is minted with :func:`secrets.token_urlsafe` (CSPRNG) and only its
    sha256 hash (via :func:`hash_token`) is persisted. Callers MUST surface the
    returned cleartext to the operator exactly once -- it cannot be recovered.

    Does NOT catch :class:`~sqlalchemy.exc.IntegrityError` (e.g. duplicate id);
    that is left to propagate so the caller can map it to a friendly message.
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    agent = Agent(id=agent_id, name=name, token_hash=hash_token(token), scan_roots=scan_roots)
    session.add(agent)
    await session.commit()
    return token


async def _run_add(agent_id: str, name: str, scan_roots: list[str]) -> str:
    """Open a session and delegate to :func:`add_agent`; return the cleartext token."""
    async with async_session() as session:
        return await add_agent(session, agent_id, name, scan_roots)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser with an ``agents add`` subcommand."""
    parser = argparse.ArgumentParser(prog="phaze", description="Phaze management CLI.")
    subcommands = parser.add_subparsers(dest="group", required=True)

    agents = subcommands.add_parser("agents", help="Manage agents (file-server identities).")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)

    add = agents_sub.add_parser("add", help="Register an agent and mint a bearer token.")
    add.add_argument("--id", dest="agent_id", required=True, help="Agent id (kebab-case: ^[a-z0-9]+(-[a-z0-9]+)*$).")
    add.add_argument("--name", dest="name", default=None, help="Human-readable name (defaults to the titleized id).")
    add.add_argument(
        "--scan-roots",
        dest="scan_roots",
        required=True,
        help="Comma-separated absolute paths the agent may read/write (e.g. /data/music,/data/concerts).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (0 success, 1 failure)."""
    # PR3 observability: configure the central structlog pipeline first so any
    # library/DB log lines emitted during agent creation render consistently. The
    # minted token stays print()-only and is NEVER passed to a logger (D-13).
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)

    agent_id: str = args.agent_id
    name: str = args.name if args.name is not None else agent_id.replace("-", " ").title()
    scan_roots: list[str] = [part.strip() for part in args.scan_roots.split(",") if part.strip()]

    # Validate BEFORE any DB access so an invalid id never opens a session.
    try:
        validate_agent_id(agent_id)
        validate_scan_roots(scan_roots)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        token = asyncio.run(_run_add(agent_id, name, scan_roots))
    except IntegrityError:
        print(
            f"error: agent id {agent_id!r} already exists (no row was created)",
            file=sys.stderr,
        )
        return 1

    queue_name = derive_queue_name(agent_id)
    print(f"Agent {agent_id!r} registered.")
    print("")
    print(f"  token: {token}")
    print("  ^^ SAVE THIS NOW -- it is NOT recoverable. Only its hash is stored.")
    print("")
    print(f"  queue: {queue_name}")
    print(f"  ^^ set PHAZE_AGENT_QUEUE={queue_name} in the agent's .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
