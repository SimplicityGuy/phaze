"""SQL ``ColumnElement`` half of the single-source per-stage predicate layer (Phase 78, D-04).

This module is the database-side twin of the DB-free :mod:`phaze.enums.stage` resolver. It exposes
composable :class:`~sqlalchemy.ColumnElement` builders -- ``done_clause`` / ``failed_clause`` /
``inflight_clause`` per stage, and ``stage_status_case`` which composes them into the 4-way status
CASE ladder -- so EVERY later-phase reader gets ONE place to drop a per-stage predicate into a
``.where(...)``. The DERIV-04 equivalence test
(``tests/integration/test_stage_status_equivalence.py``) locks these builders against the Python
resolver so the two can NEVER drift.

**PURELY ADDITIVE** (Phase 78): no existing reader or writer is wired to these builders here. The
pending-set / counts / recovery / DAG readers cut over in Phase 82+ behind the shadow-compare gate.

Per-stage semantics (locked in 78-CONTEXT.md, mirrored 1:1 in :func:`phaze.enums.stage.resolve_status`):
- precedence ``in_flight ≻ done ≻ failed ≻ not_started`` (DERIV-02 -- the SAQ ledger wins).
- ``done(analyze)`` requires ``analysis_completed_at IS NOT NULL`` (DERIV-03 -- a partial in-flight
  row upserted at analysis START has ``completed_at`` NULL and is NOT done).
- ``done(metadata)`` requires a row present AND ``failed_at IS NULL`` (D-03 -- a failure-only row is
  FAILED, not DONE).
- ``done(apply)`` joins ``execution_log`` through ``proposals`` on ``proposal_id`` (``execution_log``
  has NO ``file_id``) and requires a ``completed`` execution row. This is DISTINCT from apply
  *eligibility* (ELIG-02: an APPROVED proposal exists) -- see the ``inflight_clause`` /
  apply-eligibility note below.

All anti-joins use correlated ``~exists(...)`` -- never an outer-join-null or negated-membership
anti-pattern. Every operand is an ORM column or a bound param; the sole raw SQL is the
SAVEPOINT-isolated ``saq_detail`` read (static status allowlist, no interpolation).

================================================================================================
D-01 DECISION RECORD (written record, INFLIGHT-03 / SC#5) -- the authoritative ``in_flight`` source
================================================================================================
The AUTHORITATIVE source of ``in_flight`` is the durable :class:`~phaze.models.scheduling_ledger.SchedulingLedger`:
a ledger row on the ``(file, stage-function)`` key -- i.e. ``"<function>:<file_id>"`` -- means the
stage is in flight. ``saq_jobs`` (the SAQ-owned broker table) is a CORROBORATING signal ONLY and
NEVER flips the ``in_flight`` boolean.

Rationale (durability): the scheduling ledger survives a broker truncate/restore (the only genuine
post-Phase-36 Postgres-broker loss case). A file that crashed mid-run, or whose completion callback
was lost, keeps its ledger row and therefore reads ``in_flight`` -- it is NEVER falsely
``not_started``. This directly guards the 2026-06-18 over-enqueue class (~44.5K jobs), where
recovery re-queued never-scheduled work because there was no durable "was scheduled" fact.

Rejected alternatives:
- ``saq_jobs`` UNION ``ledger`` (the set union): couples the hot ``/pipeline/stats`` poll to broker liveness and
  reintroduces the false-``not_started`` window on a broker loss. Rejected.
- ``saq_jobs`` alone: the pre-ledger design behind the over-enqueue incident. Rejected.

Consequently ``saq_jobs`` is READ-ONLY here, detail-only, SAVEPOINT-isolated (``saq_detail``), and
degrades to a safe default on ANY error; **Alembic NEVER references ``saq_jobs``** (Phase-77 banner
carried forward -- this plan adds no migration).
================================================================================================

================================================================================================
D-01a AMENDMENT (phaze-2u8v.2) -- a ledger row means SCHEDULED, which is not the same as RUNNING
================================================================================================
D-01 above is UNCHANGED and still correct: :func:`inflight_clause` stays ledger-only, ``saq_jobs``
still never flips it, and the locked ladder / :func:`eligible_clause` / recovery all keep reading
exactly what they read before. This amendment adds the fact D-01 left unsaid, and the reporting
predicates that fact requires.

THE MEASUREMENT that forced it (live archive, ``process_file``/analyze lane, 2026-07-28)::

    scheduling_ledger rows for 'process_file'                            4963   <- reported in_flight
      of which a LIVE (queued/active) saq_jobs row exists                2583   <- SAQ's own truth
      of which no saq_jobs row but a busy cloud_job (compute dispatch)     58   <- really in flight
      of which the stage has DOMAIN-COMPLETED (done/skipped/failed)       176   <- resolved, leaked row
      of which nothing is running and nothing completed                  2146   <- ORPHANED

A ledger row is written at the ``before_enqueue`` chokepoint and deleted ONLY by a terminal-outcome
callback. It carries no liveness corroboration and no expiry, and until this bead NOTHING anywhere
reconciled one. So every job that dies without running its terminal clear leaks a ledger row that
reads ``in_flight`` FOREVER, and the count is monotonically non-decreasing in those leaks. 2145 of
the 2146 orphans above were enqueued on a single day (2026-06-21, the remediation window of the
2026-06-18 over-enqueue incident); the 176 resolved rows accrue continuously, and one of their
producers is ``reap_stuck_aborting_jobs`` itself, which DELETEs the ``saq_jobs`` row to release the
deterministic key and deliberately leaves the ledger row standing.

RELATION TO CLOSED EPIC phaze-qmc2 -- **NEVER-COVERED, not a regression.** That epic's charter was
``saq_jobs`` / ``scan_batches`` / ``cloud_job`` status honesty, and all eight of its members touch
only those three tables. ``scheduling_ledger`` was never brought under its "a row's status must
describe reality" rule, so its guarantees did not regress here -- they simply stop one table short
of the table this counter reads.

THE SPLIT. "Scheduled and unresolved" (the ledger) is the union of three populations, and the
reporting layer must not call all three ``in_flight``:

- RUNNING     -- a live ``saq_jobs`` key, or a busy ``cloud_job`` for compute dispatch that SAQ
                 cannot see. This, and only this, is in flight.
- ORPHANED    -- :func:`orphaned_clause`. Previously scheduled, running nowhere, not domain-complete.
                 Genuinely owed work: the ledger is RIGHT to hold the row (that IS the 2026-06-18
                 over-enqueue guard) and ``recover_orphaned_work`` is what re-drives it. Already a
                 first-class concept with its own count (``get_stage_orphan_counts``) and its own
                 amber rail badge -- so folding it into ``in_flight`` was a DOUBLE-COUNT of an
                 already-named bucket, not a missing label.
- RESOLVED    -- :func:`resolved_ledger_clause`. Running nowhere AND domain-complete: the terminal
                 clear was lost. Pure stale state, safe to reconcile away -- see
                 :mod:`phaze.tasks.ledger_reaper`.

WHY THIS DOES NOT REOPEN D-01's REJECTED ALTERNATIVE. The rejected design put ``saq_jobs`` inside
the boolean every reader depends on, which couples the hot poll to broker liveness and re-admits a
false ``not_started`` on a broker loss. These clauses instead sit OUTSIDE the ``Stage`` dispatch
ladder (like :func:`dedup_resolved_clause` / :func:`awaiting_candidate_clause`, so the DERIV-04
equivalence test never picks them up), are consumed ONLY by reporting readers that are already
SAVEPOINT-isolated and degrade to zero, and are read by NO eligibility or recovery path. On an
unreadable ``saq_jobs`` the orphan split degrades to 0 and every count reverts to exactly today's
ledger-only behavior. A broker truncate therefore still leaves those files reading ORPHANED --
never ``not_started`` -- which is the guarantee D-01's durability rationale actually protects.
``saq_jobs`` stays READ-ONLY, and it is referenced through a bare :func:`~sqlalchemy.table` clause
that is NOT attached to ``Base.metadata``, so **Alembic still never sees it**.
================================================================================================
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, String, and_, case, cast, column as sa_column, exists, false, func, not_, or_, select, table as sa_table, text
import structlog

from phaze.enums.stage import ELIGIBLE_AFTER_FAILURE, FAILURE_IS_TERMINAL, Stage, Status
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import RenameProposal
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.models.tracklist import Tracklist
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


def dedup_resolved_clause() -> ColumnElement[bool]:
    """Return the correlated ``dedup-resolved`` predicate for a file (a ``ColumnElement[bool]``).

    This is a **FILE-LEVEL** predicate, NOT a per-:class:`~phaze.enums.stage.Stage` one -- dedup
    resolution is a corpus-hygiene fact about a file, not one of the pipeline stages. It takes NO
    ``stage`` argument and correlates to :class:`~phaze.models.file.FileRecord` in the enclosing query
    via a correlated ``exists(...)`` (never an outer-join-null / negated-membership anti-pattern),
    identical in body to the Phase-90-retired ``shadow_compare._dedup_exists`` migration-verification
    helper (the module was removed with ``files.state`` in Phase 90's writer-removal cleanup). Marker-row
    existence means resolved; ``~dedup_resolved_clause()`` therefore means "not resolved" (the shape the Wave-2
    dedup readers consume).

    It is deliberately kept OUT of the ``Stage`` dispatch ladders (:func:`done_clause` /
    :func:`failed_clause` / :func:`inflight_clause` / :func:`domain_completed_clause` /
    :func:`stage_status_case`) -- those all raise ``ValueError`` on an unknown stage and are
    drift-locked to the Python resolver by ``tests/integration/test_stage_status_equivalence.py`` (D-13).
    A non-``Stage`` clause must not touch that test.

    Consumers import this predicate from here (the single-source predicate module, Phase 78) --
    ``services/dedup.py`` does so at module level. A consumer that sits on the agent side of the
    import boundary must import it INSIDE its function instead (D-00e).
    """
    return exists(select(DedupResolution.id).where(DedupResolution.file_id == FileRecord.id))


def applied_clause() -> ColumnElement[bool]:
    """Return the correlated ``applied`` predicate for a file (a ``ColumnElement[bool]``).

    READ-05 / D-01: a file is ``applied`` iff an ``executed`` proposal exists for it --
    ``exists(proposals WHERE file_id == FileRecord.id AND status == 'executed')``. This is the
    single authoritative apply-outcome source: ``proposals.status`` is transactionally coupled to the
    agent's copy->verify->delete apply path (an IO failure forces ``status='failed'``), whereas a
    ``FileState.EXECUTED`` value is produced by NO writer in ``src/`` (the whole reason READ-05's gates
    were dead). This predicate therefore NEVER reads the file's ``state`` column and NEVER touches
    ``execution_log`` (a best-effort, swallowed-exception audit log that can false-positive on a
    stale/deleted path -- T-85-02).

    Like :func:`dedup_resolved_clause`, this is a **FILE-LEVEL** predicate: it takes NO ``stage``
    argument, correlates to :class:`~phaze.models.file.FileRecord` via a correlated ``exists(...)``,
    and is deliberately kept OUT of the ``Stage`` dispatch ladders (:func:`done_clause` /
    :func:`failed_clause` / :func:`stage_status_case`) so it never perturbs the DERIV-04 equivalence
    test. Do NOT reuse ``done_clause(Stage.APPLY)`` here -- that joins ``execution_log`` (rejected by
    D-01).

    A file CAN carry multiple non-pending proposals (``uq_proposals_file_id_pending`` enforces one
    PENDING proposal per file ONLY); a file with BOTH a ``failed`` and an ``executed`` proposal is
    applied. ``exists(status == 'executed')`` is the correct authoritative multi-proposal test.
    """
    return exists(
        select(RenameProposal.id).where(
            RenameProposal.file_id == FileRecord.id,
            RenameProposal.status == "executed",  # ProposalStatus.EXECUTED.value
        )
    )


async def is_applied(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """READ-05 / D-01 per-record twin of :func:`applied_clause` -- ``True`` iff an executed proposal exists.

    Issues a single scalar ``EXISTS`` query for ``file_id`` (for the write guards that hold a
    ``file_id`` + ``session`` but no proposal). NEVER reads the file's ``state`` column, NEVER touches
    ``execution_log``, and never lazy-loads ``proposal.file`` (``lazy="raise"``).
    """
    return bool(
        await session.scalar(
            select(
                exists(
                    select(RenameProposal.id).where(
                        RenameProposal.file_id == file_id,
                        RenameProposal.status == "executed",  # ProposalStatus.EXECUTED.value
                    )
                )
            )
        )
    )


def done_clause(stage: Stage) -> ColumnElement[bool]:
    """Return the correlated ``done`` predicate for ``stage`` (a ``ColumnElement[bool]``).

    Correlates to :class:`~phaze.models.file.FileRecord` in the enclosing query. Uses ``exists(...)``
    only (never an outer-join-null / negated-membership anti-pattern). The Phase-77 partial indexes
    back each probe.
    """
    if stage is Stage.ANALYZE:
        # DERIV-03: completion discriminator, NOT bare row existence (a partial in-flight row has NULL).
        return exists(select(AnalysisResult.id).where(AnalysisResult.file_id == FileRecord.id, AnalysisResult.analysis_completed_at.isnot(None)))
    if stage is Stage.METADATA:
        # D-03: a row present AND not a failure-only row.
        return exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id, FileMetadata.failed_at.is_(None)))
    if stage is Stage.TRACKLIST:
        return exists(select(Tracklist.id).where(Tracklist.file_id == FileRecord.id))
    if stage in (Stage.PROPOSE, Stage.REVIEW):
        # Presence: done = a proposal exists (ELIG-02 review semantics; RESEARCH OQ2 resolution).
        return exists(select(RenameProposal.id).where(RenameProposal.file_id == FileRecord.id))
    if stage is Stage.APPLY:
        # execution_log has NO file_id -- join through proposals (Pitfall 4).
        return exists(
            select(ExecutionLog.id)
            .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
            .where(RenameProposal.file_id == FileRecord.id, ExecutionLog.status == "completed")
        )
    raise ValueError(f"unknown stage: {stage!r}")  # pragma: no cover - exhaustive dispatch above


def skipped_clause(stage: Stage) -> ColumnElement[bool]:
    """Return the correlated ``skipped`` predicate for an ENRICH ``stage`` (a ``ColumnElement[bool]``).

    D-08 force-skip marker: a ``stage_skip`` row on ``(file_id, stage)`` means the operator has
    force-skipped this enrich stage. Mirrors :func:`done_clause`'s correlated-``exists`` shape (marker-row
    existence = the fact), correlating to :class:`~phaze.models.file.FileRecord` in the enclosing query --
    never an outer-join-null / negated-membership anti-pattern. Every operand is an ORM column or the
    bound ``stage.value`` param (T-87-05: never f-string SQL).

    Defined ONLY for the two enrich stages (the keys of :data:`~phaze.enums.stage.ELIGIBLE_AFTER_FAILURE`),
    mirroring :func:`eligible_clause`'s enrich-only guard: force-skip is an enrich-only affordance (D-10),
    so reaching for it on a downstream stage raises ``ValueError`` (T-87-06). The ``stage_skip`` table's
    own ``CHECK(stage IN ('metadata','analyze'))`` is the storage-side twin of this guard.
    """
    if stage not in ELIGIBLE_AFTER_FAILURE:
        # Mirrors the Python twin's guard, including the raw-`str` stage case (see enums/stage.py).
        got = getattr(stage, "value", stage)
        raise ValueError(f"skipped_clause is defined only for the enrich stages {sorted(s.value for s in ELIGIBLE_AFTER_FAILURE)}; got {got!r}")
    return exists(select(StageSkip.id).where(StageSkip.file_id == FileRecord.id, StageSkip.stage == stage.value))


def failed_clause(stage: Stage) -> ColumnElement[bool]:
    """Return the correlated ``failed`` predicate for ``stage`` (a ``ColumnElement[bool]``).

    Note the ladder precedence ``done ≻ failed`` in :func:`stage_status_case`: for the presence
    stages (propose/review/apply) a row that also satisfies ``done`` is reported ``done``, so this
    ``failed`` branch only surfaces when the stage is not otherwise done.
    """
    if stage is Stage.ANALYZE:
        return exists(select(AnalysisResult.id).where(AnalysisResult.file_id == FileRecord.id, AnalysisResult.failed_at.isnot(None)))
    if stage is Stage.METADATA:
        return exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id, FileMetadata.failed_at.isnot(None)))
    if stage is Stage.TRACKLIST:
        return false()  # no failure marker on tracklists
    if stage in (Stage.PROPOSE, Stage.REVIEW):
        return exists(select(RenameProposal.id).where(RenameProposal.file_id == FileRecord.id, RenameProposal.status == "failed"))
    if stage is Stage.APPLY:
        return exists(
            select(ExecutionLog.id)
            .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
            .where(RenameProposal.file_id == FileRecord.id, ExecutionLog.status == "failed")
        )
    raise ValueError(f"unknown stage: {stage!r}")  # pragma: no cover - exhaustive dispatch above


def ledger_key_for_function(func_name: str) -> ColumnElement[str]:
    """Return the deterministic ``"<function>:<file_id>"`` ledger-key expression, correlated to ``files``.

    ONE spelling of the key, shared by every probe that has to talk about a ledger row: the
    stage-keyed :func:`inflight_clause` / :func:`live_job_clause` and the function-keyed
    :func:`inflight_for_function` / :func:`live_job_for_function` below. A re-spelled prefix silently
    mismatches the real ledger PK, so the concat lives here exactly once.
    """
    return func.concat(func_name + ":", cast(FileRecord.id, String))


def inflight_for_function(func_name: str) -> ColumnElement[bool]:
    """Return "a ``scheduling_ledger`` row exists on ``<func_name>:<file_id>``" (the D-01 fact, by FUNCTION).

    The function-keyed core of :func:`inflight_clause`. It exists because the cloud-lane keyed
    producers (``s3_upload`` / ``push_file``) have a per-file ledger key but are NOT
    :class:`~phaze.enums.stage.Stage` members, so they cannot be reached through the stage ladder --
    and phaze-k95r7's stale ``s3_upload`` rows are precisely rows nothing could name.
    """
    return exists(select(SchedulingLedger.key).where(SchedulingLedger.key == ledger_key_for_function(func_name)))


def inflight_clause(stage: Stage) -> ColumnElement[bool]:
    """Return ``in_flight`` for ``stage`` -- authoritative from ``scheduling_ledger`` (D-01).

    ``in_flight`` iff a ledger row exists on the deterministic ``"<function>:<file_id>"`` key. The
    function name is looked up in :data:`STAGE_TO_FUNCTION` (imported, never re-spelled -- a
    re-spelled key silently mismatches the real ledger PK). ``saq_jobs`` is NEVER consulted for the
    boolean (D-01/D-02).

    Only the two file-keyed enrich stages have a per-file ledger key. ``propose`` is keyed on a
    batch set-hash (``sha256(sorted file_ids)``), NOT per-file, so there is no per-file
    ``in_flight(propose)`` -- scoped OUT of Phase 78 (RESEARCH Pitfall 5 / OQ1). The downstream
    presence stages likewise have no file-keyed enqueue, so they return a constant ``false()``,
    matching the Python twin (which defaults ``inflight`` to ``False`` for those stages).
    """
    func_name = STAGE_TO_FUNCTION.get(stage.value)
    if func_name is None:
        return false()
    return inflight_for_function(func_name)


def domain_completed_clause(stage: Stage) -> ColumnElement[bool]:
    """SQL twin of :func:`phaze.enums.stage.domain_completed` -- has ``stage`` reached a DOMAIN-COMPLETE state?

    ``DONE`` and ``SKIPPED`` (D-08 force-skip marker) are always domain-complete; a ``FAILED`` stage
    counts as complete ONLY when its failure is
    terminal (:data:`~phaze.enums.stage.FAILURE_IS_TERMINAL`). Reuses the LOCKED ``done_clause`` /
    ``skipped_clause`` / ``failed_clause`` predicates verbatim (never a fresh CASE) so this stays
    byte-equivalent to its ``ColumnElement`` siblings and the Python twin -- drift-locked by the
    equivalence test (``tests/integration/test_stage_status_equivalence.py``), D-17.

    When ``FAILURE_IS_TERMINAL[stage]`` is ``False`` the failure disjunct is dropped and the clause
    collapses to ``or_(done_clause, skipped_clause)`` -- an auto-retrying stage is not domain-complete
    on failure, but a force-skipped one still is. No surviving stage sets ``False`` (phaze-0jpe removed
    the one that did, fingerprint); the gate stays TABLE-DRIVEN rather than being folded flat, because
    the axis it encodes is a property of a stage's retry policy, not a constant of the pipeline.

    Defined ONLY for the two enrich stages (the keys of :data:`~phaze.enums.stage.FAILURE_IS_TERMINAL`),
    matching the Python twin. Without this guard the bare subscript raised ``KeyError`` for the four
    downstream stages while the Python twin happily returned ``True`` for a ``DONE`` one -- a silent twin
    divergence on every non-failed downstream row.

    D-11 REJECTED OPTION (do NOT "harden" this clause): ``~inflight_clause(stage)`` MUST NEVER be
    added as a conjunct here. Every recovery candidate is a scheduling-ledger row BY CONSTRUCTION, so
    ``~inflight_clause`` would be False for every candidate, making ``domain_completed`` return False
    for ALL of them -- silently disabling the secondary over-enqueue net (the 2026-06-18 ~44.5K-job
    incident class) while staying a green no-op for the drain/card (which already conjoin
    ``~inflight_clause`` separately in :func:`awaiting_candidate_clause`). This clause answers ONLY
    "has the domain reached a terminal state?" and must stay orthogonal to in-flight-ness.
    """
    if stage not in FAILURE_IS_TERMINAL:
        # Mirrors the Python twin's guard, including the raw-`str` stage case (see enums/stage.py).
        got = getattr(stage, "value", stage)
        raise ValueError(f"domain_completed_clause is defined only for the enrich stages {sorted(s.value for s in FAILURE_IS_TERMINAL)}; got {got!r}")
    # D-08: a force-skipped stage is ALWAYS domain-complete (recovery must never re-enqueue it), so
    # `skipped_clause` is an unconditional disjunct alongside `done_clause` (matching the Python twin's
    # `st in (DONE, SKIPPED)`). The terminal-failure disjunct stays gated on FAILURE_IS_TERMINAL.
    disjuncts = [done_clause(stage), skipped_clause(stage)]
    if FAILURE_IS_TERMINAL[stage]:
        disjuncts.append(failed_clause(stage))
    return or_(*disjuncts)


def eligible_clause(stage: Stage) -> ColumnElement[bool]:
    """SQL twin of :func:`phaze.enums.stage.eligible` for the two ENRICH stages only (READ-01).

    Mirrors the Python truth (``enums/stage.py``, the enrich branch)::

        status not in (DONE, IN_FLIGHT, SKIPPED) and (status != FAILED or ELIGIBLE_AFTER_FAILURE[stage])

    Because :func:`stage_status_case` applies the precedence ladder ``in_flight ≻ done ≻ skipped ≻ failed
    ≻ not_started`` (the SAQ ledger wins), a file is none of ``IN_FLIGHT`` / ``DONE`` / ``SKIPPED`` iff
    ``~inflight_clause(stage)`` and ``~done_clause(stage)`` and ``~skipped_clause(stage)`` all hold -- so
    ``status not in (DONE, IN_FLIGHT, SKIPPED)`` maps exactly to ``~inflight ∧ ~done ∧ ~skipped`` (D-08: a
    force-skipped stage leaves the pending set). The FAILED carve-out is TABLE-DRIVEN off
    :data:`~phaze.enums.stage.ELIGIBLE_AFTER_FAILURE` (NEVER an inline per-stage identity check):

    - metadata (``ELIGIBLE_AFTER_FAILURE True`` -- ELIG-04 auto-retry): ``~inflight ∧ ~done``
      leaves a FAILED (and a NOT_STARTED) row eligible.
    - analyze (``ELIGIBLE_AFTER_FAILURE False`` -- ELIG-03 terminal, manual retry only): append
      ``~failed_clause(stage)`` so ONLY a NOT_STARTED analyze is eligible. This is the load-bearing
      conjunct behind the 2026-06-18 ~44.5K over-enqueue guard; dropping it re-admits failed analyze
      rows to the pending set (the ``(ANALYZE, seed_analysis_failed, False)`` drift cell + mutation
      check in the equivalence test guard it).

    ``has_approved_proposal`` (an APPLY-only flag in the Python twin) is irrelevant here, so the
    signature stays a single ``stage`` param.

    Defined ONLY for the two enrich stages (the keys of :data:`~phaze.enums.stage.ELIGIBLE_AFTER_FAILURE`),
    matching the Python twin -- reaching for eligibility on a downstream stage via THIS builder is a
    question this layer deliberately does not answer, so it raises ``ValueError`` (same shape as
    :func:`domain_completed_clause`).

    Correlated-``~exists`` join contract: composes ``inflight_clause`` / ``done_clause`` /
    ``failed_clause`` verbatim, each a correlated ``~exists(... == FileRecord.id)``, so the enclosing
    query MUST select-from / join :class:`~phaze.models.file.FileRecord` (the pending-set queries
    already do). Dedup is a file-level fact kept OUT of here: compose ``~dedup_resolved_clause()`` at
    the ``pipeline.py`` query level (D-03), not inside ``eligible_clause``.
    """
    if stage not in ELIGIBLE_AFTER_FAILURE:
        # Mirrors the Python twin's guard, including the raw-`str` stage case (see enums/stage.py).
        got = getattr(stage, "value", stage)
        raise ValueError(f"eligible_clause is defined only for the enrich stages {sorted(s.value for s in ELIGIBLE_AFTER_FAILURE)}; got {got!r}")
    conjuncts = [not_(inflight_clause(stage)), not_(done_clause(stage)), not_(skipped_clause(stage))]  # D-08: a skipped stage leaves the pending set
    if not ELIGIBLE_AFTER_FAILURE[stage]:  # analyze: a FAILED analyze is terminal (ELIG-03 over-enqueue guard)
        conjuncts.append(not_(failed_clause(stage)))
    return and_(*conjuncts)


def awaiting_candidate_clause() -> ColumnElement[bool]:
    """Return the single-source awaiting-cloud candidate predicate (Phase 80, D-08/D-09).

    A file is an awaiting-cloud candidate iff it carries a ``cloud_job(status='awaiting')`` sidecar
    row AND is NOT analyze-in-flight AND has NOT domain-completed its analyze:

        ``and_(CloudJob.status == 'awaiting', ~inflight_clause(ANALYZE), ~domain_completed_clause(ANALYZE))``

    -- the same three conjuncts, in the same order, as the two inline spellings this builder REPLACES
    (``get_awaiting_cloud_count`` + ``get_cloud_staging_candidates`` in ``services/pipeline.py``), so the
    card and the drain derive from ONE source and can NEVER disagree (D-08). (Plan 80-04 had added a third
    consumer, ``recover_orphaned_work``'s ``_get_awaiting_cloud_ids``; 83-06 reversed D-09 and made the
    drain the single owner of held files, so recovery now EXCLUDES awaiting-cloud files via a plain
    ``cloud_job.status == 'awaiting'`` set rather than reusing this candidacy clause.)

    Composed ENTIRELY from the LOCKED :func:`inflight_clause` / :func:`domain_completed_clause`
    builders verbatim (no re-spelled predicate) so the DERIV-04 equivalence guarantee holds. A file
    mid-local-analysis (which still carries an inert ``awaiting`` row until the D-14 reap seam) is
    correctly excluded by ``~inflight_clause`` and never routed to a compute agent (D-08).

    Like :func:`dedup_resolved_clause`, this takes NO ``stage`` argument and is deliberately kept OUT
    of the ``Stage`` dispatch ladder (:func:`stage_status_case` et al.), so the equivalence test that
    raises on unknown stages does not pick it up (D-13). It needs only the AWAITING status literal (no
    ``backends.toml`` config), so it does not touch 83 D-12's pushing/pushed rejection (D-09).

    Callers MUST provide the ``CloudJob`` ⋈ ``FileRecord`` join (INNER, on
    ``CloudJob.file_id == FileRecord.id``) so the correlated ``~exists(... == FileRecord.id)`` inside
    the composed builders resolves.
    """
    return and_(
        CloudJob.status == CloudJobStatus.AWAITING.value,
        ~inflight_clause(Stage.ANALYZE),
        ~domain_completed_clause(Stage.ANALYZE),
    )


# D-01a. ``saq_jobs`` is SAQ-owned and deliberately NOT an ORM model: a bare ``table()`` clause keeps
# it out of ``Base.metadata`` (so Alembic never emits it -- the Phase-77 banner) while still letting the
# probes below be built from real column objects rather than interpolated SQL. Mirrors the read-only
# discipline of ``_SAQ_DETAIL_SQL`` / ``pipeline._LIVE_KEYS_SQL``.
_saq_jobs = sa_table("saq_jobs", sa_column("key"), sa_column("status"))

# The LIVE broker statuses -- spelled identically to ``pipeline._LIVE_KEYS_SQL`` and
# ``scheduling_ledger._GUARDED_CLEAR_SQL`` so "is this key still live?" has ONE answer everywhere. A
# parked/paused job keeps ``status='queued'`` and so correctly stays LIVE (it is queued, not lost).
_LIVE_SAQ_STATUSES: tuple[str, ...] = ("queued", "active")

# D-01a: the ``cloud_job`` statuses that mean "this file is busy on a compute lane". SAQ cannot see
# compute dispatch at all (there is no controller-side ``saq_jobs`` row for it), so without this
# disjunct every dispatched file would read ORPHANED. This is ``backends.IN_FLIGHT`` plus AWAITING --
# a held file is owned by the ``stage_cloud_window`` drain, exactly the fourth exclusion
# ``_compute_stage_orphan_counts`` applies (phaze-w0yr). Spelled from ``CloudJobStatus`` locally rather
# than imported from ``services.backends``, which imports ``inflight_clause`` FROM this module (cycle);
# ``test_stage_status_equivalence`` pins it against ``backends.IN_FLIGHT`` so it cannot drift.
_CLOUD_BUSY_STATUSES: tuple[str, ...] = (
    CloudJobStatus.UPLOADING.value,
    CloudJobStatus.UPLOADED.value,
    CloudJobStatus.SUBMITTED.value,
    CloudJobStatus.RUNNING.value,
    CloudJobStatus.AWAITING.value,
)


def live_job_clause(stage: Stage) -> ColumnElement[bool]:
    """Return "a LIVE ``saq_jobs`` row exists for this file's ``stage`` key" (D-01a).

    The broker-side corroboration D-01 keeps OUT of :func:`inflight_clause`. Keyed on the SAME
    deterministic ``"<function>:<file_id>"`` string :func:`inflight_clause` builds, from the SAME
    :data:`STAGE_TO_FUNCTION` lookup (never a re-spelled prefix), so the two predicates can only ever
    agree about which key they are talking about. A stage with no per-file ledger key returns
    ``false()``, matching :func:`inflight_clause`.

    READ-ONLY, and deliberately kept OUT of the ``Stage`` dispatch ladder -- it must never reach
    :func:`stage_status_case` or :func:`eligible_clause` (D-01a: the rejected alternative). Callers are
    reporting readers that already own a SAVEPOINT and a zero degrade.
    """
    func_name = STAGE_TO_FUNCTION.get(stage.value)
    if func_name is None:
        return false()
    return live_job_for_function(func_name)


def live_job_for_function(func_name: str) -> ColumnElement[bool]:
    """Return "a LIVE ``saq_jobs`` row exists on ``<func_name>:<file_id>``" -- :func:`live_job_clause` by FUNCTION.

    Same relationship to :func:`live_job_clause` that :func:`inflight_for_function` has to
    :func:`inflight_clause`, and built from the SAME :func:`ledger_key_for_function` spelling, so the
    ledger probe and the broker probe can only ever be talking about the same key. READ-ONLY, and out
    of the ``Stage`` dispatch ladder for the same reason its stage-keyed sibling is.
    """
    return exists(
        select(_saq_jobs.c.key).where(
            _saq_jobs.c.key == ledger_key_for_function(func_name),
            _saq_jobs.c.status.in_(_LIVE_SAQ_STATUSES),
        )
    )


def cloud_busy_clause() -> ColumnElement[bool]:
    """Return "this file is busy on a compute lane" -- a ``cloud_job`` in :data:`_CLOUD_BUSY_STATUSES` (D-01a).

    A FILE-LEVEL predicate (no ``stage`` argument), correlated to :class:`~phaze.models.file.FileRecord`
    like :func:`dedup_resolved_clause`, and kept out of the ``Stage`` dispatch ladder for the same
    reason. It exists so :func:`orphaned_clause` never mistakes compute dispatch -- which is invisible to
    ``saq_jobs`` -- for lost work.
    """
    return exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status.in_(_CLOUD_BUSY_STATUSES)))


def running_clause(stage: Stage) -> ColumnElement[bool]:
    """Return "``stage`` is actually moving somewhere for this file" (D-01a) -- the honest in-flight test.

    ``live_job_clause(stage) OR cloud_busy_clause()``: a live broker row, or a busy compute lane SAQ
    cannot see. The cloud disjunct is applied ONLY to the cloud-owned stage (analyze / ``process_file``),
    mirroring the ``_CLOUD_OWNED_FUNCTIONS`` scoping in ``_compute_stage_orphan_counts`` -- applying it to
    every stage would let a cloud-busy file mask a genuinely lost ``extract_file_metadata`` row that
    recovery DOES re-drive (phaze-fc2l).
    """
    if stage is Stage.ANALYZE:
        return or_(live_job_clause(stage), cloud_busy_clause())
    return live_job_clause(stage)


def _metadata_orphaned_retry_clause() -> ColumnElement[bool]:
    """True iff METADATA's ledger row is a lost OPERATOR RETRY of a terminally-failed file (D-10, phaze-hr627).

    The SQL twin of the ONE call-site refinement ``tasks.reenqueue.is_domain_completed`` applies on top
    of ``domain_completed_clause(METADATA)`` and that ``domain_completed_clause`` deliberately does NOT
    fold in itself (D-11 REJECTED OPTION, ``tests/integration/test_stage_status_equivalence.py`` --
    ``domain_completed_clause`` must stay the raw, call-site-independent, inflight-orthogonal predicate
    so its OTHER consumers -- ``eligible_clause``, ``cloud_lane_completed_clause`` -- never silently
    change scope). ``retry_metadata_failed`` LEAVES ``FileMetadata.failed_at`` set (81 D-11: clearing it
    would make a zero-metadata file read DONE forever) and then re-enqueues, so a fresh
    ``extract_file_metadata:<file_id>`` ledger row with ``enqueued_at > failed_at`` is that retry, not a
    stale terminal clear -- it is genuinely pending work, not domain-complete.

    Correlated to :class:`~phaze.models.file.FileRecord` like its siblings: pins the ledger row via the
    SAME deterministic-key spelling :func:`ledger_key_for_function` builds elsewhere, and the metadata
    row via ``FileMetadata.file_id``, so it can only ever match THIS file's own rows.
    """
    ledger_key = ledger_key_for_function(STAGE_TO_FUNCTION[Stage.METADATA.value])
    return exists(
        select(SchedulingLedger.key).where(
            SchedulingLedger.key == ledger_key,
            FileMetadata.file_id == FileRecord.id,
            FileMetadata.failed_at.isnot(None),
            SchedulingLedger.enqueued_at > FileMetadata.failed_at,
        )
    )


def _recovery_domain_completed_clause(stage: Stage) -> ColumnElement[bool]:
    """``domain_completed_clause(stage)``, refined by recovery's D-10 gate for METADATA (phaze-hr627).

    The shared, call-site-independent :func:`domain_completed_clause` stays untouched (D-11). This
    wrapper is for the two RECOVERY-ADJACENT consumers only -- :func:`orphaned_clause` and
    :func:`resolved_ledger_clause` -- which must agree with ``recover_orphaned_work``'s own
    ``is_domain_completed`` about which METADATA rows are genuinely finished vs. a lost operator
    retry, exactly mirroring how ``is_domain_completed`` applies the SAME gate at ITS call site rather
    than inside the shared predicate. A no-op for every stage but METADATA (analyze's retry clears
    ``failed_at`` first, CR-01, so it has no such ambiguous cell -- D-10).
    """
    complete = domain_completed_clause(stage)
    if stage is not Stage.METADATA:
        return complete
    return and_(complete, not_(_metadata_orphaned_retry_clause()))


def orphaned_clause(stage: Stage) -> ColumnElement[bool]:
    """Return the ORPHANED predicate (D-01a): previously scheduled, running nowhere, not domain-complete.

    ``inflight_clause ∧ ¬running_clause ∧ ¬domain_completed_clause`` (METADATA: D-10-refined via
    :func:`_recovery_domain_completed_clause`, phaze-hr627) -- the per-file twin of the ledger-set
    arithmetic ``_compute_stage_orphan_counts`` performs in Python, and therefore of exactly the set
    :func:`~phaze.tasks.reenqueue.recover_orphaned_work` would re-enqueue for the stage. Composed
    ENTIRELY from LOCKED builders plus the one new broker probe, so it can never re-derive a predicate
    the equivalence test owns.

    This is NOT a sixth :class:`~phaze.enums.stage.Status`. It is a REPORTING refinement carved out of
    the ``in_flight`` bucket: the derived per-file status, eligibility and recovery are all unchanged, so
    an orphaned file stays ineligible for auto-enqueue and stays in recovery's work set. Defined only for
    the two enrich stages (``domain_completed_clause`` raises otherwise), and kept OUT of the ``Stage``
    dispatch ladder (D-13).
    """
    return and_(inflight_clause(stage), not_(running_clause(stage)), not_(_recovery_domain_completed_clause(stage)))


def resolved_ledger_clause(stage: Stage) -> ColumnElement[bool]:
    """Return the RESOLVED-but-uncleared ledger predicate (D-01a) -- the reaper's target set.

    ``inflight_clause ∧ ¬running_clause ∧ domain_completed_clause`` (METADATA: D-10-refined via
    :func:`_recovery_domain_completed_clause`, phaze-hr627): the stage reached a terminal domain state,
    nothing is running it, yet the ledger row is still standing -- i.e. its terminal clear was lost (a
    reaped ``aborting`` row, a crashed callback, ``clear_ledger_entry``'s documented residual window, a
    broker truncate). Pure stale state: the row is invisible to recovery (which excludes
    domain-completed rows -- ``is_domain_completed`` applies the SAME D-10 gate this clause now does)
    while still reporting ``in_flight`` and still blocking the file from :func:`eligible_clause` and
    :func:`awaiting_candidate_clause` forever.

    Before phaze-hr627 this used the RAW ``domain_completed_clause(METADATA)`` and so also matched a
    lost OPERATOR RETRY (``enqueued_at > failed_at``, 81 D-11) that ``recover_orphaned_work`` still owed
    a replay -- the reaper deleted the retry's ledger row out from under it, silently discarding the
    retry with no attempt ever run. The D-10 refinement closes that: such a row now reads
    domain-INCOMPLETE here too, so the reaper leaves it for recovery.

    The exact complement of :func:`orphaned_clause` on the domain-completion axis -- the two share the
    ``¬running_clause`` core, so a row can never be BOTH and can never be NEITHER while unresolved.
    Consumed by :mod:`phaze.tasks.ledger_reaper`. Kept OUT of the ``Stage`` dispatch ladder (D-13).
    """
    return and_(inflight_clause(stage), not_(running_clause(stage)), _recovery_domain_completed_clause(stage))


# phaze-k95r7. The file-keyed AGENT producers that serve the cloud analyze lane. They own a per-file
# ``scheduling_ledger`` key but are NOT ``Stage`` members, which is exactly why they fell through every
# stage-keyed predicate: ``s3_upload`` had no completion exclusion of ANY kind, so a row for a file
# whose analysis had long since landed stayed a recovery candidate forever (17 such rows, dated
# 2026-07-07/14, were still being re-driven on 2026-08-08). ``submit_cloud_job`` is deliberately ABSENT:
# it is a CONTROLLER task (``enqueue_router.CONTROLLER_TASKS``) whose ledger clear runs in the same
# ``after_process`` the broker guarantees, so the live-key filter alone is the right exclusion for it.
CLOUD_LANE_FUNCTIONS: tuple[str, ...] = ("push_file", "s3_upload")
"""Cloud-lane keyed functions carrying a per-file ledger key -- ordered, so counters render stably."""


def cloud_lane_completed_clause() -> ColumnElement[bool]:
    """Return "the cloud staging/push lane has nothing left to do for this file" (phaze-k95r7).

    ``cloud_job.status = 'succeeded' OR domain_completed_clause(ANALYZE)`` -- a FILE-LEVEL predicate
    (no ``stage`` argument), correlated to :class:`~phaze.models.file.FileRecord` like
    :func:`cloud_busy_clause`, and kept out of the ``Stage`` dispatch ladder for the same reason
    (D-13). Both disjuncts say the same thing from opposite ends of the lane:

    - SUCCEEDED covers the landed-but-not-yet-analyzed window -- the upload/push demonstrably ran;
    - ``domain_completed(analyze)`` covers the onward advance, INCLUDING a terminally FAILED analyze
      (``FAILURE_IS_TERMINAL[analyze]``): re-staging bytes for a file the domain has given up on is
      exactly the waste this predicate exists to stop.

    This is the SINGLE definition of cloud-lane completion. ``reenqueue._build_done_sets`` derives the
    ``cloud_lane_done`` set from it (so recovery EXCLUDES such a row) and
    :func:`resolved_cloud_ledger_clause` composes it (so the reaper CLEARS the row recovery is now
    ignoring). Deriving those two independently is how a row ends up excluded-but-immortal.
    """
    succeeded = exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status == CloudJobStatus.SUCCEEDED.value))
    return or_(succeeded, domain_completed_clause(Stage.ANALYZE))


def resolved_cloud_ledger_clause(func_name: str) -> ColumnElement[bool]:
    """Return the RESOLVED-but-uncleared predicate for a CLOUD-LANE ledger row (phaze-k95r7).

    The function-keyed twin of :func:`resolved_ledger_clause`, with the same three conjuncts in the
    same order: ``inflight_for_function ∧ ¬running ∧ completed``. ``running`` is
    ``live_job_for_function OR cloud_busy_clause()`` -- BOTH substrates, mirroring
    :func:`running_clause`'s analyze branch, because the cloud lane's work is invisible to ``saq_jobs``
    while a ``cloud_job`` is UPLOADING/SUBMITTED/RUNNING/AWAITING. On ``saq_jobs`` alone every file
    mid-upload would look reapable.

    Consumed by :mod:`phaze.tasks.ledger_reaper`. ``func_name`` is a member of
    :data:`CLOUD_LANE_FUNCTIONS`; it is interpolated ONLY into the bound-parameter side of the key
    concat (:func:`ledger_key_for_function`), never into raw SQL (T-87-05).
    """
    return and_(
        inflight_for_function(func_name),
        not_(or_(live_job_for_function(func_name), cloud_busy_clause())),
        cloud_lane_completed_clause(),
    )


def stage_status_case(stage: Stage) -> ColumnElement[str]:
    """Compose the per-stage status CASE ladder (``in_flight ≻ done ≻ skipped ≻ failed ≻ not_started``).

    The SQL twin of :func:`phaze.enums.stage.resolve_status`, locked equal by the DERIV-04
    equivalence test. Drop it into a ``SELECT`` correlated to :class:`~phaze.models.file.FileRecord`.

    The two ENRICH stages (metadata/analyze) get a 5-way ladder with the
    ``skipped_clause`` branch inserted ``done ≻ skipped ≻ failed`` (D-08 force-skip marker). The four
    downstream stages have NO force-skip affordance (``skipped_clause`` raises on them, D-10), so they
    keep the original 4-way ladder. ``skipped ≻ failed`` is load-bearing: the force-skip writer is
    additive (never clears ``failed_at``), so ``failed_clause`` may still be True -- CASE order makes
    ``skipped`` win (Pitfall 2), matching the Python twin's branch order.

    NOTE on apply eligibility (do NOT wire this here -- additive-only): ``done(apply)`` above means an
    ``execution_log`` completion row exists. Apply *eligibility* (ELIG-02) is a DIFFERENT predicate --
    "an APPROVED proposal exists" -- which later-phase apply pending ``.where()`` builders must
    express as ``exists(select(RenameProposal.id).where(RenameProposal.file_id == FileRecord.id,
    RenameProposal.status == 'approved'))`` (join through ``proposals``; ``execution_log`` has no
    ``file_id``), mirroring the Python ``has_approved_proposal`` apply flag (plan 78-01). It is NOT a
    bare ``done(review)`` (which only means a proposal exists). Eligibility clauses land at cutover.
    """
    branches = [
        (inflight_clause(stage), Status.IN_FLIGHT.value),
        (done_clause(stage), Status.DONE.value),
    ]
    if stage in ELIGIBLE_AFTER_FAILURE:  # enrich stages only -- skipped_clause raises on downstream (D-10)
        branches.append((skipped_clause(stage), Status.SKIPPED.value))
    branches.append((failed_clause(stage), Status.FAILED.value))
    return case(*branches, else_=Status.NOT_STARTED.value)


# ==================================================================================================
# phaze-cvn6.1 -- the DISPLAY order for a stage-status column.
#
# READ THIS BEFORE REORDERING IT. This tuple is NOT the precedence ladder above and the two must
# never be conflated:
#
#   * ``stage_status_case``'s precedence (``in_flight ≻ done ≻ skipped ≻ failed ≻ not_started``)
#     answers "which ONE bucket does this file land in when several clauses are true at once". It is
#     locked against the Python twin by the DERIV-04 equivalence test and is not a display concern.
#   * STAGE_STATUS_DISPLAY_ORDER answers "when the operator sorts a stage COLUMN, which bucket comes
#     first". Changing it changes only what an operator sees; changing the ladder above changes what
#     a file IS.
#
# The order below is a PROGRESS ladder with the two off-ladder outcomes trailing it:
#
#   0 done         -- the stage finished. The most-advanced state, so ascending leads with it.
#   1 in_flight    -- the stage is running right now: advanced, but not settled.
#   2 not_started  -- the stage has not begun. Least-advanced point on the ladder.
#   3 failed       -- attempted and errored. NOT a point on the progress ladder at all, so it sits
#                     after the ladder rather than inside it; before ``skipped`` because a failure
#                     still wants an operator (retry) and a skip never will.
#   4 skipped      -- deliberately bypassed (D-08 force-skip). The one terminal state that is
#                     nobody's outstanding work, so it sorts last and stays out of the way.
#
# Descending therefore surfaces the exceptional rows -- skipped, then failed -- at the top, which is
# the second click an operator makes when triaging a stage. The first click (ascending) answers the
# commoner question, "how far has this stage got across the corpus".
#
# Note this is deliberately NOT alphabetical: alphabetical would read
# ``done, failed, in_flight, not_started, skipped`` and interleave a terminal error between two
# healthy states, so "sort by Analyze" would tell an operator nothing about the stage.
#
# The stage/status FILTER lens (``?stage=…&bucket=failed``, ``_status_filter_bar.html``) remains the
# direct way to ask for failures only; this ordering is for scanning, not for filtering.
# ==================================================================================================
STAGE_STATUS_DISPLAY_ORDER: tuple[Status, ...] = (
    Status.DONE,
    Status.IN_FLIGHT,
    Status.NOT_STARTED,
    Status.FAILED,
    Status.SKIPPED,
)
"""The operator-facing rank of the five derived buckets, best-progressed first (see the block above)."""


def stage_status_sort_case(stage: Stage) -> ColumnElement[int]:
    """Compose the ORDER BY rank for ``stage``'s status column: the derived bucket mapped to its display rank.

    The sortable-column expression behind the five stage headers on the Files matrix (phaze-cvn6.1).
    It wraps :func:`stage_status_case` VERBATIM rather than re-deriving the buckets (D-04: never a
    fresh ``CASE`` ladder), so the value this orders by is by construction the SAME value the
    ``_stage_pill`` cell renders -- an operator can never see one order and read another status.

    The outer ``CASE`` is a pure value mapping over the five status literals, so it introduces no new
    join, no new predicate, and nothing an untrusted string can reach: the wire only ever selects
    WHICH stage, via the router's ``SortContract`` whitelist (``column_sort`` rule 2).

    COST, stated plainly because it is the reason phaze-a6hm.3 left these columns out: ordering by a
    correlated ``CASE`` makes Postgres evaluate it for every candidate row, not just the page, so a
    stage sort is O(corpus) where a sort on ``current_path`` is index-ordered. That is accepted here
    on the same ground the ALREADY-SHIPPED filter rests on -- ``_files_page_stmt`` has evaluated
    ``stage_status_case(stage) == bucket`` corpus-wide since Phase 87 -- and it stays off the hot
    path because ``files_table_view.html`` carries NO self-poll (T-87-11 is about a poll scanning the
    corpus; this is a click).

    Args:
        stage: The stage whose derived status column is being ordered.

    Returns:
        An integer-valued ``ColumnElement`` ranking each row per :data:`STAGE_STATUS_DISPLAY_ORDER`.
    """
    ranks = {status.value: rank for rank, status in enumerate(STAGE_STATUS_DISPLAY_ORDER)}
    # `else_` is unreachable while STAGE_STATUS_DISPLAY_ORDER covers every Status member (a guard test
    # asserts it does); it exists so an added status sorts to the END rather than NULL-ordering.
    return case(ranks, value=stage_status_case(stage), else_=len(STAGE_STATUS_DISPLAY_ORDER))


# Corroborating detail ONLY (D-02). Static SQL -- the sole literals are the status allowlist
# ('queued','active'); no interpolated operand (T-45 read-only-probe discipline). `saq_jobs` has no
# `function` column and this read never flips `in_flight` (the ledger owns the boolean, D-01).
_SAQ_DETAIL_SQL = text("SELECT status, COUNT(*) AS n FROM saq_jobs WHERE status IN ('queued', 'active') GROUP BY status")


async def saq_detail(session: AsyncSession) -> dict[str, int]:
    """Return the corroborating ``{queued, active}`` broker counts -- SAVEPOINT-isolated, degrade-safe.

    Copies the ``pipeline.py:488-499`` (``get_stage_busy_counts``) idiom VERBATIM: the read runs
    inside a ``begin_nested()`` SAVEPOINT so ANY error (a missing/renamed ``saq_jobs`` table, a DB
    hiccup) rolls back the nested scope ALONE -- recovering the aborted transaction WITHOUT expiring
    the caller's already-loaded ORM objects and WITHOUT poisoning later queries -- then logs a
    warning and returns the zeroed safe default. It NEVER raises into a hot poll, and it NEVER flips
    ``in_flight`` (that boolean comes from the durable ledger; INFLIGHT-02 / T-78-04).
    """
    out: dict[str, int] = {"queued": 0, "active": 0}
    try:
        async with session.begin_nested():
            rows = (await session.execute(_SAQ_DETAIL_SQL)).all()
    except Exception:
        logger.warning("saq_detail_degraded", exc_info=True)
        return out
    for status_label, n in rows:
        if status_label in out:
            out[status_label] = int(n)
    return out
