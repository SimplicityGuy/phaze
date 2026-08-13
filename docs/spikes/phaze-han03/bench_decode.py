"""Raw decode-cost microbenchmark for the phaze-han03 essentia seek prototype.

This is the headline measurement of the spike, and it is deliberately the SIMPLEST possible one:
no window fan-out, no Pool, nothing but "how long does it take to get this span of PCM out of
this file". It answers the one structural question -- is decode cost proportional to the SPAN
ASKED FOR, or to the WHOLE FILE?

Three cases, chosen because they are the three that occur in phaze's pipeline:

    44.1 kHz source -> 44100   the fine tier on a 44.1 kHz file. Resample's ratio-1.0 branch
                               (resample.cpp, streaming, ``if (_data.src_ratio == 1.0)``) is a
                               fastcopy, so decode is cheap and this is the LEAST favourable
                               case for the patch.
    48 kHz source   -> 44100   the fine tier on a 48 kHz file: ratio 0.91875, libsamplerate runs
                               over every sample. phaze-b2qs9 section 4d found source sample rate
                               to be a first-order term in decode cost.
    44.1 kHz source -> 16000   the coarse tier, which always resamples.

Run against a build WITH the seek patch applied. ``EasyLoader`` is the unpatched-equivalent
control in every row: it exposes the same startTime/endTime but has no seek, so it decodes from
byte 0 regardless, which is what phaze's per-window fallback rung pays today.

Usage: bench_decode.py <audio-dir>
"""

from __future__ import annotations

import sys
import time
from typing import Any

import essentia.standard as es


# (label, file, tier sample rate, tier window length)
CASES = [
    ("44.1k src -> 44100 (ratio 1.0, fastcopy)", "synth-3600s.mp3", 44100, 30.0),
    ("48k src   -> 44100 (ratio 0.91875, SRC) ", "synth-3600s-48k.mp3", 44100, 30.0),
    ("44.1k src -> 16000 (coarse tier, SRC)   ", "synth-3600s.mp3", 16000, 180.0),
]

SEEK_AT = 1800.0  # halfway into the 3600 s files, so "seek" and "full" differ by exactly 2x


def timed(algo: Any) -> float:
    """Wall-clock seconds for one decode. Monotonic clock, construction included."""
    t0 = time.monotonic()
    algo()
    return time.monotonic() - t0


def main() -> int:
    audio = sys.argv[1] if len(sys.argv) > 1 else "audio"

    hdr = f"{'case':42s} {'full [0,3600)':>14} {'seek [1800,3600)':>17} {'seek 1 window':>14} {'EasyLoader 1 win':>17} {'speedup':>9}"
    print(hdr)
    print("-" * len(hdr))

    for label, name, rate, win in CASES:
        path = f"{audio}/{name}"
        full = timed(es.MonoLoader(filename=path, sampleRate=rate))
        half = timed(es.MonoLoader(filename=path, sampleRate=rate, startTime=SEEK_AT))
        one = timed(es.MonoLoader(filename=path, sampleRate=rate, startTime=SEEK_AT, endTime=SEEK_AT + win))
        easy = timed(es.EasyLoader(filename=path, sampleRate=rate, startTime=SEEK_AT, endTime=SEEK_AT + win))
        print(f"{label:42s} {full:13.3f}s {half:16.3f}s {one:13.3f}s {easy:16.3f}s {easy / one:8.1f}x")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
