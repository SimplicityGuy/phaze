#!/usr/bin/env python
"""Measure the metric contract against a REAL collector and a REAL analysis.

phaze-m1drf.3 acceptance 3 and 5: *"MEASURED series count and emission rate from a real
analysis run"* and *"histogram bucket boundaries chosen against MEASURED distributions, not
defaults"*. Both are questions about what phaze actually emits, so both are answered by
running it and reading the collector's own Prometheus exposition -- the exact surface
homelab's Prometheus scrapes.

This is also where the Prometheus metric NAMES in the catalogue come from. They are read
off the real OTLP -> Prometheus translation rather than derived from the naming rules on
paper, which is how two defects were found that no schema check could have seen: a label
named ``job`` collides with the reserved Prometheus label and the collector DROPS THE WHOLE
METRIC (logging an error and exposing nothing), and ``unit="1"`` on a gauge renders as a
``_ratio`` suffix.

    docker compose -f docker-compose.telemetry.example.yml up -d
    uv run python scripts/measure_metric_contract.py \
        --minutes 30 --models-dir /path/to/models --out docs/telemetry/measurements/
"""

from __future__ import annotations

import argparse
import collections
import json
import math
from pathlib import Path
import re
import subprocess  # nosec B404 - runs the analysis in its own process so its peak RSS is its own
import sys
import tempfile
import time
import wave

import httpx


_SOURCE_RATE = 8000


def emit(text: str = "") -> None:
    """The script's entire output is what it prints; it is the evidence the doc cites."""
    print(text, flush=True)  # noqa: T201


def write_sine_wav(path: str, total_sec: int) -> None:
    import numpy as np  # noqa: PLC0415

    with wave.open(path, "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SOURCE_RATE)
        for offset in range(0, total_sec, 60):
            seconds = min(60, total_sec - offset)
            t = (offset + np.arange(_SOURCE_RATE * seconds) / _SOURCE_RATE).astype(np.float32)
            samples = 0.4 * np.sin(2 * math.pi * 220 * t) + 0.3 * np.sin(2 * math.pi * 331 * t)
            handle.writeframes((samples * 32767).astype("<i2").tobytes())


_ARM = r"""
import json, sys, time
from phaze.telemetry import configure_telemetry, shutdown_telemetry
on = configure_telemetry("analysis")
from phaze.services.analysis import analyze_file, _peak_rss_gib
started = time.perf_counter()
result = analyze_file(sys.argv[1], sys.argv[2], fine_window_sec=30, coarse_window_sec=180)
elapsed = time.perf_counter() - started
flushed = shutdown_telemetry(10000)
print("PHAZE_RUN " + json.dumps({
    "telemetry_on": on, "wall_sec": elapsed, "flushed": flushed,
    "peak_rss_gib": _peak_rss_gib(),
    "fine_windows_analyzed": result["fine_windows_analyzed"],
    "fine_windows_total": result["fine_windows_total"],
    "coarse_windows_analyzed": result["coarse_windows_analyzed"],
    "coarse_windows_total": result["coarse_windows_total"],
}))
"""


def scrape(endpoint: str) -> str:
    """GET the collector's Prometheus exposition.

    ``httpx`` rather than ``urllib.request`` deliberately: it is what the rest of this repo
    uses, and it speaks only http(s). ``urlopen`` additionally honours ``file:`` and custom
    schemes, which is what ruff S310 / bandit B310 / semgrep's dynamic-urllib rule all flag --
    so switching client REMOVES that class rather than suppressing three warnings about it.
    The scheme check below is kept for the error message, not as a security control.
    """
    if not endpoint.startswith(("http://", "https://")):
        msg = f"--collector-metrics must be an http(s) URL, got {endpoint!r}"
        raise SystemExit(msg)
    response = httpx.get(endpoint, timeout=15)
    response.raise_for_status()
    return response.text


def parse(text: str) -> tuple[dict[str, str], collections.Counter[str], dict[str, list[tuple[float, float]]]]:
    """Return (family -> type, family -> series count, histogram family -> [(le, count)])."""
    types: dict[str, str] = {}
    series: collections.Counter[str] = collections.Counter()
    buckets: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for line in text.splitlines():
        type_match = re.match(r"^# TYPE (phaze_\S+) (\w+)", line)
        if type_match:
            types[type_match.group(1)] = type_match.group(2)
            continue
        sample = re.match(r"^(phaze_\S*?)\{(.*)\}\s+(\S+)$", line)
        if not sample:
            continue
        name, labels, value = sample.group(1), sample.group(2), sample.group(3)
        series[name] += 1
        if name.endswith("_bucket"):
            le = re.search(r'le="([^"]+)"', labels)
            if le:
                family = name.removesuffix("_bucket")
                buckets[family].append((float(le.group(1)), float(value)))
    return types, series, buckets


def bucket_distribution(pairs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Cumulative bucket counts summed across label sets, ascending by boundary."""
    totals: dict[float, float] = collections.defaultdict(float)
    for le, count in pairs:
        totals[le] += count
    return sorted(totals.items())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--endpoint", default="http://localhost:4318", help="the collector's OTLP/HTTP receiver")
    parser.add_argument("--collector-metrics", default="http://localhost:8889/metrics", help="the collector's Prometheus exposition")
    parser.add_argument("--instance", default="measurement-host")
    parser.add_argument("--out", default="", help="directory to write the raw exposition and a JSON summary into")
    parser.add_argument(
        "--settle-sec", type=int, default=20, help="wait for the collector's batch processor before scraping; see the note at the call site"
    )
    parser.add_argument("--scrape-only", action="store_true", help="skip the analysis and report on what the collector already holds")
    args = parser.parse_args()

    before = parse(scrape(args.collector_metrics))[1]
    emit(f"# collector already exposing {sum(before.values())} phaze series before the run")

    if args.scrape_only:
        return _report(scrape(args.collector_metrics), {}, 0, args)

    scratch = Path(tempfile.mkdtemp(prefix="phaze-metric-contract-"))
    audio = str(scratch / f"sine-{args.minutes}min.wav")
    emit(f"# synthesizing {args.minutes} min of audio")
    write_sine_wav(audio, args.minutes * 60)

    import os  # noqa: PLC0415

    env = dict(os.environ)
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = args.endpoint
    env["PHAZE_TELEMETRY_INSTANCE"] = args.instance

    emit("# running the analysis (real essentia, real models, exporting to the collector)")
    started = time.perf_counter()
    completed = subprocess.run(  # noqa: S603  # nosec B603 - resolved interpreter, literal script, fixed argv
        [sys.executable, "-c", _ARM, audio, args.models_dir],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    wall = time.perf_counter() - started
    report: dict[str, object] = {}
    for line in completed.stdout.splitlines():
        if line.startswith("PHAZE_RUN "):
            report = json.loads(line.removeprefix("PHAZE_RUN "))
    if not report:
        emit(completed.stdout[-3000:])
        sys.stderr.write(completed.stderr[-3000:])
        msg = f"the analysis produced no report (exit {completed.returncode})"
        raise SystemExit(msg)
    emit(f"#   wall {report['wall_sec']:.2f}s  peak {report['peak_rss_gib']} GiB  flushed={report['flushed']}")

    # MEASURED, and it cost a whole 13-minute run to learn: the collector's `batch` processor
    # holds a push for up to its own timeout (5 s in the example config) before the Prometheus
    # exporter ever sees it. Scraping the instant the producer exits therefore reports
    # everything EXCEPT the final export -- which for an analysis is the last model sweeps,
    # the derive phase and the whole-run totals, i.e. exactly the tail that matters. The first
    # run of this script scraped immediately and reported 2,058 series and 32 of 34 models;
    # the same collector, read a minute later, held 2,242 series and all 34.
    #
    # This is not a harness quirk -- it is what homelab sees too. A short-lived analyze pod's
    # final push is not at the scrape endpoint until the collector's batcher has flushed it.
    emit(f"# waiting {args.settle_sec}s for the collector's batch processor to flush the final export")
    time.sleep(args.settle_sec)

    return _report(scrape(args.collector_metrics), report, wall, args)


def _report(exposition: str, report: dict[str, object], wall: float, args: argparse.Namespace) -> int:
    types, series, buckets = parse(exposition)

    emit()
    emit("## Series minted by ONE analysis")
    emit()
    emit("| prometheus family | type | series |")
    emit("| --- | --- | ---: |")
    for family in sorted(types):
        total = sum(count for name, count in series.items() if name.startswith(family))
        emit(f"| `{family}` | {types[family]} | {total} |")
    emit(f"| **total** | | **{sum(series.values())}** |")

    emit()
    emit("## Emission rate")
    emit()
    if report:
        emit(f"- analysis wall clock: **{report['wall_sec']:.2f} s** for **{args.minutes * 60} s** of audio")
        emit(f"- fine windows: **{report['fine_windows_analyzed']} / {report['fine_windows_total']}**")
        emit(f"- coarse windows: **{report['coarse_windows_analyzed']} / {report['coarse_windows_total']}**")
        emit(f"- harness wall clock including synthesis: **{wall:.2f} s**")
    emit(f"- total series at the scrape endpoint: **{sum(series.values())}**")
    model_combinations = len(set(re.findall(r'model_name="[^"]+",model_variant="[^"]+"', exposition)))
    emit(f"- distinct model combinations observed: **{model_combinations}** (the registry declares 34)")

    emit()
    emit("## Measured histogram distributions")
    emit()
    for family in sorted(buckets):
        distribution = bucket_distribution(buckets[family])
        if not distribution:
            continue
        observations = distribution[-1][1]
        if not observations:
            continue
        emit(f"### `{family}`  (n = {observations:.0f})")
        emit()
        emit("| le | cumulative | share |")
        emit("| ---: | ---: | ---: |")
        for le, count in distribution:
            label = "+Inf" if le == float("inf") else f"{le:g}"
            emit(f"| {label} | {count:.0f} | {count / observations * 100:.1f}% |")
        emit()

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "collector-exposition.txt").write_text(exposition, encoding="utf-8")
        (out / "analysis-run.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        emit(f"# raw exposition and run report written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
