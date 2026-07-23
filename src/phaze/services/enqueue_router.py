"""Shared enqueue-routing foundation (Phase 30 Plan 01).

Single source of truth that maps every control-plane task name to the SAQ queue an
actual worker consumes, so no API code path can enqueue onto the consumer-less
unnamed ``default`` queue (the v4.0.6 incident: 11,428 ``process_file`` jobs
stranded in ``saq:job:default:*``).

Two destinations:

- The named ``controller`` queue (``app.state.controller_queue``), consumed by the
  application-server ``phaze-worker`` (``phaze.tasks.controller.settings``).
- A per-agent ``phaze-agent-<id>`` queue (``AgentTaskRouter.queue_for(agent_id)``),
  consumed by a file-server ``phaze-agent-worker``
  (``phaze.tasks.agent_worker.settings``).

``CONTROLLER_TASKS`` and ``AGENT_TASKS`` MUST stay in sync with the ``functions``
lists registered in ``phaze.tasks.controller`` and ``phaze.tasks.agent_worker``
respectively. A task missing from both sets is unroutable and
:func:`resolve_queue_for_task` raises ``ValueError`` (fail loud, never silently
default).

Per-lane routing (quick-260707-dh1): agent tasks are further partitioned into four
lanes (``analyze`` / ``fingerprint`` / ``meta`` / ``io``) by :data:`LANE_TASKS`,
the SINGLE source of truth for task->lane membership. ``AGENT_TASKS`` is the derived
union of every lane's frozenset, so existing membership checks are unchanged.
:func:`lane_for_task` is the reverse lookup every agent-queue producer MUST call to
resolve its lane (it raises ``ValueError`` for any non-agent / unmapped name -- the
fail-loud guard that keeps a producer from ever building an un-suffixed / bad queue
name and re-stranding jobs, Phase-30 class). Both the producer (this module) and the
consumer (``phaze.tasks.agent_worker`` lane worker settings) derive from
``LANE_TASKS``, mirroring the "MUST mirror" contract between ``AGENT_TASKS`` and the
agent worker's registered ``functions``.

Active-agent selection policy: :func:`select_active_agent` returns the
most-recently-seen non-revoked agent (``revoked_at IS NULL`` AND
``last_seen_at IS NOT NULL``, ORDER BY ``last_seen_at DESC`` LIMIT 1). This is the
simplest deterministic rule; round-robin / least-loaded dispatch is deferred. The
``revoked_at IS NULL`` predicate excludes the permanently-revoked
``legacy-application-server`` (its ``revoked_at`` equals its ``created_at``).

Ownership affinity (phaze-c9w9): the most-recently-seen rule is a LIVENESS pick, not a
placement policy. Every file-keyed agent task (``process_file`` / ``extract_file_metadata`` /
``fingerprint_file`` / ``scan_live_set``) runs against a path on ONE specific fileserver's
media mount -- the agent recorded on ``FileRecord.agent_id`` (the composite unique key
``(agent_id, original_path)`` explicitly models the same path existing under two different
agents as two different files). With two live fileservers the most-recently-seen winner flaps
with heartbeat timing, landing agent A's files on agent B, where the path either does not
exist (spurious -- for analyze, TERMINAL -- failure rows) or names B's DIFFERENT bytes (silent
cross-agent result corruption). File-keyed producers therefore pass the owning ``agent_id``
into :func:`resolve_queue_for_task` (bulk producers group via
:func:`resolve_queues_for_owned_files`); :func:`select_active_agent` remains only for tasks
with no owning file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from sqlalchemy import select
import structlog

from phaze.models.agent import Agent


if TYPE_CHECKING:
    from collections.abc import Sequence

    from saq import Queue
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


CONTROLLER_TASKS: frozenset[str] = frozenset(
    {
        "generate_proposals",
        "search_tracklist",
        "scrape_and_store_tracklist",
        "match_tracklist_to_discogs",
        "refresh_tracklists",
        "submit_cloud_job",  # Phase 54: fast kube-submit producer (control-plane; kube creds live here)
    }
)
"""Fileless tasks the application-server controller worker consumes.

MUST mirror ``phaze.tasks.controller.settings["functions"]`` (+ the
``refresh_tracklists`` cron). ``reap_stalled_scans`` is cron-only (never
operator-enqueued) so it is intentionally omitted from the routable set.
"""

LANE_TASKS: dict[str, frozenset[str]] = {
    # CPU-bound in-process essentia analysis (host CPU budget).
    "analyze": frozenset({"process_file"}),
    # CPU-bound via the panako/audfprint sidecars (same finite core budget).
    "fingerprint": frozenset({"fingerprint_file"}),
    # Light / fast control-and-metadata tasks.
    "meta": frozenset(
        {
            "extract_file_metadata",
            "scan_directory",
            "scan_live_set",
            "execute_approved_batch",
        }
    ),
    # Network-bound offload (off the CPU budget).
    "io": frozenset(
        {
            "s3_upload",  # Phase 53: agent httpx multipart-PUT upload to presigned S3 URLs (KSTAGE-02)
            "push_file",  # Phase 50: fileserver rsync-over-SSH push to the compute scratch dir
        }
    ),
}
"""Canonical task->lane partition (quick-260707-dh1). The ONE place task->lane
membership lives; the producer (:func:`lane_for_task` / :func:`resolve_queue_for_task`)
and the consumer (``phaze.tasks.agent_worker`` lane worker settings) both derive from
it. Every agent task appears in EXACTLY one lane (totality asserted in tests).

Add a lane -> add its frozenset here; add a task -> put it in exactly one lane.
"""

LANES: tuple[str, ...] = tuple(LANE_TASKS)
"""Ordered lane names (``analyze``, ``fingerprint``, ``meta``, ``io``) -- the insertion
order of :data:`LANE_TASKS`. ``all_lane_queues`` iterates this so depth readers and the
compose split enumerate lanes deterministically."""

AGENT_TASKS: frozenset[str] = frozenset().union(*LANE_TASKS.values())
"""File-touching tasks a file-server agent worker consumes -- the DERIVED union of every
:data:`LANE_TASKS` lane (single source of truth). Kept as a flat frozenset so existing
membership checks are unchanged.

MUST mirror ``phaze.tasks.agent_worker.settings["functions"]`` (excluding the
``heartbeat_tick`` cron, which agents schedule for themselves and operators never
dispatch).
"""

# Reverse index built once at import: task name -> its lane. LANE_TASKS totality
# guarantees each agent task maps to exactly one lane.
_TASK_TO_LANE: dict[str, str] = {task: lane for lane, tasks in LANE_TASKS.items() for task in tasks}


def lane_for_task(task_name: str) -> str:
    """Return the lane an agent task routes to, or raise ``ValueError`` (fail loud).

    The guard every agent-queue producer MUST call before building a queue: a name that
    is not in exactly one :data:`LANE_TASKS` lane (a controller task, a cron-only name, a
    typo) raises rather than silently defaulting -- the same fail-loud posture as
    :func:`resolve_queue_for_task`'s unroutable branch, and the invariant that keeps a
    producer from ever stranding a job on an un-consumed / bad-suffixed queue (Phase-30).
    """
    lane = _TASK_TO_LANE.get(task_name)
    if lane is None:
        msg = f"no agent lane for task: {task_name} (not an agent task, or unmapped in LANE_TASKS)"
        raise ValueError(msg)
    return lane


class NoActiveAgentError(RuntimeError):
    """Raised when no eligible (non-revoked, recently-seen) agent exists."""


class RoutedQueue(NamedTuple):
    """The resolved destination for an enqueue: a queue + the selected agent.

    ``agent_id`` is ``None`` for controller tasks (the controller queue is not
    agent-scoped) and the chosen agent's id for per-agent tasks.
    """

    queue: Queue
    agent_id: str | None


async def select_active_agent(session: AsyncSession, kind: str | None = None) -> Agent:
    """Return the most-recently-seen non-revoked agent.

    Filters ``revoked_at IS NULL`` AND ``last_seen_at IS NOT NULL``, orders by
    ``last_seen_at DESC`` and takes the first row. The ``revoked_at IS NULL``
    predicate excludes ``legacy-application-server`` (permanently revoked).

    Phase 49 (D-13): when ``kind`` is given (``"compute"`` / ``"fileserver"``) the
    selection is scoped to agents of that ``Agent.kind`` — the deterministic
    most-recently-seen rule still holds, but only within the requested kind.
    ``kind=None`` preserves the original behavior (any kind), so every existing
    caller (e.g. :func:`resolve_queue_for_task`) is unchanged.

    Raises :class:`NoActiveAgentError` when no agent satisfies the filter — the
    consuming sites surface a clear error/empty-state instead of a silent success.
    """
    stmt = (
        select(Agent)
        .where(
            Agent.revoked_at.is_(None),
            Agent.last_seen_at.is_not(None),
        )
        .order_by(Agent.last_seen_at.desc())
        .limit(1)
    )
    if kind is not None:
        stmt = stmt.where(Agent.kind == kind)
    result = await session.execute(stmt)
    agent = result.scalar_one_or_none()
    if agent is None:
        msg = "no active agent available (all agents are revoked or have never checked in)"
        raise NoActiveAgentError(msg)
    return agent


async def select_agent_by_id(session: AsyncSession, agent_id: str, *, kind: str | None = None) -> Agent:
    """Return the specifically-bound agent whose ``Agent.id == agent_id`` iff it is live.

    The per-entry-binding sibling of :func:`select_active_agent` (Phase 72, MCOMP-01 / D-01): it
    reuses the SAME liveness filter (``revoked_at IS NULL`` AND ``last_seen_at IS NOT NULL``) and the
    optional ``kind`` scope, but keys on ``Agent.id == agent_id`` instead of ordering by
    ``last_seen_at`` — a compute backend resolves to ITS bound agent (``config.agent_ref``), not "the
    single active compute agent" (the retired single-active pick).

    Matches on ``Agent.id`` ONLY — the constrained slug PK / FK target — never on the free-form,
    collidable ``Agent.name`` (D-01, no id-or-name fallback), so a spoof-shaped name can never be
    selected. The query is parameterized, so ``agent_id`` cannot inject SQL.

    Raises :class:`NoActiveAgentError` when no row matches — an absent / unregistered / revoked /
    never-seen / wrong-kind bound agent — which the compute ``is_available`` gate consumes as the
    degrade-to-hold signal (D-05): ``False``, never a raise out to the drain/cron.
    """
    stmt = select(Agent).where(
        Agent.id == agent_id,
        Agent.revoked_at.is_(None),
        Agent.last_seen_at.is_not(None),
    )
    if kind is not None:
        stmt = stmt.where(Agent.kind == kind)
    result = await session.execute(stmt)
    agent = result.scalar_one_or_none()
    if agent is None:
        msg = f"no active agent {agent_id!r} available (absent, revoked, never checked in, or wrong kind)"
        raise NoActiveAgentError(msg)
    return agent


async def resolve_queue_for_task(
    task_name: str,
    app_state: Any,
    session: AsyncSession | None,
    *,
    agent_id: str | None = None,
) -> RoutedQueue:
    """Resolve ``task_name`` to the queue a real worker consumes.

    - ``task_name in CONTROLLER_TASKS`` -> ``app_state.controller_queue`` (agent_id None).
      ``agent_id`` must be ``None`` (the controller queue is not agent-scoped; fail loud).
    - ``task_name in AGENT_TASKS`` -> the per-agent queue. Requires ``session`` (raises
      ``ValueError`` if ``None``). With ``agent_id`` given (phaze-c9w9: a file-keyed task,
      passing the FILE's owning ``FileRecord.agent_id``) the destination is THAT agent iff
      it is a live fileserver (:func:`select_agent_by_id`); without it, the most-recently-seen
      live fileserver (:func:`select_active_agent`) -- for agent tasks with no owning file.
      Both raise :class:`NoActiveAgentError` when the target is not live.
    - anything else -> ``ValueError`` (fail loud; never returns the default queue).
    """
    if task_name in CONTROLLER_TASKS:
        if agent_id is not None:
            msg = f"controller task {task_name!r} is not agent-scoped; agent_id must be None"
            raise ValueError(msg)
        queue = app_state.controller_queue
        # Phase 36: open the PostgresQueue broker pool (built open=False) before the caller
        # enqueues. connect() is idempotent (guarded by self._connected) -- a no-op after the
        # first call. Single chokepoint so every routed.queue.enqueue(...) site (and the
        # background tasks that receive routed.queue) finds an open pool.
        await queue.connect()
        return RoutedQueue(queue, None)
    if task_name in AGENT_TASKS:
        if session is None:
            msg = f"resolving per-agent task {task_name!r} requires a database session"
            raise ValueError(msg)
        # phaze-5r8f: scope the pick to the FILESERVER kind. Every task routed through this branch
        # (scan_live_set, process_file, extract_file_metadata, fingerprint_file) runs against the
        # fileserver's local media mount, so it MUST land on a fileserver agent. Phase 48/49 compute
        # agents heartbeat through the same endpoint and run the same worker module, so an unscoped
        # pick (any kind, most-recently-seen) could route a fileserver-local task to a media-less
        # compute agent where the path does not exist -- an intermittent failure gated on heartbeat
        # timing. Compute agents are only ever addressed explicitly via agent_ref / queue_for.
        #
        # phaze-c9w9: when the caller names the file's owning agent, the destination is THAT agent
        # or nothing -- never "some other live fileserver" (whose mount lacks the path, or worse,
        # holds a DIFFERENT file at the same path). select_agent_by_id keeps the identical liveness
        # filter, so an offline/revoked owner raises NoActiveAgentError instead of misrouting.
        if agent_id is not None:
            agent = await select_agent_by_id(session, agent_id, kind="fileserver")
        else:
            agent = await select_active_agent(session, kind="fileserver")
        # quick-260707-dh1: route to the task's LANE queue (phaze-agent-<id>-<lane>), never the
        # bare base. lane_for_task raises for an unmapped name -- but task_name is in AGENT_TASKS
        # here, so it always resolves.
        lane = lane_for_task(task_name)
        queue = app_state.task_router.queue_for(agent.id, lane)
        await queue.connect()
        return RoutedQueue(queue, agent.id)
    msg = f"unroutable task: {task_name}"
    raise ValueError(msg)


class OwnedFile(Protocol):
    """Structural shape of a row that carries an owning ``agent_id`` (``FileRecord``)."""

    @property
    def agent_id(self) -> str: ...


async def resolve_queues_for_owned_files[FileT: OwnedFile](
    task_name: str,
    app_state: Any,
    session: AsyncSession,
    files: Sequence[FileT],
) -> tuple[list[tuple[RoutedQueue, list[FileT]]], list[FileT]]:
    """Group ``files`` by owning ``agent_id`` and resolve each owner's lane queue (phaze-c9w9).

    The bulk-producer companion to :func:`resolve_queue_for_task`'s per-file ``agent_id`` form:
    every bulk trigger used to resolve ONE most-recently-seen fileserver and land its entire
    pending set there, silently misrouting files owned by any other agent. Here each distinct
    owner is resolved independently (same liveness rule as :func:`select_agent_by_id`,
    ``kind="fileserver"``), preserving encounter order within each group.

    Returns ``(routed_groups, skipped)``:

    - ``routed_groups`` -- one ``(RoutedQueue, files)`` pair per LIVE owning agent.
    - ``skipped`` -- files whose owner is offline/revoked/unknown. They are NEVER rerouted to
      another agent (the misroute this exists to prevent); the caller reports/logs them and the
      operator retries once the owner is back.

    Raises :class:`NoActiveAgentError` when ``files`` is non-empty and NO owner is live, so
    callers keep their existing "no active agent" empty-state on the total-outage path.
    """
    groups: dict[str, list[FileT]] = {}
    for f in files:
        groups.setdefault(f.agent_id, []).append(f)

    routed_groups: list[tuple[RoutedQueue, list[FileT]]] = []
    skipped: list[FileT] = []
    for owner_id, owned in groups.items():
        try:
            routed = await resolve_queue_for_task(task_name, app_state, session, agent_id=owner_id)
        except NoActiveAgentError:
            logger.warning(
                "owning agent offline -- files skipped, not rerouted",
                task=task_name,
                agent_id=owner_id,
                skipped=len(owned),
            )
            skipped.extend(owned)
            continue
        routed_groups.append((routed, owned))
    if files and not routed_groups:
        msg = f"no owning fileserver agent is live for any of the {len(files)} file(s) pending {task_name!r}"
        raise NoActiveAgentError(msg)
    return routed_groups, skipped
