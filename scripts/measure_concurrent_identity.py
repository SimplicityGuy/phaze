#!/usr/bin/env python
"""Measure what a REAL collector does with two concurrent analysis producers.

phaze-21nnf acceptance 1: *"two concurrent analysis processes exporting interleaved counter
totals produce a MONOTONIC series ... demonstrated against the real otel-collector from the
example compose, not against an in-process reader."*

**Why an in-process reader cannot answer this.** The defect is not in what phaze records --
each process's own ``InMemoryMetricReader`` shows a perfectly monotonic counter. It is in
what the COLLECTOR does when two resource identities are identical: its Prometheus exporter
keeps one series per identity and takes the last write. That behaviour lives in the
collector, so only the collector can be asked about it. This is ADR-0012 rule 3 applied to
a claim about a component phaze does not own.

Three arms, because the decision needed all three answered against the same collector:

    shared  -- today's default: one `service.instance.id` for every child
    slots   -- the fix: a bounded worker-slot index appended to the identity
    delta   -- the rejected alternative: delta temporality under one identity

Each arm runs two producer processes that increment a real catalogued counter
(``phaze.analysis.windows``) by different steps on INTERLEAVED schedules, forcing a flush at
each step, while the parent scrapes the collector's exposition. Flush spacing is deliberately
several times the collector's `batch` timeout (5 s in the example config): at a tighter
spacing the batch processor coalesces the two producers' points and the dips DISAPPEAR from
the exposition, which makes the defect look intermittent and is itself worth knowing.

    docker compose -f docker-compose.telemetry.example.yml up -d otel-collector
    uv run python scripts/measure_concurrent_identity.py --arm shared

**RESTART THE COLLECTOR BETWEEN ARMS.** The prometheus exporter holds a series for
``metric_expiration`` (10 m in the example config), so the ``phaze-analysis`` series arm A
left behind is still being exposed while arm B runs and gets counted as one of arm B's. Each
arm in the record was taken after ``docker restart``; ``--arm all`` runs them back to back in
one process and does NOT restart anything, so use it only for a quick look.

NOTE ON DOCKER-ON-MAC: Colima and Lima share only ``$HOME``, so a bind mount of a config
living outside it silently mounts an empty DIRECTORY and the collector exits with "is a
directory". Copy ``deploy/telemetry/otel-collector.example.yaml`` under ``$HOME`` and mount
it from there; verify with ``shasum -a 256`` that it is byte-identical, because a
measurement taken against a config you edited is a measurement of something else.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
from pathlib import Path
import re
import subprocess  # nosec B404 -- fixed argv re-invoking THIS script as a producer, no shell
import sys
import time
from typing import TYPE_CHECKING, Any
import urllib.parse
import urllib.request


if TYPE_CHECKING:
    from collections.abc import Sequence


#: The counter this measures. A real catalogued instrument, not a throwaway: the whole
#: question is about ``phaze_analysis_*`` panels, and a metric outside the catalogue would
#: not survive `instruments._checked_attributes` anyway.
COUNTER = "phaze.analysis.windows"
PROM_NAME = "phaze_analysis_windows_total"
_SERIES = re.compile(rf"^({PROM_NAME}\{{[^}}]*\}})\s+([0-9.e+-]+)$", re.M)
_INSTANCE = re.compile(r'instance="([^"]*)"')


def _label(series: str) -> str:
    """The `instance` label alone -- what the whole finding turns on -- or the full series."""
    found = _INSTANCE.search(series)
    return found.group(1) if found else series


def _say(text: str) -> None:
    print(text, flush=True)  # noqa: T201


def _producer(step: float, start: float, offset: float, period: float, flushes: int) -> None:
    """One simulated analysis child: increment, force an export, repeat on schedule.

    ``force_flush`` rather than waiting on ``OTEL_METRIC_EXPORT_INTERVAL`` because the
    interleaving has to be DETERMINISTIC -- the whole finding is about which process wrote
    last, and a 15 s periodic reader would make that a coin toss the record could not
    reproduce.
    """
    from phaze.telemetry import (  # noqa: PLC0415  # deferred: the parent process never installs an SDK
        bootstrap,
        configure_telemetry,
        instruments,
        shutdown_telemetry,
    )

    on = configure_telemetry("analysis")
    identity = bootstrap._resource_attributes("analysis", "phaze-analysis")["service.instance.id"]
    _say(json.dumps({"event": "producer_start", "pid": os.getpid(), "telemetry_on": on, "instance": identity}))

    provider = bootstrap._meter_provider
    if provider is None:
        msg = "telemetry did not configure: is OTEL_EXPORTER_OTLP_ENDPOINT set?"
        raise SystemExit(msg)

    own_total = 0.0
    for index in range(flushes):
        delay = start + offset + index * period - time.time()
        if delay > 0:
            time.sleep(delay)
        instruments.add(COUNTER, step, tier="fine", outcome="analyzed")
        own_total += step
        provider.force_flush(5_000)
        _say(json.dumps({"event": "flush", "pid": os.getpid(), "instance": identity, "t": round(time.time() - start, 2), "own_total": own_total}))
    shutdown_telemetry(3_000)


def _checked_scrape_url(url: str) -> str:
    """Admit only an ``http``/``https`` scrape URL; refuse anything else loudly.

    ``urlopen`` honours whatever scheme it is handed, and the ones that are not HTTP are
    the interesting ones: ``file:`` turns a scrape into a local file read, and this URL
    arrives from ``--scrape-url`` on the command line. The scheme is therefore checked
    against an allowlist rather than checked for the schemes to reject.

    Raises rather than falling back, and is called OUTSIDE :func:`_scrape`'s ``except``:
    a dead collector is a normal condition this harness reports and continues through, but
    a URL that is not a scrape endpoint means the run would measure something other than
    the thing it claims to.
    """
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in {"http", "https"}:
        msg = f"--scrape-url must be http or https, not {scheme!r}: {url!r}"
        raise ValueError(msg)
    return url


def _scrape(url: str) -> dict[str, float]:
    """The collector's exposition for this counter, keyed by its full label set."""
    checked = _checked_scrape_url(url)
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        body = urllib.request.urlopen(checked, timeout=5).read().decode()  # noqa: S310  # nosec B310 -- the operator's own collector scrape URL, http(s) only
    except Exception as exc:  # pragma: no cover - a dead collector is the operator's signal, not a crash
        _say(f"  scrape failed: {exc}")
        return {}
    return {series: float(value) for series, value in _SERIES.findall(body)}


def prometheus_increase(samples: Sequence[float]) -> float:
    """What ``increase()`` would report over ``samples``, counter resets included.

    Prometheus treats any DECREASE as a counter restart and counts the post-reset value as
    increment, which is correct for a process that genuinely restarted and is exactly what
    turns a last-write-wins merge into an over-count. This is the arithmetic that makes the
    finding a number rather than an adjective.
    """
    total = 0.0
    previous: float | None = None
    for value in samples:
        if previous is None:
            pass
        elif value >= previous:
            total += value - previous
        else:
            total += value  # a reset: everything since zero is new
        previous = value
    return total


def _delivered_between(transcripts: Sequence[str], steps: Sequence[float], after: float, until: float) -> float:
    """What the PRODUCERS say they delivered inside the sampled window.

    The non-circular truth for an `increase()` comparison. Each producer prints one JSON
    line per flush carrying its own running total, so the increment is the step and the
    only question is whether the flush lands in the window -- an increment flushed before
    the first scrape is already baked into the first sample, and Prometheus's `increase()`
    never counts the first sample's own rise from zero.
    """
    total = 0.0
    for text, step in zip(transcripts, steps, strict=True):
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "flush" and after < float(event["t"]) <= until:
                total += step
    return total


def _run_arm(arm: str, args: argparse.Namespace) -> dict[str, Any]:
    """One arm end to end: two producers, a scrape series, and a verdict."""
    env = dict(os.environ)
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = args.otlp_endpoint
    # Keep the periodic reader out of the way; every export here is an explicit flush.
    env["OTEL_METRIC_EXPORT_INTERVAL"] = "600000"
    env["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] = "delta" if arm == "delta" else "cumulative"
    env.pop("PHAZE_TELEMETRY_INSTANCE", None)
    env.pop("PHAZE_TELEMETRY_SLOT", None)

    start = time.time() + 3.0
    steps = (10.0, 100.0)
    processes = []
    for index, step in enumerate(steps):
        arm_env = dict(env)
        if arm == "slots":
            # What the agent worker's slot pool does for a real child.
            arm_env["PHAZE_TELEMETRY_SLOT"] = str(index)
        processes.append(
            subprocess.Popen(  # noqa: S603  # nosec B603 -- fixed argv: this interpreter, this file, numeric arguments
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--producer",
                    str(step),
                    str(start),
                    str(index * args.period / 2),
                    str(args.period),
                    str(args.flushes),
                ],
                env=arm_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        )

    samples: list[tuple[float, dict[str, float]]] = []
    end = start + args.flushes * args.period + 6.0
    while time.time() < end:
        time.sleep(args.scrape_every)
        samples.append((round(time.time() - start, 2), _scrape(args.scrape_url)))

    transcript = [process.communicate(timeout=120)[0] for process in processes]
    true_total = sum(step * args.flushes for step in steps)

    _say(f"\n===== ARM: {arm} =====")
    for line in transcript:
        _say(line.rstrip())

    series_names = sorted({name for _, point in samples for name in point})
    _say(f"\nexposed series at the collector: {len(series_names)}")
    for name in series_names:
        instance = _INSTANCE.search(name)
        _say(f"  instance={instance.group(1) if instance else '?'}")

    _say("\nscrape sequence (t, per-series values):")
    verdicts: dict[str, Any] = {"arm": arm, "true_total": true_total, "series": {}}
    for moment, point in samples:
        rendered = ", ".join(f"{_label(name)}={value:g}" for name, value in sorted(point.items()))
        _say(f"  t={moment:6.2f}  {rendered or '(no series yet)'}")

    summed_increase = 0.0
    exposed_final = 0.0
    for name in series_names:
        values = [point[name] for _, point in samples if name in point]
        monotonic = all(later >= earlier for earlier, later in itertools.pairwise(values))
        increase = prometheus_increase(values)
        summed_increase += increase
        final = values[-1] if values else None
        if values:
            exposed_final += values[-1]
        verdicts["series"][_label(name)] = {
            "monotonic": monotonic,
            "final": final,
            "prometheus_increase": increase,
            "samples": values,
        }
        _say(f"\n  {_label(name)}: monotonic={monotonic} final={final} increase()={increase:g}")

    # TWO comparisons, because each alone misleads. `increase()` is only ever about the
    # SAMPLED WINDOW, so comparing it to a producer's lifetime total charges the fix for the
    # counter's rise from zero before the first scrape -- which made a correct arm read as
    # -33% error on the first pass at this.
    #
    # And the window's truth must come from the PRODUCERS, not from the exposition. Deriving
    # it from the exposed series' own first-to-last rise asks the corrupted series to certify
    # its own corruption, which is circular precisely where the answer matters: on the shared
    # arm it quietly reported the merge's own rise as the truth the merge was measured
    # against. So it is summed from the flush transcript instead.
    window_truth = _delivered_between(transcript, steps, samples[0][0], samples[-1][0])
    verdicts["summed_increase"] = summed_increase
    verdicts["window_truth"] = window_truth
    verdicts["increase_error_pct"] = round((summed_increase - window_truth) / window_truth * 100.0, 1) if window_truth else None
    verdicts["exposed_final_sum"] = exposed_final
    verdicts["final_error_pct"] = round((exposed_final - true_total) / true_total * 100.0, 1) if true_total else None
    verdicts["all_series_monotonic"] = all(series["monotonic"] for series in verdicts["series"].values())

    _say(f"\n  every exposed series monotonic: {verdicts['all_series_monotonic']}")
    _say(
        f"  sum(increase()) over the sampled window: {summed_increase:g} vs a window truth of {window_truth:g}  ({verdicts['increase_error_pct']:+}%)"
    )
    _say(f"  sum(final exposed value): {exposed_final:g} vs a true delivered total of {true_total:g}  ({verdicts['final_error_pct']:+}%)")
    return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--producer", nargs=5, metavar=("STEP", "START", "OFFSET", "PERIOD", "FLUSHES"), help="internal: run as one producer child")
    parser.add_argument("--arm", default="all", choices=["shared", "slots", "delta", "all"])
    parser.add_argument("--otlp-endpoint", default="http://127.0.0.1:4318")
    parser.add_argument("--scrape-url", default="http://127.0.0.1:8889/metrics")
    parser.add_argument(
        "--period", type=float, default=16.0, help="seconds between one producer's flushes; keep it well above the collector's batch timeout"
    )
    parser.add_argument("--flushes", type=int, default=3)
    parser.add_argument("--scrape-every", type=float, default=4.0)
    parser.add_argument("--out", type=Path, help="write the verdicts as JSON here")
    args = parser.parse_args(argv)

    if args.producer:
        step, start, offset, period, flushes = args.producer
        _producer(float(step), float(start), float(offset), float(period), int(flushes))
        return 0

    arms = ["shared", "slots", "delta"] if args.arm == "all" else [args.arm]
    verdicts = [_run_arm(arm, args) for arm in arms]
    if args.out:
        args.out.write_text(json.dumps(verdicts, indent=2), encoding="utf-8")
        _say(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
