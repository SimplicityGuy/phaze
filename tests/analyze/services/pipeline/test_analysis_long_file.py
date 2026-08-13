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
   window.

   **This test's subject changed with phaze-w55w1 and its assertions had to change with it.**
   Under the Phase 43 caps a long file was STRIDED to 60 fine + 30 coarse windows, and
   bounded memory came for free from analyzing less of the file. Analysis is now exhaustive
   (ADR-0007 §7): the 12h file's ~1440 natural fine windows are ALL analyzed, so the old
   proof (``len(fine) == cap``) is gone and the property it stood in for has to be proved
   directly — peak RSS is bounded by the CHUNK, not by the file.

   Both tiers still retain a whole unit of buffers *deliberately*. COARSE has since
   phaze-15sw, because model-major inference needs every window of its unit in hand before
   the first graph is built — the trade that removed ~4 GiB of co-resident TF graphs. FINE
   does since phaze-5lop, because its windows come off ONE streaming decode pass instead of
   one non-seeking ``EasyLoader`` call each. What phaze-w55w1 changed is what that unit IS:
   a CHUNK (:data:`_FINE_CHUNK_WINDOWS` / :data:`_COARSE_CHUNK_WINDOWS`) rather than a cap on
   the whole file. If chunking leaked — if a chunk's buffers outlived it — the 12h file would
   add >7GB here, which is exactly what the peak assertions below refuse.

   **What that paragraph promised, and could not deliver — read this before trusting it
   again (phaze-u1n7j).** Chunking DID leak, this test stayed green, and the pod OOMKilled
   on every file past ~3 hours. The reason is structural and no assertion tuning fixes it:
   essentia is MOCKED here, so the mock's Python buffers are all this can weigh, and the
   thing that actually grew was C++ — the streaming network's connection buffers, retained
   per chunk because dropping a Python proxy does not disconnect an essentia edge (D-09).
   A mocked decode has no edges, so it cannot leak them, and this test would pass just as
   happily on the broken code as on the fixed code. It is a real proof about the WINDOWING
   LOOP and nothing at all about the decode's native memory. The property it appears to
   promise is held by
   ``test_analysis_streaming_decode.py::test_repeated_gated_chunk_decodes_do_not_grow_peak_rss``,
   which runs the real network over real audio for exactly this reason.

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

import gc
import multiprocessing
import resource
import sys
from typing import TYPE_CHECKING, Any
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
_SHORT_SEC = 240.0  # 4 min  -> 8 fine, 2 coarse: ONE partial chunk per tier
_LONG_SEC = 7210.0  # just over 2 hours -> ~240 fine windows = 4 fine chunks, ALL analyzed
_LONGER_SEC = 43_200.0  # 12 hours -> ~1440 fine windows = 24 fine chunks, ALL analyzed

_BUF_MB = _FINE_BUF_SAMPLES * 4 / 1024 / 1024  # ~5.05 MB per mocked window buffer

# The designed concurrent retention, in buffers, of a file large enough to fill BOTH tiers'
# chunks. Since phaze-5lop each tier decodes a whole unit in ONE streaming pass and so holds
# that unit's worth of windows: FINE 60 x 30 s @ 44.1 kHz, then (after the fine buffers are
# dropped and trimmed) COARSE 30 x 180 s @ 16 kHz, which phaze-15sw already required so a
# model-major sweep can run one graph across every window. The two never overlap in the
# pipeline, but ru_maxrss is a high-water mark and the platform running this test may not
# return the fine tier's pages to the OS before the coarse tier faults its own (macOS has no
# `malloc_trim`), so the bound is the SUM -- the honest worst case for this instrument.
#
# phaze-w55w1: the numbers are unchanged; only their SOURCE moved, from the removed caps to
# the chunk sizes. That equality is the whole point of picking those chunk sizes (D-07), and
# it is what keeps ADR-0005's memory limits valid across the cap removal.
_DESIGNED_RETENTION_MB = (analysis_mod._FINE_CHUNK_WINDOWS + analysis_mod._COARSE_CHUNK_WINDOWS) * _BUF_MB

# Headroom over that for interpreter, mock and allocator overhead. Measured 1.05x on macOS
# (478.8 MB against a 454.2 MB design) for both the 2h and the 12h file under the caps; 1.35x
# is a ceiling that a real duration-proportional retention could not fit under -- a 12h file's
# 1440 fine windows are ~7.3 GB if a chunk's buffers ever outlive their chunk.
_MAX_PEAK_RATIO = 1.35

# 2h and 12h now analyze 4 and 24 fine chunks respectively -- 6x the WORK -- but one chunk at
# a time either way, so their designed retention is identical and their peaks must be too.
# This is the sharp assertion, and it is sharper than it was under the caps: back then the two
# runs did identical work, so an equal peak proved little. Now the work differs 6x and the
# peak still must not move, which is what "memory is O(chunk), not O(duration)" actually means.
_MAX_CHUNKED_DELTA_MB = 25.0


def _ru_maxrss_mb() -> float:
    """Process peak RSS in MB (ru_maxrss is KB on Linux, bytes on macOS)."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 if sys.platform == "darwin" else 1.0  # bytes->KB on macOS
    return (raw / divisor) / 1024.0


def _peak_rss_child(duration_sec: float, queue: Any) -> None:  # pragma: no cover -- runs in a forked child
    """Analyze one mocked file of ``duration_sec`` and post this child's own peak RSS delta.

    **``gc.freeze()`` first, and it is the instrument, not a tweak (phaze-u1n7j).** This child
    is ``fork``ed, so its pages start copy-on-write shared with the pytest parent. A
    ``gc.collect()`` anywhere in the code under test then WRITES to the GC header of every
    object inherited from that parent, un-sharing the whole parent heap into this child and
    charging it to this child's ``ru_maxrss``. The pipeline gained exactly such a collect in
    D-09 (`_release_decode_network`), and the effect is not subtle -- measured here, forking
    from a parent holding 3.0 M objects: child peak **7.8 MiB** with no collect, **597.7 MiB**
    with one, **7.9 MiB** with this freeze in front of it. Without the freeze this test
    measures the size of the pytest process that happened to run it, which is why it passed
    against this file alone and failed inside the wider suite.

    ``gc.freeze()`` moves everything already allocated into a permanent generation the
    collector does not touch, so the collect still does its job on what the ANALYSIS allocates
    -- the only thing this test is trying to weigh -- while the inherited heap stays shared.

    Production is unaffected either way and that is why the fix belongs here rather than in the
    pipeline: `analysis_child` is an EXEC'd child with a fresh address space (phaze-b2qs9 §1,
    "one exec'd child per file"), so it has no COW-shared parent heap to un-share.
    """
    gc.freeze()
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


class _StubRhythm:
    """Plain callable standing in for ``RhythmExtractor2013``. NOT a ``MagicMock`` -- see below."""

    def __call__(self, _buf: Any) -> tuple[Any, ...]:
        return (128.0, np.array([0.5]), 3.8, np.array([]), np.array([0.5]))


class _StubKey:
    """Plain callable standing in for ``KeyExtractor``. NOT a ``MagicMock`` -- see below."""

    def __call__(self, _buf: Any) -> tuple[str, str, float]:
        return ("C", "minor", 0.8)


def _build_mock_es() -> MagicMock:
    """essentia mock whose EasyLoader returns a FRESH ~5MB buffer per window call.

    **The per-window extractors are deliberately NOT ``MagicMock``s** (phaze-w55w1), and this
    is load-bearing for the measurement rather than a style choice. A ``MagicMock`` records
    every call in ``mock_calls``, which means it holds a STRONG REFERENCE to each argument it
    was called with -- here, the decoded window buffer. Under the Phase 43 caps that was
    invisible: 60 fine calls retained 60 buffers, ~318 MB, indistinguishable from the designed
    retention the test was measuring. Exhaustive analysis makes it fatal to the instrument: a
    12h file makes 1440 calls, so the mock alone accumulates ~3.7 GiB and the test reports a
    duration-proportional peak that the pipeline does not actually have. Measured here before
    the fix -- 5.365 GiB, against a real per-chunk design of 454 MB.

    Plain callables record nothing, so what ``ru_maxrss`` sees is the pipeline's retention and
    only the pipeline's. ``mock_es`` itself stays a ``MagicMock``: its calls carry a filename
    and a few floats, never a buffer.
    """
    mock_es = MagicMock()

    loader_instance = MagicMock()
    # Fresh allocation each call so any accidental retention shows up in RSS. Called with NO
    # arguments, so its own call recording retains nothing.
    loader_instance.side_effect = lambda: np.zeros(_FINE_BUF_SAMPLES, dtype=np.float32)
    mock_es.EasyLoader.return_value = loader_instance

    mock_es.RhythmExtractor2013.return_value = _StubRhythm()
    mock_es.KeyExtractor.return_value = _StubKey()
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
    # `new=` rather than `side_effect=` for the two per-window seams, for the same reason
    # `_build_mock_es` uses plain callables: a `side_effect=` patch still wraps the function in
    # a MagicMock, and `_predict_single` is called once per (model, window) -- 34 x every coarse
    # window -- so its call log would pin every coarse buffer of the whole file and turn this
    # bounded-memory test into a measurement of unittest.mock.
    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_predict_single", new=_mock_predict_single),
        patch.object(analysis_mod, "_get_labels", new=_mock_get_labels),
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
    longer_fine = [w for w in longer_result["windows"] if w["tier"] == "fine"]

    # phaze-w55w1: EXHAUSTIVE. Every natural window of every file is analyzed -- work now DOES
    # scale with length, deliberately, and it is memory that must not. The counts below are the
    # counterfactual to the Phase 43 assertion this replaces (`len(long_fine) == 60`).
    assert len(long_fine) == long_result["fine_windows_total"] >= 200
    assert len(longer_fine) == longer_result["fine_windows_total"] >= 1400
    assert len(short_fine) == short_result["fine_windows_total"] == 8
    # The 12h file really does do ~6x the fine-window work of the 2h file. That inequality is
    # what makes the peak-equality assertion below meaningful rather than tautological.
    assert len(longer_fine) > len(long_fine) * 4

    # Each peak is measured in its OWN process (see `_peak_rss_for`), so these are three
    # independent measurements rather than three points on one monotonic high-water curve.
    short_peak = _peak_rss_for(_SHORT_SEC)
    long_peak = _peak_rss_for(_LONG_SEC)
    longer_peak = _peak_rss_for(_LONGER_SEC)

    # The absolute bound: a >=2h file's peak is the DESIGNED chunk-bounded retention (one fine
    # chunk, then one coarse chunk -- phaze-5lop / phaze-15sw / D-07) plus overhead, not
    # something proportional to its ~240 fine windows (~1.2 GB) nor to the 12h file's ~1440
    # (~7.3 GB). Both of those are now genuinely ANALYZED, so this is the only thing standing
    # between exhaustive analysis and the ADR-0005 memory limit.
    ceiling_mb = _DESIGNED_RETENTION_MB * _MAX_PEAK_RATIO
    assert long_peak < ceiling_mb, (
        f"a 2h file peaked at {long_peak:.1f}MB, past {ceiling_mb:.0f}MB "
        f"({_MAX_PEAK_RATIO}x the {_DESIGNED_RETENTION_MB:.0f}MB of designed chunk-bounded retention); "
        f"decoded buffers must be bounded by the chunk, not by the window count"
    )
    # The short file fits inside one partial chunk, so it must hold -- and peak at -- strictly less.
    assert short_peak < long_peak, f"a 4min file ({short_peak:.1f}MB) must not peak at or above a 2h file ({long_peak:.1f}MB)"

    # The sharp one: 6x the duration AND 6x the analyzed windows, identical chunk size, so
    # identical retention and identical peak. Anything that scales with duration rather than
    # with the chunk shows up here, and cannot hide behind the ceiling above.
    chunked_delta_mb = abs(longer_peak - long_peak)
    assert chunked_delta_mb < _MAX_CHUNKED_DELTA_MB, (
        f"peak RSS moved {chunked_delta_mb:.1f}MB between a 2h file ({long_peak:.1f}MB) and a 12h file "
        f"({longer_peak:.1f}MB) at identical chunk sizes; per-file memory must be bounded by the chunk, "
        f"not by duration (threshold {_MAX_CHUNKED_DELTA_MB}MB)"
    )


@pytest.mark.integration
def test_real_decode_short_no_overflow(tmp_path: Path) -> None:
    """Real EasyLoader + RhythmExtractor2013 + KeyExtractor on real 30s buffers: no overflow."""
    path = str(tmp_path / "real.wav")
    _write_sine_wav(path, 90)  # 3 fine (30s) + 1 coarse window; real decode

    # Real decode + real rhythm/key; only the TF model pass is mocked (no .pb in CI).
    with (
        patch.object(analysis_mod, "_predict_single", new=_mock_predict_single),
        patch.object(analysis_mod, "_get_labels", new=_mock_get_labels),
    ):
        result = analyze_file(path, "/fake/models")  # must NOT raise OnsetDetectionGlobal overflow

    fine = [w for w in result["windows"] if w["tier"] == "fine"]
    coarse = [w for w in result["windows"] if w["tier"] == "coarse"]
    assert [(w["start_sec"], w["end_sec"]) for w in fine] == [(0.0, 30.0), (30.0, 60.0), (60.0, 90.0)]
    assert len(coarse) == 1
    # A real BPM aggregate was produced from the real fine-window decode.
    assert result["bpm"] is not None
