"""Per-window embeddings out of the REAL ``analyze_file``: real decode, real graphs, real chunking (phaze-l5mzm).

Real essentia and real weights, no mock anywhere. Every test SKIPS where the weights are absent (CI today); that skip is a
disclosed gap, not coverage, and the reason says so.

What is claimed, and by which test:

* the ``embeddings`` key holds one 1280-d float16 vector per coarse window, and each equals a direct call of the genre
  graph's embedding output on that window's own decoded audio
  -> ``test_each_windows_embedding_equals_a_direct_graph_call``;
* switching the option on changes nothing else in the result, grids on or off
  -> ``test_the_rest_of_the_result_is_identical_with_and_without_embeddings``;
* with the option on, repeated multi-chunk analyses do not grow peak RSS (D-07 / D-09, for the new graph call)
  -> ``test_repeated_multi_chunk_analyses_with_embeddings_do_not_grow_peak_rss``.

THE FIXTURE is a generated tone-plus-click WAV at 8 kHz: 150 s with 60 s coarse windows, so two full windows and a 30 s
trailing one.
"""

from __future__ import annotations

import gc
import math
import os
import resource
import struct
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
import wave

import numpy as np
import pytest

from phaze.services import analysis as analysis_mod
from phaze.services.analysis import _COARSE_SAMPLE_RATE, GENRE_MODEL, analyze_file
from phaze.services.analysis_embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_OUTPUT, decode_embedding
from tests._real_models import ENV_VAR as _REAL_MODELS_ENV_VAR


if TYPE_CHECKING:
    from pathlib import Path


_MODELS_DIR = os.environ.get(_REAL_MODELS_ENV_VAR) or None
pytestmark = pytest.mark.skipif(
    _MODELS_DIR is None,
    reason=f"REAL-MODEL EMBEDDINGS THROUGH analyze_file NOT RUN (a skip, not a pass): {_REAL_MODELS_ENV_VAR} is unset or empty",
)

_RATE = 8000
_SECONDS = 150
_COARSE = 60
_FINE = 30
_FINE_MIN = 15


def _write_wav(path: Path, seconds: int) -> None:
    period = round(_RATE * 60.0 / 124)
    frames = bytearray()
    for index in range(seconds * _RATE):
        elapsed = index / _RATE
        value = 0.25 * math.sin(2.0 * math.pi * 196.0 * elapsed) + 0.1 * math.sin(2.0 * math.pi * 392.0 * elapsed)
        if index % period < 64:
            value += 0.6 * math.exp(-(index % period) / 12.0)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_RATE)
        handle.writeframes(bytes(frames))


@pytest.fixture(scope="module")
def audio(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("embeddings_real") / "tone_click_124bpm.wav"
    _write_wav(path, _SECONDS)
    return path


def _run(path: Path, **options: Any) -> dict[str, Any]:
    assert _MODELS_DIR is not None
    return analyze_file(str(path), _MODELS_DIR, fine_window_sec=_FINE, coarse_window_sec=_COARSE, fine_min_sec=_FINE_MIN, **options)


@pytest.mark.integration
def test_each_windows_embedding_equals_a_direct_graph_call(audio: Path) -> None:
    """The acceptance criterion: shape ``(n_coarse, 1280)`` float16, equal to a direct graph call within tolerance.

    The direct call is the wrapper phaze has always used for the genre model, asked for the embedding output
    (``PartitionedCall:1``) instead of the logits, on the window's audio decoded at the coarse rate by the same
    streaming decode the analysis runs, and pooled by the mean over patches. STATED TOLERANCE: float16's own,
    ``rtol=2**-11``; the fused route is bit-identical to the wrapper in float32, so the narrowing is the only error.
    """
    import essentia.standard as es

    assert _MODELS_DIR is not None
    result = _run(audio, include_embeddings=True)
    coarse = [w for w in result["windows"] if w["tier"] == "coarse"]
    assert len(coarse) == result["coarse_windows_total"] == 3

    payload = result["embeddings"]
    assert (payload["model"], payload["output"], payload["dim"]) == (EMBEDDING_MODEL, EMBEDDING_OUTPUT, EMBEDDING_DIM)
    assert [row["window_index"] for row in payload["windows"]] == [w["window_index"] for w in coarse]
    got = np.stack([decode_embedding(row["vector"]) for row in payload["windows"]])
    assert got.shape == (len(coarse), EMBEDDING_DIM)
    assert got.dtype == np.float16

    spans = [(w["window_index"], w["start_sec"], w["end_sec"]) for w in coarse]
    decoded = analysis_mod._decode_windows_streaming(str(audio), _COARSE_SAMPLE_RATE, spans)
    wrapper = es.TensorflowPredictEffnetDiscogs(graphFilename=f"{_MODELS_DIR}/{GENRE_MODEL.filename}.pb", output=EMBEDDING_OUTPUT)
    reference = np.stack([np.mean(np.atleast_2d(np.asarray(wrapper(decoded[idx]))), axis=0) for idx, _s, _e in spans])

    np.testing.assert_allclose(got.astype(np.float32), reference, rtol=2.0**-11, atol=1e-7)
    assert len({row["vector"] for row in payload["windows"]}) == len(coarse), "distinct windows must not share one vector"


@pytest.mark.integration
@pytest.mark.parametrize("grids", [False, True], ids=["no_grids", "grids"])
def test_the_rest_of_the_result_is_identical_with_and_without_embeddings(audio: Path, grids: bool) -> None:
    plain = _run(audio, include_grids=grids)
    with_embeddings = _run(audio, include_grids=grids, include_embeddings=True)
    assert "embeddings" not in plain
    assert {k: v for k, v in with_embeddings.items() if k != "embeddings"} == plain


# --- memory: repeated multi-chunk analyses must not grow peak RSS ----------------------------------------------------

# Several chunks per run, so the chunk boundary (graph build, sweep, release, PCM clear) is crossed with the option on:
# 30 s coarse windows over 180 s are 6 windows, and a chunk of 2 makes 3 coarse chunks per run, each building and
# releasing the fused graph once. The chunk size is monkeypatched rather than the audio lengthened, as the D-07 tests do:
# the property is about chunk BOUNDARIES.
_RSS_SECONDS = 180
_RSS_COARSE = 30
_RSS_CHUNK = 2
# TWO warm-up runs, measured: run 2 still raises the high-water mark (+103 MiB with the option off, +45 MiB with it on,
# 2026-11-13, arm64 Darwin), and from run 3 on it holds to within 6 MiB either way. One warm-up left first-use growth
# inside the measured window (156.3 MiB, a false red) whenever this test ran first in its process.
_RSS_WARMUP = 2
_RSS_MEASURED = 2
_RSS_BUDGET_BYTES = 64 * 1024 * 1024


def _hwm_bytes() -> int:
    """Whole-process peak RSS in bytes, from the kernel's high-water mark (macOS bytes, Linux KiB)."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


@pytest.mark.integration
def test_repeated_multi_chunk_analyses_with_embeddings_do_not_grow_peak_rss(tmp_path: Path) -> None:
    """N identical multi-chunk analyses with the option on must not cost N times one analysis's memory.

    Nothing changes between runs, so the high-water mark may rise during the warm-up and must then hold. The budget is
    written against the leak this option could plausibly introduce: a fused graph (one TensorFlow session) retained each
    time it is built, i.e. once per chunk. Planted by hand (wrapping ``_construct_classifier`` to keep every fused
    classifier) it grew peak RSS by 701.4 MiB over the two measured runs, twelve sessions retained in all, against a
    64 MiB budget; the clean code passes with this test first in its process.
    """
    path = tmp_path / "rss.wav"
    _write_wav(path, _RSS_SECONDS)
    assert _MODELS_DIR is not None

    def _analyze() -> dict[str, Any]:
        with patch.object(analysis_mod, "_COARSE_CHUNK_WINDOWS", _RSS_CHUNK):
            return analyze_file(
                str(path), _MODELS_DIR, fine_window_sec=_FINE, coarse_window_sec=_RSS_COARSE, fine_min_sec=_FINE_MIN, include_embeddings=True
            )

    for _ in range(_RSS_WARMUP):
        _analyze()
    gc.collect()
    settled = _hwm_bytes()

    for _ in range(_RSS_MEASURED):
        result = _analyze()
        assert len(result["embeddings"]["windows"]) == result["coarse_windows_analyzed"] == _RSS_SECONDS // _RSS_COARSE, (
            "the run must keep producing every window's embedding -- a flat curve that drops embeddings is not a pass"
        )
        del result
        gc.collect()

    grew = _hwm_bytes() - settled
    assert analysis_mod._classifier_cache == {}
    assert grew < _RSS_BUDGET_BYTES, (
        f"peak RSS grew {grew / 2**20:.1f} MiB across {_RSS_MEASURED} identical multi-chunk analyses with embeddings on "
        f"(budget {_RSS_BUDGET_BYTES / 2**20:.0f} MiB): something on the embedding path is retained per chunk"
    )
