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
- ``done(fingerprint)`` is a 1:N aggregation -- one ``success``/``completed`` engine row wins over a
  sibling ``failed`` engine (DERIV-05). Spelled ``status IN ('success','completed')`` which Postgres
  renders ``= ANY (ARRAY[...])``, matching the Phase-59 WR-02 spelling and the ``ix_fprint_success``
  partial index.
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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, String, and_, case, cast, exists, false, func, not_, or_, select, text
import structlog

from phaze.enums.stage import ELIGIBLE_AFTER_FAILURE, FAILURE_IS_TERMINAL, Stage, Status
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.dedup_resolution import DedupResolution
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
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


# DERIV-05: a fingerprint engine row in either of these states counts the stage done. Mirrors the
# Phase-59 WR-02 spelling and the ``ix_fprint_success`` partial index (renders ``= ANY (ARRAY[...])``).
_DONE_FP: tuple[str, ...] = ("success", "completed")


def dedup_resolved_clause() -> ColumnElement[bool]:
    """Return the correlated ``dedup-resolved`` predicate for a file (a ``ColumnElement[bool]``).

    This is a **FILE-LEVEL** predicate, NOT a per-:class:`~phaze.enums.stage.Stage` one -- dedup
    resolution is a corpus-hygiene fact about a file, not one of the pipeline stages. It takes NO
    ``stage`` argument and correlates to :class:`~phaze.models.file.FileRecord` in the enclosing query
    via a correlated ``exists(...)`` (never an outer-join-null / negated-membership anti-pattern),
    identical in body to the Phase-90-retired ``shadow_compare._dedup_exists`` migration-verification
    helper (the module was removed with ``files.state`` in Phase 90's writer-removal cleanup). Marker-row
    existence means resolved; ``~dedup_resolved_clause()`` therefore means "not resolved" (the shape the Wave-2
    dedup readers and ``get_fingerprint_progress``'s denominator consume).

    It is deliberately kept OUT of the ``Stage`` dispatch ladders (:func:`done_clause` /
    :func:`failed_clause` / :func:`inflight_clause` / :func:`domain_completed_clause` /
    :func:`stage_status_case`) -- those all raise ``ValueError`` on an unknown stage and are
    drift-locked to the Python resolver by ``tests/integration/test_stage_status_equivalence.py`` (D-13).
    A non-``Stage`` clause must not touch that test.

    Both consumers import this predicate from here (the single-source predicate module, Phase 78):
    ``services/dedup.py`` at module level, ``services/fingerprint.py`` **inside** its function (the
    agent-worker import boundary, D-00e).
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
    if stage is Stage.FINGERPRINT:
        # DERIV-05: any engine success wins. `.in_((...))` renders `= ANY (ARRAY[...])` (Phase-59 WR-02).
        return exists(select(FingerprintResult.id).where(FingerprintResult.file_id == FileRecord.id, FingerprintResult.status.in_(_DONE_FP)))
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

    Defined ONLY for the three enrich stages (the keys of :data:`~phaze.enums.stage.ELIGIBLE_AFTER_FAILURE`),
    mirroring :func:`eligible_clause`'s enrich-only guard: force-skip is an enrich-only affordance (D-10),
    so reaching for it on a downstream stage raises ``ValueError`` (T-87-06). The ``stage_skip`` table's
    own ``CHECK(stage IN ('metadata','analyze','fingerprint'))`` is the storage-side twin of this guard.
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
    if stage is Stage.FINGERPRINT:
        # ELIG-04: failed iff NO engine succeeded AND at least one engine failed (~exists anti-join).
        return and_(
            ~exists(select(FingerprintResult.id).where(FingerprintResult.file_id == FileRecord.id, FingerprintResult.status.in_(_DONE_FP))),
            exists(select(FingerprintResult.id).where(FingerprintResult.file_id == FileRecord.id, FingerprintResult.status == "failed")),
        )
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


def inflight_clause(stage: Stage) -> ColumnElement[bool]:
    """Return ``in_flight`` for ``stage`` -- authoritative from ``scheduling_ledger`` (D-01).

    ``in_flight`` iff a ledger row exists on the deterministic ``"<function>:<file_id>"`` key. The
    function name is looked up in :data:`STAGE_TO_FUNCTION` (imported, never re-spelled -- a
    re-spelled key silently mismatches the real ledger PK). ``saq_jobs`` is NEVER consulted for the
    boolean (D-01/D-02).

    Only the three file-keyed enrich stages have a per-file ledger key. ``propose`` is keyed on a
    batch set-hash (``sha256(sorted file_ids)``), NOT per-file, so there is no per-file
    ``in_flight(propose)`` -- scoped OUT of Phase 78 (RESEARCH Pitfall 5 / OQ1). The downstream
    presence stages likewise have no file-keyed enqueue, so they return a constant ``false()``,
    matching the Python twin (which defaults ``inflight`` to ``False`` for those stages).
    """
    func_name = STAGE_TO_FUNCTION.get(stage.value)
    if func_name is None:
        return false()
    return exists(select(SchedulingLedger.key).where(SchedulingLedger.key == func.concat(func_name + ":", cast(FileRecord.id, String))))


def domain_completed_clause(stage: Stage) -> ColumnElement[bool]:
    """SQL twin of :func:`phaze.enums.stage.domain_completed` -- has ``stage`` reached a DOMAIN-COMPLETE state?

    ``DONE`` and ``SKIPPED`` (D-08 force-skip marker) are always domain-complete; a ``FAILED`` stage
    counts as complete ONLY when its failure is
    terminal (:data:`~phaze.enums.stage.FAILURE_IS_TERMINAL`). Reuses the LOCKED ``done_clause`` /
    ``skipped_clause`` / ``failed_clause`` predicates verbatim (never a fresh CASE) so this stays
    byte-equivalent to its ``ColumnElement`` siblings and the Python twin -- drift-locked by the
    equivalence test (``tests/integration/test_stage_status_equivalence.py``), D-17.

    When ``FAILURE_IS_TERMINAL[stage]`` is ``False`` (fingerprint) the failure disjunct is dropped and
    the clause collapses to ``or_(done_clause, skipped_clause)`` -- a FAILED fingerprint is NOT
    domain-complete (it auto-retries, ELIG-04), but a force-skipped one still is.

    Defined ONLY for the three enrich stages (the keys of :data:`~phaze.enums.stage.FAILURE_IS_TERMINAL`),
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
    """SQL twin of :func:`phaze.enums.stage.eligible` for the three ENRICH stages only (READ-01).

    Mirrors the Python truth (``enums/stage.py``, the enrich branch)::

        status not in (DONE, IN_FLIGHT, SKIPPED) and (status != FAILED or ELIGIBLE_AFTER_FAILURE[stage])

    Because :func:`stage_status_case` applies the precedence ladder ``in_flight ≻ done ≻ skipped ≻ failed
    ≻ not_started`` (the SAQ ledger wins), a file is none of ``IN_FLIGHT`` / ``DONE`` / ``SKIPPED`` iff
    ``~inflight_clause(stage)`` and ``~done_clause(stage)`` and ``~skipped_clause(stage)`` all hold -- so
    ``status not in (DONE, IN_FLIGHT, SKIPPED)`` maps exactly to ``~inflight ∧ ~done ∧ ~skipped`` (D-08: a
    force-skipped stage leaves the pending set). The FAILED carve-out is TABLE-DRIVEN off
    :data:`~phaze.enums.stage.ELIGIBLE_AFTER_FAILURE` (NEVER an inline per-stage identity check):

    - metadata / fingerprint (``ELIGIBLE_AFTER_FAILURE True`` -- ELIG-04 auto-retry): ``~inflight ∧ ~done``
      leaves a FAILED (and a NOT_STARTED) row eligible.
    - analyze (``ELIGIBLE_AFTER_FAILURE False`` -- ELIG-03 terminal, manual retry only): append
      ``~failed_clause(stage)`` so ONLY a NOT_STARTED analyze is eligible. This is the load-bearing
      conjunct behind the 2026-06-18 ~44.5K over-enqueue guard; dropping it re-admits failed analyze
      rows to the pending set (the ``(ANALYZE, seed_analysis_failed, False)`` drift cell + mutation
      check in the equivalence test guard it).

    ``has_approved_proposal`` (an APPLY-only flag in the Python twin) is irrelevant here, so the
    signature stays a single ``stage`` param.

    Defined ONLY for the three enrich stages (the keys of :data:`~phaze.enums.stage.ELIGIBLE_AFTER_FAILURE`),
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


def stage_status_case(stage: Stage) -> ColumnElement[str]:
    """Compose the per-stage status CASE ladder (``in_flight ≻ done ≻ skipped ≻ failed ≻ not_started``).

    The SQL twin of :func:`phaze.enums.stage.resolve_status`, locked equal by the DERIV-04
    equivalence test. Drop it into a ``SELECT`` correlated to :class:`~phaze.models.file.FileRecord`.

    The three ENRICH stages (metadata/analyze/fingerprint) get a 5-way ladder with the
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
