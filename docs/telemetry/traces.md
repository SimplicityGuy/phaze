# Traces: per-file progress, and the only place it exists

**Metrics cannot tell you how far along one file is. That is by construction, not an
oversight.** File identity, window index and chunk index are span attributes and are
deliberately never metric labels — the constraint that holds an 11,428-file corpus at 8,587
series ([`metric-catalogue.md`](metric-catalogue.md) §1). `phaze_analysis_windows_total{tier}`
is a monotonic counter with **no file dimension**; Prometheus can give you fleet-level rate and
nothing per-file.

**Adding a `file_id` label is not the fix.** It is the specific thing the cardinality budget
exists to prevent, and `test_no_label_name_looks_like_an_identifier` fails the build on it.
Per-file and per-chunk detail lives in traces or nowhere.

The topology decision, the sampling arithmetic and the alternatives are
[`docs/design/0017-telemetry-export-topology.md`](../design/0017-telemetry-export-topology.md)
§7. This page is what a trace actually looks like, and the record that one was viewed.

______________________________________________________________________

## 1. What phaze emits

One file's analysis is **one trace**, and it crosses the exec'd-analysis process boundary — the
worker injects a W3C `TRACEPARENT` into the child's environment and the child continues the
same trace rather than starting a second one.

| span | one per | carries |
| --- | --- | --- |
| `analysis.subprocess` | file (worker side) | file path, stall timeout |
| `analysis.child` | file (child side) | file path |
| `analysis.file` | file | audio duration, the four window counts |
| `analysis.tier` | tier (2) | `tier`, window total, chunk total |
| `analysis.chunk` | chunk | `tier`, **`chunk_index`**, chunk window count |
| `analysis.chunk.decode` | chunk | `tier`, `chunk_index` |
| `analysis.chunk.derive` | coarse chunk | `tier`, `chunk_index` |
| `analysis.model_sweep` | model × coarse chunk (34 per chunk) | model name, variant, classifier type, buffer count |

**There are no per-window spans and no per-model-per-window spans.** That is deliberate: those
costs are carried by histograms whose cardinality is bounded at 34. It is also what makes
always-on sampling affordable — see §4.

`http.server.request` and `saq.job` spans exist on the api and worker paths respectively.

## 2. Seeing one

```bash
docker compose -f docker-compose.telemetry.example.yml up -d      # collector, Tempo, Prometheus, Grafana
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# ... run an analysis ...
open http://localhost:3000                                        # Explore -> Tempo -> paste the trace id
```

From a dashboard: **phaze / Analysis pipeline (live)** and **phaze / Analysis cost breakdown**
carry a *Find the trace for one file* link into Explore. It targets a `${tracesource}` template
variable, chosen at import time exactly as `${datasource}` is, so the dashboards stay importable
into any Grafana; one without a trace datasource simply leaves the link inert.

## 3. What was actually seen

**Measured 2026-08-27** — a real analysis, real essentia, the real 34-graph model set, exported
into the example stack and read back **through Grafana's own Tempo datasource proxy**, which is
the path the trace UI uses. Not a schema check, and not an assertion that the exporter was
called.

A 720-second input (24 fine windows → 1 fine chunk; 4 coarse windows → 1 coarse chunk) produced
**45 spans in one trace, across two services and two OS processes**:

```
analysis.demo               310507.6 ms  [phaze-agent/47598]
  analysis.subprocess       310507.0 ms  [phaze-agent/47598]
    analysis.child          310334.0 ms  [phaze-analysis/47599]     <- exec boundary, same trace
      analysis.file         308487.7 ms  [phaze-analysis/47599]  audio_duration_sec=720
        analysis.tier        19058.2 ms  tier=fine
          analysis.chunk     19058.1 ms  tier=fine   chunk_index=0  chunk_windows=24
            analysis.chunk.decode  12875.0 ms
        analysis.tier       289202.3 ms  tier=coarse
          analysis.chunk    289202.3 ms  tier=coarse chunk_index=0  chunk_windows=4
            analysis.chunk.decode   1390.2 ms
            analysis.model_sweep    8091.8 ms  mood_acoustic / musicnn
            analysis.model_sweep    8039.6 ms  mood_acoustic / musicnn
            analysis.model_sweep   10208.5 ms  mood_acoustic / vggish
            ... 34 model sweeps in total ...
            analysis.chunk.derive      0.2 ms
```

Three things that trace settles, none of which a metric could have answered:

- **The exec boundary is genuinely crossed inside one trace** — two `service.name` values
  (`phaze-agent`, `phaze-analysis`) and two distinct pids, one trace id.
- **The tier split is visible directly from one file**: fine **19,058 ms (6.2%)** against
  coarse **289,202 ms (93.8%)**. That sits where `phaze-zaf2l` §3b's **5.31% / 94.69%** does, which
  is what a *short* file should give: the split is **duration-dependent** and inverts to 49.3% fine
  on a 10 h 03 m file (that spike's 2026-08-28 forward note, `phaze-bg115`). The point here is not
  the number but that **one file's tier split is readable at all** — which is the measurement this
  entire epic was motivated by, and which `phaze-zaf2l` could only get by differencing pod logs.
- **`chunk_index` is on the span**, so per-chunk duration against chunk index is a query rather
  than an inference.

The trace was **stable at 45 of 45 spans across five consecutive queries** through Grafana, and
Tempo's own counters confirmed `tempo_discarded_spans_total` = 0 for every reason.

> **The unstable version of this result is recorded in ADR-0017 §7d, and it is the more useful
> half.** With Tempo's `max_block_duration` below the trace's lifetime, the same analysis was
> split across 8 blocks and returned **28 spans on one query and 16 on the next, out of 45**,
> with nothing dropped and no error anywhere. A partial trace that looks complete is the failure
> mode to design against, and it is why `max_block_duration` must exceed the longest trace you
> intend to read whole.

## 4. Sampling: always on, and why that is affordable

No head sampling, no tail sampling, no ratio-based sampler. Spans are per **chunk**, not per
window, so the count scales with `ceil(duration / 1800)`:

| file | spans |
| --- | ---: |
| corpus median, 3,531.967 s | **46** |
| 36,182.359 s (10 h 03 m) | **306** |
| corpus maximum, 43,466.880 s (12 h 04 m) | **388** |

Whole-corpus re-analysis of all 11,412 files carrying a duration: **~566,073 spans**, a mean of
**49.6 per file**, and at the measured 2.4480 files/hour a steady state of **~121 spans/hour —
0.034 spans/second**.

Head sampling would discard whole traces at random and lose exactly the long runs that matter
most; tail sampling would need a decision window longer than a 10-hour trace. The worst case,
**388 spans, is 18.9% of the 2,048-span bounded queue** phaze already configures, so one file
cannot overflow it.

## 5. What phaze does not own

**The production trace store is homelab's**, exactly as Prometheus and Grafana are. The Tempo in
`docker-compose.telemetry.example.yml` is a development illustration and says so in its own
header; it makes no retention, sizing or availability decision. `homelab-73j` carries the
production-side request.
