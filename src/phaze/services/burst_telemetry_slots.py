"""The BURST lane's telemetry worker slot: allocated by the controller, persisted on ``cloud_job``.

**THE DEFECT, and why the host lane's answer cannot be reused.** Concurrent analysis producers must
carry distinct ``service.instance.id`` values. Under one identity a collector's Prometheus exporter
keeps a single series and takes the last write, which measurably produced a DECREASING cumulative
counter and an ``increase()`` of 590 where 320 was delivered -- **+84.4%**, growing to **+221.5%**
over twice as many exports (``docs/telemetry/concurrent-identity.md``,
``docs/design/0017-telemetry-export-topology.md`` section 8a).

:mod:`phaze.telemetry.slots` answers this for the HOST lane with a process-local free list, and that
is the right granularity there: concurrent analysis children on a host are bounded by ONE
``asyncio.Semaphore(worker_process_pool_size)`` inside ONE agent-worker process, so a free list in
that process sees every competitor. A BURST-lane child runs in a one-shot Kueue pod that is
Postgres-less by invariant and shares no memory with its peers, so nothing inside the pod can
allocate against them. Every burst pod built its own pool of one and took slot 0 -- the same merge
under the name ``phaze-analysis-0``, which is why "the identity has a slot in it" was never evidence
that this was fixed.

**THE SEAT THAT CAN SEE THE COMPETITORS IS THE CONTROLLER.** It is already the seat that enforces
the per-backend in-flight cap, from the same ``cloud_job`` rows this module reads. So the slot is
allocated at submit time, persisted on the row (``cloud_job.telemetry_slot``, migration 065), and
injected into the Job env by ``kube_staging.build_job_manifest``. Persistence is not bookkeeping
convenience: a controller restarted between two submits would otherwise allocate from an empty view
and hand the second pod a slot the first is still exporting under.

**OCCUPANCY IS DERIVED FROM ``status``, SO NOTHING RELEASES A SLOT.** The held set is "the
``telemetry_slot`` values on OTHER ``cloud_job`` rows whose ``status`` is in
:data:`phaze.services.backends.base.IN_FLIGHT`". A row that reaches succeeded/failed, or is spilled
back to ``'awaiting'`` by the reconcile cron or by ``KueueBackend._reap_stranded_staging``, frees its
slot at that instant, with no release call to forget. That is what makes a crashed, evicted or
node-lost pod safe: the same tick that terminalizes its row frees its slot, so a leak cannot
permanently shrink the pool. An explicit persisted free list could not have this property -- every
terminal writer in the lane would need a release, and the one that was missed would shrink the pool
for good.

**THE BOUND IS THE SUM OF THE KUEUE BACKENDS' CAPS, not one backend's cap, and the difference is
deliberate.** The identity space is global: two clusters' pods both reporting ``phaze-analysis-2``
merge at a shared collector exactly as two pods in one cluster would, and nothing in phaze's
configuration establishes that distinct kueue backends push to distinct collectors (the endpoint
comes from the operator's own ``phaze-agent-env`` ConfigMap, which phaze does not create). So the
free list spans every kueue backend and its size is the sum of their caps -- which, in the deployed
single-cluster configuration, IS that backend's own cap, the quantity acceptance 1 names. Each
backend can only hold ``cap`` in-flight rows, so it can only hold ``cap`` slots; the union cannot
exceed the sum. **The bound remains a multiple of the CONCURRENCY and never of the corpus** -- a
slot is reused by the next Job, so the figure does not move as the archive grows. At the deployed
cap of 4 the analysis role's 2,290-series block costs 9,160 series; a per-pod identity would be
2,290 x 11,428 = 26,170,120.

**EXHAUSTION RETURNS None, IT DOES NOT RAISE.** Instrumentation may never be the thing that fails
the work it observes (``telemetry.bootstrap``'s acceptance-7 contract, and
:meth:`phaze.telemetry.slots.SlotPool.acquire`'s own contract). A pod with no slot analyzes its file
correctly and merely shares an identity -- the pre-existing behaviour, degraded but bounded. The
opposite choice, minting an unbounded identity, is the cardinality bomb the bound exists to prevent
and is not recoverable once it has been scraped into somebody else's storage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, select, text, update
import structlog

from phaze.models.cloud_job import CloudJob


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)


def _in_flight_statuses() -> list[str]:
    """The in-flight ``cloud_job`` status values, read from the ONE place that defines them.

    **Imported inside the function because the package import is a cycle.**
    ``phaze.services.backends.base`` cannot be imported at module scope from here:
    ``phaze.tasks.submit_cloud_job`` imports this module, and importing any
    ``phaze.services.backends`` submodule runs that package's ``__init__``, which imports
    ``backends.kueue`` -> ``tasks.reconcile_cloud_jobs`` -> ``tasks.submit_cloud_job`` -- back into a
    half-initialized module. A local copy of the status tuple would break the cycle too and is the
    wrong trade: :data:`~phaze.services.backends.base.IN_FLIGHT` is what bounds the lane's cap, and
    the slot pool is only correct while occupancy and the cap are read from the SAME set. A second
    copy is how the two stop agreeing.
    """
    from phaze.services.backends.base import IN_FLIGHT  # noqa: PLC0415 -- deferred to break the backends<->submit_cloud_job import cycle

    return [status.value for status in IN_FLIGHT]


#: Serializes the read-decide-write of a slot allocation across concurrent submits.
#:
#: **A dedicated key, NOT the drain's ``5_000_504``.** Reusing the drain lock would serialize every
#: submit behind a whole ``stage_cloud_window`` tick walk, which holds it for the length of the walk.
#: This critical section is one SELECT and one UPDATE; it has no business waiting on that.
#:
#: The lock is what makes "lowest free index" correct under concurrency. Two submits racing without
#: it would both read the same held set and both take the same lowest free index -- the merge this
#: module exists to prevent, reintroduced by the allocator itself.
ALLOCATION_ADVISORY_LOCK_KEY = 5_000_505


def burst_slot_bound(settings: ControlSettings) -> int:
    """The exclusive upper bound on a burst telemetry slot: the sum of the kueue backends' caps.

    Returns at least 1 so a configuration with no kueue backend at all still yields a usable bound
    rather than a zero-sized pool (a submit cannot happen without a kueue backend -- see
    ``submit_cloud_job._resolve_backend_kube`` -- so this floor is defensive, not a live path).

    See the module docstring for why the bound spans every kueue backend instead of being the one
    backend's ``cap``.
    """
    total = sum(entry.cap for entry in settings.backends if entry.kind == "kueue")
    return max(1, total)


async def allocate_slot(session: AsyncSession, file_id: uuid.UUID, *, bound: int) -> int | None:
    """Reserve and persist this file's burst telemetry slot; return it, or None when the pool is full.

    Takes :data:`ALLOCATION_ADVISORY_LOCK_KEY` as a transaction-scoped advisory lock, reads the slots
    held by OTHER in-flight ``cloud_job`` rows, keeps this row's existing slot when it is still free
    and otherwise takes the lowest free index, writes it to the row and returns it. **The caller owns
    the commit** -- the write must be durable before the Job is POSTed, or a crash between the two
    leaves a pod exporting under a slot no row records.

    **Why "other rows" and not "rows with a slot".** Excluding this file's own row is what makes the
    function uniformly correct for a first submit and a re-submit. A re-driven file spills to
    ``'awaiting'`` (out of the in-flight set, so its slot is freed and may be reissued) and comes
    back through staging with the OLD value still sitting in its column -- deliberately, as a
    post-mortem record. Trusting that value on the next submit would hand it a slot another Job now
    holds. Re-validating it against the held set instead keeps a re-submit idempotent when the slot
    is genuinely still free -- which is the common case, and which matters because a re-submit of a
    LIVE Job (the 409-refresh path) must not move the identity its pod is already exporting under.

    Returns None when every index below ``bound`` is held, which degrades that pod to the shared
    identity rather than failing its analysis -- see the module docstring.
    """
    # Transaction-scoped: released by the caller's commit or rollback, so no unlock call can be
    # skipped on an error path.
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ALLOCATION_ADVISORY_LOCK_KEY})

    # ``is_not(None)`` is in the predicate AND the values are re-checked below: the column is
    # ``int | None`` to the type checker, and a None slipping into ``held`` would make the
    # lowest-free scan compare against it.
    held = {
        slot
        for slot in (
            await session.execute(
                select(CloudJob.telemetry_slot).where(
                    CloudJob.file_id != file_id,
                    CloudJob.telemetry_slot.is_not(None),
                    CloudJob.status.in_(_in_flight_statuses()),
                )
            )
        )
        .scalars()
        .all()
        if slot is not None
    }

    current = (await session.execute(select(CloudJob.telemetry_slot).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    if current is not None and 0 <= current < bound and current not in held:
        chosen: int | None = current
    else:
        chosen = next((index for index in range(bound) if index not in held), None)

    if chosen is None:
        logger.warning(
            "burst telemetry slot pool exhausted; this pod exports under the shared identity",
            file_id=str(file_id),
            bound=bound,
            held=sorted(held),
        )
        return None

    result = cast("CursorResult[Any]", await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(telemetry_slot=chosen)))
    if result.rowcount == 0:
        # No cloud_job row to record the decision on. A submit cannot reach here (the caller resolves
        # its backend from that row and raises without one), so this is a guard against a future
        # caller rather than a live path -- and an unrecorded slot must never be issued, because
        # nothing would then stop the next submit from issuing it again.
        logger.warning("no cloud_job row to record a burst telemetry slot on; using the shared identity", file_id=str(file_id))
        return None
    return chosen
