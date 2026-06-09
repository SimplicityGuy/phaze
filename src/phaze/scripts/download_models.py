"""Python helper that fetches the essentia weight files (Phase 29 D-21).

The same URL list the existing bash script uses, exposed as a Python function so
both bash (``scripts/download-models.sh``) and the agent bootstrap
(``phaze.tasks._shared.model_bootstrap.ensure_models_present``) can drive the
download.

Local-validation contract (260608-u8g): on every invocation each expected file's
on-disk byte size is compared against a baked-in size ``MANIFEST`` -- a pure
``os.stat`` check with ZERO network I/O. The network is touched ONLY to repair a
missing or wrong-size file. A present-and-correct-size file is kept without any
HTTP request; a missing or truncated file is (re-)downloaded via the atomic,
retrying, integrity-checked ``_download_one`` GET path.

The manifest sizes equal the server ``Content-Length`` captured at model-pin time
(the validated production deployment). The repair GET still validates the streamed
byte count against the server ``Content-Length`` before promoting the file into
place, so a repaired file is integrity-checked against the remote as before. There
are no published checksums for the essentia weights, so the byte size is the
authoritative signal -- same-size bit-flips are out of scope.

Supersedes 260608-jbg: the previous contract issued a synchronous ``HEAD`` per
weight file on EVERY boot to re-read the remote ``Content-Length``. Inside the
``async def`` agent startup hook that froze the event loop for minutes whenever
essentia.upf.edu's TLS flaked, starving the ``scan_directory`` SAQ job. The
always-remote-HEAD-validate rationale is removed: the healthy path no longer
depends on a flaky remote and never blocks.

Atomicity (T-29-05-03): each download writes to ``<dest>.part`` and is promoted
to ``<dest>`` via ``os.replace`` (atomic on POSIX) only after the byte stream
completes and any ``Content-Length`` is satisfied; a crash mid-download leaves
only the ``.part`` file which is NOT counted by ``models_dir.glob("*.pb")`` in
the bootstrap caller.

Resilience (260608-i21 / 260608-u8g): the repair GET is driven through the shared
``_with_retries`` helper, which retries transient transport errors and 5xx server
responses with bounded exponential backoff + jitter (via ``time.sleep``). A single
TLS/handshake/read drop no longer kills the worker, and no un-timeouted request can
wedge it indefinitely. A 4xx response fails fast (no retry); only after exhausting
``_MAX_ATTEMPTS`` does a per-file named ``RuntimeError`` propagate.

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


# Authoritative ``.json`` byte sizes keyed by stem (no extension), captured from the
# server ``Content-Length`` of the validated production deployment at model-pin time
# (260608-u8g). Used to build MANIFEST; do not hand-edit without re-capturing.
_JSON_SIZES: dict[str, int] = {
    "danceability-musicnn-msd-2": 2677,
    "danceability-musicnn-mtt-2": 2688,
    "danceability-vggish-audioset-1": 2691,
    "discogs-effnet-bs64-1": 14990,
    "gender-musicnn-msd-2": 2664,
    "gender-musicnn-mtt-2": 2664,
    "gender-vggish-audioset-1": 2678,
    "mood_acoustic-musicnn-msd-2": 3078,
    "mood_acoustic-musicnn-mtt-2": 3079,
    "mood_acoustic-vggish-audioset-1": 3093,
    "mood_aggressive-musicnn-msd-2": 3085,
    "mood_aggressive-musicnn-mtt-2": 3085,
    "mood_aggressive-vggish-audioset-1": 3099,
    "mood_electronic-musicnn-msd-2": 3093,
    "mood_electronic-musicnn-mtt-2": 3093,
    "mood_electronic-vggish-audioset-1": 3107,
    "mood_happy-musicnn-msd-2": 3049,
    "mood_happy-musicnn-mtt-2": 3049,
    "mood_happy-vggish-audioset-1": 3063,
    "mood_party-musicnn-msd-2": 3049,
    "mood_party-musicnn-mtt-2": 3049,
    "mood_party-vggish-audioset-1": 3063,
    "mood_relaxed-musicnn-msd-2": 3062,
    "mood_relaxed-musicnn-mtt-2": 3063,
    "mood_relaxed-vggish-audioset-1": 3077,
    "mood_sad-musicnn-msd-2": 3034,
    "mood_sad-musicnn-mtt-2": 3034,
    "mood_sad-vggish-audioset-1": 3048,
    "tonal_atonal-musicnn-msd-2": 2680,
    "tonal_atonal-musicnn-mtt-2": 2681,
    "tonal_atonal-vggish-audioset-1": 2695,
    "voice_instrumental-musicnn-msd-1": 2712,
    "voice_instrumental-musicnn-mtt-2": 2712,
    "voice_instrumental-vggish-audioset-1": 2785,
}

# The single ``.pb`` byte-size outliers; every other ``.pb`` resolves to the common
# musicnn weight size via ``_expected_pb_size``.
_PB_VOICE_INSTRUMENTAL_MSD = 3239625
_PB_DISCOGS_EFFNET = 18366619
_PB_VGGISH_AUDIOSET = 288629030
_PB_MUSICNN_COMMON = 3239548


def _expected_pb_size(filename: str) -> int:
    """Return the authoritative ``Content-Length`` for a ``.pb`` weight ``filename``.

    Rule set captured from the validated production deployment (260608-u8g):
    the lone musicnn outlier and the discogs effnet weight are exact-matched, the
    vggish-audioset family shares one large size, and every other musicnn weight
    shares the common size.
    """
    if filename == "voice_instrumental-musicnn-msd-1.pb":
        return _PB_VOICE_INSTRUMENTAL_MSD
    if filename == "discogs-effnet-bs64-1.pb":
        return _PB_DISCOGS_EFFNET
    if filename.endswith("-vggish-audioset-1.pb"):
        return _PB_VGGISH_AUDIOSET
    return _PB_MUSICNN_COMMON


def _build_manifest() -> dict[str, int]:
    """Build the baked-in ``filename -> expected byte size`` manifest.

    Derived programmatically from ``CLASSIFIER_MODELS`` then ``GENRE_MODELS`` so it
    cannot drift from the model list (T-u8g-04). Each model contributes a ``.pb``
    (sized via ``_expected_pb_size``) and a ``.json`` (sized via ``_JSON_SIZES``),
    yielding exactly ``(len(CLASSIFIER_MODELS) + len(GENRE_MODELS)) * 2 == 68``
    entries.
    """
    manifest: dict[str, int] = {}
    for model_path in (*CLASSIFIER_MODELS, *GENRE_MODELS):
        stem = model_path.rsplit("/", 1)[-1]
        manifest[f"{stem}.pb"] = _expected_pb_size(f"{stem}.pb")
        manifest[f"{stem}.json"] = _JSON_SIZES[stem]
    return manifest


MANIFEST: dict[str, int] = _build_manifest()


def _with_retries[T](label: str, fn: Callable[[], T]) -> T:
    """Run ``fn`` with bounded exponential backoff + jitter on transient failures.

    This is the SINGLE retry/backoff/timeout-recovery implementation; the repair GET
    path (``_download_one``) flows through it. ``fn`` is invoked up to
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


def _download_one(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` atomically with bounded retry.

    Always fetches: the validate-or-download decision lives in
    ``_ensure_present_local`` (local size compare), so callers route through it
    rather than calling this directly. A crash mid-stream leaves only
    ``<dest>.part`` which the bootstrap's ``*.pb`` glob does NOT match -- the next
    start will retry.

    Resilience (260608-i21 / 260608-u8g): the byte stream is driven through the
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


def _ensure_present_local(url: str, dest: Path, expected_size: int) -> None:
    """Validate ``dest`` against the baked-in ``expected_size`` and repair via GET if needed.

    Implements the local-validation decision (260608-u8g):

    - file present AND ``st_size == expected_size`` -> keep it, issuing NO network
      call (pure ``os.stat``).
    - file present but wrong size -> truncated/corrupt: WARN naming on-disk vs
      manifest size and re-fetch via ``_download_one`` (its atomic ``os.replace``
      overwrites in place; a failed repair leaves the stale file, which the next
      boot re-detects and re-repairs).
    - file missing -> INFO and download via ``_download_one``.
    """
    if dest.exists():
        actual = dest.stat().st_size
        if actual == expected_size:
            return
        logger.warning(
            "Size mismatch for %s: on-disk %d bytes != manifest %d bytes; re-downloading",
            dest.name,
            actual,
            expected_size,
        )
    else:
        logger.info("Missing weight file %s; downloading", dest.name)
    _download_one(url, dest)


def download_to(target_dir: Path) -> None:
    """Local-validate + repair all classifier + genre weight files into ``target_dir``.

    Each file is routed through ``_ensure_present_local``, which compares the on-disk
    byte size against the baked-in ``MANIFEST`` and only GETs a file that is missing
    or whose size does not match. A fully valid directory therefore incurs ZERO HTTP
    requests (pure ``os.stat`` per file, no HEAD, no GET). A partial-completion
    scenario (network drop after 17/33 classifiers) can be safely resumed by
    re-running ``download_to`` on the same directory.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    for model_path in CLASSIFIER_MODELS:
        filename = model_path.rsplit("/", 1)[-1]
        _ensure_present_local(f"{_CLASSIFIER_BASE}/{model_path}.pb", target_dir / f"{filename}.pb", MANIFEST[f"{filename}.pb"])
        _ensure_present_local(f"{_CLASSIFIER_BASE}/{model_path}.json", target_dir / f"{filename}.json", MANIFEST[f"{filename}.json"])
    for model in GENRE_MODELS:
        _ensure_present_local(f"{_GENRE_BASE}/{model}.pb", target_dir / f"{model}.pb", MANIFEST[f"{model}.pb"])
        _ensure_present_local(f"{_GENRE_BASE}/{model}.json", target_dir / f"{model}.json", MANIFEST[f"{model}.json"])


if __name__ == "__main__":  # pragma: no cover  # CLI invocation guard
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "./models")
    download_to(target)
