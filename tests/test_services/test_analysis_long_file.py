"""Phase 31 integration tests: bounded-memory windowing + real-decode crash guard.

Together these are the automated proof of the core crash/OOM fix (complementing the
Plan 31-01 homelab spike, which did the real-file >=1.49h validation). They are
split into two fast, honest halves because decoding a *real* >=2h file end-to-end
is not CI-feasible — essentia runs at ~0.3s wall per second-of-audio, so a real 2h
decode is ~35 min, and VALIDATION.md already records that a real multi-hour fixture
"Requires a real multi-hour archive file unavailable in CI fixtures" (that is the
spike's job, not CI's).

1. ``test_long_file_bounded`` — proves the *windowing loop* never accumulates over a
   >=2h file. essentia is mocked so ``EasyLoader`` returns a realistically-sized
   (~5MB) buffer per window; the loop must discard each buffer. If the loop wrongly
   retained buffers, 240 fine windows x ~5MB would add >1GB — the asserted RSS
   increment threshold catches that. This is the bounded-memory-at-2h-scale proof.

2. ``test_real_decode_short_no_overflow`` — proves the *real* essentia decode path
   (``EasyLoader`` + ``RhythmExtractor2013`` + ``KeyExtractor``) completes on real
   30s window buffers with no ``OnsetDetectionGlobal`` overflow. Short (real ~90s
   synthetic WAV) so it is fast. Only the TF model pass is mocked (no ``.pb`` graphs
   in CI). This is the crash-fix proof on real buffers.

By construction a >=2h file is only ever fed 30s/180s buffers (test 2 proves those
are safe) by a loop that never accumulates (test 1 proves that at 2h scale) — so the
whole-file ``OnsetDetectionGlobal`` overflow and whole-file OOM cannot occur.

Both marked ``integration`` (deselected by the default unit run).
"""

from __future__ import annotations

import resource
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
import wave

import numpy as np
import pytest

import phaze.services.analysis as analysis_mod
from phaze.services.analysis import analyze_file


if TYPE_CHECKING:
    from pathlib import Path


_SOURCE_RATE = 8000  # cheap source rate; EasyLoader resamples to 44.1k/16k regardless
_FINE_BUF_SAMPLES = 1_323_000  # 30s @ 44.1kHz float32 ~= 5.3MB per window buffer

# Mocked-decode 2h-scale memory test durations.
_SHORT_SEC = 240.0  # 4 min
_LONG_SEC = 7210.0  # just over 2 hours

# Generous bound: if the loop retained all 240 fine + 40 coarse window buffers it
# would hold >1.4GB; a non-accumulating loop keeps the long-vs-short peak increment
# far below this.
_MAX_RSS_INCREMENT_MB = 400.0


def _ru_maxrss_mb() -> float:
    """Process peak RSS in MB (ru_maxrss is KB on Linux, bytes on macOS)."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 if sys.platform == "darwin" else 1.0  # bytes->KB on macOS
    return (raw / divisor) / 1024.0


def _mock_predict_single(_audio: object, _model: object, _models_dir: str) -> np.ndarray:
    """Stand in for a TF model prediction (no .pb graphs in CI)."""
    return np.array([0.7, 0.3], dtype=np.float32)


def _mock_get_labels(model_filename: str, _models_dir: str) -> list[str]:
    if "discogs" in model_filename:
        return [f"Genre{i}" for i in range(400)]
    return ["positive_class", "negative_class"]


def _build_mock_es() -> MagicMock:
    """essentia mock whose EasyLoader returns a FRESH ~5MB buffer per window call."""
    mock_es = MagicMock()

    loader_instance = MagicMock()
    # Fresh allocation each call so any accidental retention shows up in RSS.
    loader_instance.side_effect = lambda: np.zeros(_FINE_BUF_SAMPLES, dtype=np.float32)
    mock_es.EasyLoader.return_value = loader_instance

    rhythm = MagicMock()
    rhythm.return_value = (128.0, np.array([0.5]), 3.8, np.array([]), np.array([0.5]))
    mock_es.RhythmExtractor2013.return_value = rhythm

    key = MagicMock()
    key.return_value = ("C", "minor", 0.8)
    mock_es.KeyExtractor.return_value = key
    return mock_es


def _write_sine_wav(path: str, total_sec: int) -> None:
    """Write a mono int16 sine WAV of ``total_sec`` seconds, one second at a time."""
    t = np.arange(_SOURCE_RATE) / _SOURCE_RATE
    chunk = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SOURCE_RATE)
        for _ in range(total_sec):
            w.writeframes(chunk)


@pytest.mark.integration
def test_long_file_bounded() -> None:
    """A >=2h file's window loop completes and does NOT accumulate memory with length."""
    mock_es = _build_mock_es()
    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_predict_single", side_effect=_mock_predict_single),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_get_labels),
        patch.object(analysis_mod, "_probe_duration_sec", return_value=_SHORT_SEC),
    ):
        short_result = analyze_file("/fake/short.mp3", "/fake/models")
        rss_after_short = _ru_maxrss_mb()

    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_predict_single", side_effect=_mock_predict_single),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_get_labels),
        patch.object(analysis_mod, "_probe_duration_sec", return_value=_LONG_SEC),
    ):
        long_result = analyze_file("/fake/long.mp3", "/fake/models")
        rss_after_long = _ru_maxrss_mb()

    short_fine = [w for w in short_result["windows"] if w["tier"] == "fine"]
    long_fine = [w for w in long_result["windows"] if w["tier"] == "fine"]

    # The loop traversed the whole >=2h file (7210s / 30s ~= 240 fine windows).
    assert len(long_fine) >= 200, f"expected the loop to cover the whole 2h file; got {len(long_fine)} fine windows"
    # Window count scales with length (the loop really iterates the longer file).
    assert len(long_fine) > len(short_fine) * 10

    # Peak RSS does NOT scale with file length. ru_maxrss is a monotonic high-water
    # mark, so the increment after the long run over the short run is exactly how
    # much higher (if at all) the >=2h file pushed peak memory. Each window holds one
    # ~5MB buffer that must be discarded before the next.
    increment_mb = rss_after_long - rss_after_short
    assert increment_mb < _MAX_RSS_INCREMENT_MB, (
        f"peak RSS grew {increment_mb:.1f}MB from short->long file; the window loop must not accumulate buffers (threshold {_MAX_RSS_INCREMENT_MB}MB)"
    )


@pytest.mark.integration
def test_real_decode_short_no_overflow(tmp_path: Path) -> None:
    """Real EasyLoader + RhythmExtractor2013 + KeyExtractor on real 30s buffers: no overflow."""
    path = str(tmp_path / "real.wav")
    _write_sine_wav(path, 90)  # 3 fine (30s) + 1 coarse window; real decode

    # Real decode + real rhythm/key; only the TF model pass is mocked (no .pb in CI).
    with (
        patch.object(analysis_mod, "_predict_single", side_effect=_mock_predict_single),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_get_labels),
    ):
        result = analyze_file(path, "/fake/models")  # must NOT raise OnsetDetectionGlobal overflow

    fine = [w for w in result["windows"] if w["tier"] == "fine"]
    coarse = [w for w in result["windows"] if w["tier"] == "coarse"]
    assert [(w["start_sec"], w["end_sec"]) for w in fine] == [(0.0, 30.0), (30.0, 60.0), (60.0, 90.0)]
    assert len(coarse) == 1
    # A real BPM aggregate was produced from the real fine-window decode.
    assert result["bpm"] is not None
