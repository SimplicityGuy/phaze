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

Phase 101 (OBS-03, phaze-bo3p.3): the windowed ``analyze_file`` runs in a REAL child
process (``python -m phaze.analysis_child`` via ``services.analysis_exec``), so this
pod's asyncio event loop is never GIL-starved by essentia's C++ — progress POSTs go
out mid-analysis and the essentia wheel never loads in THIS process at all (the
deferred-import seam moved into the child). SIGTERM is intentionally NOT trapped
into a 0 exit — the default Python SIGTERM->143 is honestly non-zero so an evicted
pod is never mistaken for success (Pitfall 6).
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
from phaze.logging_config import _parse_bool, configure_logging
from phaze.schemas.agent_analysis import AnalysisProgressPayload, AnalysisWindowPayload, AnalysisWritePayload, PresignDownloadMetadata
from phaze.services.analysis_exec import run_analysis_subprocess
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


class PresignedDownloadError(RuntimeError):
    """Raised when the presigned-GET download fails, message redacted (D-13 style).

    httpx's ``HTTPStatusError`` renders ``str(response.url)`` verbatim into its message, and the
    staging GET URL is presigned (X-Amz-Signature/X-Amz-Credential in the query string) -- a
    complete, live, self-authenticating credential. The caller logs download failures with
    ``log.exception`` (``exc_info=True``), so an unwrapped ``HTTPStatusError`` would put that
    credential into the pod's log stream. Mirrors ``agent_client``'s ``METHOD path -> status``
    redaction: carry only the status code and the URL with its query string stripped.
    """


def _redact_query(url: str) -> str:
    """Strip the query string from ``url`` so a presigned credential never reaches a log line."""
    return urlparse(url)._replace(query="").geturl()


def _elapsed_ms(start: float) -> int:
    """Whole-millisecond wall-clock delta since ``start`` (``time.monotonic``)."""
    return int((time.monotonic() - start) * 1000)


def _mb(size_bytes: int) -> float:
    """Bytes -> mebibytes, rounded to 1 dp, for human-readable step/banner lines (OBS-02)."""
    return round(size_bytes / (1024 * 1024), 1)


def _resolve_friendly_default() -> bool:
    """Resolve the ONE-SHOT pod's friendly-console default (OBS-02, phaze-sfbx.3).

    Every other phaze entrypoint calls ``configure_logging()`` with friendly rendering
    defaulting OFF (JSON-only). The one-shot Job pod is the exception: an operator tailing
    ``kubectl logs`` wants a human line next to each machine JSON line WITHOUT piping through a
    pretty-printer, so this pod defaults friendly ON. Precedence is deliberately inverted only
    for THIS pod: an explicit ``PHAZE_LOG_FRIENDLY`` (``0``/``false``/``no`` turns it back OFF,
    truthy keeps it ON) always wins; only the ABSENCE of the env var flips the default to ON.
    Returning a concrete ``bool`` (never ``None``) means ``configure_logging`` sees an explicit
    choice and never falls back to its own default-off ``_resolve_friendly`` env path.
    """
    env_value = os.environ.get("PHAZE_LOG_FRIENDLY")
    if env_value is None or env_value.strip() == "":
        return True
    return _parse_bool(env_value)


def _log_banner(file_id: str, metadata: PresignDownloadMetadata | None) -> None:
    """Emit the one-shot startup banner right after presign succeeds (OBS-02, phaze-sfbx.3).

    Renders the human-readable identity threaded through the presign response
    (phaze-sfbx.1's ``PresignDownloadMetadata`` block) so an operator tailing the pod's friendly
    console line sees WHICH file this Job analyzes -- filename, source path/origin, duration,
    size, and the target cluster/bucket -- not just an opaque UUID. Every field degrades
    independently: an absent metadata block (older control plane) OR an absent individual field
    (partial ``CloudJob``/``FileRecord`` row) is simply omitted, so the worst case is a
    ``file_id``-only banner. The banner is COSMETIC -- the exit-code contract (D-01) is untouched
    -- so the whole build+emit is guarded and a rendering failure never fails the job.
    """
    try:
        fields: dict[str, Any] = {"file_id": file_id}
        if metadata is not None:
            if metadata.original_filename is not None:
                fields["filename"] = metadata.original_filename
            if metadata.current_path is not None:
                fields["source_path"] = metadata.current_path
            if metadata.source_agent_id is not None:
                fields["source_agent_id"] = metadata.source_agent_id
            if metadata.duration_sec is not None:
                fields["duration_sec"] = metadata.duration_sec
            if metadata.file_size is not None:
                fields["file_size_mb"] = _mb(metadata.file_size)
            if metadata.staging_bucket is not None:
                fields["staging_bucket"] = metadata.staging_bucket
            if metadata.backend_id is not None:
                fields["backend_id"] = metadata.backend_id
        log.info("job_runner_banner", **fields)
    except Exception:  # cosmetic banner: NEVER fail the job for odd/missing metadata (D-01 untouched)
        log.debug("job_runner_banner_failed", file_id=file_id)


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


async def _download_to(url: str, dest: Path) -> None:
    """Stream the presigned GET to ``dest`` in 64 KiB chunks.

    Uses a FRESH httpx client with NO Authorization header: the presigned URL is
    self-authenticating, and attaching the internal bearer would leak it to the
    object store (T-52-04). ``verify`` defaults to True (system CAs) for the
    public bucket endpoint — distinct from the internal-CA callback (the CA is
    mounted from a K8s Secret at runtime, KDEPLOY-06).

    A non-2xx response raises ``PresignedDownloadError`` rather than letting
    ``httpx.HTTPStatusError`` propagate: that exception's message embeds the FULL
    presigned URL (including ``X-Amz-Signature``/``X-Amz-Credential``), and the caller's
    ``log.exception`` would otherwise put that live credential into the pod's log stream. The
    original exception is deliberately NOT chained (``from None``) -- structlog's
    ``format_exc_info`` renders a chained cause's message too, which would leak the URL right
    back through the "direct cause" traceback section.
    """
    timeout = httpx.Timeout(_DOWNLOAD_CONNECT_TIMEOUT_S, read=_DOWNLOAD_READ_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout) as downloader, downloader.stream("GET", url) as resp:
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            raise PresignedDownloadError(f"GET {_redact_query(url)} -> {resp.status_code}") from None
        with dest.open("wb") as fh:
            async for chunk in resp.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
                fh.write(chunk)


async def _safe_post_progress(client: Any, file_id: uuid.UUID, payload: AnalysisProgressPayload) -> None:
    """Best-effort counter-only progress POST (Phase 57.1, D-16).

    Swallows ANY error (the ``AgentApiError`` hierarchy from the client's single-attempt,
    short-timeout progress path (Phase 99 OBS-01), plus anything unexpected) so a dropped
    progress POST can never change the one-shot exit code — the completion ``put_analysis``
    writes the final count regardless, so the bar reaches 100% from completion. Phase 101:
    runs as a fire-and-forget loop task created by the progress callback (which now fires
    ON the event loop as the analysis child's protocol lines arrive).
    """
    try:
        await client.post_analysis_progress(file_id, payload)
    except Exception:  # best-effort: progress never alters the EXIT_ANALYSIS/EXIT_CALLBACK contract
        log.debug("job_runner_progress_dropped", file_id=str(file_id))


def _make_progress_cb(client: Any, file_id: uuid.UUID, interval_sec: float) -> tuple[Any, set[asyncio.Task[None]]]:
    """Build the sync ``progress_cb`` the analysis-child driver invokes per FINE window.

    Phase 101: ``analyze_file`` runs in a real child process, so this callback fires ON the
    event loop (from the driver's protocol pump) — the POST is a fire-and-forget loop task
    (strong-ref'd in the returned ``pending`` set so it is never GC'd mid-flight), no
    cross-thread scheduling needed. The caller drains ``pending`` after analysis so the
    one-shot process never exits with POSTs still in flight. Throttled to ``interval_sec``
    (``monotonic()``-keyed); the START count and the final count (``analyzed >= total``)
    always post so the bar gets an early total and a final value even inside the throttle
    window. Any exception is swallowed so a progress failure can never escape into the
    analysis outcome (KJOB-04 contract).
    """
    # Seed to -inf, NOT 0.0: time.monotonic()'s epoch is boot-relative, so on a freshly booted
    # host (a one-shot pod ALWAYS is) with uptime < interval_sec, `now - 0.0` would be < the
    # interval and THROTTLE the very first callback -- silently breaking the documented "the START
    # count always posts" contract (and the console START line). -inf makes the first gap
    # infinite, so the start count passes the gate regardless of machine uptime.
    state = {"last_post": float("-inf")}
    pending: set[asyncio.Task[None]] = set()

    def _cb(analyzed: int, total: int) -> None:
        try:
            now = time.monotonic()
            is_final = total > 0 and analyzed >= total
            if interval_sec > 0.0 and not is_final and (now - state["last_post"]) < interval_sec:
                return
            state["last_post"] = now
            payload = AnalysisProgressPayload(fine_windows_analyzed=analyzed, fine_windows_total=total)
            # Fire-and-forget loop task (we're already ON the loop); the pending set keeps a
            # strong reference so the task is never garbage-collected before it runs.
            task = asyncio.get_running_loop().create_task(_safe_post_progress(client, file_id, payload))
            pending.add(task)
            task.add_done_callback(pending.discard)
            # OBS-02 (phaze-sfbx.3): the console progress line shares the SAME throttle gate and
            # counter as the UI progress POST above -- one throttle, one counter -- so the tailed
            # pod log and the web progress bar can never diverge. `is_final` bypasses the throttle,
            # so a final "N/N (100%)" line is ALWAYS emitted. This log sits INSIDE the same swallow
            # contract below: a rendering failure never escapes the analysis thread (KJOB-04).
            percent = round(100.0 * analyzed / total, 1) if total > 0 else 0.0
            log.info(
                "job_runner_progress",
                file_id=str(file_id),
                fine_windows_analyzed=analyzed,
                fine_windows_total=total,
                percent=percent,
            )
        except Exception:  # a progress-cb error must never escape into the analysis outcome
            log.debug("job_runner_progress_cb_error", file_id=str(file_id))

    return _cb, pending


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
            # phaze-sfbx.1 widened this to a 4-tuple; phaze-sfbx.3 now CONSUMES the
            # display-identity block (Phase 100) for the console banner below.
            url, expected_sha256, audio_ext, presign_metadata = await client.request_download_url(file_id)
        except Exception:
            log.exception("job_runner_presign_failed", file_id=fid, step="presign")
            sys.exit(EXIT_DOWNLOAD)
        log.info("job_runner_step_ok", file_id=fid, step="presign", elapsed_ms=_elapsed_ms(t_presign))
        # OBS-02 (phaze-sfbx.3): human-readable startup banner, best-effort from the presign
        # metadata block. Degrades to a UUID-only line and never fails the job (D-01 untouched).
        _log_banner(fid, presign_metadata)

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
        # OBS-02 (phaze-sfbx.3): carry the downloaded size so the friendly line reads
        # "...step=download downloaded_mb=130.4...". event/step/elapsed_ms stay UNCHANGED
        # (machine parsers key off them); downloaded_mb is a purely additive human field.
        log.info(
            "job_runner_step_ok",
            file_id=fid,
            step="download",
            elapsed_ms=_elapsed_ms(t_download),
            downloaded_mb=_mb(tmp_path.stat().st_size),
        )

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
        # OBS-02 (phaze-sfbx.3): a truncated hash makes the friendly "verified sha256" line
        # human-recognizable; event/step/elapsed_ms unchanged, sha256 is additive.
        log.info("job_runner_step_ok", file_id=fid, step="verify", elapsed_ms=_elapsed_ms(t_verify), sha256=actual_sha256[:12])

        # (4) analyze — the windowed analyze_file runs in a REAL child process via the
        # shared subprocess driver (Phase 101, OBS-03): the pod's event loop is no longer
        # GIL-starved, so the progress callback fires mid-analysis. No retry loop —
        # fail-fast, D-02 / KJOB-03. models_dir from env (D-05). There is NO in-process
        # timeout here (timeout=None): a hung analysis is bounded by the Kueue/Job
        # wall-clock deadline (activeDeadlineSeconds -> SIGTERM -> 143). Only a raised
        # exception maps to EXIT_ANALYSIS (12); the contract docstring documents this
        # delegation (WR-04).
        t_analyze = time.monotonic()
        progress_cb, pending_progress = _make_progress_cb(client, file_id, cfg.analysis_progress_interval_sec)
        # phaze-sfbx.4 markers, phaze-bo3p.3 capture: essentia's ``[ INFO ] MusicExtractor...``
        # banners are written by C++ directly to fd 1/2 — in the CHILD, whose fd 1 is
        # re-routed to the stderr pipe, so the driver frames every banner line as an
        # ``analysis_child_output`` event (Phase 101 delivered the capture the in-process
        # model deferred). The begin/end markers still bracket the framed child output so
        # an operator tailing the console sees where analysis starts and stops.
        log.info("job_runner_analyze_begin", file_id=fid, step="analyze", detail="analysis running -- framed essentia output follows")
        try:
            # Any-typed on purpose: the dict guard below must stay a REACHABLE runtime check
            # (the seam is monkeypatched in tests, and KJOB-04 wants a loud EXIT_ANALYSIS on
            # any malformed analysis output), not be optimized away by the driver's annotation.
            result: Any = await run_analysis_subprocess(
                str(tmp_path),
                models_dir,
                fine_cap=cfg.analysis_fine_cap,
                coarse_cap=cfg.analysis_coarse_cap,
                progress_cb=progress_cb,
            )
        except Exception:
            # Close the frame BEFORE the failure log so the (possibly noisy) essentia output above
            # stays bracketed even on the crash path, then map to EXIT_ANALYSIS (D-01 unchanged).
            log.info("job_runner_analyze_end", file_id=fid, step="analyze", outcome="error")
            log.exception("job_runner_analysis_failed", file_id=fid, step="analyze")
            sys.exit(EXIT_ANALYSIS)
        # Close the frame on the success path too: everything below is analysis-OUTPUT validation
        # (NOT essentia stdout), so it sits OUTSIDE the frame markers.
        log.info("job_runner_analyze_end", file_id=fid, step="analyze", outcome="ok")
        # Drain any in-flight progress POSTs (each swallows its own errors) so the one-shot
        # process never races its own exit — the completion PUT below remains the authority.
        if pending_progress:
            await asyncio.gather(*pending_progress, return_exceptions=True)

        # Payload construction is part of the analyze step (NOT the callback step): a malformed
        # analyze result (non-dict, windows present-but-None, or an unexpected window key) is a
        # bad-analysis-output failure and MUST map to EXIT_ANALYSIS, not EXIT_CALLBACK (KJOB-04
        # distinct-exit-code contract). Mirrors the process_file dict-guard.
        if not isinstance(result, dict):
            log.error("job_runner_bad_result", file_id=fid, step="analyze", got=type(result).__name__)
            sys.exit(EXIT_ANALYSIS)
        # Fail LOUDLY on a zero-window analysis (cloud-analyze-empty-no-ext hardening).
        # ``*_total`` is the NATURAL pre-stride window count; both being 0 means the
        # duration probe read 0 seconds (an undecodable/mis-suffixed download), which
        # previously recorded a NULL-everything "success". A real audio file always
        # yields >=1 window, so 0/0 is a decode failure — exit non-zero so Kueue/Workload
        # reads it as failed_at instead of a false completion.
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
        # A window dict carrying an unexpected key fails AnalysisWindowPayload (extra="forbid")
        # during payload build -- still an analysis-output error, so it maps to EXIT_ANALYSIS
        # (12), NOT EXIT_CALLBACK (13) (WR-01: payload build is part of the analyze step).
        try:
            payload = _build_payload(result)
        except Exception:
            log.exception("job_runner_analysis_failed", file_id=fid, step="analyze")
            sys.exit(EXIT_ANALYSIS)
        # OBS-02 (phaze-sfbx.3): surface the analyzed/total fine-window counts so the friendly
        # line reads "...step=analyze fine_windows_analyzed=94 fine_windows_total=94...".
        # event/step/elapsed_ms/sampled unchanged; the window counts are additive human fields.
        log.info(
            "job_runner_step_ok",
            file_id=fid,
            step="analyze",
            elapsed_ms=_elapsed_ms(t_analyze),
            sampled=result.get("sampled"),
            fine_windows_analyzed=result.get("fine_windows_analyzed"),
            fine_windows_total=result.get("fine_windows_total"),
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
        # OBS-02 (phaze-sfbx.3): name the destination so the friendly "analysis written" line
        # reads for humans; event/step/elapsed_ms unchanged, result is additive.
        log.info("job_runner_step_ok", file_id=fid, step="callback", elapsed_ms=_elapsed_ms(t_callback), result="analysis written")

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
    """Configure logging FIRST (D-03), then run the one-shot flow to a ``sys.exit``.

    OBS-02 (phaze-sfbx.3): unlike every other phaze entrypoint, this one-shot pod defaults
    friendly dual-rendering ON (``friendly=_resolve_friendly_default()``) so an operator tailing
    ``kubectl logs`` reads a human line beside each machine JSON line without a pretty-printer.
    Precedence (see ``_resolve_friendly_default``): an explicit ``PHAZE_LOG_FRIENDLY`` always
    wins -- ``PHAZE_LOG_FRIENDLY=0`` still turns friendly rendering back OFF for this pod -- and
    only the ABSENCE of the env var selects the pod's ON default. Passing an explicit ``bool``
    (never ``None``) keeps ``configure_logging`` from re-consulting the env with its own
    default-off fallback, so this inverted default is scoped to THIS entrypoint alone.
    """
    configure_logging(friendly=_resolve_friendly_default())
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover  # CLI invocation guard
    main()
