"""SAQ task: process_file -- essentia analysis of a music file, posted via HTTP (Phase 26 D-05).

Replaces the prior ORM-bound body. Reads the file from local disk via
payload.original_path, runs essentia in a dedicated child process (Phase 101:
``python -m phaze.analysis_child`` via the shared ``services.analysis_exec`` driver,
replacing the pebble ProcessPool + Manager-queue bridge), and posts the result via
ctx["api_client"].put_analysis (PUT /api/internal/agent/analysis/{file_id}).
A fresh child per file preserves pebble's ``max_tasks=1`` leak-recycling semantics
(essentia leaks ~7 GiB/file); the ctx-provided ``analysis_semaphore`` (sized from
``worker_process_pool_size``) preserves the pool's concurrency bound.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/shared/core/test_task_split.py (Plan 10).

Wire-format conversion (D-26):
- ``analyze_file`` returns ``mood``/``style`` as strings (dominant label).
- ``AnalysisWritePayload`` requires ``mood``/``style`` as ``dict[str, float]``.
- We rebuild the dicts from ``analysis["features"]`` so the wire contract is
  honored end-to-end: ``mood`` averages each ``mood_*`` set's positive-class
  prediction across the 3 variants; ``style`` takes the genre predictions
  returned by the discogs effnet model.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.schemas.agent_analysis import AnalysisFailurePayload, AnalysisProgressPayload, AnalysisWindowPayload, AnalysisWritePayload
from phaze.schemas.agent_tasks import ProcessFilePayload
from phaze.services.analysis_exec import AnalysisSubprocessError, run_analysis_subprocess
from phaze.services.analysis_wire import _features_to_mood_dict, _features_to_style_dict
from phaze.services.hashing import compute_sha256


if TYPE_CHECKING:
    import uuid

    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)


# Phase 43 (T-43-09): cap the worker-side exception text before it crosses the HTTP
# boundary. The control-side `AnalysisFailurePayload.error` is the authoritative bound
# (max_length=2000); truncating here avoids shipping a huge traceback string at all.
_ERROR_DETAIL_MAX = 2000


def _agent_settings() -> AgentSettings:
    """Return the AgentSettings for this worker process (Phase 43).

    ``process_file`` is registered ONLY on the agent worker (``PHAZE_ROLE=agent``), so
    ``get_settings()`` returns an :class:`AgentSettings`. The module-level ``settings``
    singleton is ``ControlSettings``-typed and intentionally lacks the agent-only
    ``analysis_*`` fields (config.py docstring), so we MUST resolve via ``get_settings()``
    and narrow — mirroring the agent_worker startup invariant.
    """
    cfg = get_settings()
    if not isinstance(cfg, AgentSettings):  # pragma: no cover - defensive; worker always agent-role
        msg = f"process_file requires PHAZE_ROLE=agent; get_settings() returned {type(cfg).__name__}"
        raise RuntimeError(msg)
    return cfg


# phaze-p0l9: the worker's accepted set MUST agree with the control-plane analyze pending set
# (services/pipeline.MUSIC_VIDEO_TYPES), which is derived from the SAME EXTENSION_MAP and includes BOTH
# music AND video. essentia decodes video containers via ffmpeg, so concert videos (the project's core
# use case) are analyzed like audio. Previously this gate was music-ONLY, so every video was enqueued,
# skipped without crossing any HTTP boundary, and left its process_file:<id> scheduling-ledger row
# uncleared -- the analyze stage never converged, recovery re-enqueued it forever, and a cloud-pushed
# video permanently jammed the bounded cloud window and leaked its scratch copy. Sourcing from
# EXTENSION_MAP keeps the two sets from ever drifting again.
_ANALYZABLE_FILE_TYPES = frozenset(ext.lstrip(".") for ext, cat in EXTENSION_MAP.items() if cat in (FileCategory.MUSIC, FileCategory.VIDEO))

# The mood/style wire-format converters (_features_to_mood_dict / _features_to_style_dict)
# now live in phaze.services.analysis_wire (Phase 52, KJOB-02) so the one-shot job_runner
# (Plan 02) and this SAQ path share one definition. They are imported above and re-exported
# from this module so existing callers (and tests/test_tasks/test_functions.py) resolve
# unchanged.


async def _post_progress_count(api: PhazeAgentClient, file_id: uuid.UUID, count: tuple[int, int]) -> None:
    """Best-effort counter-only POST of a single ``(analyzed, total)`` count (Phase 57.1, D-16).

    Swallows ANY error (the ``AgentApiError`` hierarchy from the client's single-attempt,
    short-timeout progress path (Phase 99 OBS-01), plus anything unexpected) so a dropped
    progress POST can never fail the analysis job — the completion ``put_analysis`` writes
    the final count regardless, so the bar still reaches 100% from completion.
    """
    analyzed, total = count
    try:
        await api.post_analysis_progress(file_id, AnalysisProgressPayload(fine_windows_analyzed=analyzed, fine_windows_total=total))
    except Exception:  # best-effort progress; never fail the job (mirrors report_analysis_failed discipline)
        logger.debug("process_file: progress POST dropped (best-effort)", file_id=str(file_id))


async def _run_analysis_with_progress(
    api: PhazeAgentClient,
    cfg: AgentSettings,
    file_id: uuid.UUID,
    read_path: str,
    models_path: str,
    fine_cap: int,
    coarse_cap: int,
) -> Any:
    """Run windowed analysis in the child subprocess while relaying throttled progress.

    Phase 101: the shared driver (``run_analysis_subprocess``) execs the analysis child
    and invokes ``_progress`` ON the event loop per fine window — the Manager-queue
    drainer this replaced is gone. Throttling stays parent-side and keeps the drainer's
    semantics: the FIRST emission always posts (``last_post`` starts ``None`` — a ``0.0``
    baseline would throttle away the START on a freshly-booted host), later emissions
    post at most every ``interval_sec``, and the last seen count is flushed on the way
    out even when the throttle swallowed it (D-04 final flush) — belt-and-suspenders
    with the completion PUT.

    Returns the ``analyze_file`` result dict. Raises ``TimeoutError`` (the driver kills a
    child exceeding ``analysis_inner_timeout_sec`` — the same exception the pebble SIGKILL
    produced) and :class:`AnalysisSubprocessError` (child crash/nonzero exit — the
    ``ProcessExpired`` replacement) for ``process_file``'s terminal handlers; the progress
    bridge itself never alters the terminal mapping.
    """
    interval_sec = cfg.analysis_progress_interval_sec
    last_post: float | None = None
    last_count: tuple[int, int] | None = None
    last_posted: tuple[int, int] | None = None
    pending: set[asyncio.Task[None]] = set()

    def _progress(analyzed: int, total: int) -> None:
        nonlocal last_post, last_count, last_posted
        last_count = (analyzed, total)
        now = time.monotonic()
        if interval_sec > 0.0 and last_post is not None and (now - last_post) < interval_sec:
            return
        last_post = now
        last_posted = (analyzed, total)
        # Fire-and-forget loop task (we're ON the loop); strong-ref'd so it is never GC'd
        # mid-flight. _post_progress_count swallows its own errors (best-effort, D-16).
        task = asyncio.get_running_loop().create_task(_post_progress_count(api, file_id, (analyzed, total)))
        pending.add(task)
        task.add_done_callback(pending.discard)

    try:
        return await run_analysis_subprocess(
            read_path,
            models_path,
            fine_cap=fine_cap,
            coarse_cap=coarse_cap,
            progress_cb=_progress,
            timeout=cfg.analysis_inner_timeout_sec,
        )
    finally:
        # Bounded, kill-safe teardown on every exit path (success, timeout kill, crash,
        # cancellation): drain in-flight POSTs, then flush the last seen count if the
        # throttle swallowed it. Best-effort by construction — never masks the outcome.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if last_count is not None and last_count != last_posted:
                await _post_progress_count(api, file_id, last_count)


async def process_file(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Run essentia analysis on a local file and post results via HTTP."""
    payload = ProcessFilePayload.model_validate(kwargs)

    # phaze-p0l9: skip only genuinely non-analyzable types (companion/unknown). Music AND video both
    # flow to analysis so the worker agrees with the pending set that enqueued them -- otherwise a
    # skipped video's ledger row never clears (perpetual in-flight + recovery churn + cloud-window jam).
    # In practice only music/video file_types are ever enqueued for process_file, so this guard is now
    # a defensive no-op for the real enqueue set rather than a silent video sink.
    if payload.file_type not in _ANALYZABLE_FILE_TYPES:
        return {"file_id": str(payload.file_id), "status": "skipped", "reason": "not_analyzable"}

    api: PhazeAgentClient = ctx["api_client"]

    # CPU-bound analysis in a killable child process (D-23: original_path is in the payload).
    # The inner per-task timeout (settings.analysis_inner_timeout_sec, default 6600s) has the
    # driver SIGKILL a runaway essentia child; the 60/30 caps bound how many windows
    # analyze_file decodes (Plan 02). Both are threaded from settings here so config drives them.
    cfg = _agent_settings()
    # Phase 44: a per-job payload cap override (the "deepen analysis" lever) takes precedence over
    # the AgentSettings 60/30 defaults; absent it (None), fall back to config exactly as before. A
    # cap of 0 reaches analysis.py::_stride_to_cap as the analyze-ALL-windows no-op (not special-cased here).
    fine_cap = payload.fine_cap if payload.fine_cap is not None else cfg.analysis_fine_cap
    coarse_cap = payload.coarse_cap if payload.coarse_cap is not None else cfg.analysis_coarse_cap

    # Phase 50 (D-11): when the control plane pinned a scratch_path, the agent reads/cleans that
    # ephemeral pushed copy instead of original_path -- the analyzer is path-agnostic so this is a
    # pure read-path swap. ``scratch_path is not None`` is ITSELF the compute-read signal. The
    # outer ``finally`` guarantees the scratch copy never outlives the job (CLOUDPIPE-04).
    read_path = payload.scratch_path or payload.original_path
    # CLOUDPIPE-04 / CR-01: the scratch copy is deleted in the ``finally`` ONLY on a TERMINAL
    # outcome (success, sha256 mismatch, inner-timeout/crash, or a non-retryable failure). On a
    # RETRYABLE re-raise this flips to False so the copy SURVIVES for the in-place SAQ retry to
    # re-verify and analyze -- otherwise the retry hits a missing scratch file, raises an uncaught
    # FileNotFoundError, and strands the file in PUSHED forever (permanently jamming the bounded
    # cloud window). Default True: every terminal path cleans up.
    cleanup_scratch = True
    try:
        # CLOUDPIPE-03: integrity-verify the pushed bytes BEFORE trusting them. sha256 is computed
        # OFF the event loop (chunked stdlib hash; the scan.py pattern). A mismatch means a
        # corrupt/partial transfer -> delete it, report so the control plane re-pushes (50-05 caps
        # attempts), and DO NOT analyze (T-50-corrupt). Gated on both fields being present so the
        # bulk local producer (neither set) takes none of this branch.
        if payload.scratch_path and payload.expected_sha256:
            try:
                actual_sha256 = await asyncio.to_thread(compute_sha256, Path(payload.scratch_path))
            except FileNotFoundError:
                # CR-01 defense-in-depth: the scratch copy is gone (a prior attempt raced cleanup,
                # or the push never landed). Route to a re-pushable mismatch rather than let the
                # FileNotFoundError escape uncaught and strand the file in PUSHED with no callback.
                # T-50-scratch-skew diagnostic: a persistent miss here most often means the
                # control plane's PHAZE_COMPUTE_SCRATCH_DIR (which built this path) does not match
                # the fileserver/agent PHAZE_CLOUD_SCRATCH_DIR (where push_file rsync'd the file),
                # which otherwise only surfaces as an endless silent re-push loop. Name the path so
                # the operator can diagnose a scratch-dir skew instead of guessing.
                logger.warning(
                    "process_file: pushed scratch copy not found at the expected path — routing to "
                    "push-mismatch for re-push. If this repeats for every cloud file, the control "
                    "plane PHAZE_COMPUTE_SCRATCH_DIR does not match the agent PHAZE_CLOUD_SCRATCH_DIR.",
                    file_id=str(payload.file_id),
                    scratch_path=payload.scratch_path,
                )
                await ctx["api_client"].report_push_mismatch(payload.file_id)
                return {"file_id": str(payload.file_id), "status": "push_mismatch"}
            if actual_sha256 != payload.expected_sha256:
                Path(payload.scratch_path).unlink(missing_ok=True)
                # ``report_push_mismatch`` is added to the agent client by Plan 50-03 (same wave);
                # reach it via the Any-typed ctx so this module need not import that parallel change.
                await ctx["api_client"].report_push_mismatch(payload.file_id)
                return {"file_id": str(payload.file_id), "status": "push_mismatch"}
        elif payload.scratch_path:
            # IN-01: scratch copy present but the control plane did not pin an expected sha256.
            # The control plane ALWAYS pins both (report_pushed reads the non-null sha256_hash), so
            # this is only reachable via a malformed payload. Analyze anyway (the documented skip
            # behavior) but WARN -- an unverified pushed copy is a defense-in-depth gap.
            logger.warning(
                "process_file: analyzing an unverified scratch copy (no expected_sha256 pinned)",
                file_id=str(payload.file_id),
            )

        try:
            # Phase 101 (OBS-03): run the analysis in the exec'd child via the shared driver,
            # with the parent-side throttled progress bridge posting
            # ctx["api_client"].post_analysis_progress mid-analysis (best-effort). The
            # ctx-provided semaphore (sized from worker_process_pool_size by agent_worker)
            # preserves the retired pebble pool's concurrency bound; absent (bare test ctx),
            # the single call needs no bound.
            semaphore: asyncio.Semaphore | None = ctx.get("analysis_semaphore")
            async with semaphore if semaphore is not None else contextlib.nullcontext():
                analysis = await _run_analysis_with_progress(
                    api,
                    cfg,
                    payload.file_id,
                    read_path,
                    payload.models_path,
                    fine_cap,
                    coarse_cap,
                )
        except TimeoutError:
            # Inner kill (the driver SIGKILLs a child past analysis_inner_timeout_sec): the file
            # is deterministically too long. TERMINAL -- report and return NORMALLY so SAQ marks
            # the job COMPLETE (no blind re-run of a >timeout file; T-43-08). RESEARCH §Q5.
            await api.report_analysis_failed(payload.file_id, AnalysisFailurePayload(reason="timeout"))
            return {"file_id": str(payload.file_id), "status": "analysis_failed"}
        except AnalysisSubprocessError as exc:
            # essentia OOM/segfault/raise crashed the child (nonzero exit). Also deterministic ->
            # TERMINAL the same way (the ProcessExpired mapping, preserved). The child's terminal
            # error line rides along as detail so the durable failure marker names the actual
            # cause -- e.g. phaze-zibn's AnalysisDecodeError (every window failed to decode)
            # is distinguishable from an essentia segfault without re-running anything.
            await api.report_analysis_failed(payload.file_id, AnalysisFailurePayload(reason="crashed", error=str(exc)[:_ERROR_DETAIL_MAX]))
            return {"file_id": str(payload.file_id), "status": "analysis_failed"}

        features = analysis.get("features", {}) if isinstance(analysis, dict) else {}
        mood_dict = _features_to_mood_dict(features) if isinstance(features, dict) else None
        style_dict = _features_to_style_dict(features) if isinstance(features, dict) else None

        # Phase 31 ANL-01: forward the per-window time-series. ``analyze_file`` returns
        # ``windows`` as plain dicts (Plan 04), so we build AnalysisWindowPayload from each
        # dict directly -- NO ORM/database import (D-25 import boundary; tests/test_task_split.py).
        windows = [AnalysisWindowPayload(**w) for w in analysis.get("windows", [])] if isinstance(analysis, dict) else []

        # PUT result via HTTP (D-26 idempotent upsert; CR-01 partial-PUT semantics preserved by exclude_unset)
        await api.put_analysis(
            payload.file_id,
            AnalysisWritePayload(
                bpm=analysis.get("bpm"),
                musical_key=analysis.get("musical_key"),
                mood=mood_dict,
                style=style_dict,
                danceability=analysis.get("danceability"),
                energy=analysis.get("energy"),
                # Phase 43 windowed-analysis coverage (the five-field contract analyze_file emits).
                # Absent keys stay None so the partial-PUT contract preserves unset coverage.
                fine_windows_analyzed=analysis.get("fine_windows_analyzed"),
                fine_windows_total=analysis.get("fine_windows_total"),
                coarse_windows_analyzed=analysis.get("coarse_windows_analyzed"),
                coarse_windows_total=analysis.get("coarse_windows_total"),
                sampled=analysis.get("sampled"),
                windows=windows,
            ),
        )
        return {"file_id": str(payload.file_id), "status": "analyzed"}
    except Exception as exc:
        # Generic / possibly-transient error from the analysis pool OR the put_analysis callback
        # (the latter sits OUTSIDE the inner pool try, so it MUST be handled here too -- a put_analysis
        # 5xx was the second CR-01 trap). Report ONLY on the terminal attempt (so SAQ has already
        # exhausted retries). On a retryable attempt KEEP the scratch copy so the one real retry
        # (retries=2) can re-verify and analyze it, then re-raise so SAQ records the failed attempt.
        job = ctx.get("job")
        if job is not None and not job.retryable:
            await api.report_analysis_failed(
                payload.file_id,
                AnalysisFailurePayload(reason="error", error=str(exc)[:_ERROR_DETAIL_MAX]),
            )
        else:
            # Retryable: do NOT delete the pushed scratch copy -- the in-place SAQ retry needs it
            # (CR-01). The push_file task is NOT re-run, so a deleted copy can never be recovered.
            cleanup_scratch = False
        raise
    finally:
        # CLOUDPIPE-04: bound scratch-dir disk to the in-flight set -- delete on every TERMINAL exit
        # path (success, timeout, crash, mismatch early-return, non-retryable failure). ``missing_ok``
        # absorbs the mismatch branch's explicit unlink and any local-file (no-scratch) job. A
        # retryable failure leaves ``cleanup_scratch`` False so the copy survives for the retry
        # (T-50-scratch-dos still holds: a terminal failure always reclaims the disk).
        if payload.scratch_path and cleanup_scratch:
            Path(payload.scratch_path).unlink(missing_ok=True)
