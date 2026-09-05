"""Locate the operator's consolidated essentia model set for tests that need the real weights.

Operator instruction, 2026-09-02 (bead phaze-ynv6w): every essentia/TensorFlow model on the
development host lives in ONE directory, ``~/essentia-models``, and phaze must use it from
there and never re-download it. The real-model tests (``test_analysis_with_real_models``)
key off ``PHAZE_TEST_MODELS_DIR``; this helper supplies that default from the convention so
the operator does not have to export it in every shell, and supplies it ONLY when the
directory holds the complete pinned set -- a partial copy would turn a skip into a
misleading red.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from phaze.scripts.download_models import MANIFEST


if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from pathlib import Path


ENV_VAR = "PHAZE_TEST_MODELS_DIR"
CONVENTIONAL_DIR_NAME = "essentia-models"


def holds_complete_model_set(models_dir: Path) -> bool:
    """True iff every manifest file exists under ``models_dir`` at its pinned byte size."""
    try:
        return all((models_dir / name).is_file() and (models_dir / name).stat().st_size == size for name, size in MANIFEST.items())
    except OSError:
        return False


def default_test_models_dir(home: Path) -> Path | None:
    """``<home>/essentia-models`` when it holds the complete set; otherwise ``None``."""
    candidate = home / CONVENTIONAL_DIR_NAME
    return candidate if holds_complete_model_set(candidate) else None


def apply_default(environ: MutableMapping[str, str], home: Path) -> str | None:
    """Set ``PHAZE_TEST_MODELS_DIR`` from the convention unless the operator already exported it.

    An explicit export wins even when EMPTY: ``PHAZE_TEST_MODELS_DIR= just check`` is the
    opt-out that skips the ~2 min real-model test on a host that holds the set. Returns the
    value now in ``environ`` (``None`` when nothing supplies one, or the opt-out is in force).
    """
    if ENV_VAR in environ:
        return environ[ENV_VAR] or None
    found = default_test_models_dir(home)
    if found is None:
        return None
    environ[ENV_VAR] = str(found)
    return environ[ENV_VAR]
