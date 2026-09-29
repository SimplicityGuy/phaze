"""phaze-ww6yk: the scoped reset of cloud attempts that an infrastructure fault spent.

Seeded to the production shape spike phaze-79mu7 measured: ``cloud_job`` rows parked at
``status='awaiting'`` with ``attempts = cloud_submit_max_attempts`` on the burst backend, each folded
once into the durable ``cloud_budget`` ledger at spill time, inside the incident window. Decoys sit one
second outside each window bound, on another backend, below the floor and in flight. The drain test
runs the real ``stage_cloud_window`` before and after the reset, so it fails if either half of the
budget (the per-chain ``attempts`` or the ledger's cooldown) is left in place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_budget import CloudBudget
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.cloud_attempts_reset import (
    AnalysisState,
    Disposition,
    LedgerAction,
    ResetReport,
    ResetScope,
    ScopedRow,
    apply_reset,
    preview_reset,
)
from phaze.tasks import release_awaiting_cloud
from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from tests._queue_fakes import DedupFakeTaskRouter, seed_active_agent
from tests.analyze.tasks.test_drain_head_of_line import _Cfg, _CloudStub, _FullLocalBackend, _make_ctx, _make_file


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


BURST = "kueue-burst"
CAP = 3
WINDOW_START = datetime(2026, 9, 26, 16, 11, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 27, 3, 29, 0, tzinfo=UTC)
INSIDE = datetime(2026, 9, 26, 22, 0, 0, tzinfo=UTC)
SCOPE = ResetScope(backend_id=BURST, window_start=WINDOW_START, window_end=WINDOW_END, attempts_floor=CAP)
_FILE_TIME = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)


class _BudgetCfg(_Cfg):
    """The head-of-line cell's config plus the durable-ledger knobs at their shipped defaults.

    ``_Cfg`` omits them because its tests never carry a ledger row. Here a ledger row the reset failed
    to unfold must produce a real ``cloud_budget_cooldown`` hold rather than an AttributeError.
    """

    cloud_budget_cooldown_days = 14.0
    cloud_budget_max_chains = 3
    cloud_budget_max_node_loss = 1


@pytest.fixture(autouse=True)
def _fresh_resume_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_awaiting_cloud, "_resume_cursor", None)


async def _seed_row(
    session: AsyncSession,
    *,
    updated_at: datetime = INSIDE,
    backend_id: str | None = BURST,
    status: str = CloudJobStatus.AWAITING.value,
    attempts: int = CAP,
    chains_spent: int | None = 1,
    budget_spent_at: datetime | None = None,
    order: int = 0,
    **cloud_job_fields: Any,
) -> uuid.UUID:
    """One file and its ``cloud_job`` row, plus the ledger fold its spill wrote (``chains_spent=None`` for none).

    ``budget_spent_at`` defaults to ``updated_at``: the spill's CAS and its fold run in one transaction,
    and both stamp ``func.now()``, so production rows carry the same instant in both columns.
    """
    file = _make_file(_FILE_TIME + timedelta(seconds=order))
    session.add(file)
    await session.flush()
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            status=status,
            attempts=attempts,
            backend_id=backend_id,
            created_at=updated_at - timedelta(minutes=3),
            updated_at=updated_at,
            **cloud_job_fields,
        )
    )
    if chains_spent is not None:
        session.add(
            CloudBudget(
                file_id=file.id,
                chains_spent=chains_spent,
                attempts_spent=CAP * chains_spent,
                node_loss_spent=0,
                budget_spent_at=budget_spent_at or updated_at,
            )
        )
    await session.commit()
    return file.id


async def _job(session: AsyncSession, file_id: uuid.UUID) -> CloudJob:
    session.expire_all()
    return (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one()


async def _budget(session: AsyncSession, file_id: uuid.UUID) -> CloudBudget | None:
    session.expire_all()
    return (await session.execute(select(CloudBudget).where(CloudBudget.file_id == file_id))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_scope_selects_exactly_the_incident_population_and_nothing_outside_it(session: AsyncSession) -> None:
    """Both window bounds are inclusive; one second past either, another backend, or below the floor is out."""
    in_scope = [
        await _seed_row(session, updated_at=WINDOW_START, order=0),
        await _seed_row(session, updated_at=INSIDE, order=1),
        await _seed_row(session, updated_at=WINDOW_END, order=2),
        await _seed_row(session, attempts=CAP + 1, order=3),
    ]
    await _seed_row(session, updated_at=WINDOW_START - timedelta(seconds=1), order=10)
    await _seed_row(session, updated_at=WINDOW_END + timedelta(seconds=1), order=11)
    await _seed_row(session, backend_id="compute-1", order=12)
    await _seed_row(session, backend_id=None, order=13)
    await _seed_row(session, attempts=CAP - 1, order=14)
    await _seed_row(session, status=CloudJobStatus.SUBMITTED.value, order=15)

    report = await preview_reset(session, SCOPE)

    assert [row.file_id for row in report.rows] == in_scope  # FIFO order, exactly the four
    assert report.by_disposition == {Disposition.RESET.value: 4}
    assert {row.ledger for row in report.rows} == {LedgerAction.CLEARED}
    lines = report.breakdown_lines()
    assert lines[1] == "4 row(s) in scope"
    assert "by attempts: 3=3 4=1" in lines


@pytest.mark.asyncio
async def test_dry_run_changes_nothing(session: AsyncSession) -> None:
    file_id = await _seed_row(session)

    report = await preview_reset(session, SCOPE)

    assert not report.applied
    assert report.rows_reset == 0
    assert [row.disposition for row in report.rows] == [Disposition.RESET]
    job = await _job(session, file_id)
    assert (job.attempts, job.updated_at) == (CAP, INSIDE)
    budget = await _budget(session, file_id)
    assert budget is not None
    assert budget.chains_spent == 1


@pytest.mark.asyncio
async def test_apply_lets_the_drain_route_the_rows_to_cloud(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Before the reset the drain holds every row (local is full); after it, the cloud stub takes them.

    The ledger rows matter: with ``attempts`` reset but the fold left in place, the 14-day cooldown
    still bars cloud and this tick stages nothing.
    """
    cloud = _CloudStub(id=BURST, cap=5)
    monkeypatch.setattr("phaze.tasks.release_awaiting_cloud.get_settings", lambda: _BudgetCfg())
    monkeypatch.setattr("phaze.services.backends.resolve_backends", lambda cfg: [cloud, _FullLocalBackend(id="local", rank=99, cap=1)])  # noqa: ARG005
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    ids = [await _seed_row(session, order=i) for i in range(3)]

    before = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))
    assert before["staged"] == 0
    assert cloud.dispatched == []

    report = await apply_reset(session, SCOPE)
    await session.commit()
    assert report.rows_reset == 3
    assert report.ledger_rows_deleted == 3

    after = await stage_cloud_window(_make_ctx(DedupFakeTaskRouter()))
    assert after["staged"] == 3
    assert cloud.dispatched == ids
    for file_id in ids:
        job = await _job(session, file_id)
        assert job.attempts == 0
        assert job.updated_at > WINDOW_END  # the lane-entry clock restarted
        assert await _budget(session, file_id) is None


@pytest.mark.asyncio
async def test_rows_with_analysis_are_reset_only_when_the_analysis_is_partial(session: AsyncSession) -> None:
    """A partial analysis row is re-analysed. A complete, failed, in-flight or applied file is excluded and left untouched."""
    partial = await _seed_row(session, order=0)
    complete = await _seed_row(session, order=1)
    failed = await _seed_row(session, order=2)
    in_flight = await _seed_row(session, order=3)
    applied = await _seed_row(session, order=4)
    session.add_all(
        [
            AnalysisResult(file_id=partial, fine_windows_analyzed=10, fine_windows_total=240),
            AnalysisResult(file_id=complete, fine_windows_analyzed=240, fine_windows_total=240, analysis_completed_at=INSIDE),
            AnalysisResult(file_id=failed, failed_at=INSIDE, error_message="boom"),
            SchedulingLedger(key=f"process_file:{in_flight}", function="process_file", routing="agent", payload={"file_id": str(in_flight)}),
            RenameProposal(id=uuid.uuid4(), file_id=applied, proposed_filename="x.mp3", status=ProposalStatus.EXECUTED),
        ]
    )
    await session.commit()

    report = await apply_reset(session, SCOPE)
    await session.commit()

    by_file = {row.file_id: row for row in report.rows}
    assert (by_file[partial].disposition, by_file[partial].analysis) == (Disposition.RESET, AnalysisState.PARTIAL)
    assert (by_file[complete].disposition, by_file[complete].analysis) == (Disposition.EXCLUDED_ANALYZE_DONE, AnalysisState.COMPLETE)
    assert (by_file[failed].disposition, by_file[failed].analysis) == (Disposition.EXCLUDED_ANALYZE_DONE, AnalysisState.FAILED)
    assert by_file[in_flight].disposition is Disposition.EXCLUDED_ANALYZE_IN_FLIGHT
    assert by_file[applied].disposition is Disposition.EXCLUDED_APPLIED
    assert report.rows_reset == 1
    assert (await _job(session, partial)).attempts == 0
    for excluded in (complete, failed, in_flight, applied):
        assert (await _job(session, excluded)).attempts == CAP
        assert await _budget(session, excluded) is not None
        assert by_file[excluded].ledger is LedgerAction.UNTOUCHED
    assert "analysis rows by disposition: excluded_analyze_done=2 reset=1" in report.breakdown_lines()


@pytest.mark.asyncio
async def test_rows_that_touched_the_node_loss_budget_are_excluded(session: AsyncSession) -> None:
    redriven = await _seed_row(session, order=0, node_loss_redrives=1)
    pending = await _seed_row(session, order=1, node_loss_pending="node_lost (NotReady)")
    inadmissible = await _seed_row(session, order=2, inadmissible=True)

    report = await apply_reset(session, SCOPE)

    assert report.by_disposition == {Disposition.EXCLUDED_NODE_LOSS.value: 3}
    assert report.rows_reset == 0
    for file_id in (redriven, pending, inadmissible):
        assert (await _job(session, file_id)).attempts == CAP


@pytest.mark.asyncio
async def test_ledger_unfold_removes_only_this_chain(session: AsyncSession) -> None:
    """Two chains: decrement and keep the cooldown clock. A fold outside the window, or no fold at all: leave the ledger alone."""
    two_chains = await _seed_row(session, order=0, chains_spent=2)
    fold_elsewhere = await _seed_row(session, order=1, budget_spent_at=WINDOW_END + timedelta(days=1))
    no_ledger = await _seed_row(session, order=2, chains_spent=None)

    report = await apply_reset(session, SCOPE)
    await session.commit()

    by_file = {row.file_id: row.ledger for row in report.rows}
    assert by_file == {two_chains: LedgerAction.DECREMENTED, fold_elsewhere: LedgerAction.OUTSIDE_WINDOW, no_ledger: LedgerAction.NONE}
    assert (report.rows_reset, report.ledger_rows_deleted, report.ledger_rows_decremented) == (3, 0, 1)
    decremented = await _budget(session, two_chains)
    assert decremented is not None
    assert (decremented.chains_spent, decremented.attempts_spent, decremented.budget_spent_at) == (1, CAP, INSIDE)
    untouched = await _budget(session, fold_elsewhere)
    assert untouched is not None
    assert (untouched.chains_spent, untouched.attempts_spent) == (1, CAP)
    assert await _budget(session, no_ledger) is None


@pytest.mark.asyncio
async def test_apply_keeps_the_failure_record_and_a_second_run_selects_nothing(session: AsyncSession) -> None:
    """phaze-1xngw's columns are the post-mortem record and survive the reset. A rerun finds no rows: attempts is 0 and updated_at is outside the window."""
    failed_at = INSIDE - timedelta(seconds=5)
    file_id = await _seed_row(session, last_exit_code=10, last_failure_reason="Error", last_failed_at=failed_at)

    first = await apply_reset(session, SCOPE)
    await session.commit()
    second = await apply_reset(session, SCOPE)

    assert first.rows_reset == 1
    assert "by last_exit_code: 10=1" in first.breakdown_lines()
    assert second.rows == []
    assert second.rows_reset == 0
    job = await _job(session, file_id)
    assert (job.last_exit_code, job.last_failure_reason, job.last_failed_at) == (10, "Error", failed_at)


def test_audit_lines_name_every_row_by_file_id_only() -> None:
    row = ScopedRow(
        file_id=uuid.UUID(int=1),
        attempts=CAP,
        updated_at=INSIDE,
        last_exit_code=None,
        last_failure_reason=None,
        analysis=AnalysisState.NONE,
        disposition=Disposition.RESET,
        ledger=LedgerAction.CLEARED,
        ledger_chains_spent=1,
    )
    report = ResetReport(scope=SCOPE, rows=[row])

    assert report.audit_lines() == [f"  {uuid.UUID(int=1)}  reset                       attempts=3  analysis=none      ledger_cleared"]
    assert report.breakdown_lines()[-1] == f"updated_at span: {INSIDE.isoformat()} .. {INSIDE.isoformat()}"
    assert ResetReport(scope=SCOPE, rows=[]).breakdown_lines()[2] == "by disposition: (none)"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"window_start": WINDOW_START.replace(tzinfo=None)}, "timezone-aware"),
        ({"window_end": WINDOW_START}, "is not after"),
        ({"attempts_floor": 0}, "attempts floor must be >= 1"),
    ],
)
def test_scope_refuses_an_ambiguous_or_empty_window(kwargs: dict[str, Any], message: str) -> None:
    fields: dict[str, Any] = {"backend_id": BURST, "window_start": WINDOW_START, "window_end": WINDOW_END, "attempts_floor": CAP, **kwargs}
    with pytest.raises(ValueError, match=message):
        ResetScope(**fields)
