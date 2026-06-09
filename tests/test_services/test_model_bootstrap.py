"""Tests for ``phaze.tasks._shared.model_bootstrap.ensure_models_present`` (260608-jbg).

Always-validate contract (the count gate was removed in 260608-jbg):
- any dir (empty/partial/full) -> ``download_to`` is invoked exactly once; the
  per-file HEAD size validation lives in ``download_to`` (proven in
  tests/test_scripts/test_download_models.py case d), not here
- the startup INFO log reflects the ~3.1 GB / 34-file reality, NOT "150MB"/"2-5min"
- network-fail -> ``RuntimeError("Model download failed")`` wrapping the underlying exception
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import httpx
import pytest


if TYPE_CHECKING:
    from pathlib import Path


def _assert_estimate_log(text: str) -> None:
    """The startup log must reflect the ~3.1 GB / 34-file reality, not the stale estimate."""
    assert "3.1 GB" in text, f"expected the corrected ~3.1 GB estimate, got: {text!r}"
    assert "150MB" not in text, f"stale 150MB estimate must be gone, got: {text!r}"
    assert "2-5min" not in text, f"stale 2-5min estimate must be gone, got: {text!r}"


def test_ensure_models_present_empty_dir_downloads(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty models directory triggers ``download_to`` and logs the corrected estimate."""
    import phaze.tasks._shared.model_bootstrap as mb

    def fake_download(target: Path) -> tuple[int, int]:
        # Simulate a real download by writing a sentinel .pb file.
        (target / "test_model.pb").touch()
        return (0, 1)  # (present_count, repaired_count) tally surfaced by download_to

    mock = MagicMock(side_effect=fake_download)
    monkeypatch.setattr(mb, "download_to", mock)

    with caplog.at_level(logging.INFO, logger="phaze.tasks._shared.model_bootstrap"):
        mb.ensure_models_present(tmp_path)

    mock.assert_called_once_with(tmp_path)
    _assert_estimate_log("\n".join(rec.getMessage() for rec in caplog.records))


def test_ensure_models_present_populated_still_calls_download_to(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-populated dir still calls ``download_to`` once (no count short-circuit).

    260608-jbg: the count gate is gone. ``download_to`` owns the per-file HEAD size
    validation and issues no GET when sizes match (proven in download_models case d),
    so a valid set returns without error after a single ``download_to`` call here.
    """
    import phaze.tasks._shared.model_bootstrap as mb

    for idx in range(mb._EXPECTED_MODEL_COUNT):
        (tmp_path / f"model_{idx:03d}.pb").touch()

    mock = MagicMock(return_value=(mb._EXPECTED_MODEL_COUNT, 0))
    monkeypatch.setattr(mb, "download_to", mock)

    with caplog.at_level(logging.INFO, logger="phaze.tasks._shared.model_bootstrap"):
        mb.ensure_models_present(tmp_path)

    mock.assert_called_once_with(tmp_path)
    _assert_estimate_log("\n".join(rec.getMessage() for rec in caplog.records))


def test_ensure_models_present_partial_still_calls_download_to(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """260608-jbg: a partial dir calls ``download_to`` once -- no special partial branch.

    A truncated file can satisfy a glob count, so the old count gate was removed.
    Completeness is decided entirely by ``download_to``'s per-file size validation,
    so a partial dir is handled identically to an empty or full one here.
    """
    import phaze.tasks._shared.model_bootstrap as mb

    # 1 out of N: clearly partial.
    (tmp_path / "first_model.pb").touch()
    assert len(list(tmp_path.glob("*.pb"))) < mb._EXPECTED_MODEL_COUNT

    mock = MagicMock(return_value=(1, mb._EXPECTED_MODEL_COUNT - 1))
    monkeypatch.setattr(mb, "download_to", mock)

    mb.ensure_models_present(tmp_path)

    mock.assert_called_once_with(tmp_path)


def test_ensure_models_present_download_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network failure during download is wrapped in ``RuntimeError`` with the original cause chained."""
    import phaze.tasks._shared.model_bootstrap as mb

    underlying = httpx.HTTPError("network down")

    def boom(target: Path) -> None:
        raise underlying

    monkeypatch.setattr(mb, "download_to", boom)

    with pytest.raises(RuntimeError, match="Model download failed") as excinfo:
        mb.ensure_models_present(tmp_path)
    assert excinfo.value.__cause__ is underlying


def test_download_models_classifier_count_matches_bash() -> None:
    """CLASSIFIER_MODELS contains exactly the 33 paths declared in scripts/download-models.sh."""
    from phaze.scripts.download_models import CLASSIFIER_MODELS, GENRE_MODELS

    assert len(CLASSIFIER_MODELS) == 33
    assert len(GENRE_MODELS) == 1
    assert GENRE_MODELS == ("discogs-effnet-bs64-1",)


def test_download_to_creates_pb_and_json_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``download_to`` routes a .pb + .json pair through ``_ensure_present_local`` for every model.

    260608-u8g: ``download_to`` no longer calls ``_download_one`` directly -- the
    validate-or-download decision lives in ``_ensure_present_local`` (local size
    compare) -- so this patches that boundary instead.
    """
    from phaze.scripts import download_models

    fetched: list[tuple[str, Path]] = []

    def fake_ensure_present_local(url: str, dest: Path, expected_size: int) -> None:
        fetched.append((url, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00")

    monkeypatch.setattr(download_models, "_ensure_present_local", fake_ensure_present_local)

    download_models.download_to(tmp_path)

    # 33 classifier models x 2 files (.pb + .json) + 1 genre x 2 = 68 files.
    expected_file_count = (len(download_models.CLASSIFIER_MODELS) + len(download_models.GENRE_MODELS)) * 2
    assert len(fetched) == expected_file_count
    pb_files = sorted(p.name for _, p in fetched if p.suffix == ".pb")
    json_files = sorted(p.name for _, p in fetched if p.suffix == ".json")
    assert len(pb_files) == len(download_models.CLASSIFIER_MODELS) + len(download_models.GENRE_MODELS)
    assert len(json_files) == len(download_models.CLASSIFIER_MODELS) + len(download_models.GENRE_MODELS)
