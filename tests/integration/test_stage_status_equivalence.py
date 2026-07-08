"""DERIV-04 anti-drift lock: the SQL ``ColumnElement`` twin == the DB-free Python resolver.

This is the drift-lock guaranteeing the two halves of the single-source predicate layer can
never diverge (Phase 78 D-04). For every ``(stage x fixture cell)`` it seeds real output rows in
a real Postgres, derives the per-stage status TWO independent ways --

* SQL side: run ``phaze.services.stage_status.stage_status_case(stage)`` (the ``ColumnElement``
  CASE ladder) inside a ``SELECT ... WHERE files.id == :file_id`` and read the label back;
* Python side: read the SAME rows into the plain-scalar dict shape and feed
  ``phaze.enums.stage.resolve_status(stage, scalars)`` (the Wave-1 twin) --

and asserts ``sql_status == py_status == expected``. The matrix covers every stage across
``{not_started, in_flight, done, failed}`` plus the load-bearing edge cells:

* analyze partial row (``analysis_completed_at`` NULL) -> ``not_started`` (DERIV-03);
* analyze ``failed_at`` set + a scheduling-ledger row -> ``in_flight`` (precedence proof);
* fingerprint ``[success, failed]`` -> ``done`` (DERIV-05 1:N aggregation);
* fingerprint ``[failed]``-only -> ``failed`` NOT ``done`` (ELIG-04 -- stays eligible);
* metadata failure-only row -> ``failed`` NOT ``done`` (D-03).

A separate ``savepoint_degrade`` test proves the corroborating ``saq_jobs`` read is
``begin_nested()`` SAVEPOINT-isolated: dropping ``saq_jobs`` mid-test degrades ``saq_detail`` to a
safe default with NO raise, and ``in_flight`` still resolves ``True`` from the durable ledger
(INFLIGHT-02).

Real-PG harness idiom mirrors ``tests/integration/conftest.py`` (DSN derivation +
connectivity-probe ``pytest.skip`` so a bare ``uv run pytest`` skips rather than errors when
Postgres is down). Run with real PG via ``just integration-test`` (ephemeral PG ``:5433``).

NOTE: ``phaze.services.stage_status`` is imported lazily INSIDE the runtime helpers (not at module
top) so ``pytest --co`` collects the matrix even before Task 2 lands the builders -- the file is
RED at RUN time (import error) only, which is the intended TDD RED state.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from typing import TYPE_CHECKING, Any
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.enums.stage import Stage, eligible, resolve_status
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.base import Base
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import RenameProposal
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.tracklist import Tracklist
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable


pytestmark = pytest.mark.integration


# Raw libpq broker DSN + SQLAlchemy async DSN, derived exactly as tests/integration/conftest.py
# (prefer PHAZE_QUEUE_URL / TEST_DATABASE_URL; fall back to the local dev DSN).
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")

_LEGACY_AGENT_ID = "legacy-application-server"


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables present and the FK agent seeded.

    Probes broker connectivity first and ``pytest.skip``s when Postgres is down (bare ``uv run
    pytest`` skips, not errors). ``Base.metadata.create_all`` makes the harness independent of
    Alembic having run on the ephemeral DB. Each test runs in one transaction that is rolled back
    at teardown, so the parametrized cells never contaminate one another.
    """
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
        # The files.agent_id FK (ON DELETE RESTRICT) needs the default agent to exist.
        session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
        await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


# --------------------------------------------------------------------------------------------------
# Seed helpers -- each writes the output rows (+ optional SchedulingLedger row on the deterministic
# "<function>:<file_id>" key) for one matrix cell and returns the file id as a string.
# --------------------------------------------------------------------------------------------------
async def _new_file(session: AsyncSession) -> uuid.UUID:
    fid = uuid.uuid4()
    session.add(
        FileRecord(
            id=fid,
            sha256_hash=uuid.uuid4().hex,
            original_path=f"/media/{fid}.mp3",
            original_filename=f"{fid}.mp3",
            current_path=f"/media/{fid}.mp3",
            file_type="mp3",
            file_size=1234,
            state="discovered",
        )
    )
    await session.flush()
    return fid


async def _seed_ledger(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> None:
    """Seed a scheduling_ledger row on the deterministic ``<function>:<file_id>`` key (INFLIGHT-01)."""
    func_name = STAGE_TO_FUNCTION[stage.value]
    session.add(
        SchedulingLedger(
            key=f"{func_name}:{file_id}",
            function=func_name,
            routing="agent",
            payload={"file_id": str(file_id)},
        )
    )
    await session.flush()


async def seed_analysis_none(session: AsyncSession) -> uuid.UUID:
    return await _new_file(session)


async def seed_analysis_partial(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    session.add(AnalysisResult(file_id=fid, analysis_completed_at=None))  # DERIV-03: completed_at NULL != done
    await session.flush()
    return fid


async def seed_analysis_completed(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    session.add(AnalysisResult(file_id=fid, analysis_completed_at=datetime.now(UTC)))
    await session.flush()
    return fid


async def seed_analysis_failed(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    session.add(AnalysisResult(file_id=fid, failed_at=datetime.now(UTC)))
    await session.flush()
    return fid


async def seed_analysis_failed_inflight(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    session.add(AnalysisResult(file_id=fid, failed_at=datetime.now(UTC)))
    await session.flush()
    await _seed_ledger(session, Stage.ANALYZE, fid)  # precedence: ledger row wins over failed
    return fid


async def seed_metadata_none(session: AsyncSession) -> uuid.UUID:
    return await _new_file(session)


async def seed_metadata_done(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    session.add(FileMetadata(file_id=fid, failed_at=None))
    await session.flush()
    return fid


async def seed_metadata_failed_only(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    session.add(FileMetadata(file_id=fid, failed_at=datetime.now(UTC)))  # D-03: failure-only != done
    await session.flush()
    return fid


async def seed_metadata_inflight(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    await _seed_ledger(session, Stage.METADATA, fid)
    return fid


async def seed_fp_none(session: AsyncSession) -> uuid.UUID:
    return await _new_file(session)


async def seed_fp_success(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    session.add(FingerprintResult(file_id=fid, engine="audfprint", status="success"))
    await session.flush()
    return fid


async def seed_fp_success_and_failed(session: AsyncSession) -> uuid.UUID:
    """DERIV-05: one success engine wins over a sibling failed engine -> done."""
    fid = await _new_file(session)
    session.add(FingerprintResult(file_id=fid, engine="audfprint", status="success"))
    session.add(FingerprintResult(file_id=fid, engine="panako", status="failed"))
    await session.flush()
    return fid


async def seed_fp_failed_only(session: AsyncSession) -> uuid.UUID:
    """ELIG-04: a failed-only fingerprint is NOT done, so it stays eligible for auto-retry."""
    fid = await _new_file(session)
    session.add(FingerprintResult(file_id=fid, engine="audfprint", status="failed"))
    await session.flush()
    return fid


async def seed_fp_inflight(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    await _seed_ledger(session, Stage.FINGERPRINT, fid)
    return fid


async def seed_tracklist_none(session: AsyncSession) -> uuid.UUID:
    return await _new_file(session)


async def seed_tracklist_done(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    session.add(Tracklist(external_id=uuid.uuid4().hex, source_url="https://example/tl", file_id=fid))
    await session.flush()
    return fid


async def seed_propose_none(session: AsyncSession) -> uuid.UUID:
    return await _new_file(session)


async def seed_propose_done(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    session.add(RenameProposal(file_id=fid, proposed_filename="better.mp3", status="pending"))
    await session.flush()
    return fid


async def seed_propose_failed_still_done(session: AsyncSession) -> uuid.UUID:
    """Presence semantics: done(propose) = a proposal EXISTS -- even a failed one (matches the Python twin)."""
    fid = await _new_file(session)
    session.add(RenameProposal(file_id=fid, proposed_filename="better.mp3", status="failed"))
    await session.flush()
    return fid


async def seed_review_none(session: AsyncSession) -> uuid.UUID:
    return await _new_file(session)


async def seed_review_done(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    session.add(RenameProposal(file_id=fid, proposed_filename="better.mp3", status="approved"))
    await session.flush()
    return fid


async def seed_apply_none(session: AsyncSession) -> uuid.UUID:
    return await _new_file(session)


async def seed_apply_done(session: AsyncSession) -> uuid.UUID:
    fid = await _new_file(session)
    proposal = RenameProposal(file_id=fid, proposed_filename="better.mp3", status="executed")
    session.add(proposal)
    await session.flush()
    session.add(
        ExecutionLog(
            proposal_id=proposal.id,
            operation="rename",
            source_path="/media/old.mp3",
            destination_path="/media/better.mp3",
            sha256_verified=True,
            status="completed",
        )
    )
    await session.flush()
    return fid


# (stage, seed_fn, expected_status) -- one row per matrix cell. Covers every stage across the four
# statuses plus the required edge cells (analyze partial/precedence, fingerprint DERIV-05 + ELIG-04,
# metadata D-03).
CASES: list[tuple[Stage, Callable[[AsyncSession], Awaitable[uuid.UUID]], str]] = [
    # analyze
    (Stage.ANALYZE, seed_analysis_none, "not_started"),
    (Stage.ANALYZE, seed_analysis_partial, "not_started"),  # completed_at NULL edge
    (Stage.ANALYZE, seed_analysis_completed, "done"),
    (Stage.ANALYZE, seed_analysis_failed, "failed"),
    (Stage.ANALYZE, seed_analysis_failed_inflight, "in_flight"),  # precedence: ledger wins
    # metadata
    (Stage.METADATA, seed_metadata_none, "not_started"),
    (Stage.METADATA, seed_metadata_done, "done"),
    (Stage.METADATA, seed_metadata_failed_only, "failed"),  # D-03 failure-only != done
    (Stage.METADATA, seed_metadata_inflight, "in_flight"),
    # fingerprint
    (Stage.FINGERPRINT, seed_fp_none, "not_started"),
    (Stage.FINGERPRINT, seed_fp_success, "done"),
    (Stage.FINGERPRINT, seed_fp_success_and_failed, "done"),  # DERIV-05 aggregation
    (Stage.FINGERPRINT, seed_fp_failed_only, "failed"),  # ELIG-04 not-done
    (Stage.FINGERPRINT, seed_fp_inflight, "in_flight"),
    # tracklist (downstream presence)
    (Stage.TRACKLIST, seed_tracklist_none, "not_started"),
    (Stage.TRACKLIST, seed_tracklist_done, "done"),
    # propose (downstream presence)
    (Stage.PROPOSE, seed_propose_none, "not_started"),
    (Stage.PROPOSE, seed_propose_done, "done"),
    (Stage.PROPOSE, seed_propose_failed_still_done, "done"),
    # review (downstream presence)
    (Stage.REVIEW, seed_review_none, "not_started"),
    (Stage.REVIEW, seed_review_done, "done"),
    # apply (execution_log completed, joined through proposals)
    (Stage.APPLY, seed_apply_none, "not_started"),
    (Stage.APPLY, seed_apply_done, "done"),
]


# --------------------------------------------------------------------------------------------------
# Python-side scalar readers -- read the SAME rows back into the plain-scalar dict resolve_status wants.
# --------------------------------------------------------------------------------------------------
async def _ledger_inflight(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> bool:
    func_name = STAGE_TO_FUNCTION.get(stage.value)
    if func_name is None:
        return False
    row = await session.execute(select(SchedulingLedger.key).where(SchedulingLedger.key == f"{func_name}:{file_id}"))
    return row.first() is not None


async def load_scalars(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> dict[str, Any]:
    """Read the persisted rows into the DB-free scalar shape ``resolve_status`` consumes."""
    inflight = await _ledger_inflight(session, stage, file_id)
    if stage is Stage.ANALYZE:
        row = (
            await session.execute(select(AnalysisResult.analysis_completed_at, AnalysisResult.failed_at).where(AnalysisResult.file_id == file_id))
        ).first()
        return {"completed_at": row[0] if row else None, "failed_at": row[1] if row else None, "inflight": inflight}
    if stage is Stage.METADATA:
        row = (await session.execute(select(FileMetadata.failed_at).where(FileMetadata.file_id == file_id))).first()
        return {"row_present": row is not None, "failed_at": row[0] if row else None, "inflight": inflight}
    if stage is Stage.FINGERPRINT:
        rows = (await session.execute(select(FingerprintResult.status).where(FingerprintResult.file_id == file_id))).all()
        return {"engine_statuses": [r[0] for r in rows], "inflight": inflight}
    if stage is Stage.TRACKLIST:
        present = (await session.execute(select(Tracklist.id).where(Tracklist.file_id == file_id))).first() is not None
        return {"row_present": present, "failed": False, "inflight": inflight}
    if stage in (Stage.PROPOSE, Stage.REVIEW):
        present = (await session.execute(select(RenameProposal.id).where(RenameProposal.file_id == file_id))).first() is not None
        failed = (
            await session.execute(select(RenameProposal.id).where(RenameProposal.file_id == file_id, RenameProposal.status == "failed"))
        ).first() is not None
        return {"row_present": present, "failed": failed, "inflight": inflight}
    # apply: execution_log completed, joined through proposals (execution_log has NO file_id)
    present = (
        await session.execute(
            select(ExecutionLog.id)
            .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
            .where(RenameProposal.file_id == file_id, ExecutionLog.status == "completed")
        )
    ).first() is not None
    failed = (
        await session.execute(
            select(ExecutionLog.id)
            .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
            .where(RenameProposal.file_id == file_id, ExecutionLog.status == "failed")
        )
    ).first() is not None
    return {"row_present": present, "failed": failed, "inflight": inflight}


async def eval_sql_status(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> str:
    """Run the ``ColumnElement`` CASE ladder in a SELECT and read the derived label back."""
    from phaze.services.stage_status import stage_status_case  # lazy: keeps --co green in the RED state

    result = await session.execute(select(stage_status_case(stage)).where(FileRecord.id == file_id))
    return str(result.scalar_one())


@pytest.mark.parametrize("stage,seed_fn,expected", CASES)
async def test_sql_equals_python(
    db_session: AsyncSession,
    stage: Stage,
    seed_fn: Callable[[AsyncSession], Awaitable[uuid.UUID]],
    expected: str,
) -> None:
    """DERIV-04 drift-lock: for every matrix cell, SQL-derived == Python-derived == expected."""
    file_id = await seed_fn(db_session)
    sql_status = await eval_sql_status(db_session, stage, file_id)
    scalars = await load_scalars(db_session, stage, file_id)
    py_status = resolve_status(stage, scalars)
    assert sql_status == py_status == expected


async def test_failed_fingerprint_stays_eligible(db_session: AsyncSession) -> None:
    """ELIG-04: a failed-only fingerprint is NOT done, so ``eligible()`` keeps it eligible for retry."""
    file_id = await seed_fp_failed_only(db_session)
    sql_status = await eval_sql_status(db_session, Stage.FINGERPRINT, file_id)
    assert sql_status == "failed"
    status_map = {Stage.FINGERPRINT: resolve_status(Stage.FINGERPRINT, await load_scalars(db_session, Stage.FINGERPRINT, file_id))}
    assert eligible(status_map, Stage.FINGERPRINT) is True


async def _eval_inflight(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> bool:
    from phaze.services.stage_status import inflight_clause  # lazy: keeps --co green in the RED state

    result = await session.execute(select(inflight_clause(stage)).where(FileRecord.id == file_id))
    return bool(result.scalar_one())


async def test_inflight_savepoint_degrade(db_session: AsyncSession) -> None:
    """INFLIGHT-02: the corroborating ``saq_jobs`` read is SAVEPOINT-isolated and degrade-safe.

    A file is ``in_flight`` via a durable ledger row. ``saq_detail`` enriches the queued-vs-active
    detail while ``saq_jobs`` is present; after the table is dropped mid-test it degrades to the
    safe default with NO raise -- and ``in_flight`` still reads ``True`` from the ledger in BOTH
    cases (the ledger, not ``saq_jobs``, is the authoritative boolean -- D-01).
    """
    from phaze.services.stage_status import saq_detail  # lazy: keeps --co green in the RED state

    file_id = await seed_analysis_failed_inflight(db_session)  # ledger row on process_file:<file_id>

    # (a) present saq_jobs -- a minimal stand-in (saq_detail only reads key/status). Rebuilt inside the
    # rolled-back test transaction so a real broker table (if any) is restored at teardown.
    await db_session.execute(text("DROP TABLE IF EXISTS saq_jobs CASCADE"))
    await db_session.execute(text("CREATE TABLE saq_jobs (key text, status text)"))
    await db_session.execute(text("INSERT INTO saq_jobs (key, status) VALUES ('process_file:a', 'queued'), ('process_file:b', 'active')"))
    await db_session.flush()

    assert await saq_detail(db_session) == {"queued": 1, "active": 1}
    assert await _eval_inflight(db_session, Stage.ANALYZE, file_id) is True

    # (b) drop saq_jobs -> saq_detail degrades to the safe default, no exception surfaced.
    await db_session.execute(text("DROP TABLE saq_jobs CASCADE"))
    await db_session.flush()

    assert await saq_detail(db_session) == {"queued": 0, "active": 0}
    # The SAVEPOINT rolled back alone: the outer transaction is intact and the ledger still resolves in_flight.
    assert await _eval_inflight(db_session, Stage.ANALYZE, file_id) is True
