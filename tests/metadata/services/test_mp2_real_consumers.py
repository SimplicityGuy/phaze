"""phaze-f78n6: a real ``.mp2`` file through the REAL consumers, not the tool that wrote it.

``.mp2`` (MPEG-1 Layer II) was missing from ``EXTENSION_MAP``, so it was never ingested. Admitting a
format the pipeline cannot process would only move the failure downstream, so these tests hand an
ffmpeg-encoded ``.mp2`` to the two consumers that matter:

* metadata extraction -- ``extract_tags`` (mutagen), strict, so an unreadable file raises;
* the analysis decode path -- the ffprobe duration probe plus the streaming essentia decode at BOTH
  tier sample rates, the code ``analyze_file`` runs for every file.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404  # fixed-argv ffmpeg fixture encode
from typing import TYPE_CHECKING

from mutagen.id3 import ID3, TALB, TIT2, TPE1
import numpy as np
import pytest

from phaze.services.analysis import _COARSE_SAMPLE_RATE, _FINE_SAMPLE_RATE, _decode_windows, analyze_file
from phaze.services.analysis_probe import _probe_duration_sec
from phaze.services.metadata import extract_tags


if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required to encode the .mp2 fixture")

_DURATION_SEC = 3


def _encode_mp2(path: Path, duration_sec: int) -> None:
    subprocess.run(  # nosec B603  # noqa: S603  # fixed argv, no shell
        [
            str(shutil.which("ffmpeg")),
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_sec}",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "mp2",
            str(path),
        ],
        check=True,
    )


@pytest.fixture
def mp2_file(tmp_path: Path) -> Path:
    """A 3 s stereo 440 Hz sine encoded as MPEG-1 Layer II, tagged with ID3v2 as real .mp2 files are."""
    path = tmp_path / "song.mp2"
    _encode_mp2(path, _DURATION_SEC)
    tags = ID3()
    tags.add(TPE1(encoding=3, text="Nova Ryn"))
    tags.add(TIT2(encoding=3, text="Dusk"))
    tags.add(TALB(encoding=3, text="Nightgrove"))
    tags.save(str(path))
    return path


def test_mutagen_extracts_duration_and_bitrate_from_real_mp2(mp2_file: Path) -> None:
    tags = extract_tags(str(mp2_file), strict=True)

    # The tags are what feeds filename proposals: they must survive extraction from a .mp2.
    assert (tags.artist, tags.title, tags.album) == ("Nova Ryn", "Dusk", "Nightgrove")
    assert tags.duration == pytest.approx(_DURATION_SEC, abs=0.1)
    assert tags.bitrate is not None
    assert tags.bitrate > 0


def test_analysis_decode_path_reads_real_mp2_at_both_tier_rates(mp2_file: Path) -> None:
    assert _probe_duration_sec(str(mp2_file)) == pytest.approx(_DURATION_SEC, abs=0.1)

    for rate in (_FINE_SAMPLE_RATE, _COARSE_SAMPLE_RATE):
        skipped: list[tuple[int, float, float, bool]] = []
        decoded = _decode_windows(str(mp2_file), rate, [(0, 0.0, 1.0), (1, 1.0, 2.0)], lambda *args, sink=skipped: sink.append(args))

        assert skipped == []
        assert set(decoded) == {0, 1}
        for buffer in decoded.values():
            assert len(buffer) == rate
            # Real audio, not silence: the sine survives decode + resample.
            assert float(np.abs(buffer).max()) > 0.05


@pytest.mark.skipif(not os.environ.get("PHAZE_TEST_MODELS_DIR"), reason="needs the real essentia model set; set PHAZE_TEST_MODELS_DIR")
def test_analyze_file_end_to_end_on_real_mp2(tmp_path: Path) -> None:
    """The real ``analyze_file`` yields windows and model outputs for a .mp2 -- never the zero-window class (phaze-3ea41)."""
    path = tmp_path / "long.mp2"
    _encode_mp2(path, 70)

    result = analyze_file(str(path), os.environ["PHAZE_TEST_MODELS_DIR"])

    assert result["fine_windows_analyzed"] >= 1
    assert result["coarse_windows_analyzed"] >= 1
    assert result["fine_windows_analyzed"] == result["fine_windows_total"]
    assert result["coarse_windows_analyzed"] == result["coarse_windows_total"]
    assert result["windows"]
    assert result["features"]
    assert result["bpm"] > 0
    assert result["mood"]
    assert result["musical_key"]
