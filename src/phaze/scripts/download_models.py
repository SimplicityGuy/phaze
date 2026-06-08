"""Python helper that fetches the essentia weight files (Phase 29 D-21).

The same URL list the existing bash script uses, exposed as a Python function so
both bash (``scripts/download-models.sh``) and the agent bootstrap
(``phaze.tasks._shared.model_bootstrap.ensure_models_present``) can drive the
download.

Integrity validation (260608-jbg): on every invocation each expected file's
on-disk byte size is validated against the server's ``Content-Length`` (read via
a ``HEAD`` request). A present-but-truncated file (on-disk size != expected) is
removed and re-downloaded; a fully valid file is kept without a GET. There are no
published checksums for the essentia weights, so the server ``Content-Length`` is
the authoritative size signal -- same-size bit-flips are out of scope.

Atomicity (T-29-05-03): each download writes to ``<dest>.part`` and is promoted
to ``<dest>`` via ``os.replace`` (atomic on POSIX) only after the byte stream
completes and any ``Content-Length`` is satisfied; a crash mid-download leaves
only the ``.part`` file which is NOT counted by ``models_dir.glob("*.pb")`` in
the bootstrap caller.

Resilience (260608-i21 / 260608-jbg): EVERY HTTP request (HEAD and GET) is issued
with an explicit ``_TIMEOUT`` and driven through the shared ``_with_retries``
helper, which retries transient transport errors and 5xx server responses with
bounded exponential backoff + jitter (via ``time.sleep``). A single
TLS/handshake/read drop no longer kills the worker, and no un-timeouted request
can wedge it indefinitely. A 4xx response fails fast (no retry); only after
exhausting ``_MAX_ATTEMPTS`` does a per-file named ``RuntimeError`` propagate.

CLI entry:
    python -m phaze.scripts.download_models [output_dir]

The single positional argument defaults to ``./models``. The Bash shim
``scripts/download-models.sh`` is a thin wrapper that invokes this module.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import random
import socket
import ssl
import sys
import time
from typing import TYPE_CHECKING
import urllib.error

import httpx


if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)

_CLASSIFIER_BASE = "https://essentia.upf.edu/models/classifiers"
_GENRE_BASE = "https://essentia.upf.edu/models/music-style-classification/discogs-effnet"

# Retry/backoff tuning for transient network failures (260608-i21).
_MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0
_JITTER_SECONDS = 1.0

# Explicit connect/read/write/pool timeouts so a stalled transfer cannot hang the
# worker indefinitely; a stall surfaces as a TransportError and is retried.
_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)

# Transport-level errors treated as transient and retried. httpx.HTTPStatusError is
# deliberately NOT here so a 4xx fails fast (see _download_one).
_TRANSIENT_ERRORS = (
    httpx.TransportError,
    ssl.SSLError,
    socket.timeout,
    TimeoutError,
    urllib.error.URLError,
    ConnectionError,
)


class _RetryableDownloadError(Exception):
    """Local sentinel for application-level retryable conditions (5xx, truncated read)."""


# 11 classifier model families x 3 variants = 33 models = 66 files (.pb + .json each).
# Byte-for-byte aligned with scripts/download-models.sh lines 16-50; order matters
# for diff-against-bash.
CLASSIFIER_MODELS: tuple[str, ...] = (
    "mood_acoustic/mood_acoustic-musicnn-msd-2",
    "mood_acoustic/mood_acoustic-musicnn-mtt-2",
    "mood_acoustic/mood_acoustic-vggish-audioset-1",
    "mood_electronic/mood_electronic-musicnn-msd-2",
    "mood_electronic/mood_electronic-musicnn-mtt-2",
    "mood_electronic/mood_electronic-vggish-audioset-1",
    "mood_aggressive/mood_aggressive-musicnn-msd-2",
    "mood_aggressive/mood_aggressive-musicnn-mtt-2",
    "mood_aggressive/mood_aggressive-vggish-audioset-1",
    "mood_relaxed/mood_relaxed-musicnn-msd-2",
    "mood_relaxed/mood_relaxed-musicnn-mtt-2",
    "mood_relaxed/mood_relaxed-vggish-audioset-1",
    "mood_happy/mood_happy-musicnn-msd-2",
    "mood_happy/mood_happy-musicnn-mtt-2",
    "mood_happy/mood_happy-vggish-audioset-1",
    "mood_sad/mood_sad-musicnn-msd-2",
    "mood_sad/mood_sad-musicnn-mtt-2",
    "mood_sad/mood_sad-vggish-audioset-1",
    "mood_party/mood_party-musicnn-msd-2",
    "mood_party/mood_party-musicnn-mtt-2",
    "mood_party/mood_party-vggish-audioset-1",
    "danceability/danceability-musicnn-msd-2",
    "danceability/danceability-musicnn-mtt-2",
    "danceability/danceability-vggish-audioset-1",
    "gender/gender-musicnn-msd-2",
    "gender/gender-musicnn-mtt-2",
    "gender/gender-vggish-audioset-1",
    "tonal_atonal/tonal_atonal-musicnn-msd-2",
    "tonal_atonal/tonal_atonal-musicnn-mtt-2",
    "tonal_atonal/tonal_atonal-vggish-audioset-1",
    "voice_instrumental/voice_instrumental-musicnn-msd-1",
    "voice_instrumental/voice_instrumental-musicnn-mtt-2",
    "voice_instrumental/voice_instrumental-vggish-audioset-1",
)

GENRE_MODELS: tuple[str, ...] = ("discogs-effnet-bs64-1",)


def _with_retries[T](label: str, fn: Callable[[], T]) -> T:
    """Run ``fn`` with bounded exponential backoff + jitter on transient failures.

    This is the SINGLE retry/backoff/timeout-recovery implementation shared by both
    the HEAD path (``_head_content_length``) and the GET path (``_download_one``);
    every HTTP request in this module flows through it. ``fn`` is invoked up to
    ``_MAX_ATTEMPTS`` times; its return value is propagated on success.

    A ``_TRANSIENT_ERRORS`` transport error or a ``_RetryableDownloadError`` (5xx /
    truncated read) triggers a ``time.sleep`` backoff and a retry. Any other
    exception -- notably ``httpx.HTTPStatusError`` raised for a 4xx -- propagates
    immediately, preserving 4xx fail-fast. After the final attempt the underlying
    error is wrapped in a ``RuntimeError`` naming ``label`` and the attempt count,
    chained from the cause.

    ``fn`` owns its own per-attempt cleanup (e.g. removing a ``.part`` file); this
    helper performs no resource cleanup of its own.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return fn()
        except (*_TRANSIENT_ERRORS, _RetryableDownloadError) as exc:
            if attempt >= _MAX_ATTEMPTS:
                msg = f"Failed to {label} after {_MAX_ATTEMPTS} attempts: {exc}"
                raise RuntimeError(msg) from exc
            logger.warning("Transient error during %s (attempt %d/%d): %s", label, attempt, _MAX_ATTEMPTS, exc)
            delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)) + random.uniform(0, _JITTER_SECONDS)  # noqa: S311  # nosec B311
            time.sleep(delay)
    # Unreachable: the loop always returns on success or raises on the final attempt.
    msg = f"{label} exhausted all {_MAX_ATTEMPTS} attempts without resolving"  # pragma: no cover
    raise RuntimeError(msg)  # pragma: no cover


def _head_content_length(url: str) -> int | None:
    """Return the server's ``Content-Length`` for ``url`` (or ``None`` if absent).

    Issued as a ``HEAD`` with the shared ``_TIMEOUT`` and driven through
    ``_with_retries`` so a stalled or transiently-failing HEAD cannot wedge the
    worker. A 4xx fails fast (propagates ``httpx.HTTPStatusError``); a 5xx is
    retried. When the response omits ``Content-Length`` the size is unobtainable
    and ``None`` is returned.
    """

    def _attempt() -> int | None:
        response = httpx.head(url, follow_redirects=True, timeout=_TIMEOUT)
        status = response.status_code
        if 400 <= status < 500:
            # Fail fast: HTTPStatusError is not in _TRANSIENT_ERRORS, so no retry.
            response.raise_for_status()
        if status >= 500:
            msg = f"server error {status} for HEAD {url}"
            raise _RetryableDownloadError(msg)
        content_length = response.headers.get("Content-Length")
        return int(content_length) if content_length is not None else None

    return _with_retries(f"HEAD {url}", _attempt)


def _try_head_size(url: str, dest: Path) -> int | None:
    """Best-effort expected size for ``dest`` from a HEAD; degrade to ``None`` on failure.

    A HEAD that exhausts retries (``RuntimeError``) or 4xxs (``httpx.HTTPError``)
    must never crash or wedge the bootstrap: it degrades to the "size unobtainable"
    path (keep an existing file / GET a missing one) with a WARNING naming the file.
    """
    try:
        return _head_content_length(url)
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.warning("Could not determine expected size for %s via HEAD: %s", dest.name, exc)
        return None


def _ensure_present(url: str, dest: Path) -> None:
    """Validate ``dest`` against the server size and download it when needed.

    Implements the LOCKED validate-or-download decision (260608-jbg):

    - expected size unobtainable + file present -> keep it (cannot validate), WARN.
    - expected size unobtainable + file missing -> download.
    - expected size known + file present, size matches -> keep it, NO GET.
    - expected size known + file present, size mismatches -> truncated/corrupt:
      remove the stale file and re-download.
    - expected size known + file missing -> download.
    """
    expected = _try_head_size(url, dest)
    if expected is None:
        if dest.exists():
            logger.warning("Cannot validate %s (no Content-Length from HEAD); keeping existing file", dest.name)
            return
        _download_one(url, dest)
        return
    if dest.exists():
        actual = dest.stat().st_size
        if actual == expected:
            return
        logger.warning("Size mismatch for %s: on-disk %d bytes != expected %d bytes; removing and re-downloading", dest.name, actual, expected)
        dest.unlink()
    _download_one(url, dest)


def _download_one(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` atomically with bounded retry.

    Always fetches: the validate-or-download decision lives in ``_ensure_present``
    (size-based), so callers route through it rather than calling this directly.
    A crash mid-stream leaves only ``<dest>.part`` which the bootstrap's ``*.pb``
    glob does NOT match -- the next start will retry.

    Resilience (260608-i21 / 260608-jbg): the byte stream is driven through the
    shared ``_with_retries`` helper, so transient transport errors (see
    ``_TRANSIENT_ERRORS``) and 5xx server responses are retried up to
    ``_MAX_ATTEMPTS`` times with bounded exponential backoff + jitter. A 4xx
    response fails fast: its ``httpx.HTTPStatusError`` is not in the retry tuple,
    so it propagates uncaught and the route is hit exactly once. A truncated
    transfer (bytes written != ``Content-Length``) is treated as a retryable
    incomplete read. Only a fully-streamed file is promoted into place via
    ``os.replace``; every failed attempt removes its ``.part`` file before
    re-raising. After exhausting all attempts a ``RuntimeError`` naming the file
    and attempt count is raised, chained from the last underlying error.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    def _attempt() -> None:
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=_TIMEOUT) as response:
                status = response.status_code
                if 400 <= status < 500:
                    # Fail fast: HTTPStatusError is not in _TRANSIENT_ERRORS, so no retry.
                    response.raise_for_status()
                if status >= 500:
                    msg = f"server error {status} for {dest.name}"
                    raise _RetryableDownloadError(msg)
                bytes_written = 0
                with tmp.open("wb") as fh:
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        fh.write(chunk)
                        bytes_written += len(chunk)
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) != bytes_written:
                    msg = f"truncated transfer for {dest.name}: wrote {bytes_written} of {content_length} bytes"
                    raise _RetryableDownloadError(msg)
            os.replace(tmp, dest)  # noqa: PTH105  # atomic on POSIX once the full stream landed
        except Exception:
            # Any failure (transient, 5xx, truncated, or fail-fast 4xx) must not
            # leave a partial scratch file behind; clean up then re-raise so
            # _with_retries can decide whether to retry or propagate.
            tmp.unlink(missing_ok=True)
            raise

    _with_retries(f"download {dest.name}", _attempt)


def download_to(target_dir: Path) -> None:
    """Download + size-validate all classifier + genre weight files into ``target_dir``.

    Each file is routed through ``_ensure_present``, which issues a HEAD to read the
    server ``Content-Length`` and only GETs a file that is missing or whose on-disk
    size does not match. A fully valid directory therefore incurs only HEAD requests
    (no GET). A partial-completion scenario (network drop after 17/33 classifiers)
    can be safely resumed by re-running ``download_to`` on the same directory.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    for model_path in CLASSIFIER_MODELS:
        filename = model_path.rsplit("/", 1)[-1]
        _ensure_present(f"{_CLASSIFIER_BASE}/{model_path}.pb", target_dir / f"{filename}.pb")
        _ensure_present(f"{_CLASSIFIER_BASE}/{model_path}.json", target_dir / f"{filename}.json")
    for model in GENRE_MODELS:
        _ensure_present(f"{_GENRE_BASE}/{model}.pb", target_dir / f"{model}.pb")
        _ensure_present(f"{_GENRE_BASE}/{model}.json", target_dir / f"{model}.json")


if __name__ == "__main__":  # pragma: no cover  # CLI invocation guard
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "./models")
    download_to(target)
