"""The storage geometry phaze-f6eeb fixed for sub-window mood and the beat grid.

docs/design/0026-subwindow-mood-and-beat-grid.md rests on four measured facts. Each one is pinned
here, so the decision record cites a test instead of a one-off run:

1. The patch geometry of the deployed mood graphs: musicnn (msd and mtt) and VGGish each take
   93-frame hops, which is 1.488 s and 0.93 s. Checked against REAL graphs on real buffers. The
   patch count of a buffer is a closed form of its length, and a hop one frame off would give a
   different count at 180 s.
2. :func:`iter_subwindows` lays ``ceil(len / SUBWINDOW_SEC)`` slices from each coarse window's own
   start, and 15 s divides both default window lengths.
3. The writer stores beats at float32 precision, strictly ascending, and a value already at stored
   precision is unchanged by it.
4. float32 is enough: real ``RhythmExtractor2013`` ticks are float32 on a 512-sample grid at
   44.1 kHz (11.61 ms), and float32 rounding at the longest analyzed file (12.07 h) moves a position
   by at most 1.95 ms.
"""

from __future__ import annotations

import itertools
import math
import os
from pathlib import Path
import struct

import pytest

from phaze.services.analysis_windows import SUBWINDOW_SEC, iter_subwindows
from phaze.services.beat_grid import stored_beat_grid, to_stored_precision
from tests._real_models import ENV_VAR as _REAL_MODELS_ENV_VAR


_REAL_MODELS_DIR = os.environ.get(_REAL_MODELS_ENV_VAR) or None
_LONGEST_FILE_SEC = 12.07 * 3600  # docs/design/0002-fingerprint-removal.md, measured 2026-07-28

requires_real_models = pytest.mark.skipif(
    _REAL_MODELS_DIR is None,
    reason=f"REAL-MODEL PATCH GEOMETRY NOT RUN (a skip, not a pass): {_REAL_MODELS_ENV_VAR} is unset or empty",
)


# --- 2. sub-window layout ---------------------------------------------------------------------


def test_15_s_divides_both_default_window_lengths() -> None:
    from phaze.services.analysis import _DEFAULT_COARSE_WINDOW_SEC, _DEFAULT_FINE_WINDOW_SEC

    assert SUBWINDOW_SEC == 15
    assert _DEFAULT_COARSE_WINDOW_SEC % SUBWINDOW_SEC == 0
    assert _DEFAULT_FINE_WINDOW_SEC % SUBWINDOW_SEC == 0


def test_a_full_coarse_window_tiles_into_twelve_contiguous_sub_windows() -> None:
    slices = iter_subwindows(360.0, 540.0)
    assert [index for index, _, _ in slices] == list(range(12))
    assert [(start, end) for _, start, end in slices] == [(360.0 + 15 * i, 375.0 + 15 * i) for i in range(12)]


@pytest.mark.parametrize("span", [0.5, 1.0, 14.999, 15.0, 15.001, 29.5, 100.0, 179.9, 180.0])
def test_every_coarse_window_yields_ceil_span_over_sub_sec_contiguous_slices(span: float) -> None:
    start = 7200.0
    slices = iter_subwindows(start, start + span)
    assert len(slices) == math.ceil(span / SUBWINDOW_SEC)
    assert slices[0][1] == start
    assert slices[-1][2] == start + span
    for (_, _, previous_end), (_, next_start, _) in itertools.pairwise(slices):
        assert next_start == previous_end
    assert all(end > begin for _, begin, end in slices)


def test_slices_are_laid_from_the_coarse_window_start_not_from_file_zero() -> None:
    # A coarse_window_sec that is not a multiple of 15: the second window starts at 100 s, so its slices
    # must too. A file-zero grid would start the second window's first slice at 90 s, inside window 0.
    assert [(start, end) for _, start, end in iter_subwindows(100.0, 200.0)][:2] == [(100.0, 115.0), (115.0, 130.0)]
    assert iter_subwindows(100.0, 200.0)[-1][1:] == (190.0, 200.0)


def test_an_empty_or_inverted_window_has_no_slices_and_a_non_positive_length_is_refused() -> None:
    assert iter_subwindows(30.0, 30.0) == []
    assert iter_subwindows(30.0, 20.0) == []
    with pytest.raises(ValueError, match="sub_sec must be positive"):
        iter_subwindows(0.0, 180.0, sub_sec=0)


# --- 3. beat-grid stored precision ----------------------------------------------------------


def test_stored_precision_is_float32_and_idempotent() -> None:
    value = 43200.371519274
    stored = to_stored_precision(value)
    assert stored != value
    assert struct.unpack("<f", struct.pack("<f", value))[0] == stored
    assert to_stored_precision(stored) == stored


def test_a_grid_is_rounded_beat_by_beat_and_keeps_its_order() -> None:
    raw = [0.3715 + i * 0.46875 for i in range(1000)] + [_LONGEST_FILE_SEC]
    stored = stored_beat_grid(raw)
    assert stored == [to_stored_precision(beat) for beat in raw]
    assert stored_beat_grid(stored) == stored
    assert stored_beat_grid([]) == []


@pytest.mark.parametrize(
    "beats",
    [[1.0, 1.0], [2.0, 1.0], [43200.0, 43200.0001]],
    ids=["duplicate", "backwards", "collapses-at-float32"],
)
def test_a_grid_that_is_not_strictly_ascending_at_stored_precision_is_refused(beats: list[float]) -> None:
    with pytest.raises(ValueError, match="strictly ascending"):
        stored_beat_grid(beats)


def test_a_whole_grid_is_rounded_in_one_pass_not_beat_by_beat(monkeypatch: pytest.MonkeyPatch) -> None:
    # phaze-7hcgn.4 finding 10: BeatGridPayload validates up to 600,000 beats synchronously in the request path. The
    # grid is rounded in one C-level pass, with exactly the values the per-beat struct round trip gives.
    import phaze.services.beat_grid as module

    raw = [0.3715 + i * 0.46875 for i in range(600_000)]
    expected = [struct.unpack("<f", struct.pack("<f", beat))[0] for beat in raw]

    def per_beat(_seconds: float) -> float:
        raise AssertionError("rounded beat by beat")

    monkeypatch.setattr(module, "to_stored_precision", per_beat)
    assert stored_beat_grid(raw) == expected


def test_a_beat_beyond_float32_range_is_refused_as_a_value_error() -> None:
    # struct.pack raised OverflowError here, which a pydantic validator does not turn into a 422.
    with pytest.raises(ValueError, match="float32"):
        stored_beat_grid([1.0, 1e39])


def test_float32_moves_a_position_by_at_most_two_milliseconds_up_to_the_longest_file() -> None:
    # The spacing of float32 values below 2**16 s is 2**-8 s, so rounding is at most half of that.
    worst = max(abs(to_stored_precision(t) - t) for t in (_LONGEST_FILE_SEC * k / 997 + 0.0001234 for k in range(998)))
    assert worst <= 2**-9  # 1.953 ms


# --- 1 and 4. real essentia ------------------------------------------------------------------

# (frame_size, frame_hop) in samples at 16 kHz, and (patch_frames, hop_frames), per classifier type.
_GEOMETRY = {"musicnn": ((512, 256), (187, 93)), "vggish": ((400, 160), (96, 93))}


def _expected_patches(samples: int, classifier_type: str) -> int:
    (frame_size, frame_hop), (patch, hop) = _GEOMETRY[classifier_type]
    frames = (samples - frame_size) // frame_hop + 1
    return (frames - patch) // hop + 1


def test_the_stated_hops_in_seconds_follow_from_the_frame_geometry() -> None:
    (_, musicnn_frame_hop), (musicnn_patch, musicnn_hop) = _GEOMETRY["musicnn"]
    (_, vggish_frame_hop), (vggish_patch, vggish_hop) = _GEOMETRY["vggish"]
    assert (musicnn_hop * musicnn_frame_hop / 16000, musicnn_patch * musicnn_frame_hop / 16000) == (1.488, 2.992)
    assert (vggish_hop * vggish_frame_hop / 16000, vggish_patch * vggish_frame_hop / 16000) == (0.93, 0.96)


@requires_real_models
@pytest.mark.parametrize("variant", ["musicnn_msd", "musicnn_mtt", "vggish"])
def test_each_deployed_mood_graph_emits_patches_on_the_stated_hop(variant: str) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("essentia")
    from phaze.services import analysis
    from phaze.services.analysis_models import FULL_MODEL_SETS, _resolve_tf_batch_size

    mood_happy = next(group for group in FULL_MODEL_SETS if group.name == "mood_happy")
    model = next(candidate for candidate in mood_happy.models if candidate.variant == variant)
    graph = str(Path(str(_REAL_MODELS_DIR)) / f"{model.filename}.pb")
    classifier = analysis._construct_classifier(model, graph, _resolve_tf_batch_size(model))
    rng = np.random.default_rng(20261110)
    try:
        for seconds in (30.0, 60.0, 179.5, 180.0):
            samples = int(seconds * 16000)
            activations = np.atleast_2d(classifier((0.1 * rng.standard_normal(samples)).astype(np.float32)))
            assert activations.shape[0] == _expected_patches(samples, model.classifier_type), (variant, seconds)
    finally:
        del classifier


def test_the_expected_patch_count_discriminates_a_hop_one_frame_off() -> None:
    samples = 180 * 16000
    for classifier_type, (frame, (patch, hop)) in _GEOMETRY.items():
        frames = (samples - frame[0]) // frame[1] + 1
        assert (frames - patch) // (hop + 1) + 1 != _expected_patches(samples, classifier_type)


def test_real_beat_ticks_are_float32_on_the_512_sample_grid_at_44_1_khz() -> None:
    np = pytest.importorskip("numpy")
    es = pytest.importorskip("essentia.standard")
    sample_rate, period = 44100, 60 / 128
    audio = np.zeros(sample_rate * 30, dtype=np.float32)
    for beat in range(int(29.5 / period)):
        at = round((0.25 + beat * period) * sample_rate)
        audio[at : at + 200] += 0.8 * np.hanning(200).astype(np.float32)
    _, ticks, _, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
    ticks = np.asarray(ticks)
    assert ticks.dtype == np.float32
    assert len(ticks) > 50
    frames = ticks * sample_rate / 512
    assert float(np.max(np.abs(frames - np.round(frames)))) < 0.01
