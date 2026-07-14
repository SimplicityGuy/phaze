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

from phaze.enums.stage import Stage, domain_completed, eligible, resolve_status
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.base import Base
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import RenameProposal
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
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

_LEGACY_AGENT_ID = "test-fileserver"


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
        # The files.agent_id FK (ON DELETE RESTRICT) needs the default agent to exist. Seed it
        # IDEMPOTENTLY: under 92-03's session-scoped engine the shared ``async_engine`` fixture (and
        # ``committed_db``'s teardown re-seed) COMMIT a ``test-fileserver`` Agent for the whole session,
        # so a blind ``INSERT`` collided on ``pk_agents`` whenever an earlier bucket cell instantiated
        # that fixture -- the 74 setup ERRORs seen only in the FULL integration bucket (the file passes
        # 59/59 in isolation, where no committed row exists). Get-or-insert satisfies the FK either way:
        # reuse the committed parent when present, else seed our own within this rolled-back transaction.
        if await session.get(Agent, _LEGACY_AGENT_ID) is None:
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
            agent_id="test-fileserver",
            id=fid,
            sha256_hash=uuid.uuid4().hex,
            original_path=f"/media/{fid}.mp3",
            original_filename=f"{fid}.mp3",
            current_path=f"/media/{fid}.mp3",
            file_type="mp3",
            file_size=1234,
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


async def seed_analysis_skipped_over_failed(session: AsyncSession) -> uuid.UUID:
    """D-08 precedence cell: a terminally-failed analyze + a ``stage_skip`` marker derives SKIPPED not FAILED.

    The force-skip writer is ADDITIVE (never clears ``failed_at``), so ``failed_clause`` is still True --
    the CASE / branch order ``done ≻ skipped ≻ failed`` (not the writer) is what makes ``skipped`` win
    (Pitfall 2). This is the single load-bearing precedence cell of Plan 03.
    """
    fid = await _new_file(session)
    session.add(AnalysisResult(file_id=fid, failed_at=datetime.now(UTC)))  # terminally failed
    await session.flush()
    session.add(StageSkip(file_id=fid, stage="analyze", reason="corrupt source"))  # additive marker
    await session.flush()
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


async def seed_metadata_skipped(session: AsyncSession) -> uuid.UUID:
    """D-08: a ``stage_skip(metadata)`` marker on an otherwise not-started file derives SKIPPED."""
    fid = await _new_file(session)
    session.add(StageSkip(file_id=fid, stage="metadata", reason="operator force-skip"))
    await session.flush()
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


async def seed_fp_skipped(session: AsyncSession) -> uuid.UUID:
    """D-08: a ``stage_skip(fingerprint)`` marker on an otherwise not-started file derives SKIPPED."""
    fid = await _new_file(session)
    session.add(StageSkip(file_id=fid, stage="fingerprint", reason="operator force-skip"))
    await session.flush()
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
    (Stage.ANALYZE, seed_analysis_skipped_over_failed, "skipped"),  # D-08 skipped ≻ failed precedence
    # metadata
    (Stage.METADATA, seed_metadata_none, "not_started"),
    (Stage.METADATA, seed_metadata_done, "done"),
    (Stage.METADATA, seed_metadata_failed_only, "failed"),  # D-03 failure-only != done
    (Stage.METADATA, seed_metadata_inflight, "in_flight"),
    (Stage.METADATA, seed_metadata_skipped, "skipped"),  # D-08 force-skip marker
    # fingerprint
    (Stage.FINGERPRINT, seed_fp_none, "not_started"),
    (Stage.FINGERPRINT, seed_fp_success, "done"),
    (Stage.FINGERPRINT, seed_fp_success_and_failed, "done"),  # DERIV-05 aggregation
    (Stage.FINGERPRINT, seed_fp_failed_only, "failed"),  # ELIG-04 not-done
    (Stage.FINGERPRINT, seed_fp_inflight, "in_flight"),
    (Stage.FINGERPRINT, seed_fp_skipped, "skipped"),  # D-08 force-skip marker
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


async def _skipped_marker(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> bool:
    """Read the D-08 ``stage_skip`` marker for the enrich ``(file, stage)`` into the DB-free scalar shape.

    Mirrors the SQL twin's :func:`phaze.services.stage_status.skipped_clause` correlated-``exists`` probe
    (marker-row existence = the fact) so the Python side of the equivalence sees the SAME ``skipped``
    bool the ``ColumnElement`` CASE ladder does.
    """
    row = (await session.execute(select(StageSkip.id).where(StageSkip.file_id == file_id, StageSkip.stage == stage.value))).first()
    return row is not None


async def load_scalars(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> dict[str, Any]:
    """Read the persisted rows into the DB-free scalar shape ``resolve_status`` consumes."""
    inflight = await _ledger_inflight(session, stage, file_id)
    if stage is Stage.ANALYZE:
        row = (
            await session.execute(select(AnalysisResult.analysis_completed_at, AnalysisResult.failed_at).where(AnalysisResult.file_id == file_id))
        ).first()
        return {
            "completed_at": row[0] if row else None,
            "failed_at": row[1] if row else None,
            "inflight": inflight,
            "skipped": await _skipped_marker(session, stage, file_id),
        }
    if stage is Stage.METADATA:
        row = (await session.execute(select(FileMetadata.failed_at).where(FileMetadata.file_id == file_id))).first()
        return {
            "row_present": row is not None,
            "failed_at": row[0] if row else None,
            "inflight": inflight,
            "skipped": await _skipped_marker(session, stage, file_id),
        }
    if stage is Stage.FINGERPRINT:
        rows = (await session.execute(select(FingerprintResult.status).where(FingerprintResult.file_id == file_id))).all()
        return {"engine_statuses": [r[0] for r in rows], "inflight": inflight, "skipped": await _skipped_marker(session, stage, file_id)}
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


# --------------------------------------------------------------------------------------------------
# D-17 domain_completed drift-lock: the DB-free ``domain_completed`` table twin == the SQL
# ``domain_completed_clause`` twin, per seeded cell. Extends the DERIV-04 anti-drift guarantee to the
# terminality axis so ``FAILURE_IS_TERMINAL`` can never drift between its Python and SQL readers (the
# 44.5K over-enqueue guard rests on the two agreeing).
#
# SCOPE: the two ``domain_completed`` twins are LEDGER-AGNOSTIC by design -- ``domain_completed_clause``
# is ``or_(done_clause, failed_clause)`` (terminal) / ``done_clause`` (fingerprint), with NO
# ``inflight_clause`` disjunct; the Python ``domain_completed`` reads a resolved status but never treats
# IN_FLIGHT as complete. in_flight precedence is layered separately at the ``resolve_status`` / ``eligible``
# level, so the equivalence holds ONLY for non-in-flight rows. These cells therefore reuse the enrich-stage
# seed fns EXCLUDING the ``*_inflight`` seeds. ``FAILURE_IS_TERMINAL`` is defined only for the three enrich
# stages (D-15), so only enrich cells are exercised.
#
# D-11 REJECTED-OPTION RATIONALE (Phase 80, READ-03 -- why this test is NOT the lock, and what the
# ``*_inflight`` exclusion above protects): a tempting "fix" (WR-02's own literal suggestion) is to add a
# ``~inflight_clause(stage)`` conjunct to ``domain_completed_clause`` so an in-flight row never reads as
# domain-complete. That is a TRAP and MUST NEVER be done. ``inflight_clause`` is *scheduling-ledger row
# existence* (``stage_status.py``), and in ``recover_orphaned_work`` EVERY candidate is a ledger row by
# construction -- so the added ``~inflight_clause`` disjunct would make ``domain_completed`` return False
# for EVERY recovery candidate, silently DISABLING the secondary over-enqueue net wholesale (the 44.5K
# incident class, reintroduced). Crucially, the trap is INVISIBLE HERE: the drain and the "Awaiting cloud"
# count card already read ``... AND ~inflight_clause``, and these DERIV-04 cells deliberately EXCLUDE the
# ``*_inflight`` seeds -- so adding ``~inflight_clause`` to ``domain_completed_clause`` is a pure no-op for
# every row this equivalence test (and the drain/card) ever sees, and the test STAYS GREEN. The real lock
# is the recovery-LAYER regression in ``tests/analyze/tasks/test_recovery.py`` (Plan 80-04): because its
# candidates ARE ledger rows, the trap makes ``domain_completed`` False for all of them and its Cell B goes
# RED. Keep the ``*_inflight`` exclusion (it is what makes this test correctly ledger-agnostic); do NOT
# "harden" it by adding in-flight cells here to try to catch the trap -- that responsibility lives at the
# recovery layer, by design.
DOMAIN_COMPLETED_CASES: list[tuple[Stage, Callable[[AsyncSession], Awaitable[uuid.UUID]], bool]] = [
    # analyze -- terminal failure: DONE and FAILED both count as domain-complete.
    (Stage.ANALYZE, seed_analysis_none, False),
    (Stage.ANALYZE, seed_analysis_partial, False),  # completed_at NULL -> not_started -> not complete
    (Stage.ANALYZE, seed_analysis_completed, True),
    (Stage.ANALYZE, seed_analysis_failed, True),  # FAIL-01: analyze failure is TERMINAL
    (Stage.ANALYZE, seed_analysis_skipped_over_failed, True),  # D-08: skipped is domain-complete (recovery must not re-run)
    # metadata -- terminal failure: a failure-only row IS domain-complete (recovery must not re-run).
    (Stage.METADATA, seed_metadata_none, False),
    (Stage.METADATA, seed_metadata_done, True),
    (Stage.METADATA, seed_metadata_failed_only, True),  # metadata failure is TERMINAL
    (Stage.METADATA, seed_metadata_skipped, True),  # D-08: skipped is domain-complete
    # fingerprint -- NON-terminal failure: DONE completes, a failed-only row does NOT (auto-retries).
    (Stage.FINGERPRINT, seed_fp_none, False),
    (Stage.FINGERPRINT, seed_fp_success, True),
    (Stage.FINGERPRINT, seed_fp_success_and_failed, True),  # DERIV-05: success wins -> done -> complete
    (Stage.FINGERPRINT, seed_fp_failed_only, False),  # FAIL-04: fingerprint failure is NOT terminal
    (Stage.FINGERPRINT, seed_fp_skipped, True),  # D-08: skipped is domain-complete even though FP failure is not terminal
]


async def eval_sql_domain_completed(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> bool:
    """Run the ``domain_completed_clause`` predicate in a SELECT and read the boolean back."""
    from phaze.services.stage_status import domain_completed_clause  # lazy: keeps --co green in the RED state

    result = await session.execute(select(domain_completed_clause(stage)).where(FileRecord.id == file_id))
    return bool(result.scalar_one())


@pytest.mark.parametrize("stage,seed_fn,expected", DOMAIN_COMPLETED_CASES)
async def test_domain_completed_sql_equals_python(
    db_session: AsyncSession,
    stage: Stage,
    seed_fn: Callable[[AsyncSession], Awaitable[uuid.UUID]],
    expected: bool,
) -> None:
    """D-17 drift-lock: for every seeded cell, SQL ``domain_completed_clause`` == Python ``domain_completed`` == expected."""
    file_id = await seed_fn(db_session)
    sql_complete = await eval_sql_domain_completed(db_session, stage, file_id)
    py_status = resolve_status(stage, await load_scalars(db_session, stage, file_id))
    py_complete = domain_completed({stage: py_status}, stage)
    assert sql_complete == py_complete == expected


# --------------------------------------------------------------------------------------------------
# READ-01 eligibility drift-lock: the DB-free ``eligible`` table twin == the SQL ``eligible_clause``
# twin, per seeded cell. Extends the DERIV-04 anti-drift guarantee to the ELIGIBILITY axis so
# ``ELIGIBLE_AFTER_FAILURE`` can never drift between its Python and SQL readers -- the 44.5K
# over-enqueue guard rests on the two agreeing on the analyze carve-out (ELIG-03).
#
# SCOPE: ``eligible_clause`` is enrich-only (keyed on ``ELIGIBLE_AFTER_FAILURE``, three keys), so only
# enrich cells are exercised. Unlike ``DOMAIN_COMPLETED_CASES`` these cells DO include the ``*_inflight``
# seeds: ``eligible`` folds the ``in_flight`` precedence in directly (an in-flight stage is ineligible),
# and ``eligible_clause`` mirrors it via the ``~inflight_clause`` conjunct, so the equivalence holds for
# the in-flight rows too.
#
# The single load-bearing anti-drift cell is ``(Stage.ANALYZE, seed_analysis_failed, False)``: it goes
# RED if a future edit drops the analyze ``~failed_clause`` conjunct (the ELIG-03 44.5K over-enqueue
# guard). METADATA/FINGERPRINT keep their FAILED rows eligible (ELIGIBLE_AFTER_FAILURE True -- ELIG-04
# auto-retry). Reuses the enrich-stage seed fns verbatim -- no new fixtures.
ELIGIBLE_CASES: list[tuple[Stage, Callable[[AsyncSession], Awaitable[uuid.UUID]], bool]] = [
    # metadata: eligible when NOT_STARTED or FAILED; not when DONE or IN_FLIGHT (ELIG-01/04).
    (Stage.METADATA, seed_metadata_none, True),
    (Stage.METADATA, seed_metadata_done, False),
    (Stage.METADATA, seed_metadata_failed_only, True),  # ELIGIBLE_AFTER_FAILURE True -> stays eligible
    (Stage.METADATA, seed_metadata_inflight, False),
    (Stage.METADATA, seed_metadata_skipped, False),  # D-08: a skipped stage leaves the pending set
    # fingerprint: same shape (ELIG-04 -- a failed-only fingerprint stays eligible for auto-retry).
    (Stage.FINGERPRINT, seed_fp_none, True),
    (Stage.FINGERPRINT, seed_fp_success, False),
    (Stage.FINGERPRINT, seed_fp_success_and_failed, False),  # DERIV-05 success wins -> done -> ineligible
    (Stage.FINGERPRINT, seed_fp_failed_only, True),  # ELIG-04 failed-only stays eligible
    (Stage.FINGERPRINT, seed_fp_inflight, False),
    (Stage.FINGERPRINT, seed_fp_skipped, False),  # D-08: a skipped stage leaves the pending set
    # analyze: eligible ONLY when NOT_STARTED -- a FAILED analyze is TERMINAL (ELIG-03, 44.5K guard).
    (Stage.ANALYZE, seed_analysis_none, True),
    (Stage.ANALYZE, seed_analysis_partial, True),  # completed_at NULL -> not_started -> eligible
    (Stage.ANALYZE, seed_analysis_completed, False),
    (Stage.ANALYZE, seed_analysis_failed, False),  # <- the load-bearing ELIG-03 carve-out cell
    (Stage.ANALYZE, seed_analysis_failed_inflight, False),  # precedence: ledger row -> in_flight -> ineligible
    (Stage.ANALYZE, seed_analysis_skipped_over_failed, False),  # D-08: skipped ≻ failed, still leaves the pending set
]


async def eval_sql_eligible(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> bool:
    """Run the ``eligible_clause`` predicate in a SELECT and read the boolean back."""
    from phaze.services.stage_status import eligible_clause  # lazy: keeps --co green in the RED state

    result = await session.execute(select(eligible_clause(stage)).where(FileRecord.id == file_id))
    return bool(result.scalar_one())


@pytest.mark.parametrize("stage,seed_fn,expected", ELIGIBLE_CASES)
async def test_eligible_sql_equals_python(
    db_session: AsyncSession,
    stage: Stage,
    seed_fn: Callable[[AsyncSession], Awaitable[uuid.UUID]],
    expected: bool,
) -> None:
    """READ-01 drift-lock: for every seeded cell, SQL ``eligible_clause`` == Python ``eligible`` == expected."""
    file_id = await seed_fn(db_session)
    sql_eligible = await eval_sql_eligible(db_session, stage, file_id)
    py_status = resolve_status(stage, await load_scalars(db_session, stage, file_id))
    py_eligible = eligible({stage: py_status}, stage)
    assert sql_eligible == py_eligible == expected


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
