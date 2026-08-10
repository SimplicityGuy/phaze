"""Agent liveness classification (Phase 29 D-12 + UI-SPEC §Status Pill Component).

``classify``/``sort_key`` are pure functions — no DB, no I/O. The router
(``phaze.routers.admin_agents``) calls ``classify(agent, now)`` for every row and
injects the result on a transient ``agent._status`` attribute, then sorts the list
with ``sort_key(agent, now)`` before rendering. Tests and renderer share a single
source of truth via ``phaze.constants.AGENT_LIVENESS_*`` thresholds.

``derive_compute_lane_identities(session)`` (RECORD-03 / D-07 → COMPUTE-01) is the one
DB-touching read here — a degrade-safe, read-only ``CloudJob`` aggregation that models each
ephemeral compute cluster (one per non-local registry backend) as an Active/Waiting/Idle
Job-based identity (NEVER a perpetually-DEAD agent). It mirrors the ``try/except → default``
count discipline in ``phaze.services.pipeline`` and lives beside ``classify`` because both
answer the same operator question ("what's alive right now?") for the two-section Agents page.

Status precedence (D-12 LOCKED):

    1. ``revoked``  — ``agent.revoked_at IS NOT NULL`` (takes precedence over
                      all ``last_seen_at`` math).
    2. ``never``    — ``revoked_at IS NULL AND last_seen_at IS NULL``.
    3. ``alive``    — ``now - last_seen_at < AGENT_LIVENESS_ALIVE_SECONDS`` (90s).
    4. ``stale``    — ``AGENT_LIVENESS_ALIVE_SECONDS <= delta
                       < AGENT_LIVENESS_STALE_SECONDS`` (90..300s).
    5. ``dead``     — ``delta >= AGENT_LIVENESS_STALE_SECONDS`` (>=300s).

Sort key (UI-SPEC LOCKED):

    ``(revoked_int, status_rank, -last_seen_unix_or_-inf)``

    - revoked agents land AFTER every non-revoked agent;
    - within non-revoked: ``alive (0) → stale (1) → dead (2) → never (3)``;
    - within the same status bucket: ``last_seen_at`` DESCENDING (more recent
      first) via the negated unix-timestamp tiebreaker.

Import-boundary note: importing ``phaze.models.agent`` IS allowed here. The
Postgres-free invariant applies only to ``phaze.cert_bootstrap``,
``phaze.entrypoint``, ``phaze.tasks.agent_worker``, and ``phaze.tasks._shared.*``
— NOT to ``phaze.services.*``.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
import structlog

from phaze.config import get_settings
from phaze.constants import AGENT_LIVENESS_ALIVE_SECONDS, AGENT_LIVENESS_STALE_SECONDS
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord


if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.models.agent import Agent


logger = structlog.get_logger(__name__)


AgentStatus = Literal["alive", "stale", "dead", "revoked", "never"]
"""5 LOCKED status values per UI-SPEC §Status Pill Component (Phase 29 D-12)."""

_STATUS_RANK: dict[AgentStatus, int] = {
    "alive": 0,
    "stale": 1,
    "dead": 2,
    "revoked": 3,
    "never": 3,
}
"""Sort-rank inside the non-revoked group: alive=0 → stale=1 → dead=2 → never=3.

'revoked' has rank 3 too but is dominated by the leading 'revoked_int' tier in
``sort_key`` — so its rank never decides ordering against non-revoked agents.
'never' shares rank 3 with 'revoked' because both represent "no signal", but
'never' agents stay in the non-revoked group so they appear above any revoked
row in the rendered table.
"""


def classify(agent: Agent, now: datetime) -> AgentStatus:
    """Return the 5-state liveness label for ``agent`` evaluated at ``now``.

    Precedence (D-12 LOCKED): revoked → never → alive/stale/dead by threshold.

    The ``now`` parameter is explicit (not ``datetime.now()`` inside the body)
    so tests are time-deterministic without freezegun. Mirrors the
    ``elapsed_seconds(batch)`` shape in ``phaze.routers.pipeline_scans``.
    """
    if agent.revoked_at is not None:
        return "revoked"
    if agent.last_seen_at is None:
        return "never"
    delta_seconds = (now - agent.last_seen_at).total_seconds()
    if delta_seconds < AGENT_LIVENESS_ALIVE_SECONDS:
        return "alive"
    if delta_seconds < AGENT_LIVENESS_STALE_SECONDS:
        return "stale"
    return "dead"


def sort_key(agent: Agent, now: datetime) -> tuple[int, int, float]:
    """Return the sort tuple for ``agent`` at ``now`` (UI-SPEC LOCKED order).

    Tuple shape: ``(revoked_int, status_rank, -last_seen_unix_or_-inf)``.

    - ``revoked_int`` is 1 for revoked agents, 0 otherwise. Sorted ascending,
      so non-revoked agents (0) come before revoked agents (1).
    - ``status_rank`` is the entry in ``_STATUS_RANK`` for ``classify(agent, now)``.
      Sorted ascending so 'alive' (0) → 'stale' (1) → 'dead' (2) → 'never' (3).
    - The tiebreaker is the NEGATED unix timestamp of ``last_seen_at`` so
      more-recently-seen agents sort first. Agents with ``last_seen_at IS NULL``
      tie at ``-inf`` (negation of ``+inf``) — they land at the END of their
      bucket, which only matters for the 'never' bucket (revoked agents with
      NULL last_seen still get the float fallback but never compete inside the
      non-revoked group).
    """
    revoked_int = 1 if agent.revoked_at is not None else 0
    status = classify(agent, now)
    status_rank = _STATUS_RANK[status]
    # Agents with last_seen_at IS NULL land at the END of their bucket via +inf
    # (negation of -inf would be ambiguous; +inf is the largest finite-or-inf
    # value so ascending sort puts these rows last within the bucket). Only
    # the 'never' bucket actually exercises this path inside the non-revoked
    # group.
    neg_last_seen = math.inf if agent.last_seen_at is None else -agent.last_seen_at.timestamp()
    return (revoked_int, status_rank, neg_last_seen)


ComputeLaneState = Literal["ACTIVE", "WAITING", "IDLE"]
"""3-state liveness for the k8s burst lane (RECORD-03 / D-07). DEAD is NEVER a member.

The Kubernetes burst lane is modeled as an ephemeral, Job-based identity — NOT a
heartbeating agent — so it can never be "perpetually DEAD". Its liveness is derived
live from in-flight ``CloudJob`` counts and degrades to ``IDLE`` (never DEAD/red) on
any DB error (KDEPLOY-04).
"""


@dataclass(frozen=True)
class ComputeLane:
    """One derived compute-lane identity for the two-section Agents page (COMPUTE-01).

    A per-cluster liveness identity composed from the Phase-67 backend registry (one lane per
    NON-local entry) and the live in-flight ``CloudJob`` counts attributed to that backend. A lane
    is NEVER a heartbeating agent — its ``state`` is derived purely from in-flight work (``running`` /
    ``waiting``), so a configured-but-quiet cluster is ``IDLE`` (listed, never DEAD/red) and a DB
    hiccup degrades every lane to ``IDLE`` rather than raising into the hot poll (KDEPLOY-04).

    ``running``/``waiting`` answer the ADMISSION question this page renders (the ACTIVE/WAITING/IDLE
    pill + the "N waiting on quota" caption): ``running`` is a bare ``status == RUNNING`` count,
    ``waiting`` is ``SUBMITTED AND inadmissible`` -- a QUOTA-FAULT signal, deliberately narrower than
    "queued". ``queued``/``working`` (phaze-5c6i2) answer a DIFFERENT question -- the operator's
    lane-card queued/working split -- via the SAME phaze-zyoag staged/analyzing seam
    ``services.backends._cloud_lane_queued_working`` uses for the ``/s/analyze`` lane cards, so the two
    pages can never render CONTRADICTORY queued/working figures for one lane (acceptance rule 9): both
    are now one derivation, read twice. Do NOT repoint ``running``/``waiting`` at this seam -- it would
    silently change what "waiting on quota" means (a fault caption, not a queue depth) for a page this
    bead does not otherwise touch (phaze-rdxfu scoped ``/s/analyze`` and ``/admin/agents`` apart on
    purpose; see that bead's commit message).
    """

    backend_id: str
    kind: str
    state: ComputeLaneState
    running: int
    waiting: int
    queued: int
    working: int


def _lane_state(running: int, waiting: int) -> ComputeLaneState:
    """Return the 3-state lane liveness by precedence: running≥1 → ACTIVE, waiting≥1 → WAITING, else IDLE.

    DEAD is structurally impossible here (KDEPLOY-04): a compute lane is an ephemeral Job-based
    identity, so quiescence is ``IDLE`` (green/neutral), never a perpetually-DEAD pill.
    """
    if running >= 1:
        return "ACTIVE"
    if waiting >= 1:
        return "WAITING"
    return "IDLE"


def non_local_backend_kinds(settings: ControlSettings) -> dict[str, str]:
    """Return ``{backend_id: kind}`` for every registry entry whose ``kind != "local"`` (COMPUTE-01).

    A pure, session-free projection of the Phase-67 registry (``settings.backends``) — the shared
    helper the per-cluster lane derivation here and the later header-count / file-badge beads all
    consume so "which backends are cloud lanes?" is answered in exactly one place. Insertion order
    mirrors ``settings.backends`` so downstream lane ordering is registry-deterministic.
    """
    return {backend.id: backend.kind for backend in settings.backends if backend.kind != "local"}


async def derive_compute_lane_identities(session: AsyncSession) -> list[ComputeLane]:
    """Return one :class:`ComputeLane` per non-local registry backend + a trailing unattributed lane (COMPUTE-01).

    Composes the Phase-67 registry (``get_settings().backends``, non-local entries) with a SINGLE
    grouped ``CloudJob`` read (``GROUP BY backend_id`` with filtered counts — ``RUNNING`` → running,
    ``SUBMITTED AND inadmissible`` → waiting, plus the phaze-5c6i2 ``queued``/``working`` pair below),
    mirroring the ``_admission_by_backend_id`` idiom in ``services.backends``. Every configured cluster
    appears even when IDLE (0 counts); liveness is in-flight WORK, never a reachability probe. In-flight
    rows with a NULL ``backend_id`` collapse into ONE trailing ``"unattributed"``/``kind="cloud"`` lane,
    emitted only when its counts are non-zero.

    phaze-5c6i2 (acceptance rule 9): ``queued``/``working`` are ADDED to the SAME grouped read via
    ``phaze.services.pipeline._cloud_window_clauses`` (the phaze-zyoag staged/analyzing seam) so this
    page and the ``/s/analyze`` lane cards (``services.backends._cloud_lane_queued_working``) derive
    their queued/working figures from the identical predicate — one derivation, read twice, never two
    independent ones that can drift (see :class:`ComputeLane`'s docstring for why ``running``/``waiting``
    themselves stay untouched). The import is deferred to call time: ``pipeline`` imports
    ``non_local_backend_kinds`` from THIS module at module scope, so importing ``pipeline`` back from
    here at module scope would cycle (mirrors ``_cloud_window_clauses``'s own deferred import of
    ``services.backends``, for the identical reason in the other direction).

    Degrade-safe (KDEPLOY-04): the ``CloudJob`` read runs inside a SAVEPOINT (``begin_nested``) so a
    :class:`~sqlalchemy.exc.SQLAlchemyError` rolls back the NESTED scope ALONE and returns the registry
    lanes all-``IDLE`` (a DB hiccup must NEVER paint a lane DEAD/red) WITHOUT expiring the caller's
    already-loaded ``Agent`` rows — both ``admin_agents`` routes call ``_load_agents(session)`` on this
    SAME session before deriving lanes, so a plain ``session.rollback()`` here would expire those rows
    and 500 the template render on the next lazy load (CR-01 / D-00b). A settings/registry read failure
    returns ``[]``. This must never raise on the hot poll path -- unlike the lane cards' own
    ``queued``/``working`` (which degrade to ``None``, an explicit unknown), THIS page's pre-existing
    contract degrades every ``ComputeLane`` field to 0/IDLE, so ``queued``/``working`` here follow that
    SAME established 0-degrade discipline rather than introducing a second one on one page.
    """
    from phaze.services.pipeline import _cloud_window_clauses  # noqa: PLC0415 -- breaks a module-load import cycle, see docstring

    try:
        kinds = non_local_backend_kinds(cast("ControlSettings", get_settings()))
    except Exception:
        logger.warning("compute_lane_identity_registry_unavailable", exc_info=True)
        return []

    try:
        staged, analyzing = _cloud_window_clauses()
    except Exception:
        logger.warning("compute_lane_identity_window_clauses_degraded", exc_info=True)
        return [
            ComputeLane(backend_id=backend_id, kind=kind, state="IDLE", running=0, waiting=0, queued=0, working=0)
            for backend_id, kind in kinds.items()
        ]

    try:
        # SAVEPOINT degrade (CR-01 / D-00b): roll back the NESTED scope alone on error so the aborted
        # transaction recovers WITHOUT expiring the caller's already-loaded Agent rows (a plain
        # ``session.rollback()`` would expire them and 500 the admin_agents render on the next lazy load).
        async with session.begin_nested():
            stmt = select(
                CloudJob.backend_id,
                func.count().filter(CloudJob.status == CloudJobStatus.RUNNING.value).label("running"),
                func.count().filter(CloudJob.status == CloudJobStatus.SUBMITTED.value, CloudJob.inadmissible.is_(True)).label("waiting"),
                func.count().filter(staged).label("queued"),
                func.count().filter(analyzing).label("working"),
            ).group_by(CloudJob.backend_id)
            rows = (await session.execute(stmt)).all()
    except SQLAlchemyError:
        logger.warning("compute_lane_identity_degraded", exc_info=True)
        return [
            ComputeLane(backend_id=backend_id, kind=kind, state="IDLE", running=0, waiting=0, queued=0, working=0)
            for backend_id, kind in kinds.items()
        ]

    counts = {
        backend_id: (int(running or 0), int(waiting or 0), int(queued or 0), int(working or 0))
        for backend_id, running, waiting, queued, working in rows
    }

    lanes: list[ComputeLane] = []
    for backend_id, kind in kinds.items():
        running, waiting, queued, working = counts.get(backend_id, (0, 0, 0, 0))
        lanes.append(
            ComputeLane(
                backend_id=backend_id,
                kind=kind,
                state=_lane_state(running, waiting),
                running=running,
                waiting=waiting,
                queued=queued,
                working=working,
            )
        )

    null_running, null_waiting, null_queued, null_working = counts.get(None, (0, 0, 0, 0))
    if null_running or null_waiting or null_queued or null_working:
        lanes.append(
            ComputeLane(
                backend_id=UNATTRIBUTED_LANE_ID,
                kind="cloud",
                state=_lane_state(null_running, null_waiting),
                running=null_running,
                waiting=null_waiting,
                queued=null_queued,
                working=null_working,
            )
        )

    return lanes


# --- phaze-2u8v.5: burst-lane workload drill-down --------------------------------------------
#
# The compute/burst-lane panel (Section 2 of /admin/agents) used to report only a bare count
# ("3 workloads · 3 running") with no way to see WHAT was running -- an operator could see that
# work existed but not act on it. This is the degrade-safe read behind that drill-down.

UNATTRIBUTED_LANE_ID = "unattributed"
"""The synthetic backend id for in-flight ``CloudJob`` rows with a NULL ``backend_id`` (pre-
attribution rows, or rows from a since-removed registry backend). Shared by
:func:`derive_compute_lane_identities` (which emits the trailing lane under this id) and
:func:`get_compute_lane_running_jobs` (which must query the SAME NULL-``backend_id`` rows when
asked to drill into it)."""

COMPUTE_LANE_RUNNING_N = 50
"""Bounded cap on the drill-down's running-workload list (mirrors ``services.backends.LANE_RECENT_N``'s
shape) -- predictable render cost under any throughput. A lane genuinely running more files than this
at once is far beyond any single compute backend's configured ``cap`` in practice."""


async def get_compute_lane_running_jobs(session: AsyncSession, backend_id: str, limit: int = COMPUTE_LANE_RUNNING_N) -> list[dict[str, Any]]:
    """Return up to ``limit`` in-flight (RUNNING) workloads for a compute/burst lane, each identifying its file.

    One row per ``RUNNING`` :class:`CloudJob` attributed to ``backend_id``, joined to its
    :class:`FileRecord` for the display filename (``COALESCE(original_filename_repaired,
    original_filename)`` -- the same expression :mod:`phaze.services.search_queries` uses). Newest-
    first by ``updated_at``, with ``CloudJob.id`` as a tiebreaker for a total order (mirrors
    :func:`phaze.services.backends.get_lane_recent_completions`).

    ``backend_id == UNATTRIBUTED_LANE_ID`` queries ``CloudJob.backend_id IS NULL`` instead of
    equality -- the same NULL collapse :func:`derive_compute_lane_identities` performs for its
    trailing lane.

    Degrade-safe (KDEPLOY-04 sibling): any query error rolls back and returns ``[]`` rather than
    raising into the hot 5s tick -- an operator drilling into a lane during a DB hiccup sees an
    empty list, never a 500. Secret-free: only the CloudJob/FileRecord scalars needed to identify
    the file leave here.
    """
    try:
        display_filename = func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename)
        stmt = (
            select(CloudJob.file_id, display_filename.label("filename"), CloudJob.kueue_workload, CloudJob.updated_at)
            .join(FileRecord, FileRecord.id == CloudJob.file_id)
            .where(CloudJob.status == CloudJobStatus.RUNNING.value)
            .order_by(CloudJob.updated_at.desc(), CloudJob.id.desc())
            .limit(limit)
        )
        stmt = stmt.where(CloudJob.backend_id.is_(None)) if backend_id == UNATTRIBUTED_LANE_ID else stmt.where(CloudJob.backend_id == backend_id)
        rows = (await session.execute(stmt)).all()
    except SQLAlchemyError:
        logger.warning("compute_lane_running_jobs_degraded", backend_id=backend_id, exc_info=True)
        try:
            await session.rollback()
        except SQLAlchemyError:
            logger.warning("compute_lane_running_jobs_rollback_failed", backend_id=backend_id, exc_info=True)
        return []
    return [
        {"file_id": file_id, "filename": filename, "kueue_workload": kueue_workload, "updated_at": updated_at}
        for file_id, filename, kueue_workload, updated_at in rows
    ]
