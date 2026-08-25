"""The proposal convergence gate -- one predicate, shared by the counter and the batching producer.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr). The in-flight gate the
trigger routes check (``get_proposal_busy_count``) is a ``saq_jobs`` probe and lives in
:mod:`phaze.services.pipeline.jobs` with its shape-identical siblings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select
import structlog

from phaze.enums.stage import Stage
from phaze.models.file import FileRecord
from phaze.services.stage_status import (
    done_clause,
    inflight_clause,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


logger = structlog.get_logger(__name__)


def _proposal_pending_clauses() -> tuple[ColumnElement[bool], ...]:
    """The D-02 convergence-gate predicate, defined ONCE, over ``FileRecord``.

    phaze-37i1.2: extracted so the batching producer (:func:`get_proposal_pending_batches`)
    and the read-only counter (:func:`count_proposal_pending_files`) can never drift apart.
    A counter that answered a slightly different question than the trigger would put a
    number in front of the operator that the button does not honour -- exactly the class of
    dishonest UI this bead exists to remove.

    Four conjuncts: not already proposed, metadata present, a COMPLETED analysis, and (phaze-3542b)
    NO enrich stage in flight. The fourth is not a convergence condition -- it is a safety
    interlock against the move a proposal ultimately performs; see its comment below.
    """
    return (
        # Phase 90 (PR-A, Pitfall 4): the ``files.state IN (ANALYZED, METADATA_EXTRACTED)`` gate is
        # REPLACED by ``~done_clause(Stage.PROPOSE)`` -- a file with an existing proposal is a done
        # PROPOSE and is EXCLUDED, so no already-proposed file is ever re-proposed. The two
        # convergence clauses below (metadata DONE and analysis DONE) still bound the set.
        ~done_clause(Stage.PROPOSE),
        # phaze-rhs6m: metadata is gated on ``done_clause(Stage.METADATA)`` -- a row present AND
        # ``failed_at IS NULL`` -- NOT on bare row-existence. A metadata FAILURE is persisted as a
        # ``metadata`` row with ``failed_at`` set and the payload columns NULL
        # (``routers/agent_metadata.py``'s ``report_metadata_failed``), so the previous bare
        # ``exists(FileMetadata)`` admitted a file whose metadata never landed. That was the
        # ASYMMETRY with the analysis conjunct below, which has required its own completion
        # discriminator since Phase 57.1, and it was reachable rather than theoretical: such a file
        # could be proposed, approved and EXECUTED, and -- because ``done_clause(Stage.METADATA)``
        # stays False until real metadata lands -- it then sat in the metadata pending set FOREVER,
        # where all four ``ExtractMetadataPayload`` producers re-drive it at ``original_path``, the
        # ingest-time location it has just been moved away from (D-24, schemas/agent_tasks.py).
        #
        # The producers are deliberately UNCHANGED (operator decision 2026-08-24, phaze-rhs6m): the
        # fix is at the gate, so the state that strands them is never reached, rather than at four
        # call sites whose read path would change for every file in the archive.
        #
        # Force-skip is NOT affected and must not be "fixed" to compensate: ``force_skip_stage``
        # (routers/pipeline/skip.py) writes ONLY a ``stage_skip`` marker and is documented
        # additive-only and "deliberately NOT ``done``", so a skipped file has no ``metadata`` row to
        # satisfy either form of this conjunct -- it was un-proposable before this change too.
        done_clause(Stage.METADATA),
        # Phase 57.1 (D-03 KEY RISK): require the COMPLETION discriminator, not bare row-existence.
        # D-03 upserts a partial `analysis` row at analysis START (NULL aggregates, completed_at NULL)
        # while the file is still METADATA_EXTRACTED -- bare `exists(AnalysisResult)` would batch that
        # partial row into generate_proposals with NULL bpm/key/mood. `analysis_completed_at IS NOT
        # NULL` (stamped only in the put_analysis completion branch) gates it out; in-flight rows have
        # completed_at NULL. phaze-rhs6m composes this from the shared ``done_clause`` builder rather
        # than hand-rolling the same EXISTS -- a byte-equivalent swap (stage_status.py's ANALYZE
        # branch IS this predicate), made so both conjuncts now read from ONE definition and the
        # asymmetry above cannot silently reappear on either side.
        done_clause(Stage.ANALYZE),
        # phaze-3542b: NO ENRICH STAGE MAY BE IN FLIGHT. A proposal that is approved and executed
        # MOVES the file and unlinks the source, while every enrich payload carries the pre-move
        # `FileRecord.original_path` (D-24). A job already enqueued when the move lands therefore
        # opens a path that no longer exists, and nothing re-validates it at the consumer
        # (`tasks/functions.py` `read_path = payload.scratch_path or payload.original_path`;
        # `tasks/s3_upload.py`'s docstring calls an unreadable original_path "a clear TERMINAL
        # error -- the task NEVER falls back"). These two conjuncts are the DOOR: a file with work
        # in flight is not proposable, so the move cannot be scheduled underneath it.
        #
        # OPERATOR DECISION 2026-08-25 (ADR-0012 rule 2). Question as put: the mechanism for
        # phaze-3542b's confirmed enqueue-then-execute TOCTOU, offered as labelled options.
        # Answer as given -- the option LABEL the operator selected, and the whole of what they
        # authored: "Close the door: add ~inflight to the propose gate". Durable record: the
        # phaze-3542b bead comment dated 2026-08-25. "Narrow the producer" was REFUSED, so
        # `services/reanalysis_backfill.py` is deliberately NOT changed. The label decided the
        # MECHANISM and the SEAM; it did not decide the stage scope, which is the implementer's
        # call and is argued below.
        #
        # WHY BOTH STAGES, when only analyze carries the CONFIRMED shape. Analyze is the measured
        # one: `services/reanalysis_backfill.py` deliberately bypasses the `~done` gate (its module
        # docstring :59-79), so `done(analyze) AND inflight(analyze)` is reachable head-on -- it is
        # the ONLY producer of eight whose selection set intersects this gate's. Metadata is NOT
        # merely redundant, and that is why it is here rather than left to phaze-rhs6m:
        #   - BOTH metadata retry routes select on a bare `failed_clause` with NO in-flight
        #     conjunct of their own (`pipeline/pending.py::get_metadata_failed_files` and
        #     `routers/pipeline/extraction.py::retry_metadata_failed_file`), leaning entirely on
        #     SAQ's deterministic-key dedup; and
        #   - `services/scheduling_ledger.clear_ledger_entry` can legitimately SKIP its clear (the
        #     phaze-3yln ownership guard, and the phaze-jf7xt degraded-probe path), so a completed
        #     `put_metadata` can leave `done(metadata)` TRUE with a ledger row still standing.
        # Those two together make `done(metadata) AND genuinely-inflight(metadata)` reachable by a
        # narrow interleaving, which this conjunct closes. Independently: an ASYMMETRY between this
        # predicate's two enrich conjuncts is precisely what produced the parent bead phaze-rhs6m,
        # and re-introducing one here would repeat that defect's shape.
        #
        # THE COST, stated because it is real. `inflight` is bare ledger-row existence (D-01), so a
        # LEAKED row -- a job that finished but whose clear was skipped -- blocks proposing a file
        # whose work is actually done. That is bounded, not indefinite: `tasks/ledger_reaper.py`'s
        # `*/5` cron deletes exactly the domain-completed-and-not-live rows, so the false block
        # self-clears within ~5 minutes. It CANNOT reap a genuinely in-flight row, because
        # `resolved_ledger_clause` requires liveness to be ABSENT -- which is what makes this door
        # hold for the whole of a multi-hour analysis rather than being swept out from under it.
        ~inflight_clause(Stage.ANALYZE),
        ~inflight_clause(Stage.METADATA),
    )


async def count_proposal_pending_files(session: AsyncSession) -> int:
    """Return HOW MANY files currently clear the D-02 convergence gate, without loading them.

    phaze-37i1.2. Same predicate as :func:`get_proposal_pending_batches` (shared via
    :func:`_proposal_pending_clauses`) but a plain ``COUNT(*)``: the Audit Log's empty state
    needs the size of the eligible set, not its members, and the pending set is corpus-sized
    -- materialising every ``FileRecord`` to call ``len()`` on it would make an informational
    banner cost as much as the trigger itself.
    """
    stmt = select(func.count()).select_from(FileRecord).where(*_proposal_pending_clauses())
    return int((await session.execute(stmt)).scalar_one())


async def get_proposal_pending_batches(session: AsyncSession, batch_size: int) -> list[list[str]]:
    """Return the ``generate_proposals`` pending set as deterministic, sorted file-id batches.

    Runs the convergence query (files NOT yet proposed -- ``~done_clause(PROPOSE)`` -- with BOTH a
    ``FileMetadata`` AND a COMPLETED ``AnalysisResult`` row, and since phaze-3542b with NEITHER
    enrich stage in flight -- the EXACT set the manual proposals triggers use), then SORTS the
    file-id strings before chunking into ``batch_size`` groups.
    Phase 90 (PR-A, Pitfall 4): the propose-exclusion replaces the retired ``files.state`` membership,
    so an already-proposed file is never re-batched.

    Sorting BEFORE chunking makes a SINGLE call's batches deterministic (order-independent), which
    matters because ``generate_proposals`` is keyed on ``generate_proposals:<sha256(sorted
    file_ids)>`` (an order-independent SET hash, D-04, 42-RESEARCH Pitfall 2). Pure ORM / bound
    params, NO f-string SQL (T-42-03).

    phaze-8qheu CORRECTION: this does NOT make two SEPARATE calls dedup against each other, and
    recovery does not call this helper at all (it replays by stored scheduling-ledger key, not by
    re-deriving the pending set -- see ``tasks/reenqueue.py``). The pending set this query reads is
    a MOVING target: as soon as one file's proposal lands, ``~done_clause(PROPOSE)`` excludes it,
    every later chunk's boundary shifts, and every recomputed batch hashes to a KEY that shares
    nothing with the in-flight batches it overlaps. A second manual trigger mid-drain therefore
    dedups nothing and can re-propose files whose first proposal already landed (including
    already-approved/executed files, since the store's dedup is scoped to PENDING proposals only).
    Callers MUST NOT trigger a second drain while a ``generate_proposals`` batch from a prior
    trigger is still in flight -- see :func:`get_proposal_busy_count`, which both trigger routes
    gate on for exactly this reason.

    phaze-ceuvd: ``batch_size`` is used as the ``range()`` step below, so it degrades rather
    than crashes on a misconfigured value -- ``llm_batch_size`` now carries ``gt=0`` at the
    config layer (config.py), but this clamp is the second, independent layer: 0 previously
    raised ``ValueError: range() arg 3 must not be zero`` (unhandled 500 on GENERATE ALL) and a
    negative value made ``range(0, N, -k)`` empty, silently returning zero batches (success
    with nothing enqueued). Both non-positive inputs clamp to 1 (one file per batch) instead.
    """
    if batch_size < 1:
        logger.warning("proposal_pending_batches_size_clamped", requested_batch_size=batch_size, clamped_to=1)
        batch_size = 1
    stmt = select(FileRecord).where(*_proposal_pending_clauses())
    result = await session.execute(stmt)
    file_ids = sorted(str(f.id) for f in result.scalars().all())
    return [file_ids[i : i + batch_size] for i in range(0, len(file_ids), batch_size)]
