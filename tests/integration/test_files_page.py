"""Behavior-7 lock for the paginated, per-row-derived files page (87-04, UI-01 / PERF-01 / D-00c).

Against a REAL Postgres, this proves ``phaze.services.pipeline.get_files_page`` honours the phase's two
DoS-relevant anti-features:

* **Bounded, no whole-corpus COUNT per poll** (T-87-11): the emitted SQL is LIMIT-bounded and contains
  NO ``COUNT(`` -- ``has_next`` rides a ``LIMIT page_size + 1`` sentinel row, not a count.
* **Correlated per-page derivation**: each row's buckets come from the correlated ``stage_status_case``
  CASE columns (evaluated for the page rows only), matching the seeded marker rows.
* **Partial indexes are usable** (PERF-01): an ``EXPLAIN`` of the bounded statement (with ``enable_seqscan``
  off, so the planner must reveal which indexes it CAN use on the tiny harness corpus) names the Phase-77
  partial indexes ``ix_metadata_failed`` / ``ix_analysis_completed`` / ``ix_analysis_failed``.
* **SAVEPOINT degrade-safe** (T-87-12 / INFLIGHT-02): a forced build error degrades to a safe empty page,
  never a raise.

The root session fixture owns the real-Postgres schema lifecycle and guards the test-only DSN.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import RenameProposal
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.models.tracklist import Tracklist
from phaze.routers.pipeline import FILES_SORT
from phaze.services.pipeline import _files_page_stmt, get_files_page
from phaze.services.stage_status import display_status_case


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


_LEGACY_AGENT_ID = "test-fileserver"


@pytest_asyncio.fixture
async def db_env(async_engine: AsyncEngine) -> AsyncGenerator[tuple[AsyncSession, AsyncEngine]]:
    """Yield ``(session, engine)`` on a real PG with all ORM tables + partial indexes present.

    The session-scoped ``async_engine`` fixture builds the schema, including the ``__table_args__``
    partial indexes the EXPLAIN test probes. One transaction is rolled back at teardown. The engine is
    yielded too so the SQL-capture test can attach a ``before_cursor_execute`` listener and the EXPLAIN
    test can run its probe.
    """
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        # Seed the FK-parent IDEMPOTENTLY: a committed ``test-fileserver`` may already exist (the
        # ``committed_db`` fixture re-seeds one, and the session-scoped ``async_engine`` seeds one), so a
        # blind INSERT collides on ``pk_agents`` under the full-bucket ordering (92-05, DI-92-04-02).
        # Get-or-insert satisfies the FK either way and keeps this hermetic fixture order-independent.
        if await session.get(Agent, _LEGACY_AGENT_ID) is None:
            session.add(Agent(id=_LEGACY_AGENT_ID, name="legacy"))
            await session.flush()
        try:
            yield session, async_engine
        finally:
            await session.rollback()


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


# Behavior 7: bounded, no whole-corpus COUNT per poll.


@pytest.mark.asyncio
async def test_files_page_is_bounded_and_emits_no_count(db_env: tuple[AsyncSession, AsyncEngine]) -> None:
    """The emitted SQL is LIMIT-bounded and contains NO ``COUNT(`` -- has_next rides a +1 sentinel."""
    session, engine = db_env
    file_ids = [await _new_file(session) for _ in range(12)]

    captured: list[str] = []
    captured_params = []

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:
        captured.append(statement)
        captured_params.append(parameters)

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
    # History is projected in that same bounded SELECT, not fetched separately for each row.
    assert len(derivation_sql) == 1
    selects = [sql for sql in captured if sql.lstrip().lower().startswith(("select", "with"))]
    assert len(selects) == 2
    # Membership and badges share one MVCC statement snapshot; the second read is only the
    # existing bounded profile loader. The barrier contains IDs/order/paging, never display work.
    assert "files_page_members AS MATERIALIZED" in selects[0]
    membership, enrichment = selects[0].split(")\n SELECT", 1)
    assert "CASE" not in membership
    assert "LIMIT" in membership and "OFFSET" in membership
    assert "CASE" in enrichment and "JOIN files_page_members" in enrichment
    assert "set_profile" in selects[1]
    profile_params = next(params for statement, params in zip(captured, captured_params, strict=True) if "set_profile" in statement)
    assert tuple(profile_params) == tuple(sorted(file_ids)[:11]), "profile loading must cover only this page plus its sentinel"
    assert "analysis_window" in derivation_sql[0].lower()
    assert all(row.stage_history == {"metadata": False, "analyze": False, "tracklist": False} for row in page.rows)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("sort_key", [column.key for column in FILES_SORT.columns])
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_bounded_membership_preserves_every_sort_and_page(db_env: tuple[AsyncSession, AsyncEngine], sort_key: str, direction: str) -> None:
    """Compare every page to the full derived order, including ties and status precedence.

    The independent reference has no paging barrier: paging its completed ordered result in
    Python detects limiting before a stage sort, missing outer order, dropped ties, and a
    repeated/omitted sentinel. Existing lens tests separately cover membership predicates.
    """
    session, _engine = db_env
    from phaze.services.pipeline.files import _FILES_PAGE_STAGES

    for index in range(23):
        fid = await _new_file(session)
        record = await session.get(FileRecord, fid)
        assert record is not None
        # Current paths may tie even when original paths are unique; every sort needs the ID suffix.
        record.current_path = f"/synthetic/root-{index % 2}/track-{index // 4:02d}.mp3"
        record.file_type = "txt" if index % 4 == 0 else "mp3"
        if index % 3 == 0:
            session.add(FileMetadata(file_id=fid, failed_at=datetime.now(UTC) if index % 6 == 0 else None))
        if index % 4 == 0:
            session.add(AnalysisResult(file_id=fid, analysis_completed_at=datetime.now(UTC)))
        elif index % 4 == 1:
            session.add(AnalysisResult(file_id=fid, failed_at=datetime.now(UTC)))
        if index % 5 == 0:
            session.add(SchedulingLedger(key=f"process_file:{fid}", function="process_file", routing="agent", payload={"file_id": str(fid)}))
        if index % 7 == 0:
            session.add(StageSkip(file_id=fid, stage="analyze", reason="Synthetic scope decision"))
        session.add(
            RenameProposal(
                file_id=fid, proposed_filename="Synthetic.mp3", status=("pending", "approved", "rejected", "executed", "failed")[index % 5]
            )
        )
        if index % 6 == 0:
            session.add(Tracklist(file_id=fid, external_id=f"synthetic-{index}", source_url="https://example.invalid/source"))
    await session.flush()

    sort = FILES_SORT.resolve(sort=sort_key, order=direction, view_state={})
    full = (
        await session.execute(
            select(FileRecord.id, *(display_status_case(stage) for stage in _FILES_PAGE_STAGES)).order_by(*sort.order_by(), FileRecord.id)
        )
    ).all()
    assert len(full) == 23
    seen: list[uuid.UUID] = []
    for page_number in range(1, 5):
        page = await get_files_page(session, page=page_number, page_size=10, sort=sort)
        reference = full[(page_number - 1) * 10 : page_number * 10]
        assert [row.file.id for row in page.rows] == [row[0] for row in reference]
        assert [tuple(row.buckets[stage.value] for stage in _FILES_PAGE_STAGES) for row in page.rows] == [tuple(row[1:]) for row in reference]
        assert page.has_next is (page_number * 10 < len(full))
        seen.extend(row.file.id for row in page.rows)
    assert seen == [row[0] for row in full]


# Correlated per-page derivation matches the seeded markers.


@pytest.mark.asyncio
async def test_per_row_buckets_match_seeded_markers(db_env: tuple[AsyncSession, AsyncEngine]) -> None:
    """Each row's derived buckets match its seeded output rows (metadata/analyze markers)."""
    session, _engine = db_env

    meta_failed = await _new_file(session)
    session.add(FileMetadata(file_id=meta_failed, failed_at=datetime.now(UTC)))
    analyze_done = await _new_file(session)
    session.add(AnalysisResult(file_id=analyze_done, analysis_completed_at=datetime.now(UTC)))
    plain = await _new_file(session)
    await session.flush()

    page = await get_files_page(session, page=1, page_size=50)
    by_id = {row.file.id: row.buckets for row in page.rows}

    assert by_id[meta_failed][Stage.METADATA.value] == "failed"
    assert by_id[analyze_done][Stage.ANALYZE.value] == "done"
    # A plain discovered file derives not_started across every stage.
    assert set(by_id[plain].values()) == {"not_started"}


# PERF-01: EXPLAIN shows the Phase-77 partial indexes are usable by the bounded statement.


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
    await session.flush()

    stmt = _files_page_stmt(page=1, page_size=25, stage=None, bucket=None)
    compiled = stmt.compile(dialect=session.bind.dialect, compile_kwargs={"literal_binds": True})  # type: ignore[union-attr]

    # SET LOCAL so the planner MUST reveal which indexes it can use on the tiny harness corpus (on a
    # 3-row table it would otherwise seq-scan). This asserts index USABILITY, the perf property under test.
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    rows = (await session.execute(text(f"EXPLAIN {compiled}"))).all()
    plan = "\n".join(r[0] for r in rows)

    for index_name in ("ix_metadata_failed", "ix_analysis_completed", "ix_analysis_failed"):
        assert index_name in plan, f"{index_name} not used in EXPLAIN plan:\n{plan}"


# T-87-12: SAVEPOINT degrade -> safe empty page, never a raise.


# phaze-7sdwt: EXPLAIN of the "any stage = failed" OR lens -- the fix for the silently-ignored
# bucket-alone filter. Same PERF-01 posture as the single-stage EXPLAIN above: seqscan disabled so
# the planner must reveal what it CAN use, proving the OR over _FILES_PAGE_STAGES still reaches the
# partial indexes rather than degrading to a sequential scan across every disjunct.


@pytest.mark.asyncio
async def test_explain_any_stage_failed_filter_uses_partial_indexes(db_env: tuple[AsyncSession, AsyncEngine]) -> None:
    """EXPLAIN of the any-stage (``stage=None``) ``bucket="failed"`` OR lens names the partial indexes."""
    session, _engine = db_env
    meta_failed = await _new_file(session)
    session.add(FileMetadata(file_id=meta_failed, failed_at=datetime.now(UTC)))
    analyze_failed = await _new_file(session)
    session.add(AnalysisResult(file_id=analyze_failed, failed_at=datetime.now(UTC)))
    await session.flush()

    stmt = _files_page_stmt(page=1, page_size=25, stage=None, bucket="failed")
    compiled = stmt.compile(dialect=session.bind.dialect, compile_kwargs={"literal_binds": True})  # type: ignore[union-attr]

    await session.execute(text("SET LOCAL enable_seqscan = off"))
    rows = (await session.execute(text(f"EXPLAIN {compiled}"))).all()
    plan = "\n".join(r[0] for r in rows)

    for index_name in ("ix_metadata_failed", "ix_analysis_failed"):
        assert index_name in plan, f"{index_name} not used in EXPLAIN plan:\n{plan}"


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
    monkeypatch.setattr("phaze.services.pipeline.files.display_status_case", _boom)

    page = await get_files_page(session, page=1, page_size=10)
    assert page.rows == []
    assert page.has_next is False

    # The outer session/transaction survives the degrade -- once the fault clears, a real query works.
    monkeypatch.undo()
    recovered = await get_files_page(session, page=1, page_size=10)
    assert len(recovered.rows) == 3
