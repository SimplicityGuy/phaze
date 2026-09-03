"""Tests for ``phaze.tasks._shared.model_bootstrap.ensure_models_present`` (phaze-ynv6w).

Validate-only contract (supersedes the Phase 29 D-21 auto-download and its 260608-jbg /
260608-u8g / phaze-mb8d refinements):
- a complete, size-valid set -> succeeds, ZERO network, ZERO writes, works on a read-only dir
- a missing dir / missing file / wrong-size file -> ``RuntimeError`` naming the directory and
  every offending file; nothing is downloaded, deleted or created
- ``download_to`` / ``_download_one`` are never reached from the bootstrap
- the startup INFO log reflects the ~3.1 GB / 34-model reality, NOT "150MB"/"2-5min"
"""

from __future__ import annotations

import logging
import os
import stat
from typing import TYPE_CHECKING

import httpx
import pytest

from phaze.scripts import download_models
from phaze.scripts.download_models import MANIFEST
import phaze.tasks._shared.model_bootstrap as mb


if TYPE_CHECKING:
    from pathlib import Path


def _write_complete_set(models_dir: Path) -> None:
    """Lay down every manifest file at exactly its pinned byte size (sparse, so it is instant)."""
    models_dir.mkdir(parents=True, exist_ok=True)
    for name, size in MANIFEST.items():
        with (models_dir / name).open("wb") as fh:
            fh.truncate(size)


@pytest.fixture(autouse=True)
def _no_network_no_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to fetch or repair from the bootstrap is a test failure, not a slow test."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the model bootstrap must never touch the network or the downloader")

    monkeypatch.setattr(httpx, "stream", _boom)
    monkeypatch.setattr(httpx, "get", _boom)
    monkeypatch.setattr(download_models, "download_to", _boom)
    monkeypatch.setattr(download_models, "_download_one", _boom)
    monkeypatch.setattr(download_models, "_ensure_present_local", _boom)


def _assert_estimate_log(text: str) -> None:
    """The startup log must reflect the ~3.1 GB / 34-model reality, not the stale estimate."""
    assert "3.1 GB" in text, f"expected the corrected ~3.1 GB estimate, got: {text!r}"
    assert "150MB" not in text, f"stale 150MB estimate must be gone, got: {text!r}"
    assert "2-5min" not in text, f"stale 2-5min estimate must be gone, got: {text!r}"


def test_ensure_models_present_accepts_a_complete_size_valid_set(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The healthy path: every manifest file present at its pinned size -> success, no side effects."""
    models_dir = tmp_path / "models"
    _write_complete_set(models_dir)
    before = sorted(p.name for p in models_dir.iterdir())

    with caplog.at_level(logging.INFO):
        mb.ensure_models_present(models_dir)

    assert sorted(p.name for p in models_dir.iterdir()) == before, "validation must create nothing (no lockfile, no scratch)"
    _assert_estimate_log(caplog.text)
    assert "never downloads" in caplog.text


def test_ensure_models_present_works_on_a_read_only_directory(tmp_path: Path) -> None:
    """The compose mounts are now ``:ro`` -- the validator must not need write access."""
    if os.geteuid() == 0:  # pragma: no cover  # root ignores mode bits, so the guard would prove nothing
        pytest.skip("read-only directory semantics are not enforced for root")
    models_dir = tmp_path / "models"
    _write_complete_set(models_dir)
    models_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(PermissionError):
            (models_dir / "probe").touch()  # the directory really is read-only for this test
        mb.ensure_models_present(models_dir)
    finally:
        models_dir.chmod(stat.S_IRWXU)


def test_ensure_models_present_missing_dir_names_the_path_and_creates_nothing(tmp_path: Path) -> None:
    """A missing directory is the operator's mistake to fix: name it, do not ``mkdir`` it."""
    models_dir = tmp_path / "absent"

    with pytest.raises(RuntimeError) as excinfo:
        mb.ensure_models_present(models_dir)

    message = str(excinfo.value)
    assert str(models_dir) in message
    assert f"{len(MANIFEST)} missing" in message
    assert "never downloads" in message
    assert not models_dir.exists(), "the validator must not create the models directory"


def test_ensure_models_present_missing_file_is_named_and_not_downloaded(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    _write_complete_set(models_dir)
    (models_dir / "mood_happy-musicnn-msd-2.pb").unlink()
    (models_dir / "discogs-effnet-bs64-1.json").unlink()

    with pytest.raises(RuntimeError) as excinfo:
        mb.ensure_models_present(models_dir)

    message = str(excinfo.value)
    assert str(models_dir) in message
    assert "2 missing, 0 wrong-size" in message
    assert "missing: mood_happy-musicnn-msd-2.pb" in message
    assert "missing: discogs-effnet-bs64-1.json" in message
    assert not (models_dir / "mood_happy-musicnn-msd-2.pb").exists(), "nothing may be (re-)downloaded"


def test_ensure_models_present_wrong_size_file_is_named_and_kept(tmp_path: Path) -> None:
    """A truncated weight is reported with both sizes and left in place -- the validator owns no writes."""
    models_dir = tmp_path / "models"
    _write_complete_set(models_dir)
    truncated = models_dir / "danceability-vggish-audioset-1.pb"
    truncated.write_bytes(b"\x00" * 10)

    with pytest.raises(RuntimeError) as excinfo:
        mb.ensure_models_present(models_dir)

    message = str(excinfo.value)
    assert "0 missing, 1 wrong-size" in message
    assert f"wrong size: danceability-vggish-audioset-1.pb is 10 bytes, manifest expects {MANIFEST['danceability-vggish-audioset-1.pb']}" in message
    assert truncated.stat().st_size == 10, "the validator must not delete or repair a bad file"


def test_validate_models_reports_in_manifest_order(tmp_path: Path) -> None:
    """The structured result lists files in manifest order so the log is stable across runs."""
    models_dir = tmp_path / "models"
    _write_complete_set(models_dir)
    names = list(MANIFEST)
    for name in (names[5], names[0], names[40]):
        (models_dir / name).unlink()

    result = mb.validate_models(models_dir)

    assert bool(result) is True
    assert result.missing == (names[0], names[5], names[40])
    assert result.wrong_size == ()
    assert result.models_dir == models_dir


def test_validate_models_complete_set_is_falsy(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    _write_complete_set(models_dir)

    result = mb.validate_models(models_dir)

    assert bool(result) is False
    assert result.missing == () and result.wrong_size == ()


def test_bootstrap_module_has_no_network_or_write_surface() -> None:
    """The bootstrap imports only the manifest: no downloader, no lock, no scratch sweep."""
    assert not hasattr(mb, "download_to")
    assert not hasattr(mb, "_sweep_stale_part_files")
    assert not hasattr(mb, "_LOCK_FILENAME")
    assert "fcntl" not in mb.__dict__
    assert "httpx" not in mb.__dict__


def test_download_models_classifier_count_matches_bash() -> None:
    """CLASSIFIER_MODELS contains exactly the 33 paths the provisioning tool fetches."""
    from phaze.scripts.download_models import CLASSIFIER_MODELS, GENRE_MODELS

    assert len(CLASSIFIER_MODELS) == 33
    assert len(GENRE_MODELS) == 1
    assert GENRE_MODELS == ("discogs-effnet-bs64-1",)
    assert mb._EXPECTED_MODEL_COUNT == 34
    assert len(MANIFEST) == 68
