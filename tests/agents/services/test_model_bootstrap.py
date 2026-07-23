"""Tests for ``phaze.tasks._shared.model_bootstrap.ensure_models_present`` (260608-jbg).

Always-validate contract (the count gate was removed in 260608-jbg):
- any dir (empty/partial/full) -> ``download_to`` is invoked exactly once; the
  per-file HEAD size validation lives in ``download_to`` (proven in
  tests/test_scripts/test_download_models.py case d), not here
- the startup INFO log reflects the ~3.1 GB / 34-file reality, NOT "150MB"/"2-5min"
- network-fail -> ``RuntimeError("Model download failed")`` wrapping the underlying exception
"""

from __future__ import annotations

import fcntl
import logging
import subprocess
import sys
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


def test_ensure_models_present_holds_exclusive_flock_during_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-mb8d: the download runs UNDER an exclusive flock on the models-dir lockfile.

    The probe opens an independent fd on the lockfile mid-``download_to`` and
    verifies a non-blocking exclusive flock is denied (flock conflicts across
    independent open file descriptions, including within one process), i.e. a
    sibling lane worker booting concurrently would block until the download ends.
    """
    import phaze.tasks._shared.model_bootstrap as mb

    lock_states: list[str] = []

    def probing_download(target: Path) -> tuple[int, int]:
        with (target / mb._LOCK_FILENAME).open("a") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_states.append("held")
            else:
                lock_states.append("unlocked")
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return (0, 0)

    monkeypatch.setattr(mb, "download_to", probing_download)

    mb.ensure_models_present(tmp_path)

    assert lock_states == ["held"], "the exclusive download lock must be held while download_to runs"


def test_ensure_models_present_sweeps_stale_scratch_files_not_the_lockfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-mb8d: stale ``*.part*`` leftovers from crashed writers are swept under the lock.

    Both the pre-fix fixed name (``.part``) and the pid-suffixed name
    (``.part.<pid>``) are garbage once the exclusive lock is held; the lockfile
    itself and real weight files must survive.
    """
    import phaze.tasks._shared.model_bootstrap as mb

    (tmp_path / "discogs-effnet-bs64-1.pb.part").write_bytes(b"crashed legacy writer")
    (tmp_path / "mood_happy-musicnn-msd-2.pb.part.12345").write_bytes(b"crashed pid writer")
    (tmp_path / "gender-musicnn-mtt-2.json.part.99").write_bytes(b"{}")
    (tmp_path / "intact.pb").write_bytes(b"real weight")

    monkeypatch.setattr(mb, "download_to", MagicMock(return_value=(0, 0)))

    mb.ensure_models_present(tmp_path)

    assert list(tmp_path.glob("*.part*")) == [], "every stale scratch file must be removed"
    assert (tmp_path / "intact.pb").read_bytes() == b"real weight", "real weights must survive the sweep"
    assert (tmp_path / mb._LOCK_FILENAME).exists(), "the lockfile itself must not be swept"


_CONCURRENT_BOOT_CHILD = """
import sys
import time
from pathlib import Path

import phaze.tasks._shared.model_bootstrap as mb

models_dir = Path(sys.argv[1])
log_path = Path(sys.argv[2])


def slow_download(target):
    with log_path.open("a") as fh:
        fh.write(f"start {time.monotonic()}\\n")
    time.sleep(0.2)
    with log_path.open("a") as fh:
        fh.write(f"end {time.monotonic()}\\n")
    return (0, 0)


mb.download_to = slow_download
mb.ensure_models_present(models_dir)
"""


def test_ensure_models_present_serializes_across_processes(tmp_path: Path) -> None:
    """phaze-mb8d: three concurrent OS processes never overlap inside download_to.

    Reproduces the first-boot topology (multiple lane workers, one shared models
    dir) with real subprocesses: each child patches ``download_to`` to log a
    start/end interval around a sleep, then calls ``ensure_models_present`` on
    the SAME directory. With the exclusive flock the intervals must be strictly
    serialized (start/end pairs never interleave); without it all three starts
    land before any end.
    """
    models_dir = tmp_path / "models"
    log_path = tmp_path / "intervals.log"

    procs = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", _CONCURRENT_BOOT_CHILD, str(models_dir), str(log_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(3)
    ]
    for proc in procs:
        _stdout, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, f"child worker failed: {stderr.decode()}"

    events = []
    for line in log_path.read_text().splitlines():
        kind, stamp = line.split()
        events.append((float(stamp), kind))
    events.sort()

    assert len(events) == 6, "each of the 3 workers must log exactly one start and one end"
    assert [kind for _, kind in events] == ["start", "end"] * 3, "download intervals must never overlap across processes"


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
