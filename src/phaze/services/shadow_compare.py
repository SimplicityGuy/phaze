"""The ONE shared state↔derived shadow-compare assertion core (Phase 79, D-01).

This module is THE standing gate phases 80-90 keep green and the hard precondition for the
destructive ``033`` migration (Phase 90). It defines an :data:`INVARIANTS` registry -- one
*implication* per :class:`~phaze.models.file.FileState` value per design §6.1 (D-04) -- and an async
:func:`run_shadow_compare` that, for each invariant, runs a corpus-wide anti-join
(``state = X AND NOT <derived-condition>``) and returns a :class:`Report` of per-invariant divergent
counts + capped ``file_id`` samples (D-05).

**Implication, not equality.** Every invariant asserts ``state = X ⇒ <derived-condition>`` -- never
the converse. A file whose derivation is *more informative* than its scalar state (e.g. a row at
``metadata_extracted`` that ALSO carries a completed analysis) is consistent and never flags: the
derived side is a strictly richer source than the coarse ``files.state`` cursor.

**D-03 reuse + accepted circularity.** The derived side REUSES Phase-78's
:func:`~phaze.services.stage_status.done_clause` / :func:`~phaze.services.stage_status.failed_clause`
(the gate doubles as a guard on the derivation layer) and NEVER
:func:`~phaze.services.stage_status.stage_status_case` (its ``in_flight ≻ done`` ladder would
false-flag a done file with a queued re-analysis). The eight predicates Phase 78 does not cover use
raw correlated ``exists(select(Model.id).where(Model.file_id == FileRecord.id, ...))`` in the
``stage_status`` house style -- never ``LEFT JOIN ... IS NULL`` / ``not_in(subquery)`` / ``text()``
interpolation (bandit B608 + SQLi hygiene, T-79-01).

The accepted D-03 circularity: the invariants for ``ANALYSIS_FAILED``, ``AWAITING_CLOUD``,
``PUSHING``, ``PUSHED`` and ``DUPLICATE_RESOLVED`` assert rows that migration ``032`` created *from*
``files.state`` -- so they hold near-tautologically on a fresh backfill. The genuine drift-catchers
are the states that derive from *pre-existing output rows*: ``METADATA_EXTRACTED``, ``ANALYZED``,
``PROPOSAL_GENERATED`` and the apply-outcome proposal-status states.

**Soft allowlist (D-06).** ``FINGERPRINTED`` and ``LOCAL_ANALYZING`` are ``soft=True``: they are
COUNTED and printed as "expected divergence (§6.1)" but NEVER contribute to
:attr:`Report.hard_fail_total`. ``FINGERPRINTED`` need not imply fingerprint success (its sole writer
is ``retry_analysis_failed``); ``LOCAL_ANALYZING`` has no durable stored marker (it lives only in the
transient scheduling ledger). The code-commented allowlist is exactly ``{fingerprinted,
local_analyzing}`` so it cannot silently grow.

**Read-only.** :func:`run_shadow_compare` issues only ``SELECT``s -- no ``INSERT``/``UPDATE``/
``DELETE``, no ``saq_jobs`` access. The rendered sample emits ``file_id`` UUIDs ONLY (capped at
``sample_cap``), NEVER ``original_path`` / ``original_filename`` or other PII (T-79-02).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, and_, exists, false, func, select

from phaze.enums.stage import Stage
from phaze.models.cloud_job import CloudJob
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import RenameProposal
from phaze.services.stage_status import done_clause, failed_clause


if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession


# --------------------------------------------------------------------------------------------------
# Derived-side predicate factories for the 8 states with NO Phase-78 builder (gap tables). Each is a
# zero-arg factory returning a correlated ``exists(...)`` in the ``stage_status`` house style -- never
# an outer-join-null / negated-membership / ``text()`` anti-pattern (T-79-01).
# --------------------------------------------------------------------------------------------------
def _cloud_job_exists() -> ColumnElement[bool]:
    """Any ``cloud_job`` row for the file, regardless of status.

    RESEARCH A3 / OQ1: design §6.2 reads "a cloud_job row exists with the corresponding status", but a
    LIVE-cloud-path file (row created by the real push, not the ``032`` backfill) may legitimately have
    advanced past ``uploading``/``uploaded`` to ``submitted``/``running``/``succeeded``. The safe
    reading for ``PUSHING`` / ``PUSHED`` is therefore mere row EXISTENCE. The exact-status check is
    reserved for ``AWAITING_CLOUD`` (``status='awaiting'`` is unambiguous -- see :func:`_cloud_awaiting`).
    """
    return exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id))


def _cloud_awaiting() -> ColumnElement[bool]:
    """A ``cloud_job`` row at the exact ``awaiting`` status (RESEARCH A3 -- unambiguous for AWAITING_CLOUD)."""
    return exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status == "awaiting"))


def _dedup_exists() -> ColumnElement[bool]:
    """A ``dedup_resolution`` marker row for the file (existence = resolved)."""
    return exists(select(DedupResolution.id).where(DedupResolution.file_id == FileRecord.id))


def _proposal_status(status: str) -> Callable[[], ColumnElement[bool]]:
    """Return a factory asserting a ``proposals`` row at the given ``status`` (apply-outcome joint-write).

    RESEARCH A1: the apply-outcome states derive from ``proposals.status`` (the authoritative
    joint-write in ``agent_proposals.py``), NEVER ``execution_log``. ``status`` is a fixed enum value
    bound as a parameter -- no interpolation.
    """
    return lambda: exists(select(RenameProposal.id).where(RenameProposal.file_id == FileRecord.id, RenameProposal.status == status))


@dataclass(frozen=True)
class Invariant:
    """One ``state ⇒ derived-condition`` implication in the registry.

    ``predicate`` is a zero-arg factory returning the correlated derived-side ``ColumnElement[bool]``
    (a factory, not a bound element, so each ``run_shadow_compare`` call builds a fresh clause).
    """

    name: str
    state: str  # the FileState `.value`
    predicate: Callable[[], ColumnElement[bool]]
    soft: bool
    doc_ref: str


# --------------------------------------------------------------------------------------------------
# INVARIANTS -- one entry per FileState value per RESEARCH's 17-value table (§6.1), EXCEPT DISCOVERED.
#
# DISCOVERED is intentionally VACUOUS and is NOT an entry: its implication is "none" (the baseline
# state; the derivation is strictly more informative), and Pitfall 2 warns that a rescan-wiped file
# can legitimately still carry output rows -- so any DISCOVERED implication would false-flag. It is
# documented here as an omission rather than encoded.
# --------------------------------------------------------------------------------------------------
INVARIANTS: list[Invariant] = [
    # --- Phase-78 clause reuse (D-03): the genuine drift-catchers derive from pre-existing output rows.
    Invariant("metadata_extracted", FileState.METADATA_EXTRACTED.value, lambda: done_clause(Stage.METADATA), soft=False, doc_ref="§6.1 #2"),
    Invariant("analyzed", FileState.ANALYZED.value, lambda: done_clause(Stage.ANALYZE), soft=False, doc_ref="§6.1 #4"),
    # analysis_failed is D-03-circular (032 backfilled the analysis row from files.state) but reuses failed_clause.
    Invariant("analysis_failed", FileState.ANALYSIS_FAILED.value, lambda: failed_clause(Stage.ANALYZE), soft=False, doc_ref="§6.1 #5"),
    Invariant("proposal_generated", FileState.PROPOSAL_GENERATED.value, lambda: done_clause(Stage.PROPOSE), soft=False, doc_ref="§6.1 #10"),
    # --- Cloud sidecar gap tables (raw exists); circular on a fresh 032 backfill (accepted, D-03).
    Invariant("awaiting_cloud", FileState.AWAITING_CLOUD.value, _cloud_awaiting, soft=False, doc_ref="§6.1 #6"),
    # PUSHING/PUSHED loosen to mere cloud_job-row existence (RESEARCH A3/OQ1 -- see _cloud_job_exists), not §6.2's exact status.
    Invariant("pushing", FileState.PUSHING.value, _cloud_job_exists, soft=False, doc_ref="§6.1 #7 (loosened §6.2)"),
    Invariant("pushed", FileState.PUSHED.value, _cloud_job_exists, soft=False, doc_ref="§6.1 #8 (loosened §6.2)"),
    Invariant("duplicate_resolved", FileState.DUPLICATE_RESOLVED.value, _dedup_exists, soft=False, doc_ref="§6.1 #15"),
    # --- Proposal-status apply-outcome states (raw exists on proposals.status, NEVER execution_log -- RESEARCH A1).
    Invariant("approved", FileState.APPROVED.value, _proposal_status("approved"), soft=False, doc_ref="§6.1 #11"),
    Invariant("rejected", FileState.REJECTED.value, _proposal_status("rejected"), soft=False, doc_ref="§6.1 #12"),
    Invariant("executed", FileState.EXECUTED.value, _proposal_status("executed"), soft=False, doc_ref="§6.1 #13"),
    # FileState.FAILED has zero writers in src/ (design §4.1) -- authored for D-04 comprehensiveness; simply finds no rows.
    Invariant("failed", FileState.FAILED.value, _proposal_status("failed"), soft=False, doc_ref="§6.1 #14"),
    # MOVED/UNCHANGED are the joint-write apply outcomes: MOVED↔proposals 'executed', UNCHANGED↔proposals 'failed'.
    Invariant("moved", FileState.MOVED.value, _proposal_status("executed"), soft=False, doc_ref="§6.1 #16"),
    Invariant("unchanged", FileState.UNCHANGED.value, _proposal_status("failed"), soft=False, doc_ref="§6.1 #17"),
    # --- SOFT allowlist (D-06) -- counted + printed as expected divergence, NEVER gated. Exactly these two.
    # FINGERPRINTED: sole writer is retry_analysis_failed; the state need not imply fingerprint success (§6.1 #3).
    # LOCAL_ANALYZING: no durable stored marker (lives only in the transient scheduling ledger -- §6.1 #9).
    # The benign `false()` placeholder makes NO derived claim: ~false() is always true, so every row at
    # the state is surfaced as expected divergence. The allowlist must NEVER silently grow past these two.
    Invariant("fingerprinted", FileState.FINGERPRINTED.value, false, soft=True, doc_ref="§6.1 #3 (allowlist)"),
    Invariant("local_analyzing", FileState.LOCAL_ANALYZING.value, false, soft=True, doc_ref="§6.1 #9 (allowlist)"),
]


@dataclass(frozen=True)
class InvariantResult:
    """The per-invariant outcome of one :func:`run_shadow_compare` pass."""

    name: str
    state: str
    soft: bool
    count: int
    sample: list[str]  # divergent file_id UUIDs (strings), capped at sample_cap unless verbose
    doc_ref: str


@dataclass(frozen=True)
class Report:
    """The full shadow-compare result: one :class:`InvariantResult` per invariant."""

    results: list[InvariantResult]

    @property
    def hard_fail_total(self) -> int:
        """Sum of divergent counts for the NON-soft invariants only (the gate value, D-06)."""
        return sum(r.count for r in self.results if not r.soft)

    @property
    def soft_divergence_total(self) -> int:
        """Sum of divergent counts for the soft-allowlist invariants (informational only)."""
        return sum(r.count for r in self.results if r.soft)

    def render(self, *, verbose: bool = False) -> str:
        """Render the D-05 output: one line per invariant + a totals line.

        Soft invariants are labelled "expected divergence (§6.1)". The sample lists ``file_id`` UUIDs
        ONLY (never a path/filename -- T-79-02). ``verbose`` widens each line's sample from the capped
        head to the full divergent set.
        """
        lines: list[str] = []
        for r in self.results:
            label = "expected divergence (§6.1)" if r.soft else "HARD"
            head = ", ".join(r.sample) if r.sample else "-"
            suffix = "" if (verbose or r.count <= len(r.sample)) else " …"
            lines.append(f"[{label}] {r.name} (state={r.state}, {r.doc_ref}): {r.count} divergent -- sample: {head}{suffix}")
        lines.append(f"TOTALS: hard_fail_total={self.hard_fail_total}, soft_divergence_total={self.soft_divergence_total}")
        return "\n".join(lines)


async def run_shadow_compare(session: AsyncSession, *, sample_cap: int = 20, verbose: bool = False) -> Report:
    """Run the state↔derived anti-join for every :data:`INVARIANTS` entry and return a :class:`Report`.

    For each invariant, count and sample the files that assert ``state = X`` yet violate the derived
    implication (``NOT <derived-condition>``). Read-only: only ``SELECT``s, no writes, no ``saq_jobs``
    access. The sample is capped at ``sample_cap`` unless ``verbose`` (which returns the full divergent
    set). ``Report.hard_fail_total`` gates on the non-soft invariants only (D-06).
    """
    results: list[InvariantResult] = []
    for inv in INVARIANTS:
        # `state = X AND NOT <derived-condition>` -- the divergent rows (implication violated).
        condition = and_(FileRecord.state == inv.state, ~inv.predicate())

        count = int((await session.execute(select(func.count(FileRecord.id)).where(condition))).scalar_one())

        sample_query = select(FileRecord.id).where(condition)
        if not verbose:
            sample_query = sample_query.limit(sample_cap)
        sample = [str(fid) for fid in (await session.execute(sample_query)).scalars().all()]

        results.append(InvariantResult(name=inv.name, state=inv.state, soft=inv.soft, count=count, sample=sample, doc_ref=inv.doc_ref))
    return Report(results=results)
