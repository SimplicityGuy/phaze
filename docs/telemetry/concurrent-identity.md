# Concurrent analysis producers at a real collector — 2026-09-15

Measured evidence for `phaze-21nnf`, taken on **2026-09-15**. It lives here rather than under
`measurements/` because that directory is a CLOSED historical corpus -- its population is
pinned to one revision by `scripts/audit_historical_evidence.py`, which tolerates renames and
no additions, so a new record there fails the build. The shipped behaviour is
[`metric-catalogue.md`](metric-catalogue.md) and
[`exporter.md`](exporter.md); the decision is
[`docs/design/0017-telemetry-export-topology.md`](../design/0017-telemetry-export-topology.md) §8.

## 1. The question

Up to `worker_process_pool_size` analysis children (default **4**) run at once, each its own
OS process calling `configure_telemetry("analysis")` and exporting its own **cumulative**
counters. Until this measurement they all reported the same `service.instance.id`.

> **What does a real collector expose when two producers write the same series at different
> running totals?**

Not answerable in process. Each child's own `InMemoryMetricReader` shows a perfectly
monotonic counter — the corruption happens in the collector's accumulator, so only the
collector can be asked. ADR-0012 (verification fidelity and operator attribution) rule 3.

## 2. Environment

| | |
| --- | --- |
| collector | `otel/opentelemetry-collector-contrib:0.140.0` |
| config | `deploy/telemetry/otel-collector.example.yaml`, **byte-identical** (sha256 `bfa40a69…52195`) |
| harness | `scripts/measure_concurrent_identity.py` |
| SDK | opentelemetry-sdk 1.44.0, OTLP **HTTP/protobuf** |
| host | macOS arm64, Colima VM (Ubuntu 24.04, 2 vCPU / 8 GiB) |

> **Docker-on-Mac note, because it cost a false start.** Colima shares **only `$HOME`**. A
> bind mount of a path outside it does not fail — it creates an empty **directory** in the
> VM, and the collector exits with `read /etc/otelcol/config.yaml: is a directory`. The
> config was copied under `$HOME` and its sha256 compared before every run; a measurement
> against a config you retyped is a measurement of something else.

Two producer processes increment the real catalogued counter `phaze.analysis.windows`
(`tier="fine"`, `outcome="analyzed"`) by **10** and **100** respectively, forcing an export
at each step on schedules offset by half a period. Flush spacing is **16 s** — deliberately
several times the config's `batch: timeout: 5s`. At 4 s spacing the batch processor
coalesces the two producers' points and **the dips disappear from the exposition**, so the
defect presents intermittently; that is a property of the batcher, not a refutation.

## 3. Arm A — one shared identity (the behaviour before this bead)

Producers flush at totals 10/20/30 and 100/200/300. The collector exposes **one** series,
`instance="phaze-analysis"`:

```
  t=  1.01  phaze-analysis=10
  t=  5.13  phaze-analysis=10
  t=  9.23  phaze-analysis=10
  t= 13.34  phaze-analysis=100
  t= 17.48  phaze-analysis=100
  t= 21.48  phaze-analysis=20     <-- DECREASE
  t= 25.61  phaze-analysis=20
  t= 29.72  phaze-analysis=200
  t= 33.73  phaze-analysis=200
  t= 37.87  phaze-analysis=30     <-- DECREASE
  t= 41.94  phaze-analysis=300
  t= 45.94  phaze-analysis=300
  t= 49.96  phaze-analysis=300
  t= 53.98  phaze-analysis=300
  t= 57.99  phaze-analysis=300
```

**Last write wins, and the series is not monotonic.** Prometheus reads each decrease as a
counter reset and counts the post-reset value as increment.

| | measured |
| --- | ---: |
| every exposed series monotonic | **False** |
| `sum(increase())` over the window | **590** |
| what the producers delivered in that window | 320 |
| error | **+84.4%** |
| final exposed total | 300 |
| true delivered total | 330 |

**The over-count grows with the number of interleaved exports**, which is what makes it a
production problem rather than a curiosity. The same arm at **6** flushes per producer:
`sum(increase())` = **2,090** against a window truth of **650** — **+221.5%**.

## 4. Arm B — a bounded worker slot per producer (CHOSEN)

Identical schedule; each producer carries `PHAZE_TELEMETRY_SLOT=0` / `=1`, so the identities
are `phaze-analysis-0` and `phaze-analysis-1`. The collector exposes **two** series:

```
  t=  1.05  phaze-analysis-0=10
  t=  5.06  phaze-analysis-0=10
  t=  9.09  phaze-analysis-0=10
  t= 13.19  phaze-analysis-0=10, phaze-analysis-1=100
  t= 17.21  phaze-analysis-0=10, phaze-analysis-1=100
  t= 21.35  phaze-analysis-0=20, phaze-analysis-1=100
  t= 25.47  phaze-analysis-0=20, phaze-analysis-1=100
  t= 29.59  phaze-analysis-0=20, phaze-analysis-1=200
  t= 33.73  phaze-analysis-0=20, phaze-analysis-1=200
  t= 37.88  phaze-analysis-0=30, phaze-analysis-1=200
  t= 41.93  phaze-analysis-0=30, phaze-analysis-1=300
  t= 46.06  phaze-analysis-0=30, phaze-analysis-1=300
  t= 50.07  phaze-analysis-0=30, phaze-analysis-1=300
  t= 54.11  phaze-analysis-0=30, phaze-analysis-1=300
```

| | measured |
| --- | ---: |
| every exposed series monotonic | **True** |
| final exposed total, summed across series | **330** |
| true delivered total | **330** — exact, both at 3 flushes and at 6 (660) |

**No dashboard or alert change is needed**, and that was checked rather than assumed: every
expression in `dashboards/*.json` and `alerts/phaze-alerts.yml` already wraps its selector in
`sum(...)` or `sum by (<label>)(...)`. Summing **after** `rate` is the canonical Prometheus
treatment of one series per producer.

### The one figure that needs its caveat stated

`sum(increase())` on this arm reads **220** against a window truth of 320 — **−31.2%**, and
that shortfall is **not** a property of the scheme. Prometheus never counts a series' *first*
sample as increment, because it cannot know the counter started at zero, and
`phaze-analysis-1` first appears at `t=13.19` already at 100. The amount uncounted is fixed
at one export, so it shrinks as the window grows, while the shared arm's error grows. Both
halves were measured rather than argued, at 6 flushes per producer:

| arm | 3 flushes | 6 flushes |
| --- | ---: | ---: |
| shared identity | +84.4% | **+221.5%** |
| worker slots | −31.2% | **−15.4%** |

Over a real analysis — hours, one export every 15 s — the uncounted first export is
negligible. Over the same real analysis the merge's error is unbounded.

## 5. Arm C — delta temporality under one identity (REJECTED, refuted here)

`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta`, identities left shared. The
appeal was real: deltas from several producers *summed* by the collector would need no extra
identity and no cardinality at all.

**The stock `prometheus` exporter does not accumulate them.** It exposes the last delta it
received, so the series oscillates between the two producers' per-flush increments and the
running total appears nowhere:

```
  t=  1.02  phaze-analysis=10
  t=  5.04  phaze-analysis=10
  t=  9.10  phaze-analysis=10
  t= 13.25  phaze-analysis=100
  t= 17.40  phaze-analysis=100
  t= 21.49  phaze-analysis=10
  t= 25.60  phaze-analysis=10
  t= 29.72  phaze-analysis=100
  t= 33.73  phaze-analysis=100
  t= 37.76  phaze-analysis=10
  t= 41.83  phaze-analysis=100
  t= 45.84  phaze-analysis=100
  t= 49.94  phaze-analysis=100
  t= 54.03  phaze-analysis=100
```

| | measured |
| --- | ---: |
| every exposed series monotonic | **False** |
| final exposed value | **100** |
| true delivered total | 330 — **−69.7%** |

No collector error was logged. Making this arm work needs the `deltatocumulative` processor
in the collector pipeline — **configuration phaze does not own**, whose absence fails exactly
this silently. That is the same objection ADR-0017 (telemetry export topology) §3b raised against the pushgateway, so it
cannot be answered by adding a processor to the example compose: homelab's collector is the
one that matters and this repo cannot constrain it.

## 6. What this does and does not establish

**Does.** That the shared identity corrupts the exposition at a real collector; by how much,
and that the amount grows with concurrency-time; that a bounded per-slot identity fixes it
exactly; and that delta temporality does not fix it with a stock `prometheus` exporter.

**Does not.** Anything about **where a slot comes from**. These are two processes on one host,
which is the host lane's shape: its slots come from a pool in the one process that bounds the
concurrency. A burst-lane child runs in a one-shot Kueue pod that is Postgres-less and shares no
memory with its peers, so its slot has to be injected by the controller at submit time — not built
here.

## 7. The burst lane, and why it needed no re-measurement (`phaze-w15ju`, 2026-09-16)

**When this record was written, concurrent burst pods each took slot 0 from their own pod-local pool
and shared `phaze-analysis-0` — the same merge as §3 under a new name.** `phaze-w15ju` closed that:
the controller allocates a slot per in-flight burst Job against the `cloud_job` rows, persists it on
`cloud_job.telemetry_slot`, and code-injects `PHAZE_TELEMETRY_SLOT` / `PHAZE_TELEMETRY_SLOT_MAX` into
the Job env. The bound is the sum of the kueue backends' `cap` values.
[ADR-0017 §8d](../design/0017-telemetry-export-topology.md) has the mechanism and the release
property.

**The arms above were NOT re-run for it, and that is a claim about what they measure rather than a
shortcut.** §3 and §4 characterise the **collector**: what its Prometheus exporter does with one
identity versus several, at a real version with this repo's own example configuration. That result is
a property of the collector and is unchanged by which process allocated the index — arm B's per-slot
exposition is what a burst pod's slot produces too, because the collector cannot tell the difference.
What `phaze-w15ju` had to establish is different and is not a collector question: that two concurrent
burst **submissions** reach their pods with different bounded slots. That is a property of phaze's own
code, and it is asserted against a real Postgres and through the pod's own
`assign` → `_instance_id` chain by
`test_two_concurrent_burst_submissions_reach_the_child_with_distinct_identities` —
**not** against the manifest alone, because a correct manifest whose slot the pod then overwrote with
its own local 0 is exactly the failure that design invited.

**One thing here still stands unfixed** and is recorded rather than implied: the two lanes share one
slot space and one default base, so a host-lane child and a burst pod on the same index still merge
when `PHAZE_TELEMETRY_INSTANCE` is unset. Bead `phaze-7nl67`.
