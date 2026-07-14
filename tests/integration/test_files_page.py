"""Behavior-7 lock for the paginated, per-row-derived files page (87-04, UI-01 / PERF-01 / D-00c).

Against a REAL Postgres, this proves ``phaze.services.pipeline.get_files_page`` honours the phase's two
DoS-relevant anti-features:

* **Bounded, no whole-corpus COUNT per poll** (T-87-11): the emitted SQL is LIMIT-bounded and contains
  NO ``COUNT(`` -- ``has_next`` rides a ``LIMIT page_size + 1`` sentinel row, not a count.
* **Correlated per-page derivation**: each row's six buckets come from the correlated ``stage_status_case``
  CASE columns (evaluated for the page rows only), matching the seeded marker rows.
* **Partial indexes are usable** (PERF-01): an ``EXPLAIN`` of the bounded statement (with ``enable_seqscan``
  off, so the planner must reveal which indexes it CAN use on the tiny harness corpus) names the Phase-77
  partial indexes ``ix_metadata_failed`` / ``ix_analysis_completed`` / ``ix_analysis_failed`` /
  ``ix_fprint_success``.
* **SAVEPOINT degrade-safe** (T-87-12 / INFLIGHT-02): a forced build error degrades to a safe empty page,
  never a raise.

Real-PG harness idiom mirrors ``tests/integration/test_stage_status_equivalence.py`` (DSN derivation +
connectivity-probe ``pytest.skip`` so a bare ``uv run pytest`` skips rather than errors when PG is down).
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.base import Base
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.services.pipeline import _files_page_stmt, get_files_page


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")

_LEGACY_AGENT_ID = "test-fileserver"


@pytest_asyncio.fixture
async def db_env() -> AsyncGenerator[tuple[AsyncSession, AsyncEngine]]:
    """Yield ``(session, engine)`` on a real PG with all ORM tables + partial indexes present.

    ``Base.metadata.create_all`` builds the schema (including the ``__table_args__`` partial indexes the
    EXPLAIN test probes), so the harness is independent of Alembic. One transaction, rolled back at
    teardown. The engine is yielded too so the SQL-capture test can attach a ``before_cursor_execute``
    listener and the EXPLAIN test can run its probe.
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
        # Seed the FK-parent IDEMPOTENTLY: a committed ``test-fileserver`` may already exist (the
        # ``committed_db`` fixture re-seeds one, and the session-scoped ``async_engine`` seeds one), so a
        # blind INSERT collides on ``pk_agents`` under the full-bucket ordering (92-05, DI-92-04-02).
        # Get-or-insert satisfies the FK either way and keeps this hermetic fixture order-independent.
        if await session.get(Agent, _LEGACY_AGENT_ID) is None:
            session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
            await session.flush()
        try:
            yield session, engine
        finally:
            await session.rollback()
    await engine.dispose()


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


# --------------------------------------------------------------------------------------------------
# Behavior 7: bounded, no whole-corpus COUNT per poll.
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_page_is_bounded_and_emits_no_count(db_env: tuple[AsyncSession, AsyncEngine]) -> None:
    """The emitted SQL is LIMIT-bounded and contains NO ``COUNT(`` -- has_next rides a +1 sentinel."""
    session, engine = db_env
    for _ in range(12):  # > page_size (min clamp is 10) so the sentinel row proves has_next
        await _new_file(session)

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:
        captured.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        page = await get_files_page(session, page=1, page_size=10)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    # page_size=10 with 12 rows -> exactly 10 rows returned + has_next True (the sentinel saw an 11th row).
    assert len(page.rows) == 10
    assert page.has_next is True

    derivation_sql = [s for s in captured if "files" in s.lower() and "case" in s.lower()]
    assert derivation_sql, f"no per-row derivation SELECT captured; saw: {captured}"
    for sql in derivation_sql:
        lowered = sql.lower()
        assert "limit" in lowered, f"derivation query not LIMIT-bounded: {sql}"
        assert "count(" not in lowered, f"derivation query emitted a COUNT (whole-corpus scan risk): {sql}"


@pytest.mark.asyncio
async def test_files_page_last_page_has_no_next(db_env: tuple[AsyncSession, AsyncEngine]) -> None:
    """The final page reports has_next False (the +1 sentinel found no further row)."""
    session, _engine = db_env
    for _ in range(12):
        await _new_file(session)

    page = await get_files_page(session, page=2, page_size=10)
    assert len(page.rows) == 2  # 12 total, page 2 of 10 -> the remaining 2
    assert page.has_next is False


# --------------------------------------------------------------------------------------------------
# Correlated per-page derivation matches the seeded markers.
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_row_buckets_match_seeded_markers(db_env: tuple[AsyncSession, AsyncEngine]) -> None:
    """Each row's six derived buckets match its seeded output rows (metadata/analyze/fingerprint markers)."""
    session, _engine = db_env

    meta_failed = await _new_file(session)
    session.add(FileMetadata(file_id=meta_failed, failed_at=datetime.now(UTC)))
    analyze_done = await _new_file(session)
    session.add(AnalysisResult(file_id=analyze_done, analysis_completed_at=datetime.now(UTC)))
    fp_done = await _new_file(session)
    session.add(FingerprintResult(file_id=fp_done, engine="chromaprint", status="success"))
    plain = await _new_file(session)
    await session.flush()

    page = await get_files_page(session, page=1, page_size=50)
    by_id = {row.file.id: row.buckets for row in page.rows}

    assert by_id[meta_failed][Stage.METADATA.value] == "failed"
    assert by_id[analyze_done][Stage.ANALYZE.value] == "done"
    assert by_id[fp_done][Stage.FINGERPRINT.value] == "done"
    # A plain discovered file derives not_started across every stage.
    assert set(by_id[plain].values()) == {"not_started"}


# --------------------------------------------------------------------------------------------------
# PERF-01: EXPLAIN shows the Phase-77 partial indexes are usable by the bounded statement.
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_uses_partial_indexes(db_env: tuple[AsyncSession, AsyncEngine]) -> None:
    """EXPLAIN of the bounded per-page statement names the Phase-77 partial indexes (seqscan disabled)."""
    session, _engine = db_env
    # Seed a marker row per partial index so each correlated probe has a candidate to consider. The
    # `analysis` table CHECK forbids completed_at + failed_at on the SAME row, so they get distinct files.
    meta_failed = await _new_file(session)
    session.add(FileMetadata(file_id=meta_failed, failed_at=datetime.now(UTC)))
    analyze_done = await _new_file(session)
    session.add(AnalysisResult(file_id=analyze_done, analysis_completed_at=datetime.now(UTC)))
    analyze_failed = await _new_file(session)
    session.add(AnalysisResult(file_id=analyze_failed, failed_at=datetime.now(UTC)))
    fp_done = await _new_file(session)
    session.add(FingerprintResult(file_id=fp_done, engine="chromaprint", status="success"))
    await session.flush()

    stmt = _files_page_stmt(page=1, page_size=25, stage=None, bucket=None)
    compiled = stmt.compile(dialect=session.bind.dialect, compile_kwargs={"literal_binds": True})  # type: ignore[union-attr]

    # SET LOCAL so the planner MUST reveal which indexes it can use on the tiny harness corpus (on a
    # 3-row table it would otherwise seq-scan). This asserts index USABILITY, the perf property under test.
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    rows = (await session.execute(text(f"EXPLAIN {compiled}"))).all()
    plan = "\n".join(r[0] for r in rows)

    for index_name in ("ix_metadata_failed", "ix_analysis_completed", "ix_analysis_failed", "ix_fprint_success"):
        assert index_name in plan, f"{index_name} not used in EXPLAIN plan:\n{plan}"


# --------------------------------------------------------------------------------------------------
# T-87-12: SAVEPOINT degrade -> safe empty page, never a raise.
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degrades_to_empty_page_on_error(db_env: tuple[AsyncSession, AsyncEngine], monkeypatch: pytest.MonkeyPatch) -> None:
    """A forced build-time error degrades to a safe empty page (rows=[], has_next False), never a 500."""
    session, _engine = db_env
    for _ in range(3):
        await _new_file(session)

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("forced derivation failure")

    # The statement is built INSIDE get_files_page's begin_nested try/except, so a build-time raise
    # degrades exactly like a DB hiccup would.
    monkeypatch.setattr("phaze.services.pipeline.stage_status_case", _boom)

    page = await get_files_page(session, page=1, page_size=10)
    assert page.rows == []
    assert page.has_next is False

    # The outer session/transaction survives the degrade -- once the fault clears, a real query works.
    monkeypatch.undo()
    recovered = await get_files_page(session, page=1, page_size=10)
    assert len(recovered.rows) == 3
