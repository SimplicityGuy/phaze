"""Scoped reset of cloud attempts that an INFRASTRUCTURE fault spent (phaze-ww6yk).

Spike ``phaze-79mu7`` traced 302 ``cloud_job`` rows parked at ``status='awaiting'`` with
``attempts = cloud_submit_max_attempts`` to two burst-node faults on 2026-09-26/27: the control plane
was down, then the burst node's cluster DNS lost its upstream. Every pod exited ``EXIT_DOWNLOAD``
seconds after admission because its presign POST never reached the API. Nothing was wrong with the
files, but the per-file budget is spent, so ``select_backend`` bars them from cloud for good
(``HOLD_CLOUD_ATTEMPTS_EXHAUSTED``) and they sit at the head of the drain's FIFO.

The operator asked for a scoped recovery command rather than hand-written SQL. This module holds its
selection and its write; ``phaze backfill reset-cloud-attempts`` is the thin CLI wrapper.

SCOPE. A row is in scope iff ``status='awaiting'``, ``attempts >= attempts_floor``, ``backend_id`` equals
the named backend, and ``updated_at`` lies in ``[window_start, window_end]``. All four are explicit
arguments, so the command names exactly one incident and cannot drift onto rows it was not asked about.

WHAT A RESET WRITES, AND WHY EACH PART IS NEEDED.

* ``cloud_job.attempts = 0``. This is the per-chain half of the D-04 budget.
* ``cloud_job.updated_at = now()``. That column is the drain's lane-entry clock (``lane_entered_at``).
  Left at the incident time, it would already exceed ``cloud_spill_to_local_after_seconds``, so a reset
  file would skip the cloud wait and could go straight to local. It also moves the row out of the
  window, which makes a second run select nothing.
* **Unfold the chain from the durable ``cloud_budget`` ledger.** Resetting ``attempts`` alone is not
  enough. When the chain spilled, ``hold_awaiting_cloud`` folded it into ``cloud_budget``
  (``chains_spent += 1``, ``attempts_spent += attempts``, ``budget_spent_at = now()``), and
  ``cloud_budget_hold_reason`` then bars cloud for ``cloud_budget_cooldown_days`` (default 14) after
  ``budget_spent_at``. So the unfold removes exactly what that fold added. The ledger row is deleted
  when this chain was its only chain. Otherwise it is decremented, and its cooldown clock stays as it
  is: the earlier ``budget_spent_at`` was overwritten by the fold and cannot be recovered, so such a
  file stays cloud-barred until the cooldown runs out. Those rows are counted separately. The unfold
  runs only when the ledger's ``budget_spent_at`` falls inside the same window. Otherwise the last fold
  was a different event, and the ledger is left untouched and reported.

WHAT A RESET KEEPS. ``last_exit_code`` / ``last_failure_reason`` / ``last_failed_at`` (phaze-1xngw) are
documented as never cleared: they are the post-mortem record of the failure that spent the budget, and
the next attempt overwrites them if it fails too. ``kueue_workload`` / ``telemetry_slot`` are inert on an
awaiting row and are re-stamped by the next dispatch.

EXCLUSIONS. Four classes of in-scope row are NOT reset. Each is reported with its count and, per row,
in the audit lines.

* ``analyze_done``: ``domain_completed_clause(ANALYZE)`` holds, meaning an analysis completed, was
  force-skipped or failed terminally. Resetting would spend a cloud burst on a file with nothing left
  to analyze. The drain does not offer these rows anyway (``awaiting_candidate_clause``).
* ``analyze_in_flight``: a ``process_file`` ledger row exists, so local owns the file right now.
  Handing it back to cloud could analyze it twice.
* ``applied``: an executed proposal moved the file, and ``original_path`` no longer names it. This is
  the same exclusion ``phaze backfill reenqueue-incomplete-analyses`` makes.
* ``node_loss``: the row spent node-loss budget or carries a pending node-loss or inadmissible marker.
  That is not this incident's signature, since the spike found none on any of the 302, and the
  node-loss ledger is a separate budget this command does not own.

A row with a PARTIAL analysis row (``analysis_completed_at`` and ``failed_at`` both NULL) IS reset. That
row is the start marker ``put_analysis`` writes at analysis START (D-03), or an incomplete-coverage row
that ``reenqueue-incomplete-analyses`` held for cloud on purpose. In both cases re-analysis is the
intent, and ``put_analysis`` replaces the windows wholesale. The ``analysis`` column of each audit line
shows which analysis state each row was in.

CONCURRENCY. ``apply`` takes the drain's ``pg_advisory_xact_lock(5_000_504)``, which serializes it with
``stage_cloud_window`` and every reconcile per-row unit. It also locks the selected rows
``FOR UPDATE OF cloud_job`` and repeats the full scope predicate in the UPDATE's ``WHERE`` (a CAS), so a
row that left scope between the SELECT and the UPDATE is not written. The caller owns the commit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import enum
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, case, delete, func, or_, select, text, update
from sqlalchemy.orm import aliased
import structlog

from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_budget import CloudBudget
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services.stage_status import applied_clause, domain_completed_clause, inflight_clause
from phaze.tasks.release_awaiting_cloud import _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY


if TYPE_CHECKING:
    from datetime import datetime
    import uuid

    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


logger = structlog.get_logger(__name__)


class Disposition(enum.StrEnum):
    """What the command does, or would do, with one in-scope row. Only ``RESET`` is written."""

    RESET = "reset"
    EXCLUDED_APPLIED = "excluded_applied"
    EXCLUDED_ANALYZE_DONE = "excluded_analyze_done"
    EXCLUDED_ANALYZE_IN_FLIGHT = "excluded_analyze_in_flight"
    EXCLUDED_NODE_LOSS = "excluded_node_loss"


class LedgerAction(enum.StrEnum):
    """What the reset does to the row's durable ``cloud_budget`` entry (see the module docstring)."""

    NONE = "no_ledger_row"
    CLEARED = "ledger_cleared"
    DECREMENTED = "ledger_decremented_cooldown_kept"
    OUTSIDE_WINDOW = "ledger_fold_outside_window"
    UNTOUCHED = "ledger_untouched"  # the row is excluded, so its ledger is not this command's to edit


class AnalysisState(enum.StrEnum):
    """The file's ``analysis`` row as the audit line reports it."""

    NONE = "none"
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True)
class ResetScope:
    """The four explicit scope parameters. Every one is required at the CLI; none is inferred."""

    backend_id: str
    window_start: datetime
    window_end: datetime
    attempts_floor: int

    def __post_init__(self) -> None:
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
            msg = "window bounds must be timezone-aware (cloud_job.updated_at is timestamptz)"
            raise ValueError(msg)
        if self.window_end <= self.window_start:
            msg = f"window end {self.window_end.isoformat()} is not after window start {self.window_start.isoformat()}"
            raise ValueError(msg)
        if self.attempts_floor < 1:
            msg = f"attempts floor must be >= 1 (got {self.attempts_floor}); a floor of 0 would select rows with budget left"
            raise ValueError(msg)


@dataclass(frozen=True)
class ScopedRow:
    """One in-scope ``cloud_job`` row, classified. Carries ids and counters only, never an archive path."""

    file_id: uuid.UUID
    attempts: int
    updated_at: datetime
    last_exit_code: int | None
    last_failure_reason: str | None
    analysis: AnalysisState
    disposition: Disposition
    ledger: LedgerAction
    ledger_chains_spent: int | None


@dataclass
class ResetReport:
    """The command's full account: the scope, every in-scope row, and what was written."""

    scope: ResetScope
    rows: list[ScopedRow]
    applied: bool = False
    rows_reset: int = 0
    ledger_rows_deleted: int = 0
    ledger_rows_decremented: int = 0

    @property
    def by_disposition(self) -> Counter[str]:
        return Counter(row.disposition.value for row in self.rows)

    @property
    def to_reset(self) -> list[ScopedRow]:
        return [row for row in self.rows if row.disposition is Disposition.RESET]

    def breakdown_lines(self) -> list[str]:
        """The count and its breakdown. Printed before any per-row line, so the total comes first."""
        scope = self.scope
        lines = [
            (
                f"scope: status='awaiting' backend_id={scope.backend_id!r} attempts>={scope.attempts_floor} "
                f"updated_at in [{scope.window_start.isoformat()}, {scope.window_end.isoformat()}]"
            ),
            f"{len(self.rows)} row(s) in scope",
            "by disposition: " + _tally(self.by_disposition, [d.value for d in Disposition]),
            "by attempts: " + _tally(Counter(str(row.attempts) for row in self.rows)),
            "by analysis row: " + _tally(Counter(row.analysis.value for row in self.rows), [a.value for a in AnalysisState]),
            "analysis rows by disposition: " + _tally(Counter(row.disposition.value for row in self.rows if row.analysis is not AnalysisState.NONE)),
            "by ledger action: " + _tally(Counter(row.ledger.value for row in self.rows), [a.value for a in LedgerAction]),
            "by last_exit_code: " + _tally(Counter("NULL" if row.last_exit_code is None else str(row.last_exit_code) for row in self.rows)),
        ]
        if self.rows:
            first = min(row.updated_at for row in self.rows)
            last = max(row.updated_at for row in self.rows)
            lines.append(f"updated_at span: {first.isoformat()} .. {last.isoformat()}")
        return lines

    def audit_lines(self) -> list[str]:
        """One line per in-scope row: file id, disposition, attempts, analysis state, ledger action."""
        return [
            f"  {row.file_id}  {row.disposition.value:<26}  attempts={row.attempts}  analysis={row.analysis.value:<8}  {row.ledger.value}"
            for row in self.rows
        ]


def _tally(counter: Counter[str], order: list[str] | None = None) -> str:
    keys = [key for key in order if counter.get(key)] if order is not None else sorted(counter)
    return " ".join(f"{key}={counter[key]}" for key in keys) or "(none)"


def _scope_clause(scope: ResetScope) -> ColumnElement[bool]:
    """The four-part scope predicate. Used by the SELECT and repeated in the UPDATE (the CAS)."""
    return and_(
        CloudJob.status == CloudJobStatus.AWAITING.value,
        CloudJob.attempts >= scope.attempts_floor,
        CloudJob.backend_id == scope.backend_id,
        CloudJob.updated_at >= scope.window_start,
        CloudJob.updated_at <= scope.window_end,
    )


def _node_loss_clause() -> ColumnElement[bool]:
    return or_(CloudJob.node_loss_redrives > 0, CloudJob.node_loss_pending.isnot(None), CloudJob.inadmissible.is_(True))


async def select_scope(session: AsyncSession, scope: ResetScope, *, for_update: bool = False) -> list[ScopedRow]:
    """Return every in-scope row, classified, in the drain's FIFO order. Read-only.

    Classification reuses the canonical ``stage_status`` builders (``applied_clause``,
    ``domain_completed_clause``, ``inflight_clause``) rather than re-spelling them, so "done" and
    "in flight" mean here exactly what they mean to the drain.
    """
    # Aliased so the correlated ``exists(analysis ...)`` inside ``domain_completed_clause`` keeps its own
    # FROM. Joining the bare entity here would auto-correlate that subquery onto this outer join.
    analysis = aliased(AnalysisResult)
    analysis_state = case(
        (analysis.id.is_(None), AnalysisState.NONE.value),
        (analysis.analysis_completed_at.isnot(None), AnalysisState.COMPLETE.value),
        (analysis.failed_at.isnot(None), AnalysisState.FAILED.value),
        else_=AnalysisState.PARTIAL.value,
    )
    stmt = (
        select(
            CloudJob.file_id,
            CloudJob.attempts,
            CloudJob.updated_at,
            CloudJob.last_exit_code,
            CloudJob.last_failure_reason,
            analysis_state.label("analysis_state"),
            applied_clause().label("applied"),
            domain_completed_clause(Stage.ANALYZE).label("analyze_done"),
            inflight_clause(Stage.ANALYZE).label("analyze_in_flight"),
            _node_loss_clause().label("node_loss"),
            CloudBudget.chains_spent,
            CloudBudget.budget_spent_at,
        )
        .select_from(CloudJob)
        .join(FileRecord, FileRecord.id == CloudJob.file_id)
        .outerjoin(analysis, analysis.file_id == CloudJob.file_id)
        .outerjoin(CloudBudget, CloudBudget.file_id == CloudJob.file_id)
        .where(_scope_clause(scope))
        .order_by(FileRecord.created_at.asc(), FileRecord.id.asc())
    )
    if for_update:
        stmt = stmt.with_for_update(of=CloudJob)
    return [_classify(row, scope) for row in (await session.execute(stmt)).all()]


def _classify(row: Any, scope: ResetScope) -> ScopedRow:
    if row.applied:
        disposition = Disposition.EXCLUDED_APPLIED
    elif row.analyze_done:
        disposition = Disposition.EXCLUDED_ANALYZE_DONE
    elif row.analyze_in_flight:
        disposition = Disposition.EXCLUDED_ANALYZE_IN_FLIGHT
    elif row.node_loss:
        disposition = Disposition.EXCLUDED_NODE_LOSS
    else:
        disposition = Disposition.RESET

    if disposition is not Disposition.RESET:
        ledger = LedgerAction.UNTOUCHED
    elif row.budget_spent_at is None:
        ledger = LedgerAction.NONE
    elif not scope.window_start <= row.budget_spent_at <= scope.window_end:
        ledger = LedgerAction.OUTSIDE_WINDOW
    elif row.chains_spent <= 1:
        ledger = LedgerAction.CLEARED
    else:
        ledger = LedgerAction.DECREMENTED

    return ScopedRow(
        file_id=row.file_id,
        attempts=int(row.attempts),
        updated_at=row.updated_at,
        last_exit_code=row.last_exit_code,
        last_failure_reason=row.last_failure_reason,
        analysis=AnalysisState(row.analysis_state),
        disposition=disposition,
        ledger=ledger,
        ledger_chains_spent=None if row.chains_spent is None else int(row.chains_spent),
    )


async def preview_reset(session: AsyncSession, scope: ResetScope) -> ResetReport:
    """The dry run: select and classify, write nothing. The caller should run it read-only."""
    return ResetReport(scope=scope, rows=await select_scope(session, scope))


async def apply_reset(session: AsyncSession, scope: ResetScope) -> ResetReport:
    """Reset every ``RESET``-classified row and unfold its chain from the ledger. NEVER commits.

    The report's ``rows`` are the classification taken under the lock, which is the population that
    was written. ``rows_reset`` is the UPDATE's own rowcount, so a mismatch against
    ``len(report.to_reset)`` means a row left scope between the SELECT and the UPDATE.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
    report = ResetReport(scope=scope, rows=await select_scope(session, scope, for_update=True), applied=True)
    targets = report.to_reset
    if not targets:
        return report

    # Unfold before the reset. Each row's own ``attempts`` is what the fold added, and the reset is about to zero it.
    for row in targets:
        if row.ledger is LedgerAction.CLEARED:
            await session.execute(delete(CloudBudget).where(CloudBudget.file_id == row.file_id))
            report.ledger_rows_deleted += 1
        elif row.ledger is LedgerAction.DECREMENTED:
            await session.execute(
                update(CloudBudget)
                .where(CloudBudget.file_id == row.file_id)
                .values(
                    chains_spent=CloudBudget.chains_spent - 1,
                    attempts_spent=func.greatest(CloudBudget.attempts_spent - row.attempts, 0),
                    updated_at=func.now(),
                )
            )
            report.ledger_rows_decremented += 1

    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(CloudJob)
            .where(CloudJob.file_id.in_([row.file_id for row in targets]), _scope_clause(scope))
            .values(attempts=0, updated_at=func.now())
        ),
    )
    report.rows_reset = result.rowcount
    logger.warning(
        "reset_cloud_attempts: applied",
        backend_id=scope.backend_id,
        window_start=scope.window_start.isoformat(),
        window_end=scope.window_end.isoformat(),
        attempts_floor=scope.attempts_floor,
        in_scope=len(report.rows),
        rows_reset=report.rows_reset,
        ledger_rows_deleted=report.ledger_rows_deleted,
        ledger_rows_decremented=report.ledger_rows_decremented,
        by_disposition=dict(report.by_disposition),
    )
    return report
