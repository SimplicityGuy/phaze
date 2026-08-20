"""phaze-5lop: the streaming fan-out decode — output identity, topology, failure isolation.

``EasyLoader`` does not seek. Standard ``EasyLoader`` wraps the *streaming* ``EasyLoader``
composite, so ``Trimmer``'s "tell my parent to stop" optimisation cannot cross the
``MonoLoader`` composite boundary and every window decoded and resampled the file from byte 0
— making per-file decode ``O(n_windows x total_duration)``, measured at 6.15 hours of
single-threaded libsamplerate for a 12-hour file at production caps (phaze-esut §8,
phaze-rc1q §4a). The decode is now ONE streaming network per tier, fanned out to a ``Trimmer``
per window.

That is a rewrite of the code path every downstream number comes from, so the bar is output
identity, not plausibility: **a sample-alignment error would be invisible in any aggregate and
wrong in every window.** These tests hold that bar on real audio in CI —

* **identity** — every window's raw float32 bytes are compared, by sha256 and by
  ``array_equal``, against what the ``EasyLoader`` call it replaced returns for the same
  ``(startTime, endTime, sampleRate)``. Both tier rates, contiguous and strided window sets,
  a non-zero start, a fractional boundary and a truncated trailing window.
* **topology** — the two upstream traps that silently corrupt this shape are asserted
  directly rather than trusted: the per-branch ``Scale`` really is in the signal path, and an
  early-ending window does not truncate a later one.
* **failure isolation** — the per-window contract phaze-zibn depends on survives a decode
  that is now shared: a network-level failure falls back to the per-window decode, and a
  window the pass produced no audio for is skipped like any other bad window.

The wall-clock and peak-RSS halves are not CI-feasible (they need the real 34-graph model set
on the Linux burst node) and live in the bead's before/after tables.
"""

from __future__ import annotations

import gc
import hashlib
import math
import resource
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
import wave

import numpy as np
import pytest

import phaze.services.analysis as analysis_mod
from phaze.services.analysis import _COARSE_SAMPLE_RATE, _FINE_SAMPLE_RATE, _decode_windows, _decode_windows_streaming


if TYPE_CHECKING:
    from pathlib import Path


_SOURCE_RATE = 8000  # cheap source rate; both tiers resample away from it, exercising Resample
_TOTAL_SEC = 40

# phaze-u1n7j leak-guard sizing — every number here was measured, not chosen for comfort.
#
# The shape matters more than the size: the leak only appears on a GATED decode (the chunk gate
# phaze-w55w1 added), and only once the gate's span is beyond ~100 s. A 40 s span leaks nothing
# even gated, which is why the 40 s `audio` fixture above cannot host this test and
# `long_audio` exists. Measured per-branch retention across shapes: 5.03-5.34 MiB at 44.1 kHz
# and 4.22-5.16 MiB at 16 kHz, i.e. essentially constant per BRANCH and independent of the
# window's LENGTH. 16 kHz and a 120 s span are simply the cheapest combination that still
# reproduces it — 464 MiB of growth in 1.7 s.
_LEAK_WINDOW_SEC = 4.0
_LEAK_WINDOWS = 30  # 30 x 4 s = a 120 s gate span; below ~100 s the leak does not appear at all
_LEAK_SPAN_SEC = _LEAK_WINDOWS * _LEAK_WINDOW_SEC
_LEAK_WARMUP = 1
_LEAK_MEASURED = 3
# The MEASURED retention of one branch. This is the unit the budget is written in, NOT a tuning
# knob: lowering it to quiet a red test is re-accepting the bug that OOMKilled the pod.
_LEAK_BYTES_PER_BRANCH = 5 * 1024 * 1024


def _write_sine_wav(path: str, total_sec: int) -> None:
    """Write a mono int16 WAV of two summed sines — content that survives resampling legibly."""
    t = np.arange(_SOURCE_RATE * total_sec) / _SOURCE_RATE
    samples = 0.4 * np.sin(2 * math.pi * 220 * t) + 0.3 * np.sin(2 * math.pi * 331 * t)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SOURCE_RATE)
        w.writeframes((samples * 32767).astype("<i2").tobytes())


@pytest.fixture
def audio(tmp_path: Path) -> str:
    path = str(tmp_path / "sine.wav")
    _write_sine_wav(path, _TOTAL_SEC)
    return path


@pytest.fixture
def long_audio(tmp_path: Path) -> str:
    """Audio long enough to host a gated chunk decode — the only shape that leaks (phaze-u1n7j).

    Separate from `audio` because the 40 s clip is deliberately cheap and, measured, a gate span
    that short retains nothing at all. ~2 MB of int16 at the fixture's 8 kHz source rate.
    """
    path = str(tmp_path / "sine_long.wav")
    _write_sine_wav(path, int(_LEAK_SPAN_SEC) + 10)
    return path


def _sha(buf: Any) -> str:
    return hashlib.sha256(np.asarray(buf, dtype=np.float32).tobytes()).hexdigest()


# A deliberately awkward window set: window 0 at the origin, a NON-CONTIGUOUS jump (what
# `_stride_to_cap` produces on a long file, and what a truncating fan-out breaks first), a
# fractional boundary, and a trailing window whose endTime runs past the end of the audio.
_WINDOWS: list[tuple[int, float, float]] = [
    (0, 0.0, 5.0),
    (2, 10.0, 15.0),
    (5, 22.5, 27.5),
    (7, 35.0, 45.0),
]


@pytest.mark.integration
@pytest.mark.parametrize("rate", [_FINE_SAMPLE_RATE, _COARSE_SAMPLE_RATE])
def test_streaming_buffers_are_byte_identical_to_easyloader(audio: str, rate: int) -> None:
    """Every fanned-out window is byte-identical to the ``EasyLoader`` call it replaced.

    This is the whole safety argument for the change. The 34 ``TensorflowPredict*`` graphs and
    the two fine-tier extractors are pure functions of the buffer, so identical PCM implies
    identical predictions by construction — and conversely, a half-frame misalignment here
    would move every downstream number while every aggregate still looked reasonable.
    """
    expected = {idx: analysis_mod.es.EasyLoader(filename=audio, sampleRate=rate, startTime=start, endTime=end)() for idx, start, end in _WINDOWS}
    got = _decode_windows_streaming(audio, rate, _WINDOWS)

    assert sorted(got) == sorted(expected), "the fan-out must produce a buffer for every window EasyLoader does"
    for idx, _start, _end in _WINDOWS:
        assert len(got[idx]) == len(expected[idx]), f"window {idx} length {len(got[idx])} != EasyLoader's {len(expected[idx])}"
        assert _sha(got[idx]) == _sha(expected[idx]), f"window {idx} float32 bytes differ from EasyLoader's"
        assert np.array_equal(got[idx], expected[idx]), f"window {idx} samples differ from EasyLoader's"


@pytest.mark.integration
def test_a_late_window_is_not_truncated_by_an_early_one(audio: str) -> None:
    """The §3b hazard, asserted rather than assumed.

    A streaming ``Trimmer`` calls ``_input.source()->parent()->shouldStop(true)`` when it hits
    ``endTime`` — upstream's own optimisation, carrying upstream's own ``FIXME``. Hung straight
    off the shared loader, the window that ends FIRST would shut the shared decode down and
    truncate every window after it. The per-branch ``Scale`` gives each ``Trimmer`` a private
    parent to stop instead. A very short window 0 alongside a late one is exactly the shape
    that fails if that interposer is ever removed.
    """
    windows = [(0, 0.0, 0.5), (1, 30.0, 40.0)]
    got = _decode_windows_streaming(audio, _COARSE_SAMPLE_RATE, windows)

    assert len(got[0]) == pytest.approx(0.5 * _COARSE_SAMPLE_RATE, rel=0.02)
    assert len(got[1]) == pytest.approx(10.0 * _COARSE_SAMPLE_RATE, rel=0.02), "an early-ending window truncated the shared decode"
    expected_late = analysis_mod.es.EasyLoader(filename=audio, sampleRate=_COARSE_SAMPLE_RATE, startTime=30.0, endTime=40.0)()
    assert np.array_equal(got[1], expected_late)


@pytest.mark.integration
def test_the_scale_interposer_is_really_in_the_signal_path(audio: str) -> None:
    """``Scale`` is load-bearing, so prove the graph is wired through it, not around it.

    Identity at ``factor=1.0`` cannot distinguish "``Scale`` is interposed" from "``Scale`` was
    never connected" — both produce the same samples. Building the same network with a
    non-unit factor does distinguish them: if the interposer is in the path the output scales,
    and if it is not, the output is unchanged and the ``Trimmer`` is hanging off the shared
    loader with the truncation hazard above live.
    """
    baseline = _decode_windows_streaming(audio, _COARSE_SAMPLE_RATE, [(0, 0.0, 5.0)])[0]
    scaled = _scaled_decode(audio, _COARSE_SAMPLE_RATE, (0, 0.0, 5.0), factor=0.5)
    assert len(scaled) == len(baseline)
    assert np.allclose(scaled, baseline * 0.5, atol=1e-6), "Scale is not in the signal path; the Trimmer is hanging off the shared loader"


def _scaled_decode(path: str, rate: int, window: tuple[int, float, float], *, factor: float) -> Any:
    """Rebuild :func:`_decode_windows_streaming`'s network with a non-unit ``Scale`` factor."""
    import essentia

    ess = analysis_mod.ess
    _idx, start, end = window
    pool = essentia.Pool()
    loader = ess.MonoLoader(filename=path, sampleRate=rate)
    scale = ess.Scale(factor=factor)
    trimmer = ess.Trimmer(sampleRate=rate, startTime=start, endTime=end)
    loader.audio >> scale.signal
    scale.signal >> trimmer.signal
    trimmer.signal >> (pool, "w")
    essentia.run(loader)
    return pool["w"]


@pytest.mark.integration
def test_analyze_file_decodes_each_tier_in_exactly_one_pass(audio: str) -> None:
    """One streaming pass per tier — two per file — regardless of how many windows there are.

    This is the O(window) claim's structural half: the whole point is that the file is decoded
    and resampled twice per analysis instead of once per window. Asserting the call count is
    what would catch a regression back to a per-window loop, which no output assertion could.
    """
    calls: list[tuple[int, int]] = []
    real = analysis_mod._decode_windows_streaming

    def _counting(path: str, rate: int, windows: Any) -> dict[int, Any]:
        calls.append((rate, len(windows)))
        return real(path, rate, windows)

    with (
        patch.object(analysis_mod, "_decode_windows_streaming", side_effect=_counting),
        patch.object(analysis_mod, "_predict_single", return_value=np.array([0.7, 0.3], dtype=np.float32)),
        patch.object(analysis_mod, "_get_labels", return_value=["positive_class", "negative_class"]),
        patch.object(analysis_mod.es, "EasyLoader", side_effect=AssertionError("the per-window decode must not run")),
    ):
        result = analysis_mod.analyze_file(audio, "/fake/models", fine_window_sec=10, coarse_window_sec=20, fine_min_sec=5)

    assert [rate for rate, _n in calls] == [_FINE_SAMPLE_RATE, _COARSE_SAMPLE_RATE], "expected exactly one pass per tier, fine first"
    assert calls[0][1] == 4, "the fine pass must carry all 4 of the 10s windows"  # 40s / 10s
    assert calls[1][1] == 2, "the coarse pass must carry all 2 of the 20s windows"  # 40s / 20s
    assert result["bpm"] is not None


def test_a_network_failure_falls_back_to_the_per_window_decode() -> None:
    """A tier-level failure must not fail every window — that is what phaze-zibn's floor reads.

    A single shared network cannot isolate one bad window from the rest of the file the way a
    per-window loop can: one raise takes the tier. So the fallback is not defensive padding,
    it is the thing that keeps the failure contract identical to the decode this replaced.
    """
    skips: list[tuple[int, bool]] = []
    windows = [(0, 0.0, 5.0), (1, 5.0, 10.0), (2, 10.0, 15.0)]

    def _easyloader(*, filename: str, sampleRate: int, startTime: float, endTime: float) -> Any:
        if startTime == 5.0:
            msg = "no decodable frames"
            raise RuntimeError(msg)
        loader = MagicMock()
        loader.return_value = np.full(int((endTime - startTime) * sampleRate), 0.25, dtype=np.float32)
        return loader

    mock_es = MagicMock()
    mock_es.EasyLoader.side_effect = _easyloader
    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_decode_windows_streaming", side_effect=RuntimeError("network build failed")),
    ):
        decoded = _decode_windows("/fake/audio.mp3", _COARSE_SAMPLE_RATE, windows, lambda idx, _s, _e, exc: skips.append((idx, exc)))

    assert sorted(decoded) == [0, 2], "the fallback must decode every window the per-window loop could"
    assert skips == [(1, True)], "exactly the undecodable window is skipped, and with a live exception to log"


def test_a_window_the_pass_produced_no_audio_for_is_skipped() -> None:
    """A missing sink is a per-window skip, not a tier-wide failure — and carries no fake traceback.

    ``exc_info`` is False here on purpose: there is no exception in flight, and asking
    ``logging`` to render one anyway prints ``NoneType: None`` under a message claiming a
    failure. The window is still dropped exactly like any other bad window.
    """
    skips: list[tuple[int, bool]] = []
    windows = [(0, 0.0, 5.0), (1, 5.0, 10.0), (2, 10.0, 15.0)]
    partial = {0: np.zeros(4, dtype=np.float32), 2: np.zeros(4, dtype=np.float32)}

    with patch.object(analysis_mod, "_decode_windows_streaming", return_value=partial):
        decoded = _decode_windows("/fake/audio.mp3", _COARSE_SAMPLE_RATE, windows, lambda idx, _s, _e, exc: skips.append((idx, exc)))

    assert sorted(decoded) == [0, 2]
    assert skips == [(1, False)]


def test_an_empty_sink_is_dropped_rather_than_handed_downstream() -> None:
    """A zero-length buffer is not a window — it is a window that produced nothing.

    ``RhythmExtractor2013`` on an empty signal raises, which the per-window handler would
    catch and log as a failure anyway; dropping it in the decode reports the same outcome
    without routing an empty array through two extractors and a 34-graph sweep first.
    """
    real_pool_windows = [(0, 0.0, 5.0)]

    class _EmptyPool:
        def descriptorNames(self) -> list[str]:
            return ["phaze.window.0"]

        def __getitem__(self, _key: str) -> Any:
            return np.zeros(0, dtype=np.float32)

        def remove(self, _key: str) -> None:
            return

    with (
        patch.object(analysis_mod.essentia, "Pool", _EmptyPool),
        patch.object(analysis_mod.essentia, "run", lambda _gen: None),
        patch.object(analysis_mod.ess, "MonoLoader", MagicMock()),
        patch.object(analysis_mod.ess, "Scale", MagicMock()),
        patch.object(analysis_mod.ess, "Trimmer", MagicMock()),
    ):
        assert _decode_windows_streaming("/fake/audio.mp3", _COARSE_SAMPLE_RATE, real_pool_windows) == {}


@pytest.mark.integration
def test_every_pool_key_is_removed_as_its_buffer_is_extracted(audio: str) -> None:
    """The `Pool` must not survive the decode holding a second copy of every window.

    ``pool[key]`` COPIES, so a fan-out that leaves its keys in place holds each window's PCM
    twice — and the second copy stays live through the model sweep, which is where it hurts.
    That is the ``+0.677 GiB`` phaze-rc1q §6b measured surviving a ``malloc_trim`` and the
    reason its prototype breached the 3Gi request. Extraction alone cannot prove the removal
    happened, so the removals are recorded and the ``Pool`` is asserted empty afterwards.
    """
    import essentia

    removed: list[str] = []
    pools: list[Any] = []

    class _RecordingPool(essentia.Pool):  # type: ignore[misc, name-defined]
        def __init__(self) -> None:
            super().__init__()
            pools.append(self)

        def remove(self, key: str) -> Any:
            removed.append(key)
            return super().remove(key)

    with patch.object(analysis_mod.essentia, "Pool", _RecordingPool):
        decoded = _decode_windows_streaming(audio, _COARSE_SAMPLE_RATE, _WINDOWS)

    assert sorted(decoded) == [idx for idx, _s, _e in _WINDOWS]
    assert removed == [f"phaze.window.{idx}" for idx, _s, _e in _WINDOWS], "every extracted key must be removed, in window order"
    assert pools[0].descriptorNames() == [], "the Pool must be empty before the buffers are handed to the caller"
    # And the extracted arrays are real, independent copies -- not views the removal invalidated.
    assert all(len(decoded[idx]) > 0 and float(np.abs(decoded[idx]).max()) > 0 for idx, _s, _e in _WINDOWS)


@pytest.mark.integration
def test_each_tier_trims_after_its_decode(audio: str) -> None:
    """``malloc_trim`` fires per tier, not once at the end and not never.

    glibc does not return a freed block to the kernel on its own, and TensorFlow's arena in the
    model sweep does not reuse the pages the fan-out just released — so without a trim the
    sweep's peak stacks ON TOP of the decode transient instead of sitting under it
    (phaze-rc1q §6b: -0.403 GiB for +0.13% wall). It is one line, which is exactly the kind of
    line a later refactor drops silently.
    """
    with (
        patch.object(analysis_mod, "_malloc_trim") as trim,
        patch.object(analysis_mod, "_predict_single", return_value=np.array([0.7, 0.3], dtype=np.float32)),
        patch.object(analysis_mod, "_get_labels", return_value=["positive_class", "negative_class"]),
    ):
        analysis_mod.analyze_file(audio, "/fake/models", fine_window_sec=10, coarse_window_sec=20, fine_min_sec=5)

    # Two per tier: one when the decode drops its Pool, one when the tier drops its buffers.
    assert trim.call_count >= 4, f"expected at least one trim per tier boundary, got {trim.call_count}"


def test_malloc_trim_never_raises() -> None:
    """The trim is an optimisation, so a platform without it must degrade to a no-op.

    glibc has ``malloc_trim``; macOS and musl do not. phaze runs the analyze job on glibc,
    but this module is imported (and its tests run) on macOS, and a missing symbol must not
    turn a decode into a failed file.
    """
    analysis_mod._malloc_trim()  # must not raise on any platform this suite runs on
    analysis_mod._malloc_trim()  # idempotent

    with patch.object(analysis_mod, "_MALLOC_TRIM", MagicMock(side_effect=OSError("boom"))):
        analysis_mod._malloc_trim()


# ---------------------------------------------------------------------------
# phaze-w55w1: the chunk decode gate (`stop_at_sec`)
#
# Exhaustive analysis decodes a tier one CHUNK at a time, and MonoLoader cannot seek, so each
# chunk pays its own decode. The gate is a Trimmer interposed at the HEAD of the fan-out --
# deliberately without a Scale, so that reaching endTime stops the loader (the same upstream
# behaviour the per-window Scale interposers exist to PREVENT). It is a wall-clock optimisation
# only; these tests pin that it changes nothing else and that a broken gate degrades gracefully.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_chunk_gate_does_not_change_a_single_sample(audio: str) -> None:
    """A gated pass returns byte-identical buffers to an ungated one, against real essentia.

    This is the assertion that makes the gate safe to ship without a measurement: whatever it
    does to the loader's stopping point, the windows it yields must be indistinguishable from
    the ungated network's. The gate's endTime sits `_CHUNK_GATE_MARGIN_SEC` past the chunk's
    last window precisely so the boundary window cannot be clipped, and a clipped buffer is
    exactly what this would catch.
    """
    windows = [(0, 0.0, 5.0), (1, 5.0, 10.0)]

    ungated = _decode_windows_streaming(audio, _COARSE_SAMPLE_RATE, windows)
    gated = _decode_windows_streaming(audio, _COARSE_SAMPLE_RATE, windows, stop_at_sec=10.0)

    assert sorted(gated) == sorted(ungated) == [0, 1]
    for idx in (0, 1):
        assert len(gated[idx]) == len(ungated[idx]), f"window {idx} was clipped by the gate"
        assert _sha(gated[idx]) == _sha(ungated[idx]), f"window {idx} float32 bytes differ under the gate"


@pytest.mark.integration
def test_analyze_file_gates_every_chunk_but_the_last(audio: str) -> None:
    """Across a real multi-chunk run: non-final chunks are gated, the final chunk is not.

    Driven through `analyze_file` with a tiny fine window so the 40 s fixture produces several
    fine chunks, and with the chunk size monkeypatched rather than the audio lengthened -- the
    property is about chunk BOUNDARIES, and a 12-hour fixture is not CI-feasible.
    """
    seen: list[float | None] = []
    real = analysis_mod._decode_windows_streaming

    def _spy(path: str, rate: int, windows: Any, *, stop_at_sec: float | None = None) -> dict[int, Any]:
        if rate == _FINE_SAMPLE_RATE:
            seen.append(stop_at_sec)
        return real(path, rate, windows, stop_at_sec=stop_at_sec)

    with (
        patch.object(analysis_mod, "_FINE_CHUNK_WINDOWS", 2),
        patch.object(analysis_mod, "_decode_windows_streaming", side_effect=_spy),
        patch.object(analysis_mod, "_predict_single", return_value=np.array([0.7, 0.3], dtype=np.float32)),
        patch.object(analysis_mod, "_get_labels", return_value=["positive_class", "negative_class"]),
    ):
        result = analysis_mod.analyze_file(audio, "/fake/models", fine_window_sec=10, coarse_window_sec=20, fine_min_sec=5)

    # 40 s / 10 s = 4 fine windows -> 2 chunks of 2. The first is gated at its own end, the last is not.
    assert seen == [20.0, None]
    # And the run still analyzed EVERY window, gate or no gate.
    assert result["fine_windows_analyzed"] == result["fine_windows_total"] == 4


def test_a_failing_gate_retries_ungated_before_the_per_window_fallback() -> None:
    """A gate that cannot be built costs wall clock, never correctness.

    The retry rung matters more than it looks: without it, a gate broken by some future essentia
    would demote EVERY chunk of EVERY file to the per-window `EasyLoader` decode phaze-5lop
    removed -- `O(n_windows x duration)`, i.e. the 4-hour-timeout shape -- which is far worse
    than simply decoding each chunk from byte 0. So the fallback ladder has three rungs, and
    this test pins that the middle one is real.
    """
    windows = [(0, 0.0, 5.0), (1, 5.0, 10.0)]
    buffers = {0: np.zeros(4, dtype=np.float32), 1: np.zeros(4, dtype=np.float32)}
    calls: list[float | None] = []

    def _gate_only_fails(_path: str, _rate: int, _windows: Any, *, stop_at_sec: float | None = None) -> dict[int, Any]:
        calls.append(stop_at_sec)
        if stop_at_sec is not None:
            msg = "Trimmer refused the head position"
            raise RuntimeError(msg)
        return buffers

    mock_es = MagicMock()
    mock_es.EasyLoader.side_effect = AssertionError("the per-window decode must not run: the ungated retry succeeded")
    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_decode_windows_streaming", side_effect=_gate_only_fails),
    ):
        decoded = _decode_windows("/fake/audio.mp3", _COARSE_SAMPLE_RATE, windows, lambda *_a: None, stop_at_sec=10.0)

    assert decoded == buffers
    assert calls == [10.0, None], "the gated attempt must be followed by exactly one ungated retry"


def test_the_per_window_fallback_beats_once_per_window() -> None:
    """The fallback decode must stay audible to the stall watchdog (phaze-w55w1).

    Each `EasyLoader` call in this loop re-decodes the file from byte 0 -- it cannot seek, which
    is the whole reason phaze-5lop exists -- so on a long file one window takes minutes and a
    chunk takes hours. Unbeaten, that silence is indistinguishable from a hang: the stall watchdog
    would kill the fallback and the file would go terminally ANALYSIS_FAILED, i.e. the degradation
    path would destroy exactly the work it exists to salvage. A beat per window is what prevents
    that, and it must fire for SKIPPED windows too (a skip is still forward motion).
    """
    beats: list[int] = []
    windows = [(i, i * 5.0, (i + 1) * 5.0) for i in range(8)]

    def _easyloader(*, filename: str, sampleRate: int, startTime: float, endTime: float) -> Any:
        if startTime == 15.0:  # one window fails, to prove the beat is outside the try
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
            "/fake/<set-01>",
            _COARSE_SAMPLE_RATE,
            windows,
            lambda *_a: None,
            on_beat=lambda: beats.append(len(beats)),
        )

    assert len(decoded) == 7, "the fallback still decodes every window it can"
    assert len(beats) == len(windows), "one beat per window, including the one that failed"


def test_a_failing_gated_attempt_beats_before_the_ungated_retry() -> None:
    """A gated pass that burns minutes and then fails must report before retrying.

    Both attempts decode the same audio, so a gate failure doubles an already-long silent stretch.
    Beating between them keeps the retry from starting its clock already close to the threshold.
    """
    beats: list[int] = []
    windows = [(0, 0.0, 5.0)]
    buffers = {0: np.zeros(4, dtype=np.float32)}

    def _gate_only_fails(_p: str, _r: int, _w: Any, *, stop_at_sec: float | None = None) -> dict[int, Any]:
        if stop_at_sec is not None:
            msg = "gate refused"
            raise RuntimeError(msg)
        return buffers

    with patch.object(analysis_mod, "_decode_windows_streaming", side_effect=_gate_only_fails):
        _decode_windows(
            "/fake/audio.mp3",
            _COARSE_SAMPLE_RATE,
            windows,
            lambda *_a: None,
            stop_at_sec=5.0,
            on_beat=lambda: beats.append(len(beats)),
        )

    assert beats, "a failed gated attempt must beat before the ungated retry begins"


def test_decode_windows_without_a_beat_callback_is_inert() -> None:
    """`on_beat=None` (the default, and every non-analysis caller) must not raise."""
    windows = [(0, 0.0, 5.0)]
    with patch.object(analysis_mod, "_decode_windows_streaming", return_value={0: np.zeros(4, dtype=np.float32)}):
        assert _decode_windows("/fake/audio.mp3", _COARSE_SAMPLE_RATE, windows, lambda *_a: None) == {0: pytest.approx(np.zeros(4))}


# ---------------------------------------------------------------------------
# phaze-u1n7j — the chunk decode must not leak its network (D-09)
# ---------------------------------------------------------------------------
#
# phaze-b2qs9 measured whole-process peak RSS growing +0.31 GiB per fine chunk on real audio —
# linear in duration, 4.1854 GiB at four hours, past the deployed 4Gi limit — while
# `test_analysis_long_file.py` reported the memory claim GREEN. That test drives a MOCKED
# essentia, so it proved a property of the mock and never touched the C++ allocation that was
# actually growing. These two tests exist so that cannot happen twice, and BOTH are built to be
# unsatisfiable by a mock: they run the real streaming network over real audio and read the
# kernel's own accounting.


def _hwm_bytes() -> int:
    """Whole-process peak RSS, in bytes, from the kernel's high-water mark.

    `ru_maxrss` and not a sampler: it is a high-water mark the kernel maintains, so it cannot
    miss a spike between ticks and it cannot be starved of the GIL (phaze-7i0k §9). Linux
    reports KiB, macOS bytes — the one platform difference that matters here.
    """
    import sys

    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


@pytest.mark.integration
def test_repeated_gated_chunk_decodes_do_not_grow_peak_rss(long_audio: str) -> None:
    """The bug itself: N identical chunk decodes must not cost N times one chunk's memory.

    Nothing about the chunk changes between calls — same file, same windows, same rate, same
    gate — so the process cannot legitimately need more memory on call 3 than on call 1. Before
    the D-09 fix each call retained its whole streaming network, ~5 MiB per window branch, which
    is exactly how a per-chunk constant became a duration-linear curve: 60 fine branches per
    chunk x ~5 MiB ≈ the +0.31 GiB per 30 minutes of audio that phaze-b2qs9 measured on vox.

    **`stop_at_sec` is load-bearing and must not be dropped from this test.** The retention is a
    property of the GATED network specifically; the same decode ungated leaks 0.02 MiB/branch
    against the gated 5.14 (measured, one file, one window set, the gate the only variable). In
    production every non-final chunk of every tier is gated, so the gated path is the one that
    runs ~K-1 times per file — but an ungated version of this test would pass on the broken code.

    The threshold is deliberately loose: it asserts that THREE further decodes together stay
    under a SINGLE decode's leak. That still fails the pre-fix code by ~3x (measured 464 MiB of
    growth against a 150 MiB budget) while leaving room for allocator noise and whatever
    essentia caches on first use.
    """
    windows = [(i, i * _LEAK_WINDOW_SEC, (i + 1) * _LEAK_WINDOW_SEC) for i in range(_LEAK_WINDOWS)]

    for _ in range(_LEAK_WARMUP):  # the first call also pays one-off essentia/allocator init
        _decode_windows_streaming(long_audio, _COARSE_SAMPLE_RATE, windows, stop_at_sec=_LEAK_SPAN_SEC)
    gc.collect()
    settled = _hwm_bytes()

    for _ in range(_LEAK_MEASURED):
        decoded = _decode_windows_streaming(long_audio, _COARSE_SAMPLE_RATE, windows, stop_at_sec=_LEAK_SPAN_SEC)
        assert len(decoded) == _LEAK_WINDOWS, "the decode must keep working — a leak-free decode that drops windows is not a fix"
        decoded.clear()
        gc.collect()

    grew = _hwm_bytes() - settled
    budget = _LEAK_WINDOWS * _LEAK_BYTES_PER_BRANCH  # ONE decode's worth of leak, for three decodes
    assert grew < budget, (
        f"peak RSS grew {grew / 2**20:.1f} MiB across {_LEAK_MEASURED} identical gated chunk decodes "
        f"(budget {budget / 2**20:.1f} MiB). The streaming network is being retained per chunk — see "
        f"D-09 in services/analysis.py. That is duration-linear memory growth, and it OOMKills the pod "
        f"on any file past roughly three hours."
    )


@pytest.mark.integration
def test_the_chunk_decode_leaves_no_connected_network_behind(audio: str) -> None:
    """The mechanism, asserted directly: every edge the decode built is severed before it returns.

    The RSS test above proves the SYMPTOM is gone; this one pins the CAUSE, so a regression
    reports "the teardown stopped disconnecting" instead of "memory went up somewhere". It also
    fails fast and deterministically, with no allocator noise in the loop.

    essentia's Python bindings expose no network object, so a connection can only be released by
    `_StreamConnector.disconnect`. A live edge here means the C++ buffers behind it — and the
    `PoolStorage` a Pool sink creates, which Python never holds at all — stay allocated.
    """
    made: list[Any] = []

    def _recording(real: Any) -> Any:
        def _make(**kwargs: Any) -> Any:
            algo = real(**kwargs)
            made.append(algo)
            return algo

        return _make

    with (
        patch.object(analysis_mod.ess, "MonoLoader", _recording(analysis_mod.ess.MonoLoader)),
        patch.object(analysis_mod.ess, "Scale", _recording(analysis_mod.ess.Scale)),
        patch.object(analysis_mod.ess, "Trimmer", _recording(analysis_mod.ess.Trimmer)),
    ):
        _decode_windows_streaming(audio, _COARSE_SAMPLE_RATE, _WINDOWS, stop_at_sec=20.0)

    assert made, "the recording patch never saw an algorithm — this test is not exercising the real network"
    live = [(algo.name(), connector.name, len(targets)) for algo in made for connector, targets in algo.connections.items() if targets]
    assert not live, f"the chunk decode returned with {len(live)} connection(s) still live: {live}. See D-09 in services/analysis.py"


@pytest.mark.integration
def test_a_constructor_raising_mid_fan_out_still_disconnects_the_earlier_branches(audio: str) -> None:
    """A build that dies part-way through the window loop must still tear down what it built.

    This is the D-09 leak from the other side. The RSS and topology guards above both run a
    decode that COMPLETES; nothing there notices if the teardown can only see a network the
    build finished handing over. But window ``k``'s constructor -- or either of its ``>>``
    calls -- can raise under exactly the memory pressure this module exists to survive, with
    windows ``0..k-1`` already wired. If those algorithms are not reachable from the
    ``finally``, dropping their Python proxies leaves essentia's C++ edges and the implicit
    ``PoolStorage`` sink allocated, and the gated retry rung above it then runs the decode
    again on top of the leak. That is duration-linear peak RSS, i.e. the pod OOMKill.

    So the failure is injected into the REAL network: real ``MonoLoader``, real gate, real
    ``Scale``/``Trimmer`` branches for the windows before the raise, and the assertion reads
    live connections off the essentia objects themselves. Only the third ``Scale``
    construction is replaced, and only to make it raise.
    """
    made: list[Any] = []
    real_scale = analysis_mod.ess.Scale
    fail_on = 3  # windows 0 and 1 are fully built and wired before this raises

    def _recording(real: Any) -> Any:
        def _make(**kwargs: Any) -> Any:
            algo = real(**kwargs)
            made.append(algo)
            return algo

        return _make

    scales_built = 0

    def _scale_that_dies_on_the_third_window(**kwargs: Any) -> Any:
        nonlocal scales_built
        scales_built += 1
        if scales_built == fail_on:
            msg = "essentia could not allocate another branch"
            raise RuntimeError(msg)
        algo = real_scale(**kwargs)
        made.append(algo)
        return algo

    with (
        patch.object(analysis_mod.ess, "MonoLoader", _recording(analysis_mod.ess.MonoLoader)),
        patch.object(analysis_mod.ess, "Trimmer", _recording(analysis_mod.ess.Trimmer)),
        patch.object(analysis_mod.ess, "Scale", _scale_that_dies_on_the_third_window),
        pytest.raises(RuntimeError, match="could not allocate another branch"),
    ):
        _decode_windows_streaming(audio, _COARSE_SAMPLE_RATE, _WINDOWS, stop_at_sec=20.0)

    # loader + gate + (Scale, Trimmer) for windows 0 and 1, and the Trimmer of the window that
    # died -- proof the raise landed mid-fan-out rather than before any branch existed.
    assert len(made) >= 6, f"the injected failure fired too early to prove anything: only {len(made)} algorithm(s) were built"
    live = [(algo.name(), connector.name, len(targets)) for algo in made for connector, targets in algo.connections.items() if targets]
    assert not live, (
        f"a mid-fan-out failure left {len(live)} connection(s) live: {live}. Everything built before the raise must be "
        f"reachable from the decode's `finally` -- see D-09 in services/analysis.py and `_PartialNetwork` in analysis_decoder.py"
    )


@pytest.mark.integration
def test_a_gate_that_cannot_be_wired_is_still_handed_to_the_teardown(audio: str) -> None:
    """The same hazard on the head of the fan-out: the gate exists before it is connected.

    ``_stream_source`` constructs the chunk gate and only then wires ``loader.audio`` into it.
    A raise on that wiring call used to leave the caller holding no gate at all, so the gate --
    a real streaming ``Trimmer`` -- leaked with the loader still tied to it.

    The gate here is a real ``ess.Trimmer``; only the loader's ``audio`` source is replaced, and
    only so that the wiring call is the thing that raises. essentia offers no other way to fail
    exactly there.
    """

    class _LoaderWithAnUnreadableAudioSource:
        """A stand-in MonoLoader whose ``audio`` connector cannot be read."""

        def __init__(self, **_kwargs: Any) -> None:
            pass

        @property
        def audio(self) -> Any:
            msg = "essentia refused to expose the loader's audio source"
            raise RuntimeError(msg)

    torn_down: list[Any] = []
    real_disconnect = analysis_mod._disconnect_network
    gates: list[Any] = []
    real_trimmer = analysis_mod.ess.Trimmer

    def _recording_trimmer(**kwargs: Any) -> Any:
        algo = real_trimmer(**kwargs)
        gates.append(algo)
        return algo

    def _recording_disconnect(algos: Any) -> None:
        torn_down.extend(algos)
        real_disconnect(algos)

    with (
        patch.object(analysis_mod.ess, "MonoLoader", _LoaderWithAnUnreadableAudioSource),
        patch.object(analysis_mod.ess, "Trimmer", _recording_trimmer),
        patch.object(analysis_mod, "_disconnect_network", _recording_disconnect),
        pytest.raises(RuntimeError, match="refused to expose"),
    ):
        _decode_windows_streaming(audio, _COARSE_SAMPLE_RATE, _WINDOWS, stop_at_sec=20.0)

    assert len(gates) == 1, f"the gate is the only Trimmer built before the raise; got {len(gates)}"
    assert gates[0] in torn_down, "the chunk gate was constructed but never handed to the teardown — see D-09 and `_PartialNetwork`"


def test_a_stuck_disconnect_never_masks_the_callers_exception() -> None:
    """The teardown runs in a ``finally`` on the failure path, so it must not raise there.

    `_decode_windows` retries a failed gated pass UNGATED, and that retry is exactly when the
    network being torn down is half-built. If a disconnect could raise from the ``finally`` it
    would replace the decode's real exception with its own, and the caller's retry rung — the
    thing standing between a broken gate and a re-run of the O(n_windows x duration) decode
    phaze-5lop deleted — would never be reached. A stuck edge is allowed to leak; it is not
    allowed to change control flow.
    """

    class _StuckConnector:
        name = "signal"

        def disconnect(self, _target: Any) -> None:
            msg = "essentia refused to disconnect"
            raise RuntimeError(msg)

    class _StuckAlgo:
        def __init__(self) -> None:
            self.connections = {_StuckConnector(): ["some-sink"]}

    analysis_mod._disconnect_network([_StuckAlgo()])  # must return normally


def test_disconnecting_a_partially_built_network_is_inert() -> None:
    """A build that raised mid-loop leaves `None`s and connection-less objects; teardown eats them.

    `_decode_windows_streaming` registers each algorithm on its `_PartialNetwork` before wiring
    it, so the `finally` can see algorithms that were constructed but never connected — and,
    from older call shapes and the retry rungs above it, `None`s too. That is the ordinary shape
    of the failure path, not an exotic one; the two tests above drive it end to end.
    """
    analysis_mod._disconnect_network([None, object(), analysis_mod.ess.Scale(factor=1.0)])


def test_an_unreadable_connections_map_is_survived_too() -> None:
    """Reading `connections` must not be the thing that raises out of the ``finally`` either.

    The snapshot is taken inside the guard, not before it, so an object whose `connections` is
    present but not a mapping leaks its network rather than replacing the decode's exception.
    """

    class _WeirdAlgo:
        connections = "not a mapping"  # truthy, has no .items()

    analysis_mod._disconnect_network([_WeirdAlgo()])  # must return normally
