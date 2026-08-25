"""The propose gate's IN-FLIGHT door (phaze-3542b, 2026-08-25).

A proposal that is approved and executed MOVES the file and unlinks the source, while every
enrich payload carries the pre-move ``FileRecord.original_path`` (D-24). A ``process_file`` /
``extract_file_metadata`` job already enqueued when the move lands therefore opens a path that no
longer exists. ``_proposal_pending_clauses`` closes that by refusing to propose a file with enrich
work in flight.

THE MECHANISM WAS AN OPERATOR DECISION (bead phaze-3542b, 2026-08-25; ADR-0012 rule 2). Question as
put: the mechanism for phaze-3542b's confirmed enqueue-then-execute TOCTOU, offered as labelled
options. Answer as given -- the option LABEL the operator selected, and the whole of what they
authored: "Close the door: add ~inflight to the propose gate". Durable record: the phaze-3542b bead
comment dated 2026-08-25. "Narrow the producer" was refused, so ``services/reanalysis_backfill.py``
is deliberately unchanged. The LABEL is the whole of the operator's words; the stage scope (both
enrich stages) was NOT part of it and is the implementer's decision, argued from reachability in
``_proposal_pending_clauses``'s own comment -- do not report that half as the operator's.

WHY THESE TESTS DRIVE THE REAL FUNNEL rather than inserting a ``scheduling_ledger`` row directly.
The claim under test is about a job that was ENQUEUED, and the ledger row is written by the
``before_enqueue`` chokepoint (``tasks/_shared/deterministic_key.apply_deterministic_key``), NOT by
the producer. A hand-built row would prove the PREDICATE reads a row that a test wrote -- it would
not prove that enqueuing through the real producer produces a row the predicate sees. So the queue
here runs the real hook (the ``_RealHookQueue`` shape carried over from
``tests/integration/test_agent_push_concurrency.py``) and the producers called are the real
``analysis_enqueue.enqueue_process_file`` and ``routers/pipeline/extraction._enqueue_extraction_jobs``.
This is ADR-0012 rule 3 applied to a predicate: verify with the artifact's real consumer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.pipeline.extraction import _enqueue_extraction_jobs
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.reanalysis_backfill import select_incomplete_analyses
from phaze.services.scheduling_ledger import clear_ledger_entry
from phaze.tasks._shared.deterministic_key import apply_deterministic_key
from tests._queue_fakes import FakeQueue
from tests.shared.services.pipeline._shared import (
    UTC,
    AnalysisResult,
    FileMetadata,
    ProposalStatus,
    RenameProposal,
    _make_pipeline_file,
    count_proposal_pending_files,
    datetime,
    get_proposal_pending_batches,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

    from phaze.models.file import FileRecord


_JOB_CONTROL_FIELDS = frozenset({"key", "timeout", "retries", "heartbeat", "scheduled"})


class _RealHookQueue(FakeQueue):
    """A ``FakeQueue`` whose ``enqueue()`` runs the REAL ``apply_deterministic_key`` before_enqueue
    hook, so the ``scheduling_ledger`` row under test is written by production code."""

    def __init__(self, name: str, *, ledger_sessionmaker: async_sessionmaker) -> None:
        super().__init__(name)
        self.ledger_sessionmaker = ledger_sessionmaker

    async def connect(self) -> None:
        return None

    async def enqueue(self, task_name: str, **kwargs: Any) -> Any:
        job = SimpleNamespace(
            function=task_name,
            kwargs={k: v for k, v in kwargs.items() if k not in _JOB_CONTROL_FIELDS},
            key=kwargs.get("key"),
            timeout=kwargs.get("timeout"),
            retries=kwargs.get("retries"),
            queue=self,
        )
        await apply_deterministic_key(job)
        return await super().enqueue(task_name, **kwargs)


def _hook_queue(db_connection: AsyncConnection) -> _RealHookQueue:
    """A real-hook queue whose ledger writes join THIS test's transaction.

    The production hook opens its own session off ``ledger_sessionmaker``. Binding that maker to the
    per-test ``AsyncConnection`` with the same ``create_savepoint`` recipe ``conftest.session`` uses
    keeps the hook's own ``session.commit()`` inside the per-test outer transaction, so the row it
    writes is visible to the gate query under test and is rolled back at teardown.

    NOTE the hook is best-effort by design (T-45-03: "a hiccup degrades to row-not-written and must
    NEVER block an enqueue"), so a mis-wired sessionmaker here is SWALLOWED and logged rather than
    raised. That is why every assertion below is phrased as "the file is EXCLUDED": exclusion
    requires the row to actually exist, so a silently-unwritten ledger row fails these tests instead
    of passing them vacuously.
    """
    maker = async_sessionmaker(bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    return _RealHookQueue("test-fileserver", ledger_sessionmaker=maker)


async def _proposable(session: AsyncSession) -> FileRecord:
    """A file that clears the convergence gate: metadata done, analysis COMPLETE, never proposed."""
    file = _make_pipeline_file()
    session.add(file)
    await session.flush()
    session.add(FileMetadata(file_id=file.id, artist="A", title="T"))
    session.add(
        AnalysisResult(
            file_id=file.id,
            bpm=120.0,
            analysis_completed_at=datetime.now(UTC),
            # Incomplete window coverage -- what reanalysis_backfill hunts (phaze-kj8dl).
            fine_windows_analyzed=10,
            fine_windows_total=400,
            coarse_windows_analyzed=5,
            coarse_windows_total=200,
        )
    )
    await session.flush()
    return file


async def _pending_ids(session: AsyncSession) -> set[str]:
    return {fid for batch in await get_proposal_pending_batches(session, 50) for fid in batch}


@pytest.mark.asyncio
async def test_proposal_gate_excludes_a_file_whose_reanalysis_is_in_flight(session: AsyncSession, _db_connection: AsyncConnection) -> None:
    """THE phaze-3542b REGRESSION TEST -- fails against pre-fix code, which returned the file.

    Drives the confirmed shape end to end with the real components: ``select_incomplete_analyses``
    picks the file, ``enqueue_process_file`` ships the payload, the real before_enqueue hook writes
    the ledger row, and the gate must then refuse to propose it. Pre-fix the gate had no in-flight
    conjunct at all, so the file stayed proposable for the whole multi-hour analysis and a proposal
    approved in that window moved the file out from under the running job.
    """
    file = await _proposable(session)
    original_path = file.original_path

    # The backfill's OWN selector picks it, and the gate accepts it: the window exists.
    assert file.id in {rec.id for rec, _ in await select_incomplete_analyses(session)}
    assert str(file.id) in await _pending_ids(session), "precondition: the file is proposable before the enqueue"

    # The real producer enqueues, and the real before_enqueue hook writes process_file:<id>.
    await enqueue_process_file(_hook_queue(_db_connection), file, "test-fileserver", "/models")

    # THE DOOR: the file is no longer proposable, so no move can be scheduled underneath the job.
    assert str(file.id) not in await _pending_ids(session), "a file with process_file in flight must NOT be proposable"
    # The counter and the batching producer share _proposal_pending_clauses and must not drift.
    assert await count_proposal_pending_files(session) == 0

    # And this is what the door prevents. Read the STORED ledger payload -- the durable artifact the
    # consumer (and any recovery replay) would later open -- not the producer's return value.
    stored = await session.get(SchedulingLedger, f"process_file:{file.id}")
    assert stored is not None, "the real before_enqueue hook must have written the ledger row"
    assert stored.payload["original_path"] == original_path

    # Had the gate let this through, the proposal would execute and unlink exactly that path while
    # the job still names it. current_path follows the move; original_path never does (D-24).
    session.add(RenameProposal(file_id=file.id, proposed_filename="x.mp3", proposed_path="/organized/x.mp3", status=ProposalStatus.EXECUTED.value))
    file.current_path = "/organized/x.mp3"
    await session.flush()
    assert stored.payload["original_path"] != file.current_path


@pytest.mark.asyncio
async def test_proposal_gate_excludes_a_file_whose_metadata_extraction_is_in_flight(session: AsyncSession, _db_connection: AsyncConnection) -> None:
    """The metadata half of the same door, driven through the real ``_enqueue_extraction_jobs``.

    Not redundant with phaze-rhs6m's fix: both metadata retry routes select on a bare
    ``failed_clause`` with no in-flight conjunct of their own, and ``clear_ledger_entry`` can
    legitimately skip its clear -- see ``_proposal_pending_clauses``'s comment.
    """
    file = await _proposable(session)
    assert str(file.id) in await _pending_ids(session), "precondition: proposable before the enqueue"

    await _enqueue_extraction_jobs(_hook_queue(_db_connection), [file], "test-fileserver")

    assert str(file.id) not in await _pending_ids(session), "a file with extract_file_metadata in flight must NOT be proposable"
    assert await count_proposal_pending_files(session) == 0


@pytest.mark.asyncio
async def test_proposal_gate_still_accepts_a_converged_file_with_nothing_in_flight(session: AsyncSession) -> None:
    """THE ORDINARY CASE IS UNCHANGED -- the door closes on in-flight work and on nothing else.

    Without this, a gate that excluded everything would pass the two tests above.
    """
    file = await _proposable(session)

    assert str(file.id) in await _pending_ids(session)
    assert await count_proposal_pending_files(session) == 1


@pytest.mark.asyncio
async def test_proposal_gate_readmits_the_file_once_the_ledger_row_clears(session: AsyncSession, _db_connection: AsyncConnection) -> None:
    """The door OPENS again -- the exclusion is in-flight-scoped, not a permanent disqualification.

    The terminal callback (or ``tasks/ledger_reaper``'s ``*/5`` sweep for a leaked row) deletes the
    ledger row; the file must become proposable again. A door that never reopened would strand every
    file that was ever analyzed, which is a strictly worse defect than the one being fixed.
    """
    file = await _proposable(session)
    await enqueue_process_file(_hook_queue(_db_connection), file, "test-fileserver", "/models")
    assert str(file.id) not in await _pending_ids(session)

    await clear_ledger_entry(session, f"process_file:{file.id}")
    await session.flush()

    assert str(file.id) in await _pending_ids(session), "clearing the ledger row must re-admit the file"


@pytest.mark.asyncio
async def test_an_already_proposed_file_stays_excluded_regardless_of_flight(session: AsyncSession) -> None:
    """Control: the pre-existing ``~done_clause(PROPOSE)`` exclusion is untouched by this change."""
    file = await _proposable(session)
    session.add(RenameProposal(file_id=file.id, proposed_filename="x.mp3", proposed_path="/organized/x.mp3", status=ProposalStatus.PENDING.value))
    await session.flush()

    assert str(file.id) not in await _pending_ids(session)
