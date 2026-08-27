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

______________________________________________________________________

## 7. The trace half — added 2026-08-27 by `phaze-m1drf.8`

**§§1–6 above argue the METRICS path and named no trace backend at all.** That was a real gap
rather than an omission of detail: `phaze-m1drf.1` shipped complete per-file tracing — one
file is one trace, crossing the exec'd-analysis process boundary — and the collector pipeline
routed those spans to `debug`, which prints a line and drops them. **Emission was built; the
destination was never specified.**

> **Operator decision 2026-08-27.** Question as put: *"ADR-0017 names no trace backend. Traces
> are emitted to `/v1/traces` and, as things stand, would arrive at a collector with nowhere to
> put them. Want me to add it to homelab-73j? It is a small amendment: if you want per-file
> analysis progress, you need a trace store; the spans already exist."* Answer as given,
> verbatim: **"yes please. since we're working on the otel work here, this should include
> traceability. so this item lands squarely as part of this epic/molecule."** Durable record:
> bead `phaze-m1drf.8` and a comment on epic `phaze-m1drf`.

### 7a. Why traces are not optional here

**Metrics cannot answer "how far along is THIS file", by construction.** File identity, window
index and chunk index are span attributes and are deliberately never metric labels — that
constraint is what holds an 11,428-file corpus at 8,587 series
(`docs/telemetry/metric-catalogue.md` §1). `phaze_analysis_windows_total{tier}` and
`phaze_analysis_chunks_total{tier}` are monotonic counters with **no file dimension**, so
Prometheus can only ever yield fleet-level rate and throughput.

**Adding a `file_id` label is not the fix and must not be read as one.** It is the specific
thing the cardinality budget exists to prevent, and the guard test
`test_no_label_name_looks_like_an_identifier` fails the build on it. Per-file and per-chunk
detail lives in traces or nowhere.

That blindness was paid for, not theorised: during the criterion-6 long run the dispatcher
watched a 10 h 03 m analysis for over nine hours with no per-file progress signal of any kind,
reduced to inferring the phase from CPU oscillation between ~1 core (chunk decode) and ~3.8
cores (model sweep) — and guessed wrong three times.

### 7b. Decision: Grafana Tempo, written to by the collector

**The collector's `traces` pipeline exports OTLP to a trace store; the example stack uses
Grafana Tempo.** The grounds, held to the same standard §3 sets for metrics:

- **Tempo — CHOSEN.** homelab already runs Grafana 12.3.1, and Tempo is its native trace
  store: one datasource type, no second UI, and the dashboards this epic already ships live in
  the same place an operator would open a trace. Its object/filesystem storage model suits a
  single-operator archive with no separate database to run.
- **Jaeger — REJECTED, but only just.** Lighter to stand up and a perfectly good store. It
  loses on integration rather than on merit: it is a second UI to run and authenticate, and
  the metrics→traces link in §7c would cross an application boundary instead of staying inside
  Grafana. If homelab already ran Jaeger this decision would flip, and nothing in phaze
  depends on the choice — phaze speaks OTLP to a collector and knows nothing about what is
  behind it.
- **Zipkin — REJECTED.** Would need a second exporter and a second wire format for no gain;
  phaze's exporter is OTLP and the collector already translates.
- **No store at all, keeping `debug` — REJECTED.** That is the status quo this section
  exists to end. It also fails silently in the worst way: the pipeline is healthy, the
  collector logs the spans, and nothing is queryable.

**phaze's own configuration does not change.** It exports OTLP to
`OTEL_EXPORTER_OTLP_ENDPOINT`; the collector decides where traces land. Swapping Tempo for
Jaeger is a collector-config edit with no phaze change, which is the point of exporting to a
collector rather than to a backend.

### 7c. Metrics → traces: exemplars are EMITTED but do not survive this exposition

**phaze attaches exemplars correctly.** Measured 2026-08-27 against opentelemetry-sdk 1.44.0
with an in-memory reader: a `phaze.analysis.model.inference.duration` observation recorded
inside an active span carries an exemplar with a valid `trace_id` and `span_id`. Nothing needs
adding on phaze's side.

**They do not reach the scrape endpoint through the collector's `prometheus` exporter.**
Measured against `otel/opentelemetry-collector-contrib` 0.140.0: `:8889/metrics` returned
**zero** exemplar markers, including under OpenMetrics content negotiation
(`Accept: application/openmetrics-text; version=1.0.0`).

So the shipped path is the **documented link**, which `phaze-m1drf.8` acceptance 6 allows as
the fallback: the two analysis dashboards carry a *Find the trace for one file* link into
Explore against a `${tracesource}` template variable — picked at import time exactly as
`${datasource}` is, so importability is preserved and a Grafana with no trace datasource
simply leaves the link inert.

**This is a collector-side gap, not a phaze-side one**, and it is worth homelab knowing:
enabling exemplar pass-through (for instance via `prometheusremotewrite`, or a scrape path
that carries them) would light up exemplar links with no change to phaze at all.

### 7d. What a multi-hour trace costs, and the sampling posture

**Sampling posture: ALWAYS ON. No head sampling, no tail sampling, no `TraceIdRatioBased`
sampler.** That is affordable here, and the arithmetic is the trace analogue of the cardinality
budget:

**phaze emits no per-window spans and no per-model-per-window spans** — a deliberate
`phaze-m1drf.1` decision. Per-window and per-model-per-window cost is carried by *histograms*,
whose cardinality is bounded at 34. Spans are per **chunk**, so the count scales with
`ceil(duration / 1800)` coarse chunks rather than with windows:

| file | fine ch | coarse ch | spans |
| --- | ---: | ---: | ---: |
| corpus median, 3,531.967 s | 2 | 1 | **46** |
| 3,578.964 s (the criterion-6 short arm) | 2 | 1 | **46** |
| 36,182.359 s (the long arm, 10 h 03 m) | 21 | 7 | **306** |
| corpus maximum, 43,466.880 s (12 h 04 m) | 25 | 9 | **388** |

`spans = 5 + 2 x (fine_chunks + coarse_chunks) + coarse_chunks + 34 x coarse_chunks`.

**Whole-corpus figures**, over the duration distribution in
`docs/spikes/phaze-zaf2l-where-phaze-spends-time.md` §2: **~566,073 spans** for a complete
re-analysis of all 11,412 files carrying a duration, a mean of **49.6 spans per file**, and at
the measured 2.4480 files/hour a steady-state rate of **~121 spans/hour — 0.034 spans/second.**

That is why always-on wins outright. Head sampling would discard whole traces at random and
would lose precisely the long runs that matter most — the 12-hour set is the one nobody can
otherwise see into. Tail sampling would need a decision window longer than the trace itself,
which for a 10-hour analysis is absurd. And the worst case, **388 spans, is 18.9% of the
2,048-span bounded queue** phaze already configures, so a single file cannot overflow it.

**The property that DOES bite on a multi-hour trace, measured rather than predicted.** An
analysis span stays open for the length of the file, so its spans arrive over hours and are
written across many storage blocks. With Tempo's `max_block_duration` set below the trace's
lifetime, a single trace is split and **the read path returns a partial trace with no error**:
measured 2026-08-27 with `max_block_duration: 1m`, a 12-minute analysis was cut across **8
blocks** and `GET /api/traces/<id>` returned **28 spans on one call and 16 on the next, out of
45** — while Tempo had accepted all 45 and discarded none
(`tempo_discarded_spans_total` = 0 for every reason). Nothing was lost in transit; the trace
was simply unreadable as a whole.

**So `max_block_duration` must exceed the longest trace you intend to read whole** — for phaze
that means longer than the longest file's analysis, currently over 12 hours of audio at a
measured 0.5591x wall-clock ratio uncontended. The example stack sets 2 h, which is ample for a
demo and honest about being a development value. **This is the single most important thing for
homelab to get right when sizing a real Tempo**, and it is not obvious: the failure presents as
a trace that looks complete and is not.

### 7e. What is lost, and how it fails

| situation | what happens |
| --- | --- |
| no trace backend configured | collector's `traces` pipeline drops the spans; phaze is unaffected |
| backend unreachable | the collector queues and drops per its own limits; phaze is unaffected, because phaze talks only to the collector |
| **collector** unreachable | phaze's `BatchSpanProcessor` queue fills to **2,048 spans** and then drops, oldest first. A single file cannot fill it (worst case 388), so what is lost is older files' traces, never a truncated current one |
| process exits mid-trace | the analysis span never closes; the store shows an incomplete trace. Bounded by `PHAZE_TELEMETRY_FLUSH_TIMEOUT_MS` at exit |
| trace store restarts | anything not yet flushed from its ingester is lost. Tempo's own concern, not phaze's |

**Traces are not, and must not become, a delivery guarantee.** Everything in §4 applies
unchanged: a dropped span is acceptable, a failed analyze job is not.

### 7f. Verification

Per ADR-0012 rule 3, discharged against the real consumer — the trace UI, not an assertion
that the exporter was called. Recorded in `docs/telemetry/traces.md`.
