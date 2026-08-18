"""The ``Backend`` protocol, the shared ``_BaseBackend`` carrier, and the two in-flight status sets.

Extracted verbatim from the former single-module ``services/backends.py`` (phaze-dr9df). This is the
bottom of the package's dependency DAG: it imports nothing else from ``phaze.services.backends`` and
every backend-kind module (:mod:`~phaze.services.backends.local`,
:mod:`~phaze.services.backends.compute_agent`, :mod:`~phaze.services.backends.kueue`) builds on it.

The decision records this file realizes (D-02 the uniform ``in_flight_count`` substrate, D-10 the
in-flight status set) are stated in full in the package docstring -- see
:mod:`phaze.services.backends`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func, select

from phaze.models.cloud_job import CloudJob, CloudJobStatus


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config_backends import BackendConfig
    from phaze.models.file import FileRecord
    from phaze.services.agent_task_router import AgentTaskRouter


# D-10 (Q3): the exact non-terminal / in-flight CloudJobStatus set in_flight_count counts. Terminal =
# {SUCCEEDED, FAILED}. Pinned against the live CloudJobStatus members in models/cloud_job.py.
IN_FLIGHT: tuple[CloudJobStatus, ...] = (
    CloudJobStatus.UPLOADING,
    CloudJobStatus.UPLOADED,
    CloudJobStatus.SUBMITTED,
    CloudJobStatus.RUNNING,
)

# phaze-ul2v: the PRE-SUBMIT half of :data:`IN_FLIGHT`. These two statuses consume a lane cap slot
# exactly like {SUBMITTED, RUNNING}, but nothing in the reconcile cron selected them -- they are
# terminalized ONLY by the agent HTTP callbacks (``/uploaded``, ``/failed``). A dead agent or a lost
# ``s3_upload`` SAQ job strands the row here forever. :meth:`KueueBackend._reap_stranded_staging` is
# the age-bounded safety net that covers exactly this set.
STAGING: tuple[CloudJobStatus, ...] = (CloudJobStatus.UPLOADING, CloudJobStatus.UPLOADED)


class Backend(Protocol):
    """The single internal dispatch seam that removes the ``if kind == …`` cloud-target fork (§4.2).

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

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
        """Flip ``file`` into the cloud window + write its ``cloud_job`` row, IN the caller's txn (D-03). Never commits.

        Returns ``True`` when new dispatch work was actually enqueued (a genuine stage) and ``False``
        when the enqueue was a deterministic-key dedup no-op / a clean hold -- the drain counts the
        former as ``staged`` and the latter as ``skipped`` (preserves the Phase-50 tally semantics).
        """
        ...

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:
        """Advance this backend's in-flight ``cloud_job`` rows toward terminal (kueue: cron read; local/compute: no-op).

        Returns a per-backend outcome ``tally`` dict for the cron to aggregate (kueue), or ``None`` for
        the callback-driven no-op backends (local/compute) that own no cron read.
        """
        ...


class _BaseBackend:
    """Shared ``id`` / ``rank`` / ``cap`` carrier + the uniform ``cloud_job``-derived ``in_flight_count``.

    Each concrete backend binds to a single Phase-67 registry entry (``config``). The shared
    ``in_flight_count`` is the D-02/D-10 substrate: a pure DB COUNT filtered by ``backend_id`` + the
    in-flight status set (the per-backend replacement for the Phase-69-retired global window count).
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
