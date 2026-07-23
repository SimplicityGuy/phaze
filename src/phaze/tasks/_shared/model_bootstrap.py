"""Auto-download essentia weights when /models is empty (Phase 29 D-21).

IMPORT-BOUNDARY (extends Phase 26 D-25 + Phase 27 D-22):
    Postgres-free. Imports: stdlib + phaze.scripts.download_models only.
    Verified by tests/shared/core/test_task_split.py::test_model_bootstrap_stays_postgres_free
    (Phase 29 BLOCKER-1: explicit subprocess case for this module, parallel
    to the existing test_shared_bootstrap_stays_postgres_free which covers
    agent_bootstrap.py only).

Race avoidance (Phase 29 WARNING-7, superseded for multi-worker by phaze-mb8d):
    Only phaze.tasks.agent_worker.startup invokes ensure_models_present.
    phaze.agent_watcher.__main__ does NOT -- the watcher does file discovery
    only and cannot dispatch analysis jobs until the worker is up anyway.
    Since quick-260707-dh1 the "worker" is FOUR lane containers
    (worker-analyze/fingerprint/meta/io) booting concurrently against the same
    rw /models mount, so single-owner is no longer a topology guarantee.
    Cross-process serialization is now explicit: ensure_models_present takes a
    blocking exclusive ``fcntl.flock`` on ``.models.download.lock`` inside
    models_dir around the whole validate+repair pass. The winner downloads;
    each waiter acquires afterwards, re-runs the per-file size check via
    ``download_to``, sees everything present+size-valid, and no-ops with zero
    network. Beneath the lock, ``_download_one`` streams to a per-process
    unique ``<dest>.part.<pid>`` scratch name as defense in depth, so even an
    unserialized caller (e.g. a manual CLI run racing an agent boot) can never
    truncate another process's in-flight stream. Stale ``*.part*`` leftovers
    from crashed processes are swept while the exclusive lock is held.

Public exports:
    - ensure_models_present(models_dir): local size-manifest validation (per-file
      os.stat compare via download_to) + (re-)download missing/wrong-size files

Local-validation contract (260608-u8g, supersedes 260608-jbg): the healthy path is
now a pure ``os.stat`` size-manifest check -- ZERO network, near-instant -- and the
network is touched ONLY to repair a missing or wrong-size file. The previous
contract issued a remote ``HEAD`` per weight file on EVERY boot; inside the async
startup hook that blocked the event loop for minutes whenever essentia.upf.edu's TLS
flaked, starving the ``scan_directory`` SAQ job. That per-boot remote validation was
removed because it depended on a flaky remote and blocked startup.
"""

from __future__ import annotations

import fcntl
from typing import TYPE_CHECKING

import structlog

from phaze.scripts.download_models import CLASSIFIER_MODELS, GENRE_MODELS, download_to


if TYPE_CHECKING:
    from pathlib import Path


logger = structlog.get_logger(__name__)


_EXPECTED_MODEL_COUNT = len(CLASSIFIER_MODELS) + len(GENRE_MODELS)
"""Total model configurations the production agent must have on disk (each
contributing a ``.pb`` + ``.json`` pair, i.e. ``_EXPECTED_MODEL_COUNT * 2`` files).

Used only for the operator-facing startup estimate. It is NOT a completeness
gate: a glob count cannot tell a truncated `.pb` from a full one, so a count
short-circuit blessed corrupt files as "present" (260608-jbg). Completeness is
now "all canonical files present AND size-valid", enforced per file by
``download_to``'s local size-manifest comparison (`_ensure_present_local`, 260608-u8g).
"""


_LOCK_FILENAME = ".models.download.lock"
"""Lockfile name inside ``models_dir`` guarding the validate+repair pass (phaze-mb8d).

Deliberately does NOT match the ``*.part*`` stale-scratch sweep glob or the
bootstrap's ``*.pb`` completeness glob.
"""


def _sweep_stale_part_files(models_dir: Path) -> int:
    """Remove leftover ``*.part*`` scratch files; return how many were removed.

    MUST only be called while the exclusive download lock is held: under the lock
    no other agent process can have an in-flight stream, so any surviving scratch
    file (fixed-name ``.part`` from pre-phaze-mb8d builds, or ``.part.<pid>`` from
    a hard-killed process) is garbage from a crashed writer.
    """
    stale = list(models_dir.glob("*.part*"))
    for stray in stale:
        stray.unlink(missing_ok=True)
    return len(stale)


def ensure_models_present(models_dir: Path) -> None:
    """Validate every weight file's size and (re-)download as needed.

    Local-validation contract (260608-u8g): there is no glob-count short-circuit.
    ``download_to`` is invoked unconditionally and performs the per-file integrity
    check -- but the healthy path is now a pure ``os.stat`` comparison against a
    baked-in size manifest (ZERO network, near-instant). It keeps any file whose
    on-disk byte size matches the manifest (no HTTP call) and re-downloads only a
    missing or wrong-size one. A fully valid on-disk set therefore returns instantly
    without touching the network, while a correctly-named-but-truncated `.pb` (which
    the old count gate accepted) is detected and replaced. The per-boot remote
    ``HEAD`` validation (260608-jbg) was removed because it blocked the async startup
    event loop and depended on a flaky remote; the rare repair GET still validates
    the streamed byte count against the server ``Content-Length``.

    Cross-process serialization (phaze-mb8d): the whole validate+repair pass runs
    under a blocking exclusive ``fcntl.flock`` on ``.models.download.lock`` inside
    ``models_dir``, because all four lane workers (plus worker-drain) boot this
    same startup concurrently against one shared rw /models mount. Exactly one
    process downloads; each waiter then acquires the lock, re-runs the per-file
    size check, finds everything present+size-valid, and returns with zero
    network. Stale ``*.part*`` scratch files from crashed writers are swept while
    the lock is held. The flock is advisory but every ensure_models_present
    caller takes it, and it is dropped automatically on process death (fd close),
    so a hard-killed winner can never wedge the waiters.

    Failures during the download are wrapped in :class:`RuntimeError` so the
    agent_worker container exits non-zero and the ``restart: unless-stopped``
    policy retries (T-29-05-02). The wrapped cause is the per-file
    ``RuntimeError`` raised by ``_download_one`` after exhausting its in-process
    retries (260608-i21), so the surfaced message names the specific file and the
    attempt count -- a transient TLS/handshake/read drop or a 5xx is retried in
    place and no longer reaches this wrap. A repair whose promoted size still
    violates the manifest raises the same way (phaze-10ij).
    """
    models_dir.mkdir(parents=True, exist_ok=True)
    lock_path = models_dir / _LOCK_FILENAME
    with lock_path.open("a") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # A sibling lane worker is mid-download on the shared volume. Block until
            # it finishes (a fresh full set is ~3.1 GB, so this can take minutes),
            # then fall through to the zero-network re-validation.
            logger.info(
                "another worker holds the model download lock -- waiting for it to finish before re-validating",
                lock=str(lock_path),
                dir=str(models_dir),
            )
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        swept = _sweep_stale_part_files(models_dir)
        if swept:
            logger.info("swept stale model scratch files", count=swept, dir=str(models_dir))
        logger.info(
            "validating model weights -- essentia weights at %s (~3.1 GB across %d files); a fresh "
            "download is multi-GB and can take many minutes (longer on a slow link) -- a legitimate "
            "transfer is not a hang",
            models_dir,
            _EXPECTED_MODEL_COUNT,
            count=_EXPECTED_MODEL_COUNT,
            dir=str(models_dir),
        )
        try:
            present_count, repaired_count = download_to(models_dir)
        except Exception as exc:
            msg = f"Model download failed: {exc}"
            raise RuntimeError(msg) from exc
        logger.info(
            "models validated",
            present_count=present_count,
            repaired_count=repaired_count,
            dir=str(models_dir),
        )
