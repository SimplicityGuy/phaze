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

from saq import Status
import structlog

from phaze.config import AgentSettings, get_settings
from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.schemas.agent_analysis import AnalysisFailurePayload, AnalysisProgressPayload, AnalysisWindowPayload, AnalysisWritePayload
from phaze.schemas.agent_tasks import ProcessFilePayload
from phaze.services.analysis_exec import AnalysisSubprocessError, run_analysis_subprocess
from phaze.services.analysis_wire import _features_to_mood_dict, _features_to_style_dict
from phaze.services.hashing import compute_sha256
from phaze.services.video_audio import AudioExtractionError, NoAudioTrackError, extract_audio_track


if TYPE_CHECKING:
    import uuid

    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)


# Phase 43 (T-43-09): cap the worker-side exception text before it crosses the HTTP
# boundary. The control-side `AnalysisFailurePayload.error` is the authoritative bound
# (max_length=2000); truncating here avoids shipping a huge traceback string at all.
_ERROR_DETAIL_MAX = 2000

# How many SAQ liveness touches must fit inside one job-heartbeat deadline (phaze-w55w1). 3 is
# the smallest value that tolerates a dropped touch: `_touch_job_heartbeat` swallows broker
# errors by design, so a cadence of exactly one-per-deadline would let a single transient
# failure sweep a healthy multi-hour analysis.
_HEARTBEAT_TOUCHES_PER_DEADLINE = 3


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


async def _touch_job_heartbeat(job: Any) -> None:
    """Best-effort SAQ heartbeat touch (phaze-w55w1).

    ``process_file`` runs ``timeout=0`` with a ``heartbeat`` instead (see
    ``services/analysis_enqueue.py``), so ``Job.stuck`` — and therefore SAQ's sweep and
    ``classify_process_file_collision`` — reads ``touched``. This is what keeps ``touched``
    fresh while a legitimately multi-hour analysis runs. Swallows every error for the same
    reason the progress POST does: a broker hiccup must not fail an analysis that is working.
    """
    try:
        await job.update()
    except Exception:  # best-effort liveness; never fail the job
        logger.debug("process_file: SAQ heartbeat touch dropped (best-effort)")


async def _run_analysis_with_progress(
    api: PhazeAgentClient,
    cfg: AgentSettings,
    file_id: uuid.UUID,
    read_path: str,
    models_path: str,
    job: Any = None,
) -> Any:
    """Run exhaustive analysis in the child subprocess while relaying throttled progress.

    Phase 101: the shared driver (``run_analysis_subprocess``) execs the analysis child
    and invokes ``_progress`` ON the event loop per fine window — the Manager-queue
    drainer this replaced is gone. Throttling stays parent-side and keeps the drainer's
    semantics: the FIRST emission always posts (``last_post`` starts ``None`` — a ``0.0``
    baseline would throttle away the START on a freshly-booted host), later emissions
    post at most every ``interval_sec``, and the last seen count is flushed on the way
    out even when the throttle swallowed it (D-04 final flush) — belt-and-suspenders
    with the completion PUT.

    phaze-w55w1 adds the LIVENESS relay alongside it. The driver's ``heartbeat_cb`` fires on
    every unit of analysis progress (both tiers, chunk decodes, model sweeps) and this bridge
    turns it into ``job.update()`` — the SAQ-side touch that keeps ``Job.stuck`` false while
    the analysis is genuinely working. It rides the SAME throttle gate and the same
    fire-and-forget task set as the progress POST, so a chatty long file cannot turn liveness
    into a write storm on the broker.

    Returns the ``analyze_file`` result dict. Raises ``TimeoutError`` — specifically
    :class:`AnalysisStalledError`, when the driver kills a child that stopped reporting
    progress for ``analysis_stall_timeout_sec`` — and :class:`AnalysisSubprocessError`
    (child crash/nonzero exit — the ``ProcessExpired`` replacement) for ``process_file``'s
    terminal handlers; the progress bridge itself never alters the terminal mapping.
    """
    interval_sec = cfg.analysis_progress_interval_sec
    # The SAQ liveness touch gets its OWN cadence, deliberately NOT the UI progress throttle
    # (phaze-w55w1). `analysis_progress_interval_sec` is a DISPLAY knob -- an operator raising it
    # to quieten the progress bar has no reason to expect it to affect broker liveness, but under
    # a shared throttle it would: set it above the job's heartbeat deadline and every healthy
    # analysis stops touching in time and gets swept. Coupling a correctness bound to a cosmetic
    # knob is a trap regardless of the default, so the touch cadence is capped at a third of the
    # deadline (>=3 touches per window, so two may be dropped -- the touch is best-effort -- and
    # the job still stays live) and only tightened, never loosened, by the display knob.
    touch_interval_sec = cfg.analysis_job_heartbeat_sec / _HEARTBEAT_TOUCHES_PER_DEADLINE
    if interval_sec > 0.0:
        touch_interval_sec = min(interval_sec, touch_interval_sec)
    last_post: float | None = None
    last_count: tuple[int, int] | None = None
    last_posted: tuple[int, int] | None = None
    last_touch: float | None = None
    pending: set[asyncio.Task[None]] = set()

    def _spawn(coro: Any) -> None:
        # Fire-and-forget loop task (we're ON the loop); strong-ref'd so it is never GC'd
        # mid-flight. Each coroutine swallows its own errors (best-effort, D-16).
        task = asyncio.get_running_loop().create_task(coro)
        pending.add(task)
        task.add_done_callback(pending.discard)

    def _progress(analyzed: int, total: int) -> None:
        nonlocal last_post, last_count, last_posted
        last_count = (analyzed, total)
        now = time.monotonic()
        if interval_sec > 0.0 and last_post is not None and (now - last_post) < interval_sec:
            return
        last_post = now
        last_posted = (analyzed, total)
        _spawn(_post_progress_count(api, file_id, (analyzed, total)))

    def _heartbeat(_stage: str, _done: int, _total: int) -> None:
        nonlocal last_touch
        if job is None:
            return  # no SAQ job in this context (direct call / test harness): nothing to touch
        now = time.monotonic()
        if last_touch is not None and (now - last_touch) < touch_interval_sec:
            return
        last_touch = now
        _spawn(_touch_job_heartbeat(job))

    try:
        return await run_analysis_subprocess(
            read_path,
            models_path,
            progress_cb=_progress,
            heartbeat_cb=_heartbeat,
            stall_timeout=cfg.analysis_stall_timeout_sec,
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


async def _report_terminal_failure(api: PhazeAgentClient, file_id: uuid.UUID, failure: AnalysisFailurePayload) -> None:
    """Deliver a TERMINAL failure report without letting delivery failure escape (phaze-x3dg).

    The inner-timeout and child-crash outcomes are terminal by design (T-43-08: no blind
    re-run of a >timeout file), but the report POST used to run unprotected inside the
    outer try: a transient control-plane outage during the POST escaped into the generic
    ``except Exception`` handler, converting a terminal outcome into a full SAQ retry of a
    deterministically-doomed multi-hour analysis (and reporting ``reason="error"`` on the
    final attempt, corrupting the timeout/crashed distinction). On delivery failure we log
    and move on — the caller still returns the terminal status dict, and queue-loss
    reconcile/Recover delivers the file's state later.
    """
    try:
        await api.report_analysis_failed(file_id, failure)
    except Exception:
        logger.warning(
            "process_file: terminal failure report POST failed — keeping the terminal outcome "
            "(no re-analysis); reconcile/recovery will deliver the state later",
            file_id=str(file_id),
            reason=failure.reason,
        )


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
    # phaze-w55w1: the child is bounded by SILENCE, not by elapsed time -- the driver kills it
    # only after settings.analysis_stall_timeout_sec with no reported progress, so an exhaustive
    # multi-hour analysis runs to completion (ADR-0007 §7). Threaded from settings here so config
    # drives it. There is no window cap left to thread: every file gets every window.
    cfg = _agent_settings()

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
    # phaze-3ea41: the video-audio extraction scratch file. Unlike ``payload.scratch_path`` --
    # preserved across a retryable failure because re-fetching it means a network re-push --
    # this is ALWAYS deleted in the outer ``finally`` regardless of retry: re-extracting from
    # the still-present source (``read_path``) is a cheap local operation, so there is no reason
    # to keep it around, and always deleting bounds extraction scratch disk to one file's audio
    # track at a time no matter how many attempts a job takes.
    #
    # phaze-l832u: this holds ``AudioSource.cleanup_path`` and NOTHING ELSE. It stays None when
    # extraction was skipped (the file was already plain audio), because the finally below
    # unlinks it unconditionally and the file that would otherwise be sitting here is the
    # operator's archive original -- ``read_path`` is the real file on the local lane, not a
    # staged copy. Assigning the analyzer's read path here would delete the archive.
    extracted_audio_path: str | None = None
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
                # phaze-3ea41 (operator decision, format scope): pre-analysis audio-track
                # extraction is offered EVERY file (probed, then extracted only if it is not
                # already plain audio), not just recognized video extensions -- see
                # services/video_audio.py's decision record. ffprobe is the sole authority on
                # whether read_path has an audio stream at all; there is no payload.file_type
                # gate here anymore. Runs INSIDE the concurrency semaphore, same as the analysis
                # it feeds, so a burst of files cannot spawn unbounded concurrent ffmpeg
                # extractions alongside the essentia-bounded pool.
                #
                # phaze-l832u: the call returns an AudioSource, NOT a path, and the two fields
                # are not interchangeable. ``cleanup_path`` is None whenever nothing was created
                # (the already-plain-audio skip), which is what keeps the outer finally's
                # unconditional unlink off ``read_path`` -- on this lane, with no pushed copy,
                # read_path IS the operator's archive original. Never assign
                # ``extracted_audio_path`` from ``analysis_path``.
                job = ctx.get("job")

                async def _extraction_heartbeat() -> None:
                    # Extraction runs BEFORE run_analysis_subprocess spawns the analysis
                    # child, so the driver's inner stall watchdog isn't armed yet -- only the
                    # SAQ job's OWN outer heartbeat deadline (analysis_job_heartbeat_sec) can
                    # expire during a long extraction, and this keeps it touched exactly like
                    # _run_analysis_with_progress's _heartbeat does for the analysis phase.
                    await _touch_job_heartbeat(job)

                audio_source = await extract_audio_track(
                    read_path,
                    file_id=str(payload.file_id),
                    # phaze-3ea41 (review correction): thread the agent's configured scratch
                    # dir -- a directory provisioned for large landed files (the SAME one
                    # push_file rsyncs pushed containers into) -- rather than leaving the
                    # extracted-audio intermediate to fall back to bare /tmp, which is often a
                    # small tmpfs unfit for a multi-hour set's audio track. None (unset on a
                    # pure local-only agent) still falls back to tempfile.gettempdir() inside
                    # extract_audio_track itself.
                    scratch_dir=cfg.cloud_scratch_dir,
                    heartbeat_cb=_extraction_heartbeat if job is not None else None,
                    heartbeat_interval_sec=cfg.analysis_job_heartbeat_sec / _HEARTBEAT_TOUCHES_PER_DEADLINE,
                )
                # Register the scratch file for cleanup BEFORE the analysis that can fail (and
                # ``None`` when there is no scratch file at all -- the skip branch).
                extracted_audio_path = audio_source.cleanup_path
                analysis = await _run_analysis_with_progress(
                    api,
                    cfg,
                    payload.file_id,
                    audio_source.analysis_path,
                    payload.models_path,
                    job=ctx.get("job"),
                )
        except NoAudioTrackError as exc:
            # phaze-3ea41: the container has no audio stream at all -- deterministic and
            # TERMINAL (retrying ffprobe against the same bytes reports the same absence every
            # time), so this reports immediately and does NOT fall into the generic retry path
            # below, mirroring the TimeoutError/AnalysisSubprocessError "no blind re-run" handling.
            await _report_terminal_failure(api, payload.file_id, AnalysisFailurePayload(reason="error", error=str(exc)[:_ERROR_DETAIL_MAX]))
            return {"file_id": str(payload.file_id), "status": "analysis_failed"}
        except AudioExtractionError as exc:
            # phaze-3ea41 (review correction): ffprobe/ffmpeg failed for a reason OTHER than
            # "no audio track" -- dominantly a corrupt/truncated container, which is just as
            # deterministic as an essentia child crash (AnalysisSubprocessError below): the
            # SAME bytes reproduce the SAME failure on retry. TERMINAL immediately (T-43-08:
            # no blind re-run of a deterministically-doomed file), NOT the generic
            # retryable-aware handler this used to fall through to.
            await _report_terminal_failure(api, payload.file_id, AnalysisFailurePayload(reason="error", error=str(exc)[:_ERROR_DETAIL_MAX]))
            return {"file_id": str(payload.file_id), "status": "analysis_failed"}
        except TimeoutError as exc:
            # STALL kill (phaze-w55w1): the driver killed a child that reported no progress for
            # analysis_stall_timeout_sec. A file that keeps producing windows is never killed here
            # however long it runs, so reaching this branch means the child was genuinely wedged,
            # which is deterministic. TERMINAL -- report and return NORMALLY so SAQ marks the job
            # COMPLETE (no blind re-run; T-43-08). RESEARCH §Q5. The report is delivery-guarded
            # (phaze-x3dg): a failed POST must not escape into the generic retry path and re-run
            # the doomed analysis. ``reason`` stays "timeout" -- the stored vocabulary and every
            # consumer of it are unchanged -- while ``error`` carries the stall detail, so the
            # durable marker says "stopped making progress", not merely "ran too long".
            await _report_terminal_failure(api, payload.file_id, AnalysisFailurePayload(reason="timeout", error=str(exc)[:_ERROR_DETAIL_MAX]))
            return {"file_id": str(payload.file_id), "status": "analysis_failed"}
        except AnalysisSubprocessError as exc:
            # essentia OOM/segfault/raise crashed the child (nonzero exit). Also deterministic ->
            # TERMINAL the same way (the ProcessExpired mapping, preserved). The child's terminal
            # error line rides along as detail so the durable failure marker names the actual
            # cause -- e.g. phaze-zibn's AnalysisDecodeError (every window failed to decode)
            # is distinguishable from an essentia segfault without re-running anything.
            await _report_terminal_failure(api, payload.file_id, AnalysisFailurePayload(reason="crashed", error=str(exc)[:_ERROR_DETAIL_MAX]))
            return {"file_id": str(payload.file_id), "status": "analysis_failed"}

        # phaze-by30: mirror job_runner's zero-natural-window floor. phaze-zibn's guard in
        # analyze_file (services/analysis.py) only fires when >=1 natural window existed
        # (``fine_total > 0 or coarse_total > 0``); it is a no-op when the duration probe
        # itself reads 0 seconds -- e.g. a truncated download whose readable ID3 header
        # nonetheless yields zero-length audio properties. That leaves analyze_file free to
        # return a false "success" (windows=[], all-None aggregates) which the completion PUT
        # below would otherwise stamp as ``analysis_completed_at`` forever. Only trip this when
        # BOTH coverage fields are EXPLICITLY present and zero -- their absence (older/mocked
        # analyzers) means "unknown", not "zero", and must keep falling through to the normal
        # partial-PUT path (see test_process_file_coverage_fields_default_none_when_absent).
        fine_total = analysis.get("fine_windows_total") if isinstance(analysis, dict) else None
        coarse_total = analysis.get("coarse_windows_total") if isinstance(analysis, dict) else None
        if fine_total is not None and coarse_total is not None and (fine_total or 0) == 0 and (coarse_total or 0) == 0:
            await _report_terminal_failure(
                api,
                payload.file_id,
                AnalysisFailurePayload(reason="crashed", error="zero natural analysis windows (undecodable or zero-length audio)"),
            )
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
                # Windowed-analysis progress counts (the four-field contract analyze_file emits;
                # phaze-w55w1 dropped the fifth, `sampled`, with the window caps). Absent keys stay
                # None so the partial-PUT contract preserves unset counts.
                fine_windows_analyzed=analysis.get("fine_windows_analyzed"),
                fine_windows_total=analysis.get("fine_windows_total"),
                coarse_windows_analyzed=analysis.get("coarse_windows_analyzed"),
                coarse_windows_total=analysis.get("coarse_windows_total"),
                windows=windows,
            ),
        )
        return {"file_id": str(payload.file_id), "status": "analyzed"}
    except asyncio.CancelledError:
        # phaze-2cqx: SAQ cancellation (job-net timeout OR -- the routine case, since the
        # agent worker is started without shutdown_grace_period_s and saq defaults that to a
        # ZERO-second grace -- every worker shutdown/restart) raises CancelledError, a
        # BaseException that the ``except Exception`` below never sees. Left unguarded, the
        # outer ``finally`` still ran with ``cleanup_scratch`` at its default True and deleted
        # the pushed scratch copy out from under a job SAQ is about to retry in place, turning
        # a free retry into a wasted push-mismatch round-trip (CR-01). Preserve the copy only
        # when the job is actually coming back: a job already ``ABORTING`` (Worker.abort()) is
        # terminal with no retry, and preserving there would leak scratch disk (T-50-scratch-dos).
        # ``ctx["job"]`` is always present under a real worker; absent only in a bare test ctx,
        # where the default-True cleanup is correct (nothing will retry it).
        job = ctx.get("job")
        if job is not None and job.retryable and job.status is not Status.ABORTING:
            cleanup_scratch = False
        raise
    except Exception as exc:
        # Generic / possibly-transient error from the analysis pool OR the put_analysis callback
        # (the latter sits OUTSIDE the inner pool try, so it MUST be handled here too -- a put_analysis
        # 5xx was the second CR-01 trap). Report ONLY on the terminal attempt (so SAQ has already
        # exhausted retries). On a retryable attempt KEEP the scratch copy so the one real retry
        # (retries=2) can re-verify and analyze it, then re-raise so SAQ records the failed attempt.
        job = ctx.get("job")
        if job is not None and not job.retryable:
            # phaze-ys4d (WR-01): delivery-guarded, like every sibling terminal ack (scan.py,
            # fingerprint.py, metadata_extraction.py). An unguarded ack POST failure (E2) would
            # propagate INSTEAD of the bare `raise` below, so SAQ's recorded traceback leads with
            # the ack's own error rather than the real analysis failure (exc, E1) this handler
            # exists to report. _report_terminal_failure already swallows + logs E2.
            await _report_terminal_failure(
                api,
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
        # phaze-3ea41: the video-audio extraction scratch file, if one was produced, ALWAYS on
        # every exit path -- success, timeout, crash, no-audio-track, or a retryable failure
        # (unlike the pushed scratch copy above, re-extraction is a cheap local operation, so
        # there is no retry case worth preserving it for; see the variable's own comment above).
        if extracted_audio_path is not None:
            Path(extracted_audio_path).unlink(missing_ok=True)
