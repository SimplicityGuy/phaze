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
import datetime
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
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.enqueue_router import NoActiveAgentError, lane_for_task, select_active_agent
from phaze.services.fingerprint_requeue import FingerprintEnqueueResult, enqueue_fingerprint_jobs, select_outage_failed_files
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


def parse_window(value: str, field: str) -> datetime.datetime:
    """Parse an ISO-8601 window bound, normalizing to a tz-aware UTC datetime.

    A naive input is INTERPRETED as UTC rather than local time: the operator is matching
    against ``fingerprint_results.updated_at``, which Postgres stores in UTC, and silently
    applying the shell's local offset would shift the window by hours and quietly select
    the wrong population.
    """
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"invalid {field} {value!r}: expected ISO-8601 (e.g. 2026-07-18T05:00:00)"
        raise ValueError(msg) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


async def _run_requeue(
    since: datetime.datetime, until: datetime.datetime, limit: int | None, dry_run: bool
) -> tuple[int, FingerprintEnqueueResult, str | None]:
    """Select the burned files and (unless ``dry_run``) re-enqueue them.

    Returns ``(selected, result, agent_id)``; for a dry run / empty selection ``result`` is an
    empty tally and ``agent_id`` is ``None`` (nothing was enqueued). ``result.accepted`` excludes
    jobs the deterministic-key hook collapsed, and ``result.blocked`` (phaze-e57w) separates files
    collided against a DEAD zombie key from those genuinely in flight.

    Ordering mirrors the bulk retry endpoints: resolve the target agent BEFORE any
    enqueue, so "no active agent" fails loudly having changed nothing rather than
    stranding jobs on a default queue.
    """
    empty = FingerprintEnqueueResult(accepted=0, in_flight=0, blocked=0, blocked_keys=())
    settings = get_settings()
    async with async_session() as session:
        files = await select_outage_failed_files(session, since, until, limit=limit)
        if not files or dry_run:
            return len(files), empty, None

        agent = await select_active_agent(session, kind="fileserver")
        router = AgentTaskRouter(queue_url=settings.queue_url, cache_redis_url=settings.redis_url, ledger_sessionmaker=async_session)
        queue = router.queue_for(agent.id, lane_for_task("fingerprint_file"))
        await queue.connect()
        result = await enqueue_fingerprint_jobs(queue, files, agent.id)
        return len(files), result, agent.id


def _main_fingerprint(args: argparse.Namespace) -> int:
    """Handle ``phaze fingerprint requeue``. Returns a process exit code."""
    try:
        since = parse_window(args.since, "--since")
        until = parse_window(args.until, "--until")
        if until <= since:
            msg = f"--until ({args.until}) must be after --since ({args.since})"
            raise ValueError(msg)
        if args.limit is not None and args.limit < 1:
            msg = f"--limit must be >= 1 (got {args.limit})"
            raise ValueError(msg)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        selected, result, agent_id = asyncio.run(_run_requeue(since, until, args.limit, args.dry_run))
    except NoActiveAgentError:
        print(
            "error: no active fileserver agent -- nothing was enqueued. Bring an agent online and re-run.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print(f"DRY RUN -- {selected} file(s) would be re-queued for fingerprinting.")
        print(f"  window: {since.isoformat()} .. {until.isoformat()}")
        print("  Re-run without --dry-run to enqueue.")
        return 0

    if selected == 0:
        print("No fingerprint-FAILED files found in that window; nothing to re-queue.")
        return 0

    print(f"Re-queued {result.accepted} of {selected} selected file(s) to agent {agent_id!r}.")
    # phaze-e57w: a deterministic-key collision is NOT uniformly "already in flight". Report the two
    # cases separately: genuinely-in-flight is benign; BLOCKED-by-a-dead-row is a file that can never
    # recover on its own and needs the aborting-reaper -- so it is printed LOUDLY, not folded away.
    if result.in_flight:
        print(f"  {result.in_flight} skipped -- already in flight (deterministic-key dedup).")
    if result.blocked:
        print(f"  WARNING: {result.blocked} file(s) BLOCKED by a dead job row (status aborting/failed/stuck) --")
        print("           NOT in flight; the file cannot recover until its zombie key is reaped.")
        print("           The controller aborting-reaper releases these automatically (every minute);")
        print("           the affected deterministic keys are:")
        for key in result.blocked_keys:
            print(f"             {key}")
    print("")
    print("  NOTE: if the fingerprint stage is paused, these jobs are PARKED, not running.")
    print("  Release them only once both engines are proven healthy:")
    print("    POST /pipeline/stages/fingerprint/resume")
    return 0


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

    fingerprint = subcommands.add_parser("fingerprint", help="Fingerprint stage operations.")
    fingerprint_sub = fingerprint.add_subparsers(dest="fingerprint_command", required=True)

    requeue = fingerprint_sub.add_parser(
        "requeue",
        help="Re-queue files whose fingerprint stage FAILED during an engine-outage window.",
        description=(
            "Recovery for an engine outage (phaze-rf04.1). Selects music/video files that are "
            "fingerprint-FAILED with at least one failed engine row written inside [--since, --until], "
            "and re-enqueues them. Operator-SKIPPED and dedup-resolved files are excluded. "
            "NOTE: a paused fingerprint stage does NOT block this -- jobs land parked and are released "
            "by POST /pipeline/stages/fingerprint/resume, which is the intended recovery order."
        ),
    )
    requeue.add_argument("--since", dest="since", required=True, help="Window start, ISO-8601 UTC (e.g. 2026-07-18T05:00:00).")
    requeue.add_argument("--until", dest="until", required=True, help="Window end, ISO-8601 UTC (e.g. 2026-07-18T13:39:00).")
    requeue.add_argument("--limit", dest="limit", type=int, default=None, help="Cap the number of files re-queued (for a staged rollout).")
    requeue.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Report what WOULD be re-queued and exit without enqueueing anything.",
    )

    # phaze-grx3: operator diagnostic -- split a queue's 'active' count into genuinely-running vs
    # claimed-but-buffered rows, so "active: N" is never misread as "N files fingerprinting".
    queue_grp = subcommands.add_parser("queue", help="SAQ queue diagnostics.")
    queue_sub = queue_grp.add_subparsers(dest="queue_command", required=True)
    status = queue_sub.add_parser(
        "status",
        help="Break a queue's status='active' count into RUNNING vs CLAIMED-but-unrun (phaze-grx3).",
        description=(
            "SAQ marks a row 'active' at dequeue and buffers it in-process; only 'concurrency' rows "
            "actually run at once, so a raw 'active' count over-reports. This splits it using the "
            "attempts signal: attempts>=1 is genuinely running, attempts=0 is claimed-but-unrun."
        ),
    )
    status.add_argument("--queue", dest="queue_name", required=True, help="SAQ queue name (e.g. phaze-agent-nox-fingerprint).")
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
    if args.group == "fingerprint":
        return _main_fingerprint(args)
    if args.group == "queue":
        return _main_queue_status(args)
    return _main_agents_add(args)


def _main_queue_status(args: argparse.Namespace) -> int:
    """Handle ``phaze queue status``. Returns a process exit code."""
    breakdown = asyncio.run(_run_queue_status(args.queue_name))
    for line in breakdown.as_lines():
        print(line)
    return 0


async def _run_queue_status(queue_name: str) -> ActiveJobBreakdown:
    """Read the RUNNING vs CLAIMED-but-unrun split for ``queue_name`` (phaze-grx3)."""
    async with async_session() as session:
        return await summarize_active_jobs(session, queue_name)


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
