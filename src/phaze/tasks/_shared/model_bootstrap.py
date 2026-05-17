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
    - ensure_models_present(models_dir): idempotent .pb-file check + download-on-empty
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from phaze.scripts.download_models import CLASSIFIER_MODELS, GENRE_MODELS, download_to


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)


_EXPECTED_MODEL_COUNT = len(CLASSIFIER_MODELS) + len(GENRE_MODELS)
"""Total `.pb` weight files the production agent must have on disk.

Compared against the count of `*.pb` files in `models_dir` to detect partial
downloads. The previous implementation skipped re-download whenever *any*
`.pb` file existed, so an interrupted first run (e.g., 1/34 files written)
permanently left the agent broken because every subsequent start short-
circuited. See Phase 29 CR-03.
"""


def ensure_models_present(models_dir: Path) -> None:
    """Skip if all expected .pb files exist; else download. Raises RuntimeError on failure.

    Completeness contract (Phase 29 CR-03): the directory is treated as
    "populated" only when the count of `*.pb` files meets or exceeds
    `_EXPECTED_MODEL_COUNT`. A non-empty-but-incomplete directory logs a
    WARNING and re-invokes ``download_to``, which is idempotent at the
    per-file level (existing files are skipped by `_download_one`).

    Failures during the download are wrapped in :class:`RuntimeError` so the
    agent_worker container exits non-zero and the ``restart: unless-stopped``
    policy retries (T-29-05-02).
    """
    pb_files = list(models_dir.glob("*.pb"))
    if len(pb_files) >= _EXPECTED_MODEL_COUNT:
        logger.info("Models present (%d weight files at %s)", len(pb_files), models_dir)
        return
    if pb_files:
        logger.warning(
            "Partial model state at %s: %d/%d .pb files present; re-running download to complete",
            models_dir,
            len(pb_files),
            _EXPECTED_MODEL_COUNT,
        )
    else:
        logger.info(
            "%s is empty; downloading essentia weights (~150MB, takes 2-5min on first start)...",
            models_dir,
        )
    try:
        download_to(models_dir)
    except Exception as exc:
        msg = f"Model download failed: {exc}"
        raise RuntimeError(msg) from exc
    logger.info("Models downloaded successfully to %s", models_dir)
