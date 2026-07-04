"""The internal ``Backend`` protocol + its three re-homed implementations (Phase 68, BACK-01/03).

This is the phase's center of gravity. It houses one ``typing.Protocol`` (design §4.2 shape --
``is_available`` / ``in_flight_count`` / ``dispatch`` / ``reconcile``) and the three implementations
``LocalBackend`` / ``ComputeAgentBackend`` / ``KueueBackend`` that **re-home** the existing staging /
push / submit / reconcile bodies verbatim -- this is a behavior-preserving refactor, NOT a rewrite.

Phase 68 is a **lay-and-prove** phase (D-02): it defines the protocol + the uniform per-backend
``in_flight_count`` substrate and proves the equivalence invariant ``sum(in_flight_count(b)) ==
get_cloud_window_count()`` for the single-backend case, but it does NOT flip the drain onto per-backend
caps (that is Phase 69 / SCHED-02). This module is **purely additive** this phase -- the live drain
(``release_awaiting_cloud.stage_cloud_window``), the reconcile cron, and the config accessors are NOT
rewired here (Wave 3 / plan 68-04 owns that). The protocol methods are per-backend and unit-tested as
such.

Decisions realized here:

* **D-01a** -- the GATE-1 asymmetry lives in per-kind ``is_available``: compute REQUIRES a live compute
  agent; Kueue deliberately probes the cluster with NO compute-agent dependency; local is always up.
* **D-02** -- ``in_flight_count`` is the uniform ``cloud_job``-derived per-backend count; the equivalence
  invariant is the characterization proof that the new substrate matches the old count.
* **D-03** -- ``dispatch`` owns BOTH the ``FileState -> PUSHING`` flip AND the ``cloud_job`` upsert in the
  SAME caller-passed session, before/with the flip, NEVER after a separate commit (Pitfall 4 limbo guard).
* **D-05** -- ``KueueBackend`` calls today's single-cluster ``_stage_file_to_s3`` / ``kube_staging``
  VERBATIM (reads ``active_kube`` / ``active_bucket``); per-cluster parameterization is Phase 70.
* **D-07** -- the raise-on-``>1``-non-local guard is relocated into :func:`resolve_backends` (fail fast at
  boot); ``cloud_enabled`` stays in config as the registry on/off gate.
* **D-10** -- the in-flight status set is ``{UPLOADING, UPLOADED, SUBMITTED, RUNNING}``.

Cron no-op discipline (T-68-05): ``is_available`` / ``dispatch`` / ``reconcile`` degrade to a clean hold
(return ``False`` / a no-op) on an absent agent or a probe failure -- they never raise out to a cron.
Secret hygiene (T-68-04): this module logs only ``{id, kind, rank, cap}``-level fields, never a
``SecretStr`` / ``*_file`` value or a kube SA token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import func, select
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.config_backends import BackendConfig
    from phaze.models.file import FileRecord
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


# D-10 (Q3): the exact non-terminal / in-flight CloudJobStatus set in_flight_count counts. Terminal =
# {SUCCEEDED, FAILED}. Pinned against the live CloudJobStatus members in models/cloud_job.py.
IN_FLIGHT: tuple[CloudJobStatus, ...] = (
    CloudJobStatus.UPLOADING,
    CloudJobStatus.UPLOADED,
    CloudJobStatus.SUBMITTED,
    CloudJobStatus.RUNNING,
)


class Backend(Protocol):
    """The single internal dispatch seam that removes the ``if active_cloud_kind == …`` fork (§4.2).

    Structural (``typing.Protocol``): the three impls below conform by shape, no explicit subclassing.
    ``id`` / ``rank`` / ``cap`` mirror the Phase-67 registry submodel fields (cost-tier rank, concurrency
    cap); the four async methods are the per-backend dispatch lifecycle.
    """

    id: str
    rank: int
    cap: int

    async def is_available(self, session: AsyncSession) -> bool:
        """Whether this backend can accept a dispatch right now (compute: agent gate; kueue: cluster probe)."""
        ...

    async def in_flight_count(self, session: AsyncSession) -> int:
        """COUNT(cloud_job WHERE backend_id == self.id AND status IN {in-flight}) -- the D-02 substrate."""
        ...

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> None:
        """Flip ``file`` into the cloud window + write its ``cloud_job`` row, IN the caller's txn (D-03). Never commits."""
        ...

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> None:
        """Advance this backend's in-flight ``cloud_job`` rows toward terminal (kueue: cron read; local/compute: no-op)."""
        ...


class _BaseBackend:
    """Shared ``id`` / ``rank`` / ``cap`` carrier + the uniform ``cloud_job``-derived ``in_flight_count``.

    Each concrete backend binds to a single Phase-67 registry entry (``config``). The shared
    ``in_flight_count`` is the D-02/D-10 substrate: a pure DB COUNT filtered by ``backend_id`` + the
    in-flight status set (modelled on ``pipeline.get_cloud_window_count``).
    """

    def __init__(self, *, id: str, rank: int, cap: int, config: BackendConfig | None = None) -> None:
        self.id = id
        self.rank = rank
        self.cap = cap
        self.config = config

    async def in_flight_count(self, session: AsyncSession) -> int:
        """Return COUNT(cloud_job WHERE backend_id == self.id AND status IN {UPLOADING,UPLOADED,SUBMITTED,RUNNING})."""
        return int(
            (
                await session.execute(
                    select(func.count(CloudJob.id)).where(
                        CloudJob.backend_id == self.id,
                        CloudJob.status.in_([status.value for status in IN_FLIGHT]),
                    )
                )
            ).scalar()
            or 0
        )


class LocalBackend(_BaseBackend):
    """On-prem/all-local backend -- analysis runs on the fileserver agent via ``process_file`` (no cloud_job).

    ``is_available`` is unconditionally True (local dispatch needs no remote cloud agent);
    ``in_flight_count`` is always 0 (a local burst writes NO ``cloud_job`` row); ``reconcile`` is a no-op
    (local completion is synchronous, no cron read). ``dispatch`` re-homes the ``process_file`` local
    enqueue path (Phase-69 scheduler uses it; unit-tested here, NOT wired into the single-path drain).
    """

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature; local needs no session probe
        """Always True -- local dispatch never depends on a remote cloud agent."""
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- protocol signature; local holds no cloud_job rows
        """Always 0 -- a local burst writes no ``cloud_job`` row, so it holds no in-flight cloud slot."""
        return 0

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> None:
        """Enqueue ``process_file`` on the fileserver agent's queue; a clean hold when no agent is online.

        Re-homes the local ``enqueue_process_file`` producer (``analysis_enqueue``). Writes NO
        ``cloud_job`` row. An absent agent degrades to a clean hold (NoActiveAgentError -> no-op),
        matching the cron no-op discipline -- never a raise.
        """
        cfg = cast("ControlSettings", get_settings())
        try:
            agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            logger.info("LocalBackend.dispatch hold: no fileserver agent online", file_id=str(file.id))
            return
        queue = task_router.queue_for(agent.id)
        await enqueue_process_file(queue, file, agent.id, cfg.models_path)

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> None:  # noqa: ARG002 -- protocol signature; local has no cron read
        """No-op: local analysis completion is synchronous -- there is no cron read to run."""
        return None


class ComputeAgentBackend(_BaseBackend):
    """Cloud-compute (rsync/push over Tailscale) backend -- re-homes the ``push_file`` control-side enqueue leg.

    Fleshed out in Task 2. ``in_flight_count`` is inherited from :class:`_BaseBackend` (the D-02 substrate).
    """

    async def is_available(self, session: AsyncSession) -> bool:
        raise NotImplementedError

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> None:
        raise NotImplementedError

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> None:
        raise NotImplementedError


class KueueBackend(_BaseBackend):
    """Kueue-cluster backend -- re-homes today's single-cluster S3-staging + kube submit/reconcile (D-05).

    Fleshed out in Task 2. ``in_flight_count`` is inherited from :class:`_BaseBackend` (the D-02 substrate).
    """

    async def is_available(self, session: AsyncSession) -> bool:
        raise NotImplementedError

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> None:
        raise NotImplementedError

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> None:
        raise NotImplementedError


def resolve_backends(settings: ControlSettings) -> list[Backend]:
    """Build one :class:`Backend` impl per registry entry; raise fast at boot if >1 non-local resolves.

    D-07 boot guard (relocated from ``config._single_non_local``): Phase 68 stays single-dispatch-path,
    so the registry must reduce to exactly one non-local backend (+ any locals). A ``>1``-non-local
    registry is a Phase-69 (SCHED) capability -- fail fast here naming the offending ids rather than
    silently picking one. Each impl binds to its Phase-67 discriminated-union submodel (``config``).
    """
    resolved: list[Backend] = []
    for entry in settings.backends:
        if entry.kind == "local":
            resolved.append(LocalBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
        elif entry.kind == "compute":
            resolved.append(ComputeAgentBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
        elif entry.kind == "kueue":
            resolved.append(KueueBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))

    non_local = [backend for backend in resolved if not isinstance(backend, LocalBackend)]
    if len(non_local) > 1:
        raise ValueError(
            f"multi-backend dispatch lands in Phase 69 (SCHED): {len(non_local)} non-local backends "
            f"{[backend.id for backend in non_local]} are configured, but Phase 68 resolves only a ≤1-non-local registry"
        )
    return resolved


def resolved_non_local_kind(settings: ControlSettings) -> str:
    """Return the registry-derived active kind: ``"local"`` when all-local, else the single non-local kind.

    The Wave-3 replacement for the deleted ``active_cloud_kind`` accessor (D-07/D-09): ``"local"`` when
    ``cloud_enabled`` is False, otherwise the sole non-local backend's kind (``"compute"`` | ``"kueue"``).
    The single-non-local invariant is enforced by :func:`resolve_backends`'s boot guard.
    """
    if not settings.cloud_enabled:
        return "local"
    non_local = [backend for backend in settings.backends if backend.kind != "local"]
    return non_local[0].kind
