"""Tests for `services/pipeline/reconciliation.py` (split from test_pipeline.py, phaze-7l8jh).

deduped_count, get_scanned_total, get_global_reconciliation, get_agent_reconciliations -- `services/pipeline/reconciliation.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.services.pipeline._shared import (
    UTC,
    Agent,
    ScanBatch,
    ScanStatus,
    _completed_batch,
    _NullSavepoint,
    _recon_file,
    datetime,
    deduped_count,
    get_agent_reconciliations,
    get_global_reconciliation,
    get_scanned_total,
    pytest,
    seed_active_agent,
    uuid,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def test_deduped_count_none_passthrough() -> None:
    """A None ``scanned`` passes through as None so the UI hides the reconciliation line."""
    assert deduped_count(None, 5) is None


def test_deduped_count_basic_arithmetic() -> None:
    """deduped = scanned - unique when scanned > unique."""
    assert deduped_count(10, 4) == 6


def test_deduped_count_clamps_negative_to_zero() -> None:
    """deduped never goes negative: more files than scanned clamps to 0."""
    assert deduped_count(3, 8) == 0


@pytest.mark.asyncio
async def test_get_scanned_total_single_completed_batch(session: AsyncSession) -> None:
    """One agent with one completed batch (total_files=100) → scanned 100."""
    await seed_active_agent(session, "nox")
    session.add(_completed_batch("nox", 100))
    await session.commit()
    assert await get_scanned_total(session) == 100


@pytest.mark.asyncio
async def test_get_scanned_total_rescan_counts_latest_only(session: AsyncSession) -> None:
    """A second completed batch (a re-scan) counts the LATEST only — never doubles the total."""
    from datetime import datetime

    await seed_active_agent(session, "nox")
    # Only the relative ORDER of these two stamps matters here, never the absolute instant.
    earlier = _completed_batch("nox", 100, created_at=datetime(2026, 1, 1, 10, 0, 0))
    later = _completed_batch("nox", 120, created_at=datetime(2026, 1, 1, 11, 0, 0))
    session.add_all([earlier, later])
    await session.commit()
    # Latest (120), not the sum (220) and not the earlier (100).
    assert await get_scanned_total(session) == 120


@pytest.mark.asyncio
async def test_get_scanned_total_tiebreaks_tied_created_at_by_id_desc(session: AsyncSession) -> None:
    """phaze-imih regression: a ``created_at`` tie must resolve via ``ScanBatch.id.desc()``, not
    executor-arbitrary heap/plan order -- mirroring
    ``test_get_agent_reconciliations_tiebreaks_tied_created_at_by_id_desc`` (phaze-n2d2), whose
    fix was applied to :func:`get_agent_reconciliations` only and left this sibling window behind.

    Seeds several completed batches for ONE agent sharing an EXPLICIT ``created_at`` with ids
    assigned in a SCRAMBLED order relative to insertion, each ``total_files`` derived from its id
    index so the row actually selected as ``rn == 1`` is identifiable precisely.
    """
    await seed_active_agent(session, "nox")
    tied_at = datetime(2026, 7, 20, 12, 0, 0)  # naive: test schema's created_at is TIMESTAMP WITHOUT TZ
    ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{i:02d}") for i in range(5)]
    scrambled_indices = [2, 0, 4, 1, 3]
    for i in scrambled_indices:
        batch = ScanBatch(
            id=ids[i],
            agent_id="nox",
            scan_path="/music",
            status=ScanStatus.COMPLETED.value,
            total_files=(i + 1) * 10,
            processed_files=(i + 1) * 10,
        )
        batch.created_at = tied_at  # type: ignore[assignment]
        session.add(batch)
    await session.commit()

    # id DESC as the tiebreak -> ids[4] (the LARGEST id) must win -> total_files=(4+1)*10=50.
    assert await get_scanned_total(session) == 50


@pytest.mark.asyncio
async def test_get_scanned_total_sums_across_agents(session: AsyncSession) -> None:
    """scanned sums each agent's latest completed batch: 100 (nox) + 50 (lux) → 150."""
    await seed_active_agent(session, "nox")
    await seed_active_agent(session, "lux")
    session.add_all([_completed_batch("nox", 100), _completed_batch("lux", 50)])
    await session.commit()
    assert await get_scanned_total(session) == 150


@pytest.mark.asyncio
async def test_get_scanned_total_ignores_non_completed(session: AsyncSession) -> None:
    """RUNNING / FAILED / LIVE batches never contribute to scanned."""
    await seed_active_agent(session, "nox")
    session.add_all(
        [
            _completed_batch("nox", 100),
            _completed_batch("nox", 999, status=ScanStatus.RUNNING.value),
            _completed_batch("nox", 999, status=ScanStatus.FAILED.value),
        ]
    )
    await session.commit()
    assert await get_scanned_total(session) == 100


@pytest.mark.asyncio
async def test_get_scanned_total_empty_db_returns_none(session: AsyncSession) -> None:
    """No completed batches → None (the 'hide' sentinel, distinct from a real 0)."""
    assert await get_scanned_total(session) is None


@pytest.mark.asyncio
async def test_get_scanned_total_degrades_to_none_on_db_error() -> None:
    """A forced read error degrades scanned to None (hidden state), never raising into the 5s poll.

    The read runs inside a SAVEPOINT (``begin_nested``); the exception propagates out of the nested
    scope and is caught by the degrade ``except`` (CR-01 -- the caller's shared session is never
    touched with a full ``session.rollback()``).
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("scan_batches table unavailable")

    assert await get_scanned_total(_ExplodingSession()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_scanned_total_degrades_when_begin_nested_itself_raises() -> None:
    """Even if the session is so broken ``begin_nested()`` itself raises, scanned still degrades to None.

    Exercises the last-ditch branch where opening the SAVEPOINT fails synchronously (before any
    query runs). The function must still swallow everything and return the hidden-state sentinel
    rather than propagating into the 5s poll.
    """

    class _DoublyExplodingSession:
        def begin_nested(self) -> object:
            raise RuntimeError("connection already closed")

    assert await get_scanned_total(_DoublyExplodingSession()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_scanned_total_degrade_preserves_caller_loaded_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-01: the degrade must NOT expire ORM rows the caller already loaded on this same session.

    ``build_dashboard_context`` loads ``agents`` on the request session BEFORE calling
    :func:`get_scanned_total` (transitively via :func:`get_global_reconciliation`). A plain
    ``session.rollback()`` in the degrade branch would expire that already-loaded ``Agent`` row,
    500-ing the template render on the next lazy load (MissingGreenlet from a sync context).

    Distinguishing signal (fixture never commits, so ``inspect().expired`` cannot tell a SAVEPOINT
    rollback apart from a plain one -- a plain rollback expunges the pending flush to *transient*,
    not *expired*): flush an Agent row, force ONLY the scanned-total SELECT to fail, then assert
    ``session.get`` still finds the agent afterwards -- proving the outer transaction survived.
    """
    from unittest.mock import AsyncMock

    agent = Agent(id="cr01-scanned-total-agent", name="Cr01ScanBox", scan_roots=[], last_seen_at=datetime.now(UTC), kind="fileserver")
    session.add(agent)
    await session.flush()

    real_execute = session.execute
    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=RuntimeError("boom")))
    result = await get_scanned_total(session)
    monkeypatch.setattr(session, "execute", real_execute)  # restore for the assertion query

    assert result is None
    assert await session.get(Agent, "cr01-scanned-total-agent") is not None


@pytest.mark.asyncio
async def test_get_global_reconciliation_happy_path(session: AsyncSession) -> None:
    """scanned 11428 with 11106 discovered files → {'scanned': 11428, 'deduped': 322}."""
    await seed_active_agent(session, "nox")
    session.add(_completed_batch("nox", 11428))
    # 5 files stands in for the discovery_done COUNT; assert the arithmetic, not a 11106-row seed.
    session.add_all([_recon_file("nox", i) for i in range(5)])
    await session.commit()
    recon = await get_global_reconciliation(session)
    assert recon == {"scanned": 11428, "deduped": 11423}


@pytest.mark.asyncio
async def test_get_global_reconciliation_hidden_when_scanned_unavailable(session: AsyncSession) -> None:
    """When get_scanned_total degrades to None the whole reconciliation is the hidden state."""
    recon = await get_global_reconciliation(session)  # empty DB → no completed batches → None
    assert recon == {"scanned": None, "deduped": None}


@pytest.mark.asyncio
async def test_get_global_reconciliation_clamps_when_discovery_ge_scanned(session: AsyncSession) -> None:
    """deduped clamps to 0 when discovery_done ≥ scanned (never negative)."""
    await seed_active_agent(session, "nox")
    session.add(_completed_batch("nox", 2))
    session.add_all([_recon_file("nox", i) for i in range(5)])  # 5 files > scanned 2
    await session.commit()
    recon = await get_global_reconciliation(session)
    assert recon == {"scanned": 2, "deduped": 0}


@pytest.mark.asyncio
async def test_get_agent_reconciliations_per_agent_dedup(session: AsyncSession) -> None:
    """Per-agent: A latest 100 with 90 rows → deduped 10; B latest 50 with 50 rows → deduped 0."""
    await seed_active_agent(session, "nox")
    await seed_active_agent(session, "lux")
    session.add_all([_completed_batch("nox", 100), _completed_batch("lux", 50)])
    session.add_all([_recon_file("nox", i) for i in range(90)])
    session.add_all([_recon_file("lux", i) for i in range(50)])
    await session.commit()

    recon = await get_agent_reconciliations(session)
    assert recon["nox"] == {"scanned": 100, "unique": 90, "deduped": 10}
    assert recon["lux"] == {"scanned": 50, "unique": 50, "deduped": 0}


@pytest.mark.asyncio
async def test_get_agent_reconciliations_rescan_counts_latest_only(session: AsyncSession) -> None:
    """A second completed batch for one agent counts the latest total_files only."""
    from datetime import datetime

    await seed_active_agent(session, "nox")
    # Only the relative ORDER of these two stamps matters here, never the absolute instant.
    earlier = _completed_batch("nox", 100, created_at=datetime(2026, 1, 1, 10, 0, 0))
    later = _completed_batch("nox", 120, created_at=datetime(2026, 1, 1, 11, 0, 0))
    session.add_all([earlier, later])
    session.add_all([_recon_file("nox", i) for i in range(90)])
    await session.commit()

    recon = await get_agent_reconciliations(session)
    assert recon["nox"] == {"scanned": 120, "unique": 90, "deduped": 30}


@pytest.mark.asyncio
async def test_get_agent_reconciliations_tiebreaks_tied_created_at_by_id_desc(session: AsyncSession) -> None:
    """rn==1 for a ``created_at`` tie must be the MAX id (``ScanBatch.id.desc()`` tiebreak), not
    arbitrary heap/plan order.

    Mirrors the phaze-c6j5 regression-guard technique (``test_get_agent_recent_scans_tiebreaker_
    orders_tied_created_at_by_id_desc`` above): seeds several completed batches for ONE agent
    sharing an EXPLICIT ``created_at`` with ids assigned in a SCRAMBLED order relative to insertion,
    and pins each batch's ``total_files`` to a value derived from its id index so the row actually
    selected as rn==1 is identifiable precisely. Only the ``ScanBatch.id.desc()`` tiebreaker
    appended to the window's ``order_by`` (matching the primary ``created_at.desc()``) makes the
    "agent's most recent completed batch" pick deterministic on a tie; without it the pick tracks
    Postgres heap/plan order, not id order -- which the scrambled insertion order below defeats.
    """
    await seed_active_agent(session, "nox")
    tied_at = datetime(2026, 7, 20, 12, 0, 0)  # naive: test schema's created_at is TIMESTAMP WITHOUT TZ
    # 5 fixed, distinct ids -- inserted in a SCRAMBLED order (not ascending, not descending).
    ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{i:02d}") for i in range(5)]
    scrambled_indices = [2, 0, 4, 1, 3]
    for i in scrambled_indices:
        batch = ScanBatch(
            id=ids[i],
            agent_id="nox",
            scan_path="/music",
            status=ScanStatus.COMPLETED.value,
            total_files=(i + 1) * 10,
            processed_files=(i + 1) * 10,
        )
        batch.created_at = tied_at  # type: ignore[assignment]
        session.add(batch)
    await session.commit()

    recon = await get_agent_reconciliations(session)

    # id DESC as the tiebreak -> ids[4] (the LARGEST id) must win -> total_files=(4+1)*10=50.
    assert recon["nox"]["scanned"] == 50


@pytest.mark.asyncio
async def test_get_agent_reconciliations_degrades_to_empty_on_db_error() -> None:
    """A forced read error degrades to an empty map (no annotations), never raising.

    The reads run inside a SAVEPOINT (``begin_nested``); the exception propagates out of the
    nested scope and is caught by the degrade ``except`` (CR-01 -- the caller's shared session is
    never touched with a full ``session.rollback()``).
    """

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("scan_batches table unavailable")

    assert await get_agent_reconciliations(_ExplodingSession()) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_agent_reconciliations_degrades_when_begin_nested_itself_raises() -> None:
    """Even if the session is so broken ``begin_nested()`` itself raises, the per-agent map still
    degrades to ``{}`` rather than propagating into the 5s dashboard poll."""

    class _DoublyExplodingSession:
        def begin_nested(self) -> object:
            raise RuntimeError("connection already closed")

    assert await get_agent_reconciliations(_DoublyExplodingSession()) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_agent_reconciliations_degrade_preserves_caller_loaded_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-01: the degrade must NOT expire ORM rows the caller already loaded on this same session.

    ``build_recent_scans`` (``routers.pipeline_scans``) loads ScanBatch ORM rows on this SAME
    session BEFORE calling ``get_agent_reconciliations``. A plain ``session.rollback()`` in the
    degrade branch would expire those already-loaded rows, 500-ing the render on the next lazy load
    (MissingGreenlet from a sync context).

    Distinguishing signal (fixture never commits, so ``inspect().expired`` cannot tell a SAVEPOINT
    rollback apart from a plain one -- a plain rollback expunges the pending flush to *transient*,
    not *expired*): flush a ScanBatch row, force ONLY the reconciliation reads to fail, then assert
    ``session.get`` still finds the scan batch afterwards -- proving the outer transaction survived.
    """
    from unittest.mock import AsyncMock

    session.add(Agent(id="cr01-recon-agent", name="Cr01ReconBox", scan_roots=[], kind="fileserver"))
    await session.flush()
    batch = ScanBatch(id=uuid.uuid4(), agent_id="cr01-recon-agent", scan_path="/data/music", status=ScanStatus.COMPLETED.value, total_files=5)
    session.add(batch)
    await session.flush()

    real_execute = session.execute
    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=RuntimeError("boom")))
    recon = await get_agent_reconciliations(session)
    monkeypatch.setattr(session, "execute", real_execute)  # restore for the assertion query

    assert recon == {}
    assert await session.get(ScanBatch, batch.id) is not None
