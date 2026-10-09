"""`phaze` management CLI (stdlib argparse, no third-party dependency).

Command groups:

    phaze agents add --id <id> --name <name> --scan-roots /a,/b
    phaze queue status --queue <name>
    phaze backfill reenqueue-incomplete-analyses
    phaze backfill recover-stranded-analyses [--enqueue]
    phaze backfill set-projection
    phaze backfill reset-cloud-attempts --backend <id> --window-start <ts> --window-end <ts> --attempts-floor <n> [--apply]
    phaze backfill moved-twin-candidates --agent <id> > candidates.json
    phaze backfill retire-moved-twins --agent <id> [--apply] < checked.json
    phaze backfill stale-row-candidates --agent <id> [--under <path>] > candidates.json
    phaze backfill reconcile-stale-rows --agent <id> [--apply] < located.json
    phaze backfill companion-features [--apply] [--page-size <n>]
    phaze backfill junk-review [--apply]
    phaze backfill companion-links [--apply]
    phaze junk quarantine [--apply]

`agents add` mints a per-agent bearer token, inserts an `agents` row, and prints
the cleartext token exactly once (it is NOT recoverable afterwards -- only the
sha256 hash is persisted) alongside the derived `phaze-agent-<id>` queue name.
A `--kind fileserver` agent also gets its LIVE sentinel `ScanBatch` seeded here
(via `phaze.services.live_sentinel.ensure_live_sentinel`) so its watcher's
`batch_id`-omitted upserts have somewhere to resolve to from the first call.

`backfill moved-twin-candidates` / `retire-moved-twins` are the phaze-oxn2m one-off cleanup of the
stale rows a watcher move used to leave behind (a file posted under one name, moved, and posted again
under the new one). Only the agent can say whether a path still exists, so it runs in three steps:

    docker compose exec -T api phaze backfill moved-twin-candidates --agent <id> > candidates.json
    docker compose -f docker-compose.agent.yml exec -T watcher uv run python -m phaze.agent_watcher check-paths < candidates.json > checked.json
    docker compose exec -T api phaze backfill retire-moved-twins --agent <id> < checked.json            # dry run
    docker compose exec -T api phaze backfill retire-moved-twins --agent <id> --apply < checked.json    # the operator's call

Run the check in the watcher container: it sees the scan roots at the same paths the rows were posted
with. Selection and retirement rules live in `phaze.services.scan_deletion.retire_moved_twins`.

`backfill stale-row-candidates` / `reconcile-stale-rows` are the phaze-5rfev reconcile of rows whose
file is no longer at its recorded path: re-point a row whose bytes moved, merge it with the row a
later scan made for the same bytes (keeping the older row and all of both rows' history), or mark it
missing (never deleted). Same three-step shape, with the content search on the agent:

    docker compose exec -T api phaze backfill stale-row-candidates --agent <id> [--under <root>] > candidates.json
    docker compose -f docker-compose.agent.yml exec -T watcher uv run python -m phaze.agent_watcher locate-stale < candidates.json > located.json
    docker compose exec -T api phaze backfill reconcile-stale-rows --agent <id> < located.json            # dry run
    docker compose exec -T api phaze backfill reconcile-stale-rows --agent <id> --apply < located.json    # the operator's call

Verdicts and merge rules live in `phaze.services.stale_rows`.

`backfill companion-features` is the phaze-osy6j one-off that covers the companion rows ingested
before the agents reported content features (references, tracklist flag, junk class, encoding) at
ingest. Without `--apply` it only counts, per agent, the companion rows with current features and
those with none, with features of older bytes, or from an older extractor -- in a READ ONLY
transaction. With `--apply` it enqueues one meta-lane `extract_companion_features` job per page of
those rows on the owning agent, which reads them off its mount and reports them through the same
route ingest uses. Idempotent: a row whose features are current is never selected again. Selection
lives in `phaze.services.companion_content`. Upgrade the agents before `--apply`: an agent image that
predates the task fails the jobs (nothing is written), and the rows stay selected for the next run.

`backfill junk-review` runs the phaze-bk5jp junk-companion detector over every agent: companion
rows that are junk by their stored content features, or byte-identical orphan copies of an
already-linked companion (operator decision 1 of 2026-10-07, epic phaze-4x319), plus quarantined
identities that came back (decision 4). Without `--apply` it only counts, per agent and reason, the pending rows a pass would
create, keep or withdraw -- in a READ ONLY transaction. With `--apply` it writes them and commits.
Nothing is ever approved here: every row it creates is pending. Rules live in
`phaze.services.companion_junk_review`.

`junk quarantine` lists every APPROVED junk-review row -- agent, reason, size, path and the
destination the agent would move it to, `<root>/.phaze-quarantine/<relative path>` -- in a READ ONLY
transaction. That is the dry run, and the default. With `--apply` it dispatches them: each row moves
to `executing` and one meta-lane `quarantine_companion` job goes to its owning agent, which re-checks
the file and moves it (phaze-lwuf6, `phaze.services.junk_quarantine`). Only rows in the `approved`
status are ever listed or moved.

`backfill companion-links` is the phaze-spd83 one-off that links the companions ingested while
association ran only when the operator asked for it (phaze-bniuk): it runs the same unit the
automatic run does, per agent -- re-derive every link of the companions with current content
features, then refresh the junk-review queue. Without `--apply` it only counts, per agent, the links
a run would add, remove and keep, by chain step, and the companions still awaiting features -- in a
READ ONLY transaction, with the junk-review refresh skipped (its duplicates read the stored links).
With `--apply` it writes, one committed page at a time, under the agent's association lock (an agent
whose automatic run holds the lock is reported and skipped). Idempotent: a second run writes nothing.
Run it after `backfill companion-features --apply` has finished (phaze-rmhfr's deploy order).
Rules live in `phaze.services.companion` and `phaze.services.companion_autolink`.

`backfill reenqueue-incomplete-analyses` is the phaze-kj8dl one-time operator command: it
re-enqueues every file whose prior analysis did not cover the whole file (the payoff step of the
exhaustive-analysis decision in ``docs/design/0007-windowed-analysis.md``). It lives HERE, not under
`scripts/` (a bare repo-tree script is never COPYed into the `api` image -- see
`docs/runbook.md`'s "One-time exhaustive-analysis re-enqueue backfill" section for the full
deployment ordering and the Dockerfile COPY list this depends on), so `docker compose exec api
phaze backfill reenqueue-incomplete-analyses` actually works against a deployed container. All
selection/routing logic lives in `phaze.services.reanalysis_backfill`; this module is a thin
argparse + print wrapper, matching `agents add` / `queue status`'s own split.

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
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import secrets
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from phaze.config import get_settings
from phaze.database import async_session
from phaze.logging_config import configure_logging
from phaze.models.agent import Agent
from phaze.routers.agent_auth import hash_token
from phaze.schemas.agent_tasks import CompanionFeaturesTarget, ExtractCompanionFeaturesPayload
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.cloud_attempts_reset import ResetScope, apply_reset, preview_reset
from phaze.services.companion_autolink import agent_association_lock, run_agent_association
from phaze.services.companion_content import count_backfill, select_backfill_page
from phaze.services.companion_junk_review import detect_junk_reviews
from phaze.services.junk_quarantine import enqueue_quarantine, plan_quarantine
from phaze.services.live_sentinel import ensure_live_sentinel
from phaze.services.queue_introspection import ActiveJobBreakdown, summarize_active_jobs
from phaze.services.reanalysis_backfill import (
    ReanalysisOutcome,
    compute_exit_code,
    count_applied_incomplete_analyses_rows,
    count_null_windows_columns_rows,
    enqueue_incomplete_reanalysis,
)
from phaze.services.scan_deletion import moved_twin_candidates, retire_moved_twins
from phaze.services.set_projection_backfill import run_backfill
from phaze.services.stale_rows import Verdict, reconcile_stale_rows, stale_row_candidates
from phaze.services.stranded_analysis_recovery import select_stranded_analysis_keys
from phaze.tasks.reenqueue import recover_orphaned_work


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

COMPANION_FEATURES_PAGE_SIZE = 200
"""Companion rows per ``extract_companion_features`` job: a few seconds of small reads on the meta lane."""

COMPANION_FEATURES_PAGE_MAX = 1000
"""Mirrors the default ``agent_file_chunk_max``, the payload's and the report chunk's bound."""

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

    ``kind`` is the agent capability marker: ``"fileserver"`` (the
    default) owns scan roots; ``"compute"`` is a media-less cloud agent with no
    scan roots. The value is constrained at the CLI (argparse ``choices=``) and
    the DB through the ``ck_agents_kind_enum`` CHECK.

    phaze-tvdu1: a ``"fileserver"`` agent also gets its LIVE sentinel
    ``ScanBatch`` seeded here, via the shared
    :func:`phaze.services.live_sentinel.ensure_live_sentinel` -- the same
    helper the dev seed (``services/agent_bootstrap.py``) and the
    ``upsert_files`` self-heal path (``routers/agent_files.py``) use. Without
    it, an agent registered through this CLI had no sentinel until its first
    watcher upsert self-healed one (or, pre-phaze-tvdu1, 500ed instead). A
    ``"compute"`` agent never scans files and gets no sentinel.

    Does NOT catch :class:`~sqlalchemy.exc.IntegrityError` (e.g. duplicate id);
    that is left to propagate so the caller can map it to a friendly message.
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    agent = Agent(id=agent_id, name=name, token_hash=hash_token(token), scan_roots=scan_roots, kind=kind)
    session.add(agent)
    if kind == "fileserver":
        await session.flush()
        await ensure_live_sentinel(session, agent_id)
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
    # Outer layer of the 3-layer kind defense: argparse `choices=`
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

    # phaze-kj8dl: one-time operator backfill, see docs/runbook.md's "One-time exhaustive-analysis
    # re-enqueue backfill" section for the full deployment ordering this command is step 4 of.
    backfill_grp = subcommands.add_parser("backfill", help="One-time operator backfills.")
    backfill_sub = backfill_grp.add_subparsers(dest="backfill_command", required=True)
    backfill_sub.add_parser(
        "reenqueue-incomplete-analyses",
        help="Re-enqueue every file whose prior analysis did not cover the whole file (phaze-kj8dl).",
        description=(
            "Selects every file whose AnalysisResult windows coverage is incomplete "
            "(fine_windows_analyzed < fine_windows_total OR the coarse equivalent -- never the "
            "dropped 'sampled' column) and NOT already applied/moved, then routes each one: a file "
            "already in the cloud pipeline is skipped, a long file (>= the duration threshold, cloud "
            "enabled, not force-localed) is HELD for the bounded cloud drain, and everything else is "
            "enqueued through the standard per-agent process_file funnel. Idempotent: safe to re-run. "
            "See docs/runbook.md's phaze-kj8dl section for the full deployment ordering and the "
            "per-outcome remediation guide."
        ),
    )
    # phaze-x1qr3.3: the set-projection backfill. Reads only already-stored analysis_window rows
    # (musical_key/bpm/features JSONB) -- NEVER re-analyzes -- and writes the per-window
    # energy/camelot/mood_scores plus the per-file set_profile row for every file whose profile is
    # missing or behind services.set_projection_writer.CURRENT_PROJECTION_VERSION. Idempotent and
    # resumable by that same version check: see services/set_projection_backfill.py's module
    # docstring for the full selection contract.
    backfill_sub.add_parser(
        "set-projection",
        help="Backfill energy/camelot/mood_scores + set_profile from stored JSONB, no re-analysis (phaze-x1qr3.3).",
        description=(
            "Walks every file with analysis_window rows whose set_profile is missing or behind the "
            "current projection_version, computes the per-window energy/camelot/mood_scores and the "
            "per-file set_profile row from the ALREADY-STORED musical_key/bpm/features JSONB -- no "
            "re-analysis, no agent involvement -- and reports counts. Idempotent: a second run is a "
            "no-op; a future projection_version bump re-fills only the rows that change."
        ),
    )
    stranded = backfill_sub.add_parser(
        "recover-stranded-analyses",
        help="Count incomplete fine-tier analyses with lost queue keys; --enqueue replays only those keys (phaze-hia9z).",
    )
    stranded.add_argument("--enqueue", action="store_true", help="Replay the selected keys; without this flag the command reads and counts only.")
    # phaze-ww6yk: reset the cloud attempts an infrastructure fault spent. Every scope parameter is a
    # required flag, so the command names one incident and cannot drift onto rows it was not asked about.
    reset = backfill_sub.add_parser(
        "reset-cloud-attempts",
        help="Reset cloud attempts spent by an infrastructure fault, for one backend and time window (phaze-ww6yk). Dry run unless --apply.",
        description=(
            "Selects cloud_job rows with status='awaiting', attempts >= --attempts-floor, backend_id = --backend and "
            "updated_at inside [--window-start, --window-end]. It excludes rows whose analysis is done, in flight or "
            "applied, and rows that touched the node-loss budget. It then resets attempts to 0, restarts the "
            "lane-entry clock, and unfolds that chain from the durable cloud_budget ledger so no cooldown bars "
            "cloud. Without --apply it runs in a READ ONLY transaction and prints the count, the breakdown and one "
            "audit line per row. See phaze.services.cloud_attempts_reset for the recorded decisions."
        ),
    )
    reset.add_argument(
        "--backend", dest="backend_id", required=True, help="cloud_job.backend_id of the backend whose rows to reset (the burst backend)."
    )
    reset.add_argument(
        "--window-start",
        dest="window_start",
        required=True,
        type=_aware_datetime,
        help="Inclusive updated_at lower bound, ISO 8601 with offset (e.g. 2026-09-26T16:11:00Z).",
    )
    reset.add_argument(
        "--window-end",
        dest="window_end",
        required=True,
        type=_aware_datetime,
        help="Inclusive updated_at upper bound, ISO 8601 with offset (e.g. 2026-09-27T03:29:00Z).",
    )
    reset.add_argument(
        "--attempts-floor",
        dest="attempts_floor",
        required=True,
        type=int,
        help="Select rows with attempts >= this (the cloud_submit_max_attempts cap for exhausted rows).",
    )
    # phaze-oxn2m: the candidates -> agent existence check -> retire pipeline (module docstring).
    twins = backfill_sub.add_parser(
        "moved-twin-candidates",
        help="Print, as JSON, every group of an agent's rows sharing a filename and size (phaze-oxn2m step 1 of 3).",
    )
    twins.add_argument("--agent", dest="agent_id", required=True, help="The fileserver agent whose rows to group.")
    retire = backfill_sub.add_parser(
        "retire-moved-twins",
        help="Retire rows whose file is gone and whose moved-to twin exists, from the agent-checked JSON on stdin (phaze-oxn2m step 3). "
        "Dry run unless --apply.",
        description=(
            "Reads the document `python -m phaze.agent_watcher check-paths` printed on the agent. In each group, a row whose path "
            "is gone is retired (deleted with its derived rows) only when exactly one other row of the group exists on disk; that "
            "row is kept. A row carrying operator-reviewed state is reported and never deleted. Without --apply it runs READ ONLY."
        ),
    )
    retire.add_argument("--agent", dest="agent_id", required=True, help="The agent the checked document was produced for.")
    retire_mode = retire.add_mutually_exclusive_group()
    retire_mode.add_argument("--dry-run", dest="apply", action="store_false", help="Count and classify only, read-only (the default).")
    retire_mode.add_argument("--apply", dest="apply", action="store_true", help="Delete the stale rows. Without it nothing is written.")
    retire.set_defaults(apply=False)
    # phaze-5rfev: the candidates -> agent locate -> reconcile pipeline (module docstring).
    stale = backfill_sub.add_parser(
        "stale-row-candidates",
        help="Print, as JSON, an agent's rows for the agent-side `locate-stale` check (phaze-5rfev step 1 of 3).",
    )
    stale.add_argument("--agent", dest="agent_id", required=True, help="The fileserver agent whose rows to check.")
    stale.add_argument("--under", dest="under", default=None, help="Only rows whose current path is at or below this path.")
    reconcile = backfill_sub.add_parser(
        "reconcile-stale-rows",
        help="Re-point, merge or mark missing the rows `locate-stale` found gone, from its JSON on stdin (phaze-5rfev step 3). "
        "Dry run unless --apply.",
        description=(
            "Reads the document `python -m phaze.agent_watcher locate-stale` printed on the agent. A gone row whose bytes are at "
            "exactly one other path is re-pointed there, or merged with the row a scan already made for that path (the older row "
            "is kept with both rows' history). A gone row whose bytes are nowhere is marked missing, never deleted. A row "
            "carrying operator-reviewed state is reported and left alone. Without --apply it runs READ ONLY."
        ),
    )
    reconcile.add_argument("--agent", dest="agent_id", required=True, help="The agent the located document was produced for.")
    reconcile_mode = reconcile.add_mutually_exclusive_group()
    reconcile_mode.add_argument("--dry-run", dest="apply", action="store_false", help="Classify only, read-only (the default).")
    reconcile_mode.add_argument("--apply", dest="apply", action="store_true", help="Write the verdicts. Without it nothing is written.")
    reconcile.set_defaults(apply=False)
    # phaze-osy6j: companion content features for rows ingested before the agents reported them.
    features = backfill_sub.add_parser(
        "companion-features",
        help="Count companion rows without current content features; --apply has each owning agent read them (phaze-osy6j). Dry run unless --apply.",
    )
    features_mode = features.add_mutually_exclusive_group()
    features_mode.add_argument("--dry-run", dest="apply", action="store_false", help="Count only, read-only (the default).")
    features_mode.add_argument("--apply", dest="apply", action="store_true", help="Enqueue the agent reads. Without it nothing is enqueued.")
    features.add_argument(
        "--page-size",
        dest="page_size",
        type=int,
        default=COMPANION_FEATURES_PAGE_SIZE,
        help=f"Companion rows per agent job (1-1000, default {COMPANION_FEATURES_PAGE_SIZE}).",
    )
    features.set_defaults(apply=False)
    # phaze-bk5jp: the junk-companion detector.
    junk = backfill_sub.add_parser(
        "junk-review",
        help="Count the pending junk-review rows the detector would create per agent and reason; --apply writes them (phaze-bk5jp).",
    )
    junk_mode = junk.add_mutually_exclusive_group()
    junk_mode.add_argument("--dry-run", dest="apply", action="store_false", help="Count only, read-only (the default).")
    junk_mode.add_argument("--apply", dest="apply", action="store_true", help="Write the pending rows. Without it nothing is written.")
    junk.set_defaults(apply=False)
    # phaze-spd83: link the companions association never reached while it ran only on demand.
    links = backfill_sub.add_parser(
        "companion-links",
        help="Count the companion links an association run would add, remove and keep per agent; --apply runs it (phaze-spd83).",
    )
    links_mode = links.add_mutually_exclusive_group()
    links_mode.add_argument("--dry-run", dest="apply", action="store_false", help="Count only, read-only (the default).")
    links_mode.add_argument("--apply", dest="apply", action="store_true", help="Write the links. Without it nothing is written.")
    links.set_defaults(apply=False)
    mode = reset.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="Count and classify only, read-only (the default).")
    mode.add_argument("--apply", dest="apply", action="store_true", help="Write the reset. Without it nothing is written.")
    reset.set_defaults(apply=False)
    # phaze-lwuf6: dispatch the APPROVED junk companions to the quarantine move.
    junk_grp = subcommands.add_parser("junk", help="Junk-companion review actions.")
    junk_sub = junk_grp.add_subparsers(dest="junk_command", required=True)
    quarantine = junk_sub.add_parser(
        "quarantine",
        help="List the approved junk companions and where each would be moved; --apply dispatches the moves (phaze-lwuf6).",
    )
    quarantine_mode = quarantine.add_mutually_exclusive_group()
    quarantine_mode.add_argument("--dry-run", dest="apply", action="store_false", help="List only, read-only (the default).")
    quarantine_mode.add_argument("--apply", dest="apply", action="store_true", help="Dispatch the moves. Without it nothing is moved.")
    quarantine.set_defaults(apply=False)
    return parser


def _aware_datetime(value: str) -> datetime:
    """argparse ``type=`` for the window bounds: ISO 8601 WITH an offset. A naive time is refused, not assumed UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"not an ISO 8601 timestamp: {value!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    if parsed.tzinfo is None:
        msg = f"timestamp {value!r} has no UTC offset; add one (e.g. 'Z' or '+00:00')"
        raise argparse.ArgumentTypeError(msg)
    return parsed


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
    if args.group == "backfill":
        return _main_backfill(args)
    if args.group == "junk":
        return asyncio.run(_run_junk_quarantine(apply=args.apply))
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


def _main_backfill(args: argparse.Namespace) -> int:
    """Handle ``phaze backfill <command>``. Returns a process exit code."""
    if args.backfill_command == "reenqueue-incomplete-analyses":
        return asyncio.run(_run_reenqueue_incomplete_analyses())
    if args.backfill_command == "set-projection":
        return asyncio.run(_run_backfill_set_projection())
    if args.backfill_command == "recover-stranded-analyses":
        return asyncio.run(_run_recover_stranded_analyses(enqueue=args.enqueue))
    if args.backfill_command == "reset-cloud-attempts":
        try:
            scope = ResetScope(args.backend_id, args.window_start, args.window_end, args.attempts_floor)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return asyncio.run(_run_reset_cloud_attempts(scope, apply=args.apply))
    if args.backfill_command == "moved-twin-candidates":
        return asyncio.run(_run_moved_twin_candidates(args.agent_id))
    if args.backfill_command == "retire-moved-twins":
        return asyncio.run(_run_retire_moved_twins(args.agent_id, sys.stdin.read(), apply=args.apply))
    if args.backfill_command == "stale-row-candidates":
        return asyncio.run(_run_stale_row_candidates(args.agent_id, args.under))
    if args.backfill_command == "reconcile-stale-rows":
        return asyncio.run(_run_reconcile_stale_rows(args.agent_id, sys.stdin.read(), apply=args.apply))
    if args.backfill_command == "companion-features":
        if not 1 <= args.page_size <= COMPANION_FEATURES_PAGE_MAX:
            print(f"error: --page-size must be between 1 and {COMPANION_FEATURES_PAGE_MAX}", file=sys.stderr)
            return 1
        return asyncio.run(_run_companion_features(apply=args.apply, page_size=args.page_size))
    if args.backfill_command == "junk-review":
        return asyncio.run(_run_junk_review(apply=args.apply))
    if args.backfill_command == "companion-links":
        return asyncio.run(_run_companion_links(apply=args.apply))
    msg = f"unhandled backfill command: {args.backfill_command!r}"  # pragma: no cover - exhaustive dispatch above
    raise AssertionError(msg)  # pragma: no cover


async def _run_backfill_set_projection() -> int:
    """Run ``phaze backfill set-projection`` (phaze-x1qr3.3). Returns a process exit code.

    Opens ONE session for the whole run (mirrors ``_run_reenqueue_incomplete_analyses``'s
    ``async_session()`` wiring) and delegates the walk itself to
    ``services.set_projection_backfill.run_backfill``, which commits per file so a later file's
    failure can never roll back an earlier file's already-written projection.
    """
    async with async_session() as session:
        report = await run_backfill(session)
    print(f"{report.files_scanned} file(s) scanned")
    print(f"{report.files_projected} file(s) projected")
    print(f"{report.files_skipped_no_windows} file(s) skipped (no windows)")
    print(f"{report.files_failed} file(s) failed")
    print(f"wall clock: {report.wall_clock_sec:.2f}s")
    return 1 if report.files_failed else 0


async def _run_recover_stranded_analyses(*, enqueue: bool) -> int:
    """Count the exact phaze-hia9z population, then optionally replay only those keys."""
    async with async_session() as session:
        keys = await select_stranded_analysis_keys(session)
    print(f"{len(keys)} stranded analysis file(s) selected")
    if not enqueue or not keys:
        return 0

    settings = get_settings()
    task_router = AgentTaskRouter(queue_url=settings.queue_url, cache_redis_url=settings.redis_url, ledger_sessionmaker=async_session)
    try:
        result = await recover_orphaned_work({"async_session": async_session, "queue": None, "task_router": task_router}, force=True, only_keys=keys)
    finally:
        await task_router.close()

    tally = result["stages"]["process_file"]
    print("summary: " + " ".join(f"{name}={tally[name]}" for name in ("reenqueued", "skipped", "errored", "unreplayable")))
    return 0 if tally["reenqueued"] == len(keys) else 1


async def _run_reset_cloud_attempts(scope: ResetScope, *, apply: bool) -> int:
    """Run ``phaze backfill reset-cloud-attempts`` (phaze-ww6yk). Returns a process exit code.

    The dry run opens its transaction ``READ ONLY``, so Postgres itself refuses a write, and rolls back.
    ``--apply`` prints the same breakdown and audit lines, then commits only when the UPDATE's rowcount
    equals the number of rows classified for reset under the lock. On a mismatch it rolls back and exits 1.
    """
    async with async_session() as session:
        if apply:
            report = await apply_reset(session, scope)
        else:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            report = await preview_reset(session, scope)
        for line in report.breakdown_lines():
            print(line)
        for line in report.audit_lines():
            print(line)
        expected = len(report.to_reset)
        if not apply:
            await session.rollback()
            print(f"DRY RUN: nothing written; {expected} row(s) would be reset. Re-run with --apply to write.")
            return 0
        if report.rows_reset != expected:
            await session.rollback()
            print(
                f"error: reset matched {report.rows_reset} row(s) but {expected} were classified for reset; rolled back, nothing written",
                file=sys.stderr,
            )
            return 1
        await session.commit()
    print(
        f"APPLIED: {report.rows_reset} row(s) reset; cloud_budget rows deleted={report.ledger_rows_deleted} decremented={report.ledger_rows_decremented}"
    )
    return 0


async def _run_moved_twin_candidates(agent_id: str) -> int:
    """Print ``phaze backfill moved-twin-candidates``' JSON document on stdout (phaze-oxn2m). Read-only."""
    async with async_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            document = await moved_twin_candidates(session, agent_id)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(document))
    groups = document["groups"]
    print(f"{len(groups)} group(s), {sum(len(group) for group in groups)} row(s) for agent {agent_id!r}", file=sys.stderr)
    return 0


async def _run_retire_moved_twins(agent_id: str, checked_json: str, *, apply: bool) -> int:
    """Run ``phaze backfill retire-moved-twins`` (phaze-oxn2m). Returns a process exit code.

    The dry run opens its transaction READ ONLY, so Postgres itself refuses a write. One line per
    stale/twin pair, then the counts; ``--apply`` deletes in one transaction and commits at the end.
    """
    try:
        checked = json.loads(checked_json)
    except json.JSONDecodeError as exc:
        print(f"error: stdin is not the checked JSON document: {exc}", file=sys.stderr)
        return 1
    async with async_session() as session:
        if not apply:
            await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            report = await retire_moved_twins(session, agent_id, checked, apply=apply)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for stale_id, twin_id in report.retire:
            print(f"  retire {stale_id}  keep {twin_id}")
        for stale_id, twin_id, blockers in report.kept_reviewed:
            print(f"  KEEP   {stale_id}  twin {twin_id}  reviewed: {','.join(blockers)}")
        print(
            f"summary: retire={len(report.retire)} (same content={report.same_content}) kept_reviewed={len(report.kept_reviewed)} "
            f"absent_without_twin={report.absent_without_twin} absent_with_several_twins={report.absent_with_several_twins} "
            f"unverifiable_groups={report.unverifiable_groups} changed_since_check={report.changed_since_check}"
        )
        if not apply:
            await session.rollback()
            print(f"DRY RUN: nothing written; {len(report.retire)} row(s) would be retired. Re-run with --apply to write.")
            return 0
        await session.commit()
    print(f"APPLIED: {len(report.retire)} row(s) retired")
    return 0


async def _run_stale_row_candidates(agent_id: str, under: str | None) -> int:
    """Print ``phaze backfill stale-row-candidates``' JSON document on stdout (phaze-5rfev). Read-only."""
    async with async_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            document = await stale_row_candidates(session, agent_id, under)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(document))
    print(f"{len(document['rows'])} row(s) for agent {agent_id!r}", file=sys.stderr)
    return 0


async def _run_reconcile_stale_rows(agent_id: str, located_json: str, *, apply: bool) -> int:
    """Run ``phaze backfill reconcile-stale-rows`` (phaze-5rfev). Returns a process exit code.

    The dry run opens its transaction READ ONLY, so Postgres itself refuses a write. One line per row
    that needs (or blocks) a change, then the counts; ``--apply`` writes in one transaction and commits
    at the end.
    """
    try:
        located = json.loads(located_json)
    except json.JSONDecodeError as exc:
        print(f"error: stdin is not the located JSON document: {exc}", file=sys.stderr)
        return 1
    async with async_session() as session:
        if not apply:
            await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            report = await reconcile_stale_rows(session, agent_id, located, apply=apply)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for action in report.actions:
            line = f"  {action.verdict.value.upper():<13} {action.file_id}  {action.path}"
            if action.target_path is not None:
                line += f"  -> {action.target_path}"
            if action.target_id is not None:
                line += f"  (merge into row {action.target_id})" if action.verdict is Verdict.MERGE else f"  (row {action.target_id})"
            if action.detail:
                line += f"  [{'; '.join(action.detail)}]"
            print(line)
        counts = report.counts()
        print(
            "summary: "
            + " ".join(f"{verdict}={count}" for verdict, count in counts.items())
            + f" already_missing={report.already_missing} present={report.present} unverifiable={report.unverifiable} walk_errors={report.walk_errors}"
        )
        writes = sum(counts[verdict.value] for verdict in (Verdict.REPOINT, Verdict.MERGE, Verdict.MISSING, Verdict.RESTORED))
        if not apply:
            await session.rollback()
            print(f"DRY RUN: nothing written; {writes} row(s) would change. Re-run with --apply to write.")
            return 0
        await session.commit()
    print(f"APPLIED: {writes} row(s) changed")
    return 0


async def _run_companion_features(*, apply: bool, page_size: int) -> int:
    """Run ``phaze backfill companion-features`` (phaze-osy6j). Returns a process exit code.

    The counts are read first, in a READ ONLY transaction either way. ``--apply`` then walks each
    agent's pending rows by keyset page and enqueues one ``extract_companion_features`` job per page
    on that agent's meta lane, printing a line per agent; it exits 1 if any enqueue failed.
    """
    async with async_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        counts = await count_backfill(session)
        await session.rollback()
    for row in counts:
        print(
            f"  {row.agent_id}: companions={row.companions} current={row.current} missing={row.missing} "
            f"stale_content={row.stale_content} stale_extractor={row.stale_extractor}"
        )
    pending = sum(row.pending for row in counts)
    jobs = sum(-(-row.pending // page_size) for row in counts)
    print(f"summary: companions={sum(row.companions for row in counts)} pending={pending} jobs={jobs} page_size={page_size}")
    if not apply:
        print(f"DRY RUN: nothing enqueued; {pending} companion row(s) would be read in {jobs} job(s). Re-run with --apply to enqueue.")
        return 0

    settings = get_settings()
    task_router = AgentTaskRouter(queue_url=settings.queue_url, cache_redis_url=settings.redis_url, ledger_sessionmaker=async_session)
    failed = 0
    try:
        for row in counts:
            if not row.pending:
                continue
            enqueued, rows, error = await _enqueue_companion_feature_pages(task_router, row.agent_id, page_size)
            failed += bool(error)
            print(f"  {row.agent_id}: enqueued {enqueued} job(s) for {rows} row(s)" + (f"; STOPPED: {error}" if error else ""))
    finally:
        await task_router.close()
    print("APPLIED: the agents report features as the jobs run; re-run without --apply to watch pending fall.")
    return 1 if failed else 0


async def _run_junk_review(*, apply: bool) -> int:
    """Run ``phaze backfill junk-review`` (phaze-bk5jp). Returns a process exit code.

    One transaction per agent: READ ONLY and rolled back on a dry run, committed on ``--apply``.
    """
    async with async_session() as session:
        agent_ids = list((await session.execute(select(Agent.id).order_by(Agent.id))).scalars())
    totals: Counter[str] = Counter()
    for agent_id in agent_ids:
        async with async_session() as session:
            if not apply:
                await session.execute(text("SET TRANSACTION READ ONLY"))
            outcome = await detect_junk_reviews(session, agent_id, apply=apply)
            if apply:
                await session.commit()
            else:
                await session.rollback()
        totals.update(outcome.created)
        by_reason = " ".join(f"{reason}={count}" for reason, count in sorted(outcome.created.items())) or "none"
        print(
            f"  {agent_id}: new pending: {by_reason} (reappeared={outcome.reappeared}); kept pending={outcome.refreshed} "
            f"withdrawn={outcome.withdrawn} already decided={outcome.decided} skipped rejected content={outcome.rejected_content}"
        )
    print(f"summary: new pending={sum(totals.values())} " + " ".join(f"{reason}={count}" for reason, count in sorted(totals.items())))
    if not apply:
        print("DRY RUN: nothing written. Re-run with --apply to create the pending rows.")
    else:
        print("APPLIED: pending rows written; nothing is approved until the operator decides.")
    return 0


async def _run_junk_quarantine(*, apply: bool) -> int:
    """Run ``phaze junk quarantine`` (phaze-lwuf6). Returns a process exit code.

    The listing is read in a READ ONLY transaction. ``--apply`` then dispatches exactly the listed
    rows; one approved after the listing waits for the next run.
    """
    async with async_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        plan = await plan_quarantine(session)
        await session.rollback()
    by_agent: Counter[str] = Counter()
    for item in plan:
        by_agent[item.agent_id] += 1
        destination = item.destination_path or "REFUSED: outside every scan root of the agent"
        print(f"  {item.agent_id} {item.reason} {item.size} bytes: {item.source_path} -> {destination}")
    print(f"summary: approved={len(plan)} " + " ".join(f"{agent}={count}" for agent, count in sorted(by_agent.items())))
    if not apply:
        print("DRY RUN: nothing moved. Re-run with --apply to dispatch these moves to their agents.")
        return 0
    async with async_session() as session:
        enqueued = await enqueue_quarantine(session, [item.review_id for item in plan])
    print(f"APPLIED: {enqueued} of {len(plan)} moves dispatched; each row turns quarantined or failed as its agent reports.")
    return 0 if enqueued == len(plan) else 1


async def _run_companion_links(*, apply: bool) -> int:
    """Run ``phaze backfill companion-links`` (phaze-spd83). Returns a process exit code.

    One session per agent: READ ONLY and rolled back on a dry run. With ``--apply`` the agent's run
    holds its association lock; an agent whose lock is taken (an automatic run in flight) is skipped
    and the command exits 1, so a re-run finishes it.
    """
    async with async_session() as session:
        agent_ids = list((await session.execute(select(Agent.id).order_by(Agent.id))).scalars())
    totals: Counter[str] = Counter()
    skipped = 0
    for agent_id in agent_ids:
        async with async_session() as session:
            if not apply:
                await session.execute(text("SET TRANSACTION READ ONLY"))
                association, detection = await run_agent_association(session, agent_id, apply=False)
                await session.rollback()
            else:
                async with agent_association_lock(session, agent_id) as acquired:
                    if not acquired:
                        skipped += 1
                        print(f"  {agent_id}: SKIPPED: an automatic association run holds this agent's lock; re-run to finish it")
                        continue
                    association, detection = await run_agent_association(session, agent_id, apply=True)
        totals.update(
            added=association.links_created, removed=association.links_removed, kept=association.links_kept, awaiting=association.awaiting_features
        )
        by_step = " ".join(f"{step}={count}" for step, count in sorted(association.decided.items())) or "none"
        junk = f"; junk review: new pending={sum(detection.created.values())} withdrawn={detection.withdrawn}" if detection else ""
        print(
            f"  {agent_id}: decided by step: {by_step}; links added={association.links_created} removed={association.links_removed} "
            f"kept={association.links_kept}; awaiting features={association.awaiting_features}{junk}"
        )
    print(f"summary: links added={totals['added']} removed={totals['removed']} kept={totals['kept']} awaiting features={totals['awaiting']}")
    if not apply:
        print("DRY RUN: nothing written; the junk-review refresh runs only with --apply. Re-run with --apply to write the links.")
        return 0
    print("APPLIED: links re-derived and the junk-review queue refreshed" + (f"; {skipped} agent(s) skipped, re-run to finish" if skipped else ""))
    return 1 if skipped else 0


async def _enqueue_companion_feature_pages(task_router: AgentTaskRouter, agent_id: str, page_size: int) -> tuple[int, int, str | None]:
    """Enqueue every pending page of one agent; returns ``(jobs, rows, first error or None)``."""
    jobs = rows = 0
    after = None
    while True:
        async with async_session() as session:
            page = await select_backfill_page(session, agent_id, after=after, limit=page_size)
        if not page:
            return jobs, rows, None
        payload = ExtractCompanionFeaturesPayload(
            agent_id=agent_id,
            targets=[CompanionFeaturesTarget(file_id=file_id, original_path=path) for file_id, path in page],
        )
        try:
            await task_router.enqueue_for_agent(agent_id=agent_id, task_name="extract_companion_features", payload=payload)
        except Exception as exc:
            return jobs, rows, str(exc)
        jobs += 1
        rows += len(page)
        last_id, last_path = page[-1]
        after = (last_path, last_id)


async def _run_reenqueue_incomplete_analyses() -> int:
    """Run ``phaze backfill reenqueue-incomplete-analyses`` (phaze-kj8dl). Returns a process exit code.

    Wires a database session + ``AgentTaskRouter`` the same way ``main.py``'s FastAPI lifespan does
    (``process_file`` is an agent task, never a controller task, so the bare ``SimpleNamespace``
    ``app_state`` needs only ``.task_router``), prints an auditable report, and returns
    :func:`~phaze.services.reanalysis_backfill.compute_exit_code`'s verdict. All selection/routing
    logic lives in ``phaze.services.reanalysis_backfill`` -- see its module docstring for the full
    rationale, and ``docs/runbook.md``'s phaze-kj8dl section for the deployment ordering this command
    is step 4 of.
    """
    settings = get_settings()
    task_router = AgentTaskRouter(queue_url=settings.queue_url, cache_redis_url=settings.redis_url, ledger_sessionmaker=async_session)
    app_state = SimpleNamespace(task_router=task_router)
    try:
        async with async_session() as session:
            legacy_null_count = await count_null_windows_columns_rows(session)
            print(
                f"{legacy_null_count} pre-Phase-43 legacy row(s) with all windows columns NULL -- "
                "deliberately SKIPPED (already exhaustive before caps existed; see "
                "phaze.services.reanalysis_backfill module docstring for the recorded decision)"
            )

            moved_count = await count_applied_incomplete_analyses_rows(session)
            print(
                f"{moved_count} file(s) with incomplete prior coverage already executed/moved -- "
                "deliberately SKIPPED (re-analysis at the recorded path would fail; see "
                "phaze.services.reanalysis_backfill module docstring for the recorded decision)"
            )

            def _print_outcome(outcome: ReanalysisOutcome) -> None:
                # Printed AS EACH OUTCOME HAPPENS (not batched until the run returns) so a partial
                # run still leaves a durable, readable audit trail if something interrupts it.
                print(f"  {outcome.file_id}  {outcome.outcome:<18}  {outcome.original_path}", flush=True)

            outcomes = await enqueue_incomplete_reanalysis(session, app_state, on_outcome=_print_outcome)
            print(f"{len(outcomes)} file(s) with incomplete prior analysis coverage routed")

            tally = Counter(outcome.outcome for outcome in outcomes)
            print("summary: " + " ".join(f"{key}={tally.get(key, 0)}" for key in sorted(tally)))
    finally:
        await task_router.close()
    return compute_exit_code(outcomes)


def _main_agents_add(args: argparse.Namespace) -> int:
    """Handle ``phaze agents add``. Returns a process exit code."""
    agent_id: str = args.agent_id
    name: str = args.name if args.name is not None else agent_id.replace("-", " ").title()
    kind: str = args.kind
    scan_roots: list[str] = [part.strip() for part in args.scan_roots.split(",") if part.strip()]

    # Validate BEFORE any DB access so an invalid id never opens a session.
    # A compute agent owns no media and no scan roots, so the absolute-path
    # requirement is enforced ONLY for fileserver agents; a fileserver
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
