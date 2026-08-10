"""Per-enrich-stage orphaned/stuck (recovery-candidate) count -- UI-05 / D-05 (Phase 87, 87-08).

``services.pipeline.get_stage_orphan_counts`` returns ``{metadata, analyze}`` where each
value is the number of ``scheduling_ledger`` rows for that stage's function that are NEITHER live (a
queued/active ``saq_jobs`` key) NOR domain-completed NOR owned by an in-flight ``cloud_job`` -- EXACTLY
the set :func:`phaze.tasks.reenqueue.recover_orphaned_work` would re-enqueue for that stage. This is the
badge-recovery no-drift contract (T-87-31 / OQ-2): the helper reuses recovery's OWN classification
predicate, so the two agree by construction.

Load-bearing cells:

* **orphan == recovery-candidate** -- a scheduled-then-lost file (ledger row, no output, no live key)
  counts as one orphan for its stage; the count matches the inline recovery-candidate derivation over
  the SAME session (parity, no drift).
* **exclusions mirror recovery** -- a domain-completed file (done output), a force-SKIPPED stage
  (behavior 5), a live-keyed row, and an in-flight-cloud-owned analyze file each drop OUT of the count,
  exactly as recovery excludes them.
* **degrade-safe** -- a forced error inside the derivation returns all-zeros and leaves the session
  usable (SAVEPOINT rollback, never a poll 500 -- T-87-28).

Real-PG ``db_session`` fixture + ``*_test`` guard + seed helpers mirror
``tests/integration/test_stage_progress_buckets.py``. Run with real PG via
``just test-bucket integration`` on port 5433 (export ``TEST_DATABASE_URL``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.base import Base
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.services.pipeline import _BUSY_FUNCTION_TO_STAGE, _compute_stage_orphan_counts, get_live_job_keys, get_stage_orphan_counts
from phaze.services.scheduling_ledger import get_ledger_rows
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION
from phaze.tasks.reenqueue import (
    _CLOUD_OWNED_FUNCTIONS,
    _awaiting_cloud_job_ids,
    _build_done_sets,
    _in_flight_cloud_job_ids,
    _ledger_fids,
    _natural_id,
    is_domain_completed,
)
from tests.db_guard import integration_dsns, require_test_database


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.integration


# DSN derivation + destructive-DB guard, identical to test_stage_progress_buckets.py.
# DSN pair + destructive-DB guard, shared with every other integration module via `tests.db_guard`.
BROKER_DSN, SA_DSN = integration_dsns()
_TARGET_DB = require_test_database(SA_DSN, context="orphan-count integration tests")

_LEGACY_AGENT_ID = "test-fileserver"

# phaze-xwaj: ``saq_jobs`` is SAQ-owned and absent from ``Base.metadata`` (never Alembic-managed), and
# sibling suites create it with differing schemas, so it is pinned here with the shape this module
# controls (idiom from tests/integration/test_stage_progress_buckets.py / tests/shared/routers/test_pipeline.py).
# The drop+create is undone when the per-test transaction rolls back (db_session's teardown). Since the
# fix for phaze-xwaj makes _compute_stage_orphan_counts read ``saq_jobs`` DIRECTLY (raising, no longer
# via the degrade-safe get_live_job_keys wrapper), the table must actually EXIST for the happy-path tests
# below to exercise a real empty read rather than accidentally relying on a "table missing" degrade.
_PIN_SAQ_JOBS = (
    text("DROP TABLE IF EXISTS saq_jobs"),
    text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"),
)


async def _pin_saq_jobs(session: AsyncSession) -> None:
    """Pin a module-controlled, empty ``saq_jobs`` table so the live-keys read has a real relation."""
    for stmt in _PIN_SAQ_JOBS:
        await session.execute(stmt)
    await session.flush()


async def _live_job(session: AsyncSession, key: str, *, status: str = "active") -> None:
    """Register ``key`` as a LIVE broker row directly in the pinned ``saq_jobs`` table."""
    await session.execute(text("INSERT INTO saq_jobs (key, status) VALUES (:key, :status)"), {"key": key, "status": status})
    await session.flush()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables + the FK agent (copied from the bucket harness)."""
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    engine = create_async_engine(SA_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        if await session.get(Agent, _LEGACY_AGENT_ID) is None:
            session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
            await session.flush()
        await _pin_saq_jobs(session)
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _file(session: AsyncSession) -> FileRecord:
    """Seed a bare FileRecord and return it (id set)."""
    fid = uuid.uuid4()
    rec = FileRecord(
        agent_id="test-fileserver",
        id=fid,
        sha256_hash=uuid.uuid4().hex,
        original_path=f"/media/{fid}.mp3",
        original_filename=f"{fid}.mp3",
        current_path=f"/media/{fid}.mp3",
        file_type="mp3",
        file_size=1234,
    )
    session.add(rec)
    await session.flush()
    return rec


async def _ledger(session: AsyncSession, stage: str, file: FileRecord) -> str:
    """Seed a scheduling_ledger row on the deterministic ``<function>:<file_id>`` key; return the key."""
    func_name = STAGE_TO_FUNCTION[stage]
    key = f"{func_name}:{file.id}"
    session.add(
        SchedulingLedger(
            key=key,
            function=func_name,
            routing="agent",
            payload={"file_id": str(file.id)},
        )
    )
    await session.flush()
    return key


async def _recovery_candidate_counts(session: AsyncSession) -> dict[str, int]:
    """Compute the per-enrich-stage recovery-candidate counts INLINE the way recover_orphaned_work does.

    Uses recovery's OWN public predicate (``is_domain_completed`` + ``_build_done_sets`` + BOTH cloud
    exclusions ``_in_flight_cloud_job_ids`` and ``_awaiting_cloud_job_ids``) over the SAME session so the
    assertion is a real no-drift proof, not a restatement of the helper's internals. ``get_live_job_keys``
    degrades to an empty set when the SAQ-owned ``saq_jobs`` table is absent (the test DB only has the ORM
    tables), which is correct: no seeded row is live. phaze-w0yr: the ``awaiting`` fourth exclusion mirrors
    recover_orphaned_work's 83-06 filter -- omitting it re-opened the badge drift this test guards.
    """
    out = {"metadata": 0, "analyze": 0}
    rows = await get_ledger_rows(session)
    live = await get_live_job_keys(session)
    done_sets = await _build_done_sets(session, _ledger_fids(rows))
    in_flight = await _in_flight_cloud_job_ids(session)
    awaiting = await _awaiting_cloud_job_ids(session)
    for row in rows:
        stage = _BUSY_FUNCTION_TO_STAGE.get(row.function)
        if stage is None:
            continue
        # phaze-fc2l: cloud exclusions are SCOPED to the cloud-owned functions (only process_file among
        # the badge stages), matching recover_orphaned_work -- a metadata row for a cloud-busy
        # file still recovers, so the badge must count it.
        cloud_excluded = row.function in _CLOUD_OWNED_FUNCTIONS and (_natural_id(row) in in_flight or _natural_id(row) in awaiting)
        if row.key in live or is_domain_completed(row, done_sets) or cloud_excluded:
            continue
        out[stage] += 1
    return out


async def test_no_progress_file_is_one_orphan_for_its_stage(db_session: AsyncSession) -> None:
    """A scheduled-then-lost analyze file (ledger row, no analysis output, no live key) counts as 1 orphan."""
    f = await _file(db_session)
    await _ledger(db_session, "analyze", f)

    counts = await get_stage_orphan_counts(db_session)

    assert counts == {"metadata": 0, "analyze": 1}


async def test_non_badge_function_row_is_skipped_not_counted(db_session: AsyncSession) -> None:
    """A ``push_file`` ledger row -- not one of the two enrich badge functions -- is skipped entirely.

    ``_BUSY_FUNCTION_TO_STAGE`` maps only ``extract_file_metadata``/``process_file`` to a badge stage;
    ``push_file`` and the controller functions resolve to ``None`` and hit the ``continue`` branch,
    proven here by a lone ``push_file`` orphan row leaving BOTH badge counts at zero.
    """
    f = await _file(db_session)
    key = f"push_file:{f.id}"
    db_session.add(
        SchedulingLedger(
            key=key,
            function="push_file",
            routing="agent",
            payload={"file_id": str(f.id)},
        )
    )
    await db_session.flush()

    counts = await get_stage_orphan_counts(db_session)

    assert counts == {"metadata": 0, "analyze": 0}


async def test_orphan_count_matches_recovery_candidate_set(db_session: AsyncSession) -> None:
    """Over a mixed corpus the badge count equals the inline recovery-candidate set for EVERY stage (no drift)."""
    # metadata: one no-progress (orphan) + one domain-completed (metadata row present -> excluded).
    m_orphan = await _file(db_session)
    await _ledger(db_session, "metadata", m_orphan)
    m_done = await _file(db_session)
    await _ledger(db_session, "metadata", m_done)
    from phaze.models.metadata import FileMetadata

    db_session.add(FileMetadata(file_id=m_done.id, failed_at=None))  # done -> domain-complete

    # analyze: two no-progress (orphans) + one done (analysis_completed_at set -> excluded).
    for _ in range(2):
        a = await _file(db_session)
        await _ledger(db_session, "analyze", a)
    a_done = await _file(db_session)
    await _ledger(db_session, "analyze", a_done)
    db_session.add(AnalysisResult(file_id=a_done.id, analysis_completed_at=datetime.now(UTC)))

    # metadata: one more force-skipped (behavior 5 -> excluded).
    m_skip = await _file(db_session)
    await _ledger(db_session, "metadata", m_skip)
    db_session.add(StageSkip(id=uuid.uuid4(), file_id=m_skip.id, stage="metadata", reason="operator force-skip"))

    await db_session.flush()

    counts = await get_stage_orphan_counts(db_session)
    expected = await _recovery_candidate_counts(db_session)

    assert counts == expected
    assert counts == {"metadata": 1, "analyze": 2}


async def test_domain_completed_file_is_not_orphaned(db_session: AsyncSession) -> None:
    """A done analyze file (ledger row + completed analysis) is domain-complete -> excluded (recovery parity)."""
    f = await _file(db_session)
    await _ledger(db_session, "analyze", f)
    db_session.add(AnalysisResult(file_id=f.id, analysis_completed_at=datetime.now(UTC)))
    await db_session.flush()

    counts = await get_stage_orphan_counts(db_session)

    assert counts["analyze"] == 0


async def test_force_skipped_stage_is_not_orphaned(db_session: AsyncSession) -> None:
    """A force-SKIPPED metadata stage (ledger row + stage_skip) is domain-complete -> never re-driven (behavior 5)."""
    f = await _file(db_session)
    await _ledger(db_session, "metadata", f)
    db_session.add(StageSkip(id=uuid.uuid4(), file_id=f.id, stage="metadata", reason="operator force-skip"))
    await db_session.flush()

    counts = await get_stage_orphan_counts(db_session)

    assert counts["metadata"] == 0


async def test_live_keyed_row_is_not_orphaned(db_session: AsyncSession) -> None:
    """A ledger row whose deterministic key is a LIVE saq_jobs key is still in flight -> excluded.

    phaze-xwaj: seeds a REAL ``saq_jobs`` row (the core now reads the table directly, raising, rather
    than through the degrade-safe :func:`get_live_job_keys` wrapper -- a monkeypatch on that wrapper
    would no longer be observed here).
    """
    f = await _file(db_session)
    key = await _ledger(db_session, "analyze", f)
    await _live_job(db_session, key)

    counts = await get_stage_orphan_counts(db_session)

    assert counts["analyze"] == 0


async def test_in_flight_cloud_owned_file_is_not_orphaned(db_session: AsyncSession) -> None:
    """An analyze file with an in-flight cloud_job is owned by the backend reconcile path -> excluded (SCHED-05)."""
    f = await _file(db_session)
    await _ledger(db_session, "analyze", f)
    db_session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, backend_id=None, s3_key=None, status=CloudJobStatus.RUNNING.value))
    await db_session.flush()

    counts = await get_stage_orphan_counts(db_session)

    assert counts["analyze"] == 0


async def test_awaiting_cloud_held_file_is_not_orphaned(db_session: AsyncSession) -> None:
    """phaze-w0yr: a file HELD awaiting cloud (cloud_job status='awaiting') is drain-owned -> excluded, matching recovery.

    'awaiting' is deliberately NOT in _in_flight_cloud_job_ids' IN_FLIGHT set, so the pre-fix badge (which
    subtracted only in_flight) counted such files as phantom orphans that Recover could never clear -- the
    exact badge/recovery drift (the file is excluded from recover_orphaned_work's replay set via
    _awaiting_cloud_job_ids). The badge and the inline recovery-candidate re-derivation must BOTH read 0.
    MUTATION: dropping the awaiting exclusion in _compute_stage_orphan_counts -> counts['analyze']==1 -> RED.
    """
    f = await _file(db_session)
    await _ledger(db_session, "analyze", f)
    db_session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, backend_id=None, s3_key=None, status=CloudJobStatus.AWAITING.value))
    await db_session.flush()

    counts = await get_stage_orphan_counts(db_session)
    expected = await _recovery_candidate_counts(db_session)

    assert counts["analyze"] == 0
    assert counts == expected


async def test_in_flight_cloud_held_file_orphaned_metadata_still_counts(db_session: AsyncSession) -> None:
    """phaze-fc2l: an in-flight cloud_job scopes out ONLY the analyze re-drive -> a lost metadata row of that file IS an orphan.

    extract_file_metadata has no cloud second owner, so recovery re-drives it even when the file carries
    an in-flight cloud_job; the badge must count it. phaze-0jpe: this used to have an awaiting-set
    companion over the fingerprint stage; with fingerprinting removed, metadata is the only remaining
    badge stage that is NOT cloud-owned, so this cell alone carries the fc2l scoping guard.
    """
    f = await _file(db_session)
    await _ledger(db_session, "metadata", f)
    db_session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, backend_id=None, s3_key=None, status=CloudJobStatus.RUNNING.value))
    await db_session.flush()

    counts = await get_stage_orphan_counts(db_session)
    expected = await _recovery_candidate_counts(db_session)

    assert counts["metadata"] == 1
    assert counts == expected


async def test_degrades_to_zero_and_session_stays_usable(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A forced error inside the derivation returns all-zeros AND leaves the session usable (SAVEPOINT, T-87-28)."""
    f = await _file(db_session)
    await _ledger(db_session, "analyze", f)

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("forced derivation failure")

    # _build_done_sets is imported function-locally from reenqueue inside the helper; patch it there.
    monkeypatch.setattr("phaze.tasks.reenqueue._build_done_sets", _boom)

    counts = await get_stage_orphan_counts(db_session)

    assert counts == {"metadata": 0, "analyze": 0}

    # The SAVEPOINT rollback must NOT have poisoned the outer transaction -- a follow-up read succeeds.
    still_there = (await db_session.execute(select(SchedulingLedger.key))).scalars().all()
    assert f"{STAGE_TO_FUNCTION['analyze']}:{f.id}" in still_there


async def test_live_keys_read_failure_raises_from_the_core_instead_of_masquerading_as_empty(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """phaze-xwaj CORE REGRESSION: a failed live-keys read must propagate out of the RAISING core
    (:func:`_compute_stage_orphan_counts`), not silently degrade to an empty live set that would
    misclassify every genuinely in-flight ledger row as a phantom orphan.

    Before the fix, ``_compute_stage_orphan_counts`` called the degrade-safe
    :func:`get_live_job_keys` wrapper, whose own nested SAVEPOINT rollback un-aborted the enclosing
    transaction on a live-keys failure -- letting the REST of the (genuinely raising) derivation go
    on to "succeed" with ``live == set()``. Simulates that failure by pointing the module's
    ``_LIVE_KEYS_SQL`` at a relation that does not exist, isolated to just that one read (the other
    reads -- ``scheduling_ledger``, the done-sets, the cloud exclusions -- are untouched and would
    otherwise succeed, which is exactly the silent-inflation shape this regression guards against).
    """
    f = await _file(db_session)
    await _ledger(db_session, "analyze", f)  # a genuinely LIVE (in-flight) file, no live saq_jobs row seeded

    monkeypatch.setattr("phaze.services.pipeline._LIVE_KEYS_SQL", text("SELECT key FROM nonexistent_saq_jobs_probe"))

    with pytest.raises(Exception, match="nonexistent_saq_jobs_probe"):
        await _compute_stage_orphan_counts(db_session)

    # The swallowing public wrapper degrades to all-zero -- NEVER an inflated phantom-orphan count
    # (which is what "live == set()" from a masqueraded failure would have produced: analyze == 1).
    counts = await get_stage_orphan_counts(db_session)
    assert counts == {"metadata": 0, "analyze": 0}

    # The SAVEPOINT rollback must NOT have poisoned the outer transaction -- a follow-up read succeeds.
    still_there = (await db_session.execute(select(SchedulingLedger.key))).scalars().all()
    assert f"{STAGE_TO_FUNCTION['analyze']}:{f.id}" in still_there
