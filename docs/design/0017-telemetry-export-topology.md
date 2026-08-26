# ADR-0017 (telemetry export topology): push OTLP to a collector phaze does not own

- **Status:** accepted
- **Date:** 2026-08-26
- **Bead:** `phaze-m1drf.2` (epic `phaze-m1drf`)
- **Supersedes / superseded by:** nothing

______________________________________________________________________

## 1. Context

`phaze-m1drf.1` instruments the pipeline. This ADR decides where the data goes, and the
deciding question is **not** "which backend is nicest" — it is:

> **What happens to a k8s analyze Job's final counters when it exits between scrapes?**

phaze has three telemetry producers and they are not alike:

| producer | lifetime | reachability |
| --- | --- | --- |
| `phaze-api` | long-lived compose service on `host-prod` | on the home LAN |
| `phaze-controller` / `phaze-agent` | long-lived compose services | on the home LAN / `host-store` |
| **an analyze Job** | **a k8s pod on `vox` that runs for HOURS and then exits** | reaches the home server **over Tailscale** |

The third one is the whole problem. At the production operating point one file costs
**1.4951×** its own duration (`docs/spikes/phaze-zaf2l-where-phaze-spends-time.md` §3a), so
a pod lives for hours, accumulates counters the whole time, and then goes away. Anything
that depends on *being scraped* has to catch it while it is alive, and anything that
depends on *its final push* has to survive homelab being unreachable at that exact moment.

**phaze does not own the collector, Prometheus, Grafana or Alertmanager.** homelab does,
and they are already running. This ADR therefore decides only what phaze EMITS and how; it
makes no decision about retention, storage sizing, scrape topology or alert routing.

## 2. Decision

**phaze PUSHES OTLP over HTTP/protobuf to a collector endpoint given by
`OTEL_EXPORTER_OTLP_ENDPOINT`, and that collector exposes an aggregated Prometheus
endpoint for homelab's Prometheus to scrape. phaze operates no pushgateway and speaks no
remote-write.**

## 3. The alternatives, and why each loses

### 3a. Prometheus scrapes the analyze pod directly — REJECTED

The pod would have to be up when Prometheus comes round. It is not: an analyze Job exists
for the length of one file and then terminates, so a scrape interval that is long enough
to be cheap is long enough to miss whole jobs, and the *last* interval of every job is
missed by construction. Service discovery for a Job that Kueue admits and deletes is
additional machinery on homelab's side for a target that is gone before it stabilises.

It also inverts the network direction. `vox` reaches the home server over **Tailscale**;
making Prometheus reach *into* the burst node's pod network is a strictly larger surface
than one outbound HTTP POST.

### 3b. A pushgateway — REJECTED

A pushgateway is the classic answer for a batch job, and it is the wrong one here for two
reasons that are properties of the pushgateway itself:

- **Its series are immortal.** A pushgateway holds what was last pushed to a group until
  something deletes it. An analyze Job pushing under a per-job grouping key leaves a series
  behind for every file ever analyzed — 11,428 of them and counting, in a store phaze does
  not own. Pushing under a *shared* key instead makes concurrent pods (cap = 4 on `vox`)
  overwrite each other, which silently loses three quarters of the data.
- **It flattens the timeline.** A pushgateway holds a value, not a history; a job that ran
  for two hours contributes one point. The question this epic exists to answer is *how the
  coarse tier splits over time*, which that shape cannot express.

### 3c. Prometheus remote-write from phaze — REJECTED

It would work, and it moves a retention-and-authentication decision into phaze's
configuration — exactly the boundary this epic is trying not to cross. It also gives up the
collector's buffering: with remote-write, phaze's own process is the only thing between an
observation and a Prometheus that might be restarting.

### 3d. OTLP push to a collector, collector exposes Prometheus — CHOSEN

It answers the deciding question directly. **A pod's final counters leave the pod on the
pod's own schedule, not on Prometheus's.** The collector is long-lived, so the series it
exposes survive the pod that produced them, and homelab's Prometheus scrapes a target that
is always there. It also keeps every deployment decision on homelab's side of the line: the
collector's own configuration decides what is retained and for how long, and phaze's
configuration is one URL.

## 4. What this costs, stated rather than glossed

**Cumulative temporality, and what "the last export" is worth.** The SDK exports CUMULATIVE
metrics (the default, and what the Prometheus path requires). So each export carries the
running total rather than a delta, and losing ONE export loses only the increments since
the previous successful one — bounded by `OTEL_METRIC_EXPORT_INTERVAL`, which phaze
defaults to **15 s**. Losing the FINAL export of a job that has been exporting successfully
therefore costs at most the last 15 seconds of that job.

**The case where the loss is total is different and must be named:** if the collector is
unreachable for the *whole* run, nothing was ever delivered and the entire job's telemetry
is lost, not merely its tail. That is the accepted trade — a dropped metric is acceptable,
a failed analyze job is not — and it is why the alert rules in `phaze-m1drf.5` are built to
be silent rather than to fire on absence.

**The flush at exit is bounded and is allowed to fail.**
`phaze.telemetry.shutdown_telemetry` force-flushes within
`PHAZE_TELEMETRY_FLUSH_TIMEOUT_MS` (default **3,000 ms**) and returns False when it could
not finish. The SDK's own `atexit` shutdown is DISABLED (`shutdown_on_exit=False`) because
it would spend the SDK's 30 s budget instead — 30 s of a pod refusing to die, holding a
Kueue slot, every time homelab is rebooting.

**A collector holds stale series until it restarts.** Measured while building this: after a
metric's label set changed, the collector's Prometheus exporter kept converting the OLD
series on every scrape and logging `failed to convert` for it, alongside the new one, until
the collector process was recreated. Relevant to homelab if phaze's label set ever changes.

## 5. Consequences

- phaze's telemetry configuration is `OTEL_EXPORTER_OTLP_ENDPOINT` plus, optionally,
  `OTEL_EXPORTER_OTLP_HEADERS` and `PHAZE_TELEMETRY_INSTANCE`. Nothing else.
- homelab owns the collector, its retention, its scrape topology and any Alertmanager.
  `docker-compose.telemetry.example.yml` in this repo is a **development illustration** and
  says so at the top of the file.
- **Emitting to an endpoint nobody is listening on is a valid, tested state.** phaze's
  molecule does not block on homelab's; see `docs/telemetry/exporter.md` §4.

## 6. Verification

Per ADR-0012 rule 3, the claims above are discharged against real consumers:

| claim | discharged by |
| --- | --- |
| an unreachable collector cannot fail or stall an analysis | `tests/shared/telemetry/test_telemetry_never_breaks_analysis.py` — a REAL analysis run to completion against an unroutable address, result compared to the telemetry-off run |
| a SLOW collector cannot either | the same file — a real HTTP listener that accepts and then sleeps past every timeout |
| exit is bounded | the same file — measured elapsed on `shutdown_telemetry` against that listener |
| the export timeout is what phaze thinks it is | `tests/shared/telemetry/test_export_timeout_units.py` — constructs the REAL exporter and reads back the resolved timeout, because `OTEL_EXPORTER_OTLP_TIMEOUT` is in SECONDS in opentelemetry-python 1.44.0 while the specification says milliseconds (ADR-0016's shape) |
| the metric names and labels survive the OTLP → Prometheus translation | measured against a real `otel/opentelemetry-collector-contrib` 0.140.0, recorded in `docs/telemetry/metric-catalogue.md` §5 — which is how a reserved-label collision that silently DELETED two metrics was found |
