"""Force-skip a stage + the per-file eligibility trace."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

# The suppression below is deliberate (runtime import, NOT type-only): this module carries
# `from __future__ import annotations`, so ruff offers to move `uuid` into the TYPE_CHECKING block.
# FastAPI resolves route annotations at RUNTIME via get_type_hints, so a `file_id: uuid.UUID` path
# param would raise NameError on import. (Before phaze-0jpe this import also had a plain runtime
# use -- `uuid.uuid4()` for the scan_live_set nonce -- which masked the rule; the annotation
# requirement is the real reason it must stay here.)
import uuid  # noqa: TC003

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from phaze.database import get_session
from phaze.enums.stage import ELIGIBILITY_DAG, ELIGIBLE_AFTER_FAILURE, Stage, Status, eligible, resolve_status
from phaze.models.analysis import AnalysisResult
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.models.tracklist import Tracklist
from phaze.routers.pipeline._common import _stage_pill_oob, logger, router, templates
from phaze.services.pg_text import sanitize_pg_text
from phaze.services.pipeline import get_file_stage_buckets
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# --------------------------------------------------------------------------------------------------
# Force-skip writer (UI-04 / D-08/D-09/D-10): the right-pane escape hatch that lets the ``failed``
# bucket converge for genuinely-unprocessable files. The correctness-sensitive mutating endpoint of
# this phase: enrich-only (approval-bypass hazard, D-10), additive (never clears a failure marker, so
# the Phase-79 shadow-compare gate stays green), reason required + sanitized (NUL-abort footgun), and
# committed (get_session NEVER auto-commits).
# --------------------------------------------------------------------------------------------------
@router.post("/pipeline/files/{file_id}/skip/{stage}", response_class=HTMLResponse)
async def force_skip_stage(
    file_id: uuid.UUID,
    stage: str,
    reason: Annotated[str, Form()],
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Force-skip an ENRICH stage for one file: write a distinct ``skipped`` marker (UI-04, D-08/D-09/D-10).

    The escape hatch that lets the ``failed`` bucket converge for genuinely-unprocessable files. It is
    deliberately NOT ``done``: a ``stage_skip`` marker row derives the honest ``skipped`` bucket, which
    stays distinguishable from real completion forever (D-08). Discipline (mirrors the retry endpoints +
    ``pipeline_stages._validate_stage``):

    - ENRICH-ONLY (D-10, T-87-18): ``stage`` must be in :data:`STAGE_TO_FUNCTION`
      (metadata/analyze) — a ``propose``/``review``/``apply`` skip is an approval-bypass
      hazard and returns 422 BEFORE any write, backstopped by the Plan-01 DB CHECK.
    - REASON REQUIRED (D-09, T-87-22): a blank/whitespace reason returns the inline validation fragment
      with NO write.
    - SANITIZED (T-87-19): ``sanitize_pg_text`` strips NUL / lone surrogates before persist — a NUL in
      free text passes pydantic then aborts the PG txn (the unbounded-recovery-loop footgun).
    - ADDITIVE-ONLY (T-87-20): the writer ONLY adds the marker row; it NEVER clears ``analysis.failed_at``
      or any failure marker, so a terminally-failed stage keeps its failure fact and the shadow-compare
      gate stays green. Precedence (``done ≻ skipped ≻ failed``) — not the writer — decides the bucket.
    - COMMITTED (Pitfall 7): ``get_session`` does NOT auto-commit, so the writer commits itself.

    The pill flips to ``⊘ skipped`` on the NEXT poll tick (not optimistic) — the ack is a toast only.
    ``reason`` is never echoed back into the response (T-87-21 — no XSS surface via the free text).

    UNKNOWN/CONCURRENTLY-DELETED FILE (request_guards.py contract rule 4): ``StageSkip.file_id`` is a
    NOT NULL FK to ``files.id`` that ``on_conflict_do_nothing(index_elements=["file_id", "stage"])``
    does not shield — that arbiter only absorbs the UNIQUE(file_id, stage) replay, not a FK whose
    referent is missing. ``file_id`` is a typed ``uuid.UUID`` path param already well-formed by the
    time this body runs, so no stricter signature could reject a nonexistent-but-well-formed id; this
    is a genuine race (a concurrent ``delete_scan_cascade`` removing the row between page-render and
    submit), not a wire-boundary defect. Two layers, not one:

    - A pre-check (:func:`_force_skip_file_exists`) short-circuits the common case with a clean no-op
      toast and skips the DB round trip through Postgres's error path -- but it is a TOCTOU hole on
      its own, since the row can vanish between the check and the INSERT.
    - The INSERT itself runs inside a SAVEPOINT (``session.begin_nested()``) and a caught
      ``IntegrityError`` unwinds only the nested scope (rule 5), leaving the session usable for the
      rest of the request, so the race window between the pre-check and the write is closed too.

    Mirrors the sibling per-file endpoints' T-87-27 discipline: an unknown well-formed UUID is a safe
    no-op, never a 500.
    """
    if stage not in STAGE_TO_FUNCTION:  # D-10 enrich-only — mirror pipeline_stages._validate_stage
        raise HTTPException(status_code=422, detail="stage not force-skippable")
    # Sanitize BEFORE the blank check (WR-01): str.strip() alone does not remove NUL / control chars, so a
    # NUL-only reason would slip past a raw-input gate and then persist as "". Validate the SANITIZED value.
    clean_reason = sanitize_pg_text(reason).strip()  # project memory: NUL aborts the PG txn (services/pg_text.py)
    if not clean_reason:  # D-09 reason required — inline validation on the sanitized value, NO write
        return HTMLResponse(
            '<p class="text-sm font-medium text-red-600 dark:text-red-400" role="alert">A reason is required.</p>',
            status_code=422,
        )
    # Pre-check (T-87-27 discipline, mirrors the sibling retry endpoints): short-circuits the common
    # "stale Files-matrix row" case with a clean no-op ack and no DB error round trip. NOT sufficient
    # alone -- see the IntegrityError catch below for the race this cannot close.
    if not await _force_skip_file_exists(session, file_id):
        return _force_skip_no_op_toast(stage)
    # Idempotent additive write (CR-01): re-submitting a force-skip for the same (file, stage) is a NORMAL
    # path — `_force_skip_dialog.html` is not hidden after success — and the UNIQUE(file_id, stage) constraint
    # would turn a bare INSERT into an unhandled IntegrityError → HTTP 500. on_conflict_do_nothing mirrors
    # `insert_ledger_if_absent`: the marker's existence IS the desired end state, so a duplicate is a no-op
    # success. Never clears failed_at (additive-only, T-87-20).
    #
    # Rule 4 + rule 5: the pre-check above is TOCTOU-vulnerable (delete_scan_cascade can remove the file
    # between the SELECT and this INSERT), so the INSERT itself runs inside a SAVEPOINT and a genuine FK
    # violation is caught and converted to the same no-op ack rather than an unhandled 500. Rolling back
    # only the nested scope (not a full session.rollback()) keeps the session usable for the rest of the
    # request.
    stmt = pg_insert(StageSkip).values(file_id=file_id, stage=stage, reason=clean_reason).on_conflict_do_nothing(index_elements=["file_id", "stage"])
    try:
        async with session.begin_nested():
            await session.execute(stmt)
    except IntegrityError:
        logger.info("force_skip_stage race: file deleted between pre-check and insert", file_id=str(file_id), stage=stage)
        return _force_skip_no_op_toast(stage)
    await session.commit()  # get_session does NOT auto-commit (Pitfall 7)
    logger.info("force_skip_stage wrote marker", file_id=str(file_id), stage=stage)
    # HTMX ack: the refreshed stage pill (oob, outerHTML) + the success toast (oob to #toast-container).
    # stage is allowlisted (safe to interpolate); the operator reason is NOT echoed (T-87-21).
    # Re-derive the bucket AFTER the commit rather than hardcoding "skipped": precedence is
    # ``in_flight ≻ done ≻ skipped ≻ failed``, so a stage that is genuinely done must keep reading
    # ✓ done even once a skip marker exists. Reusing the record router's own derivation keeps the
    # pane and the Files matrix on ONE status source (CONSOLE-01).
    buckets = await get_file_stage_buckets(session, file_id)
    toast = (
        f'<div hx-swap-oob="beforeend:#toast-container">'
        f'<div role="status" aria-live="polite" x-data="{{ show: true }}" x-show="show" '
        f'x-init="setTimeout(() => show = false, 5000)" x-transition '
        f'class="rounded bg-gray-800 px-4 py-2 text-sm text-white shadow dark:shadow-none dark:ring-1 dark:ring-phaze-border">'
        f"Skipped {stage} — reason recorded.</div></div>"
    )
    return HTMLResponse(_stage_pill_oob(file_id, stage, buckets.get(stage, "skipped")) + toast)


async def _force_skip_file_exists(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """``True`` iff a ``FileRecord`` row named by ``file_id`` currently exists.

    Extracted to a named helper (rather than inlined) so a regression test can force the
    :func:`force_skip_stage` race branch deterministically -- monkeypatching this to always return
    ``True`` reproduces "file existed at pre-check time, vanished before the INSERT" without a second
    concurrent connection.
    """
    result = await session.execute(select(FileRecord.id).where(FileRecord.id == file_id))
    return result.scalar_one_or_none() is not None


def _force_skip_no_op_toast(stage: str) -> HTMLResponse:
    """Benign toast for an unknown or concurrently-deleted ``file_id`` (T-87-27 discipline).

    ``stage`` is allowlisted before this is ever called (``STAGE_TO_FUNCTION`` membership), so it is
    safe to interpolate. Returned by both the pre-check miss and the ``IntegrityError`` race branch in
    :func:`force_skip_stage` so the two failure paths are indistinguishable to the client.
    """
    return HTMLResponse(
        '<div hx-swap-oob="beforeend:#toast-container">'
        '<div role="status" aria-live="polite" x-data="{ show: true }" x-show="show" '
        'x-init="setTimeout(() => show = false, 5000)" x-transition '
        'class="rounded bg-gray-800 px-4 py-2 text-sm text-white shadow dark:shadow-none dark:ring-1 dark:ring-phaze-border">'
        f"File not found — nothing to skip {stage}.</div></div>"
    )


# --------------------------------------------------------------------------------------------------
# Per-file eligibility trace (UI-03 / D-06/D-07): the diagnostic whose absence hid the deadlock. A
# single-row resolve_status/eligible() evaluation (NOT a corpus scan, T-87-23) that names the ONE
# unmet blocker keeping a stage out of the pending set.
# --------------------------------------------------------------------------------------------------

# Display label per stage for the five-pill matrix + trace verdict (the 6->5 remap: tracklist is
# omitted; review renders as Appr, apply as Exec). Mirrors the _stage_matrix partial pill order.
_STAGE_TRACE_LABELS: dict[Stage, str] = {
    Stage.METADATA: "Meta",
    Stage.ANALYZE: "Analyze",
    Stage.PROPOSE: "Prop",
    Stage.REVIEW: "Appr",
    Stage.APPLY: "Exec",
}


async def _one_stage_scalars(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> dict[str, Any]:
    """Read ONE file's per-stage scalars in the DB-free ``resolve_status`` shape (mirrors ``load_scalars``).

    Every read is strictly ``file_id``-scoped (T-87-23 — a single-row evaluation, never a corpus scan).
    """
    func_name = STAGE_TO_FUNCTION.get(stage.value)
    inflight = False
    if func_name is not None:
        ledger_row = (await session.execute(select(SchedulingLedger.key).where(SchedulingLedger.key == f"{func_name}:{file_id}"))).first()
        inflight = ledger_row is not None

    async def _skipped() -> bool:
        found = (await session.execute(select(StageSkip.id).where(StageSkip.file_id == file_id, StageSkip.stage == stage.value))).first()
        return found is not None

    if stage is Stage.ANALYZE:
        arow = (
            await session.execute(select(AnalysisResult.analysis_completed_at, AnalysisResult.failed_at).where(AnalysisResult.file_id == file_id))
        ).first()
        return {"completed_at": arow[0] if arow else None, "failed_at": arow[1] if arow else None, "inflight": inflight, "skipped": await _skipped()}
    if stage is Stage.METADATA:
        mrow = (await session.execute(select(FileMetadata.failed_at).where(FileMetadata.file_id == file_id))).first()
        return {"row_present": mrow is not None, "failed_at": mrow[0] if mrow else None, "inflight": inflight, "skipped": await _skipped()}
    if stage is Stage.TRACKLIST:
        present = (await session.execute(select(Tracklist.id).where(Tracklist.file_id == file_id))).first() is not None
        return {"row_present": present, "failed": False, "inflight": inflight}
    if stage in (Stage.PROPOSE, Stage.REVIEW):
        present = (await session.execute(select(RenameProposal.id).where(RenameProposal.file_id == file_id))).first() is not None
        failed = (
            await session.execute(select(RenameProposal.id).where(RenameProposal.file_id == file_id, RenameProposal.status == "failed"))
        ).first() is not None
        return {"row_present": present, "failed": failed, "inflight": inflight}
    # apply: execution_log joined through proposals (execution_log has NO file_id)
    present = (
        await session.execute(
            select(ExecutionLog.id)
            .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
            .where(RenameProposal.file_id == file_id, ExecutionLog.status == "completed")
        )
    ).first() is not None
    failed = (
        await session.execute(
            select(ExecutionLog.id)
            .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
            .where(RenameProposal.file_id == file_id, ExecutionLog.status == "failed")
        )
    ).first() is not None
    return {"row_present": present, "failed": failed, "inflight": inflight}


async def _has_approved_proposal(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """file_id-scoped single-row probe: does an APPROVED proposal exist? (apply's ELIG-02 gate)."""
    row = (
        await session.execute(
            select(RenameProposal.id).where(RenameProposal.file_id == file_id, RenameProposal.status == ProposalStatus.APPROVED.value)
        )
    ).first()
    return row is not None


async def _eligibility_trace_context(session: AsyncSession, file_id: uuid.UUID, stage: Stage) -> dict[str, Any]:
    """Evaluate ``eligible()`` for ONE file/stage and build the named-conjunct trace context (UI-03, D-06/D-07).

    Loads the stage's own status plus its ``ELIGIBILITY_DAG`` upstream statuses (single-row reads),
    evaluates the REAL ``eligible()`` (the scheduler's source of truth) in Python, and names the single
    unmet blocker. Enrich stages have no upstream, so ``upstream met?`` is vacuously true. The upstream
    conjunct STRICTLY mirrors ``eligible()`` (upstream must be DONE): under the OQ-1 SCOPE-MINIMAL
    resolution a force-skipped enrich upstream does NOT unblock its downstream (Phase 90), so a SKIPPED
    upstream is rendered as still-gating — a lenient "skipped = met" display would make the trace claim a
    downstream is eligible when the scheduler permanently gates it (the deadlock UI-03 exists to expose).
    NOT a corpus query (T-87-23).
    """
    label = _STAGE_TRACE_LABELS.get(stage, stage.value)
    upstreams = ELIGIBILITY_DAG[stage]
    statuses: dict[Stage, Status] = {stage: resolve_status(stage, await _one_stage_scalars(session, stage, file_id))}
    for u in upstreams:
        statuses[u] = resolve_status(u, await _one_stage_scalars(session, u, file_id))
    has_approved = await _has_approved_proposal(session, file_id) if stage is Stage.APPLY else False

    target = statuses[stage]
    is_done = target == Status.DONE
    is_in_flight = target == Status.IN_FLIGHT
    is_skipped = target == Status.SKIPPED
    is_terminal_fail = stage in ELIGIBLE_AFTER_FAILURE and target == Status.FAILED and not ELIGIBLE_AFTER_FAILURE[stage]

    if stage is Stage.APPLY:
        # apply is gated on an APPROVED proposal (ELIG-02), NOT on bare done(review).
        upstream_met = has_approved
        upstream_phrase = "approved proposal exists" if has_approved else "no approved proposal"
    elif upstreams:
        # STRICT mirror of eligible()'s downstream check (upstream must be DONE). Under the OQ-1
        # SCOPE-MINIMAL resolution a force-skipped enrich upstream does NOT unblock its downstream
        # (deferred to Phase 90), so a SKIPPED upstream stays gating — the trace names it honestly
        # rather than claiming a downstream is eligible when the scheduler permanently gates it.
        unmet = [u for u in upstreams if statuses[u] != Status.DONE]
        upstream_met = not unmet
        if upstream_met:
            upstream_phrase = "all upstream done"
        elif statuses[unmet[0]] == Status.SKIPPED:
            upstream_phrase = f"{unmet[0].value} skipped — downstream stays gated (Phase 90)"
        else:
            upstream_phrase = f"{unmet[0].value} not done"
    else:  # enrich: empty upstream is vacuously met (ELIG-01 independence)
        upstream_met = True
        upstream_phrase = "no upstream (enrich stage)"

    # Verdict is the REAL eligible() — the single source of truth the scheduler uses. A diagnostic that
    # diverged from it would hide the very deadlock UI-03 exists to expose.
    is_eligible = eligible(statuses, stage, has_approved_proposal=has_approved)

    # The single blocker follows eligible()'s short-circuit order (skipped folds onto the settled done? line).
    blocker = ""
    if not is_eligible:
        if is_done or is_skipped:
            blocker = "done"
        elif is_in_flight:
            blocker = "inflight"
        elif is_terminal_fail:
            blocker = "terminal"
        elif not upstream_met:
            blocker = "upstream"

    if is_done:
        done_ok, done_phrase = True, "already done"
    elif is_skipped:
        done_ok, done_phrase = True, "force-skipped (⊘) — recorded as skipped, not done"
    else:
        done_ok, done_phrase = False, "not done"

    conjuncts = [
        {"question": "done?", "ok": done_ok, "phrase": done_phrase, "blocker": blocker == "done"},
        {
            "question": "in-flight?",
            "ok": is_in_flight,
            "phrase": "currently running" if is_in_flight else "not running",
            "blocker": blocker == "inflight",
        },
        {"question": "upstream met?", "ok": upstream_met, "phrase": upstream_phrase, "blocker": blocker == "upstream"},
        {
            "question": "terminal fail?",
            "ok": is_terminal_fail,
            "phrase": "terminal failure — retry is manual" if is_terminal_fail else "no terminal failure",
            "blocker": blocker == "terminal",
        },
    ]
    verdict = f"{label} — eligible (in the pending set)" if is_eligible else f"{label} — NOT eligible"
    return {"stage_label": label, "eligible": is_eligible, "verdict": verdict, "conjuncts": conjuncts, "unavailable": False}


@router.get("/pipeline/files/{file_id}/trace/{stage}", response_class=HTMLResponse)
async def eligibility_trace(
    request: Request,
    file_id: uuid.UUID,
    stage: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Per-file, per-stage eligibility trace (UI-03) — the diagnostic whose absence hid the deadlock.

    Renders ``_eligibility_trace.html`` under the clicked pill: a verdict line plus the four named
    conjuncts (``done?`` · ``in-flight?`` · ``upstream met?`` · ``terminal fail?``) with the single
    unmet blocker highlighted. It is a single-row ``resolve_status``/``eligible()`` evaluation
    (T-87-23 — never a corpus scan) and degrades to "Trace unavailable this tick." on any error, so a
    poll never 500s.
    """
    stage_enum: Stage | None
    try:
        stage_enum = Stage(stage)
    except ValueError:
        stage_enum = None
    context: dict[str, Any] = {"request": request}
    if stage_enum is None:
        context["unavailable"] = True
        return templates.TemplateResponse(request=request, name="pipeline/partials/_eligibility_trace.html", context=context)
    try:
        context.update(await _eligibility_trace_context(session, file_id, stage_enum))
    except Exception:
        logger.warning("eligibility_trace degraded", file_id=str(file_id), stage=stage, exc_info=True)
        context["unavailable"] = True
    return templates.TemplateResponse(request=request, name="pipeline/partials/_eligibility_trace.html", context=context)
