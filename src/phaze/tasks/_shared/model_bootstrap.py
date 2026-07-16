"""Auto-download essentia weights when /models is empty (Phase 29 D-21).

IMPORT-BOUNDARY (extends Phase 26 D-25 + Phase 27 D-22):
    Postgres-free. Imports: stdlib + phaze.scripts.download_models only.
    Verified by tests/shared/core/test_task_split.py::test_model_bootstrap_stays_postgres_free
    (Phase 29 BLOCKER-1: explicit subprocess case for this module, parallel
    to the existing test_shared_bootstrap_stays_postgres_free which covers
    agent_bootstrap.py only).

Race avoidance (Phase 29 WARNING-7):
    Only phaze.tasks.agent_worker.startup invokes ensure_models_present.
    phaze.agent_watcher.__main__ does NOT -- the watcher does file discovery
    only and cannot dispatch analysis jobs until the worker is up anyway,
    so we let the worker own the download and avoid a .part-file race on
    fresh /models volumes.

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

    Failures during the download are wrapped in :class:`RuntimeError` so the
    agent_worker container exits non-zero and the ``restart: unless-stopped``
    policy retries (T-29-05-02). The wrapped cause is the per-file
    ``RuntimeError`` raised by ``_download_one`` after exhausting its in-process
    retries (260608-i21), so the surfaced message names the specific file and the
    attempt count -- a transient TLS/handshake/read drop or a 5xx is retried in
    place and no longer reaches this wrap.
    """
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
