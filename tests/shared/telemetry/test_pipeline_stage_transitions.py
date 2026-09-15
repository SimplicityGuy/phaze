"""``phaze.pipeline.stage.transitions`` counter balance across every ledger create/resolve path (phaze-qyqig).

Before this bead the counter was unbalanced in both directions:

- :func:`~phaze.services.scheduling_ledger.upsert_ledger_entry` counted "scheduled" on EVERY
  ON-CONFLICT re-upsert, including a repeat enqueue of an already-scheduled key that creates no
  new row -- inflating the counter and keeping ``PhazeAnalysisProgressStalled``'s arming term
  (``increase(...{stage="analyze", transition="scheduled"}[1h])) > 0``) true on a quiet, periodic
  no-op re-upsert.
- :func:`~phaze.services.scheduling_ledger.insert_ledger_if_absent` and its batched sibling
  :func:`~phaze.services.scheduling_ledger.insert_ledger_rows_if_absent` -- the reenqueue/recovery
  backfill's own ledger-write primitive (``tasks.recovery_backfill.backfill_ledger_from_saq_jobs``)
  -- recorded NO "scheduled" transition at all, undercounting the arming term exactly when a
  scheduling burst reached the ledger only through a backfill path.
- :func:`~phaze.services.scheduling_ledger.clear_ledger_entry` counted "resolved" unconditionally,
  even on a guarded no-op (key absent, or protected by the phaze-3yln live-job guard).
- Three BULK-delete paths -- the ledger reaper (``tasks.ledger_reaper``), the scan-batch deletion
  cascade (``services.scan_deletion``), and the backfill-cloud CAS delete
  (``routers.pipeline.backfill`` / ``routers.pipeline.analysis``) -- resolved ledger rows with NO
  telemetry at all.

This file lives under ``tests/shared/telemetry/`` (rather than alongside each producer's own unit
tests) because the assertions need the real in-memory-OTel ``telemetry_sink`` fixture
(``tests/shared/telemetry/conftest.py``), which -- like every fixture defined in a ``conftest.py``
-- is visible only within its own directory subtree, never to a sibling directory. Each producer's
OWN unit tests (``tests/analyze/services/backends/test_scheduling_ledger.py``,
``tests/analyze/tasks/test_ledger_reaper.py``, ``tests/discovery/services/test_scan_deletion.py``,
``tests/shared/routers/pipeline/test_backfill.py``) are the right place for everything ELSE about
those functions and are unchanged by this bead except where a fake/mocked session had to be
updated to keep supporting the new ``RETURNING`` read (see their own diffs).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select, text

from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.scan_deletion import delete_scan_cascade
from phaze.services.scheduling_ledger import (
    clear_ledger_entry,
    insert_ledger_if_absent,
    insert_ledger_rows_if_absent,
    upsert_ledger_entry,
)
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION
from phaze.tasks.ledger_reaper import reap_resolved_ledger_rows
from tests._background_drain import drain_router_background_tasks
from tests._queue_fakes import seed_active_agent, wire_fakes
from tests.shared.routers.pipeline._shared import (
    _LONG,
    _cloud_compute_registry,  # noqa: F401 -- autouse fixture (pins settings.backends to one compute backend), never referenced by name
    _persist_failed_with_duration,
    _process_file_ledger_rows,
)


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from tests.shared.telemetry.conftest import TelemetrySink


def _transition_total(sink: TelemetrySink, stage: str, transition: str) -> float:
    """Sum ``phaze.pipeline.stage.transitions`` points matching ``(stage, transition)`` exactly."""
    return sum(
        point.value
        for point in sink.points("phaze.pipeline.stage.transitions")
        if dict(point.attributes or {}) == {"stage": stage, "transition": transition}
    )


# ---------------------------------------------------------------------------
# upsert_ledger_entry / insert_ledger_if_absent / insert_ledger_rows_if_absent / clear_ledger_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_counts_scheduled_once_and_reupsert_counts_nothing(session: AsyncSession, telemetry_sink: TelemetrySink) -> None:
    """upsert_ledger_entry: "scheduled" fires on the INSERT branch only, never on a re-upsert that
    merely refreshes an already-scheduled key (the arming-term over-count this bead fixes)."""
    key = "process_file:sched-balance-1"
    await upsert_ledger_entry(session, key=key, function="process_file", kwargs={"file_id": "sched-balance-1"})
    await session.commit()
    assert _transition_total(telemetry_sink, "analyze", "scheduled") == 1

    # A re-upsert of the SAME still-scheduled key (the WRITE hook's normal re-enqueue refresh) must
    # count NOTHING additional.
    await upsert_ledger_entry(session, key=key, function="process_file", kwargs={"file_id": "sched-balance-1", "v": 2})
    await session.commit()
    assert _transition_total(telemetry_sink, "analyze", "scheduled") == 1, "a re-upsert must not inflate the scheduled count"

    # Resolve it and the ledger's own counter balances to zero net.
    await clear_ledger_entry(session, key)
    await session.commit()
    assert _transition_total(telemetry_sink, "analyze", "resolved") == 1
    scheduled = _transition_total(telemetry_sink, "analyze", "scheduled")
    resolved = _transition_total(telemetry_sink, "analyze", "resolved")
    assert scheduled - resolved == 0, "one create + one resolve must balance the counter"


@pytest.mark.asyncio
async def test_insert_if_absent_counts_scheduled_once_and_repeat_counts_nothing(session: AsyncSession, telemetry_sink: TelemetrySink) -> None:
    """insert_ledger_if_absent: "scheduled" fires only when it actually inserts (ON CONFLICT DO
    NOTHING must count nothing on a repeat call for the same key -- this primitive recorded NO
    transition at all before this bead, undercounting every row the backfill seeded)."""
    key = "submit_cloud_job:sched-balance-2"
    await insert_ledger_if_absent(session, key=key, function="submit_cloud_job", kwargs={"file_id": "sched-balance-2"})
    await session.commit()
    assert _transition_total(telemetry_sink, "submit_cloud_job", "scheduled") == 1

    # A repeat insert-if-absent against the SAME (already-present) key is a genuine no-op.
    await insert_ledger_if_absent(session, key=key, function="submit_cloud_job", kwargs={"file_id": "sched-balance-2", "v": 2})
    await session.commit()
    assert _transition_total(telemetry_sink, "submit_cloud_job", "scheduled") == 1, "a DO NOTHING conflict must not count"

    await clear_ledger_entry(session, key)
    await session.commit()
    scheduled = _transition_total(telemetry_sink, "submit_cloud_job", "scheduled")
    resolved = _transition_total(telemetry_sink, "submit_cloud_job", "resolved")
    assert resolved == 1
    assert scheduled - resolved == 0


@pytest.mark.asyncio
async def test_insert_ledger_rows_if_absent_counts_scheduled_per_row_and_repeat_counts_nothing(
    session: AsyncSession, telemetry_sink: TelemetrySink
) -> None:
    """insert_ledger_rows_if_absent -- the reenqueue/recovery backfill's OWN ledger-write primitive
    (``tasks.recovery_backfill.backfill_ledger_from_saq_jobs``) -- counts "scheduled" once per row
    THIS call actually inserted, spanning multiple functions in one batched statement, and a
    re-run over the SAME rows (the backfill's own idempotency contract) counts nothing further."""
    rows = [
        {"key": "process_file:sched-balance-3a", "function": "process_file", "kwargs": {"file_id": "sched-balance-3a"}},
        {"key": "process_file:sched-balance-3b", "function": "process_file", "kwargs": {"file_id": "sched-balance-3b"}},
        {"key": "extract_file_metadata:sched-balance-3c", "function": "extract_file_metadata", "kwargs": {"file_id": "sched-balance-3c"}},
    ]
    await insert_ledger_rows_if_absent(session, rows)
    await session.commit()
    assert _transition_total(telemetry_sink, "analyze", "scheduled") == 2  # the two process_file rows
    assert _transition_total(telemetry_sink, "metadata", "scheduled") == 1  # the one extract_file_metadata row

    # A second pass over the IDENTICAL rows (the backfill re-running at the next controller boot)
    # must count nothing further -- every key already exists.
    await insert_ledger_rows_if_absent(session, rows)
    await session.commit()
    assert _transition_total(telemetry_sink, "analyze", "scheduled") == 2, "a repeat backfill pass must not double-count"
    assert _transition_total(telemetry_sink, "metadata", "scheduled") == 1

    for row in rows:
        await clear_ledger_entry(session, row["key"])
    await session.commit()
    assert _transition_total(telemetry_sink, "analyze", "resolved") == 2
    assert _transition_total(telemetry_sink, "metadata", "resolved") == 1
    assert _transition_total(telemetry_sink, "analyze", "scheduled") - _transition_total(telemetry_sink, "analyze", "resolved") == 0
    assert _transition_total(telemetry_sink, "metadata", "scheduled") - _transition_total(telemetry_sink, "metadata", "resolved") == 0


async def _seed_saq_jobs_table(session: AsyncSession) -> None:
    """A minimal ``saq_jobs`` stand-in (key/status only -- all the guarded clear ever reads)."""
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.execute(text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"))


async def _seed_saq_job_row(session: AsyncSession, *, key: str, status: str) -> None:
    await session.execute(text("INSERT INTO saq_jobs (key, status) VALUES (:key, :status)"), {"key": key, "status": status})


@pytest.mark.asyncio
async def test_clear_entry_counts_resolved_once_and_guarded_noop_counts_nothing(session: AsyncSession, telemetry_sink: TelemetrySink) -> None:
    """clear_ledger_entry: "resolved" fires only when a row is ACTUALLY deleted -- never on the
    phaze-3yln guarded no-op (a live same-key saq_jobs row protects the row), which was
    unconditionally counted before this bead."""
    key = "extract_file_metadata:resolved-balance-1"
    await _seed_saq_jobs_table(session)
    await upsert_ledger_entry(session, key=key, function="extract_file_metadata", kwargs={"file_id": "resolved-balance-1"})
    await _seed_saq_job_row(session, key=key, status="active")  # a LIVE same-key job protects the row
    await session.commit()
    assert _transition_total(telemetry_sink, "metadata", "scheduled") == 1

    # The guard makes this a no-op (the row survives) -- must count NOTHING.
    await clear_ledger_entry(session, key)
    await session.commit()
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none()
    assert row is not None, "the live-job guard must have protected the row"
    assert _transition_total(telemetry_sink, "metadata", "resolved") == 0, "a guarded no-op clear must not count resolved"

    # Retire the live job and clear for real: now it counts exactly once.
    await session.execute(text("UPDATE saq_jobs SET status = 'complete' WHERE key = :key"), {"key": key})
    await session.commit()
    await clear_ledger_entry(session, key)
    await session.commit()
    assert (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none() is None
    assert _transition_total(telemetry_sink, "metadata", "resolved") == 1

    # A THIRD clear of the now-absent key is also a no-op and must not count again.
    await clear_ledger_entry(session, key)
    await session.commit()
    assert _transition_total(telemetry_sink, "metadata", "resolved") == 1, "clearing an absent key a second time must not count"


@pytest.mark.asyncio
async def test_resolved_transition_key_parse_matches_ledger_key_for_function(session: AsyncSession, telemetry_sink: TelemetrySink) -> None:
    """phaze-qyqig acceptance 3: clear_ledger_entry recovers the function from a ledger key via
    ``key.split(":", 1)[0]`` -- a SEPARATE Python-side spelling from
    ``stage_status.ledger_key_for_function`` (a SQL expression BUILDER, correlated to
    ``FileRecord.id``, not itself reusable to parse a Python string). This pins the two spellings
    together: the key under test is built THROUGH ``ledger_key_for_function`` -- the same builder
    ``inflight_clause`` / the ledger reaper use -- executed for real against Postgres, not
    hand-written as an f-string, so a future change to that builder's format (delimiter, field
    order) that the split-based parse silently stopped matching would show up here as the wrong
    stage label, not as a passing test that never exercised the real builder.
    """
    from phaze.services.stage_status import ledger_key_for_function

    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=file_id.hex,
            original_path=f"/music/{file_id.hex}.mp3",
            original_filename=f"{file_id.hex}.mp3",
            current_path=f"/music/{file_id.hex}.mp3",
            file_type="mp3",
            file_size=1000,
        )
    )
    await session.flush()

    function = "extract_file_metadata"
    key = (
        await session.execute(select(ledger_key_for_function(function).label("key")).select_from(FileRecord).where(FileRecord.id == file_id))
    ).scalar_one()
    assert key == f"extract_file_metadata:{file_id}"  # sanity: still the documented "<function>:<id>" shape

    await upsert_ledger_entry(session, key=key, function=function, kwargs={"file_id": str(file_id)})
    await session.commit()
    assert _transition_total(telemetry_sink, "metadata", "scheduled") == 1

    await clear_ledger_entry(session, key)
    await session.commit()
    # The parse recovered "extract_file_metadata" from the REAL ledger_key_for_function output and
    # stage_label mapped it to "metadata" -- the same stage the scheduled half above counted under.
    assert _transition_total(telemetry_sink, "metadata", "resolved") == 1


# ---------------------------------------------------------------------------
# ledger_reaper (phaze.tasks.ledger_reaper.reap_resolved_ledger_rows)
# ---------------------------------------------------------------------------

_CREATE_SAQ_JOBS = (
    text("DROP TABLE IF EXISTS saq_jobs"),
    text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"),
)


async def _saq_table(session: AsyncSession) -> None:
    for stmt in _CREATE_SAQ_JOBS:
        await session.execute(stmt)
    await session.commit()


def _make_ctx() -> dict[str, Any]:
    from phaze.database import async_session

    return {"async_session": async_session}


def _reaper_key(stage: Stage, file: FileRecord) -> str:
    return f"{STAGE_TO_FUNCTION[stage.value]}:{file.id}"


async def _seed_reaper_ledger(session: AsyncSession, stage: Stage, file: FileRecord) -> str:
    func_name = STAGE_TO_FUNCTION[stage.value]
    session.add(SchedulingLedger(key=_reaper_key(stage, file), function=func_name, routing="agent", payload={"file_id": str(file.id)}))
    await session.commit()
    return _reaper_key(stage, file)


async def _reaper_ledger_keys(session: AsyncSession) -> set[str]:
    return set((await session.execute(select(SchedulingLedger.key))).scalars().all())


async def _analyze_done(session: AsyncSession, file: FileRecord) -> None:
    session.add(AnalysisResult(file_id=file.id, analysis_completed_at=datetime.now(UTC)))
    await session.commit()


@pytest.mark.asyncio
async def test_resolved_row_reap_counts_one_resolved_transition(session: AsyncSession, make_file, telemetry_sink: TelemetrySink) -> None:  # type: ignore[no-untyped-def]
    """The enrich-stage lane (``tasks.ledger_reaper``): reaping ONE resolved ``analyze`` row counts
    ONE "resolved" under stage="analyze" -- the real function is ``process_file``, and
    ``record_transition`` must map the reaper's per-lane label through ``stage_label``, not receive
    the Stage VALUE as if it were already a SAQ function name."""
    await _saq_table(session)
    file = await make_file()
    key = await _seed_reaper_ledger(session, Stage.ANALYZE, file)
    await _analyze_done(session, file)

    outcome = await reap_resolved_ledger_rows(_make_ctx())

    assert outcome["reaped"] == 1
    assert key not in await _reaper_ledger_keys(session)
    assert _transition_total(telemetry_sink, "analyze", "resolved") == 1


@pytest.mark.asyncio
async def test_reap_counts_resolved_once_per_row_not_once_per_lane(session: AsyncSession, make_file, telemetry_sink: TelemetrySink) -> None:  # type: ignore[no-untyped-def]
    """Two INDEPENDENT resolved ``analyze`` rows in the same reap pass count TWO "resolved" -- the
    per-lane ``add(rowcount, ...)`` batching must scale with rows actually deleted, not collapse to
    one observation per lane regardless of how many rows that lane's DELETE matched."""
    await _saq_table(session)
    file_a = await make_file()
    file_b = await make_file()
    key_a = await _seed_reaper_ledger(session, Stage.ANALYZE, file_a)
    key_b = await _seed_reaper_ledger(session, Stage.ANALYZE, file_b)
    await _analyze_done(session, file_a)
    await _analyze_done(session, file_b)

    outcome = await reap_resolved_ledger_rows(_make_ctx())

    assert outcome["analyze"] == 2
    assert {key_a, key_b}.isdisjoint(await _reaper_ledger_keys(session))
    assert _transition_total(telemetry_sink, "analyze", "resolved") == 2


@pytest.mark.asyncio
async def test_cloud_lane_reap_counts_resolved_under_the_keyed_function_name(  # type: ignore[no-untyped-def]
    session: AsyncSession, make_file, telemetry_sink: TelemetrySink
) -> None:
    """The cloud-lane pass (phaze-k95r7): ``s3_upload`` / ``push_file`` are file-keyed AGENT tasks
    with no ``Stage`` to map through, so ``stage_label`` leaves the function name AS the stage label
    (absent from ``_FUNCTION_TO_STAGE``). This is the 17-row-regression lane
    (``test_resolved_s3_upload_row_is_reaped`` in ``test_ledger_reaper.py``) with the telemetry
    assertion added."""
    await _saq_table(session)
    file = await make_file()
    await _analyze_done(session, file)
    key = f"s3_upload:{file.id}"
    session.add(SchedulingLedger(key=key, function="s3_upload", routing="agent", payload={"file_id": str(file.id)}))
    await session.commit()

    outcome = await reap_resolved_ledger_rows(_make_ctx())

    assert outcome["s3_upload"] == 1
    assert key not in await _reaper_ledger_keys(session)
    assert _transition_total(telemetry_sink, "s3_upload", "resolved") == 1


# ---------------------------------------------------------------------------
# scan_deletion cascade (phaze.services.scan_deletion.delete_scan_cascade)
# ---------------------------------------------------------------------------


def _make_scan_file(batch_id: uuid.UUID, suffix: str) -> FileRecord:
    path = f"/data/music/{uuid.uuid4().hex}-{suffix}.mp3"
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type="mp3",
        file_size=4096,
        batch_id=batch_id,
    )


@pytest.mark.asyncio
async def test_cascade_records_one_resolved_transition_per_deleted_ledger_row(session: AsyncSession, telemetry_sink: TelemetrySink) -> None:
    """phaze-qyqig: this cascade (phaze-u5dn's stranded-ledger-row purge) was one of three
    bulk-delete paths that resolved ledger rows uncounted. Seeds a batch's file with a
    ``process_file`` row AND an ``extract_file_metadata`` row (the two IMMORTAL stages
    ``test_cascade_purges_scheduling_ledger_rows_for_its_files`` in ``test_scan_deletion.py`` also
    covers), so the RETURNING-based tally must attribute each deleted row to its OWN
    function/stage, not just a flat count.
    """
    batch = ScanBatch(id=uuid.uuid4(), agent_id="test-fileserver", scan_path="/a", status=ScanStatus.COMPLETED.value)
    session.add(batch)
    await session.flush()

    file = _make_scan_file(batch.id, "a-media")
    session.add(file)
    await session.flush()

    session.add(
        SchedulingLedger(
            key=f"process_file:{file.id}", function="process_file", routing="agent", payload={"file_id": str(file.id), "agent_id": "nox"}
        )
    )
    session.add(
        SchedulingLedger(
            key=f"extract_file_metadata:{file.id}",
            function="extract_file_metadata",
            routing="agent",
            payload={"file_id": str(file.id), "agent_id": "nox"},
        )
    )
    await session.flush()

    counts = await delete_scan_cascade(session, batch.id)

    assert counts["scheduling_ledger"] == 2
    assert _transition_total(telemetry_sink, "analyze", "resolved") == 1  # the process_file row
    assert _transition_total(telemetry_sink, "metadata", "resolved") == 1  # the extract_file_metadata row


# ---------------------------------------------------------------------------
# backfill-cloud CAS delete (routers.pipeline.backfill / routers.pipeline.analysis)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_cas_delete_records_one_resolved_transition_per_deleted_ledger_row(
    client: AsyncClient, session: AsyncSession, telemetry_sink: TelemetrySink
) -> None:
    """phaze-qyqig: the backfill-cloud CAS delete was one of three bulk-delete paths that resolved
    ledger rows uncounted. ``_persist_failed_with_duration(with_ledger=True)`` seeds each
    candidate's ``process_file:<id>`` row via ``insert_ledger_if_absent`` -- itself fixed by this
    bead -- so the seed already counts one "scheduled" per candidate; backfilling them must count
    exactly as many "resolved", balancing the counter end to end through a real HTTP round trip.
    """
    candidates = await _persist_failed_with_duration(session, [_LONG, _LONG, _LONG])
    await seed_active_agent(session, "nox", kind="fileserver")
    wire_fakes(client)

    assert _transition_total(telemetry_sink, "analyze", "scheduled") == len(candidates)

    response = await client.post("/pipeline/backfill-cloud")
    assert response.status_code == 200
    await drain_router_background_tasks()

    for candidate in candidates:
        assert await _process_file_ledger_rows(session, candidate.id) == []

    resolved = _transition_total(telemetry_sink, "analyze", "resolved")
    scheduled = _transition_total(telemetry_sink, "analyze", "scheduled")
    assert resolved == len(candidates)
    assert scheduled - resolved == 0, "the ledger's own scheduled/resolved counter must balance"
