# What the instrumentation costs

`phaze-m1drf.1` acceptance 6: *"Instrumentation overhead measured on a real multi-hour file
and stated as a percentage of wall clock. Peak RSS must not grow with duration. If overhead
cannot be shown negligible, that is the finding."*

**The finding: the overhead is below this machine's run-to-run noise floor.** Across three
duration pairs the instrumented arm was **−4.15%**, **+1.80%** and **−3.73%** against the
uninstrumented one — mean **−2.03%**, standard deviation **3.32 percentage points**. Two of
three are *negative*. An instrumented run cannot actually be faster than an uninstrumented
one, so what those numbers measure is the variance, not the cost.

**What was NOT measured, stated plainly:** no multi-hour file. The longest arm is 60
minutes. §4 gives the operation-count arithmetic for why the percentage is
duration-independent, and §5 records the narrowing explicitly rather than quietly redefining
"multi-hour".

______________________________________________________________________

## 1. Method

`scripts/telemetry_overhead.py`, run 2026-08-26 on the local MacBookPro18,1 (macOS,
10 cores, 32 GiB), against the **real** `analyze_file` — real essentia, the real 34-graph
model set, the real D-07 chunk loop.

- **Each arm is its own process**, so one arm's peak-RSS high-water mark cannot contaminate
  the next.
- **`wall_sec` brackets `analyze_file` only.** The final flush is timed separately, so a
  change to shutdown behaviour cannot move the overhead figure.
- **Two arms.** `off` has no OTLP endpoint, so no SDK provider is installed at all. `blackhole`
  points at RFC 5737 TEST-NET-1 (`192.0.2.1:4318`) — reserved for documentation, not routed —
  so the SDK is fully installed, every span and metric is created and serialized, and every
  export attempt hangs and times out. That is the **worst** case: all of the instrumentation
  cost, none of the export succeeding, plus the exporter's own retry work.
- **Audio is synthetic** (two summed sines, the same shape
  `test_analysis_streaming_decode.py` uses). What is being measured is a RATIO between two
  arms decoding the same bytes through the same code, so the content cancels. It also keeps
  operator media out of a measurement whose output is committed.

## 2. The measurement

| minutes | arm | wall (s) | ratio to duration | peak RSS (GiB) | flush (s) | vs `off` |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 10 | off | 269.19 | 0.4487× | 4.5142 | 0.000 | — |
| 10 | blackhole | 258.04 | 0.4301× | 4.8656 | 40.278 † | **−4.15%** |
| 30 | off | 782.23 | 0.4346× | 4.6217 | 0.000 | — |
| 30 | blackhole | 796.32 | 0.4424× | 4.4798 | 4.004 | **+1.80%** |
| 60 | off | 1617.65 | 0.4493× | 4.7280 | 0.000 | — |
| 60 | blackhole | 1557.26 | 0.4326× | 4.9988 | 4.008 | **−3.73%** |

Coverage was exhaustive and identical in every arm: 20/20, 60/60 and 120/120 fine windows
and 4/4, 10/10 and 20/20 coarse windows.

† **The 10-minute flush figure is from superseded code and is not comparable to the other
two.** It is what exposed the defect: the shutdown budget was bounding only `force_flush`,
while `TracerProvider.shutdown()` takes no timeout at all and `MeterProvider.shutdown()`
defaults to 30,000 ms. Teardown now runs in a joined daemon thread and is abandoned at the
deadline — the 4.004 s and 4.008 s rows are that fix, against a 3,000 ms budget plus 1,000 ms
of slack. Raw data: `measurements/overhead-2026-08-26.json`.

## 3. Peak RSS does not grow with duration, and telemetry does not move it

Across all six runs peak RSS spans **4.4798 – 4.9988 GiB**, a spread of **0.5189 GiB
(11.0%)**, with **no ordering by duration and no ordering by arm**. The lowest peak of all
six is an *instrumented* 30-minute run; the highest is an *instrumented* 60-minute one. Six
values from a 6× duration range that interleave like that are one population, not a trend.

For contrast, the shape this is watching for: `phaze-b2qs9` measured **+0.31 GiB per fine
chunk** with **R² 0.99959** — an unmistakable straight line, reaching 10.28 GiB at 12 hours.
Nothing here resembles that.

> **These absolute figures are macOS and are NOT the production numbers.** The measured
> post-D-09 peak on the Linux burst node is **1.50 / 1.65 / 1.67 GiB** at 1:00 / 4:00 / 12:04
> (`docs/spikes/phaze-u1n7j-vox-fix-verification.md`). Thread sizing and the allocator differ;
> ~4.7 GiB here is not a breach of the 4Gi pod limit, because this is not running under that
> pod. Only the RATIO between arms and the SLOPE across durations transfer.

## 4. Why the percentage is duration-independent

The instrumentation cost is a **fixed cost per operation**, and the operation counts scale
with the same window counts the analysis wall clock does. Both sides of the ratio scale
together, so the percentage does not move with duration. The counts are not estimated — they
are exactly computable from the windowing constants:

| operation | count for a file of `d` seconds | 60 min | 12 h 04 m (the archive's longest) |
| --- | --- | ---: | ---: |
| fine windows | `ceil(d / 30)` | 120 | 1,449 |
| coarse windows | `ceil(d / 180)` | 20 | 242 |
| fine chunks | `ceil(fine / 60)` | 2 | 25 |
| coarse chunks | `ceil(coarse / 30)` | 1 | 9 |
| model sweeps (spans) | `34 × coarse chunks` | 34 | 306 |
| **inference observations** | `34 × coarse windows` | **680** | **8,228** |
| **total spans** | tiers + chunks + decodes + derives + sweeps | **~45** | **~360** |

The heaviest term is the per-inference histogram observation at `34 × coarse windows`, and
`coarse windows` is exactly what the coarse tier's own runtime is proportional to. **Spans are
the term that could have scaled badly and deliberately does not**: there is no span per
window and no span per model-window pair. A 12-hour file emits roughly 360 spans, comfortably
inside the 2,048-span bounded queue, so nothing is dropped for queue overflow at steady state
and nothing grows without bound.

## 5. What this does not discharge

- **No multi-hour file was measured.** The longest arm is 60 minutes. §4 is an argument from
  exact operation counts, not a measurement, and it is labelled as such. A 120-minute pair
  costs roughly 3 hours of wall clock on this machine and was not spent.
- **Synthetic audio, not real music.** The ratio between arms is content-independent, but the
  absolute ratio to duration (0.4301–0.4493× here) is a property of this machine running
  solo, not of production. Production is **1.4951×** at `cap = 4` concurrent pods
  (`docs/spikes/phaze-zaf2l-where-phaze-spends-time.md` §3a).
- **macOS, not the Linux burst node.** See the note in §3.
- **The `off` arm is the true zero.** With no endpoint configured no SDK provider exists, so
  every `get_tracer` / `get_meter` call site resolves to the API's no-op. What remains at each
  call site is a `perf_counter` pair, a dict build and a no-op context manager — that residual
  is inside the `off` arm's own numbers and is not separately isolated here.
