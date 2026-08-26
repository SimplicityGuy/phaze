#!/usr/bin/env python
"""Measure what phaze's OpenTelemetry instrumentation costs a real analysis.

phaze-m1drf.1 acceptance 6: *"Instrumentation overhead measured on a real multi-hour file
and stated as a percentage of wall clock. Peak RSS must not grow with duration."*

**This script measures; it asserts nothing and imports no test framework.** It runs the
REAL ``analyze_file`` -- real essentia, real audio, the real D-07 chunk loop, and (when
``--models-dir`` points at the real weights) the real 34-graph model sweep -- once per arm
per duration, and prints wall clock and peak RSS for each.

Three arms, because "overhead" is three different questions:

* ``off``     -- no OTLP endpoint. The production default, and the baseline.
* ``blackhole`` -- an endpoint on RFC 5737 TEST-NET-1 (``192.0.2.0/24``, reserved for
  documentation, not routed). The SDK is fully installed and every export attempt hangs
  and times out. This is the WORST case: all the instrumentation cost, none of the export
  succeeding, plus the exporter's own retry work.
* ``local``   -- an endpoint given with ``--endpoint``, e.g. the collector from
  ``docker-compose.telemetry.example.yml``. The realistic case.

**Why the audio is synthetic, and what that does and does not cost the measurement.** Two
summed sines, written as int16 WAV -- the same shape ``test_analysis_streaming_decode.py``
already uses. What is being measured is the RATIO between two arms that decode the SAME
bytes through the SAME code, so the content cancels. What synthetic audio cannot tell you
is the ABSOLUTE analysis cost of real music, and this script does not claim to: the
production ratio of 1.4951x its own duration is phaze-zaf2l's, measured on real files.
Synthetic audio also keeps operator media out of a measurement whose output is committed.

**Peak RSS is a HIGH-WATER MARK**, read the same way ``analysis._peak_rss_gib`` reads it
(``/proc/self/status:VmHWM`` on Linux, ``ru_maxrss`` in BYTES on Darwin -- the units differ
and both were verified rather than assumed, phaze-7qfd). Each arm runs in its own
subprocess so one arm's high-water mark cannot contaminate the next.

Usage:

    uv run python scripts/telemetry_overhead.py --minutes 10 30 --models-dir /path/to/models
    uv run python scripts/telemetry_overhead.py --minutes 120 --arms off blackhole --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess  # nosec B404 - each arm MUST be its own process so one arm's peak-RSS high-water mark cannot contaminate the next
import sys
import tempfile
import time
from typing import Any
import wave


#: RFC 5737 TEST-NET-1. Reserved for documentation and not routed, so a connect HANGS
#: rather than being refused -- the failure that could actually stall something.
BLACK_HOLE = "http://192.0.2.1:4318"

_SOURCE_RATE = 8000
_FINE_WINDOW_SEC = 30
_COARSE_WINDOW_SEC = 180


def emit(text: str = "") -> None:
    """The script's ENTIRE output is what it prints -- it is the evidence a doc cites, not
    library code (same rationale as ``scripts/parity/**`` and ``docs/spikes/**``)."""
    print(text, flush=True)  # noqa: T201


def write_sine_wav(path: str, total_sec: int) -> None:
    """Two summed sines as mono int16 -- content that survives resampling legibly.

    Written in one-minute blocks so a two-hour file does not need a 3.8 GB float64 array
    in memory before it is written; the array itself would dominate the very peak-RSS
    figure this script exists to measure.
    """
    import numpy as np  # noqa: PLC0415  # deferred so --help costs nothing

    # One minute at a time. A two-hour file materialized as one float array is ~3.8 GB,
    # which would dominate the very peak-RSS figure this script exists to measure.
    block_sec = 60
    with wave.open(path, "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SOURCE_RATE)
        for offset in range(0, total_sec, block_sec):
            seconds = min(block_sec, total_sec - offset)
            samples_t = (offset + np.arange(_SOURCE_RATE * seconds) / _SOURCE_RATE).astype(np.float32)
            samples = 0.4 * np.sin(2 * math.pi * 220 * samples_t) + 0.3 * np.sin(2 * math.pi * 331 * samples_t)
            handle.writeframes((samples * 32767).astype("<i2").tobytes())


#: The child arm. Kept as source rather than a module so the whole measurement is one file
#: and so each arm's process starts with nothing of phaze imported.
_ARM = r"""
import json, os, resource, sys, time, platform

audio, models_dir = sys.argv[1], sys.argv[2]
fine_sec, coarse_sec = int(sys.argv[3]), int(sys.argv[4])

from phaze.telemetry import configure_telemetry, shutdown_telemetry
telemetry_on = configure_telemetry("analysis")

from phaze.services.analysis import analyze_file, _peak_rss_gib

started = time.perf_counter()
result = analyze_file(audio, models_dir, fine_window_sec=fine_sec, coarse_window_sec=coarse_sec)
elapsed = time.perf_counter() - started

flush_started = time.perf_counter()
flushed = shutdown_telemetry()
flush_elapsed = time.perf_counter() - flush_started

print("PHAZE_ARM_RESULT " + json.dumps({
    "telemetry_on": telemetry_on,
    "wall_sec": elapsed,
    "flush_sec": flush_elapsed,
    "flush_completed": flushed,
    "peak_rss_gib": _peak_rss_gib(),
    "platform": platform.system(),
    "fine_windows_analyzed": result["fine_windows_analyzed"],
    "fine_windows_total": result["fine_windows_total"],
    "coarse_windows_analyzed": result["coarse_windows_analyzed"],
    "coarse_windows_total": result["coarse_windows_total"],
}))
"""


def run_arm(arm: str, audio: str, models_dir: str, endpoint: str | None) -> dict[str, Any]:
    """Run ONE arm in its own process; return its report."""
    env = dict(os.environ)
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
        env.pop(name, None)
    if arm == "blackhole":
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = BLACK_HOLE
    elif arm == "local":
        if not endpoint:
            msg = "--endpoint is required for the 'local' arm"
            raise SystemExit(msg)
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint

    completed = subprocess.run(  # noqa: S603  # nosec B603 - resolved interpreter, literal script, fixed argv, no shell
        [sys.executable, "-c", _ARM, audio, models_dir, str(_FINE_WINDOW_SEC), str(_COARSE_WINDOW_SEC)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("PHAZE_ARM_RESULT "):
            report: dict[str, Any] = json.loads(line.removeprefix("PHAZE_ARM_RESULT "))
            report["arm"] = arm
            return report
    emit(completed.stdout[-4000:])
    sys.stderr.write(completed.stderr[-4000:])
    msg = f"arm {arm!r} produced no report (exit {completed.returncode})"
    raise SystemExit(msg)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=int, nargs="+", default=[10], help="audio durations to measure, in minutes")
    parser.add_argument("--models-dir", default="", help="the real essentia model set; without it the coarse tier runs its failure path")
    parser.add_argument("--arms", nargs="+", default=["off", "blackhole"], choices=["off", "blackhole", "local"])
    parser.add_argument("--endpoint", default="", help="OTLP endpoint for the 'local' arm")
    parser.add_argument("--json", default="", help="write the full report here")
    parser.add_argument("--keep-audio", action="store_true", help="do not delete the synthesized WAVs")
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="phaze-telemetry-overhead-"))
    models_dir = args.models_dir or str(scratch / "no-models")
    reports: list[dict[str, Any]] = []

    for minutes in args.minutes:
        audio = str(scratch / f"sine-{minutes}min.wav")
        emit(f"# synthesizing {minutes} min of audio -> {audio}")
        write_sine_wav(audio, minutes * 60)
        for arm in args.arms:
            emit(f"# {minutes} min / arm={arm} ...")
            started = time.perf_counter()
            report = run_arm(arm, audio, models_dir, args.endpoint or None)
            report["minutes"] = minutes
            report["arm_total_sec"] = time.perf_counter() - started
            reports.append(report)
            emit(f"#   wall {report['wall_sec']:.2f}s  peak {report['peak_rss_gib']} GiB  flush {report['flush_sec']:.3f}s")
        if not args.keep_audio:
            Path(audio).unlink(missing_ok=True)

    emit()
    emit("| minutes | arm | wall (s) | ratio to duration | peak RSS (GiB) | flush (s) | overhead vs off |")
    emit("| ---: | --- | ---: | ---: | ---: | ---: | ---: |")
    baseline = {report["minutes"]: report["wall_sec"] for report in reports if report["arm"] == "off"}
    for report in reports:
        wall = float(report["wall_sec"])
        minutes = int(report["minutes"])
        base = baseline.get(minutes)
        overhead = f"{(wall / float(base) - 1) * 100:+.2f}%" if base else "-"
        peak = report["peak_rss_gib"]
        peak_text = f"{float(peak):.4f}" if peak is not None else "n/a"
        emit(
            f"| {minutes} | {report['arm']} | {wall:.2f} | {wall / (minutes * 60):.4f}x | {peak_text} | {float(report['flush_sec']):.3f} | {overhead} |"
        )

    if args.json:
        Path(args.json).write_text(json.dumps(reports, indent=2), encoding="utf-8")
        emit(f"\n# full report written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
