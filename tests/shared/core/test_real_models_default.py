"""``tests/_real_models.py``: the ``PHAZE_TEST_MODELS_DIR`` default is supplied only from a COMPLETE set (phaze-ynv6w)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from phaze.scripts.download_models import MANIFEST
from tests import _real_models


if TYPE_CHECKING:
    from pathlib import Path


def _write_complete_set(models_dir: Path) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    for name, size in MANIFEST.items():
        with (models_dir / name).open("wb") as fh:
            fh.truncate(size)


def test_default_supplied_from_a_complete_conventional_dir(tmp_path: Path) -> None:
    _write_complete_set(tmp_path / "essentia-models")
    env: dict[str, str] = {}

    assert _real_models.apply_default(env, tmp_path) == str(tmp_path / "essentia-models")
    assert env[_real_models.ENV_VAR] == str(tmp_path / "essentia-models")


def test_no_default_when_the_conventional_dir_is_absent(tmp_path: Path) -> None:
    env: dict[str, str] = {}

    assert _real_models.apply_default(env, tmp_path) is None
    assert _real_models.ENV_VAR not in env


def test_no_default_when_a_file_is_missing_or_wrong_size(tmp_path: Path) -> None:
    """A partial copy must SKIP the real-model test, not run it against half a set."""
    models_dir = tmp_path / "essentia-models"
    _write_complete_set(models_dir)
    (models_dir / "discogs-effnet-bs64-1.pb").unlink()
    assert _real_models.apply_default({}, tmp_path) is None

    _write_complete_set(models_dir)
    (models_dir / "gender-musicnn-mtt-2.json").write_bytes(b"{}")
    assert _real_models.apply_default({}, tmp_path) is None


def test_operator_export_wins_over_the_convention(tmp_path: Path) -> None:
    _write_complete_set(tmp_path / "essentia-models")
    env = {_real_models.ENV_VAR: "/somewhere/else"}

    assert _real_models.apply_default(env, tmp_path) == "/somewhere/else"
    assert env[_real_models.ENV_VAR] == "/somewhere/else"


def test_empty_export_is_an_opt_out_that_keeps_the_skip(tmp_path: Path) -> None:
    """``PHAZE_TEST_MODELS_DIR=`` (empty) must NOT be overridden by the convention."""
    _write_complete_set(tmp_path / "essentia-models")
    env = {_real_models.ENV_VAR: ""}

    assert _real_models.apply_default(env, tmp_path) is None
    assert env[_real_models.ENV_VAR] == ""


def test_holds_complete_model_set_on_the_real_manifest(tmp_path: Path) -> None:
    _write_complete_set(tmp_path)
    assert _real_models.holds_complete_model_set(tmp_path)
    assert not _real_models.holds_complete_model_set(tmp_path / "nope")
