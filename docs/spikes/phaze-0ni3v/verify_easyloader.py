"""Dump EasyLoader / EqloudLoader output over a fixed matrix, so a PRE-patch build and a
POST-patch build can be compared byte for byte.

This is the phaze-han03 section 4 methodology applied to the composites: the reference is not a
model of what the old code did, it IS the old code, run in its own venv on the same inputs. Run
once under each venv with a different output path, then diff with compare.py.

The matrix mixes rows where the output rate EQUALS the file's rate -- where Resample is a
fastcopy and bit-identity is therefore meaningful -- with rows where it does not, which are the
rows the patch deliberately leaves on the decode-and-trim path.

All audio is synthetic (make-corpus.sh). Nothing here touches private material.

Usage: verify_easyloader.py <audio-dir> <output.npz>
"""

import sys
from typing import Any

import numpy as np


# (tag, filename, output sampleRate)
FILES: list[tuple[str, str, int]] = [
    ("wav", "synth-600s.wav", 44100),
    ("mp3", "synth-600s.mp3", 44100),
    ("flac", "synth-600s.flac", 44100),
    ("opus", "synth-600s.opus", 48000),
    ("m4a", "synth-600s.m4a", 44100),
    ("m4a-nopns", "synth-600s-nopns.m4a", 44100),
    # ratio != 1.0: libsamplerate runs, so these must stay on the old path
    ("mp3@16k", "synth-600s.mp3", 16000),
    ("opus@44k1", "synth-600s.opus", 44100),
]

SPANS: list[tuple[float, float]] = [(1.0, 6.0), (37.5, 42.5), (123.456, 128.456), (300.0, 305.0)]

# (tag, filename, output sampleRate, duration in seconds)
BOUNDARY_FILES: list[tuple[str, str, int, float]] = [
    ("wav", "synth-600s.wav", 44100, 600.0),
    ("mp3", "synth-600s.mp3", 44100, 600.0),
    ("short", "synth-3s.mp3", 44100, 3.0),
]


def load(algo: Any, **kwargs: Any) -> np.ndarray:
    """Return the algorithm's output, or a one-element string array naming the exception.

    Recording the exception rather than raising is the point: whether a boundary case raises or
    returns nothing is exactly the behaviour being compared between the two builds.
    """
    try:
        return np.asarray(algo(**kwargs)(), dtype=np.float32)
    except Exception as exc:
        return np.array([f"{type(exc).__name__}: {exc}"])


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    from essentia.standard import EasyLoader, EqloudLoader  # noqa: PLC0415

    audio_dir, out = sys.argv[1], sys.argv[2]
    res: dict[str, np.ndarray] = {}

    for tag, name, rate in FILES:
        for start, end in SPANS:
            res[f"slice/{tag}/{start}-{end}"] = load(EasyLoader, filename=f"{audio_dir}/{name}", sampleRate=rate, startTime=start, endTime=end)

    for tag, name, rate, duration in BOUNDARY_FILES:
        cases: dict[str, dict[str, float]] = {
            "start0": {"startTime": 0.0, "endTime": 5.0},
            "endPastEOF": {"startTime": max(0.0, duration - 2.0), "endTime": duration + 100.0},
            "startPastEOF": {"startTime": duration + 10.0, "endTime": duration + 20.0},
            "zeroLength": {"startTime": 1.0, "endTime": 1.0},
            "zeroLength0": {"startTime": 0.0, "endTime": 0.0},
            "inverted": {"startTime": 10.0, "endTime": 1.0},
            "wholeFile": {"startTime": 0.0, "endTime": 1e6},
            "endExactEOF": {"startTime": 0.0, "endTime": duration},
        }
        for case, kwargs in cases.items():
            res[f"bound/{tag}/{case}"] = load(EasyLoader, filename=f"{audio_dir}/{name}", sampleRate=rate, **kwargs)

    # EqloudLoader carries the same two parameters and the same composite shape
    for start, end in [(1.0, 6.0), (123.456, 128.456)]:
        res[f"eqloud/wav/{start}-{end}"] = load(EqloudLoader, filename=f"{audio_dir}/synth-600s.wav", sampleRate=44100, startTime=start, endTime=end)

    # replayGain is applied after the slice, so it must not move either
    res["gain/wav/-12dB"] = load(EasyLoader, filename=f"{audio_dir}/synth-600s.wav", sampleRate=44100, startTime=10.0, endTime=12.0, replayGain=-12.0)

    # numpy's stub types the second positional as `compress`, so keyword-splat only
    np.savez_compressed(out, **res)  # type: ignore[arg-type]
    print(f"wrote {out}: {len(res)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
