"""Tests for `AnalysisSignals` -- the collapsed progress/liveness seam (phaze-mp0op).

phaze-w55w1's heartbeat feature added exactly ONE signal and had to thread an optional
callback through six functions, each with its own hand-written `if cb is not None:` guard.
These tests cover the seam that replaced that pattern: the two total methods in isolation,
that `analyze_file` builds exactly one instance and shares it across both tiers, and --
because there was no test anywhere asserting it before this bead -- that the heartbeat
actually emits the watchdog's stage-name contract in the shape `services/analysis_exec.py`
depends on.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np

import phaze.services.analysis as analysis_mod
from phaze.services.analysis import (
    MODEL_SETS,
    NO_SIGNALS,
    AnalysisSignals,
    _chunked,
    _decode_windows,
    analyze_file,
)


_MOCK_DURATION_SEC = 6000.0  # 200 fine windows (4 chunks) + 34 coarse windows (2 chunks)


def _build_mock_essentia(duration_sec: float = _MOCK_DURATION_SEC) -> MagicMock:
    """Build a mock `essentia.standard` module with every class `analyze_file`'s mocked path needs.

    Mirrors `test_analysis.py::_build_mock_essentia`; kept local rather than imported so this
    file's fixtures do not couple to another test module's internals.
    """
    mock_es = MagicMock()

    mock_loader_instance = MagicMock()
    mock_loader_instance.return_value = np.zeros(16000, dtype=np.float32)
    mock_es.MonoLoader.return_value = mock_loader_instance
    mock_es.EasyLoader.return_value = mock_loader_instance

    mock_rhythm = MagicMock()
    mock_rhythm.return_value = (128.0, np.array([0.5]), 3.8, np.array([]), np.array([0.5]))
    mock_es.RhythmExtractor2013.return_value = mock_rhythm

    mock_key = MagicMock()
    mock_key.return_value = ("C", "minor", 0.8)
    mock_es.KeyExtractor.return_value = mock_key

    for cls_name in ("TensorflowPredictMusiCNN", "TensorflowPredictVGGish", "TensorflowPredictEffnetDiscogs"):
        mock_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.return_value = np.array([[0.7, 0.3]] * 10, dtype=np.float32)
        mock_cls.return_value = mock_instance
        setattr(mock_es, cls_name, mock_cls)

    return mock_es


def _mock_labels_file(model_filename: str, _models_dir: str) -> list[str]:
    """Return mock labels for any model file."""
    if "discogs" in model_filename:
        return [f"Genre{i}" for i in range(400)]
    return ["positive_class", "negative_class"]


# ---------------------------------------------------------------------------
# AnalysisSignals -- the seam itself
# ---------------------------------------------------------------------------


def test_unset_signals_are_a_total_noop() -> None:
    """`NO_SIGNALS`, and a freshly built all-default `AnalysisSignals`, never raise.

    Both channels are TOTAL: an unset channel is a no-op call, never a `None` a caller has
    to test -- this is the whole point of collapsing five hand-written `if cb is not None:`
    guards into one seam.
    """
    for signals in (NO_SIGNALS, AnalysisSignals()):
        signals.progress(0, 10)
        signals.progress(10, 10)
        signals.beat("fine_decode", 0, 10)
        signals.beat("coarse", 10, 10)


def test_signals_forward_every_argument_verbatim_on_both_channels() -> None:
    """Every argument reaches the underlying callback unchanged, on both channels."""
    progress_calls: list[tuple[int, int]] = []
    beat_calls: list[tuple[str, int, int]] = []
    signals = AnalysisSignals(
        progress_cb=lambda analyzed, total: progress_calls.append((analyzed, total)),
        heartbeat_cb=lambda stage, done, total: beat_calls.append((stage, done, total)),
    )

    signals.progress(3, 7)
    signals.beat("coarse_model", 5, 34)

    assert progress_calls == [(3, 7)]
    assert beat_calls == [("coarse_model", 5, 34)]


# ---------------------------------------------------------------------------
# analyze_file builds ONE signals object and shares it across both tiers
# ---------------------------------------------------------------------------


def test_analyze_file_builds_one_signals_object_shared_by_both_tiers() -> None:
    """`analyze_file`'s public signature is unchanged, but internally it builds ONE `AnalysisSignals`.

    Both `_analyze_fine_windows` and `_analyze_coarse_windows` must receive the SAME instance
    -- not two equal-but-distinct ones -- so a future signal added to `AnalysisSignals` never
    needs a second construction site.
    """
    captured: dict[str, AnalysisSignals] = {}

    def _capture_fine(*_args: Any, signals: AnalysisSignals, **_kwargs: Any) -> tuple[list[Any], int]:
        captured["fine"] = signals
        return [], 0

    def _capture_coarse(*_args: Any, signals: AnalysisSignals, **_kwargs: Any) -> tuple[list[Any], int]:
        captured["coarse"] = signals
        return [], 0

    def _progress_cb(_analyzed: int, _total: int) -> None:
        return None

    def _heartbeat_cb(_stage: str, _done: int, _total: int) -> None:
        return None

    with (
        patch.object(analysis_mod, "_probe_duration_sec", return_value=60.0),
        patch.object(analysis_mod, "_analyze_fine_windows", side_effect=_capture_fine),
        patch.object(analysis_mod, "_analyze_coarse_windows", side_effect=_capture_coarse),
    ):
        analyze_file("/fake/audio.mp3", "/fake/models", progress_cb=_progress_cb, heartbeat_cb=_heartbeat_cb)

    assert isinstance(captured["fine"], AnalysisSignals)
    assert captured["fine"] is captured["coarse"], "both tiers must share the SAME AnalysisSignals instance"
    assert captured["fine"].progress_cb is _progress_cb
    assert captured["fine"].heartbeat_cb is _heartbeat_cb


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_heartbeat_default_none_is_inert(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """`heartbeat_cb=None` (the default) leaves the result's progress-count contract untouched."""
    mock_get_labels.side_effect = _mock_labels_file
    coverage_keys = ("fine_windows_analyzed", "fine_windows_total", "coarse_windows_analyzed", "coarse_windows_total")

    with_none = analyze_file("/fake/audio.mp3", "/fake/models")
    with_cb = analyze_file("/fake/audio.mp3", "/fake/models", heartbeat_cb=lambda _s, _d, _t: None)

    for key in coverage_keys:
        assert with_none[key] == with_cb[key]


# ---------------------------------------------------------------------------
# The watchdog contract (phaze-1b39 incident class): no test anywhere asserted this before
# ---------------------------------------------------------------------------


@patch("phaze.services.analysis._probe_duration_sec", return_value=_MOCK_DURATION_SEC)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_heartbeat_stage_sequence_is_the_watchdog_contract(
    _mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock
) -> None:
    """`analyze_file`'s heartbeat emits exactly the five-name stage vocabulary the watchdog reads.

    `services/analysis_exec.py`'s stall watchdog and `tasks/functions.py`'s terminal error
    text both key off these five strings verbatim: "fine_decode", "fine", "coarse_decode",
    "coarse_model", "coarse". Its failure mode is the phaze-1b39 incident class -- a watchdog
    SIGTERMing a legitimate multi-hour analysis -- so this asserts the full contract: the
    vocabulary is exactly that set, a `*_decode` beat precedes every chunk's window beats
    (never the reverse), `total` is constant per tier and equals the natural window count,
    and `done` is non-decreasing within a tier.

    6000s duration -> 200 fine windows across 4 chunks (60/60/60/20) and 34 coarse windows
    across 2 chunks (30/4): more than one chunk per tier is required, because a single-chunk
    file cannot distinguish "a decode beat precedes this chunk's windows" from "a decode beat
    precedes the FIRST chunk's windows" -- exactly the shape that hid the liveness gap.
    """
    mock_get_labels.side_effect = _mock_labels_file
    beats: list[tuple[str, int, int]] = []

    result = analyze_file("/fake/audio.mp3", "/fake/models", heartbeat_cb=lambda stage, done, total: beats.append((stage, done, total)))

    assert {b[0] for b in beats} == {"fine_decode", "fine", "coarse_decode", "coarse_model", "coarse"}

    fine_total = result["fine_windows_total"]
    coarse_total = result["coarse_windows_total"]
    assert fine_total == 200
    assert coarse_total == 34

    fine_beats = [b for b in beats if b[0] in ("fine_decode", "fine")]
    coarse_beats = [b for b in beats if b[0] in ("coarse_decode", "coarse_model", "coarse")]
    # The fine tier runs to completion before the coarse tier starts even one beat.
    assert beats == fine_beats + coarse_beats

    # `total` is constant per tier and equals the natural (pre-skip) window count.
    assert all(total == fine_total for _s, _d, total in fine_beats)
    assert all(total == coarse_total for _s, _d, total in coarse_beats)

    # `done` is non-decreasing within each tier.
    assert [d for _s, d, _t in fine_beats] == sorted(d for _s, d, _t in fine_beats)
    assert [d for _s, d, _t in coarse_beats] == sorted(d for _s, d, _t in coarse_beats)

    # A `*_decode` beat precedes every chunk's window beats: reconstruct the exact expected
    # stage sequence from the real chunk boundaries (`_chunked`, not a hardcoded 60/60/60/20)
    # and assert it byte-for-byte -- the strongest form of "precedes, never follows".
    fine_chunk_sizes = [len(c) for c in _chunked(list(range(fine_total)), analysis_mod._FINE_CHUNK_WINDOWS)]
    expected_fine_stages: list[str] = []
    for size in fine_chunk_sizes:
        expected_fine_stages.append("fine_decode")
        expected_fine_stages.extend(["fine"] * size)
    assert [b[0] for b in fine_beats] == expected_fine_stages

    coarse_chunk_sizes = [len(c) for c in _chunked(list(range(coarse_total)), analysis_mod._COARSE_CHUNK_WINDOWS)]
    # One on_model_done() per individual model sweep (11 sets x 3 variants), plus one for genre.
    model_ticks_per_chunk = sum(len(model_set.models) for model_set in MODEL_SETS) + 1
    expected_coarse_stages: list[str] = []
    for _size in coarse_chunk_sizes:
        expected_coarse_stages.append("coarse_decode")
        expected_coarse_stages.extend(["coarse_model"] * model_ticks_per_chunk)
        expected_coarse_stages.append("coarse")
    assert [b[0] for b in coarse_beats] == expected_coarse_stages


# ---------------------------------------------------------------------------
# The fallback decode's per-window beat, through the exact production call shape
# ---------------------------------------------------------------------------


def test_decode_fallback_beats_once_per_window_including_skips() -> None:
    """`AnalysisSignals.beat`, wired as `_decode_windows`'s `on_beat`, fires once per fallback window.

    Wired with the exact call shape production uses (`on_beat=lambda: signals.beat(stage, done,
    total)`) rather than exercising `on_beat` directly -- this proves the SEAM forwards through
    the real call site the tiers use, not just that `_decode_windows` invokes whatever callable
    it is handed. Includes a window that fails to decode: the beat fires for it too, outside the
    try, because a skip is progress and this loop's silence is what the stall watchdog would
    otherwise read as a hang.
    """
    beats: list[tuple[str, int, int]] = []
    signals = AnalysisSignals(heartbeat_cb=lambda stage, done, total: beats.append((stage, done, total)))
    windows = [(0, 0.0, 5.0), (1, 5.0, 10.0), (2, 10.0, 15.0)]
    skipped: list[int] = []

    def _easyloader(*, filename: str, sampleRate: int, startTime: float, endTime: float) -> Any:
        if startTime == 5.0:  # the middle window fails to decode
            msg = "no decodable frames"
            raise RuntimeError(msg)
        loader = MagicMock()
        loader.return_value = np.zeros(4, dtype=np.float32)
        return loader

    mock_es = MagicMock()
    mock_es.EasyLoader.side_effect = _easyloader

    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_decode_windows_streaming", side_effect=RuntimeError("network build failed")),
    ):
        decoded = _decode_windows(
            "/fake/audio.mp3",
            16000,
            windows,
            lambda idx, _s, _e, _exc: skipped.append(idx),
            on_beat=lambda: signals.beat("coarse_decode", len(beats), len(windows)),
        )

    assert len(decoded) == 2, "the fallback still decodes every window it can"
    assert skipped == [1], "the middle window is reported skipped, never fails the chunk"
    assert [b[0] for b in beats] == ["coarse_decode"] * len(windows), "one beat per window, including the failed one"
