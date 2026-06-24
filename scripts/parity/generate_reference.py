"""Deterministic parity reference-clip generator (Phase 47 / CLOUDIMG-03).

Writes ``scripts/parity/reference.wav`` — the fixed audio the arm64↔x86 parity
check runs through ``analyze_file`` on both sides. The clip is built ARITHMETICALLY
(no RNG, no seed) so regenerating it yields a byte-identical file (matching
sha256), and it is fully synthetic so it is license-clean (no copyrighted audio,
no PII — T-47-05).

Construction (NOT a degenerate single sine — RESEARCH Open Q2):

  * Harmonic content: a C-major triad (C4 / E4 / G4) with two added harmonics per
    note, fixed decreasing weights. Multiple partials give the key/mood/genre
    models real spectral structure to score, and let ``KeyExtractor`` resolve a
    key — unlike a lone 440 Hz tone.
  * Rhythmic pulse: a per-beat exponential-decay amplitude envelope retriggered at
    a fixed 120 BPM, producing clear onsets for ``RhythmExtractor2013``.

Sample rate is 8 kHz mono 16-bit so the committed clip stays under the
``check-added-large-files`` 500 KB pre-commit limit at a 30 s duration. All
partials (max 1176 Hz) sit well below the 4 kHz Nyquist, so there is no aliasing;
``analyze_file`` resamples internally to its own 44.1 kHz / 16 kHz passes, so the
source rate is parity-neutral (identical on both architectures).

For arm64-vs-x86 PARITY the clip only needs to be identical on both sides and
drive finite, non-NaN model outputs — musical realism is not the goal.

CLI:
    generate_reference.py [--out PATH]
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import wave

import numpy as np
import numpy.typing as npt


SAMPLE_RATE = 8000  # Hz — keeps a 30 s 16-bit mono clip under the 500 KB large-file limit.
DURATION_SEC = 30
BPM = 120.0
# C-major triad fundamentals (C4, E4, G4); two harmonics each stay < 4 kHz Nyquist.
CHORD_HZ = (261.63, 329.63, 392.00)
HARMONICS = (1, 2, 3)
PEAK = 0.9  # target normalized peak before int16 quantization

_DEFAULT_OUT = Path(__file__).resolve().parent / "reference.wav"


def _build_signal() -> npt.NDArray[np.float64]:
    """Build the deterministic float64 mono signal (harmonic triad x rhythmic envelope)."""
    n = SAMPLE_RATE * DURATION_SEC
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE

    tone = np.zeros(n, dtype=np.float64)
    for note in CHORD_HZ:
        for harmonic in HARMONICS:
            tone += (0.6 ** (harmonic - 1)) * np.sin(2.0 * np.pi * note * harmonic * t)

    beat_period = 60.0 / BPM
    phase = (t % beat_period) / beat_period  # 0..1 sawtooth, resets each beat
    envelope = np.exp(-6.0 * phase)  # sharp attack at each beat, exponential decay → clear onsets

    signal = envelope * tone
    peak = float(np.max(np.abs(signal)))
    return signal / peak * PEAK


def _to_int16(signal: npt.NDArray[np.float64]) -> npt.NDArray[np.int16]:
    """Quantize a normalized float64 signal to little-endian 16-bit PCM."""
    quantized = np.clip(np.round(signal * 32767.0), -32768.0, 32767.0)
    return quantized.astype("<i2")


def generate(out: Path = _DEFAULT_OUT) -> Path:
    """Write the deterministic reference clip to ``out`` and return the path."""
    samples = _to_int16(_build_signal())
    with wave.open(str(out), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(samples.tobytes())
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: (re)generate the reference clip and print its sha256."""
    parser = argparse.ArgumentParser(description="Regenerate the deterministic parity reference clip.")
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help=f"output WAV path (default: {_DEFAULT_OUT})")
    args = parser.parse_args(argv)

    out = generate(Path(args.out))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"wrote {out} ({out.stat().st_size} bytes, {DURATION_SEC}s @ {SAMPLE_RATE} Hz mono 16-bit)")
    print(f"sha256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
