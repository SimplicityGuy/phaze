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
from dataclasses import dataclass
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
) -> object:
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

    Returns the analysis child's decoded JSON value. The child normally emits the
    ``analyze_file`` result dict, but the subprocess boundary can yield another JSON
    shape; callers preserve that completed run as an empty partial result. Raises
    ``TimeoutError`` — specifically
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


async def _verify_scratch_integrity(api: PhazeAgentClient, payload: ProcessFilePayload) -> dict[str, Any] | None:
    """CLOUDPIPE-03: verify a pushed scratch copy's bytes before trusting them.

    sha256 is computed OFF the event loop (chunked stdlib hash; the scan.py pattern). A
    mismatch means a corrupt/partial transfer -> delete it, report so the control plane
    re-pushes (50-05 caps attempts), and DO NOT analyze (T-50-corrupt). Gated on both
    fields being present so the bulk local producer (neither set) takes none of this
    branch.

    Returns the ``push_mismatch`` response dict when ``process_file`` should return
    immediately, or ``None`` to continue on to analysis.
    """
    if not payload.scratch_path:
        return None
    if not payload.expected_sha256:
        # IN-01: scratch copy present but the control plane did not pin an expected sha256.
        # The control plane ALWAYS pins both (report_pushed reads the non-null sha256_hash), so
        # this is only reachable via a malformed payload. Analyze anyway (the documented skip
        # behavior) but WARN -- an unverified pushed copy is a defense-in-depth gap.
        logger.warning(
            "process_file: analyzing an unverified scratch copy (no expected_sha256 pinned)",
            file_id=str(payload.file_id),
        )
        return None

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
        await api.report_push_mismatch(payload.file_id)
        return {"file_id": str(payload.file_id), "status": "push_mismatch"}

    if actual_sha256 != payload.expected_sha256:
        Path(payload.scratch_path).unlink(missing_ok=True)
        await api.report_push_mismatch(payload.file_id)
        return {"file_id": str(payload.file_id), "status": "push_mismatch"}

    return None


@dataclass
class _ExtractionOutcome:
    """Result of :func:`_extract_and_analyze`.

    ``terminal_response`` is set when a terminal extraction/analysis error was already
    reported via ``_report_terminal_failure`` and ``process_file`` should return it
    immediately. Otherwise ``analysis`` contains the decoded child result.
    """

    analysis: object | None = None
    terminal_response: dict[str, Any] | None = None


async def _extract_and_analyze(
    api: PhazeAgentClient,
    cfg: AgentSettings,
    ctx: dict[str, Any],
    payload: ProcessFilePayload,
    read_path: str,
    scratch_state: dict[str, str | None],
) -> _ExtractionOutcome:
    """Extract the audio track, then run analysis, mapping terminal errors to a response.

    phaze-3ea41 (operator decision, format scope): pre-analysis audio-track extraction is
    offered EVERY file (probed, then extracted only if it is not already plain audio), not
    just recognized video extensions -- see services/video_audio.py's decision record.
    ffprobe is the sole authority on whether read_path has an audio stream at all. Runs
    INSIDE the concurrency semaphore, same as the analysis it feeds, so a burst of files
    cannot spawn unbounded concurrent ffmpeg extractions alongside the essentia-bounded pool.

    phaze-l832u: ``extract_audio_track`` returns an ``AudioSource``, NOT a path -- its
    ``cleanup_path`` is ``None`` whenever nothing was created (the already-plain-audio skip),
    which is what keeps the caller's unconditional unlink off ``read_path``: on that lane,
    with no pushed copy, ``read_path`` IS the operator's archive original.

    ``scratch_state["extracted_audio_path"]`` is written the MOMENT extraction produces a
    cleanup path, before the analysis call that can fail -- an out-parameter rather than a
    field on the return value, because a caller's ``finally`` must be able to find and remove
    that scratch file even when this function raises something NONE of the four handlers
    below catch (the original inline code got this for free from one shared function scope;
    the caught-terminal-error cases below additionally mirror it onto the return value's
    ``terminal_response`` path for symmetry, but ``scratch_state`` is the path that also
    covers an unrecognized exception propagating straight out).

    The four exception handlers mirror the original inline try/except: each of
    NoAudioTrackError, AudioExtractionError, TimeoutError (stall kill) and
    AnalysisSubprocessError (child crash) is deterministic for the same input bytes, so
    T-43-08 treats every one as TERMINAL -- report once, no blind SAQ re-run. Any OTHER
    exception is left to propagate to the caller's generic handler unchanged.
    """
    semaphore: asyncio.Semaphore | None = ctx.get("analysis_semaphore")
    job = ctx.get("job")

    async def _extraction_heartbeat() -> None:
        # Extraction runs BEFORE run_analysis_subprocess spawns the analysis child, so the
        # driver's inner stall watchdog isn't armed yet -- only the SAQ job's OWN outer
        # heartbeat deadline (analysis_job_heartbeat_sec) can expire during a long
        # extraction, and this keeps it touched exactly like _run_analysis_with_progress's
        # _heartbeat does for the analysis phase.
        await _touch_job_heartbeat(job)

    try:
        async with semaphore if semaphore is not None else contextlib.nullcontext():
            audio_source = await extract_audio_track(
                read_path,
                file_id=str(payload.file_id),
                # phaze-3ea41 (review correction): thread the agent's configured scratch dir --
                # a directory provisioned for large landed files (the SAME one push_file rsyncs
                # pushed containers into) -- rather than leaving the extracted-audio intermediate
                # to fall back to bare /tmp, which is often a small tmpfs unfit for a
                # multi-hour set's audio track. None (unset on a pure local-only agent) still
                # falls back to tempfile.gettempdir() inside extract_audio_track itself.
                scratch_dir=cfg.cloud_scratch_dir,
                heartbeat_cb=_extraction_heartbeat if job is not None else None,
                heartbeat_interval_sec=cfg.analysis_job_heartbeat_sec / _HEARTBEAT_TOUCHES_PER_DEADLINE,
            )
            # Register the scratch file for cleanup BEFORE the analysis that can fail.
            scratch_state["extracted_audio_path"] = audio_source.cleanup_path
            analysis = await _run_analysis_with_progress(
                api,
                cfg,
                payload.file_id,
                audio_source.analysis_path,
                payload.models_path,
                job=job,
            )
        return _ExtractionOutcome(analysis=analysis)
    except NoAudioTrackError as exc:
        # The container has no audio stream at all -- deterministic and TERMINAL (retrying
        # ffprobe against the same bytes reports the same absence every time).
        await _report_terminal_failure(api, payload.file_id, AnalysisFailurePayload(reason="error", error=str(exc)[:_ERROR_DETAIL_MAX]))
        return _ExtractionOutcome(terminal_response={"file_id": str(payload.file_id), "status": "analysis_failed"})
    except AudioExtractionError as exc:
        # ffprobe/ffmpeg failed for a reason OTHER than "no audio track" -- dominantly a
        # corrupt/truncated container, just as deterministic as an essentia child crash.
        await _report_terminal_failure(api, payload.file_id, AnalysisFailurePayload(reason="error", error=str(exc)[:_ERROR_DETAIL_MAX]))
        return _ExtractionOutcome(terminal_response={"file_id": str(payload.file_id), "status": "analysis_failed"})
    except TimeoutError as exc:
        # STALL kill (phaze-w55w1): the driver killed a child that reported no progress for
        # analysis_stall_timeout_sec. RESEARCH §Q5: reason stays "timeout" -- the stored
        # vocabulary and every consumer of it are unchanged -- while error carries the stall
        # detail, so the durable marker says "stopped making progress", not merely "ran too long".
        await _report_terminal_failure(api, payload.file_id, AnalysisFailurePayload(reason="timeout", error=str(exc)[:_ERROR_DETAIL_MAX]))
        return _ExtractionOutcome(terminal_response={"file_id": str(payload.file_id), "status": "analysis_failed"})
    except AnalysisSubprocessError as exc:
        # essentia OOM/segfault/raise crashed the child (nonzero exit); the child's terminal
        # error line rides along as detail so the durable failure marker names the actual
        # cause -- e.g. phaze-zibn's AnalysisDecodeError is distinguishable from an essentia
        # segfault without re-running anything.
        await _report_terminal_failure(api, payload.file_id, AnalysisFailurePayload(reason="crashed", error=str(exc)[:_ERROR_DETAIL_MAX]))
        return _ExtractionOutcome(terminal_response={"file_id": str(payload.file_id), "status": "analysis_failed"})


def _analysis_reports_zero_natural_windows(fine_total: int | None, coarse_total: int | None) -> bool:
    """True only when BOTH coverage fields are EXPLICITLY present and zero (phaze-by30).

    Mirrors job_runner's zero-natural-window floor. phaze-zibn's guard in analyze_file
    (services/analysis.py) only fires when >=1 natural window existed (``fine_total > 0 or
    coarse_total > 0``); it is a no-op when the duration probe itself reads 0 seconds --
    e.g. a truncated download whose readable ID3 header nonetheless yields zero-length audio
    properties. Absence of either field (older/mocked analyzers) means "unknown", not "zero",
    and must keep falling through to the normal partial-PUT path (see
    test_process_file_coverage_fields_default_none_when_absent) -- hence the ``is None``
    escape hatch below rather than treating a missing field as zero.
    """
    if fine_total is None or coarse_total is None:
        return False
    return (fine_total or 0) == 0 and (coarse_total or 0) == 0


def _build_analysis_write_payload(analysis: object) -> AnalysisWritePayload:
    """Build the wire payload from the ``analyze_file`` result dict (D-26, CR-01).

    ``mood``/``style`` are rebuilt from ``analysis["features"]`` (see module docstring);
    ``windows`` is built from the plain per-window dicts (Phase 31 ANL-01) -- NO ORM/database
    import (D-25 import boundary; tests/test_task_split.py). ``exclude_unset`` on the
    resulting model preserves partial-PUT semantics for absent keys (phaze-w55w1 dropped the
    fifth window-count field, ``sampled``, with the window caps).
    """
    # Keep the guards: this is a subprocess JSON boundary, so a successfully decoded but
    # non-object value still represents a completed analysis run. Preserve it as an empty
    # partial PUT instead of raising AttributeError and causing the expensive work to retry.
    result = analysis if isinstance(analysis, dict) else {}
    features = result.get("features", {})
    mood_dict = _features_to_mood_dict(features) if isinstance(features, dict) else None
    style_dict = _features_to_style_dict(features) if isinstance(features, dict) else None
    windows = [AnalysisWindowPayload(**w) for w in result.get("windows", [])]
    return AnalysisWritePayload(
        bpm=result.get("bpm"),
        musical_key=result.get("musical_key"),
        mood=mood_dict,
        style=style_dict,
        danceability=result.get("danceability"),
        energy=result.get("energy"),
        fine_windows_analyzed=result.get("fine_windows_analyzed"),
        fine_windows_total=result.get("fine_windows_total"),
        coarse_windows_analyzed=result.get("coarse_windows_analyzed"),
        coarse_windows_total=result.get("coarse_windows_total"),
        windows=windows,
    )


def _job_should_preserve_scratch_on_cancel(job: Any) -> bool:
    """True when a CANCELLED job is genuinely coming back for a SAQ retry (phaze-2cqx).

    SAQ cancellation (job-net timeout OR -- the routine case, since the agent worker is
    started without shutdown_grace_period_s and saq defaults that to a ZERO-second grace --
    every worker shutdown/restart) raises CancelledError, a BaseException the generic
    ``except Exception`` handler never sees. A job already ``ABORTING`` (Worker.abort()) is
    terminal with no retry, so only a retryable, non-aborting job should keep its pushed
    scratch copy alive for the retry to re-verify and analyze (CR-01); otherwise cleanup
    deletes the copy out from under a retry it turns out is not coming, or leaks scratch disk
    for one that will never use it (T-50-scratch-dos).
    """
    return job is not None and job.retryable and job.status is not Status.ABORTING


def _job_is_terminal_attempt(job: Any) -> bool:
    """True when this is the LAST SAQ attempt for the job (phaze-ys4d, WR-01).

    Report the terminal failure ONLY on the terminal attempt, so SAQ has already exhausted
    retries and the durable failure marker reflects a real, final outcome rather than one
    more re-run in flight. A retryable attempt must NOT report here -- it also keeps the
    pushed scratch copy (CR-01): the push_file task is not re-run, so a deleted copy can
    never be recovered for the in-place retry.
    """
    return job is not None and not job.retryable


def _cleanup_process_file_scratch(payload: ProcessFilePayload, *, cleanup_scratch: bool, extracted_audio_path: str | None) -> None:
    """Remove ``process_file``'s scratch files on the way out.

    CLOUDPIPE-04: bound scratch-dir disk to the in-flight set -- delete the pushed scratch
    copy on every TERMINAL exit path (success, timeout, crash, mismatch early-return,
    non-retryable failure); ``missing_ok`` absorbs the mismatch branch's explicit unlink and
    any local-file (no-scratch) job. A retryable failure leaves ``cleanup_scratch`` False so
    the copy survives for the retry (T-50-scratch-dos still holds: a terminal failure always
    reclaims the disk).

    phaze-3ea41: the video-audio extraction scratch file, if one was produced, is removed on
    EVERY exit path regardless of ``cleanup_scratch`` -- success, timeout, crash,
    no-audio-track, or a retryable failure (unlike the pushed scratch copy above,
    re-extracting from the still-present source is a cheap local operation, so there is no
    retry case worth preserving it for).
    """
    if payload.scratch_path and cleanup_scratch:
        Path(payload.scratch_path).unlink(missing_ok=True)
    if extracted_audio_path is not None:
        Path(extracted_audio_path).unlink(missing_ok=True)


async def _analyze_and_publish(
    api: PhazeAgentClient,
    cfg: AgentSettings,
    ctx: dict[str, Any],
    payload: ProcessFilePayload,
    read_path: str,
    scratch_state: dict[str, str | None],
) -> dict[str, Any]:
    """The analyze -> validate -> PUT sequence, i.e. everything ``process_file``'s try block guards.

    Split out of ``process_file`` (phaze-vu88k.7) with its steps in the same order and its early
    returns intact: this is the happy path only. Every exception it raises -- ``CancelledError``
    included -- propagates to ``process_file``, which owns the retry classification, the
    ``cleanup_scratch`` decision and the terminal-failure ack. Nothing about the SAQ job's timeout,
    heartbeat or retry semantics is visible from here, and none of it changed.
    """
    # CLOUDPIPE-03 integrity gate; see _verify_scratch_integrity's docstring. A push-mismatch
    # short-circuit returns immediately; ``None`` means the bytes are trusted (or there is no
    # pushed copy at all) and analysis proceeds.
    early_return = await _verify_scratch_integrity(api, payload)
    if early_return is not None:
        return early_return

    # Phase 101 (OBS-03): run extraction + analysis in the exec'd child via the shared driver,
    # with the parent-side throttled progress bridge posting
    # ctx["api_client"].post_analysis_progress mid-analysis (best-effort). See
    # _extract_and_analyze's docstring for the concurrency-semaphore and terminal-error mapping.
    outcome = await _extract_and_analyze(api, cfg, ctx, payload, read_path, scratch_state)
    if outcome.terminal_response is not None:
        return outcome.terminal_response
    analysis = outcome.analysis

    # phaze-by30: see _analysis_reports_zero_natural_windows's docstring. That leaves
    # analyze_file free to return a false "success" (windows=[], all-None aggregates) which
    # the completion PUT below would otherwise stamp as ``analysis_completed_at`` forever.
    fine_total = analysis.get("fine_windows_total") if isinstance(analysis, dict) else None
    coarse_total = analysis.get("coarse_windows_total") if isinstance(analysis, dict) else None
    if _analysis_reports_zero_natural_windows(fine_total, coarse_total):
        await _report_terminal_failure(
            api,
            payload.file_id,
            AnalysisFailurePayload(reason="crashed", error="zero natural analysis windows (undecodable or zero-length audio)"),
        )
        return {"file_id": str(payload.file_id), "status": "analysis_failed"}

    # PUT result via HTTP (D-26 idempotent upsert; CR-01 partial-PUT semantics preserved by exclude_unset)
    await api.put_analysis(payload.file_id, _build_analysis_write_payload(analysis))
    return {"file_id": str(payload.file_id), "status": "analyzed"}


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
    # phaze-l832u: ``scratch_state["extracted_audio_path"]`` holds ``AudioSource.cleanup_path``
    # and NOTHING ELSE. It stays None when extraction was skipped (the file was already plain
    # audio), because the finally below unlinks it unconditionally and the file that would
    # otherwise be sitting here is the operator's archive original -- ``read_path`` is the
    # real file on the local lane, not a staged copy. It is written by _extract_and_analyze the
    # MOMENT extraction produces a scratch file, so the finally below can find and remove it
    # however the try block exits -- success, a caught terminal error, OR an exception
    # _extract_and_analyze does not itself catch (see that function's docstring for why this
    # is an out-parameter rather than a field read only off its return value).
    scratch_state: dict[str, str | None] = {"extracted_audio_path": None}
    try:
        return await _analyze_and_publish(api, cfg, ctx, payload, read_path, scratch_state)
    except asyncio.CancelledError:
        # See _job_should_preserve_scratch_on_cancel's docstring (phaze-2cqx). ``ctx["job"]`` is
        # always present under a real worker; absent only in a bare test ctx, where the
        # default-True cleanup is correct (nothing will retry it).
        if _job_should_preserve_scratch_on_cancel(ctx.get("job")):
            cleanup_scratch = False
        raise
    except Exception as exc:
        # Generic / possibly-transient error from the analysis pool OR the put_analysis callback
        # (the latter sits OUTSIDE the inner pool try, so it MUST be handled here too -- a put_analysis
        # 5xx was the second CR-01 trap). See _job_is_terminal_attempt's docstring (phaze-ys4d, WR-01).
        if _job_is_terminal_attempt(ctx.get("job")):
            # An unguarded ack POST failure (E2) would propagate INSTEAD of the bare `raise` below,
            # so SAQ's recorded traceback leads with the ack's own error rather than the real
            # analysis failure (exc, E1) this handler exists to report. _report_terminal_failure
            # already swallows + logs E2.
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
        # See _cleanup_process_file_scratch's docstring for what gets removed and why.
        _cleanup_process_file_scratch(payload, cleanup_scratch=cleanup_scratch, extracted_audio_path=scratch_state["extracted_audio_path"])
