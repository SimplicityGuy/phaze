"""Sub-window mood from the REAL mood graphs on a real decoded audio fixture (phaze-x3gak).

Real essentia, real weights, real decode: no mock anywhere. It SKIPS where the weights are absent
(CI today); that skip is a disclosed gap, not coverage -- the test says so in its reason.

The discriminating claim (docs/design/0026-subwindow-mood-and-beat-grid.md section 2): every patch is attributed to exactly one sub-window, so
the patch-count-weighted mean of a coarse window's sub-window vectors IS that window's stored
``mood_scores`` source. Counts are derived here from the ADR's closed form for a graph's patch count and
the centre rule, independently of ``analysis_subwindows``.

STATED TOLERANCE: ``_TOL`` = 1e-4 absolute per mood. Sub-windows are pooled in float64 from float32
activations, so the honest error is ~1e-7; 1e-4 leaves room for one patch landing on the other side of a
slice boundary through sample-count rounding in the decode, which moves a weighted mean by well under it.
Feeding a pooling that counts overlapping patches twice must exceed it, or the test proves nothing:
``test_a_pooling_that_double_counts_patches_is_rejected``.

THE FIXTURE is the generated tone-plus-click WAV the equivalence suite uses: 90 s, coarse windows of 60 s,
so one full window (4 slices) and one 30 s trailing window (2 slices).
"""

from __future__ import annotations

import math
import os
import struct
from typing import TYPE_CHECKING, Any
import wave

import numpy as np
import pytest

from phaze.services import analysis as analysis_mod
from phaze.services.analysis import analyze_file
from phaze.services.analysis_subwindows import PATCH_GEOMETRY, positive_index
from phaze.services.analysis_windows import SUBWINDOW_SEC
from phaze.services.set_projection import MOOD_ORDER, positive_class_vector
from tests._real_models import ENV_VAR as _REAL_MODELS_ENV_VAR


if TYPE_CHECKING:
    from pathlib import Path


_MODELS_DIR = os.environ.get(_REAL_MODELS_ENV_VAR) or None
requires_real_models = pytest.mark.skipif(
    _MODELS_DIR is None,
    reason=f"REAL-MODEL SUB-WINDOW POOLING NOT RUN (a skip, not a pass): {_REAL_MODELS_ENV_VAR} is unset or empty",
)

_TOL = 1e-4
_RATE = 8000
_SECONDS = 90
_COARSE = 60


def _write_wav(path: Path) -> None:
    period = round(_RATE * 60.0 / 124)
    frames = bytearray()
    for index in range(_SECONDS * _RATE):
        elapsed = index / _RATE
        value = 0.25 * math.sin(2.0 * math.pi * 196.0 * elapsed) + 0.1 * math.sin(2.0 * math.pi * 392.0 * elapsed)
        phase = index % period
        if phase < 64:
            value += 0.6 * math.exp(-phase / 12.0)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_RATE)
        handle.writeframes(bytes(frames))


def _n_patches(seconds: float, classifier_type: str) -> int:
    samples = round(seconds * 16000)
    frame, frame_hop, patch, hop = (512, 256, 187, 93) if classifier_type == "musicnn" else (400, 160, 96, 93)
    frames = (samples - frame) // frame_hop + 1
    return max(1, (frames - patch) // hop + 1)


def _independent_counts(length_sec: float, n_slices: int) -> list[int]:
    """Patches per slice for the default (musicnn) head, by the ADR's rule, restated from the arithmetic."""
    counts = [0] * n_slices
    for k in range(_n_patches(length_sec, "musicnn")):
        counts[min(int((k * 1.488 + 1.496) // SUBWINDOW_SEC), n_slices - 1)] += 1
    return counts


@pytest.fixture(scope="module")
def audio(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("subwindow_real") / "tone_click_124bpm.wav"
    _write_wav(path)
    return path


def _run(path: Path) -> dict[str, Any]:
    assert _MODELS_DIR is not None
    return analyze_file(str(path), _MODELS_DIR, fine_window_sec=30, coarse_window_sec=_COARSE, fine_min_sec=15, include_grids=True)


def _worst_recovery_error(result: dict[str, Any]) -> float:
    """Largest |weighted sub-window mean - stored coarse mean| over every coarse window and every mood."""
    assert {m.variant for s in analysis_mod.MODEL_SETS for m in s.models} == {"musicnn_msd"}, "counts below assume the default single musicnn head"
    worst = 0.0
    for window in (w for w in result["windows"] if w["tier"] == "coarse"):
        rows = [r for r in result["subwindows"] if r["window_index"] == window["window_index"]]
        counts = _independent_counts(window["end_sec"] - window["start_sec"], len(rows))
        assert len(rows) == math.ceil((window["end_sec"] - window["start_sec"]) / SUBWINDOW_SEC)
        coarse = positive_class_vector(window["features"])
        for position in range(len(MOOD_ORDER)):
            weighted = sum(r["mood_scores"][position] * c for r, c in zip(rows, counts, strict=True) if c) / sum(counts)
            worst = max(worst, abs(weighted - coarse[position]))
    return worst


@requires_real_models
@pytest.mark.integration
def test_patch_count_weighted_sub_windows_reproduce_the_coarse_mood_on_real_graphs(audio: Path) -> None:
    result = _run(audio)
    assert _worst_recovery_error(result) < _TOL


@requires_real_models
@pytest.mark.integration
def test_every_coarse_window_yields_ceil_length_over_15_positional_vectors_on_real_graphs(audio: Path) -> None:
    result = _run(audio)
    by_window: dict[int, list[dict[str, Any]]] = {}
    for row in result["subwindows"]:
        by_window.setdefault(row["window_index"], []).append(row)
    assert sorted(by_window) == [0, 1]
    assert [len(by_window[0]), len(by_window[1])] == [4, 2]
    for rows in by_window.values():
        assert [r["sub_index"] for r in rows] == list(range(len(rows)))
        assert all(r["mood_scores"] is None or len(r["mood_scores"]) == len(MOOD_ORDER) for r in rows)
    assert by_window[0][0]["start_sec"] == 0.0
    assert by_window[1][0]["start_sec"] == 60.0


@requires_real_models
@pytest.mark.integration
def test_the_coarse_output_is_identical_with_and_without_the_grids(audio: Path) -> None:
    assert _MODELS_DIR is not None
    plain = analyze_file(str(audio), _MODELS_DIR, fine_window_sec=30, coarse_window_sec=_COARSE, fine_min_sec=15)
    with_grids = _run(audio)
    assert {k: v for k, v in with_grids.items() if k not in {"subwindows", "beat_grid"}} == plain


@requires_real_models
@pytest.mark.integration
def test_a_pooling_that_double_counts_patches_is_rejected(audio: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def overlap_pool(patches: Any, labels: Any, classifier_type: str, n_slices: int, sub_sec: int = SUBWINDOW_SEC) -> tuple[list[float], list[int]]:
        matrix = np.atleast_2d(np.asarray(patches))
        column = matrix[:, positive_index(labels)].astype(np.float64)
        length, hop = PATCH_GEOMETRY[classifier_type]
        sums, counts = [0.0] * n_slices, [0] * n_slices
        for k, value in enumerate(column):
            for slot in range(int(k * hop // sub_sec), min(int((k * hop + length) // sub_sec), n_slices - 1) + 1):
                sums[slot] += float(value)
                counts[slot] += 1
        return sums, counts

    monkeypatch.setattr(analysis_mod, "pool_positive_class", overlap_pool)
    assert _worst_recovery_error(_run(audio)) > _TOL


@requires_real_models
@pytest.mark.integration
async def test_sub_windows_and_beat_grid_survive_the_real_child_process_boundary(audio: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The consumer that matters (CLAUDE.md rule 3): the parent's own path to a child's result.

    ``PatchMean`` carries its patch matrix as an attribute, and an ndarray subclass attribute does not
    survive pickling, ``np.asarray``, copies or arithmetic. That is harmless exactly because the matrix is
    pooled to plain floats inside the sweep, in the CHILD, before anything is returned. This test does not
    argue that: it runs the real ``python -m phaze.analysis_child`` over a real file with the real graphs,
    reads the result through ``run_analysis_subprocess`` (strict ``json.dumps`` in the child, a real pipe,
    ``json.loads`` in the parent), and asserts the sub-windows and the beat grid arrive non-empty and equal
    to the in-process result.
    """
    from phaze.analysis_child import _TARGET_ENV
    from phaze.services.analysis_exec import run_analysis_subprocess

    assert _MODELS_DIR is not None
    monkeypatch.setenv(_TARGET_ENV, "tests.analyze._child_stubs:grids_analyze")
    crossed = await run_analysis_subprocess(str(audio), _MODELS_DIR, fine_window_sec=30, coarse_window_sec=_COARSE, fine_min_sec=15)

    assert crossed["subwindows"], "sub-windows were lost crossing the process boundary"
    assert [len(crossed["subwindows"]), crossed["subwindows"][0]["sub_index"]] == [6, 0]
    assert any(row["mood_scores"] is not None for row in crossed["subwindows"])
    assert crossed["beat_grid"]["beats"], "the beat grid was lost crossing the process boundary"

    in_process = _run(audio)
    assert crossed["subwindows"] == in_process["subwindows"]
    assert crossed["beat_grid"] == in_process["beat_grid"]
