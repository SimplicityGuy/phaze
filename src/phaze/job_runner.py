"""One-shot Kueue Job entrypoint (Phase 52, Plan 02 — KJOB-02..KJOB-05).

Fire-once orchestrator for the v6.0 burst flow: it analyzes EXACTLY ONE file and
translates each pipeline step's outcome into a distinct process exit code, then
``sys.exit(code)``. This is the structural divergence from the v5.0 SAQ
``process_file`` task (``phaze.tasks.functions``): that path reports failure via an
HTTP callback and RETURNS a dict so SAQ marks the job COMPLETE; the one-shot pod
must instead exit NON-ZERO so Kueue/Workload reads the failure from pod status
(D-01 / KJOB-04). A failed analysis never exits 0.

Flow: presign -> download -> sha256-verify -> windowed analyze -> callback PUT -> exit.

Exit-code contract (D-01):
    0   success
    10  presign request OR download failure (fail-fast, no retry — D-02)
    11  sha256(downloaded) != expected_sha256 (corrupt/partial transfer)
    12  windowed analysis raised / OOM (fail-fast — D-02). Wall-clock bounding
        is NOT done in-process; it is delegated to the Kueue/Job deadline
        (activeDeadlineSeconds -> SIGTERM -> 143), which is honestly non-zero.
    13  callback PUT failed after the shared bounded retry (D-02)
    20  startup/precondition failure: wrong PHAZE_ROLE, missing PHAZE_JOB_FILE_ID,
        or a malformed file_id UUID. This is a PERMANENT misconfiguration, not a
        transient download failure — kept distinct from 10 so a controller never
        re-drives a Job whose env/role can never change between attempts.

IMPORT-BOUNDARY INVARIANT (inherited from ``phaze.tasks.functions`` / D-25):
    MUST NOT import phaze.database, phaze.tasks.session, or sqlalchemy.ext.asyncio.
    The pod is Postgres-less; its only integrity check is the server-pinned
    ``expected_sha256``. Enforced by tests/test_task_split.py.

The essentia-bound ``analyze_file`` import is deferred to call time (the same seam
``phaze.tasks.functions`` uses) so module load succeeds on hosts without the
platform-gated essentia wheel and stays Postgres-free. SIGTERM is intentionally
NOT trapped into a 0 exit — the default Python SIGTERM->143 is honestly non-zero
so an evicted pod is never mistaken for success (Pitfall 6).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlparse
import uuid

import httpx
import structlog

from phaze.config import AgentSettings, get_settings
from phaze.logging_config import configure_logging
from phaze.schemas.agent_analysis import AnalysisProgressPayload, AnalysisWindowPayload, AnalysisWritePayload
from phaze.services.analysis_wire import _features_to_mood_dict, _features_to_style_dict
from phaze.services.hashing import compute_sha256
from phaze.tasks._shared.agent_bootstrap import construct_agent_client


log = structlog.get_logger(__name__)


# Exit-code contract (D-01). EXIT_OK is the single success exit (the one literal
# zero-code exit below); each failure class maps to a distinct non-zero code so
# Kueue/Workload can read the failure class straight from pod status (KJOB-04).
EXIT_OK = 0
EXIT_DOWNLOAD = 10
EXIT_INTEGRITY = 11
EXIT_ANALYSIS = 12
EXIT_CALLBACK = 13
# Startup/precondition failures (wrong role, missing/malformed file_id) are a
# PERMANENT misconfiguration, not a transient download failure. Kept distinct
# from EXIT_DOWNLOAD (10) so a Kueue/Job controller does not treat them as
# retry-worthy and re-drive a Job that can never succeed (WR-02).
EXIT_CONFIG = 20

_FILE_ID_ENV = "PHAZE_JOB_FILE_ID"
_MODELS_DIR_ENV = "PHAZE_MODELS_DIR"
_DOWNLOAD_CHUNK_BYTES = 1 << 16  # 64 KiB, matches compute_sha256 chunking
_DOWNLOAD_CONNECT_TIMEOUT_S = 30.0
_DOWNLOAD_READ_TIMEOUT_S = 300.0


def _elapsed_ms(start: float) -> int:
    """Whole-millisecond wall-clock delta since ``start`` (``time.monotonic``)."""
    return int((time.monotonic() - start) * 1000)


def _temp_suffix(audio_ext: str | None, url: str) -> str:
    """Pick the downloaded temp file's suffix so essentia can decode it.

    essentia detects the audio format from the FILE EXTENSION (``es.MetadataReader``),
    so the temp file MUST carry the file's real extension. The staged S3 key
    (``phaze-staging/<file_id>``) has no extension, so deriving the suffix from the
    presign URL path yields nothing and the old ``.audio`` fallback produced an
    undecodable file → duration 0 → 0 windows → a silent empty-but-"successful"
    analysis (cloud-analyze-empty-no-ext).

    Prefer the server-threaded ``audio_ext`` (``FileRecord.file_type``, dotless);
    fall back to the URL path suffix (older control plane omits ``audio_ext``); and
    only as a last resort keep the historical ``.audio`` sentinel.
    """
    if audio_ext:
        ext = audio_ext.strip().lstrip(".")
        if ext:
            return f".{ext}"
    url_suffix = Path(urlparse(url).path).suffix
    return url_suffix or ".audio"


def _load_analyze_file() -> Any:
    """Defer the essentia-bound ``analyze_file`` import to call time.

    Mirrors ``phaze.tasks.functions._load_analyze_file``: essentia-tensorflow is
    platform-gated in pyproject.toml, so module load (and the import-boundary
    subprocess test) must not depend on it. Only the analyze step needs it.
    """
    from phaze.services.analysis import analyze_file  # noqa: PLC0415

    return analyze_file


async def _download_to(url: str, dest: Path) -> None:
    """Stream the presigned GET to ``dest`` in 64 KiB chunks.

    Uses a FRESH httpx client with NO Authorization header: the presigned URL is
    self-authenticating, and attaching the internal bearer would leak it to the
    object store (T-52-04). ``verify`` defaults to True (system CAs) for the
    public bucket endpoint — distinct from the internal-CA callback (the CA is
    mounted from a K8s Secret at runtime, KDEPLOY-06).
    """
    timeout = httpx.Timeout(_DOWNLOAD_CONNECT_TIMEOUT_S, read=_DOWNLOAD_READ_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout) as downloader, downloader.stream("GET", url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            async for chunk in resp.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
                fh.write(chunk)


async def _safe_post_progress(client: Any, file_id: uuid.UUID, payload: AnalysisProgressPayload) -> None:
    """Best-effort counter-only progress POST (Phase 57.1, D-16).

    Swallows ANY error (the ``AgentApiError`` hierarchy from the client's single-attempt,
    short-timeout progress path (Phase 99 OBS-01), plus anything unexpected) so a dropped
    progress POST can never change the one-shot exit code — the completion ``put_analysis``
    writes the final count regardless, so the bar reaches 100% from completion. This runs as
    a ``run_coroutine_threadsafe`` task scheduled from the analysis thread.
    """
    try:
        await client.post_analysis_progress(file_id, payload)
    except Exception:  # best-effort: progress never alters the EXIT_ANALYSIS/EXIT_CALLBACK contract
        log.debug("job_runner_progress_dropped", file_id=str(file_id))


def _make_progress_cb(client: Any, file_id: uuid.UUID, loop: asyncio.AbstractEventLoop, interval_sec: float) -> Any:
    """Build the sync ``progress_cb`` that the threaded ``analyze_file`` calls per FINE window.

    The blocking ``analyze_file`` runs in ``asyncio.to_thread`` so the loop stays free; the callback
    fires fire-and-forget ``run_coroutine_threadsafe(_safe_post_progress(...), loop)`` — it NEVER calls
    ``.result()`` (blocking the analysis thread on a saturated loop would deadlock). Throttled to
    ``interval_sec`` (``monotonic()``-keyed); the START count and the final count (``analyzed >= total``)
    always post so the bar gets an early total and a final value even inside the throttle window. Any
    exception is swallowed so a progress failure can never escape the analysis thread (KJOB-04 contract).
    """
    state = {"last_post": 0.0}

    def _cb(analyzed: int, total: int) -> None:
        try:
            now = time.monotonic()
            is_final = total > 0 and analyzed >= total
            if interval_sec > 0.0 and not is_final and (now - state["last_post"]) < interval_sec:
                return
            state["last_post"] = now
            payload = AnalysisProgressPayload(fine_windows_analyzed=analyzed, fine_windows_total=total)
            # Fire-and-forget: schedule onto the captured loop; do NOT call .result() (deadlock risk).
            asyncio.run_coroutine_threadsafe(_safe_post_progress(client, file_id, payload), loop)
        except Exception:  # a progress-cb error must never escape the analysis thread
            log.debug("job_runner_progress_cb_error", file_id=str(file_id))

    return _cb


def _build_payload(result: dict[str, Any]) -> AnalysisWritePayload:
    """Convert an ``analyze_file`` result dict into the callback wire payload.

    Mirrors ``phaze.tasks.functions.process_file`` exactly: rebuild mood/style as
    ``dict[str, float]`` from ``result["features"]`` (D-26) and forward the
    per-window time-series + the five-field coverage contract.
    """
    features = result.get("features", {})
    if not isinstance(features, dict):
        features = {}
    mood_dict = _features_to_mood_dict(features)
    style_dict = _features_to_style_dict(features)
    windows = [AnalysisWindowPayload(**w) for w in (result.get("windows") or [])]
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
        sampled=result.get("sampled"),
        windows=windows,
    )


async def run() -> None:
    """Execute the one-shot flow for a single file, then ``sys.exit(<code>)``.

    Never returns normally — every terminal path raises ``SystemExit`` (via
    ``sys.exit``) so the caller's process exit code carries the outcome.
    """
    cfg = get_settings()
    if not isinstance(cfg, AgentSettings):  # pragma: no cover - pod always runs PHAZE_ROLE=agent
        log.error("job_runner_requires_agent_role", got=type(cfg).__name__)
        sys.exit(EXIT_CONFIG)

    raw_file_id = os.environ.get(_FILE_ID_ENV)
    if not raw_file_id:
        log.error("job_runner_missing_file_id", env=_FILE_ID_ENV)
        sys.exit(EXIT_CONFIG)
    try:
        file_id = uuid.UUID(raw_file_id)
    except ValueError:
        log.error("job_runner_invalid_file_id", value=raw_file_id)
        sys.exit(EXIT_CONFIG)

    models_dir = os.environ.get(_MODELS_DIR_ENV) or cfg.models_path
    fid = str(file_id)

    # KJOB-05 / T-52-01: build the callback client with verify=cfg.agent_ca_file
    # (the internal CA, mounted from a K8s Secret at runtime per KDEPLOY-06 — no
    # longer baked into the image). construct_agent_client raises if the CA is
    # missing or empty; TLS verification is never bypassed.
    client = construct_agent_client(cfg)

    tmp_path: Path | None = None
    try:
        # (1) presign — fail-fast, no extra retry loop (D-02).
        t_presign = time.monotonic()
        try:
            # `_presign_metadata` (Phase 100, phaze-sfbx.1 display-identity block) is unused here --
            # phaze-sfbx.3 threads it into the console banner. Minimal unpacking-only touch to keep
            # this call site compiling against the widened AgentClient.request_download_url return.
            url, expected_sha256, audio_ext, _presign_metadata = await client.request_download_url(file_id)
        except Exception:
            log.exception("job_runner_presign_failed", file_id=fid, step="presign")
            sys.exit(EXIT_DOWNLOAD)
        log.info("job_runner_step_ok", file_id=fid, step="presign", elapsed_ms=_elapsed_ms(t_presign))

        # (2) download — stream to a temp file; bearer never attached (T-52-04).
        # The temp file MUST carry the file's REAL audio extension: essentia detects
        # format by extension, and the staged S3 key has none (cloud-analyze-empty-no-ext).
        suffix = _temp_suffix(audio_ext, url)
        tmp_path = Path(tempfile.gettempdir()) / f"{fid}{suffix}"
        t_download = time.monotonic()
        try:
            await _download_to(url, tmp_path)
        except Exception:
            log.exception("job_runner_download_failed", file_id=fid, step="download")
            sys.exit(EXIT_DOWNLOAD)
        log.info("job_runner_step_ok", file_id=fid, step="download", elapsed_ms=_elapsed_ms(t_download))

        # (3) integrity — the only check a Postgres-free pod can make (KJOB-02);
        # sha256 runs OFF the event loop (chunked stdlib hash).
        t_verify = time.monotonic()
        actual_sha256 = await asyncio.to_thread(compute_sha256, tmp_path)
        # Normalize both sides before comparing. compute_sha256 already returns
        # lowercase hex and the schema pins expected_sha256 to lowercase-hex, so
        # this is defensive against any future case/whitespace skew (IN-02).
        if actual_sha256.strip().lower() != expected_sha256.strip().lower():
            log.error("job_runner_integrity_mismatch", file_id=fid, step="verify")
            sys.exit(EXIT_INTEGRITY)
        log.info("job_runner_step_ok", file_id=fid, step="verify", elapsed_ms=_elapsed_ms(t_verify))

        # (4) analyze — windowed/streaming analyze_file DIRECTLY (no pebble pool,
        # no retry loop — fail-fast, D-02 / KJOB-03). models_dir from env (D-05).
        # There is NO in-process timeout here: a hung analysis is bounded by the
        # Kueue/Job wall-clock deadline (activeDeadlineSeconds -> SIGTERM -> 143),
        # not by an asyncio.wait_for. Only a raised exception maps to EXIT_ANALYSIS
        # (12); the contract docstring above documents this delegation (WR-04).
        analyze_file = _load_analyze_file()
        t_analyze = time.monotonic()
        # Phase 57.1 (PROG-01): capture the loop BEFORE the offload, then run the blocking
        # analyze_file in asyncio.to_thread so the loop stays free for the cb's fire-and-forget
        # run_coroutine_threadsafe progress POSTs. A progress failure is best-effort and NEVER
        # changes the EXIT_ANALYSIS / EXIT_CALLBACK exit-code contract (KJOB-04).
        loop = asyncio.get_running_loop()
        progress_cb = _make_progress_cb(client, file_id, loop, cfg.analysis_progress_interval_sec)
        try:
            result = await asyncio.to_thread(
                analyze_file,
                str(tmp_path),
                models_dir,
                fine_cap=cfg.analysis_fine_cap,
                coarse_cap=cfg.analysis_coarse_cap,
                progress_cb=progress_cb,
            )
            # Payload construction is part of the analyze step (NOT the callback
            # step): a malformed analyze result (non-dict, windows present-but-
            # None, or an unexpected window key) is a bad-analysis-output failure
            # and MUST map to EXIT_ANALYSIS, not EXIT_CALLBACK (KJOB-04 distinct-
            # exit-code contract). Mirrors the process_file dict-guard.
            if not isinstance(result, dict):
                log.error("job_runner_bad_result", file_id=fid, step="analyze", got=type(result).__name__)
                sys.exit(EXIT_ANALYSIS)
            # Fail LOUDLY on a zero-window analysis (cloud-analyze-empty-no-ext hardening).
            # ``*_total`` is the NATURAL pre-stride window count; both being 0 means the
            # duration probe read 0 seconds (an undecodable/mis-suffixed download), which
            # previously recorded a NULL-everything "success". A real audio file always
            # yields >=1 window, so 0/0 is a decode failure — exit non-zero so Kueue/Workload
            # reads it as failed_at instead of a false completion. SystemExit is BaseException,
            # so it bypasses the `except Exception` below (same as the non-dict guard above).
            fine_total = result.get("fine_windows_total") or 0
            coarse_total = result.get("coarse_windows_total") or 0
            if fine_total == 0 and coarse_total == 0:
                log.error(
                    "job_runner_empty_analysis",
                    file_id=fid,
                    step="analyze",
                    reason="zero_windows",
                    suffix=suffix,
                    fine_windows_total=fine_total,
                    coarse_windows_total=coarse_total,
                )
                sys.exit(EXIT_ANALYSIS)
            payload = _build_payload(result)
        except Exception:
            log.exception("job_runner_analysis_failed", file_id=fid, step="analyze")
            sys.exit(EXIT_ANALYSIS)
        log.info(
            "job_runner_step_ok",
            file_id=fid,
            step="analyze",
            elapsed_ms=_elapsed_ms(t_analyze),
            sampled=result.get("sampled"),
        )

        # (5) callback — the shared _request funnel supplies the bounded ~3x
        # retry (D-02); on final failure exit 13. The payload was already built
        # in the analyze step so build errors never mis-code as EXIT_CALLBACK.
        t_callback = time.monotonic()
        try:
            await client.put_analysis(file_id, payload)
        except Exception:
            log.exception("job_runner_callback_failed", file_id=fid, step="callback")
            sys.exit(EXIT_CALLBACK)
        log.info("job_runner_step_ok", file_id=fid, step="callback", elapsed_ms=_elapsed_ms(t_callback))

        log.info("job_runner_complete", file_id=fid, outcome="success", exit_code=EXIT_OK)
        sys.exit(0)
    finally:
        # V12: the downloaded temp file never outlives the pod, on every exit path
        # (success, integrity mismatch, analysis/callback failure). The client's
        # connection pool is released regardless of outcome.
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        await client.close()


def main() -> None:
    """Configure logging FIRST (D-03), then run the one-shot flow to a ``sys.exit``."""
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover  # CLI invocation guard
    main()
