"""Phase 31 integration tests: bounded-memory windowing + real-decode crash guard.

Together these are the automated proof of the core crash/OOM fix (complementing the
Plan 31-01 homelab spike, which did the real-file >=1.49h validation). They are
split into two fast, honest halves because decoding a *real* >=2h file end-to-end
is not CI-feasible — essentia runs at ~0.3s wall per second-of-audio, so a real 2h
decode is ~35 min, and VALIDATION.md already records that a real multi-hour fixture
"Requires a real multi-hour archive file unavailable in CI fixtures" (that is the
spike's job, not CI's).

1. ``test_long_file_bounded`` — proves the *windowing loop* never accumulates with file
   LENGTH. essentia is mocked so ``EasyLoader`` returns a realistically-sized (~5MB)
   buffer per window. Phase 43 strides a long file down to 60 fine + 30 coarse windows
   (cost no longer scales with length); if the loop wrongly retained buffers in
   proportion to the file's ~240 natural fine windows it would add >1.2GB — the asserted
   RSS increment threshold catches that. This is the bounded-memory proof.

   Since phaze-15sw the coarse tier retains its ``<=coarse_cap`` buffers *deliberately*
   (model-major inference needs every window in hand before the first graph is built —
   the trade that removed ~4 GiB of co-resident TF graphs). So the invariant this test
   asserts is the one that actually holds and the one that matters: retention is bounded
   by the CAP, a constant, not by duration. The fine tier still discards per window.

2. ``test_real_decode_short_no_overflow`` — proves the *real* essentia decode path
   (``EasyLoader`` + ``RhythmExtractor2013`` + ``KeyExtractor``) completes on real
   30s window buffers with no ``OnsetDetectionGlobal`` overflow. Short (real ~90s
   synthetic WAV) so it is fast. Only the TF model pass is mocked (no ``.pb`` graphs
   in CI). This is the crash-fix proof on real buffers.

By construction a >=2h file is only ever fed 30s/180s buffers (test 2 proves those
are safe) by a loop whose retention is capped, not length-proportional (test 1 proves
that at 2h and 12h scale) — so the whole-file ``OnsetDetectionGlobal`` overflow and
whole-file OOM cannot occur.

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
from phaze.services.analysis import _DEFAULT_COARSE_CAP, _DEFAULT_FINE_CAP, analyze_file


if TYPE_CHECKING:
    from pathlib import Path


_SOURCE_RATE = 8000  # cheap source rate; EasyLoader resamples to 44.1k/16k regardless
_FINE_BUF_SAMPLES = 1_323_000  # 30s @ 44.1kHz float32 ~= 5.3MB per window buffer

# Mocked-decode 2h-scale memory test durations.
_SHORT_SEC = 240.0  # 4 min  -> 8 fine, 2 coarse (all under the caps)
_LONG_SEC = 7210.0  # just over 2 hours -> ~240 natural fine, strided to 60/30
_LONGER_SEC = 43_200.0  # 12 hours -> ~1440 natural fine, strided to the SAME 60/30

# Two bounds, because phaze-15sw made the coarse tier's retention intentional.
#
# short -> long: the coarse tier legitimately grows from 2 to _DEFAULT_COARSE_CAP (30)
# concurrently-held buffers, since model-major inference needs every window in hand
# before the first TF graph is built. That is ~30 x 5.3MB = ~159MB of *designed* growth,
# so the bound must clear it; anything past ~1.6x it is a real leak.
_DESIGNED_COARSE_RETENTION_MB = _DEFAULT_COARSE_CAP * (_FINE_BUF_SAMPLES * 4 / 1024 / 1024)
_MAX_RSS_INCREMENT_MB = 260.0

# long -> longer: 2h and 12h stride to the SAME 60 fine + 30 coarse windows, so the
# designed retention is identical and the increment must be ~zero. This is the sharp
# assertion -- it is what "cost does not scale with duration" actually means, and a
# 6x duration increase cannot hide behind the cap allowance above.
_MAX_CAPPED_INCREMENT_MB = 60.0


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


def _run_at(duration_sec: float, mock_es: MagicMock) -> tuple[dict[str, object], float]:
    """analyze_file over a mocked file of ``duration_sec``; returns (result, peak RSS MB)."""
    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_predict_single", side_effect=_mock_predict_single),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_get_labels),
        patch.object(analysis_mod, "_probe_duration_sec", return_value=duration_sec),
    ):
        return analyze_file("/fake/audio.mp3", "/fake/models"), _ru_maxrss_mb()


@pytest.mark.integration
def test_long_file_bounded() -> None:
    """A >=2h file's window loop completes and does NOT accumulate memory with length."""
    mock_es = _build_mock_es()
    short_result, rss_after_short = _run_at(_SHORT_SEC, mock_es)
    long_result, rss_after_long = _run_at(_LONG_SEC, mock_es)
    longer_result, rss_after_longer = _run_at(_LONGER_SEC, mock_es)

    short_fine = [w for w in short_result["windows"] if w["tier"] == "fine"]
    long_fine = [w for w in long_result["windows"] if w["tier"] == "fine"]

    # Phase 43: per-file cost is bounded by an even-stride cap. The loop still spans
    # the whole >=2h file (its natural ~240 fine windows are recorded as coverage),
    # but only the fine cap (60) are kept & decoded — cost no longer scales with length.
    assert len(long_fine) == _DEFAULT_FINE_CAP, f"long file should be strided down to {_DEFAULT_FINE_CAP} fine windows; got {len(long_fine)}"
    assert long_result["sampled"] is True
    assert long_result["fine_windows_total"] >= 200, "coverage must record the full natural window count of the >=2h file"
    # The short file is under the cap, so it is NOT strided: its count stays natural
    # and below the cap, and the long file does NOT scale up with length (it is capped).
    assert len(short_fine) < _DEFAULT_FINE_CAP
    assert len(short_fine) < len(long_fine) <= _DEFAULT_FINE_CAP

    # Both long files stride to the same 60/30, so their coverage differs but their work
    # -- and their designed retention -- does not.
    assert longer_result["fine_windows_total"] > long_result["fine_windows_total"] * 4
    assert len([w for w in longer_result["windows"] if w["tier"] == "fine"]) == _DEFAULT_FINE_CAP

    # ru_maxrss is a monotonic high-water mark, so the increment between two runs is
    # exactly how much higher the later one pushed peak memory.
    #
    # short -> long: allowed to grow by the DESIGNED coarse retention (2 -> 30 concurrent
    # buffers, phaze-15sw), and no further. The fine tier must still discard per window.
    increment_mb = rss_after_long - rss_after_short
    assert increment_mb < _MAX_RSS_INCREMENT_MB, (
        f"peak RSS grew {increment_mb:.1f}MB from short->long file, past the "
        f"{_DESIGNED_COARSE_RETENTION_MB:.0f}MB of designed cap-bounded coarse retention "
        f"(threshold {_MAX_RSS_INCREMENT_MB}MB); the fine loop must not accumulate buffers"
    )

    # long -> longer: the sharp one. 6x the duration, identical caps, so identical
    # retention. Anything that scales with duration rather than with the cap shows here.
    capped_increment_mb = rss_after_longer - rss_after_long
    assert capped_increment_mb < _MAX_CAPPED_INCREMENT_MB, (
        f"peak RSS grew {capped_increment_mb:.1f}MB going from a 2h to a 12h file at identical caps; "
        f"per-file memory must be bounded by the cap, not by duration (threshold {_MAX_CAPPED_INCREMENT_MB}MB)"
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
