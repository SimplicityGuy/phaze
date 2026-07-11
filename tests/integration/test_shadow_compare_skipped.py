"""Behavior 6 (D-13d): the additive force-skip marker keeps the Phase-79 shadow gate GREEN.

The ``stage_skip`` force-skip writer is PURELY ADDITIVE (Pitfall 3): it inserts a marker row and NEVER
clears ``analysis.failed_at``. This test encodes that property as a HARD test so a later writer that
"tidies" ``failed_at`` on skip regresses the standing shadow-compare gate LOUDLY instead of silently.

Setup: a terminally-failed analyze file (``state='analysis_failed'`` + ``analysis.failed_at`` set) that
ALSO carries a ``stage_skip(stage='analyze')`` marker, with ``failed_at`` left in place. Then:

* ``run_shadow_compare`` reports ZERO hard divergences (the ``analysis_failed`` invariant --
  ``state='analysis_failed' ⇒ failed_clause(ANALYZE)`` -- still holds because ``failed_at`` survives), and
* the soft allowlist is UNCHANGED (exactly ``{fingerprinted, local_analyzing}`` -- the skip needs NO new
  allowlist entry, D-13d), and
* ``failed_clause(ANALYZE)`` still evaluates True for the skipped file (the implication's antecedent is
  intact -- no false flag, no allowlist growth).

Mutation intuition (project memory): were the writer to clear ``failed_at`` on skip, ``failed_clause``
would go False and the ``analysis_failed`` invariant would flag -- turning ``hard_fail_total`` non-zero.
This test would then go RED, which is exactly the regression signal it exists to provide.

Real-PG harness idiom mirrors ``tests/integration/test_shadow_compare.py`` (DSN derivation + the *_test
destructive-write guard + connectivity-probe skip + per-test rollback). Run via
``just test-bucket integration`` with ``TEST_DATABASE_URL`` at :5433.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.base import Base
from phaze.models.file import FileRecord, FileState
from phaze.models.stage_skip import StageSkip
from phaze.services.shadow_compare import INVARIANTS, Report, run_shadow_compare
from phaze.services.stage_status import failed_clause


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.integration


# Raw libpq broker DSN + SQLAlchemy async DSN, derived exactly as tests/integration/test_shadow_compare.py.
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")

# Destructive-write safety guard: refuse any DB whose name is not a `_test` database (this file only
# rolls back, but the guard mirrors the sibling shadow-compare suite and keeps a bare `uv run pytest`
# from ever touching the dev corpus).
_TARGET_DB = make_url(SA_DSN).database or ""
if not _TARGET_DB.endswith("_test"):
    pytest.skip(
        f"Refusing to run shadow-compare integration tests against non-test database {_TARGET_DB!r}; "
        "set TEST_DATABASE_URL to a *_test DSN (e.g. run `just test-db`).",
        allow_module_level=True,
    )

_LEGACY_AGENT_ID = "legacy-application-server"


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a real-PG ``AsyncSession`` with all ORM tables + the FK agent seeded (per-test rollback)."""
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


async def _seed_skipped_over_failed_analyze(session: AsyncSession) -> uuid.UUID:
    """Seed an analysis_failed file with ``failed_at`` set AND an additive ``stage_skip(analyze)`` marker.

    Mirrors the real force-skip write path: the failure marker (``analysis.failed_at``) is LEFT in place
    and the skip is layered on top (additive-only). Returns the file id.
    """
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
            state=FileState.ANALYSIS_FAILED.value,
        )
    )
    await session.flush()
    session.add(AnalysisResult(file_id=fid, failed_at=datetime.now(UTC)))  # terminal failure marker
    await session.flush()
    session.add(StageSkip(file_id=fid, stage="analyze", reason="corrupt source"))  # additive skip -- failed_at UNTOUCHED
    await session.flush()
    return fid


async def test_shadow_compare_stays_green_with_skipped_over_failed_analyze(db_session: AsyncSession) -> None:
    """Behavior 6: an additive skip marker on a failed analyze file drives ZERO hard divergence."""
    await _seed_skipped_over_failed_analyze(db_session)

    report = await run_shadow_compare(db_session)

    # The additive marker cannot violate any state⇒derived implication: analysis_failed still holds
    # (failed_at survives), and stage_skip is referenced by NO invariant.
    assert report.hard_fail_total == 0
    assert _result_count(report, "analysis_failed") == 0


async def test_soft_allowlist_is_not_grown_by_the_skip_marker(db_session: AsyncSession) -> None:
    """D-13d: the skip needs NO new soft-allowlist entry -- it stays exactly {fingerprinted, local_analyzing}.

    A DB-touching cell (the seed proves a live skipped-over-failed row exists in the corpus) alongside
    the static registry assertion: the marker keeps the gate green WITHOUT relaxing it.
    """
    await _seed_skipped_over_failed_analyze(db_session)
    report = await run_shadow_compare(db_session)
    assert report.hard_fail_total == 0

    soft_states = {inv.state for inv in INVARIANTS if inv.soft}
    assert soft_states == {FileState.FINGERPRINTED.value, FileState.LOCAL_ANALYZING.value}


async def test_failed_clause_still_true_for_skipped_over_failed_analyze(db_session: AsyncSession) -> None:
    """The implication's antecedent survives: ``failed_clause(ANALYZE)`` is still True after the skip.

    This is the load-bearing property behind behavior 6 -- if a future writer cleared ``failed_at`` on
    skip, this assertion (and the ``analysis_failed`` invariant) would go RED.
    """
    fid = await _seed_skipped_over_failed_analyze(db_session)

    still_failed = bool((await db_session.execute(select(failed_clause(Stage.ANALYZE)).where(FileRecord.id == fid))).scalar_one())
    assert still_failed is True


def _result_count(report: Report, name: str) -> int:
    """Return the divergent count for invariant ``name`` in a shadow-compare ``Report``."""
    return next(r.count for r in report.results if r.name == name)
