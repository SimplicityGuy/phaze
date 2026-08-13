"""Sample-accuracy and analysis-equivalence verification for the phaze-han03 seek prototype.

A performance win that changes the audio is not a win. This script is the evidence for the
correctness half of the spike, and it is written to be re-runnable against any future build.

Three checks, in increasing order of what they tolerate:

  1. AudioLoader at the NATIVE rate -- the seek itself, with no resampling anywhere near it.
     This is the one that must be BIT-EXACT, and for wav/mp3/flac it is.
  2. MonoLoader at the two phaze tier rates. At 44100 on a 44.1 kHz source the ratio is 1.0 and
     Resample degenerates to a fastcopy, so this stays bit-exact. At 16000 it does not, and the
     residual measured here is libsamplerate's, not the seek's.
  3. Analysis equivalence -- BPM, key, loudness, spectral centroid -- because that is the level
     at which "does this change the product's output" is actually decided.

Reference in every case is the DECODE-AND-DISCARD path: decode the whole file, slice the window
out of it in numpy. That is exactly what the unpatched code computes, so agreement with it is the
definition of correctness here.

Usage: verify_accuracy.py <audio-dir>
"""

from __future__ import annotations

import sys
from typing import Any

import essentia.standard as es
import numpy as np


OFFSETS = (1.0, 37.5, 123.456, 300.0)
SLICE_SEC = 5.0


def main() -> int:
    audio = sys.argv[1] if len(sys.argv) > 1 else "audio"

    print("=" * 100)
    print("1. AudioLoader, native rate -- the seek in isolation. Expectation: BIT-EXACT.")
    print("=" * 100)
    print(f"{'container':10s} {'codec':10s} {'rate':>7s}  {'exact at each offset':40s} {'max|diff|':>10s}")
    for ext in ("wav", "mp3", "m4a", "flac", "opus"):
        path = f"{audio}/synth-600s.{ext}"
        full, sr, _ch, _md5, _br, codec = es.AudioLoader(filename=path)()
        full = np.array(full)
        flags, worst = [], 0.0
        for t in OFFSETS:
            # AudioLoader returns (audio, sampleRate, channels, md5, bit_rate, codec)
            seg = np.array(es.AudioLoader(filename=path, startTime=t, endTime=t + SLICE_SEC)()[0])
            i0 = round(t * sr)
            ref = full[i0 : i0 + len(seg)]
            flags.append(bool(np.array_equal(seg, ref)))
            worst = max(worst, float(np.max(np.abs(seg - ref))))
        print(f"{ext:10s} {codec:10s} {sr:7.0f}  {flags!s:40s} {worst:10.2e}")

    print()
    print("=" * 100)
    print("2. MonoLoader at phaze's tier rates. 44100 on a 44.1 kHz source has ratio 1.0")
    print("   (Resample fastcopy) and stays exact; 16000 resamples and does not.")
    print("=" * 100)
    print(f"{'container':10s} {'tier rate':>10s} {'window':>8s}  {'exact':>6s} {'max|diff|':>10s} {'rms|diff|':>10s} {'rel dB':>8s}")
    for ext in ("mp3", "m4a", "opus"):
        path = f"{audio}/synth-600s.{ext}"
        for rate, win in ((44100, 30.0), (16000, 180.0)):
            full = np.array(es.MonoLoader(filename=path, sampleRate=rate)())
            t = 60.0
            i0 = round(t * rate)
            seg = np.array(es.MonoLoader(filename=path, sampleRate=rate, startTime=t, endTime=t + win)())
            ref = full[i0 : i0 + len(seg)]
            n = min(len(seg), len(ref))
            d = np.abs(seg[:n] - ref[:n])
            rms = float(np.sqrt((d.astype(np.float64) ** 2).mean()))
            rms_ref = float(np.sqrt((ref[:n].astype(np.float64) ** 2).mean()))
            rel = 20 * np.log10(rms / rms_ref) if rms > 0 else float("-inf")
            print(f"{ext:10s} {rate:10d} {win:7.0f}s  {bool(np.array_equal(seg[:n], ref[:n]))!s:>6s} {float(d.max()):10.2e} {rms:10.2e} {rel:8.1f}")

    print()
    print("=" * 100)
    print("3. Analysis equivalence -- does any of the above move a descriptor?")
    print("=" * 100)

    def descriptors(x: Any, rate: int) -> dict[str, Any]:
        bpm, _beats, conf, _est, _int = es.RhythmExtractor2013(method="multifeature")(x)
        key, scale, strength = es.KeyExtractor(sampleRate=rate)(x)
        spec = es.Spectrum()(es.Windowing(type="hann")(x[: 2**16]))
        return {
            "bpm": bpm,
            "bpm_conf": conf,
            "key": f"{key} {scale}",
            "key_strength": strength,
            "loudness": es.Loudness()(x),
            "centroid": es.Centroid(range=rate / 2)(spec),
        }

    for ext in ("mp3", "m4a", "opus"):
        path = f"{audio}/synth-600s.{ext}"
        for rate, win in ((44100, 30.0), (16000, 180.0)):
            full = np.array(es.MonoLoader(filename=path, sampleRate=rate)())
            t = 60.0
            i0 = round(t * rate)
            seg = np.array(es.MonoLoader(filename=path, sampleRate=rate, startTime=t, endTime=t + win)())
            ref = full[i0 : i0 + len(seg)][: len(seg)]
            a, b = descriptors(ref, rate), descriptors(seg[: len(ref)], rate)
            parts = []
            for k in a:
                if isinstance(a[k], str):
                    parts.append(f"{k}:{'SAME' if a[k] == b[k] else a[k] + '!=' + b[k]}")
                else:
                    diff = abs(a[k] - b[k])
                    parts.append(f"{k}:{'EXACT' if diff == 0 else f'{diff / abs(a[k]):.1e}rel'}")
            print(f"{ext:6s} rate={rate:5d}: " + "  ".join(parts))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
