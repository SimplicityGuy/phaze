"""The beat grid from REAL ``RhythmExtractor2013`` ticks on a synthetic click track (phaze-0296l).

CLAUDE.md rule 3 / ADR-0012 (verification fidelity) rule 3: a claim about essentia's output is discharged by essentia, not a
mock. The stitcher's own arithmetic is pinned in ``test_beat_grid_stitching.py``; this file feeds it
what the deployed extractor really returns, through the real fine pass (``_analyze_fine_windows``:
real decode, real chunking, real extractor), so the seams under test are the production seams.

Fixture: a mono 16-bit WAV, a metronome click every 0.5 s (120 BPM) over a steady tone, 100 s long, so
three full 30 s fine windows plus a 10 s remainder that the fine pass drops (below ``fine_min_sec``).
Generated from ``math`` alone, no RNG and no ffmpeg, so the bytes are the same on every machine.

STATED TOLERANCES (the acceptance criterion asks for them stated):

* tempo: the median stitched interval is within ``_TEMPO_TOL`` (3 %) of 0.5 s. The extractor's tick
  grid is 11.61 ms (2.3 % of a beat), so a tolerance below that would be testing quantization.
* duplicates: no interval is shorter than ``_MIN_FRACTION`` (0.6) of a beat.
* missing beats: none, and no gap recorded. The extractor never ticks a beat on its buffer's first
  samples, so each window is decoded ``BEAT_PREROLL_SEC`` early (``services/beat_grid.py`` states the
  measurement); ``test_without_the_pre_roll_the_same_seams_lose_a_beat`` shows the loss without it.
"""

from __future__ import annotations

from itertools import pairwise
import math
from statistics import median
import struct
from typing import TYPE_CHECKING
import wave

import pytest

from phaze.services.analysis import _analyze_fine_windows, _beat_grid_payload


if TYPE_CHECKING:
    from pathlib import Path


_RATE = 8000  # both tiers resample from here, so the real Resample stage is in the path
_BEAT = 0.5
_TEMPO_TOL = 0.03
_MIN_FRACTION = 0.6
_MAX_FRACTION = 1.4  # the clean click track has no real gaps
_FINE_WINDOW = 30
_FINE_MIN = 15
_SECONDS = 100


def _write_click_track(path: Path, *, silent: tuple[float, float] | None = None) -> None:
    period = round(_RATE * _BEAT)
    frames = bytearray()
    for index in range(_SECONDS * _RATE):
        elapsed = index / _RATE
        if silent is not None and silent[0] <= elapsed < silent[1]:
            frames += struct.pack("<h", 0)
            continue
        value = 0.25 * math.sin(2.0 * math.pi * 220.0 * elapsed)
        phase = index % period
        if phase < 64:
            value += 0.6 * math.exp(-phase / 12.0)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_RATE)
        handle.writeframes(bytes(frames))


@pytest.fixture(scope="module")
def click_track(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("beat_grid_real") / "click_120bpm.wav"
    _write_click_track(path)
    return path


@pytest.fixture(scope="module")
def silent_middle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("beat_grid_real_silent") / "click_silent_middle.wav"
    _write_click_track(path, silent=(30.0, 60.0))
    return path


def _grid(path: Path) -> tuple[dict, list]:
    windows, total = _analyze_fine_windows(str(path), float(_SECONDS), _FINE_WINDOW, _FINE_MIN)
    assert total == len(windows) == 3, "the fixture must span three fine windows"
    return _beat_grid_payload(windows), windows


@pytest.mark.integration
def test_real_ticks_stitch_to_a_grid_at_the_known_tempo_with_no_duplicate_and_no_missing_beat(click_track: Path) -> None:
    payload, windows = _grid(click_track)
    beats = payload["beats"]
    assert len(beats) >= 178  # 90 s at 2 beats/s is 180; only the very first beat (no pre-roll before t=0) may be lost
    intervals = [b - a for a, b in pairwise(beats)]

    assert median(intervals) == pytest.approx(_BEAT, rel=_TEMPO_TOL)
    assert min(intervals) >= _MIN_FRACTION * _BEAT, "a beat was doubled"
    assert max(intervals) <= _MAX_FRACTION * _BEAT, "a beat is missing"
    assert payload["gaps"] == []
    assert beats == sorted(set(beats)), "not strictly ascending"
    assert all(w.beats for w in windows)


@pytest.mark.integration
@pytest.mark.parametrize("seam", [30.0, 60.0])
def test_real_ticks_lose_and_double_nothing_at_each_seam(click_track: Path, seam: float) -> None:
    """With the pre-roll, the beat on the seam itself is present (it is the one the tracker missed before)."""
    beats = _grid(click_track)[0]["beats"]
    assert any(abs(b - seam) < 0.05 for b in beats), f"no beat at the {seam} s seam"
    near = [b for b in beats if seam - 2.0 <= b <= seam + 2.0]
    steps = [b - a for a, b in pairwise(near)]
    assert len(near) >= 7
    assert all(_MIN_FRACTION * _BEAT <= st <= _MAX_FRACTION * _BEAT for st in steps), (seam, steps)


@pytest.mark.integration
def test_without_the_pre_roll_the_same_seams_lose_a_beat(click_track: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control that gives the test above teeth: the SAME code with pre-roll disabled loses the seam beat."""
    import phaze.services.analysis as analysis_mod

    monkeypatch.setattr(analysis_mod, "BEAT_PREROLL_SEC", 0.0)
    payload, _ = _grid(click_track)
    assert max(b - a for a, b in pairwise(payload["beats"])) > _MAX_FRACTION * _BEAT


@pytest.mark.integration
def test_a_silent_middle_window_is_a_recorded_gap_and_no_beat_is_invented_in_it(silent_middle: Path) -> None:
    payload, windows = _grid(silent_middle)
    # The window is not digitally silent -- the decode leaks a few hundred edge samples across it -- so the
    # extractor still reports a tempo, at very low confidence. Its ticks are not trusted.
    assert windows[1].confidence < 1.0
    assert windows[1].beats == ()
    assert len(payload["gaps"]) == 1
    gap_start, gap_end = payload["gaps"][0]
    assert gap_start <= 31.0
    assert gap_end >= 59.0
    # The click that resumes at exactly 60.0 is ticked a quantum early (59.99x), so the interior stops short of it.
    assert not [b for b in payload["beats"] if 30.0 <= b < 59.9], "a beat exists inside the silent window"
    # Both sides still hold their beats.
    assert any(b < 30.0 for b in payload["beats"])
    assert any(b >= 60.0 for b in payload["beats"])


@pytest.mark.integration
def test_fine_window_bpm_and_key_are_unchanged_by_keeping_the_ticks(click_track: Path) -> None:
    windows, _ = _analyze_fine_windows(str(click_track), float(_SECONDS), _FINE_WINDOW, _FINE_MIN)
    # The ticks are an extra output of the SAME extractor call: the window's bpm is that call's bpm.
    assert all(w.bpm == pytest.approx(120.0, abs=3.0) for w in windows)
    assert windows[0].start_sec == 0.0 and windows[1].start_sec == 30.0
