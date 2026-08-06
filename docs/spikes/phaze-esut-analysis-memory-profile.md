# phaze-esut — where `process_file`'s memory actually goes

- **Bead:** `phaze-esut` (spike — "process_file peaks at 30.2 GB RSS against an 8 GiB request")
- **Date:** 2026-08-04
- **Tree:** branch `wt/bead/issue/phaze-esut`, forked off `main` at `9dc2142`
- **Code under test:** phaze `2026.8.0` — the version running in the affected deployment
- **Status:** spike. Measurement and verdict only. **No product code changed.**

______________________________________________________________________

## Verdict in one paragraph

**The peak is not audio.** It is a duration-independent TensorFlow floor of **8.5–10.5 GiB**
paid by every single file, created almost entirely inside one call — the first
`_run_model_sets` of the coarse pass — and dominated by the **34 `TensorflowPredict*` graphs
that `_get_classifier` holds resident simultaneously while `_run_model_sets` uses them strictly
one at a time**. A **3.3-minute** synthetic file peaks at **9.73 GiB**; a **12.1-hour** file
peaks at **9.03 GiB**. The decoded signal never exists: essentia's windowed `EasyLoader` is
genuinely streaming, and decoding the last 180-second window of a 12.1-hour file costs
**0.251 GiB**, of which 0.248 GiB is the `import essentia` line. The two-decoded-copies
hypothesis in the bead note is **refuted by a pre-registered discriminating test** (predicted
39.2 GiB, measured 9.03 GiB — 77% error). The consequence that matters operationally: this is
**not 45 long-file landmines out of 6,753 — it is all 6,753**, and the 8Gi request is below the
floor for a three-minute pop song.

______________________________________________________________________

## 1. Method, and what it can and cannot support

| | |
| --- | --- |
| **Host** | macOS 26.5.2, Apple Silicon arm64, **10 cores, 32.0 GiB RAM** (deliberately close to the 31 GB burst node) |
| **essentia** | `essentia-tensorflow` 2.1-beta6-dev — the wheel pinned in `pyproject.toml` |
| **Models** | full production set: 11 sets × 3 variants + `discogs-effnet-bs64-1` = **34 graphs, 3.0 GB of `.pb`** |
| **Audio** | **synthesized with ffmpeg** — stereo 44.1 kHz sine pairs, libmp3lame 192 kbps, durations 200 s / 600 s / 3600 s / 10800 s / 43200 s |
| **Peak metric** | `resource.getrusage(RUSAGE_SELF).ru_maxrss` — on Darwin this is **bytes**, and it is the kernel's true peak-RSS high-water mark |
| **Instantaneous RSS** | `ps -o rss=` |

**No operator media was read, copied, or referenced.** Every input is a synthesized sine tone.
No filename, path, or per-file metadata value from the library appears in this document.

**On tooling.** `memray` is not installed here, and it is not needed for the question asked:
`ru_maxrss` is a kernel-level high-water mark that already accounts for native (C/C++)
allocations — precisely the allocations `tracemalloc` would have missed. **No number in this
document is a `tracemalloc` number.** Because `ru_maxrss` is monotonic, the *delta* of the
high-water between two marks is exactly the new peak attributable to the stage between them,
which is what makes the per-stage attribution below sound rather than inferred.

**The load-bearing limitation, stated up front:** all measurements *in this document* are
macOS/arm64. The production nodes are Linux/glibc. §7 gave an inference about that gap and scoped
a bead to confirm it. **That bead ran** — `phaze-7i0k`, 2026-08-05,
[`phaze-7i0k-linux-memory-measurement.md`](phaze-7i0k-linux-memory-measurement.md) — and found
Linux **cheaper** than this host by 25–39%, with **no ratchet** — §7's allocator inference is
retracted. Every number below is macOS; read §7 for the Linux equivalents before sizing anything.
The verdict is unchanged either way, because duration-independence is a property of the code path,
not of the allocator — and it reproduced across the same 216× duration span.

**On units:** `ru_maxrss` here is **bytes** (Darwin). On Linux the same field is **KiB**.
Comparing the two without that conversion produces a 1024× error.

______________________________________________________________________

## 2. Peak RSS attributed to pipeline stages

Full `analyze_file` pipeline, production caps (`fine_cap=60`, `coarse_cap=30`), on the
3.3-minute file. `Δ` is the high-water delta — the new peak the stage itself created.

| stage | RSS (GiB) | peak (GiB) | Δ (GiB) |
| --- | ---: | ---: | ---: |
| baseline python | 0.019 | 0.019 | — |
| `import essentia` + TensorFlow | 0.247 | 0.247 | **+0.228** |
| `_probe_duration_sec` (MetadataReader) | 0.247 | 0.247 | +0.000 |
| **FINE pass**, all 7 windows (`EasyLoader` → `RhythmExtractor2013` → `KeyExtractor`) | 0.287 | 0.287 | **+0.039** |
| coarse `EasyLoader`, 180 s @ 16 kHz | 0.300 | 0.300 | +0.014 |
| **`_run_model_sets`, window 0** | 5.288 | **9.734** | **+9.433** |
| `_run_model_sets`, window 1 | 7.635 | 9.734 | +0.000 |
| build window payloads (9) | 7.635 | 9.734 | +0.000 |
| JSON-serialize the result | 7.635 | 9.734 | +0.000 |

**One stage owns 96.9% of the peak.** Audio decode across both tiers contributes 0.053 GiB
(0.5%). Aggregation, payload construction and serialization contribute nothing measurable.

### 2a. Inside that one stage

Walking the 34 models in production order on a single 180 s @ 16 kHz buffer, lazily loaded
exactly as `_get_classifier` does it:

| step | RSS after (GiB) | peak after (GiB) | Δ (GiB) |
| --- | ---: | ---: | ---: |
| all 34 graphs constructed, **zero inference** | **4.338** | 4.338 | **+4.090** (2.0 s wall) |
| first musicnn inference (`mood_acoustic-musicnn-msd-2`) | 2.622 | 2.622 | +2.359 |
| first vggish inference (`mood_acoustic-vggish-audioset-1`) | 3.940 | 5.574 | +2.898 |
| remaining 31 models | 1.367 | 7.624 | +1.9 combined, none > +0.47 |

**No single model is the culprit.** The floor is the *sum* of 34 co-resident TF sessions
(+4.09 GiB of graph residency) plus the per-architecture inference arenas that the first
musicnn and first vggish each stand up (+5.3 GiB combined). `discogs-effnet-bs64-1`, the
obvious suspect on name alone, adds **+0.000** on top of what is already standing.

______________________________________________________________________

## 3. The predictor — quantified, and the decoded-copy hypothesis falsified

### 3a. The full-duration sweep

| file | duration | one decoded copy<br>(44.1 kHz × 2 ch × f32) | fine<br>analyzed/natural | coarse<br>analyzed/natural | **peak RSS** | peak ÷ one<br>decoded copy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `dur_200` | 3.3 min | 0.066 GiB | 7 / 7 | 2 / 2 | **9.734 GiB** | **148.1×** |
| `dur_600` | 10.0 min | 0.197 GiB | 20 / 20 | 4 / 4 | **9.165 GiB** | **46.5×** |
| `dur_3600` | 60.0 min | 1.183 GiB | 60 / 120 | 20 / 20 | **11.739 GiB** | **9.9×** |
| `dur_43200` | **720.0 min** | **14.194 GiB** | 2 / 1440 | 2 / 240 | **9.033 GiB** | **0.64×** |

A 216× increase in duration — and in decoded size — moves the peak by less than 30%, in no
consistent direction. The 3.3-minute file peaks **higher** than the 12-hour file.

### 3b. The pre-registered discriminating test

The first three rows cannot by themselves separate "duration" from "window count": in a normal
file the two are perfectly confounded. So two models were fitted on those three points **and a
prediction was committed before the fourth run**:

| model | fit | R² |
| --- | --- | ---: |
| **A — decoded copies** | `peak = 9.199 + 2.102 × decoded_copies` | 0.901 |
| **B — coarse window count** | `peak = 9.083 + 0.130 × n_coarse` | 0.905 |

Indistinguishable, as expected. The fourth run then broke the confound directly: the
**12.1-hour** file (14.194 GiB per decoded copy) run with the caps set to **2** coarse windows.

| | predicted | measured | error |
| --- | ---: | ---: | ---: |
| **Model A (decoded copies)** | **39.2 GiB** | 9.033 GiB | **−30.2 GiB (−77%)** — **REFUTED** |
| **Model B (window count)** | **9.38 GiB** | 9.033 GiB | **−0.35 GiB (−3.7%)** — **CONFIRMED** |

### 3c. Window count isolated at constant duration

The same 600-second file, run through 1…30 production-shaped 180 s coarse inferences, holding
file duration fixed:

| n coarse windows | 1 | 2 | 3 | 4 | 10 | 16 | 20 | **30** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| peak (GiB) | **8.55** | 9.53 | 9.80 | 10.00 | 10.00 | 10.15 | 10.15 | **10.39** |

**One coarse window already peaks at 8.55 GiB — above the 8 GiB request.** Going from 1 to 30
windows — the entire operating range the cap permits — adds only **+1.84 GiB (21%)**, and the
curve is logarithmic, not linear. Instantaneous RSS oscillated between **3.256 and 8.205 GiB**
throughout (relevant in §7).

### 3d. The recommended predictor

```
peak_GiB  ≈  8.5  +  0.41 × ln( min( ceil(duration_sec / coarse_window_sec), coarse_cap ) )
```

Residual across all four full-pipeline runs: **max |error| 1.4 GiB (12%), mean |error| 0.7 GiB**.
Range over every possible input: **8.5 – 10.5 GiB**, saturating at the coarse cap.

**Stated as the acceptance criterion demands — as a multiple of one decoded-signal copy
(`sample_rate × channels × duration`):**

> **The coefficient on one decoded copy is 0.00 ± 0.05.**

The apparent "multiple" ranges from **148× down to 0.64×** across a 216× duration span. That is
the signature of a constant, not of a multiple. The acceptance criterion rightly rejects "it
depends on the file"; the measured answer is stronger and simpler — **nothing about the file
predicts peak memory. The model set does.**

______________________________________________________________________

## 4. Decode is not merely small — it is absent

Isolating `es.EasyLoader(filename, sampleRate, startTime, endTime)`, which is the only path by
which audio enters memory:

| window | file duration | peak RSS |
| --- | ---: | ---: |
| last 30 s @ 44.1 kHz | 10 min | 0.248 GiB |
| last 30 s @ 44.1 kHz | 60 min | 0.256 GiB |
| **last 180 s @ 16 kHz** | **720 min** | **0.251 GiB** |

0.248 GiB of every one of those is the `import essentia` line measured before the decode. The
decode itself costs **~3–12 MB**, flat, at every duration.

essentia's standard `EasyLoader` wraps a *streaming* `MonoLoader → Trimmer → Scale` network, so
the whole-signal buffer that the "two decoded copies" arithmetic assumes **is never
materialized at any point, at any duration.** The comment in `analyze_file` —

> *"Instead of decoding the whole file into one buffer (the latent OOM) … it decodes one short
> window at a time … so no essentia algorithm ever sees more than one window."*

— is **accurate**. The windowing change did what it claimed. It simply addressed a different
problem from the one now killing nodes.

______________________________________________________________________

## 5. Inherent or avoidable? — **Avoidable, and specifically ~4.1 GiB of it**

**Evidence for avoidable retention.**

1. `_get_classifier` (`services/analysis.py:146`) stores every `TensorflowPredict*` instance in
   a module-level `_classifier_cache` that is **never evicted** for the process's lifetime.
1. `_run_model_sets` (`services/analysis.py:469`) consumes them **strictly sequentially** —
   `for model_set in MODEL_SETS: for model in model_set.models: _predict_single(...)`. **At
   most one graph is in use at any instant.** All 34 are resident.
1. Measured cost of that residency, isolated: **+4.090 GiB, standing up in 2.0 s of wall time**,
   with zero inference performed.

**This is not a bug.** The cache is a deliberate, documented time optimization — *"Reuses the
module-level `_classifier_cache` (inference-only; no per-window graph reload)"*. It is a
time/memory trade that was **priced against wall-clock and never against a memory bound**. The
bound is now the binding constraint, so the trade is due for repricing.

**The concrete restructure** (scoped as follow-up A, deliberately not implemented here): invert
the loop nesting so **models are the outer loop and windows the inner one**. Every model is
still constructed exactly once per file — identical load cost, no per-window reload, so the
optimization the cache exists to provide is fully preserved — but only one is resident at a
time. The price is holding the ≤30 decoded coarse buffers concurrently:

```
30 windows × 180 s × 16 000 Hz × 4 B  =  345 MB
```

Estimated peak after the change: import (0.25) + one architecture family (2.4–2.9) + buffers
(0.35) ≈ **3–4 GiB**, a 2.5–3× reduction. **That estimate must be measured by the follow-up, not
assumed** — the remaining ~4.5 GiB of per-inference arena is closer to inherent to running
musicnn + vggish + effnet under TF's C++ allocator, and §7 is a reason to expect Linux to
behave differently from this host.

**Verdict:** roughly **40% of the floor is avoidable retention** with no loss of work and no
extra model loads; the remainder is closer to inherent, pending the Linux measurement.

> **Measured on Linux (`phaze-7i0k`).** Graph residency costs **+3.995 GiB** there against
> +4.090 GiB here — the avoidable term is platform-independent to within 2.3%. Because Linux's
> per-inference arena is 6× smaller (0.87 GiB against 5.26 GiB), that same 4 GiB is **50% of the
> 7.99 GiB Linux envelope maximum**, not 40%. The 3–4 GiB post-change estimate survives, landing
> at ≈3.5–4.5 GiB — or ≈3.0–3.9 GiB combined with the 4-thread intra-op cap.
>
> **Shipped and re-measured (`phaze-15sw`, 2026-08-05).** Model-major iteration landed and the
> envelope maximum went **7.986 → 2.482 GiB (−69%, 3.2×)** on the same node, same image, same
> model set — **better than both estimates**, and better than the 50% this note predicted. The
> full-pipeline sweep lands at **2.074 / 2.141 / 2.489 GiB** for 3.3 min / 10 min / 60 min, so
> §3's duration-independence not only survives but tightens.
> The estimate's error was assuming a full architecture family's arena would stand behind the
> one resident graph; sweeping one model at a time never stands up the vggish arena on top of
> the musicnn one. Wall clock +2.1%, output byte-identical. See §11 row A.

______________________________________________________________________

## 6. Do the existing fine/coarse caps bound memory? — **No. They bound work.**

`_stride_to_cap` bounds the *number of windows analyzed*. Per §3c, across its entire operating
range (1 → 30 coarse windows) the peak moves **8.55 → 10.39 GiB**. The dominant **8.5 GiB is
paid on window one** and no cap can reach it.

- Setting `analysis_coarse_cap` to its minimum legal value (2) saves ≈ **1.0 GiB** of ~10. It
  does not bring the job under 8Gi.
- Lowering `analysis_coarse_window_sec` does not help either: the peak is graph residency, not
  buffer size. A 180 s @ 16 kHz buffer is **11.5 MB**.
- The fine tier is irrelevant at any setting — the entire fine pass costs **+0.039 GiB**.

______________________________________________________________________

## 7. Why production sees 16.7–30.2 GB and this host sees 9.0–11.7 GiB

> **MEASURED — 2026-08-05, and the inference below it was wrong.** Follow-up C ran this spike's
> harness on the real Linux/glibc burst node against the deployed image and model set. Full
> report: [`phaze-7i0k-linux-memory-measurement.md`](phaze-7i0k-linux-memory-measurement.md).
> The original text of this section is retained in §7.1 as the superseded record.

### 7.0 What the measurement found

**Linux is not more expensive than this host — it is 25–39% cheaper, and there is no ratchet.**
The same 3.3-minute file peaks at **5.948 GiB** on Linux against 9.734 GiB here; the 30-window
envelope maximum is **7.987 GiB** against 10.39 GiB. Three *live production* analyses of real
audio on the same image, sampled externally, peaked at **7.919 / 7.928 / 7.956 GiB** — within 0.9%
of the synthetic figure, which is what licenses a sine-wave measurement to size a production
limit.

| | this host (macOS) | measured on Linux |
| --- | ---: | ---: |
| `dur_200` (3.3 min), production caps | 9.734 GiB | **5.948 GiB** |
| `dur_600` (10 min), production caps | 9.165 GiB | **6.836 GiB** |
| `dur_3600` (60 min), production caps | 11.739 GiB | **7.949 GiB** |
| `dur_43200` (12.1 h), caps 2 | 9.033 GiB | **6.463 GiB** |
| 1 coarse window | 8.55 GiB | **5.809 GiB** |
| 30 coarse windows (envelope maximum) | 10.39 GiB | **7.987 GiB** |
| 34 graphs resident, zero inference | +4.090 GiB | **+3.995 GiB** |
| production real-audio peak (3 runs) | — | **7.919–7.956 GiB** |

The whole §3a sweep reproduces in shape: across a 216× duration span the Linux peak stays inside
**5.9–8.0 GiB** and moves non-monotonically, with the 12.1-hour file cheaper than the 60-minute
one. §3d's predictor holds with new constants — a least-squares refit over the seven Linux window-count
points gives `peak_GiB ≈ 6.06 + 0.64 × ln(min(n_coarse, cap))` (R² 0.93, max |error| 0.37 GiB),
with the same logarithmic shape and a range of **5.8–8.0 GiB** instead of 8.5–10.5 GiB.

**(a) Allocator — retracted in full.** There is no monotone ratchet. A host-side 2-second trace of
a 30-window Linux run finds instantaneous RSS **below its own running maximum in 96% of 1042
samples**, shedding up to **1.485 GiB** at a time and swinging **5.314 ↔ 7.962 GiB** — a sawtooth,
like this host's, just shallower (2.6 GiB against 4.9) and with a higher floor. glibc *does*
retain more; that retention is worth about 1.5 GiB and is *why Linux is cheaper*, because arena
reuse absorbs transients this host re-faults and counts in its high-water. Nor does the curve
climb toward the envelope maximum: the high-water **saturates at 7.9 GiB by window 10** and moves
+1.1% over the following twenty windows, with free-but-retained arena bytes flat at 0.59 GiB — no
fragmentation accumulation. **The claim that the production values form a ratchet continuum is
retracted**: the full kill distribution has a hard floor at 15.27 GiB with clusters near 2×/3×/4×
the measured working set — a multiplicative signature no allocator-retention mechanism produces.
Capping arenas (`MALLOC_ARENA_MAX=1`) is worth **3.6%**, so per-thread arena proliferation was the
wrong pool to suspect. (An *in-process* sampler does report a clean monotone ratchet — max
drawdown 0.081 GiB — but that is an artifact: essentia holds the GIL through inference, so the
sampler thread only runs between models, always at the same phase of the cycle.)

**(b) Thread pools — survives, in reduced form, and the knee is at 4.** Capping TF intra-op
threads saves **14.4%** of peak on Linux (7.488 → 6.412 GiB), not the near-3× the macOS
post-inference RSS figure suggested. The 5.1× wall-time penalty does not generalize: it was
measured only at 1 thread, and on Linux **the entire saving is already available at 4 threads for
+8.2% wall time** (2 threads: +70%; 1 thread: +211%, for 0.001 GiB more). This is the one
mitigation worth adopting.

Neither disturbs the verdict: **duration-independence is a property of the code path**, and it
reproduced exactly.

### 7.1 Superseded — the original inference

*Retained verbatim. Marked explicitly as inference at the time; (a) is now refuted in its
consequence and (b) confirmed at roughly a fifth of its assumed magnitude.*

> **(a) Allocator.** macOS's magazine allocator returns freed pages to the OS: instantaneous RSS
> visibly oscillated **3.256 ↔ 8.205 GiB** across the 30-window run while the high-water sat at
> 10.39 GiB. Linux glibc `malloc` with per-thread arenas does not trim by default, so the same
> allocation pattern becomes a **monotone ratchet toward the envelope maximum**, then accumulates
> fragmentation. The production values — 16.7, 17.1, 18.5, 20.9, 21.2, 23.4, 23.4, 30.2 — form a
> **continuum**, which is the signature of a ratchet sampled at kill time, not of a fixed peak.
>
> **(b) Thread pools.** TensorFlow sizes intra-op pools from the core count and each worker thread
> gets its own arena. Measured here: `TF_NUM_INTRAOP_THREADS=1 TF_NUM_INTEROP_THREADS=1
> OMP_NUM_THREADS=1` cut post-inference RSS from **5.00 → 1.79 GiB** (peak 8.94 → 8.39) at **5.1×
> the wall time** (53 s → 272 s per window). Not a viable mitigation on its own, but it locates a
> large share of the footprint in *resident-but-freeable* memory — exactly the component glibc
> would decline to return.

### 7.2 What remains genuinely open

The production kill values are real — 20 kernel OOM records, `anon-rss` 15.27–30.41 GiB, all
`constraint=CONSTRAINT_NONE`. Follow-up C **did not reproduce them** under any variant tested
(30 windows, onset-dense synthetic audio, three-way concurrency, every allocator and thread
setting), and ruled out the mechanism this section proposed. Their 2–4× multiplicative structure
is unexplained and is filed as its own investigation. The operational consequence is unchanged
and if anything sharper: the outlier is not reachable by tuning, which is exactly the case for a
**limit as a backstop** (ADR-0005) rather than a larger request.

______________________________________________________________________

## 8. Secondary finding — windowed decode is O(total duration), not O(window)

Not the memory bug, but it changes what the long-tail files are actually doing, so it changes
the mitigation advice. `EasyLoader` **does not seek**: the streaming network decodes and
resamples from byte 0 and `Trimmer` discards everything outside the requested range. Controlled
measurement — wall time to decode the **first** 180 seconds of each file:

| total file duration | decode `[0, 180)` | s per minute of total file |
| ---: | ---: | ---: |
| 10 min | 6.6 s | 0.660 |
| 60 min | 45.9 s | 0.765 |
| 180 min | 143.6 s | 0.798 |
| **720 min** | **600.2 s** | 0.834 |

Linear in *total* duration, independent of window length. So per-file cost is
**O(n_windows × duration)** — the Phase 43 caps changed O(duration²) into O(90 × duration), not
into O(duration). Extrapolated for a 724-minute file at production caps: coarse 30 × ~600 s ≈
18,000 s, fine 60 × ~42 s ≈ 2,500 s, **≈ 5.7 hours**, essentially all of it single-threaded
resampling.

**Two consequences.**

1. On the **k8s burst path this is unbounded**. `job_runner` passes `timeout=None` and, since
   `phaze-202e`, emits no `activeDeadlineSeconds`. So a 12-hour file does not get killed at
   `analysis_inner_timeout_sec` (6600 s — that governs the SAQ worker path in
   `tasks/functions.py`, not the pod). It **sits on the ~10 GiB plateau for ~5.7 hours.**
   Duration does not raise the peak; it multiplies the *exposure time at* the peak, which on a
   node running three such jobs is the more dangerous quantity.
1. It independently corroborates a field observation in the bead: *"over a 100-minute window
   with 3 jobs running, CPU averaged ~49.9%."* Three jobs each pinning roughly one core on
   single-threaded libsamplerate resampling is exactly that shape. The idle CPU is not idle
   because memory blocks it — it is idle because the workload is serialized on decode.

______________________________________________________________________

## 9. Re-examination of the requests-only decision (`services/kube_staging.py:228`)

**Superseded.** Recorded as [ADR-0005](../design/0005-analyze-job-memory-limits.md).

The lock — *"resources.requests ONLY -- NO limits (Kueue's quota accounting reads requests; Q1
RESOLVED-adopted: requests-only is locked)"* — rests on a premise this spike falsifies:

| the premise | what the measurements show |
| --- | --- |
| requests approximate actual usage | **every** file exceeds 8Gi, including a 3.3-minute one (9.73 GiB). The error is not 3.8× at the tail — it is ≥1.06× at the *floor* and 3.8× after the Linux ratchet |
| the tail is the problem | there is no tail. Peak is flat in duration; the 12.1-hour file is one of the *cheapest* runs measured |
| a bigger/duration-derived request would fix it | duration is uncorrelated with peak. A duration-derived request would be **precisely the wrong variable** |

Note carefully what the lock's *stated rationale* actually says: Kueue's quota accounting reads
**requests**. That is a reason not to remove or distort requests. **It is not a reason to omit
limits** — a limit is invisible to Kueue's quota accounting and changes no scheduling decision.
The rationale supports keeping requests authoritative, which ADR-0005 does; it never supported
the *absence* of a limit, and the absence is what turned a pod-scoped fault into a node-scoped
one that killed `coredns`, `metrics-server` and `local-path-provisioner`.

______________________________________________________________________

## 10. What a consuming cluster should do in the meantime

**Is any concurrency safe on a 31 GB node? No — not even 1.** Production p100 is 30.2 GB = 97%
of the node. Concurrency 1 removes job-vs-job collision but not job-vs-node, and §8 means a
long file holds that footprint for hours rather than minutes.

Ordered by value per unit of effort:

1. **Get a memory limit onto the analyze pod. It is free and it is the whole fix for the
   collateral damage.** It does not reduce peak usage by one byte. It converts
   `oom-kill:constraint=CONSTRAINT_NONE` — a *global* OOM that picked cluster infrastructure as
   its victims — into a **cgroup OOMKill of exactly the offending pod**. Kueue's quota reads
   requests, so scheduling is unchanged; QoS stays **Burstable** (a memory limit without a CPU
   limit does not promote a pod to Guaranteed). phaze does not emit one today — that is exactly
   what ADR-0005 changes, and it is why the deployment-side knobs were correctly assessed as
   exhausted.
1. **Make the request honest.** 8Gi is below the floor for every file in the corpus. Interim, on
   a 31 GB node: **`memory_request: 12Gi`, `memory_limit: 16Gi`, concurrency 1.** That admits
   one job with room for k0s/kubelet/coredns/metrics-server, and OOMKills the *pod* if the
   Linux ratchet exceeds 16 GiB. Re-derive both numbers from the follow-up C measurement — 12/16
   is sized from this host's 8.5–10.5 GiB floor plus the production ratchet, not from a Linux
   measurement.

   > **Superseded by the follow-up C measurement (`phaze-7i0k`): use `memory_request: 9Gi`,
   > `memory_limit: 12Gi`, concurrency 2.** The Linux peak is 7.99 GiB synthetic / 7.96 GiB on
   > real production audio, so 12Gi over-reserves by 50% and 16Gi sits *above* the 15.27 GiB floor
   > of the pathological population it was meant to catch. 9Gi is the measured peak + 13%; 12Gi is
   > 1.5× it, below that floor, so it catches every pathological run and nothing else. The request
   > is the load-bearing half: at 8Gi, Kueue's 24Gi quota admits 3 jobs whose worst case under a
   > 12Gi limit is 36 GiB — more than the node has. At 9Gi it admits 2 (worst case 24 GiB, ~7 GiB
   > left for the system). Concurrency 2 is safe; "not even 1" was derived from the same
   > unexplained p100 that §7.2 shows is not the operating footprint.
1. **Do not gate admission on duration as a memory control.** It is the wrong variable: it would
   admit every 3-minute file, and those peak just as high. The bead note's "45 known landmines"
   framing is superseded — gating those 45 would not have prevented a single OOM.
1. **A duration gate is still worth having, for a different reason.** Per §8, files past roughly
   4 hours occupy a burst slot at the memory plateau for many hours of mostly single-threaded
   decode. That is a real argument about exposure time and burst throughput. It is not an
   argument about peak.

**What node size does the current workload actually require?** For **concurrency 1** with
headroom for the k0s/kubelet/coredns/metrics-server working set: **≈36–40 GB** (production p100
30.2 GB + ~6 GB). For the **concurrency 3** the quota was originally sized for: **≈100 GB**.
Neither is worth buying. Follow-up A targets a 2.5–3× reduction in the dominant term, after
which a 31 GB node plausibly runs 2–3 jobs comfortably. **Fix the residency; do not buy RAM for
34 idle TF graphs.**

______________________________________________________________________

## 11. Follow-up work — scoped here, filed via `/bh:replan`, **not implemented in this spike**

| | scope | why | size |
| --- | --- | --- | --- |
| **A** | ~~Restructure `_run_model_sets` to model-major iteration so exactly one TF graph is resident; hold the ≤30 coarse buffers (345 MB) instead of 34 graphs (4.09 GiB). Re-measure with this spike's harness.~~ **DONE 2026-08-05 (`phaze-15sw`)** — measured on the burst node with this harness: Linux envelope maximum **7.986 → 2.482 GiB (−69%, 3.2×)**, 34 constructions per file unchanged, output byte-identical, wall clock **+2.1%**. Beat the 3–4 GiB estimate; see §5 and the [Linux measurement](phaze-7i0k-linux-memory-measurement.md) §7c. | Removes the single largest avoidable term. ~~Est. peak 9.7 → 3–4 GiB (**must be measured**).~~ Same number of model constructions as today. | **High value**, medium |
| **B** | Emit `resources.limits.memory` in `build_job_manifest` from a new optional `KubeConfig.memory_limit`; absent ⇒ no `limits` key (byte-identical manifest, backward compatible). Implements ADR-0005. | Turns a node crash into a predictable pod OOMKill. Stops phaze killing `coredns`/`metrics-server`/`local-path-provisioner`. | **High value**, small |
| **C** | ~~Measure peak + ratchet on a real Linux burst node; test `MALLOC_ARENA_MAX`, periodic `malloc_trim`, and TF thread caps.~~ **DONE 2026-08-05 (`phaze-7i0k`)** — see [the measurement](phaze-7i0k-linux-memory-measurement.md). §7 rewritten; 12Gi/16Gi corrected to 9Gi/12Gi; only the TF 4-thread cap is worth adopting. | Converts §7 from inference to measurement and calibrates B's default and §10's 12Gi/16Gi. | Medium |
| **D** | Replace the O(total-duration) per-window decode — seek-based extraction, or a single decode pass feeding both tiers. | O(90 × duration) → O(duration). Unblocks the >4 h tail and recovers the idle CPU in §8. | Medium-high |
| **E** | ~~Raise the documented `memory_request` guidance in `docs/k8s-burst.md`; log measured post-model-load RSS once at analyze start.~~ **DONE 2026-08-05 (`phaze-7qfd`)** — `docs/k8s-burst.md`'s sizing now cites the current `phaze-15sw` design peak (2.482 GiB) with a provenance table instead of either superseded figure, and `analyze_file` logs the job's peak RSS at INFO once per job (`_log_job_peak_rss`, `src/phaze/services/analysis.py`) — high-water RSS rather than a window-load-instant sample, since model-major iteration no longer has a single "all models loaded" point to log after. | Makes the floor observable instead of rediscovered by an OOM. | Small |

**Ordering note:** **B before A.** B is small, backward compatible, and stops the collateral
damage immediately; A is the real fix but wants C's measurement to verify it. Doing A first
leaves the deployment exposed to node-scoped OOM for the whole of A's development.

______________________________________________________________________

## Appendix — reproducing this

The harness is four short scripts driving the **real** `phaze.services.analysis` internals (no
reimplementation of the pipeline) and marking `ru_maxrss` at stage boundaries:

- **stage attribution** — call `_probe_duration_sec` / `_analyze_fine_windows` / `_iter_windows`
  / `_stride_to_cap` / `_run_model_sets` directly, marking RSS + `ru_maxrss` between each.
- **window-count isolation** — recycle one short file's natural 180 s coarse windows N times, so
  every inference has production shape while duration is held constant.
- **per-model attribution** — walk `MODEL_SETS` + `GENRE_MODEL` in production order against one
  buffer, recording the high-water delta per model.
- **decode isolation** — `es.EasyLoader(startTime, endTime)` alone, sweeping total file duration.

Test audio, regenerated in seconds and touching no real library:

```sh
ffmpeg -f lavfi -i "sine=frequency=440:duration=43200" \
       -f lavfi -i "sine=frequency=554:duration=43200" \
       -filter_complex "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]" \
       -map "[a]" -ar 44100 -c:a libmp3lame -b:a 192k dur_43200.mp3
```
