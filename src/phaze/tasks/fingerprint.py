"""SAQ task: fingerprint_file -- submit a local file to audfprint + panako sidecars,
post per-engine result via HTTP (Phase 26 D-05).

Per D-17: both engines run on every file. Per D-18/DERIV-05: the fingerprint stage
is DONE once ANY engine reports ``success``/``completed`` -- a failed sibling engine
does not block it. Completion is derived server-side (per-engine rows written by the
fingerprint endpoint's idempotent upsert, aggregated on read via ``done_clause`` /
``failed_clause``) rather than by flipping a single file-level state. This task only
sends the per-engine writes.

phaze-ds1z: a job is only COMPLETED when this file actually received a verdict. If no engine
succeeded and every failure was engine-level (sidecar 5xx / unreachable), the task raises
:class:`FingerprintEnginesUnavailable` instead of writing ``failed`` rows -- an infrastructure
outage must not be laundered into per-file data-quality verdicts. Zero-success outcomes are
reported as ``status="failed"``, never ``"partial"``.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/shared/core/test_task_split.py (Plan 10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from phaze.schemas.agent_fingerprint import FingerprintWriteRequest
from phaze.schemas.agent_tasks import FingerprintFilePayload


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient
    from phaze.services.fingerprint import FingerprintOrchestrator


logger = structlog.get_logger(__name__)


class FingerprintEnginesUnavailable(RuntimeError):
    """Every fingerprint engine failed with an ENGINE-level fault (phaze-ds1z).

    Raised instead of completing the job so SAQ applies retry/backoff and ultimately records
    a FAILED job, rather than the pre-fix behaviour: silently completing with
    ``status="partial"`` and PUTting a per-engine ``failed`` row for every file. That
    behaviour drained an ~11k-file backlog at ~2.6s/file into 22,856 FAILED rows with zero
    successes while the SAQ dashboard showed nothing but completions.
    """


# Consecutive all-engine-outage observations before the log escalates from per-file warning
# to a loud, operator-visible outage alert. Small enough to fire within seconds of a real
# outage, large enough that a couple of unlucky transient 503s don't cry wolf.
ENGINE_OUTAGE_ALERT_THRESHOLD: int = 3

# Process-local consecutive-outage counter. Deliberately NOT durable and NOT shared across
# lane workers: it exists only to escalate logging, never to gate work (see the module note
# in ``_note_engine_outage``).
_consecutive_engine_outages: int = 0


def _note_engine_outage(file_id: str, errors: dict[str, str | None]) -> None:
    """Record an all-engine outage and escalate the log once it looks systemic.

    This is an outage DETECTOR, not a circuit breaker. A true breaker would pause the
    fingerprint lane (``pipeline_stage_control``), but the agent worker is structurally
    forbidden from touching Postgres/SQLAlchemy (``tests/shared/core/test_task_split.py``)
    and no agent-facing pause endpoint exists -- adding one is a control-plane change well
    outside this bead. Detection plus the raise in :func:`fingerprint_file` already satisfies
    the acceptance criterion (the backlog stops draining; retries back off; an
    operator-visible error is emitted); pausing/resuming the lane stays an operator action.

    Self-healing by construction: any engine success anywhere in the worker resets the count,
    so the alert cannot latch on after the sidecars recover.
    """
    global _consecutive_engine_outages
    _consecutive_engine_outages += 1
    if _consecutive_engine_outages >= ENGINE_OUTAGE_ALERT_THRESHOLD:
        logger.error(
            "FINGERPRINT ENGINES DOWN -- every engine is failing at the engine level; "
            "fingerprint jobs are being failed, not completed. Check the audfprint/panako sidecars "
            "and consider pausing the fingerprint stage.",
            file_id=file_id,
            consecutive_failures=_consecutive_engine_outages,
            engine_errors=errors,
        )


def _reset_engine_outages() -> None:
    """Clear the consecutive-outage counter after any engine success."""
    global _consecutive_engine_outages
    _consecutive_engine_outages = 0


async def fingerprint_file(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Fingerprint a file through both engines; PUT per-engine result via HTTP."""
    payload = FingerprintFilePayload.model_validate(kwargs)

    api: PhazeAgentClient = ctx["api_client"]
    orchestrator: FingerprintOrchestrator = ctx["fingerprint_orchestrator"]

    logger.info("fingerprint started", file_id=str(payload.file_id))

    try:
        # Submit to both audfprint + panako (local sidecars)
        results = await orchestrator.ingest_all(payload.original_path)

        # phaze-ds1z: BEFORE writing any per-engine row, decide whether this file actually got
        # a verdict or whether the engines are simply down. If NO engine succeeded and EVERY
        # failure is engine-level (5xx / unreachable), this file was never really examined --
        # recording `failed` rows for it would be fabricating a data-quality verdict out of an
        # infrastructure outage. Raise instead: SAQ retries with backoff, the job is recorded
        # FAILED (visible on the dashboard), and no row is written, so the file stays merely
        # pending and needs no bulk repair once the sidecars come back.
        #
        # Deliberately narrow, to preserve D-18 and per-file failure semantics:
        #   - ANY engine success  -> normal completion (a single live engine still finishes
        #                            the stage; a dead sibling must not block it).
        #   - ANY file-level fail -> normal completion recording the failure (a corrupt file
        #                            must fail its OWN job, not stall the lane behind retries).
        if results and not any(r.status == "success" for r in results.values()) and all(r.engine_error for r in results.values()):
            engine_errors = {name: r.error for name, r in results.items()}
            _note_engine_outage(str(payload.file_id), engine_errors)
            logger.error("fingerprint aborted -- all engines failed at the engine level", file_id=str(payload.file_id), engine_errors=engine_errors)
            msg = f"all fingerprint engines failed at the engine level: {engine_errors}"
            raise FingerprintEnginesUnavailable(msg)

        if any(r.status == "success" for r in results.values()):
            _reset_engine_outages()

        # PUT per-engine result via HTTP -- idempotent on (file_id, engine) UQ
        all_success = True
        for engine_name, engine_result in results.items():
            body = FingerprintWriteRequest(
                status=engine_result.status,
                error_message=engine_result.error,
            )
            await api.put_fingerprint(payload.file_id, engine_name, body)
            logger.debug("fingerprint engine result", file_id=str(payload.file_id), engine=engine_name, status=engine_result.status)
            if engine_result.status != "success":
                all_success = False
    except Exception:
        # Phase 45 (L-02 / CR-02): clear the single-per-file scheduling-ledger row on the TERMINAL
        # attempt only (a failure anywhere in the orchestrator submit or the per-engine PUT loop),
        # then re-raise so SAQ records the failed attempt. A retryable attempt (or job absent in a
        # pure unit test) re-raises silently so the one real retry can run -- the row survives for
        # it (T-45-06). Mirrors process_file's generic guard (functions.py:179-189). Without this
        # ack a terminally-failed fingerprint file stays in get_fingerprint_pending_files forever,
        # so is_domain_completed can never fire and recover_orphaned_work re-enqueues it every pass.
        job = ctx.get("job")
        if job is not None and not job.retryable:
            # Best-effort ack: if report_fingerprint_failed ALSO raises (E2) while handling the
            # original failure (E1), swallow + log E2 so the bare `raise` below always re-raises
            # E1 -- SAQ must record the real task error, not the ack error (WR-01).
            try:
                await api.report_fingerprint_failed(payload.file_id)
            except Exception:
                logger.warning("fingerprint_file terminal-ack failed", file_id=str(payload.file_id), exc_info=True)
        raise

    # phaze-ds1z: three outcomes, not two. "partial" previously covered BOTH "one of two engines
    # succeeded" and "nothing succeeded at all", so a total outage read as partial success in the
    # log stream. Zero successes is now "failed" -- it can never be mistaken for progress.
    any_success = any(r.status == "success" for r in results.values())
    if all_success:
        status = "fingerprinted"
    elif any_success:
        status = "partial"
    else:
        status = "failed"
    log = logger.info if any_success else logger.warning
    log("fingerprint completed", file_id=str(payload.file_id), status=status, engines=len(results))
    return {
        "file_id": str(payload.file_id),
        "status": status,
    }
