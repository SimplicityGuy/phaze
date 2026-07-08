"""Hermetic fixture-corpus CI gate for the state↔derived shadow-compare core (Phase 79, MIG-02).

This is THE standing gate phases 80-90 keep green. It seeds a real Postgres corpus and drives the
ONE shared assertion core (:func:`phaze.services.shadow_compare.run_shadow_compare` over the
:data:`~phaze.services.shadow_compare.INVARIANTS` registry -- D-01: no second copy of the assertion
logic) through five properties:

* **divergent** -- every HARD invariant flags a seeded ``state=X`` file with NO backing derived row
  (non-vacuous RED cells: proves the gate is not silently green);
* **consistent** -- every HARD invariant with the correct backing row yields zero HARD divergence
  (no false positives);
* **implication** -- a file at ``state='metadata_extracted'`` that ALSO carries a completed analysis
  row (more-derived than the scalar) does NOT flag (implication, NOT equality);
* **allowlist** -- a seeded ``FINGERPRINTED`` / ``LOCAL_ANALYZING`` divergence is COUNTED and printed
  "expected divergence" but NEVER contributes to ``hard_fail_total`` (D-06);
* **core** -- a DB-free registry cell locking D-04 comprehensiveness (every non-DISCOVERED FileState
  value has an entry; DISCOVERED absent) and the D-06 soft allowlist (== {fingerprinted, local_analyzing}).

Real-PG harness idiom mirrors ``tests/integration/test_stage_status_equivalence.py`` (DSN derivation +
connectivity-probe ``pytest.skip`` so a bare ``uv run pytest`` skips rather than errors when Postgres
is down; ``Base.metadata.create_all`` + a seeded ``legacy-application-server`` Agent for the
``files.agent_id`` RESTRICT FK; per-test rollback). Run with real PG via ``just integration-test``
(ephemeral PG ``:5433``) or ``just test-bucket integration``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.base import Base
from phaze.models.cloud_job import CloudJob
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import RenameProposal
from phaze.services.shadow_compare import INVARIANTS, Invariant, InvariantResult, Report, run_shadow_compare


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator


pytestmark = pytest.mark.integration


# Raw libpq broker DSN + SQLAlchemy async DSN, derived exactly as tests/integration/conftest.py.
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")

_LEGACY_AGENT_ID = "legacy-application-server"

# The non-soft (gated) invariants -- each gets a non-vacuous divergent RED cell + a consistent GREEN cell.
HARD_INVARIANTS: list[Invariant] = [inv for inv in INVARIANTS if not inv.soft]
_HARD_IDS = [inv.name for inv in HARD_INVARIANTS]


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables present and the FK agent seeded.

    Probes broker connectivity first and ``pytest.skip``s when Postgres is down. ``create_all`` makes
    the harness independent of Alembic. Each test runs in one rolled-back transaction, so the
    parametrized cells never contaminate one another.
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
        session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
        await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


async def _new_file(session: AsyncSession, *, state: str) -> uuid.UUID:
    """Seed a bare FileRecord at ``state`` with NO backing derived rows; return its id."""
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
            state=state,
        )
    )
    await session.flush()
    return fid


async def _seed_backing(session: AsyncSession, name: str, file_id: uuid.UUID) -> None:
    """Seed the CORRECT derived-side row that satisfies the HARD invariant ``name`` for ``file_id``.

    Raises if a HARD invariant has no seeder -- so a future HARD invariant added to the registry
    without a consistent-cell backing fails this test loud (guards D-04 comprehensiveness).
    """
    now = datetime.now(UTC)
    if name == "metadata_extracted":
        session.add(FileMetadata(file_id=file_id, failed_at=None))
    elif name == "analyzed":
        session.add(AnalysisResult(file_id=file_id, analysis_completed_at=now))
    elif name == "analysis_failed":
        session.add(AnalysisResult(file_id=file_id, failed_at=now))
    elif name == "proposal_generated":
        session.add(RenameProposal(file_id=file_id, proposed_filename="p.mp3", status="pending"))
    elif name == "awaiting_cloud":
        session.add(CloudJob(file_id=file_id, status="awaiting"))
    elif name == "pushing":
        session.add(CloudJob(file_id=file_id, status="uploading"))  # row-existence only (any status)
    elif name == "pushed":
        session.add(CloudJob(file_id=file_id, status="uploaded"))
    elif name == "duplicate_resolved":
        session.add(DedupResolution(file_id=file_id))
    elif name in ("approved", "rejected", "executed", "failed"):
        session.add(RenameProposal(file_id=file_id, proposed_filename="p.mp3", status=name))
    elif name == "moved":
        session.add(RenameProposal(file_id=file_id, proposed_filename="p.mp3", status="executed"))  # joint-write
    elif name == "unchanged":
        session.add(RenameProposal(file_id=file_id, proposed_filename="p.mp3", status="failed"))  # joint-write
    else:  # pragma: no cover - defensive: a new HARD invariant without a backing seeder
        raise AssertionError(f"no backing seeder for HARD invariant {name!r}")
    await session.flush()


def _result_for(report: Report, name: str) -> InvariantResult:
    return next(r for r in report.results if r.name == name)


# --------------------------------------------------------------------------------------------------
# divergent -- every HARD invariant flags a seeded state=X file with NO backing derived row.
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("inv", HARD_INVARIANTS, ids=_HARD_IDS)
async def test_divergent_hard_invariant_flags(db_session: AsyncSession, inv: Invariant) -> None:
    fid = await _new_file(db_session, state=inv.state)  # no backing row -> implication violated
    report = await run_shadow_compare(db_session)

    result = _result_for(report, inv.name)
    assert result.soft is False
    assert result.count >= 1
    assert str(fid) in result.sample
    assert report.hard_fail_total >= 1
    assert "HARD" in report.render()


# --------------------------------------------------------------------------------------------------
# consistent -- every HARD invariant with the correct backing row yields zero HARD divergence.
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("inv", HARD_INVARIANTS, ids=_HARD_IDS)
async def test_consistent_hard_invariant_clean(db_session: AsyncSession, inv: Invariant) -> None:
    fid = await _new_file(db_session, state=inv.state)
    await _seed_backing(db_session, inv.name, fid)
    report = await run_shadow_compare(db_session)

    assert _result_for(report, inv.name).count == 0
    assert report.hard_fail_total == 0


# --------------------------------------------------------------------------------------------------
# implication -- a more-derived-than-scalar file does NOT flag (implication, not equality).
# --------------------------------------------------------------------------------------------------
async def test_implication_more_derived_than_scalar_does_not_flag(db_session: AsyncSession) -> None:
    fid = await _new_file(db_session, state=FileState.METADATA_EXTRACTED.value)
    db_session.add(FileMetadata(file_id=fid, failed_at=None))  # satisfies metadata_extracted ⇒ metadata done
    db_session.add(AnalysisResult(file_id=fid, analysis_completed_at=datetime.now(UTC)))  # ALSO analysis-done (richer)
    await db_session.flush()

    report = await run_shadow_compare(db_session)

    # The extra analysis-done derivation does NOT drive an `analyzed`-state flag (no converse invariant).
    assert _result_for(report, "metadata_extracted").count == 0
    assert report.hard_fail_total == 0


# --------------------------------------------------------------------------------------------------
# allowlist -- soft divergences counted + printed but never gated (D-06).
# --------------------------------------------------------------------------------------------------
async def test_allowlist_soft_divergence_counted_but_not_gated(db_session: AsyncSession) -> None:
    await _new_file(db_session, state=FileState.FINGERPRINTED.value)  # divergent, but soft
    await _new_file(db_session, state=FileState.LOCAL_ANALYZING.value)
    report = await run_shadow_compare(db_session)

    fp = _result_for(report, "fingerprinted")
    la = _result_for(report, "local_analyzing")
    assert fp.soft is True and la.soft is True
    assert fp.count >= 1 and la.count >= 1
    assert report.soft_divergence_total >= 2
    assert report.hard_fail_total == 0  # soft divergences never gate
    assert "expected divergence" in report.render()


# --------------------------------------------------------------------------------------------------
# core -- DB-free registry shape: D-04 comprehensiveness + D-06 allowlist (no DB touch).
# --------------------------------------------------------------------------------------------------
def test_core_registry_shape_locks_coverage_and_allowlist() -> None:
    states = {inv.state for inv in INVARIANTS}

    # DISCOVERED is intentionally vacuous -> absent from the registry.
    assert FileState.DISCOVERED.value not in states
    # The soft allowlist is EXACTLY {fingerprinted, local_analyzing} -- it must never silently grow.
    assert {inv.state for inv in INVARIANTS if inv.soft} == {FileState.FINGERPRINTED.value, FileState.LOCAL_ANALYZING.value}
    # Every non-DISCOVERED FileState value has exactly one entry (guards a future enum addition).
    assert states == {s.value for s in FileState} - {FileState.DISCOVERED.value}
    assert len(states) == len(INVARIANTS)  # no duplicate-state entries


# --------------------------------------------------------------------------------------------------
# report shape -- per-invariant sample capped at sample_cap; verbose returns the full set.
# --------------------------------------------------------------------------------------------------
async def test_report_shape_respects_sample_cap_and_verbose(db_session: AsyncSession) -> None:
    for _ in range(3):
        await _new_file(db_session, state=FileState.APPROVED.value)  # 3 divergent approved files

    capped = await run_shadow_compare(db_session, sample_cap=2)
    r = _result_for(capped, "approved")
    assert r.count == 3
    assert len(r.sample) == 2  # capped
    assert capped.render()  # exercise the capped-render "…" suffix path

    full = await run_shadow_compare(db_session, verbose=True)
    rf = _result_for(full, "approved")
    assert rf.count == 3
    assert len(rf.sample) == 3  # uncapped
    assert full.render(verbose=True)


# --------------------------------------------------------------------------------------------------
# cli -- the `python -m phaze.cli.shadow_compare` entry (D-01/D-02) drives the SAME core and locks the
# D-05 exit-code contract: 1 on a seeded-divergent corpus, 0 on a clean one.
#
# `main()` runs its OWN `asyncio.run(...)`, so these cells are SYNC (calling `asyncio.run` from inside
# pytest-asyncio's running loop would RuntimeError). They drive `main(["--database-url", SA_DSN, ...])`
# so the CLI builds its OWN engine in its OWN loop (also exercising the D-02 --database-url path). The
# corpus is COMMITTED (not flushed) via a self-contained `asyncio.run` helper -- main's separate
# session cannot see an uncommitted transaction. A module-scoped setup/teardown creates the schema,
# seeds the FK agent, and TRUNCATEs `files` CASCADE so committed rows never leak into the async cells.
# --------------------------------------------------------------------------------------------------
async def _cli_prepare_schema_and_seed_agent() -> None:
    """Create all tables and ensure the FK ``legacy-application-server`` agent exists (committed)."""
    engine = create_async_engine(SA_DSN)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            if await session.get(Agent, _LEGACY_AGENT_ID) is None:
                session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
                await session.commit()
    finally:
        await engine.dispose()


async def _cli_commit_file(*, state: str) -> uuid.UUID:
    """Commit a bare FileRecord at ``state`` (no backing rows) so the separate CLI session sees it."""
    fid = uuid.uuid4()
    engine = create_async_engine(SA_DSN)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            session.add(
                FileRecord(
                    id=fid,
                    sha256_hash=uuid.uuid4().hex,
                    original_path=f"/media/{fid}.mp3",
                    original_filename=f"{fid}.mp3",
                    current_path=f"/media/{fid}.mp3",
                    file_type="mp3",
                    file_size=1234,
                    state=state,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()
    return fid


async def _cli_truncate_corpus() -> None:
    """TRUNCATE ``agents`` CASCADE so the COMMITTED CLI corpus (the seeded FK agent + its files, which
    default ``agent_id`` to the legacy agent) never leaks into the rollback-isolated cells here or in
    sibling test files -- a committed agent row would otherwise collide with their per-test agent seed.
    """
    from sqlalchemy import text

    engine = create_async_engine(SA_DSN)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE agents CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture
def cli_corpus() -> Generator[None]:
    """Sync fixture: probe PG (skip if down), build schema + FK agent, TRUNCATE on teardown.

    Sync so the CLI cells can call the sync ``main()`` (which owns its own ``asyncio.run``) directly.
    """
    import psycopg

    async def _probe() -> None:
        try:
            probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
        except psycopg.OperationalError as exc:
            pytest.skip(f"Postgres broker unavailable: {exc}")
        else:
            await probe.close()

    asyncio.run(_probe())
    asyncio.run(_cli_prepare_schema_and_seed_agent())
    try:
        yield
    finally:
        asyncio.run(_cli_truncate_corpus())


@pytest.mark.usefixtures("cli_corpus")
def test_cli_main_exits_nonzero_on_hard_divergence() -> None:
    from phaze.cli import shadow_compare as cli

    fid = asyncio.run(_cli_commit_file(state=FileState.APPROVED.value))  # HARD-divergent: no proposals row

    # --database-url drives the CLI over its OWN engine (D-02 path); exit 1 on hard divergence (D-05).
    assert cli.main(["--database-url", SA_DSN]) == 1
    assert cli.main(["--database-url", SA_DSN, "--sample-cap", "5"]) == 1  # --sample-cap threads through
    assert str(fid)  # the divergent file id is a real UUID we committed


@pytest.mark.usefixtures("cli_corpus")
def test_cli_main_exits_zero_on_clean_corpus() -> None:
    from phaze.cli import shadow_compare as cli

    # No divergent rows committed -> every HARD invariant is vacuously satisfied -> exit 0 (D-05).
    assert cli.main(["--database-url", SA_DSN, "--verbose"]) == 0
