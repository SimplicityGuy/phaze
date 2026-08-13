"""Decode-cost benchmark for the phaze-han03 essentia seek prototype.

Measures ONE quantity, the one that governs phaze's analyze wall clock: how long it takes to
produce the PCM for one CHUNK of a tier's windows, in the two arms

    baseline  -- what ``services/analysis.py::_decode_windows_streaming`` does today: a single
                 ``MonoLoader`` decoded from byte 0, with a ``Trimmer`` hung straight off the
                 loader as an early-stop gate at the chunk's upper boundary, fanned out to one
                 ``Trimmer`` per window. This is a faithful reproduction of the shipped network,
                 not an approximation of it.
    seek      -- the same fan-out, but the loader is given ``startTime``/``endTime`` covering
                 exactly the chunk, so the decoder seeks to the chunk head and stops at its tail.

Methodology follows phaze-b2qs9 section 1: ONE FRESH PROCESS PER MEASUREMENT (this module is
invoked as a subprocess per data point by ``run_bench.sh``), so no run inherits another's
allocator state or page cache warmth, and a monotonic clock around the decode only.

Usage:  bench_seek.py <file> <arm:baseline|seek> <tier_rate> <window_sec> <chunk_index> <chunk_windows>
Prints one JSON object on stdout.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time

import essentia
import essentia.standard as es
import essentia.streaming as ess
import numpy


CHUNK_GATE_MARGIN_SEC = 1.0  # mirrors services/analysis.py::_CHUNK_GATE_MARGIN_SEC


def windows_for_chunk(chunk_index: int, chunk_windows: int, window_sec: float, duration: float) -> list[tuple[int, float, float]]:
    """The natural windows of one chunk, in absolute file time -- phaze's own geometry."""
    out: list[tuple[int, float, float]] = []
    first = chunk_index * chunk_windows
    for i in range(first, first + chunk_windows):
        start = i * window_sec
        end = start + window_sec
        if start >= duration:
            break
        out.append((i, start, min(end, duration)))
    return out


def main() -> int:
    path, arm, rate_s, win_s, chunk_s, cw_s = sys.argv[1:7]
    rate = int(rate_s)
    window_sec = float(win_s)
    chunk_index = int(chunk_s)
    chunk_windows = int(cw_s)

    # index 8 is duration in seconds -- same probe services/analysis.py::_probe_duration_sec uses
    duration = float(es.MetadataReader(filename=path, filterMetadata=True)()[8])
    windows = windows_for_chunk(chunk_index, chunk_windows, window_sec, duration)
    if not windows:
        print(json.dumps({"error": "no windows in chunk"}))
        return 1

    chunk_start = windows[0][1]
    chunk_end = windows[-1][2]
    is_final = chunk_end >= duration - 1e-6

    pool = essentia.Pool()
    branches = []
    gate = None

    t0 = time.monotonic()
    loader = ess.MonoLoader(filename=path, sampleRate=rate)
    if arm == "seek":
        # The whole point: hand the loader the chunk's own span. Non-final chunks also get an
        # endTime, so the tail is bounded by the parameter rather than by a Trimmer.
        loader = ess.MonoLoader(
            filename=path,
            sampleRate=rate,
            startTime=chunk_start,
            endTime=(chunk_end + CHUNK_GATE_MARGIN_SEC) if not is_final else 1.0e6,
        )
        source = loader.audio
    elif arm == "baseline":
        if is_final:
            source = loader.audio
        else:
            # No Scale interposer: the Trimmer's parent MUST be the loader for the early stop to
            # propagate. Same construction, and same reason, as the shipped chunk gate.
            gate = ess.Trimmer(sampleRate=rate, startTime=0.0, endTime=chunk_end + CHUNK_GATE_MARGIN_SEC)
            loader.audio >> gate.signal
            source = gate.signal
    else:
        raise SystemExit(f"unknown arm {arm!r}")

    # A seeked loader emits a CHUNK-RELATIVE stream: its first sample is startTime, not 0. The
    # per-window Trimmers downstream address the stream, not the file, so their bounds have to be
    # rebased. Getting this wrong is silent -- every Trimmer simply matches nothing and the chunk
    # comes back empty -- which is why n_decoded_full is asserted rather than assumed.
    origin = chunk_start if arm == "seek" else 0.0

    for idx, start, end in windows:
        scale = ess.Scale(factor=1.0)
        trimmer = ess.Trimmer(sampleRate=rate, startTime=start - origin, endTime=end - origin)
        source >> scale.signal
        scale.signal >> trimmer.signal
        trimmer.signal >> (pool, f"w{idx}")
        branches.append((scale, trimmer))

    essentia.run(loader)
    elapsed = time.monotonic() - t0

    decoded = {}
    # Not a security context: this digest exists only to compare two decodes of the same audio.
    digest = hashlib.md5(usedforsecurity=False)
    names = set(pool.descriptorNames())
    for idx, _s, _e in windows:
        key = f"w{idx}"
        if key in names:
            buf = numpy.asarray(pool[key], dtype=numpy.float32)
            decoded[key] = len(buf)
            # Digest the PCM itself, so the two arms can be proven to have produced the same
            # audio and not merely to have taken different amounts of time.
            digest.update(buf.tobytes())
        else:
            decoded[key] = 0

    print(
        json.dumps(
            {
                "file": path.rsplit("/", 1)[-1],
                "arm": arm,
                "rate": rate,
                "window_sec": window_sec,
                "chunk_index": chunk_index,
                "chunk_start": chunk_start,
                "chunk_end": chunk_end,
                "is_final": is_final,
                "duration": duration,
                "n_windows": len(windows),
                "n_decoded_full": sum(1 for v in decoded.values() if v >= int(window_sec * rate) - 2),
                "min_samples": min(decoded.values()),
                "pcm_md5": digest.hexdigest(),
                "elapsed_sec": round(elapsed, 4),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
