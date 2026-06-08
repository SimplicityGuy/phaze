"""Python helper that fetches the essentia weight files (Phase 29 D-21).

The same URL list + SHA manifest the existing bash script uses, exposed as a Python
function so both bash (``scripts/download-models.sh``) and the agent bootstrap
(``phaze.tasks._shared.model_bootstrap.ensure_models_present``) can drive the
download. Idempotent: skips files that already exist; verifies SHA-256 if provided
(deferred to a future plan).

Atomicity (T-29-05-03): each download writes to ``<dest>.part`` and is promoted
to ``<dest>`` via ``os.replace`` (atomic on POSIX) only after the byte stream
completes and any ``Content-Length`` is satisfied; a crash mid-download leaves
only the ``.part`` file which is NOT counted by ``models_dir.glob("*.pb")`` in
the bootstrap caller.

Resilience (260608-i21): ``_download_one`` retries transient transport errors
and 5xx server responses with bounded exponential backoff + jitter (via
``time.sleep``), so a single TLS/handshake/read drop no longer kills the worker.
A 4xx response fails fast (no retry); only after exhausting ``_MAX_ATTEMPTS`` does
a per-file named ``RuntimeError`` propagate.

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
import urllib.error

import httpx


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


def _download_one(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` with bounded retry and an atomic promotion.

    Idempotent: if ``dest`` already exists, returns immediately without touching
    the network or sleeping. A crash mid-stream leaves only ``<dest>.part`` which
    the bootstrap's ``*.pb`` glob does NOT match -- the next start will retry.

    Resilience (260608-i21): transient transport errors (see ``_TRANSIENT_ERRORS``)
    and 5xx server responses are retried up to ``_MAX_ATTEMPTS`` times with bounded
    exponential backoff + jitter via ``time.sleep``. A 4xx response fails fast: its
    ``httpx.HTTPStatusError`` is not in the caught tuple, so it propagates uncaught
    and the route is hit exactly once. A truncated transfer (bytes written !=
    ``Content-Length``) is treated as a retryable incomplete read. Only a
    fully-streamed file is promoted into place via ``os.replace``; a failed attempt
    removes its ``.part`` file before retrying. After exhausting all attempts a
    ``RuntimeError`` naming the file and attempt count is raised, chained from the
    last underlying error.
    """
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=_TIMEOUT) as response:
                status = response.status_code
                if 400 <= status < 500:
                    # Fail fast: HTTPStatusError is not caught below, so no retry.
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
            return
        except (*_TRANSIENT_ERRORS, _RetryableDownloadError) as exc:
            tmp.unlink(missing_ok=True)
            if attempt >= _MAX_ATTEMPTS:
                msg = f"Failed to download {dest.name} after {_MAX_ATTEMPTS} attempts: {exc}"
                raise RuntimeError(msg) from exc
            logger.warning("Transient error downloading %s (attempt %d/%d): %s", dest.name, attempt, _MAX_ATTEMPTS, exc)
            delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)) + random.uniform(0, _JITTER_SECONDS)  # noqa: S311  # nosec B311
            time.sleep(delay)


def download_to(target_dir: Path) -> None:
    """Download all classifier + genre weight files into ``target_dir``.

    Idempotent at the per-file level (``_download_one`` skips existing files).
    A partial-completion scenario (e.g., network drop after 17/33 classifiers)
    can be safely resumed by re-running ``download_to`` on the same directory.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    for model_path in CLASSIFIER_MODELS:
        filename = model_path.rsplit("/", 1)[-1]
        _download_one(f"{_CLASSIFIER_BASE}/{model_path}.pb", target_dir / f"{filename}.pb")
        _download_one(f"{_CLASSIFIER_BASE}/{model_path}.json", target_dir / f"{filename}.json")
    for model in GENRE_MODELS:
        _download_one(f"{_GENRE_BASE}/{model}.pb", target_dir / f"{model}.pb")
        _download_one(f"{_GENRE_BASE}/{model}.json", target_dir / f"{model}.json")


if __name__ == "__main__":  # pragma: no cover  # CLI invocation guard
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "./models")
    download_to(target)
