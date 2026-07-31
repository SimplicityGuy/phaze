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
    (and the effective name, explicit or derived) are validated -- including
    their column-width bounds (`String(64)`/`String(128)`) -- BEFORE any DB
    access so an invalid id/name never opens a session and never surfaces as
    a raw driver traceback.
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

from phaze.config import get_settings
from phaze.database import async_session
from phaze.logging_config import configure_logging
from phaze.models.agent import Agent
from phaze.routers.agent_auth import hash_token
from phaze.services.queue_introspection import ActiveJobBreakdown, summarize_active_jobs


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


AGENT_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
"""Same charset the `agents.id_charset` CheckConstraint enforces. Do NOT weaken."""

TOKEN_PREFIX = "phaze_agent_"  # noqa: S105  # nosec B105 — public wire prefix, not a secret
"""Wire-token prefix (phase-25 D-01). Hashed prefix-included by `hash_token`."""

MAX_AGENT_ID_LENGTH = 64
"""Mirrors `Agent.id` (`String(64)`, models/agent.py). Must be checked BEFORE any DB access —
a length that only Postgres rejects surfaces as an uncaught DataError, not the CLI's friendly
error-plus-exit-1 contract (StringDataRightTruncation is a DBAPIError sibling of IntegrityError,
not a subclass, so the existing `except IntegrityError` around the insert does not catch it)."""

MAX_AGENT_NAME_LENGTH = 128
"""Mirrors `Agent.name` (`String(128)`, models/agent.py). Same pre-DB rationale as
:data:`MAX_AGENT_ID_LENGTH` — applies to both an explicit `--name` and the derived/titleized
name (`agent_id.replace("-", " ").title()`), since titleizing never shortens the string."""


def validate_agent_id(agent_id: str) -> None:
    """Raise ``ValueError`` unless ``agent_id`` matches :data:`AGENT_ID_RE` and fits the
    `agents.id` column width (:data:`MAX_AGENT_ID_LENGTH`)."""
    if not AGENT_ID_RE.fullmatch(agent_id):
        msg = (
            f"invalid agent id {agent_id!r}: must match {AGENT_ID_RE.pattern} "
            "(lowercase letters/digits, single hyphens between segments, no "
            "leading/trailing hyphen)"
        )
        raise ValueError(msg)
    if len(agent_id) > MAX_AGENT_ID_LENGTH:
        msg = f"invalid agent id {agent_id!r}: must be at most {MAX_AGENT_ID_LENGTH} characters (got {len(agent_id)})"
        raise ValueError(msg)


def validate_agent_name(name: str) -> None:
    """Raise ``ValueError`` if ``name`` exceeds the `agents.name` column width
    (:data:`MAX_AGENT_NAME_LENGTH`). Applies equally to an explicit ``--name`` and the
    id-derived/titleized default, so callers must run this on the *effective* name."""
    if len(name) > MAX_AGENT_NAME_LENGTH:
        msg = f"invalid agent name {name!r}: must be at most {MAX_AGENT_NAME_LENGTH} characters (got {len(name)})"
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


async def add_agent(session: AsyncSession, agent_id: str, name: str, scan_roots: list[str], kind: str = "fileserver") -> str:
    """Insert an :class:`Agent` row and return the cleartext bearer token.

    The token is minted with :func:`secrets.token_urlsafe` (CSPRNG) and only its
    sha256 hash (via :func:`hash_token`) is persisted. Callers MUST surface the
    returned cleartext to the operator exactly once -- it cannot be recovered.

    ``kind`` is the agent capability marker (Phase 48): ``"fileserver"`` (the
    default) owns scan roots; ``"compute"`` is a media-less cloud agent with no
    scan roots. The value is constrained at the CLI (argparse ``choices=``) and
    the DB (``ck_agents_kind_enum`` CHECK from Plan 01).

    Does NOT catch :class:`~sqlalchemy.exc.IntegrityError` (e.g. duplicate id);
    that is left to propagate so the caller can map it to a friendly message.
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    agent = Agent(id=agent_id, name=name, token_hash=hash_token(token), scan_roots=scan_roots, kind=kind)
    session.add(agent)
    await session.commit()
    return token


async def _run_add(agent_id: str, name: str, scan_roots: list[str], kind: str = "fileserver") -> str:
    """Open a session and delegate to :func:`add_agent`; return the cleartext token."""
    async with async_session() as session:
        return await add_agent(session, agent_id, name, scan_roots, kind=kind)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser with an ``agents add`` subcommand."""
    parser = argparse.ArgumentParser(prog="phaze", description="Phaze management CLI.")
    subcommands = parser.add_subparsers(dest="group", required=True)

    agents = subcommands.add_parser("agents", help="Manage agents (file-server identities).")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)

    add = agents_sub.add_parser("add", help="Register an agent and mint a bearer token.")
    add.add_argument("--id", dest="agent_id", required=True, help="Agent id (kebab-case: ^[a-z0-9]+(-[a-z0-9]+)*$).")
    add.add_argument("--name", dest="name", default=None, help="Human-readable name (defaults to the titleized id).")
    # Outer layer of the 3-layer kind defense (Phase 48): argparse `choices=`
    # rejects any value other than fileserver/compute before a session opens.
    # Middle layer is AgentSettings.kind (Literal); inner is ck_agents_kind_enum.
    add.add_argument(
        "--kind",
        dest="kind",
        choices=("fileserver", "compute"),
        default="fileserver",
        help="Agent kind. 'compute' = media-less cloud agent with no scan roots.",
    )
    add.add_argument(
        "--scan-roots",
        dest="scan_roots",
        required=False,
        default="",
        help="Comma-separated absolute paths the agent may read/write (e.g. /data/music,/data/concerts). Required for --kind fileserver; omitted for --kind compute.",
    )

    # phaze-grx3: operator diagnostic -- split a queue's 'active' count into genuinely-running vs
    # claimed-but-buffered rows, so "active: N" is never misread as "N files running".
    queue_grp = subcommands.add_parser("queue", help="SAQ queue diagnostics.")
    queue_sub = queue_grp.add_subparsers(dest="queue_command", required=True)
    status = queue_sub.add_parser(
        "status",
        help="Break a queue's status='active' count into RUNNING vs CLAIMED-but-unrun vs STRANDED; exit 1 on the phaze-o0n6 alarm.",
        description=(
            "SAQ marks a row 'active' at dequeue and buffers it in-process; only 'concurrency' rows "
            "actually run at once, so a raw 'active' count over-reports. This splits it using the "
            "attempts signal: attempts>=1 is genuinely running, attempts=0 is claimed-but-unrun. "
            "It ALSO reports 'stranded' -- rows past their own timeout plus the reap slack, i.e. "
            "exactly what reap_stranded_active_jobs will delete -- and EXITS 1 when that count "
            "exceeds the lane's concurrency (phaze-o0n6). A lane can have at most 'concurrency' rows "
            "legitimately running, so more stranded than that is abandoned claims holding "
            "deterministic keys hostage: their files cannot be re-enqueued by any path until reaped. "
            "Run it from a monitor so the next occurrence is detected, not discovered 2,400 rows later."
        ),
    )
    status.add_argument("--queue", dest="queue_name", required=True, help="SAQ queue name (e.g. phaze-agent-nox-analyze).")
    status.add_argument(
        "--concurrency",
        dest="concurrency",
        type=int,
        default=None,
        help=(
            "Override the lane concurrency the stranded-row alarm compares against. Defaults to the "
            "queue's own lane knob (queue name suffix -> lane_<lane>_concurrency, clamped by "
            "WORKER_MAX_JOBS) -- correct when run against the deployment that owns the queue, and the "
            "reason this override exists when it is not."
        ),
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

    # Dispatch on the group BEFORE touching any group-specific attribute. `args`
    # only carries the selected subparser's dest names, so reading `args.agent_id`
    # unconditionally (as this did while `agents` was the only group) raises
    # AttributeError the moment a second group exists.
    if args.group == "queue":
        return _main_queue_status(args)
    return _main_agents_add(args)


def _main_queue_status(args: argparse.Namespace) -> int:
    """Handle ``phaze queue status``. Returns a process exit code.

    Exit 1 is the phaze-o0n6 GUARD, not a command failure: the read succeeded and found more rows
    stranded in ``status='active'`` than the lane's concurrency, a condition that cannot arise from
    healthy operation and that leaves every one of those rows' files un-requeueable until reaped. A
    degraded read (unreadable ``saq_jobs``) still exits 0 -- a missing measurement must not masquerade
    as a detected incident.
    """
    breakdown = asyncio.run(_run_queue_status(args.queue_name, args.concurrency))
    for line in breakdown.as_lines():
        print(line)
    return 1 if breakdown.exceeds_concurrency else 0


async def _run_queue_status(queue_name: str, concurrency: int | None = None) -> ActiveJobBreakdown:
    """Read the RUNNING vs CLAIMED-but-unrun vs STRANDED split for ``queue_name`` (phaze-grx3/o0n6)."""
    async with async_session() as session:
        return await summarize_active_jobs(session, queue_name, concurrency=concurrency)


def _main_agents_add(args: argparse.Namespace) -> int:
    """Handle ``phaze agents add``. Returns a process exit code."""
    agent_id: str = args.agent_id
    name: str = args.name if args.name is not None else agent_id.replace("-", " ").title()
    kind: str = args.kind
    scan_roots: list[str] = [part.strip() for part in args.scan_roots.split(",") if part.strip()]

    # Validate BEFORE any DB access so an invalid id never opens a session.
    # A compute agent owns no media and no scan roots, so the absolute-path
    # requirement is enforced ONLY for fileserver agents (Phase 48); a fileserver
    # with no roots still fails (validate_scan_roots rejects the empty list path).
    try:
        validate_agent_id(agent_id)
        validate_agent_name(name)
        if kind == "fileserver":
            if not scan_roots:
                msg = "--scan-roots is required for --kind fileserver (at least one absolute path)"
                raise ValueError(msg)
            validate_scan_roots(scan_roots)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        token = asyncio.run(_run_add(agent_id, name, scan_roots, kind=kind))
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
