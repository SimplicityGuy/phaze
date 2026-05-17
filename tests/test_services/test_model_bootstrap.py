"""Tests for ``phaze.tasks._shared.model_bootstrap.ensure_models_present`` (Phase 29 D-21).

Three LOCKED cases per PATTERNS lines 1077-1090:
- empty-dir -> ``download_to`` is invoked, INFO log surfaces the download notice
- populated -> ``download_to`` is NOT invoked, INFO log surfaces the "Models present" line
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


def test_ensure_models_present_empty_dir_downloads(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty models directory triggers ``download_to`` and logs the start banner."""
    import phaze.tasks._shared.model_bootstrap as mb

    def fake_download(target: Path) -> None:
        # Simulate a real download by writing a sentinel .pb file.
        (target / "test_model.pb").touch()

    mock = MagicMock(side_effect=fake_download)
    monkeypatch.setattr(mb, "download_to", mock)

    with caplog.at_level(logging.INFO, logger="phaze.tasks._shared.model_bootstrap"):
        mb.ensure_models_present(tmp_path)

    mock.assert_called_once_with(tmp_path)
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "downloading essentia weights" in text, f"expected start banner in logs, got: {text!r}"


def test_ensure_models_present_populated_no_op(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-populated models directory short-circuits before invoking ``download_to``.

    Phase 29 CR-03: "populated" now means count-equals-expected (34 .pb files),
    not "any .pb file present". Write all expected files so the short-circuit
    branch is exercised.
    """
    import phaze.tasks._shared.model_bootstrap as mb

    expected = mb._EXPECTED_MODEL_COUNT
    for idx in range(expected):
        (tmp_path / f"model_{idx:03d}.pb").touch()

    mock = MagicMock()
    monkeypatch.setattr(mb, "download_to", mock)

    with caplog.at_level(logging.INFO, logger="phaze.tasks._shared.model_bootstrap"):
        mb.ensure_models_present(tmp_path)

    mock.assert_not_called()
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert f"Models present ({expected} weight files" in text, f"expected 'Models present' log, got: {text!r}"


def test_ensure_models_present_partial_triggers_redownload(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 29 CR-03: a partial models directory (some .pb files but not all) re-runs download.

    Previously, the bootstrap short-circuited on *any* .pb file present, so an
    interrupted first download (e.g., 1/34 files written) left every subsequent
    start skipping the re-download and the agent silently broken at analysis
    time. This test pins the new behavior: partial state triggers download_to
    and logs a WARNING with the observed/expected counts.
    """
    import phaze.tasks._shared.model_bootstrap as mb

    # 1 out of N: clearly partial.
    (tmp_path / "first_model.pb").touch()
    assert len(list(tmp_path.glob("*.pb"))) < mb._EXPECTED_MODEL_COUNT

    completed = MagicMock()

    def fake_download(target: Path) -> None:
        completed(target)

    monkeypatch.setattr(mb, "download_to", fake_download)

    with caplog.at_level(logging.WARNING, logger="phaze.tasks._shared.model_bootstrap"):
        mb.ensure_models_present(tmp_path)

    completed.assert_called_once_with(tmp_path)
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Partial model state" in text, f"expected partial-state WARNING, got: {text!r}"
    assert f"1/{mb._EXPECTED_MODEL_COUNT}" in text, f"expected observed/expected counts in WARNING, got: {text!r}"


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


def test_download_one_is_idempotent_when_dest_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_download_one`` short-circuits when ``dest`` already exists -- no network call."""
    from phaze.scripts import download_models

    dest = tmp_path / "already_here.pb"
    dest.write_bytes(b"existing-bytes")

    # If httpx.stream is invoked, the test fails -- it must not be touched.
    def boom(*_args: object, **_kwargs: object) -> object:
        msg = "httpx.stream must not be called when dest exists"
        raise AssertionError(msg)

    monkeypatch.setattr(download_models.httpx, "stream", boom)

    download_models._download_one("https://example.invalid/never-fetched.pb", dest)

    assert dest.read_bytes() == b"existing-bytes"


def test_download_to_creates_pb_and_json_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``download_to`` produces a .pb + .json file pair for every classifier and genre model."""
    from phaze.scripts import download_models

    fetched: list[tuple[str, Path]] = []

    def fake_download_one(url: str, dest: Path) -> None:
        fetched.append((url, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00")

    monkeypatch.setattr(download_models, "_download_one", fake_download_one)

    download_models.download_to(tmp_path)

    # 33 classifier models x 2 files (.pb + .json) + 1 genre x 2 = 68 files.
    expected_file_count = (len(download_models.CLASSIFIER_MODELS) + len(download_models.GENRE_MODELS)) * 2
    assert len(fetched) == expected_file_count
    pb_files = sorted(p.name for _, p in fetched if p.suffix == ".pb")
    json_files = sorted(p.name for _, p in fetched if p.suffix == ".json")
    assert len(pb_files) == len(download_models.CLASSIFIER_MODELS) + len(download_models.GENRE_MODELS)
    assert len(json_files) == len(download_models.CLASSIFIER_MODELS) + len(download_models.GENRE_MODELS)
