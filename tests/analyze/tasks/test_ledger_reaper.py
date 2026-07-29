"""Tests for the control-side resolved-ledger reconciler (``phaze.tasks.ledger_reaper``).

phaze-2u8v.2. A ``scheduling_ledger`` row is created at the ``before_enqueue`` chokepoint and deleted
ONLY by a terminal-outcome callback. Before this task NOTHING anywhere reconciled one, so a lost clear
leaked a row that reports ``in_flight`` forever (bare ledger existence IS ``in_flight``, D-01) and
holds the file out of ``eligible_clause`` and the cloud drain permanently. ``recover_orphaned_work``
is structurally blind to the leak because it excludes domain-completed rows. On the live archive this
had accumulated to 176 analyze rows against 4963 total.

The line these tests defend is the one that separates a reconciler from a data-loss bug:

  RESOLVED (reap)   -- running nowhere AND the stage domain-completed. The clear the lost terminal
                       callback owed, run late.
  ORPHANED (SPARE)  -- running nowhere and NO outcome. Genuinely owed work; the ledger row IS the
                       2026-06-18 over-enqueue guard and Recover is what re-drives it. Reaping these
                       would silently discard ~2146 files' worth of analyze work on the live archive.

``test_orphaned_row_is_never_reaped`` is the mutation-check for that: drop the
``domain_completed_clause`` conjunct from ``resolved_ledger_clause`` and it goes RED.

``saq_jobs`` is created locally (SAQ owns it at runtime; ``Base.metadata.create_all`` does not know
it) so the liveness probe has something real to read -- the same idiom as
``tests/analyze/tasks/test_aborting_reaper.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import select, text

from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION
from phaze.tasks.ledger_reaper import reap_resolved_ledger_rows


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


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


def _make_ctx() -> dict[str, Any]:
    from phaze.database import async_session

    return {"async_session": async_session}


def _key(stage: Stage, file: FileRecord) -> str:
    return f"{STAGE_TO_FUNCTION[stage.value]}:{file.id}"


async def _seed_ledger(session: AsyncSession, stage: Stage, file: FileRecord) -> str:
    """Write the ledger row that makes ``stage`` read ``in_flight`` for ``file``."""
    func_name = STAGE_TO_FUNCTION[stage.value]
    session.add(SchedulingLedger(key=_key(stage, file), function=func_name, routing="agent", payload={"file_id": str(file.id)}))
    await session.commit()
    return _key(stage, file)


async def _seed_saq(session: AsyncSession, key: str, status: str) -> None:
    await session.execute(
        text("INSERT INTO saq_jobs (key, status) VALUES (:key, :status)"),
        {"key": key, "status": status},
    )
    await session.commit()


async def _ledger_keys(session: AsyncSession) -> set[str]:
    return set((await session.execute(select(SchedulingLedger.key))).scalars().all())


async def _analyze_done(session: AsyncSession, file: FileRecord) -> None:
    session.add(AnalysisResult(file_id=file.id, analysis_completed_at=datetime.now(UTC)))
    await session.commit()


async def test_resolved_row_is_reaped(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """analyze finished, no live job: the leaked ledger row is cleared -- the clear the callback owed."""
    await _saq_table(session)
    file = await make_file()
    key = await _seed_ledger(session, Stage.ANALYZE, file)
    await _analyze_done(session, file)

    outcome = await reap_resolved_ledger_rows(_make_ctx())

    assert outcome["reaped"] == 1
    assert outcome["analyze"] == 1
    assert key not in await _ledger_keys(session)


async def test_orphaned_row_is_never_reaped(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """MUTATION-CHECK: a scheduled row with NO outcome is owed work and MUST survive.

    This is the whole safety argument. The file is running nowhere -- exactly like the reaped row in
    the test above -- and differs ONLY in having no analysis result. Dropping the
    ``domain_completed_clause`` conjunct from ``resolved_ledger_clause`` turns this reconciler into a
    silent deletion of every recovery candidate, and this assertion is what goes RED.
    """
    await _saq_table(session)
    file = await make_file()
    key = await _seed_ledger(session, Stage.ANALYZE, file)

    outcome = await reap_resolved_ledger_rows(_make_ctx())

    assert outcome["reaped"] == 0
    assert key in await _ledger_keys(session), "an orphaned row is owed work -- Recover re-drives it, the reaper must not eat it"


async def test_live_broker_row_protects_a_completed_stage(session: AsyncSession, make_file) -> None:
    """A RE-RUN of an already-done file keeps its ledger row: a live saq_jobs key means it is in flight.

    ``in_flight ≻ done`` in the locked ladder is deliberate -- re-analysing a done file IS in flight.
    Reaping here would delete a live job's ledger row mid-run.
    """
    await _saq_table(session)
    file = await make_file()
    key = await _seed_ledger(session, Stage.ANALYZE, file)
    await _analyze_done(session, file)
    await _seed_saq(session, key, "active")

    outcome = await reap_resolved_ledger_rows(_make_ctx())

    assert outcome["reaped"] == 0
    assert key in await _ledger_keys(session)


async def test_queued_is_live_too(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """``queued`` counts as live alongside ``active`` -- a parked/paused job is queued, not lost."""
    await _saq_table(session)
    file = await make_file()
    key = await _seed_ledger(session, Stage.ANALYZE, file)
    await _analyze_done(session, file)
    await _seed_saq(session, key, "queued")

    assert (await reap_resolved_ledger_rows(_make_ctx()))["reaped"] == 0
    assert key in await _ledger_keys(session)


async def test_terminal_broker_row_does_not_protect(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """A ``complete`` saq_jobs row is NOT liveness -- SAQ sweeps terminal rows, so they are no signal."""
    await _saq_table(session)
    file = await make_file()
    key = await _seed_ledger(session, Stage.ANALYZE, file)
    await _analyze_done(session, file)
    await _seed_saq(session, key, "complete")

    assert (await reap_resolved_ledger_rows(_make_ctx()))["reaped"] == 1
    assert key not in await _ledger_keys(session)


async def test_cloud_busy_file_is_spared(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """Compute dispatch has NO controller-side saq_jobs row -- the cloud_job disjunct must protect it.

    Without ``cloud_busy_clause`` every file dispatched to a compute lane would look reapable the
    moment its analysis row existed, deleting the ledger row of work that is genuinely running.
    """
    await _saq_table(session)
    file = await make_file()
    key = await _seed_ledger(session, Stage.ANALYZE, file)
    await _analyze_done(session, file)
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.RUNNING.value))
    await session.commit()

    assert (await reap_resolved_ledger_rows(_make_ctx()))["reaped"] == 0
    assert key in await _ledger_keys(session)


async def test_reap_is_idempotent(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """A second pass over unchanged state matches nothing -- re-running is a no-op, never a double effect."""
    await _saq_table(session)
    file = await make_file()
    await _seed_ledger(session, Stage.ANALYZE, file)
    await _analyze_done(session, file)

    assert (await reap_resolved_ledger_rows(_make_ctx()))["reaped"] == 1
    assert (await reap_resolved_ledger_rows(_make_ctx()))["reaped"] == 0
    assert (await reap_resolved_ledger_rows(_make_ctx()))["reaped"] == 0


async def test_degrades_to_zero_without_saq_jobs(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """With no ``saq_jobs`` table the liveness probe IS the evidence, so the reaper does NOTHING.

    Deliberately ASYMMETRIC with ``clear_ledger_entry``, which falls back to an unguarded delete when
    its probe fails. That fallback is safe for a caller that just watched its own job go terminal; here
    the probe is the only thing standing between a reap and deleting a live job's row.
    """
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.commit()
    file = await make_file()
    key = await _seed_ledger(session, Stage.ANALYZE, file)
    await _analyze_done(session, file)

    outcome = await reap_resolved_ledger_rows(_make_ctx())

    assert outcome["reaped"] == 0
    assert key in await _ledger_keys(session)


async def test_non_enrich_ledger_rows_are_untouched(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """``push_file`` / ``s3_upload`` / controller rows have no domain predicate and are out of scope."""
    await _saq_table(session)
    file = await make_file()
    await _analyze_done(session, file)
    session.add(SchedulingLedger(key=f"push_file:{file.id}", function="push_file", routing="agent", payload={"file_id": str(file.id)}))
    await session.commit()

    await reap_resolved_ledger_rows(_make_ctx())

    assert f"push_file:{file.id}" in await _ledger_keys(session)
