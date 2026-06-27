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
    12  windowed analysis raised / OOM / inner timeout (fail-fast — D-02)
    13  callback PUT failed after the shared bounded retry (D-02)

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
from phaze.schemas.agent_analysis import AnalysisWindowPayload, AnalysisWritePayload
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

_FILE_ID_ENV = "PHAZE_JOB_FILE_ID"
_MODELS_DIR_ENV = "PHAZE_MODELS_DIR"
_DOWNLOAD_CHUNK_BYTES = 1 << 16  # 64 KiB, matches compute_sha256 chunking
_DOWNLOAD_CONNECT_TIMEOUT_S = 30.0
_DOWNLOAD_READ_TIMEOUT_S = 300.0


def _elapsed_ms(start: float) -> int:
    """Whole-millisecond wall-clock delta since ``start`` (``time.monotonic``)."""
    return int((time.monotonic() - start) * 1000)


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
    public bucket endpoint — distinct from the baked-CA internal callback.
    """
    timeout = httpx.Timeout(_DOWNLOAD_CONNECT_TIMEOUT_S, read=_DOWNLOAD_READ_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout) as downloader, downloader.stream("GET", url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            async for chunk in resp.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
                fh.write(chunk)


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
        sys.exit(EXIT_DOWNLOAD)

    raw_file_id = os.environ.get(_FILE_ID_ENV)
    if not raw_file_id:
        log.error("job_runner_missing_file_id", env=_FILE_ID_ENV)
        sys.exit(EXIT_DOWNLOAD)
    try:
        file_id = uuid.UUID(raw_file_id)
    except ValueError:
        log.error("job_runner_invalid_file_id", value=raw_file_id)
        sys.exit(EXIT_DOWNLOAD)

    models_dir = os.environ.get(_MODELS_DIR_ENV) or cfg.models_path
    fid = str(file_id)

    # KJOB-05 / T-52-01: build the callback client with verify=cfg.agent_ca_file
    # (the baked internal CA). construct_agent_client raises if the CA is missing
    # or empty; TLS verification is never bypassed.
    client = construct_agent_client(cfg)

    tmp_path: Path | None = None
    try:
        # (1) presign — fail-fast, no extra retry loop (D-02).
        t_presign = time.monotonic()
        try:
            url, expected_sha256 = await client.request_download_url(file_id)
        except Exception:
            log.exception("job_runner_presign_failed", file_id=fid, step="presign")
            sys.exit(EXIT_DOWNLOAD)
        log.info("job_runner_step_ok", file_id=fid, step="presign", elapsed_ms=_elapsed_ms(t_presign))

        # (2) download — stream to a temp file; bearer never attached (T-52-04).
        suffix = Path(urlparse(url).path).suffix or ".audio"
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
        if actual_sha256 != expected_sha256:
            log.error("job_runner_integrity_mismatch", file_id=fid, step="verify")
            sys.exit(EXIT_INTEGRITY)
        log.info("job_runner_step_ok", file_id=fid, step="verify", elapsed_ms=_elapsed_ms(t_verify))

        # (4) analyze — windowed/streaming analyze_file DIRECTLY (no pebble pool,
        # no retry loop — fail-fast, D-02 / KJOB-03). models_dir from env (D-05).
        analyze_file = _load_analyze_file()
        t_analyze = time.monotonic()
        try:
            result = analyze_file(str(tmp_path), models_dir, fine_cap=cfg.analysis_fine_cap, coarse_cap=cfg.analysis_coarse_cap)
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
        # retry (D-02); on final failure exit 13.
        t_callback = time.monotonic()
        try:
            await client.put_analysis(file_id, _build_payload(result))
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
