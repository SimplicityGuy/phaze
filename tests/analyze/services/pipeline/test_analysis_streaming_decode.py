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

import hashlib
import math
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
