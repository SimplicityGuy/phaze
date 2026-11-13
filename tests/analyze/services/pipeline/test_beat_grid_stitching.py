"""Stitching per-fine-window beat ticks into one file-relative grid (phaze-0296l).

Pure Python: the stitcher takes plain float positions and imports no essentia, so these run anywhere.
The claims about REAL ``RhythmExtractor2013`` output live in ``test_beat_grid_real_essentia.py``.

Acceptance criteria and the tests that discharge them:

* grid intervals match the known tempo, no duplicates, no missing beat at a seam
  -> ``test_a_clean_click_train_split_across_windows_stitches_to_the_unsplit_grid``,
     ``test_a_beat_seen_by_two_windows_is_kept_once``, ``test_no_beat_is_dropped_at_a_seam`` (here, on the
     stitcher), and the real-essentia test of the same name stem (on real ticks);
* a silent middle window leaves a recorded gap and no interpolated beats
  -> ``test_a_silent_middle_window_is_a_recorded_gap_with_no_interpolated_beats``;
* the stored grid is strictly ascending at float32 -> ``test_the_payload_grid_is_strictly_ascending_at_stored_precision``;
* the grid is file-relative, i.e. the fine pass offsets window ticks by ``start_sec``
  -> ``test_a_fine_window_carries_its_ticks_as_file_relative_seconds``.
"""

from __future__ import annotations

from itertools import pairwise
from statistics import median
from unittest.mock import MagicMock

import numpy as np
import pytest

from phaze.services import analysis as analysis_mod
from phaze.services.analysis import _beat_grid_payload, _measure_fine_window, _owned_ticks
from phaze.services.analysis_windows import FineWindow
from phaze.services.beat_grid import BEAT_PREROLL_SEC, DUPLICATE_FRACTION, GAP_FACTOR, SEAM_TOLERANCE_SEC, stitch_beat_grid, to_stored_precision


_INTERVAL = 0.5  # 120 BPM


def _train(start: float, end: float, *, interval: float = _INTERVAL, origin: float = 0.0) -> list[float]:
    """Beats of one click train, ``origin + k * interval``, that fall in ``[start, end)``."""
    first = int(np.ceil((start - origin) / interval))
    out = []
    k = first
    while origin + k * interval < end:
        out.append(origin + k * interval)
        k += 1
    return out


def test_a_clean_click_train_split_across_windows_stitches_to_the_unsplit_grid() -> None:
    whole = _train(0.0, 120.0)
    windows = [_train(0.0, 30.0), _train(30.0, 60.0), _train(60.0, 90.0), _train(90.0, 120.0)]
    stitched = stitch_beat_grid(windows)
    assert stitched.beats == whole
    assert stitched.gaps == []


def test_a_beat_seen_by_two_windows_is_kept_once() -> None:
    # Window 0 ends on a beat at 30.0 and window 1 reports the same beat (30.01, tick jitter) first.
    first = [*_train(0.0, 30.0), 30.0]
    second = [30.01, *_train(30.5, 60.0)]
    stitched = stitch_beat_grid([first, second])
    assert stitched.beats.count(30.0) == 1
    assert 30.01 not in stitched.beats
    assert all(b - a >= DUPLICATE_FRACTION * _INTERVAL for a, b in pairwise(stitched.beats))
    assert stitched.gaps == []


def test_no_beat_is_dropped_at_a_seam() -> None:
    # Every beat of the unsplit train must survive, including the first of the second window and the
    # last of the first, with the seam falling between two beats.
    whole = _train(0.0, 90.0, origin=0.2)
    left = [b for b in whole if b < 30.0]
    right = [b for b in whole if b >= 30.0]
    stitched = stitch_beat_grid([left, right])
    assert stitched.beats == whole
    assert len(stitched.beats) == len(whole)


def test_a_silent_middle_window_is_a_recorded_gap_with_no_interpolated_beats() -> None:
    windows = [_train(0.0, 30.0), [], _train(60.0, 90.0)]  # window 1 silent: contributes nothing
    stitched = stitch_beat_grid(windows)
    assert len(stitched.gaps) == 1
    gap_start, gap_end = stitched.gaps[0]
    assert gap_start == pytest.approx(29.5)
    assert gap_end == pytest.approx(60.0)
    assert not [b for b in stitched.beats if gap_start < b < gap_end], "a beat was invented inside the gap"
    # The only interval wider than a beat period is the recorded gap itself.
    wide = [(a, b) for a, b in pairwise(stitched.beats) if b - a > GAP_FACTOR * _INTERVAL]
    assert wide == [(gap_start, gap_end)]


def test_a_failed_window_that_is_simply_absent_leaves_the_same_gap() -> None:
    stitched = stitch_beat_grid([_train(0.0, 30.0), _train(60.0, 90.0)])
    assert len(stitched.gaps) == 1


def test_beats_are_never_interpolated_across_a_wide_gap() -> None:
    stitched = stitch_beat_grid([_train(0.0, 10.0), _train(100.0, 110.0)])
    assert stitched.beats == _train(0.0, 10.0) + _train(100.0, 110.0)


def test_gap_threshold_is_relative_to_the_local_tempo_not_absolute() -> None:
    slow = _train(0.0, 60.0, interval=1.0)  # 60 BPM, local median 1.0
    stitched = stitch_beat_grid([slow[:30], slow[30:]])
    assert stitched.gaps == []  # a 1.0 s step is NOT a gap at 60 BPM although it would be one at 120

    fast = _train(0.0, 60.0, interval=0.25)
    skipped = [b for b in fast if not 20.0 < b < 22.0]  # drop 2 s of 0.25 s beats
    assert len(stitch_beat_grid([skipped]).gaps) == 1


def test_silence_before_the_first_beat_or_after_the_last_is_not_a_gap() -> None:
    assert stitch_beat_grid([[], _train(30.0, 60.0), []]).gaps == []


def test_nothing_in_nothing_out() -> None:
    stitched = stitch_beat_grid([[], []])
    assert stitched.beats == []
    assert stitched.gaps == []


def test_a_backwards_beat_is_dropped_so_the_grid_stays_strictly_ascending() -> None:
    stitched = stitch_beat_grid([[1.0, 1.5, 2.0], [1.9, 2.5]])
    assert stitched.beats == [1.0, 1.5, 2.0, 2.5]


def test_the_local_median_comes_from_the_tail_not_the_whole_file() -> None:
    # 100 beats at 1.0 s, then 30 beats at 0.6 s with one beat missing (a 1.2 s step). Against the
    # file-wide median (1.0 s) that step is under the 1.5x threshold and would go unrecorded; against the
    # LOCAL median (0.6 s) it is a gap. Only the local reading is right about the second half.
    slow = _train(0.0, 100.0, interval=1.0)
    fast = [100.0 + 0.6 * k for k in range(30) if k != 15]
    stitched = stitch_beat_grid([slow, fast])
    assert stitched.beats == slow + fast
    assert len(stitched.gaps) == 1
    assert stitched.gaps[0] == pytest.approx((100.0 + 0.6 * 14, 100.0 + 0.6 * 16))


def test_a_modest_tempo_change_across_a_seam_loses_no_beat() -> None:
    slow = _train(0.0, 30.0, interval=0.6)
    fast = _train(30.0, 60.0, interval=0.4, origin=0.0)
    stitched = stitch_beat_grid([slow, fast])
    assert stitched.beats == slow + fast
    assert stitched.gaps == []


def test_the_payload_grid_is_strictly_ascending_at_stored_precision() -> None:
    windows = [
        FineWindow(0, 0.0, 30.0, 120.0, None, 1.0, beats=tuple(_train(0.0, 30.0, origin=0.001))),
        FineWindow(1, 30.0, 60.0, 120.0, None, 1.0, beats=tuple(_train(30.0, 60.0, origin=0.001))),
    ]
    payload = _beat_grid_payload(windows)
    beats = payload["beats"]
    assert all(b > a for a, b in pairwise(beats))
    assert all(to_stored_precision(b) == b for b in beats)
    assert payload["gaps"] == []
    assert median(b - a for a, b in pairwise(beats)) == pytest.approx(_INTERVAL, abs=1e-3)


def test_the_payload_orders_windows_by_start_and_a_silent_window_leaves_a_gap() -> None:
    windows = [
        FineWindow(2, 60.0, 90.0, 120.0, None, 1.0, beats=tuple(_train(60.0, 90.0))),
        FineWindow(0, 0.0, 30.0, 120.0, None, 1.0, beats=tuple(_train(0.0, 30.0))),
        FineWindow(1, 30.0, 60.0, None, None, 0.0),  # silent: bpm None, no beats
    ]
    payload = _beat_grid_payload(windows)
    assert payload["beats"] == sorted(payload["beats"])
    assert len(payload["gaps"]) == 1
    assert payload["gaps"][0] == [pytest.approx(29.5), pytest.approx(60.0)]


def test_a_fine_window_carries_its_ticks_as_file_relative_seconds() -> None:
    rhythm = MagicMock(return_value=(120.0, np.array([0.25, 0.75, 1.25], dtype=np.float32), 3.0, None, None))
    key = MagicMock(return_value=("A", "minor", 0.9))
    audio = np.full(1000, 0.1, dtype=np.float32)
    measured = _measure_fine_window(rhythm, key, (3, 90.0, 120.0), {3: audio}, MagicMock())
    assert measured is not None
    assert measured.beats == pytest.approx((90.25, 90.75, 91.25))
    assert measured.bpm == 120.0  # the tick change did not touch the tempo


def test_a_low_confidence_window_keeps_its_bpm_but_contributes_no_beats() -> None:
    rhythm = MagicMock(return_value=(88.9, np.array([0.7, 1.4, 2.1], dtype=np.float32), 0.19, None, None))
    key = MagicMock(return_value=("A", "minor", 0.9))
    measured = _measure_fine_window(rhythm, key, (1, 30.0, 60.0), {1: np.full(1000, 0.1, dtype=np.float32)}, MagicMock())
    assert measured is not None
    assert measured.bpm == 88.9
    assert measured.beats == ()


def test_a_silent_fine_window_has_no_beats_and_never_calls_the_extractor() -> None:
    rhythm = MagicMock()
    measured = _measure_fine_window(rhythm, MagicMock(), (1, 30.0, 60.0), {1: np.zeros(1000, dtype=np.float32)}, MagicMock())
    assert measured is not None
    assert measured.beats == ()
    assert measured.bpm is None
    rhythm.assert_not_called()


def test_the_payload_dict_of_a_fine_window_is_unchanged_by_its_beats() -> None:
    window = FineWindow(0, 0.0, 30.0, 120.0, "A minor", 1.0, beats=(0.5, 1.0))
    assert set(window.as_payload_dict()) == {"tier", "window_index", "start_sec", "end_sec", "bpm", "musical_key"}


def test_the_stitcher_stays_free_of_numpy_and_essentia() -> None:
    import phaze.services.beat_grid as module

    assert not hasattr(module, "np")
    assert not hasattr(module, "essentia")
    assert analysis_mod.stitch_beat_grid is stitch_beat_grid


def test_ticks_inside_the_pre_roll_are_dropped_but_the_seam_beat_just_before_the_start_is_kept() -> None:
    # Buffer starts at 56 s (4 s pre-roll of a window starting at 60 s). Ticks at 56.5 and 59.0 are the previous
    # window's; 59.994 is the beat on the seam (ticked a quantum early) and stays; 60.5 is the window's own.
    ticks = np.array([0.5, 3.0, 3.994, 4.5], dtype=np.float32)
    assert _owned_ticks(ticks, 56.0, 60.0) == pytest.approx((59.994, 60.5))
    assert SEAM_TOLERANCE_SEC < 0.5 * _INTERVAL


def test_with_a_pre_roll_bpm_and_key_come_from_the_windows_own_samples_and_ticks_from_the_longer_buffer() -> None:
    seen: list[int] = []

    def rhythm(buf: np.ndarray) -> tuple[float, np.ndarray, float, None, None]:
        seen.append(len(buf))
        return (120.0, np.array([0.1, 4.0, 4.5], dtype=np.float32), 3.0, None, None)

    key = MagicMock(return_value=("A", "minor", 0.9))
    rate = analysis_mod._FINE_SAMPLE_RATE
    full = np.full(rate * 34, 0.1, dtype=np.float32)  # 4 s pre-roll + a 30 s window starting at 60 s
    measured = _measure_fine_window(rhythm, key, (2, 60.0, 90.0), {2: full}, MagicMock(), BEAT_PREROLL_SEC)
    assert measured is not None
    assert seen == [rate * 30, rate * 34]  # window samples first (bpm), then the pre-rolled buffer (ticks)
    assert len(key.call_args.args[0]) == rate * 30
    assert measured.bpm == 120.0
    assert measured.beats == pytest.approx((60.0, 60.5))  # 56.1 is in the pre-roll and dropped


def test_the_first_window_has_no_pre_roll_and_one_tracker_call() -> None:
    calls = MagicMock(return_value=(120.0, np.array([0.5], dtype=np.float32), 3.0, None, None))
    measured = _measure_fine_window(
        calls, MagicMock(return_value=("A", "minor", 0.9)), (0, 0.0, 30.0), {0: np.full(1000, 0.1, dtype=np.float32)}, MagicMock(), BEAT_PREROLL_SEC
    )
    assert measured is not None
    assert calls.call_count == 1
    assert measured.beats == pytest.approx((0.5,))


# phaze-7hcgn.4 finding 1: an octave jump or drop between windows (RhythmExtractor2013 octave errors, or a real
# tempo change of more than 2x) must neither thin the grid nor record fake gaps. Each window's ticks come from one
# tracker run, so the window's OWN spacing is the reference inside it; the accepted tail matters only at the seam.


def test_an_octave_jump_up_keeps_every_beat_of_the_faster_window() -> None:
    # 120 BPM, then the tracker reports 260 BPM (a jump of more than 2x): every fast beat is a beat the tracker gave.
    slow = _train(0.0, 30.0, interval=0.5)
    fast = _train(30.0, 60.0, interval=60.0 / 260.0, origin=30.0)
    later = _train(60.0, 90.0, interval=60.0 / 260.0, origin=60.0)
    stitched = stitch_beat_grid([slow, fast, later])
    assert stitched.beats == slow + fast + later, "the grid was thinned after the jump"
    assert stitched.gaps == []


def test_an_octave_drop_records_no_fake_gaps() -> None:
    # 128 BPM, then the tracker halves to 60 BPM: the 1.0 s steps are the new tempo, not missing beats.
    fast = _train(0.0, 30.0, interval=60.0 / 128.0)
    slow = _train(30.0, 60.0, interval=1.0, origin=30.0)
    stitched = stitch_beat_grid([fast, slow])
    assert stitched.beats == fast + slow
    assert stitched.gaps == [], "a slower window's own spacing was recorded as gaps"


def test_a_real_gap_inside_a_window_after_an_octave_drop_is_still_recorded() -> None:
    # The window's own spacing is the reference, so a hole in the slow window is a gap against 1.0 s, not 0.47 s.
    fast = _train(0.0, 30.0, interval=60.0 / 128.0)
    slow = [b for b in _train(30.0, 60.0, interval=1.0, origin=30.0) if not 40.5 < b < 44.5]
    stitched = stitch_beat_grid([fast, slow])
    assert stitched.gaps == [(40.0, 45.0)]


def test_a_beat_seen_from_both_sides_of_an_octave_seam_is_kept_once() -> None:
    slow = [*_train(0.0, 30.0, interval=0.5), 30.0]
    fast = [30.004, *_train(30.2, 60.0, interval=0.2, origin=30.0)]
    stitched = stitch_beat_grid([slow, fast])
    assert stitched.beats.count(30.0) == 1
    assert 30.004 not in stitched.beats
    assert stitched.gaps == []


def test_a_window_the_pre_rolled_decode_skips_is_reported_with_its_own_span(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # phaze-7hcgn.4 finding 6: the decode is handed spans that start BEAT_PREROLL_SEC early, and reports a skipped
    # window with the span it was given. The log line and the skip must still name the window's own [start, end).
    seen: list[tuple[int, float, float]] = []

    def decode(_path: str, _rate: int, spans: list[tuple[int, float, float]], on_skip: object, **_kwargs: object) -> dict[int, object]:
        seen.extend(spans)
        for idx, start, end in spans:
            on_skip(idx, start, end, False)  # type: ignore[operator]
        return {}

    monkeypatch.setattr(analysis_mod, "_decode_windows", decode)
    with caplog.at_level("WARNING", logger=analysis_mod.log.name):
        windows, total = analysis_mod._analyze_fine_windows("unused.wav", 90.0, 30, 15)
    assert (windows, total) == ([], 3)
    assert seen[1] == (1, 30.0 - BEAT_PREROLL_SEC, 60.0)  # the decode really is pre-rolled
    messages = [r.getMessage() for r in caplog.records if "failed; skipping" in r.getMessage()]
    assert messages == [
        "fine window 0 [0.0, 30.0) failed; skipping",
        "fine window 1 [30.0, 60.0) failed; skipping",
        "fine window 2 [60.0, 90.0) failed; skipping",
    ]
