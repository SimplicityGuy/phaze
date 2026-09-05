"""Validate that the essentia weights are PROVISIONED at the models dir -- never download them.

Operator instruction, 2026-09-02 (bead phaze-ynv6w): the model set lives in ONE
operator-provisioned directory and phaze must use it from there and must never
re-download it. This module used to be the Phase 29 D-21 auto-download hook
(``download_to`` on every boot, repairing any missing / wrong-size file from
essentia.upf.edu under an exclusive ``flock`` in the models dir). That path is
gone: a missing or wrong-size file is now a startup FAILURE whose message names
the directory and the offending files, so the operator provisions the set
(``python -m phaze.scripts.download_models <dir>`` on a host that has no copy,
or a bind mount of the consolidated directory) instead of the worker silently
pulling ~3.1 GB.

Consequences that follow from "no writer":
    - No ``mkdir``, no lockfile, no ``*.part*`` scratch sweep. Nothing here ever
      opens a file for writing, so the models mount can be -- and now is --
      read-only (``:ro`` in docker-compose.agent.yml / docker-compose.cloud-agent.yml).
    - No network. The whole check is one ``os.stat`` per manifest entry, so the
      startup hook that used to block the event loop on essentia.upf.edu's TLS
      cannot block on anything.
    - Cross-process serialization is unnecessary: four lane workers validating
      the same read-only directory concurrently cannot interfere.

IMPORT-BOUNDARY (extends Phase 26 D-25 + Phase 27 D-22):
    Postgres-free. Imports: stdlib + phaze.scripts.download_models (for the
    baked-in size ``MANIFEST`` only). Verified by
    tests/shared/core/test_task_split.py::test_model_bootstrap_stays_postgres_free.

Public exports:
    - ensure_models_present(models_dir): validate every manifest file's presence
      and byte size; raise ``RuntimeError`` naming the dir and every offending file.
    - MissingModels: the structured result behind that error, for callers that want
      the file lists rather than the message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from phaze.scripts.download_models import CLASSIFIER_MODELS, GENRE_MODELS, MANIFEST


if TYPE_CHECKING:
    from pathlib import Path


logger = structlog.get_logger(__name__)


_EXPECTED_MODEL_COUNT = len(CLASSIFIER_MODELS) + len(GENRE_MODELS)
"""Total model configurations the production agent must have on disk (each
contributing a ``.pb`` + ``.json`` pair, i.e. ``_EXPECTED_MODEL_COUNT * 2`` files).

Used for the operator-facing log lines. Completeness is "every manifest file
present AND size-valid", checked per file below -- a glob count cannot tell a
truncated ``.pb`` from a full one (260608-jbg), so no count ever short-circuits.
"""


@dataclass(frozen=True)
class MissingModels:
    """What a failed validation found, in the order the manifest lists the files.

    ``missing`` names files that do not exist; ``wrong_size`` pairs each present
    file whose ``st_size`` disagrees with the manifest with ``(actual, expected)``.
    """

    models_dir: Path
    missing: tuple[str, ...] = field(default=())
    wrong_size: tuple[tuple[str, int, int], ...] = field(default=())

    def __bool__(self) -> bool:
        return bool(self.missing or self.wrong_size)

    def message(self) -> str:
        """The operator-facing failure text: the directory, the count, and every offending file."""
        lines = [
            f"essentia models are not provisioned at {self.models_dir}: "
            f"{len(self.missing)} missing, {len(self.wrong_size)} wrong-size of {len(MANIFEST)} expected files. "
            "phaze never downloads models -- point MODELS_PATH / PHAZE_MODELS_DIR at the consolidated model directory, "
            "or provision this one with `python -m phaze.scripts.download_models <dir>` on a host that has no copy."
        ]
        lines.extend(f"  missing: {name}" for name in self.missing)
        lines.extend(f"  wrong size: {name} is {actual} bytes, manifest expects {expected}" for name, actual, expected in self.wrong_size)
        return "\n".join(lines)


def validate_models(models_dir: Path) -> MissingModels:
    """Compare every manifest file under ``models_dir`` against its pinned byte size.

    Pure ``os.stat``: no network, no writes, no directory creation. A missing
    directory reports every file as missing rather than raising, so the caller's
    one error message covers that case too.
    """
    missing: list[str] = []
    wrong_size: list[tuple[str, int, int]] = []
    for name, expected in MANIFEST.items():
        path = models_dir / name
        if not path.is_file():
            missing.append(name)
            continue
        actual = path.stat().st_size
        if actual != expected:
            wrong_size.append((name, actual, expected))
    return MissingModels(models_dir=models_dir, missing=tuple(missing), wrong_size=tuple(wrong_size))


def ensure_models_present(models_dir: Path) -> None:
    """Fail fast unless every weight file is present at ``models_dir`` with its pinned size.

    Raises :class:`RuntimeError` carrying :meth:`MissingModels.message` -- the
    directory, the counts, and every missing / wrong-size file by name -- so the
    agent_worker container exits non-zero and the ``restart: unless-stopped``
    policy keeps retrying until the operator provisions the set (T-29-05-02). A
    wrong-size file is reported, never deleted: this function owns no writes.

    Succeeds silently (one INFO line) on a complete, size-valid set, including on
    a read-only mount.
    """
    logger.info(
        "validating model weights -- %d models (%d files, ~3.1 GB) expected at %s; phaze never downloads them",
        _EXPECTED_MODEL_COUNT,
        len(MANIFEST),
        models_dir,
        count=_EXPECTED_MODEL_COUNT,
        dir=str(models_dir),
    )
    result = validate_models(models_dir)
    if result:
        logger.error(
            "model weights are not provisioned",
            dir=str(models_dir),
            missing_count=len(result.missing),
            wrong_size_count=len(result.wrong_size),
        )
        raise RuntimeError(result.message())
    logger.info("models validated", present_count=len(MANIFEST), dir=str(models_dir))
