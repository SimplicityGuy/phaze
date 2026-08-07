"""Phase 31 integration tests: bounded-memory windowing + real-decode crash guard.

Together these are the automated proof of the core crash/OOM fix (complementing the
Plan 31-01 homelab spike, which did the real-file >=1.49h validation). They are
split into two fast, honest halves because decoding a *real* >=2h file end-to-end
is not CI-feasible — essentia runs at ~0.3s wall per second-of-audio, so a real 2h
decode is ~35 min, and VALIDATION.md already records that a real multi-hour fixture
"Requires a real multi-hour archive file unavailable in CI fixtures" (that is the
spike's job, not CI's).

1. ``test_long_file_bounded`` — proves the *windowing loop* never accumulates with file
   LENGTH. essentia is mocked so the decode yields a realistically-sized (~5MB) buffer per
   window. Phase 43 strides a long file down to 60 fine + 30 coarse windows (cost no longer
   scales with length); if the pipeline wrongly retained buffers in proportion to the file's
   ~240 natural fine windows it would add >1.2GB, and >7GB for the 12h file. This is the
   bounded-memory proof.

   Both tiers now retain their ``<=cap`` buffers *deliberately*. COARSE has since
   phaze-15sw, because model-major inference needs every window in hand before the first
   graph is built — the trade that removed ~4 GiB of co-resident TF graphs. FINE does since
   phaze-5lop, because its windows come off ONE streaming decode pass instead of one
   non-seeking ``EasyLoader`` call each. So the invariant this test asserts is the one that
   actually holds and the one that matters: retention is bounded by the CAPS, constants, and
   never by duration.

   Each duration is measured in its OWN forked process (``_peak_rss_for``). Differencing
   ``ru_maxrss`` across runs inside one process — what this test used to do — does not
   measure per-run retention: the mark is monotonic, so it only moves when a run exceeds
   every earlier one, and the allocator's fragmentation ramp does that on an arbitrary
   iteration (measured: a repeat of the SAME duration moved it 153MB, following the run's
   position in the sequence rather than the file's length). Per-process peaks reproduce to
   0.1MB across durations and orderings.

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

import multiprocessing
import resource
import sys
from typing import TYPE_CHECKING, Any
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

_BUF_MB = _FINE_BUF_SAMPLES * 4 / 1024 / 1024  # ~5.05 MB per mocked window buffer

# The designed concurrent retention, in buffers, of a file at or above BOTH caps. Since
# phaze-5lop each tier decodes in ONE streaming pass and so holds its own cap's worth of
# windows: FINE 60 x 30 s @ 44.1 kHz, then (after the fine buffers are dropped and trimmed)
# COARSE 30 x 180 s @ 16 kHz, which phaze-15sw already required so a model-major sweep can
# run one graph across every window. The two never overlap in the pipeline, but ru_maxrss is
# a high-water mark and the platform running this test may not return the fine tier's pages
# to the OS before the coarse tier faults its own (macOS has no `malloc_trim`), so the bound
# is the SUM -- the honest worst case for this instrument.
_DESIGNED_RETENTION_MB = (_DEFAULT_FINE_CAP + _DEFAULT_COARSE_CAP) * _BUF_MB

# Headroom over that for interpreter, mock and allocator overhead. Measured 1.05x on macOS
# (478.8 MB against a 454.2 MB design) for both the 2h and the 12h file; 1.35x is a ceiling
# that a real duration-proportional retention could not fit under -- a 12h file's natural
# 1440 fine windows would be ~7.3 GB.
_MAX_PEAK_RATIO = 1.35

# 2h vs 12h stride to the SAME 60 fine + 30 coarse windows, so their designed retention is
# identical and their peaks must be too. This is the sharp assertion -- it is what "cost does
# not scale with duration" actually means. Measured difference: 0.1 MB (0.02%) on macOS.
_MAX_CAPPED_DELTA_MB = 25.0


def _ru_maxrss_mb() -> float:
    """Process peak RSS in MB (ru_maxrss is KB on Linux, bytes on macOS)."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 if sys.platform == "darwin" else 1.0  # bytes->KB on macOS
    return (raw / divisor) / 1024.0


def _peak_rss_child(duration_sec: float, queue: Any) -> None:  # pragma: no cover -- runs in a forked child
    """Analyze one mocked file of ``duration_sec`` and post this child's own peak RSS delta."""
    before = _ru_maxrss_mb()
    mock_es = _build_mock_es()
    # A *touched* buffer, unlike `np.zeros`, actually faults its pages -- which is what makes
    # retention visible to ru_maxrss at all. With zero pages the whole measurement reads ~0 and
    # would pass no matter how many buffers were held.
    mock_es.EasyLoader.return_value.side_effect = lambda: np.full(_FINE_BUF_SAMPLES, 0.5, dtype=np.float32)
    _run_at(duration_sec, mock_es)
    queue.put(_ru_maxrss_mb() - before)


def _peak_rss_for(duration_sec: float) -> float:
    """Peak RSS in MB of analyzing one mocked file of ``duration_sec``, in its OWN process.

    Measuring in a fresh child rather than by differencing ``ru_maxrss`` across runs in one
    process is not fastidiousness -- the cross-run difference does not measure what it looks
    like it measures. ``ru_maxrss`` is monotonic, so a later run only moves it when it exceeds
    every earlier one, and the allocator's fragmentation ramp does that on an arbitrary
    iteration: measured here, a repeat of the SAME duration moved the in-process high-water by
    153 MB, and the jump followed the run's POSITION in the sequence, not the file's length.
    A per-process peak is reproducible to 0.1 MB across durations and orderings, which is what
    lets the assertions below be sharp instead of merely loose enough to hide the noise.
    """
    ctx = multiprocessing.get_context("fork")  # inherits the imported module + its mock patches
    queue = ctx.Queue()
    proc = ctx.Process(target=_peak_rss_child, args=(duration_sec, queue))
    proc.start()
    peak_mb = queue.get()
    proc.join()
    return float(peak_mb)


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


def _run_at(duration_sec: float, mock_es: MagicMock) -> dict[str, object]:
    """analyze_file over a mocked file of ``duration_sec``."""
    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_predict_single", side_effect=_mock_predict_single),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_get_labels),
        patch.object(analysis_mod, "_probe_duration_sec", return_value=duration_sec),
    ):
        return analyze_file("/fake/audio.mp3", "/fake/models")


@pytest.mark.integration
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded, use of fork.*:DeprecationWarning")
def test_long_file_bounded() -> None:
    """A >=2h file's window loop completes and does NOT accumulate memory with length.

    ``fork`` is chosen over ``spawn`` deliberately and the warning is silenced rather than
    dodged: the importing process is multi-threaded because importing this module imports
    essentia, which imports TensorFlow, which starts its thread pools. A ``spawn`` child
    would pay that import again per measurement AND would not inherit the mock patches this
    measurement is built on, so it would measure a different pipeline. The child does nothing
    between fork and exit but numpy allocation under an already-mocked essentia.
    """
    mock_es = _build_mock_es()
    short_result = _run_at(_SHORT_SEC, mock_es)
    long_result = _run_at(_LONG_SEC, mock_es)
    longer_result = _run_at(_LONGER_SEC, mock_es)

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

    # Each peak is measured in its OWN process (see `_peak_rss_for`), so these are three
    # independent measurements rather than three points on one monotonic high-water curve.
    short_peak = _peak_rss_for(_SHORT_SEC)
    long_peak = _peak_rss_for(_LONG_SEC)
    longer_peak = _peak_rss_for(_LONGER_SEC)

    # The absolute bound: a >=2h file's peak is the DESIGNED cap-bounded retention (fine cap
    # then coarse cap, phaze-5lop / phaze-15sw) plus overhead -- not something proportional to
    # its ~240 natural fine windows, which would be ~1.2 GB, nor to the 12h file's ~1440.
    ceiling_mb = _DESIGNED_RETENTION_MB * _MAX_PEAK_RATIO
    assert long_peak < ceiling_mb, (
        f"a 2h file peaked at {long_peak:.1f}MB, past {ceiling_mb:.0f}MB "
        f"({_MAX_PEAK_RATIO}x the {_DESIGNED_RETENTION_MB:.0f}MB of designed cap-bounded retention); "
        f"decoded buffers must be bounded by the caps, not by the natural window count"
    )
    # The short file is under both caps, so it must hold -- and peak at -- strictly less.
    assert short_peak < long_peak, f"a 4min file ({short_peak:.1f}MB) must not peak at or above a 2h file ({long_peak:.1f}MB)"

    # The sharp one: 6x the duration, identical caps, so identical retention and identical
    # peak. Anything that scales with duration rather than with the cap shows up here, and
    # cannot hide behind the ceiling above.
    capped_delta_mb = abs(longer_peak - long_peak)
    assert capped_delta_mb < _MAX_CAPPED_DELTA_MB, (
        f"peak RSS moved {capped_delta_mb:.1f}MB between a 2h file ({long_peak:.1f}MB) and a 12h file "
        f"({longer_peak:.1f}MB) at identical caps; per-file memory must be bounded by the cap, not by "
        f"duration (threshold {_MAX_CAPPED_DELTA_MB}MB)"
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
