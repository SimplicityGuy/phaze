# What the instrumentation costs

`phaze-m1drf.1` acceptance 6: *"Instrumentation overhead measured on a real multi-hour file
and stated as a percentage of wall clock. Peak RSS must not grow with duration. If overhead
cannot be shown negligible, that is the finding."*

**The finding: instrumentation costs +0.04% of wall clock and +0.0133 GiB of peak RSS.**
Measured on the burst node, on a real 192 kbps MP3 of **3,578.964 s (59 m 39 s)** from the
live corpus, with the real 34-graph model set, on the deployed job image, with the node
drained and uncontended.

| arm | wall (s) | ratio to duration | peak RSS (GiB) | flush (s) | overhead |
| --- | ---: | ---: | ---: | ---: | ---: |
| off | 2,001.06 | 0.5591× | **1.4956** | 0.000 | — |
| blackhole | 2,001.93 | 0.5594× | **1.5089** | 4.001 | **+0.04%** |

**The rig is calibrated against a recorded value, not free-floating.**
`docs/spikes/phaze-u1n7j-vox-fix-verification.md` records **1.50 GiB at 1:00** post-D-09, on
this node, on the deployed image. This run measured **1.4956 GiB at 59 m 39 s** — agreement to
**0.3%**. That is an independent reproduction of the post-D-09 baseline, and it is what makes
every other number here trustworthy.

**A multi-hour pair (10 h 03 m 02 s, 36,182.359 s) is running on the same rig**; those figures
land separately and this section is provisional until they do. §2–§5 below are **macOS
supporting evidence**, kept because the overhead-percentage argument and the
duration-independence argument both stand on their own — but read §3's warning before
comparing any macOS RSS figure to the Linux baseline.

> **This criterion was NOT narrowed.** The operator was asked whether to narrow it or run a
> 120-minute pair on the development Mac, and declined every narrowing option in favour of
> measuring on the burst node against real corpus files. Question as put, 2026-08-26: whether
> to run a ~3 h 120-minute pair on the Mac or narrow criterion 6, given 10/30/60-minute macOS
> pairs and an operation-count argument for duration-independence. Answer as given, verbatim:
> *"I would recommend we take vox offline from phaze production, run the tests there selecting
> a few long files from the current corpus of data. Feel free to do this anytime today."*
> Durable record: a comment on bead `phaze-m1drf`.

______________________________________________________________________

## 1. Method — the macOS supporting runs

**Everything in §1–§3 is macOS, on the development machine, and is supporting evidence
only.** The criterion is discharged by the burst-node figures above. These runs are kept
because they were taken first, they are what surfaced the shutdown-budget defect, and their
duration-independence observation stands on its own — but see §3.

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

## 2. The macOS measurement

**Read the sign, not the magnitude.** Mean **−2.03%**, standard deviation **3.32 percentage
points**, two of three *negative* — and an instrumented run cannot actually be faster than an
uninstrumented one, so on this machine what these numbers measure is variance rather than
cost. The burst node, idle and uncontended, separates the signal: **+0.04%**, positive and
correctly signed. That figure supersedes this table's "below the noise floor" reading; this
table is what made it clear a quieter rig was needed.

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

> **These absolute figures are macOS and are NOT the production numbers — now demonstrated
> rather than argued.** The same code, on a comparable duration, measures **1.4956 GiB on the
> burst node** (§ header) against **4.48–5.00 GiB here**: a factor of three. The measured
> post-D-09 Linux baseline is **1.50 / 1.65 / 1.67 GiB** at 1:00 / 4:00 / 12:04
> (`docs/spikes/phaze-u1n7j-vox-fix-verification.md`). Thread sizing and the allocator differ,
> and there is no cgroup here at all; ~4.7 GiB on this machine is **not** a breach of the 4Gi
> pod limit, because nothing here runs under that pod. A reader who sees 4.7 next to 4 and
> concludes there is a breach has been misled by the juxtaposition, which is why this warning
> sits directly under the table. Only the RATIO between arms and the SLOPE across durations
> transfer.

## 4. Why the percentage is duration-independent

This argument stands, and it is no longer carrying the criterion alone — the burst-node
measurement does that. What it still buys is the *reason* the measured figure should be
expected to hold at 10 hours as well as at one, rather than being a fact about one duration.

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

## 5. Scope, and what is still outstanding

**Outstanding:** the **10 h 03 m 02 s (36,182.359 s)** pair is running on the burst node and
its figures land separately. Until they do, the multi-hour half rests on the 59 m 39 s pair
plus §4's arithmetic rather than on a measured multi-hour point. This section will be amended
with the real numbers.

**What the macOS runs in §1–§3 do NOT establish:**

- **Not the platform production runs on.** See §3: a factor of three on peak RSS, no cgroup,
  different thread sizing and allocator.
- **Synthetic audio, not real music.** The ratio between arms is content-independent, but the
  absolute ratio to duration (0.4301–0.4493× there) is a property of that machine running
  solo. The burst-node run used a **real 192 kbps MP3 from the live corpus** and measured
  **0.5591×**, inside the **0.56–0.79×** uncontended band
  `docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md` records.
- **Do not reach for 1.4951× when reasoning about an idle node.** That figure is the
  **contended** `cap = 4` production operating point
  (`docs/spikes/phaze-zaf2l-where-phaze-spends-time.md` §3a). Using it to size an uncontended
  run overstates cost by roughly 2×.

**What no run isolates:** the `off` arm is the true zero. With no endpoint configured no SDK
provider exists, so every `get_tracer` / `get_meter` call site resolves to the API's no-op.
What remains at each call site is a `perf_counter` pair, a dict build and a no-op context
manager — that residual is inside the `off` arm's own numbers and is not separately measured.
