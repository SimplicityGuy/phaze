"""Auto-download essentia weights when /models is empty (Phase 29 D-21).

IMPORT-BOUNDARY (extends Phase 26 D-25 + Phase 27 D-22):
    Postgres-free. Imports: stdlib + phaze.scripts.download_models only.
    Verified by tests/test_task_split.py::test_model_bootstrap_stays_postgres_free
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
    - ensure_models_present(models_dir): always-validate (per-file HEAD size check
      via download_to) + (re-)download missing/truncated files
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from phaze.scripts.download_models import CLASSIFIER_MODELS, GENRE_MODELS, download_to


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)


_EXPECTED_MODEL_COUNT = len(CLASSIFIER_MODELS) + len(GENRE_MODELS)
"""Total weight files the production agent must have on disk.

Used only for the operator-facing startup estimate. It is NOT a completeness
gate: a glob count cannot tell a truncated `.pb` from a full one, so a count
short-circuit blessed corrupt files as "present" (260608-jbg). Completeness is
now "all canonical files present AND size-valid", enforced per file by
``download_to``'s HEAD ``Content-Length`` validation (`_ensure_present`).
"""


def ensure_models_present(models_dir: Path) -> None:
    """Validate every weight file's size and (re-)download as needed.

    Always-validate contract (260608-jbg): there is no glob-count short-circuit.
    ``download_to`` is invoked unconditionally and performs the per-file integrity
    check -- it issues a HEAD per expected file, keeps any file whose on-disk byte
    size matches the server ``Content-Length`` (no GET), and re-downloads a missing
    or truncated one. A fully valid on-disk set therefore returns without an
    operator restart, while a correctly-named-but-truncated `.pb` (which the old
    count gate accepted) is detected and replaced.

    Failures during the download are wrapped in :class:`RuntimeError` so the
    agent_worker container exits non-zero and the ``restart: unless-stopped``
    policy retries (T-29-05-02). The wrapped cause is the per-file
    ``RuntimeError`` raised by ``_download_one`` after exhausting its in-process
    retries (260608-i21), so the surfaced message names the specific file and the
    attempt count -- a transient TLS/handshake/read drop or a 5xx is retried in
    place and no longer reaches this wrap.
    """
    logger.info(
        "Validating essentia weights at %s (~3.1 GB across %d files). A fresh download is "
        "multi-GB and can take many minutes (longer on a slow link) -- a legitimate transfer "
        "is not a hang.",
        models_dir,
        _EXPECTED_MODEL_COUNT,
    )
    try:
        download_to(models_dir)
    except Exception as exc:
        msg = f"Model download failed: {exc}"
        raise RuntimeError(msg) from exc
    logger.info("Models present and size-validated at %s", models_dir)
