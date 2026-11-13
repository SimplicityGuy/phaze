"""phaze-l5mzm on the REAL graph: the fused classifier IS the wrapper, plus the embedding, at flat memory.

Every test here loads the real ``discogs-effnet-bs64-1.pb`` and runs real essentia, and SKIPS where the
weights are absent (CI today) -- a skip, not a pass. None of them can be satisfied by a mocked essentia:
the claim under test is that a hand-built front end feeding the low-level ``TensorflowPredict`` reproduces
``TensorflowPredictEffnetDiscogs`` exactly, and that only the real graph can answer.

The reference ("a direct graph call") is the wrapper phaze has always used for the genre model, asked for
each output in turn: ``output="PartitionedCall:0"`` for the logits and ``output="PartitionedCall:1"`` for
the embedding, pooled with ``np.mean(axis=0)`` exactly as ``_predict_single`` pools the logits.
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import resource
import sys
from typing import Any

import numpy as np
import pytest

from phaze.services import analysis as analysis_mod
from phaze.services.analysis import GENRE_MODEL, FusedEffnetDiscogs
from phaze.services.analysis_embeddings import EMBEDDING_DIM, EMBEDDING_OUTPUT, LOGITS_DIM, LOGITS_OUTPUT, embedding_of
from tests._real_models import ENV_VAR as _REAL_MODELS_ENV_VAR


_REAL_MODELS_DIR = os.environ.get(_REAL_MODELS_ENV_VAR) or None

pytestmark = pytest.mark.skipif(
    _REAL_MODELS_DIR is None,
    reason=f"REAL-GRAPH EMBEDDING TESTS NOT RUN (a skip, not a pass): {_REAL_MODELS_ENV_VAR} is unset or empty",
)

_SAMPLE_RATE = 16000

# Lengths chosen for the patch arithmetic (128-frame patches at hop 62, 64 patches per batch), all
# measured against the wrapper on 2026-11-13: 2.1 s is the one-patch case (phaze-ouz0y squeezes it to
# 1-D in some paths), 4.0 / 4.1 s sit either side of a patch boundary, 30 s is one partial batch, 63.9 /
# 64.5 s are 63 patches (one short of a full batch), 180 s is the coarse window (180 patches, three
# batches), 200.5 s a non-integral length past it.
_LENGTHS_SEC = (2.1, 4.0, 4.1, 30.0, 63.9, 64.5, 180.0, 200.5)
_SHORT_SEC = (0.5, 2.0)  # under one patch: the wrapper yields no patch at all

# float16 has an 11-bit significand: a narrowed value is within 2**-11 of itself, relatively.
_FLOAT16_RTOL = 2.0**-11


def _graph_path() -> str:
    assert _REAL_MODELS_DIR is not None
    return str(Path(_REAL_MODELS_DIR) / f"{GENRE_MODEL.filename}.pb")


def _noise(seconds: float, seed: int) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(int(_SAMPLE_RATE * seconds)) * 0.1).astype(np.float32)


def _tone_mix(seconds: float) -> np.ndarray:
    """A deterministic, pitched, gated signal: not noise, so the graph's activations are structured."""
    t = np.arange(int(_SAMPLE_RATE * seconds)) / _SAMPLE_RATE
    return (0.3 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * 331 * t) * (np.sin(2 * np.pi * 2 * t) > 0)).astype(np.float32)


@pytest.fixture(scope="module")
def wrappers() -> dict[str, Any]:
    import essentia.standard as es

    return {output: es.TensorflowPredictEffnetDiscogs(graphFilename=_graph_path(), output=output) for output in (LOGITS_OUTPUT, EMBEDDING_OUTPUT)}


@pytest.fixture(scope="module")
def fused() -> FusedEffnetDiscogs:
    return FusedEffnetDiscogs(_graph_path())


def _assert_matches_the_wrapper(audio: np.ndarray, wrappers: dict[str, Any], fused: FusedEffnetDiscogs) -> None:
    reference_logits = np.atleast_2d(np.asarray(wrappers[LOGITS_OUTPUT](audio)))
    reference_embedding = np.mean(np.atleast_2d(np.asarray(wrappers[EMBEDDING_OUTPUT](audio))), axis=0)

    got = fused(audio)
    embedding = embedding_of(got)

    assert got.shape == reference_logits.shape
    assert got.shape[1] == LOGITS_DIM
    # The logits are what the genre model has always read: the fused route must not move them at all,
    # or switching the option on would change every file's genre output.
    np.testing.assert_array_equal(np.asarray(got), reference_logits)
    assert embedding is not None
    assert embedding.shape == (EMBEDDING_DIM,)
    assert embedding.dtype == np.float32
    np.testing.assert_array_equal(embedding, reference_embedding)
    # ...and what leaves the process is the float16 narrowing, within float16's own precision.
    np.testing.assert_allclose(embedding.astype(np.float16).astype(np.float32), reference_embedding, rtol=_FLOAT16_RTOL, atol=1e-7)


@pytest.mark.parametrize("seconds", _LENGTHS_SEC)
def test_fused_logits_and_embedding_equal_the_wrapper_on_noise(seconds: float, wrappers: dict[str, Any], fused: FusedEffnetDiscogs) -> None:
    _assert_matches_the_wrapper(_noise(seconds, seed=int(seconds * 10)), wrappers, fused)


def test_fused_logits_and_embedding_equal_the_wrapper_on_a_tone_mix(wrappers: dict[str, Any], fused: FusedEffnetDiscogs) -> None:
    _assert_matches_the_wrapper(_tone_mix(180.0), wrappers, fused)


@pytest.mark.parametrize("seconds", _SHORT_SEC)
def test_under_one_patch_the_fused_classifier_answers_like_the_wrapper(seconds: float, wrappers: dict[str, Any], fused: FusedEffnetDiscogs) -> None:
    """The wrapper returns a bare ``(0,)`` array and no embedding exists; the fused route says the same."""
    audio = _noise(seconds, seed=1)
    reference = np.asarray(wrappers[LOGITS_OUTPUT](audio))

    got = fused(audio)

    assert reference.shape == (0,)
    assert got.shape == reference.shape
    assert embedding_of(got) is None


def test_the_fused_classifier_is_reusable_across_buffers(wrappers: dict[str, Any], fused: FusedEffnetDiscogs) -> None:
    """One instance sweeps every window of a chunk: no state may carry from one buffer to the next."""
    for seed, seconds in ((11, 30.0), (12, 7.0), (13, 30.0)):
        _assert_matches_the_wrapper(_noise(seconds, seed), wrappers, fused)


# --- memory: repeated sweeps must not grow peak RSS ----------------------------------------------------

_SWEEP_WINDOWS = 6
_SWEEP_WINDOW_SEC = 150.0  # 148 patches: three batches, the last one partial
_SWEEP_WARMUP = 1
_SWEEP_MEASURED = 3
# Retaining ONE padded input batch per call ([64, 1, 128, 96] float32, 3 MiB) -- the smallest leak this
# route could have, e.g. a Pool kept alive -- is 3 batches x 6 windows x 3 sweeps = 162 MiB. The budget is
# far under that and far over allocator noise.
_SWEEP_BUDGET_BYTES = 48 * 1024 * 1024


def _hwm_bytes() -> int:
    """Whole-process peak RSS in bytes, from the kernel's high-water mark (macOS bytes, Linux KiB)."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


@pytest.mark.integration
def test_repeated_fused_sweeps_do_not_grow_peak_rss() -> None:
    """N identical sweeps of a chunk must not cost N times one sweep's memory (the D-09 shape, for the new graph call).

    Each sweep is what the genre sweep does per chunk with the option on: build the classifier, run it
    over every window, keep only the pooled embedding per window, release it through the production
    ``_release_classifier`` path. Nothing changes between sweeps, so the high-water mark may rise on the
    first and must then hold.
    """
    buffers = [_noise(_SWEEP_WINDOW_SEC, seed=100 + i) for i in range(_SWEEP_WINDOWS)]

    def _sweep() -> list[np.ndarray]:
        analysis_mod._classifier_cache["fused-rss-probe"] = FusedEffnetDiscogs(_graph_path())
        try:
            kept = []
            for buf in buffers:
                embedding = embedding_of(analysis_mod._classifier_cache["fused-rss-probe"](buf))
                assert embedding is not None
                kept.append(embedding)
            return kept
        finally:
            analysis_mod._release_classifier("fused-rss-probe")

    for _ in range(_SWEEP_WARMUP):
        _sweep()
    gc.collect()
    settled = _hwm_bytes()

    for _ in range(_SWEEP_MEASURED):
        kept = _sweep()
        assert len(kept) == _SWEEP_WINDOWS
        del kept
        gc.collect()

    grew = _hwm_bytes() - settled
    assert "fused-rss-probe" not in analysis_mod._classifier_cache
    assert grew < _SWEEP_BUDGET_BYTES, (
        f"peak RSS grew {grew / 2**20:.1f} MiB across {_SWEEP_MEASURED} identical fused sweeps "
        f"(budget {_SWEEP_BUDGET_BYTES / 2**20:.0f} MiB): the fused EffNet call is retaining memory per call"
    )
