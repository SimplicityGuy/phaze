"""A grid or sub-window failure degrades to "no grids", never to a failed analysis (phaze-7hcgn.5).

Sub-window mood and the beat grid are secondary display data (docs/design/0026-subwindow-mood-and-beat-grid.md). The
governing rule for this module: a failure on the grid path drops the grid for that window or file, logs it and counts
it on ``phaze.analysis.grids.dropped``, and leaves the core analysis -- the windows, bpm, key and mood -- exactly as
complete as it would have been without grids.

Findings (second PR #802 review) and the tests that discharge them:

* A, a grid validation error aborted the whole completion payload
  -> ``test_an_invalid_beat_grid_is_dropped_and_the_payload_keeps_everything_else`` and the three tests after it;
* B, the tick-only tracker call shared the bpm/key ``try``
  -> ``test_a_tick_call_that_raises_keeps_the_windows_bpm_and_key``;
* C, pooling ran outside per-window failure isolation
  -> ``test_a_pooling_error_keeps_every_window_and_drops_only_its_sub_windows`` and
     ``test_a_sub_window_combine_error_keeps_the_coarse_window``;
* the acceptance criterion that each of the three leaves the core analysis complete, end to end through
  ``analyze_file`` and both payload builders -> ``test_a_grid_failure_leaves_the_core_analysis_complete``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from phaze.job_runner import _build_payload
from phaze.schemas.agent_analysis import MAX_SUBWINDOWS, grid_payload_fields
from phaze.services import analysis as analysis_mod
from phaze.services.analysis_subwindows import with_patches
from phaze.services.analysis_windows import CoarseWindow, FineWindow
from phaze.services.beat_grid import BEAT_PREROLL_SEC
from phaze.services.set_projection import MOOD_ORDER
from phaze.tasks.functions import _build_analysis_write_payload


if TYPE_CHECKING:
    from phaze.services.analysis_models import ModelConfig


_DROPPED = "phaze.analysis.grids.dropped"


@pytest.fixture
def drops(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, dict[str, Any]]]:
    """Every ``phaze.analysis.grids.dropped`` increment, as ``(value, attributes)``."""
    seen: list[tuple[float, dict[str, Any]]] = []
    real_add = analysis_mod.telemetry.add

    def spy(name: str, value: float = 1, **attributes: Any) -> None:
        if name == _DROPPED:
            seen.append((value, attributes))
        real_add(name, value, **attributes)

    monkeypatch.setattr(analysis_mod.telemetry, "add", spy)
    return seen


def _sub(window_index: int, sub_index: int, *, mood: list[float | None] | None = None) -> dict[str, Any]:
    start = window_index * 180.0 + sub_index * 15.0
    return {
        "window_index": window_index,
        "sub_index": sub_index,
        "start_sec": start,
        "end_sec": start + 15.0,
        "mood_scores": mood if mood is not None else [0.1] * len(MOOD_ORDER),
    }


def _result(**grids: Any) -> dict[str, Any]:
    return {
        "bpm": 124.0,
        "musical_key": "A minor",
        "features": {"mood_happy": {"musicnn_msd": [{"label": "happy", "prediction": 0.8}, {"label": "non_happy", "prediction": 0.2}]}},
        "windows": [
            {"tier": "fine", "window_index": 0, "start_sec": 0.0, "end_sec": 30.0, "bpm": 124.0, "musical_key": "A minor"},
            {"tier": "coarse", "window_index": 0, "start_sec": 0.0, "end_sec": 180.0, "mood": "happy"},
        ],
        "fine_windows_analyzed": 1,
        "fine_windows_total": 1,
        "coarse_windows_analyzed": 1,
        "coarse_windows_total": 1,
        **grids,
    }


# --- A: payload validation -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("build", [_build_payload, _build_analysis_write_payload], ids=["pod_lane", "worker_lane"])
def test_an_invalid_beat_grid_is_dropped_and_the_payload_keeps_everything_else(
    build: Any, drops: list[tuple[float, dict[str, Any]]], caplog: pytest.LogCaptureFixture
) -> None:
    result = _result(subwindows=[_sub(0, 0)], beat_grid={"beats": [2.0, 1.0], "gaps": []})  # descending: a 422 shape
    with caplog.at_level("WARNING"):
        payload = build(result)
    body = payload.model_dump(mode="json", exclude_unset=True)
    assert "beat_grid" not in body  # absent, not null: an absent key means "this run produced none"
    assert len(body["subwindows"]) == 1
    assert body["bpm"] == 124.0
    assert body["musical_key"] == "A minor"
    assert len(body["windows"]) == 2
    assert body["mood"]
    assert drops == [(1, {"grid_stage": "payload"})]
    assert any("beat_grid" in r.getMessage() for r in caplog.records)


def test_an_invalid_sub_window_drops_only_its_own_coarse_window() -> None:
    rows = [_sub(0, 0), _sub(0, 1), _sub(1, 0), {**_sub(1, 1), "end_sec": 0.0}]  # window 1's second slice is malformed
    fields = grid_payload_fields(_result(subwindows=rows))
    assert [(s.window_index, s.sub_index) for s in fields["subwindows"]] == [(0, 0), (0, 1)]


@pytest.mark.parametrize("bad_row", ["not a row", {**_sub(1, 0), "window_index": "one", "end_sec": 0.0}], ids=["not_a_dict", "unreadable_index"])
def test_a_bad_sub_window_row_that_cannot_name_its_window_drops_all_sub_windows(bad_row: Any) -> None:
    # No window to drop it from: only the whole list can go, and the beat grid and the core fields still go out.
    payload = _build_payload(_result(subwindows=[_sub(0, 0), bad_row], beat_grid={"beats": [0.5, 1.0], "gaps": []}))
    body = payload.model_dump(mode="json", exclude_unset=True)
    assert "subwindows" not in body
    assert body["beat_grid"]["beats"] == [0.5, 1.0]
    assert body["bpm"] == 124.0


def test_more_sub_windows_than_the_wire_admits_are_dropped_not_fatal() -> None:
    rows = [_sub(i // 12, i % 12) for i in range(MAX_SUBWINDOWS + 1)]
    payload = _build_payload(_result(subwindows=rows, beat_grid={"beats": [0.5, 1.0], "gaps": []}))
    body = payload.model_dump(mode="json", exclude_unset=True)
    assert "subwindows" not in body
    assert body["beat_grid"]["beats"] == [0.5, 1.0]


def test_a_non_finite_sub_window_mood_is_dropped_on_the_agent() -> None:
    # G on the producer side: the wire refuses NaN, so the agent drops the window rather than send a 422.
    fields = grid_payload_fields(_result(subwindows=[_sub(0, 0), _sub(1, 0, mood=[float("nan")] * len(MOOD_ORDER))]))
    assert [s.window_index for s in fields["subwindows"]] == [0]


# --- B: the tick-only tracker call -----------------------------------------------------------------------------------


def test_a_tick_call_that_raises_keeps_the_windows_bpm_and_key(drops: list[tuple[float, dict[str, Any]]]) -> None:
    rate = analysis_mod._FINE_SAMPLE_RATE

    def rhythm(buf: np.ndarray) -> tuple[float, np.ndarray, float, None, None]:
        if len(buf) > rate * 30:  # the pre-rolled buffer: the tick-only call
            raise RuntimeError("tracker blew up on the pre-roll")
        return (120.0, np.array([0.5, 1.0], dtype=np.float32), 3.0, None, None)

    on_skip = MagicMock()
    full = np.full(rate * 34, 0.1, dtype=np.float32)
    measured = analysis_mod._measure_fine_window(
        rhythm, MagicMock(return_value=("A", "minor", 0.9)), (2, 60.0, 90.0), {2: full}, on_skip, BEAT_PREROLL_SEC
    )
    assert measured is not None
    assert (measured.bpm, measured.musical_key) == (120.0, "A minor")
    assert measured.beats == ()  # no ticks for this window: the stitcher leaves it a recorded gap
    on_skip.assert_not_called()
    assert drops == [(1, {"grid_stage": "fine_ticks"})]


# --- C: sub-window pooling -------------------------------------------------------------------------------------------


def _stub_predict(_audio: Any, model: ModelConfig, _models_dir: str) -> Any:
    matrix = np.tile(np.array([0.7, 0.3], dtype=np.float32), (40, 1))
    return with_patches(np.mean(matrix, axis=0), matrix)


def _stub_labels(filename: str, _models_dir: str) -> list[str]:
    return [f"G{i}" for i in range(400)] if "discogs" in filename else ["positive", "negative"]


def _stub_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analysis_mod, "_predict_single", _stub_predict)
    monkeypatch.setattr(analysis_mod, "_get_labels", _stub_labels)
    monkeypatch.setattr(analysis_mod, "_release_classifier_timed", lambda _model: None)


def test_a_pooling_error_keeps_every_window_and_drops_only_its_sub_windows(
    monkeypatch: pytest.MonkeyPatch, drops: list[tuple[float, dict[str, Any]]]
) -> None:
    _stub_models(monkeypatch)
    real_pool = analysis_mod.pool_positive_class
    calls = {"n": 0}

    def pool(*args: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # the second pooling call of the sweep: window 1's first model
            raise ValueError("pooling broke")
        return real_pool(*args)

    monkeypatch.setattr(analysis_mod, "pool_positive_class", pool)
    audio = np.full(16000, 0.1, dtype=np.float32)
    pooled: dict[int, dict[str, list[tuple[list[float], list[int]]]]] = {}
    on_failure = MagicMock()
    features, failed = analysis_mod._run_model_sets_over_windows(
        [(0, audio), (1, audio)], "/m", on_failure, slices_by_window={0: 4, 1: 4}, pooled_out=pooled
    )
    assert failed == set()
    on_failure.assert_not_called()
    assert features[0]["mood_happy"] and features[1]["mood_happy"]  # both windows keep their mood
    assert set(pooled) == {0}  # window 1 has no sub-window mood at all, not a partial one
    assert set(pooled[0]) == set(MOOD_ORDER)
    assert drops == [(1, {"grid_stage": "coarse_pooling"})]


def test_a_sub_window_combine_error_keeps_the_coarse_window(monkeypatch: pytest.MonkeyPatch, drops: list[tuple[float, dict[str, Any]]]) -> None:
    _stub_models(monkeypatch)
    audio = np.full(16000, 0.1, dtype=np.float32)
    pooled: dict[int, dict[str, list[tuple[list[float], list[int]]]]] = {}
    features, failed = analysis_mod._run_model_sets_over_windows([(0, audio)], "/m", MagicMock(), slices_by_window={0: 4}, pooled_out=pooled)

    def broken_combine(*_args: Any) -> Any:
        raise ValueError("combine broke")

    monkeypatch.setattr(analysis_mod, "combine_variants", broken_combine)
    on_skip = MagicMock()
    window = analysis_mod._derive_coarse_window((0, 0.0, 60.0), features, failed, on_skip, pooled[0])
    assert window is not None
    assert window.mood is not None
    assert window.subwindow_moods == ()
    on_skip.assert_not_called()
    assert drops == [(1, {"grid_stage": "coarse_pooling"})]


# --- assembly --------------------------------------------------------------------------------------------------------


def _windows() -> tuple[list[FineWindow], list[CoarseWindow]]:
    fine = [FineWindow(0, 0.0, 30.0, 120.0, "A minor", 1.0, beats=(0.5, 1.0, 1.5))]
    moods = tuple((0.1,) * len(MOOD_ORDER) for _ in range(2))
    coarse = [CoarseWindow(0, 0.0, 30.0, "happy", "house", 0.5, {"danceability": {}}, subwindow_moods=moods)]
    return fine, coarse


@pytest.mark.parametrize(("broken", "kept"), [("_beat_grid_payload", "subwindows"), ("_subwindow_payloads", "beat_grid")])
def test_a_grid_assembly_error_omits_that_grid_and_keeps_the_result(
    monkeypatch: pytest.MonkeyPatch, drops: list[tuple[float, dict[str, Any]]], broken: str, kept: str
) -> None:
    fine, coarse = _windows()
    plain = analysis_mod._assemble_analysis_result("f", fine, 1, coarse, 1)

    def boom(*_args: Any) -> Any:
        raise ValueError("assembly broke")

    monkeypatch.setattr(analysis_mod, broken, boom)
    result = analysis_mod._assemble_analysis_result("f", fine, 1, coarse, 1, include_grids=True)
    assert set(result) - set(plain) == {kept}
    assert {k: v for k, v in result.items() if k in plain} == plain
    assert drops == [(1, {"grid_stage": "assembly"})]


# --- acceptance: end to end ------------------------------------------------------------------------------------------


def _stub_audio(monkeypatch: pytest.MonkeyPatch, *, tick_raises: bool) -> None:
    fine_rate = analysis_mod._FINE_SAMPLE_RATE

    def decode(_path: str, rate: int, spans: list[tuple[int, float, float]], _on_skip: object, **_kwargs: object) -> dict[int, object]:
        return {idx: np.full(round((end - start) * rate), 0.1, dtype=np.float32) for idx, start, end in spans}

    def rhythm(buf: np.ndarray) -> tuple[float, np.ndarray, float, None, None]:
        if tick_raises and len(buf) > fine_rate * 30:
            raise RuntimeError("tick call raised")
        return (120.0, np.array([0.5, 1.0], dtype=np.float32), 3.0, None, None)

    monkeypatch.setattr(analysis_mod, "_probe_duration_sec", lambda _path: 90.0)
    monkeypatch.setattr(analysis_mod, "_decode_windows", decode)
    monkeypatch.setattr(analysis_mod.es, "RhythmExtractor2013", lambda **_kw: rhythm)
    monkeypatch.setattr(analysis_mod.es, "KeyExtractor", lambda **_kw: MagicMock(return_value=("A", "minor", 0.9)))
    _stub_models(monkeypatch)


def _core(result: dict[str, Any]) -> dict[str, Any]:
    return {
        k: result[k] for k in ("bpm", "musical_key", "mood", "style", "danceability", "windows", "fine_windows_analyzed", "coarse_windows_analyzed")
    }


@pytest.mark.parametrize("failure", ["validation", "tick", "pooling"])
def test_a_grid_failure_leaves_the_core_analysis_complete(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, drops: list[tuple[float, dict[str, Any]]], failure: str
) -> None:
    _stub_audio(monkeypatch, tick_raises=False)
    baseline = analysis_mod.analyze_file("unused.wav", "/m", include_grids=True)
    assert baseline["beat_grid"]["beats"] and baseline["subwindows"]  # the healthy run really has grids to lose
    drops.clear()

    _stub_audio(monkeypatch, tick_raises=failure == "tick")
    if failure == "pooling":

        def pool(*_args: Any) -> Any:
            raise ValueError("pooling raised")

        monkeypatch.setattr(analysis_mod, "pool_positive_class", pool)
    with caplog.at_level("WARNING"):
        result = analysis_mod.analyze_file("unused.wav", "/m", include_grids=True)
        if failure == "validation":
            result["beat_grid"] = {"beats": [float("inf")], "gaps": []}
        bodies = [build(result).model_dump(mode="json", exclude_unset=True) for build in (_build_payload, _build_analysis_write_payload)]

    assert _core(result) == _core(baseline)  # windows, bpm, key and mood: all present, all unchanged
    assert result["fine_windows_analyzed"] == 3
    assert result["coarse_windows_analyzed"] == 1
    for body in bodies:
        assert body["bpm"] == baseline["bpm"]
        assert body["musical_key"] == baseline["musical_key"]
        assert body["mood"]
        assert len(body["windows"]) == len(baseline["windows"])
    if failure == "tick":
        assert result["beat_grid"]["beats"] == [0.5, 1.0]  # window 0 needs no tick call; windows 1-2 left as gaps
        assert {a["grid_stage"] for _v, a in drops} == {"fine_ticks"}
    elif failure == "pooling":
        assert result["subwindows"] == []
        assert {a["grid_stage"] for _v, a in drops} == {"coarse_pooling"}
    else:
        assert all("beat_grid" not in body for body in bodies)
        assert all(body["subwindows"] for body in bodies)
        assert {a["grid_stage"] for _v, a in drops} == {"payload"}
    assert drops, "a dropped grid is counted"
    assert any("grid" in r.getMessage() for r in caplog.records if r.levelname == "WARNING"), "a dropped grid is logged"
