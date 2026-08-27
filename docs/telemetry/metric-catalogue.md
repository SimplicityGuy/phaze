# The metric contract

**This is the artifact homelab wires against.** Every metric phaze emits, its type, its unit,
its label set, and the bounded cardinality of every label. Pointing phaze at a collector is
[`exporter.md`](exporter.md); the topology decision is
[`docs/design/0017-telemetry-export-topology.md`](../design/0017-telemetry-export-topology.md).

The source of truth is `src/phaze/telemetry/catalogue.py`. Instruments are built **only**
from it, `tests/shared/telemetry/test_metric_catalogue.py` fails the build if a metric here
is absent from that file or a label there has no stated bound, and
`instruments._checked_attributes` rejects an undeclared attribute at runtime.

______________________________________________________________________

## 1. Why the cardinality budget is a hard constraint

**phaze does not own the Prometheus that will scrape this.** homelab does, and it is shared.
A high-cardinality label added here damages someone else's storage, and it does so silently
— nothing fails, nothing logs, the series count simply grows.

The dangerous labels are the obvious ones. The archive holds **11,428 files**; analysis
touches each file in up to **34 models × N windows** combinations; the longest file has
**241** coarse windows and **1,449** fine ones. A `file_id` label on the per-model inference
histogram alone would be 11,428 × 34 × 15 bucket series — in the tens of millions.

So the rule, enforced rather than remembered:

> **File identity, window index and chunk index are SPAN attributes. They are never metric
> labels.** A span is stored per-occurrence and aged out; a metric label is stored forever,
> per series.

`catalogue.FORBIDDEN_LABEL_SUBSTRINGS` lists the names that cannot appear, and it includes
the family that looks safe: `window_index` and `chunk_index` are bounded *per file* and
unbounded *across the archive*, which is the scale a Prometheus series lives at.

> **THE CONSEQUENCE, which is easy to miss and must not be "fixed": these metrics can never
> answer "how far along is THIS file".** `phaze_analysis_windows_total{tier}` and
> `phaze_analysis_chunks_total{tier}` are monotonic counters with **no file dimension**, so
> Prometheus yields fleet-level rate and throughput and nothing per-file. That is the direct
> price of the rule above, and it is paid on purpose.
>
> **Adding a `file_id` label is not the fix.** It is the specific thing this budget exists to
> prevent, and `test_no_label_name_looks_like_an_identifier` fails the build on it. **Per-file
> and per-chunk progress lives in TRACES** — see [`traces.md`](traces.md) and
> [`docs/design/0017-telemetry-export-topology.md`](../design/0017-telemetry-export-topology.md)
> §7 — where identity is stored per-occurrence and aged out rather than forever and per-series.

**Model identity is the one unbounded-looking label that is genuinely bounded.** There are
exactly **34** models — 11 characteristic sets × 3 variants, plus the genre model — and
`test_model_label_bound_matches_the_registry` pins that number against
`services/analysis_models.py` rather than trusting a comment. It is also the dimension the
whole epic exists to expose, because `classifier_type` is what `phaze-8ifq8` needs per-model
cost split by.

## 2. Two things that are NOT labels but behave like them

**`service.instance.id` multiplies every series a service emits.** It becomes the Prometheus
`instance` label. The analysis role emits ~1,700 series and runs as a k8s Job whose pod name
is unique per analyzed file, so defaulting this to the hostname would mint a fresh
~1,700-series block for each of 11,428 files. phaze defaults it to the **service name** and
`exporter.md` §3 tells operators to set it to a HOST, never a pod.

**Resource attributes can be promoted to labels by the collector.** The
`resource_to_telemetry_conversion` option on the Prometheus exporter turns every resource
attribute into a metric label; phaze carries `process.pid`, `host.name` and `k8s.pod.name`
on its **trace** resource, so turning that option on would put all three onto every metric.
`deploy/telemetry/otel-collector.example.yaml` sets it explicitly to `false` with that note
attached, so an operator copying it inherits the reasoning.

## 3. The catalogue

Prometheus names below are what the **real** OTLP → Prometheus translation produces (see §5),
not what the naming rules suggest on paper.

| Prometheus family | type | labels (each label's bound) | budgeted combinations | series |
| --- | --- | --- | ---: | ---: |
| `phaze_analysis_run_duration_seconds` | histogram | `outcome` (2) | 2 | 34 |
| `phaze_analysis_tier_duration_seconds` | histogram | `tier` (2) | 2 | 34 |
| `phaze_analysis_chunk_decode_duration_seconds` | histogram | `tier` (2) | 2 | 32 |
| `phaze_analysis_chunk_derive_duration_seconds` | histogram | `tier` (2) | 2 | 32 |
| `phaze_analysis_chunk_peak_rss_bytes` | histogram | `tier` (2) | 2 | 26 |
| `phaze_analysis_fine_window_duration_seconds` | histogram | *none* | 1 | 16 |
| `phaze_analysis_model_inference_duration_seconds` | histogram | `model_name` (12), `model_variant` (4), `classifier_type` (3) | 34 *(not 144)* | 544 |
| `phaze_analysis_model_graph_build_duration_seconds` | histogram | `model_name` (12), `model_variant` (4), `classifier_type` (3) | 34 *(not 144)* | 510 |
| `phaze_analysis_model_graph_release_duration_seconds` | histogram | `model_name` (12), `model_variant` (4), `classifier_type` (3) | 34 *(not 144)* | 510 |
| `phaze_analysis_model_sweep_duration_seconds` | histogram | `model_name` (12), `model_variant` (4), `classifier_type` (3) | 34 *(not 144)* | 544 |
| `phaze_analysis_windows_total` | counter | `tier` (2), `outcome` (2) | 4 | 4 |
| `phaze_analysis_chunks_total` | counter | `tier` (2) | 2 | 2 |
| `phaze_analysis_audio_duration_seconds_total` | counter | `outcome` (2) | 2 | 2 |
| `phaze_http_server_request_duration_seconds` | histogram | `http_method` (8), `http_route` (160), `http_status_class` (6) | 320 *(not 7,680)* | 4,800 |
| `phaze_http_server_active_requests` | updowncounter | *none* | 1 | 1 |
| `phaze_saq_job_duration_seconds` | histogram | `saq_function` (64), `outcome` (2) | 80 *(not 128)* | 1,200 |
| `phaze_saq_jobs_total` | counter | `saq_function` (64), `outcome` (2) | 80 *(not 128)* | 80 |
| `phaze_pipeline_stage_inflight` | gauge | `stage` (64), `status` (2) | 12 *(not 128)* | 12 |
| `phaze_db_statement_duration_seconds` | histogram | `db_operation` (8) | 8 | 120 |
| `phaze_db_statements_total` | counter | `db_operation` (8) | 8 | 8 |
| `phaze_pipeline_stage_transitions_total` | counter | `stage` (64), `transition` (2) | 64 *(not 128)* | 64 |
| `phaze_pipeline_backlog` | gauge | `backlog` (12) | 12 | 12 |
| | | | **total** | **8,587** |

### The arithmetic, where the budget is not the cartesian product

A histogram is **not one series**. It is one `_bucket` series per boundary, plus the implicit
`+Inf` bucket, plus `_sum` and `_count` — `len(buckets) + 3` per label combination. Costing a
histogram as one series is the arithmetic error that makes a budget read as safe when it is
not, and it is why the ladders in §4 are deliberately short.

Three metrics are budgeted below their cartesian product, and each has a reason that is a
fact about this repo rather than an optimistic guess:

- **the four model histograms** — `model_name` × `model_variant` × `classifier_type` is
  12 × 4 × 3 = **144** on paper. The registry declares exactly **34** combinations, and no
  other combination can be constructed: the labels are read off a `ModelConfig` that only
  exists for those 34.
- **`phaze_http_server_request_duration_seconds`** — 8 × 160 × 6 = **7,680** on paper. A
  series exists only for a `(method, route, status_class)` actually observed, and a route
  serves the one or two methods it declares, not eight. Measured against
  `phaze.main.create_app()`: **105 distinct route templates** and **106 distinct
  (method, template) pairs** (POST 51 / GET 42 / PATCH 10 / PUT 2 / DELETE 1). At two status
  classes each that is 212; the budget carries **320**.
- **`phaze_pipeline_stage_transitions_total`** — `stage` × `transition` is 64 × 2 = **128**,
  but a ledger row is written once and cleared once, so both transitions occur for the same
  stage: **64**.
- **`phaze_pipeline_stage_inflight`** — `stage` × `status` is 64 × 2 = **128** on paper, but
  only the stages that route through a SAQ function are ever sampled: **12**.

**The ceiling is a ceiling, not a prediction.** A series is minted only when its label
combination is first observed, and §5's measured figure from a single real analysis is far
below it.

## 4. Histogram buckets

**The SDK default ladder is 5 ms – 10 s.** This workload runs from a sub-millisecond graph
release to a twelve-hour coarse tier, so on the default every observation would land in the
first or the last bucket and every quantile would be a bucket boundary rather than a number.

Five ladders, each sized to a range this repo has measured, and each deliberately **short**
— a histogram costs `len(buckets) + 3` series per label combination, and the three 34-model
instruments pay that thirty-four times over.

| ladder | range | used by | what sized it |
| --- | --- | --- | --- |
| `BUCKETS_MODEL_OP` | 1 ms – 60 s | per-inference, per-graph build/release, fine window | a graph release is sub-millisecond; `phaze-zaf2l` measured 2.379–2.694 s per fine window with a 5.845 s outlier |
| `BUCKETS_CHUNK` | 0.5 s – 30 min | chunk decode, chunk derive, one model's sweep over a chunk | a coarse chunk is 30 windows × 180 s of audio; the tier it belongs to measured 6,741.207 s |
| `BUCKETS_RUN` | 10 s – 24 h | whole tier, whole file | the archive's longest file is **43,466.880 s** (12 h 04 m 27 s) and at 1.4951× costs ~18 h of wall clock, so the ladder must reach past it or the p99 is `+Inf` exactly where it matters |
| `BUCKETS_REQUEST` | 5 ms – 10 s | HTTP handlers, SQL statements | dense across 100 ms – 2 s rather than the default's 5–100 ms, because the admin UI's two heavy partials measure **534.0 ms** and **1,378.6 ms** |
| `BUCKETS_JOB` | 10 ms – 12 h | SAQ jobs | a SAQ job is anything from an 11 ms dequeue to a multi-hour `process_file`, so one ladder spans both rather than splitting the instrument |
| `BUCKETS_RSS` | 0.5 – 8 GiB | per-chunk peak RSS | brackets the **4Gi** pod limit on both sides, so a breach is a bucket crossing rather than something inferred from a mean |

§5 holds the measured distributions these were checked against.

**The trap if you change a ladder.** Bucket boundaries are attached through provider-side
**Views** (`bootstrap._views()`), not through the instrument: the OTel metrics API has no
per-instrument bucket argument, because aggregation is a provider concern. `create_histogram`
does accept an `explicit_bucket_boundaries_advisory`, and phaze deliberately does not use it —
an advisory is a hint a provider may ignore, and having two mechanisms that can disagree is
worse than having one that binds.

## 5. Measured against a real collector

Everything in this section was read off a **live
`otel/opentelemetry-collector-contrib` 0.140.0** fed by a **real 30-minute analysis** — real
essentia, the real 34-graph model set, the real D-07 chunk loop. Recorded output: `measurements/metric-contract-2026-08-26.md`; the run's own report is
`measurements/analysis-run.json`. The raw 588 KB collector exposition is deliberately NOT
committed — it is reproducible in one command and its parsed form is the table below.
Reproduce with:

```bash
docker compose -f docker-compose.telemetry.example.yml up -d
uv run python scripts/measure_metric_contract.py --minutes 30 --models-dir <models>
```

**Why against a real collector and not against the naming rules.** The OTLP → Prometheus
translation is the artifact's real consumer (ADR-0012 rule 3), and reading the rules on
paper would have shipped three defects:

| what the real collector showed | what it would have cost |
| --- | --- |
| a metric label named `job` collides with the `job` Prometheus derives from `service.name`; the collector logs `duplicate label names in constant and variable labels` and **drops the whole metric** | `phaze_saq_job_duration_seconds` and `phaze_saq_jobs_total` would have been **invisible in production**, with nothing at the scrape endpoint to notice |
| `unit="1"` on a **gauge** becomes a `_ratio` **suffix** — `phaze_saq_queue_depth_ratio` — while a counter with the same unit is unaffected | a queue depth named `_ratio`, baked into every dashboard query and alert rule homelab writes |
| the collector's `batch` processor holds a push for up to its timeout; scraping the instant a producer exits misses the **final** export | the first run of the harness reported **2,058** series and **32 of 34** models; the same collector read a minute later held **2,242** and all **34**. For homelab this is real: a short-lived analyze pod's final push is not at the scrape endpoint until the batcher flushes |

A fourth, in `deploy/telemetry/otel-collector.example.yaml`: the Prometheus exporter drops a
series it has not seen updated within `metric_expiration` (**default 5 m**), so an analyze
pod's counters vanish from the endpoint five minutes after it exits. Observed directly: 203
phaze series, then zero, with the collector healthy and the Prometheus target still `up`.

### 5a. Series minted by ONE analysis

A single 30-minute analysis (60 fine windows, 10 coarse windows, 798.25 s wall clock,
34/34 model combinations observed):

| Prometheus family | series |
| --- | ---: |
| `phaze_analysis_model_graph_build_duration_seconds` | 544 |
| `phaze_analysis_model_graph_release_duration_seconds` | 544 |
| `phaze_analysis_model_inference_duration_seconds` | 544 |
| `phaze_analysis_model_sweep_duration_seconds` | 476 |
| `phaze_analysis_tier_duration_seconds` | 30 |
| `phaze_analysis_chunk_decode_duration_seconds` | 28 |
| `phaze_analysis_chunk_peak_rss_bytes` | 26 |
| `phaze_analysis_fine_window_duration_seconds` | 16 |
| `phaze_analysis_run_duration_seconds` | 15 |
| `phaze_analysis_chunk_derive_duration_seconds` | 14 |
| `phaze_analysis_windows_total` | 2 |
| `phaze_analysis_chunks_total` | 2 |
| `phaze_analysis_audio_duration_seconds_total` | 1 |
| **total** | **2,242** |

*(Series counts are from the ladders in force at measurement time; the retuning in §4
changes some of them. The ceiling in §3 is authoritative.)*

**The number that matters for homelab is that this is a CEILING already reached.** 2,242 is
the analysis role's *whole* contribution — the 34 models are all observed within one file,
so the **second** file adds **zero** new series. It does not grow with the archive. That is
the entire point of keeping file identity off the labels: 11,428 files cost the same 2,242
series as one.

Emission rate: those 2,242 series were minted over **798.25 s** and are then refreshed once
per `OTEL_METRIC_EXPORT_INTERVAL` (15 s by default), i.e. **~150 datapoint-updates per
second** at steady state with one analysis running, and four times that at the production
`cap = 4`.

### 5b. The distributions the buckets in §4 were chosen from

Measured on the same run. Cumulative share at each boundary of the ladder **in force at the
time** — these are the observations §4's retuning responds to.

| instrument | n | what the measurement showed |
| --- | ---: | --- |
| graph **build** | 34 | 61.8% ≤ 20 ms, 67.6% ≤ 100 ms, 97.1% ≤ 250 ms, 100% ≤ 500 ms |
| graph **release** | 34 | 79.4% ≤ 20 ms, 97.1% ≤ 50 ms, 100% ≤ 100 ms |
| **inference** (per model, per window) | 340 | 0% ≤ 500 ms, 2.6% ≤ 1 s, **94.7% ≤ 2.5 s**, 100% ≤ 5 s |
| **model sweep** (per model, per chunk) | 34 | 2.9% ≤ 10 s, **100% ≤ 30 s** |
| **fine window** | 60 | 0% ≤ 250 ms, **100% ≤ 500 ms** |
| chunk **decode** | 2 | one ≤ 5 s (fine), one ≤ 60 s (coarse) |
| chunk **derive** | 1 | ≤ 500 ms |
| chunk **peak RSS** | 2 | one in (1.07, 1.61] GiB at the fine boundary, one in (4.29, 6.44] GiB at the coarse boundary |
| **tier** | 2 | one in (60, 300] s (fine), one in (300, 900] s (coarse) |
| **run** | 1 | in (300, 900] s |

**Three of these sat entirely inside a single bucket** — inference, model sweep and fine
window — which means their p95 was a bucket boundary rather than a number. That is exactly
the failure a default ladder produces, reproduced on a ladder that had been reasoned about
rather than measured. §4's ladders are the response: `BUCKETS_GRAPH_OP` and
`BUCKETS_INFERENCE` split apart because the two populations are two orders of magnitude
apart, `BUCKETS_CHUNK` gains 15 / 45 / 90 s, and `BUCKETS_RUN` gains 150 / 600 s.

**What this run does NOT establish.** It is one 30-minute file of synthetic audio on macOS.
Absolute values do not transfer to the Linux burst node (peak RSS here is ~4.7 GiB against a
measured 1.50–1.67 GiB there), and a production coarse chunk holds up to 30 windows rather
than 10, so a sweep there is roughly 3× longer. The ladders are sized with that in mind, and
the first weeks of real telemetry are what will confirm or refute them.

## 6. The guard that fails loudly

`phaze-m1drf.3` acceptance 4 asks for a check that fires if a high-cardinality label is ever
introduced. It has two halves, and it needs both.

**Static** — `tests/shared/telemetry/test_metric_catalogue.py`:

| test | what it refuses |
| --- | --- |
| `test_every_label_states_a_finite_bound` | a label nobody costed |
| `test_no_label_name_looks_like_an_identifier` | `file_id`, `path`, `digest`, `uuid`, `window_index`, `chunk_index`, … |
| `test_no_label_collides_with_a_prometheus_reserved_name` | `job`, `instance`, `le`, `quantile` — see §5 for what this cost before it existed |
| `test_a_dimensionless_gauge_never_uses_the_bare_unit_one` | the `_ratio` suffix — see §5 |
| `test_no_instrument_is_created_outside_the_instruments_module` | a metric with no catalogue entry, and therefore no bound |
| `test_model_label_bound_matches_the_registry` | the 34 drifting away from `MODEL_SETS` |
| `test_the_series_ceiling_is_pinned` | the budget growing a hundred series at a time without anyone deciding to |

**Runtime** — `instruments._checked_attributes`. The static half reads declared label
*names*; it structurally cannot see an attribute assembled from a variable at a call site,
and a value read out of a payload is exactly how a file id becomes a label. Under
`PHAZE_TELEMETRY_STRICT=1` (which the test suite sets) an undeclared attribute raises; in
production it is dropped and logged, because instrumentation may never fail the work.

**And against a real run** —
`test_analysis_instrumentation.py::test_no_analysis_metric_carries_an_identifier` sweeps
everything a REAL essentia analysis actually emitted and asserts no attribute name matches a
forbidden substring and no value contains the file path.

## 7. What is explicitly NOT decided here

**No Prometheus server, retention policy, disk sizing or scrape topology is configured in
this repo.** Those are homelab's. The Prometheus in
`docker-compose.telemetry.example.yml` is a development illustration, keeps 24 hours on an
ephemeral volume so a laptop does not fill up, and says so in its own header. Nothing about
it is a recommendation.

**No local identifiers appear in this document or in any committed configuration.** Every
figure here is a quantity — 11,428 files, 34 models, 105 route templates, 43,466.880 s — and
`tests/shared/telemetry/test_dashboards.py::test_no_local_identifier_in_committed_dashboard_json`
enforces the same on the dashboard JSON.
