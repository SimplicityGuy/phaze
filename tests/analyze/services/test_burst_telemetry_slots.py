"""The burst lane's controller-allocated telemetry slot (phaze-w15ju).

``phaze.services.burst_telemetry_slots`` is the seat that keeps CONCURRENT burst pods on distinct
``service.instance.id`` values. The defect it closes is measured, not argued: two producers under
one identity produced a DECREASING cumulative counter and an ``increase()`` of 590 where 320 was
delivered -- +84.4%, growing to +221.5% over twice as many exports
(``docs/telemetry/concurrent-identity.md``).

**What is asserted here and what is asserted elsewhere.** This module tests the ALLOCATOR against a
real Postgres: the bound, the lowest-free-index rule, and above all the release property -- a slot is
held only while its ``cloud_job`` row is in flight, so nothing has to release it and a crashed pod
cannot leak one. That the allocated slot actually *reaches the pod as a distinct identity* is a
different claim with a different consumer, and it is asserted in
``tests/analyze/tasks/test_submit_cloud_job.py`` against the built Job manifest and the pod-side
environment chain.

**Why the release tests drive real writers.** ``hold_awaiting_cloud`` is the single go-forward writer
of ``status='awaiting'`` that both ``KueueBackend._reap_stranded_staging`` and the reconcile cron's
over-cap spill call, so a test that calls it is testing the reap path's own mechanism rather than a
restatement of it. The terminal statuses are covered by iterating every ``CloudJobStatus`` member
against :data:`phaze.services.backends.base.IN_FLIGHT`, which is what makes the property structural:
a future status added to the in-flight set cannot silently start holding a slot forever.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import burst_telemetry_slots
from phaze.services.backends.admission import hold_awaiting_cloud
from phaze.services.backends.base import IN_FLIGHT


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_file() -> FileRecord:
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.flac",
        original_filename=f"{uid.hex}.flac",
        current_path=f"/music/{uid.hex}.flac",
        file_type="flac",
        file_size=1000,
    )


async def _seed(
    session: AsyncSession,
    *,
    status: str = CloudJobStatus.UPLOADED.value,
    telemetry_slot: int | None = None,
) -> FileRecord:
    """Add a file plus its dispatch-stamped ``cloud_job`` row and commit both."""
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            backend_id="kueue-x64",
            s3_key=f"phaze-staging/{file.id}",
            status=status,
            telemetry_slot=telemetry_slot,
        )
    )
    await session.commit()
    return file


async def _recorded_slot(session: AsyncSession, file_id: uuid.UUID) -> int | None:
    return (await session.execute(select(CloudJob.telemetry_slot).where(CloudJob.file_id == file_id))).scalar_one()


async def _set_status(session: AsyncSession, file_id: uuid.UUID, status: str) -> None:
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one()
    row.status = status
    await session.commit()


def _registry(*caps: int) -> Any:
    """A settings stub whose kueue entries carry ``caps``, plus a local entry that must not count."""
    backends: list[Any] = [SimpleNamespace(kind="local", id="local", cap=8)]
    backends.extend(SimpleNamespace(kind="kueue", id=f"kueue-{index}", cap=cap) for index, cap in enumerate(caps))
    return SimpleNamespace(backends=backends)


# The bound


def test_the_bound_is_the_sum_of_the_kueue_caps_and_ignores_every_other_kind() -> None:
    """``burst_slot_bound`` counts kueue caps only -- a local/compute backend allocates its own slots.

    The host and compute lanes run their children under an in-process
    ``phaze.telemetry.slots.SlotPool``, sized from ``worker_process_pool_size`` in the one process
    that bounds them. Counting their caps here would inflate the burst bound -- and therefore the
    cardinality ceiling -- for identities this allocator never issues.
    """
    assert burst_telemetry_slots.burst_slot_bound(_registry(4)) == 4
    assert burst_telemetry_slots.burst_slot_bound(_registry(4, 2)) == 6


def test_a_registry_with_no_kueue_backend_still_yields_a_usable_bound() -> None:
    """The floor is 1, never 0: a zero-sized pool would make ``range(bound)`` empty and hand out nothing."""
    assert burst_telemetry_slots.burst_slot_bound(_registry()) == 1


# Allocation


async def test_concurrent_in_flight_rows_get_distinct_lowest_free_slots(session: AsyncSession) -> None:
    """Four in-flight files take 0, 1, 2, 3 -- the whole point of the allocator."""
    issued = []
    for _ in range(4):
        file = await _seed(session)
        issued.append(await burst_telemetry_slots.allocate_slot(session, file.id, bound=4))
        await session.commit()
    assert issued == [0, 1, 2, 3]


async def test_an_exhausted_pool_returns_none_rather_than_raising(session: AsyncSession) -> None:
    """A fifth concurrent submission against a bound of 4 degrades to the shared identity.

    Instrumentation may never be the thing that fails the work it observes. The returned ``None``
    means "emit no slot key", so the pod exports under the unsuffixed identity -- the pre-fix
    behaviour, which is bad and bounded. Minting an unbounded identity instead is the one failure
    direction that cannot be recovered from once it has been scraped into somebody else's storage.
    """
    for _ in range(4):
        file = await _seed(session)
        await burst_telemetry_slots.allocate_slot(session, file.id, bound=4)
        await session.commit()

    overflow = await _seed(session)
    assert await burst_telemetry_slots.allocate_slot(session, overflow.id, bound=4) is None
    await session.commit()
    assert await _recorded_slot(session, overflow.id) is None


async def test_the_allocated_slot_is_persisted_on_the_row(session: AsyncSession) -> None:
    """The decision has to survive a controller restart, which is the only reason it is a column.

    A restarted controller re-reads the held set from these rows. Without the write, its next submit
    would allocate from a view that has forgotten every pod already running and hand out a slot one
    of them is still exporting under.
    """
    file = await _seed(session)
    slot = await burst_telemetry_slots.allocate_slot(session, file.id, bound=4)
    await session.commit()
    assert await _recorded_slot(session, file.id) == slot


async def test_a_resubmit_keeps_a_slot_that_is_still_free(session: AsyncSession) -> None:
    """Re-allocating for a live Job must not move the identity its pod is already exporting under.

    The 409-AlreadyExists path leaves the existing Job -- and therefore the existing pod env --
    untouched, so a second allocation that changed the answer would put the row and the running pod
    into disagreement about which series the counters belong to.
    """
    file = await _seed(session)
    first = await burst_telemetry_slots.allocate_slot(session, file.id, bound=4)
    await session.commit()
    await _set_status(session, file.id, CloudJobStatus.SUBMITTED.value)

    assert await burst_telemetry_slots.allocate_slot(session, file.id, bound=4) == first
    await session.commit()


async def test_a_stale_slot_taken_by_another_row_is_not_reused(session: AsyncSession) -> None:
    """A re-driven file's OLD slot is re-validated, not trusted -- the one way the column could lie.

    A spill to ``'awaiting'`` frees the slot (it leaves the in-flight set) and leaves the value in
    the column on purpose, as the record of which identity that attempt exported under. If the file
    comes back through staging while another Job has since taken that index, reusing the recorded
    value would put two live pods on one identity: the defect, reintroduced by the allocator.
    """
    spilled = await _seed(session, status=CloudJobStatus.AWAITING.value, telemetry_slot=0)
    holder = await _seed(session, status=CloudJobStatus.RUNNING.value, telemetry_slot=0)

    await _set_status(session, spilled.id, CloudJobStatus.UPLOADED.value)
    reissued = await burst_telemetry_slots.allocate_slot(session, spilled.id, bound=4)
    await session.commit()

    assert reissued == 1
    assert await _recorded_slot(session, holder.id) == 0


async def test_a_row_without_a_slot_does_not_hold_one(session: AsyncSession) -> None:
    """A staging row with a NULL slot holds nothing, so the first submit still gets index 0.

    Slots are allocated at submit; the {uploading, uploaded} half of the in-flight set spends a lane
    cap slot but no telemetry slot. Counting NULL as held would shrink the pool by the size of the
    staging window.
    """
    await _seed(session, status=CloudJobStatus.UPLOADING.value)
    await _seed(session, status=CloudJobStatus.UPLOADED.value)
    file = await _seed(session)

    assert await burst_telemetry_slots.allocate_slot(session, file.id, bound=4) == 0
    await session.commit()


# Release -- derived from ``status``, so nothing calls a release


@pytest.mark.parametrize("terminal", [CloudJobStatus.SUCCEEDED.value, CloudJobStatus.FAILED.value])
async def test_a_terminalized_row_frees_its_slot_with_no_release_call(session: AsyncSession, terminal: str) -> None:
    """Completion and crash/eviction both free the slot, and neither runs any release code.

    ``succeeded`` is the completion case; ``failed`` is what the reconcile cron writes for a Job
    whose pod crashed, was evicted, or died with its node. Both leave
    :data:`phaze.services.backends.base.IN_FLIGHT`, which is the whole free-list mechanism -- so the
    "leaked slot cannot permanently shrink the pool" property holds without any writer in the lane
    having to remember to give a slot back.
    """
    finished = await _seed(session)
    assert await burst_telemetry_slots.allocate_slot(session, finished.id, bound=4) == 0
    await session.commit()
    await _set_status(session, finished.id, terminal)

    successor = await _seed(session)
    assert await burst_telemetry_slots.allocate_slot(session, successor.id, bound=4) == 0
    await session.commit()
    # The finished row keeps its value: inert (the query filters on status) and the post-mortem
    # record of which identity its counters landed on.
    assert await _recorded_slot(session, finished.id) == 0


async def test_the_real_spill_writer_frees_a_slot_bearing_row(session: AsyncSession) -> None:
    """The reap path's OWN writer frees the slot -- ``hold_awaiting_cloud``, not a stand-in for it.

    ``KueueBackend._reap_stranded_staging`` (the age-bounded safety net for a row whose callback was
    lost) and the reconcile cron's over-cap spill both terminalize through this one function in SPILL
    mode. Calling it is therefore a test of the reap mechanism rather than a re-assertion of the
    status set: if a future change made the spill write some status that is still in flight, this
    fails, and a test that merely set ``status='awaiting'`` itself would not.
    """
    stranded = await _seed(session)
    assert await burst_telemetry_slots.allocate_slot(session, stranded.id, bound=4) == 0
    await session.commit()
    await _set_status(session, stranded.id, CloudJobStatus.SUBMITTED.value)

    file = (await session.execute(select(FileRecord).where(FileRecord.id == stranded.id))).scalar_one()
    spilled = await hold_awaiting_cloud(
        session,
        file,
        attempts=1,
        expect_status=[CloudJobStatus.SUBMITTED.value],
        clear_cloud_phase=True,
    )
    await session.commit()
    assert spilled is True

    successor = await _seed(session)
    assert await burst_telemetry_slots.allocate_slot(session, successor.id, bound=4) == 0
    await session.commit()


async def test_exactly_the_in_flight_statuses_hold_a_slot(session: AsyncSession) -> None:
    """Iterate EVERY ``CloudJobStatus`` and check it holds a slot iff it is in ``IN_FLIGHT``.

    This is the structural half of the release property. The allocator reads occupancy from the same
    status set that bounds the lane's cap, so the two cannot drift -- but a new member added to
    ``IN_FLIGHT`` would start holding slots, and a member removed from it would start freeing them,
    with no other test noticing. Enumerating the enum makes that a build failure and names the
    status.
    """
    in_flight = {status.value for status in IN_FLIGHT}
    for status in CloudJobStatus:
        holder = await _seed(session, status=CloudJobStatus.UPLOADED.value)
        assert await burst_telemetry_slots.allocate_slot(session, holder.id, bound=4) == 0
        await session.commit()
        await _set_status(session, holder.id, status.value)

        probe = await _seed(session)
        issued = await burst_telemetry_slots.allocate_slot(session, probe.id, bound=4)
        await session.commit()
        expected = 1 if status.value in in_flight else 0
        assert issued == expected, f"status {status.value!r} should {'hold' if expected else 'free'} slot 0"

        # Park both rows outside the in-flight set so the next iteration starts from an empty pool.
        await _set_status(session, holder.id, CloudJobStatus.SUCCEEDED.value)
        await _set_status(session, probe.id, CloudJobStatus.SUCCEEDED.value)
