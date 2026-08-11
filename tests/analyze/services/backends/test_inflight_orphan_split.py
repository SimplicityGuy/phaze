"""The in-flight / orphaned split in the stage-bucket readers (phaze-2u8v.2, D-01a).

THE DEFECT. ``inflight_clause`` is bare ``scheduling_ledger`` existence (D-01), which means
"scheduled and unresolved" -- the UNION of work that is really running and work that was scheduled
and then lost. Nothing decays the lost half, so it accumulates forever. Measured on the live archive
for the analyze lane on 2026-07-28: 4963 ledger rows reported as in-flight against 2583 live SAQ rows
(+58 on compute lanes SAQ cannot see); 2146 of the difference were orphans and 176 were rows whose
terminal clear had been lost. The counter was wrong by more than 2x, which is what
``test_in_flight_matches_the_broker`` pins.

THE FIX under test: ``_safe_orphan_split`` carves the orphaned population out of the ``in_flight``
bucket into ``ORPHANED_BUCKET`` using ``orphaned_clause``. It is NOT a subtraction that loses files --
the six buckets still sum to the corpus total (``test_split_preserves_the_total``) -- and it is NOT a
new ``Status``: the derived per-file status, ``eligible_clause`` and recovery all read exactly what
they read before, so an orphaned file stays ineligible and stays in Recover's work set
(``test_split_does_not_touch_the_derived_status_or_eligibility``).

``saq_jobs`` is created locally (SAQ owns it at runtime; ``Base.metadata.create_all`` does not know
it). The degrade test drops it to prove the split reverts to exactly the pre-fix ledger-only numbers
rather than 500ing a hot poll -- the reason this lives OUTSIDE the locked ``stage_status_case``
ladder, where a missing broker table would break the Files matrix and eligibility too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import select, text

from phaze.enums.stage import Stage, Status, eligible, resolve_status
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.pipeline import ORPHANED_BUCKET, _safe_bucket_counts
from phaze.services.stage_status import _CLOUD_BUSY_STATUSES, stage_status_case
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# A sibling suite may have created ``saq_jobs`` with the full SAQ schema (NOT NULL ``job``/``queue``) or
# with a 2-column stand-in, so a bare ``CREATE TABLE IF NOT EXISTS`` is order-dependent. DROP + CREATE
# pins the schema THIS module controls (the idiom from tests/shared/routers/test_pipeline.py); the drop
# is undone when the per-test transaction rolls back. Only ``key``/``status`` are read by the code under
# test, so the minimal shape is faithful.
_CREATE_SAQ_JOBS = (
    text("DROP TABLE IF EXISTS saq_jobs"),
    text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"),
)


async def _saq_table(session: AsyncSession) -> None:
    """Pin a ``saq_jobs`` table this module controls (see :data:`_CREATE_SAQ_JOBS`)."""
    for stmt in _CREATE_SAQ_JOBS:
        await session.execute(stmt)
    await session.commit()


_ANALYZE_FN = STAGE_TO_FUNCTION[Stage.ANALYZE.value]


async def _ledger(session: AsyncSession, file: FileRecord) -> str:
    key = f"{_ANALYZE_FN}:{file.id}"
    session.add(SchedulingLedger(key=key, function=_ANALYZE_FN, routing="agent", payload={"file_id": str(file.id)}))
    await session.commit()
    return key


async def _saq(session: AsyncSession, key: str, status: str) -> None:
    await session.execute(
        text("INSERT INTO saq_jobs (key, status) VALUES (:key, :status)"),
        {"key": key, "status": status},
    )
    await session.commit()


async def test_orphan_is_carved_out_of_in_flight(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """A scheduled file with no live job and no result reports ORPHANED, not in-flight."""
    await _saq_table(session)
    orphan = await make_file()
    await _ledger(session, orphan)

    buckets = await _safe_bucket_counts(session, Stage.ANALYZE)

    assert buckets[ORPHANED_BUCKET] == 1
    assert buckets[Status.IN_FLIGHT.value] == 0, "nothing is running this file -- calling it in-flight is the defect"


async def test_in_flight_matches_the_broker(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """THE BEAD, in miniature: 3 ledger rows, 1 live SAQ job -> in-flight reads 1, not 3.

    This is the live archive's 4963-vs-2583 shape scaled down. On pre-fix code every one of the three
    ledger rows counts as in-flight and this assertion is RED.
    """
    await _saq_table(session)
    running = await make_file()
    await _saq(session, await _ledger(session, running), "active")
    for _ in range(2):
        await _ledger(session, await make_file())

    buckets = await _safe_bucket_counts(session, Stage.ANALYZE)

    assert buckets[Status.IN_FLIGHT.value] == 1, "SAQ is right about the queue"
    assert buckets[ORPHANED_BUCKET] == 2


async def test_queued_job_is_in_flight(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """A ``queued`` broker row is live: waiting in the queue is in flight, not lost."""
    await _saq_table(session)
    await _saq(session, await _ledger(session, await make_file()), "queued")

    buckets = await _safe_bucket_counts(session, Stage.ANALYZE)

    assert buckets[Status.IN_FLIGHT.value] == 1
    assert buckets[ORPHANED_BUCKET] == 0


async def test_compute_dispatch_is_in_flight_though_saq_cannot_see_it(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """A file busy on a compute lane has NO controller saq_jobs row and must still read in-flight.

    Without the ``cloud_busy_clause`` disjunct, every cloud-dispatched file would be mislabelled
    orphaned -- the exact "make the counter agree by weakening what it counts" failure this bead
    forbids. Compute dispatch is real in-flight work that SAQ is structurally blind to.
    """
    await _saq_table(session)
    file = await make_file()
    await _ledger(session, file)
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.RUNNING.value))
    await session.commit()

    buckets = await _safe_bucket_counts(session, Stage.ANALYZE)

    assert buckets[Status.IN_FLIGHT.value] == 1
    assert buckets[ORPHANED_BUCKET] == 0


async def test_held_awaiting_cloud_is_in_flight(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """A file HELD awaiting cloud is owned by the drain, not lost -- matching recovery's fourth exclusion."""
    await _saq_table(session)
    file = await make_file()
    await _ledger(session, file)
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.AWAITING.value))
    await session.commit()

    buckets = await _safe_bucket_counts(session, Stage.ANALYZE)

    assert buckets[ORPHANED_BUCKET] == 0


async def test_completed_stage_is_not_orphaned(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """A domain-complete file with a leaked ledger row is RESOLVED, not orphaned -- the reaper's set.

    The two populations are disjoint by construction and must not be conflated: an orphan is owed
    work, a resolved row is stale state. This one stays in ``in_flight`` until
    ``reap_resolved_ledger_rows`` clears its row.
    """
    await _saq_table(session)
    file = await make_file()
    await _ledger(session, file)
    session.add(AnalysisResult(file_id=file.id, analysis_completed_at=datetime.now(UTC)))
    await session.commit()

    buckets = await _safe_bucket_counts(session, Stage.ANALYZE)

    assert buckets[ORPHANED_BUCKET] == 0


async def test_split_preserves_the_total(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """The six buckets still sum to the corpus total -- the split MOVES files, it never hides one."""
    await _saq_table(session)
    await _saq(session, await _ledger(session, await make_file()), "active")
    await _ledger(session, await make_file())
    await make_file()
    done = await make_file()
    session.add(AnalysisResult(file_id=done.id, analysis_completed_at=datetime.now(UTC)))
    await session.commit()
    total = int((await session.execute(select(FileRecord.id))).scalars().all().__len__())

    buckets = await _safe_bucket_counts(session, Stage.ANALYZE)

    assert sum(buckets.values()) == total
    assert buckets[ORPHANED_BUCKET] == 1


async def test_split_does_not_touch_the_derived_status_or_eligibility(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """An orphaned file's DERIVED status stays ``in_flight`` and it stays INELIGIBLE.

    Load-bearing: the reporting split must not leak into the locked ladder. If an orphan resolved to
    ``not_started`` it would become eligible for auto-enqueue, re-opening the 2026-06-18 over-enqueue
    class that the ledger row exists to guard against. Recovery must remain the one thing that
    re-drives it.
    """
    await _saq_table(session)
    orphan = await make_file()
    await _ledger(session, orphan)

    derived = (await session.execute(select(stage_status_case(Stage.ANALYZE)).select_from(FileRecord).where(FileRecord.id == orphan.id))).scalar_one()

    assert derived == Status.IN_FLIGHT.value, "the locked per-file ladder is unchanged -- only the reporting bucket splits"
    assert resolve_status(Stage.ANALYZE, {"inflight": True}) is Status.IN_FLIGHT
    assert not eligible({Stage.ANALYZE: Status.IN_FLIGHT}, Stage.ANALYZE), "an orphan must stay out of the auto-enqueue set"


async def test_degrades_to_pre_fix_numbers_without_saq_jobs(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """No ``saq_jobs`` table -> ``orphaned`` stays 0 and ``in_flight`` keeps its ledger-only value.

    The reason the split lives here and not inside ``stage_status_case``: a broker probe in the locked
    ladder would take the Files matrix, the status filters and ``eligible_clause`` down with it in a
    pre-migration environment. Degrading to the previous behavior is always safe; raising never is.
    """
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.commit()
    await _ledger(session, await make_file())

    buckets = await _safe_bucket_counts(session, Stage.ANALYZE)

    assert buckets[ORPHANED_BUCKET] == 0
    assert buckets[Status.IN_FLIGHT.value] == 1


async def test_cloud_busy_statuses_track_the_backend_registry() -> None:
    """``_CLOUD_BUSY_STATUSES`` is ``backends.IN_FLIGHT`` + AWAITING, pinned so the local copy cannot drift.

    ``services/backends.py`` imports ``inflight_clause`` FROM ``stage_status``, so the set cannot be
    imported the other way without a cycle. This test is the substitute for that import: add a member
    to ``backends.IN_FLIGHT`` without updating the local tuple and it goes RED, instead of silently
    mislabelling a newly-busy file as orphaned.
    """
    from phaze.services.backends import IN_FLIGHT

    assert set(_CLOUD_BUSY_STATUSES) == {s.value for s in IN_FLIGHT} | {CloudJobStatus.AWAITING.value}
