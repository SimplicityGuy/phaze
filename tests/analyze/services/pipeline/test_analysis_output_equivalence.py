"""Output-equivalence harness for `analyze_file` -- written BEFORE the phaze-bk9el.15 refactor.

WHAT THIS IS. Not a correctness test and not a replacement for anything. It runs the REAL
``analyze_file`` over fixed, in-repo-reproducible audio and compares the WHOLE returned dict
against a recording made on the unmodified tree at the phaze-bk9el.1 baseline. It is the
discharge of phaze-bk9el.15's "the split moves code; it does not modify it" criterion under
CLAUDE.md rule 1.

REAL ESSENTIA, NOT A MOCK. Every number below comes from ``es.RhythmExtractor2013`` and
``es.KeyExtractor`` running on real decoded PCM through the real streaming decode network. That
is not a stylistic preference: CLAUDE.md records that the pre-existing long-file test
"proves the claim of a mocked essentia only and always did", and that gap is how D-09 shipped
broken once. A mocked harness here would pin the mock, not the pipeline.

THIS ADDS TO THE D-09 GUARDS, IT DOES NOT REPLACE THEM.
``test_analysis_streaming_decode.py``'s ``test_repeated_gated_chunk_decodes_do_not_grow_peak_rss``
and ``test_the_chunk_decode_leaves_no_connected_network_behind`` remain the memory-invariant
guards and must keep passing unmodified. They assert a RESOURCE property; this file asserts an
OUTPUT property. Neither substitutes for the other, and a refactor could break either alone.

WHAT IS COVERED, PRECISELY -- and what is not.

COVERED, end to end on real essentia: ``_probe_duration_sec``, ``_iter_windows`` (including the
short-trailing-window drop rule and its off-by-one edges), the chunked streaming fine decode,
``RhythmExtractor2013`` + ``KeyExtractor`` per window, the per-window rounding, the
``FineWindow.as_payload_dict`` payload shape and key set, ``aggregate_bpm`` /
``aggregate_key``, the coarse tier's window ENUMERATION (``coarse_windows_total``), the
per-window failure isolation that lets a file complete when the coarse sweep cannot run, and
the exact key set and ordering of the returned dict.

NOT COVERED, stated plainly rather than papered over: the coarse tier's TENSORFLOW MODEL SWEEP.
The 34-graph model set is not in this repository and is not present in CI, so
``_get_classifier`` raises and every coarse window is skipped -- which is why
``coarse_windows_analyzed`` is 0 and ``mood``/``style``/``danceability``/``features`` are empty
in the recordings. Supplying a fake models_dir or mocking ``TensorflowPredict*`` would produce a
harness that LOOKS like it covers the coarse tier while proving nothing about it -- exactly the
discharge-by-proxy this bead exists to prevent -- so it is not done. The pure-Python reductions
that consume the sweep's output (``derive_mood`` / ``derive_style`` / ``derive_danceability`` /
``_positive_class_prediction`` / ``aggregate_dominant`` / ``aggregate_danceability`` /
``_representative_features``) ARE pinned below over recorded inputs; they carry no essentia
claim and are labelled as such. What remains genuinely unpinned is the model-inference step
itself, and closing that needs the model set on a real node -- a separate bead, not a weaker
test here.

THE FIXTURES are generated in-process from pure Python (``wave`` + ``struct``, no numpy RNG, no
ffmpeg in the write path), so the input bytes are identical on every machine and every run --
the property the recording depends on. Four shapes: an exact window multiple, a trailing window
below ``fine_min_sec`` (dropped), a trailing window above it (kept), and a file shorter than one
window.

PORTABILITY OF THE RECORDING. Per-window BPM is ``round(float(bpm), 1)`` in production and
``aggregate_bpm`` rounds to 0.1 as well, so a last-bit float difference between architectures
does not move these values; keys are strings. If this test nonetheless goes red on a platform
where nothing was refactored, that is a finding to investigate and report, not a reason to
loosen the comparison or re-record the golden.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import struct
from typing import Any
import wave

import pytest

from phaze.services.analysis import (
    CoarseWindow,
    FineWindow,
    _positive_class_prediction,
    _representative_features,
    aggregate_bpm,
    aggregate_danceability,
    aggregate_dominant,
    aggregate_key,
    analyze_file,
    derive_danceability,
    derive_mood,
    derive_style,
)


_GOLDEN_PATH = Path(__file__).with_name("analysis_output_equivalence_golden.json")

# A cheap source rate: both tiers resample away from it, so the real Resample stage is in the
# signal path rather than being skipped as a no-op.
_SOURCE_RATE = 8000

# The production defaults, spelled explicitly so the recording cannot silently change meaning
# if the AgentSettings defaults move. If those defaults are what changed, this file failing is
# the correct outcome -- the analysis OUTPUT for a given file would have changed.
_FINE_WINDOW_SEC = 30
_COARSE_WINDOW_SEC = 180
_FINE_MIN_SEC = 15

# name -> (seconds, bpm, tone_hz). The four shapes that exercise every branch of the window
# enumeration:
#   * exact multiple of the fine window          -> no trailing window at all
#   * trailing remainder BELOW fine_min_sec      -> dropped (100 s = 3x30 + 10)
#   * trailing remainder ABOVE fine_min_sec      -> kept   (105 s = 3x30 + 15)
#   * shorter than one whole window              -> window 0 is kept regardless of the floor
# Tone frequencies are exact equal-temperament pitches so the key detector has an unambiguous
# tonic and the recorded key is a stable string rather than a coin flip between neighbours.
_FIXTURES: dict[str, tuple[int, int, float]] = {
    "exact_windows_90s_120bpm_a220": (90, 120, 220.0),
    "short_trailing_dropped_100s_90bpm_c262": (100, 90, 261.6255653005986),
    "kept_trailing_105s_150bpm_e330": (105, 150, 329.6275569128699),
    "single_window_25s_120bpm_a220": (25, 120, 220.0),
}


def _golden() -> dict[str, Any]:
    with _GOLDEN_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_wav(path: Path, *, seconds: int, bpm: int, tone_hz: float) -> None:
    """Write a byte-deterministic mono 16-bit WAV: a steady tone plus a metronome click.

    Every sample is computed from ``math.sin``/``math.exp`` on exact rationals of the sample
    index -- no RNG, no ffmpeg, no floating-point accumulation across samples -- so the file's
    bytes are identical on every machine. The click train is what makes the BPM detectable and
    the sustained tone is what gives the key detector a tonic; both are needed or the recorded
    values would be noise-driven and therefore not reproducible.
    """
    period = round(_SOURCE_RATE * 60.0 / bpm)
    frames = bytearray()
    for index in range(seconds * _SOURCE_RATE):
        elapsed = index / _SOURCE_RATE
        value = 0.25 * math.sin(2.0 * math.pi * tone_hz * elapsed)
        phase = index % period
        if phase < 64:
            value += 0.6 * math.exp(-phase / 12.0)
        value = max(-1.0, min(1.0, value))
        frames += struct.pack("<h", int(value * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SOURCE_RATE)
        handle.writeframes(bytes(frames))


@pytest.fixture(scope="module")
def fixture_audio(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The four generated WAVs, written once per module (generation is the slow part)."""
    directory = tmp_path_factory.mktemp("analysis_output_equivalence")
    paths = {}
    for name, (seconds, bpm, tone_hz) in _FIXTURES.items():
        path = directory / f"{name}.wav"
        _write_wav(path, seconds=seconds, bpm=bpm, tone_hz=tone_hz)
        paths[name] = path
    return paths


@pytest.fixture(scope="module")
def empty_models_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A models_dir with no graphs in it.

    Deliberately empty rather than mocked. ``_get_classifier`` raises on the missing ``.pb``,
    each coarse window is logged and skipped by the existing per-window failure isolation, and
    the file still completes -- which is itself a behaviour worth pinning: it is the path a
    production agent takes when a model file is missing, and it is the reason a partial failure
    is a partial success rather than a file-level one (phaze-zibn).
    """
    return tmp_path_factory.mktemp("analysis_output_equivalence_models")


@pytest.fixture(scope="module")
def analysis_results(fixture_audio: dict[str, Path], empty_models_dir: Path) -> dict[str, Any]:
    """Run the real pipeline once per fixture; every test below reads these results."""
    return {
        name: analyze_file(
            str(path),
            str(empty_models_dir),
            fine_window_sec=_FINE_WINDOW_SEC,
            coarse_window_sec=_COARSE_WINDOW_SEC,
            fine_min_sec=_FINE_MIN_SEC,
        )
        for name, path in fixture_audio.items()
    }


@pytest.mark.parametrize("fixture_name", sorted(_FIXTURES), ids=sorted(_FIXTURES))
def test_analysis_output_matches_the_recorded_baseline(analysis_results: dict[str, Any], fixture_name: str) -> None:
    """The WHOLE returned dict, compared to the recording, for one fixture.

    Whole-dict rather than field-by-field on purpose: a refactor that drops a key, adds one,
    renames one, or reorders the flat ``windows`` list (fine windows first, then coarse -- the
    order downstream ``AnalysisWindowPayload(**w)`` consumption relies on) fails here. Asserting
    only the aggregates would miss every one of those.
    """
    assert analysis_results[fixture_name] == _golden()[fixture_name]


def test_analysis_result_key_set_is_unchanged(analysis_results: dict[str, Any]) -> None:
    """The top-level contract keys, asserted separately from their values.

    Separate because the failure reads differently: a value mismatch is "the analysis changed",
    a key-set mismatch is "the contract with ``AnalysisWindowPayload`` and the completion PUT
    changed", and the second is the one that breaks callers rather than numbers.
    """
    expected = {
        "bpm",
        "coarse_windows_analyzed",
        "coarse_windows_total",
        "danceability",
        "features",
        "fine_windows_analyzed",
        "fine_windows_total",
        "mood",
        "musical_key",
        "style",
        "windows",
    }
    for name, result in analysis_results.items():
        assert set(result) == expected, name


def test_fine_window_payload_key_set_is_unchanged(analysis_results: dict[str, Any]) -> None:
    """Every fine window dict carries exactly the payload keys, no more and no fewer."""
    expected = {"tier", "window_index", "start_sec", "end_sec", "bpm", "musical_key"}
    for name, result in analysis_results.items():
        for window in result["windows"]:
            assert set(window) == expected, name


def test_analysis_is_reproducible_within_a_run(fixture_audio: dict[str, Path], empty_models_dir: Path, analysis_results: dict[str, Any]) -> None:
    """A second run over the same bytes returns the identical dict.

    The recording above is only meaningful if the pipeline is deterministic to begin with. This
    proves that independently of the golden file, so a failure of the recorded comparison can be
    read as "the behaviour changed" rather than "essentia is noisy".
    """
    name = "exact_windows_90s_120bpm_a220"
    repeat = analyze_file(
        str(fixture_audio[name]),
        str(empty_models_dir),
        fine_window_sec=_FINE_WINDOW_SEC,
        coarse_window_sec=_COARSE_WINDOW_SEC,
        fine_min_sec=_FINE_MIN_SEC,
    )
    assert repeat == analysis_results[name]


# ---------------------------------------------------------------------------
# The pure reductions the coarse sweep feeds.
#
# NO ESSENTIA CLAIM IS ATTACHED TO ANYTHING BELOW. These are pure Python functions pinned over
# hand-written inputs -- the model INFERENCE that produces real inputs for them is the gap named
# in this module's docstring and is not closed here. They are pinned anyway because they are
# inside phaze-bk9el.15's blast radius, they are pure (so a table IS a complete characterization
# of them), and the alternative -- leaving the whole coarse half unpinned because its top layer
# cannot be reached in CI -- would be strictly worse.
# ---------------------------------------------------------------------------


# Two variants per mood set, with the classes ordered ALPHABETICALLY the way essentia emits them
# -- so the negative class is first for `mood_sad`, which is the exact ordering trap
# `_positive_class_prediction` exists for.
_MOOD_FEATURES = {
    "mood_happy": {
        "a": [{"label": "happy", "prediction": 0.90}, {"label": "non_happy", "prediction": 0.10}],
        "b": [{"label": "happy", "prediction": 0.80}, {"label": "non_happy", "prediction": 0.20}],
    },
    "mood_sad": {
        "a": [{"label": "non_sad", "prediction": 0.95}, {"label": "sad", "prediction": 0.05}],
        "b": [{"label": "non_sad", "prediction": 0.85}, {"label": "sad", "prediction": 0.15}],
    },
    "mood_relaxed": {
        "a": [{"label": "non_relaxed", "prediction": 0.40}, {"label": "relaxed", "prediction": 0.60}],
        "b": [{"label": "non_relaxed", "prediction": 0.30}, {"label": "relaxed", "prediction": 0.70}],
    },
}


@pytest.mark.parametrize(
    ("predictions", "expected"),
    [
        # positive class first
        ([{"label": "danceable", "prediction": 0.7}, {"label": "non_danceable", "prediction": 0.3}], 0.7),
        # NEGATIVE class first -- the alphabetical-ordering trap
        ([{"label": "non_relaxed", "prediction": 0.4}, {"label": "relaxed", "prediction": 0.6}], 0.6),
        ([{"label": "not_party", "prediction": 0.2}, {"label": "party", "prediction": 0.8}], 0.8),
        # no label qualifies -> defensive fallback to the first entry
        ([{"label": "non_a", "prediction": 0.11}, {"label": "not_b", "prediction": 0.99}], 0.11),
        # a label the prefix rule says nothing about -> first entry wins
        ([{"label": "aggressive", "prediction": 0.33}, {"label": "calm", "prediction": 0.66}], 0.33),
    ],
)
def test_positive_class_prediction_selection_is_unchanged(predictions: list[dict[str, Any]], expected: float) -> None:
    """Which entry counts as the POSITIVE class, by label rather than by position.

    Indexing ``[0]`` here systematically inverted relaxed/sad/party once. The fallback branch is
    included because a refactor is more likely to drop a defensive branch than a load-bearing one.
    """
    assert _positive_class_prediction(predictions) == pytest.approx(expected)


def test_derive_mood_is_unchanged() -> None:
    """Highest mean positive-class score across variants wins, with the `mood_` prefix stripped."""
    assert derive_mood(_MOOD_FEATURES) == "happy"
    assert derive_mood({}) == ""
    # A set present but with no predictions contributes nothing rather than scoring 0.
    assert derive_mood({"mood_happy": {"a": []}}) == ""


def test_derive_style_is_unchanged() -> None:
    """Top-confidence genre label, with the `---` hierarchy separator rewritten to `/`."""
    predictions = {"predictions": [{"label": "Electronic---House", "confidence": 0.8}, {"label": "Rock", "confidence": 0.2}]}
    assert derive_style(predictions) == "Electronic/House"
    assert derive_style({}) == "unknown"
    assert derive_style({"predictions": []}) == "unknown"


def test_derive_danceability_is_unchanged() -> None:
    """Mean positive-class score across the danceability variants; None when the set is absent."""
    features = {
        "danceability": {
            "a": [{"label": "danceable", "prediction": 0.6}, {"label": "non_danceable", "prediction": 0.4}],
            "b": [{"label": "danceable", "prediction": 0.8}, {"label": "non_danceable", "prediction": 0.2}],
        }
    }
    assert derive_danceability(features) == pytest.approx(0.7)
    assert derive_danceability({}) is None
    assert derive_danceability({"danceability": {}}) is None
    assert derive_danceability({"danceability": {"a": []}}) is None


def _fine(index: int, start: float, end: float, bpm: float | None, key: str | None, confidence: float = 1.0) -> FineWindow:
    return FineWindow(window_index=index, start_sec=start, end_sec=end, bpm=bpm, musical_key=key, confidence=confidence)


def _coarse(
    index: int, start: float, end: float, mood: str | None, style: str | None, danceability: float | None, features: dict[str, Any] | None = None
) -> CoarseWindow:
    return CoarseWindow(window_index=index, start_sec=start, end_sec=end, mood=mood, style=style, danceability=danceability, features=features or {})


def test_aggregate_bpm_is_unchanged() -> None:
    """Median of fine-window BPMs, rounded to 0.1, EXCLUDING zero-confidence windows.

    The exclusion is the load-bearing half: a zero-confidence BPM on short or silent audio is
    not a measurement, and including it drags the median. Pinned with a case where including it
    would give a different answer, so dropping the filter cannot pass.
    """
    assert aggregate_bpm([_fine(0, 0, 30, 120.0, "A minor"), _fine(1, 30, 60, 124.0, "A minor")]) == 122.0
    assert aggregate_bpm([_fine(0, 0, 30, 120.0, "A minor"), _fine(1, 30, 60, 20.0, "A minor", confidence=0.0)]) == 120.0
    assert aggregate_bpm([_fine(0, 0, 30, None, "A minor")]) is None
    assert aggregate_bpm([]) is None


def test_aggregate_key_is_unchanged() -> None:
    """Duration-weighted modal key; ties resolve to the FIRST-SEEN key (insertion order)."""
    windows = [_fine(0, 0, 30, 120.0, "A minor"), _fine(1, 30, 60, 120.0, "C major"), _fine(2, 60, 105, 120.0, "C major")]
    assert aggregate_key(windows) == "C major"
    tie = [_fine(0, 0, 30, 120.0, "A minor"), _fine(1, 30, 60, 120.0, "C major")]
    assert aggregate_key(tie) == "A minor"
    assert aggregate_key([]) is None


def test_aggregate_dominant_and_danceability_are_unchanged() -> None:
    """Time-weighted dominant label per attribute; mean danceability across coarse windows."""
    windows = [_coarse(0, 0, 180, "happy", "House", 0.4), _coarse(1, 180, 200, "sad", "Rock", 0.8)]
    assert aggregate_dominant(windows, "mood") == "happy"
    assert aggregate_dominant(windows, "style") == "House"
    assert aggregate_danceability(windows) == pytest.approx(0.6)
    assert aggregate_dominant([], "mood") is None
    assert aggregate_danceability([]) is None
    assert aggregate_danceability([_coarse(0, 0, 180, "happy", "House", None)]) is None


def test_representative_features_is_unchanged() -> None:
    """The LONGEST coarse window's features, ties resolving to the first; {} when there are none."""
    short = _coarse(1, 180, 200, "sad", "Rock", 0.8, {"which": "short"})
    long_window = _coarse(0, 0, 180, "happy", "House", 0.4, {"which": "long"})
    assert _representative_features([short, long_window]) == {"which": "long"}
    tie_a = _coarse(0, 0, 180, "happy", "House", 0.4, {"which": "a"})
    tie_b = _coarse(1, 180, 360, "sad", "Rock", 0.8, {"which": "b"})
    assert _representative_features([tie_a, tie_b]) == {"which": "a"}
    assert _representative_features([]) == {}
