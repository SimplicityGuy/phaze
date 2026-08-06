# phaze-3j67 — how many analyze extractors actually fit, now that peak is 2.482 GiB

- **Bead:** `phaze-3j67` (spike — "measure real concurrent-extractor capacity; cash the `phaze-15sw` win")
- **Date:** 2026-08-05
- **Tree:** branch `wt/bead/issue/phaze-3j67`, forked off `main` at `75020e8`
- **Code under test:** the deployed analyze image `ghcr.io/simplicityguy/phaze/job:2026.8.0` with
  `main`'s post-`phaze-15sw` `services/analysis.py` overlaid (model-major coarse pass, sha256
  `45a84a70…`), against the deployed `phaze-models` PVC (34 graphs, 3.1 GB)
- **Status:** measurement only. **No product code changed.**

______________________________________________________________________

## Verdict in one paragraph

**The win `phaze-15sw` cashed is real, and it is not a concurrency win — the node stopped being
memory-bound and is now CPU-bound at concurrency 2.** Sweeping 1 → 12 concurrent extractors on the
burst node moves aggregate throughput **22.0 → 30.2 files/hour**, of which **67% arrives at W=2 and
84% by W=3**; from 4 to 12 workers the gain is **+2.5%** while per-file wall time grows **+115.9
seconds per added worker (R² 0.9997)**. Memory never binds: per-process peak is **flat at
2.074–2.151 GiB across the entire sweep**, node `MemAvailable` never fell below **13.10 GiB**, and
the memory wall extrapolates to W≈13 — **six times past where CPU already stopped it**. The premise
everything upstream rested on — *"one single-threaded analyze process per pod"* — is **false**: TF
sizes its intra-op pool from the core count, so **one extractor consumes ~6.2 of 8 logical cores**.
And the node is not 10 cores or even 8: it is a Xeon E3-1271 v3 with **4 physical cores** plus SMT,
which is what sets the ~30 files/hour ceiling that **every** thread configuration tested converges
to within 7%. essentia's linear-scaling claim, run literally (arm C: one single-threaded extractor
per core), is **validated to the physical core count — 3.61× on 4 workers, 90.3% efficiency — and
contradicted past it** (+3.9% from 4 → 8 workers, for double the per-file latency). It does not
raise phaze's throughput here, because phaze already reaches the same ceiling by a different route.
The one lever that does pay is the **TF intra-op cap of 4**: throughput-neutral (+0.9% at the
plateau) but **−42% on per-process peak — 2.151 → 1.211 GiB** — three times the reduction
`phaze-7i0k` measured against the pre-`phaze-15sw` code. Recommendation for the 31 GB node:
**`cap = 4` with the 4-thread cap set**, which delivers **98% of the node's maximum throughput at
57% of the memory** — sized in §9.

______________________________________________________________________

## 1. Method

| | |
| --- | --- |
| **Host** | `vox` — Debian 13 (trixie), kernel 6.12.100, glibc 2.41, Xeon E3-1271 v3, **4 physical cores / 8 logical (SMT)**, 31.31 GiB total, k0s burst node, out of the phaze backend registry, otherwise idle |
| **Runtime** | deployed job image `job:2026.8.0`, Python 3.14.6, `essentia-tensorflow` 2.1-beta6-dev, with `main`'s `analysis.py` overlaid onto a `/scratch/src` copy so the measured code is the shipped model-major pipeline |
| **Models** | the deployed `phaze-models` PVC, mounted **read-only** |
| **Audio** | **synthesized with ffmpeg** — the `phaze-esut` appendix generator, stereo 44.1 kHz sine pairs at 192 kbps: 36 **distinct** files (12 worker slots × {180, 300, 420} s), plus 8 × 1200 s for §8 and one 5400 s file for §9b's envelope check |
| **Process model** | **one exec'd child process per file**, exactly as production (`analysis_child` is exec'd once per file). Workers are independent; each runs its files sequentially |
| **Per-process peak** | the child reads its own `/proc/self/status:VmHWM` **once, at exit**. That is a kernel high-water mark, not a sampled curve, so it is immune to the `phaze-7i0k` §9 GIL trap |
| **Node RSS + CPU curve** | a **host-side** sampler outside every container, 1 s cadence: `/proc/meminfo`, `/proc/stat`, and `VmRSS`/`VmHWM` of every matching `/proc/<pid>` |
| **Safety** | the sampler trips an abort file if node `MemAvailable` falls below 2 GiB. It never fired |

**No operator media was read, copied, or referenced.** Every input is a synthesized sine pair. No
filename, path, or per-file metadata value from the library appears in this document.

### 1a. The design that makes levels comparable

Every worker, at **every** concurrency level, does **exactly the same work**: one 180 s, one 300 s
and one 420 s file — 900 s of audio, 3 files. Level *W* therefore runs 3*W* files over *W* workers,
and `files/hour` is directly comparable across levels with no mix effect to correct for. Each worker
owns a **distinct** copy of each duration (`w07_d300.mp3` etc.) so no two workers ever read the same
inode, and the start order is rotated per worker (`w % 3`) so the levels are not phase-locked into
all-decoding-then-all-inferring.

The model PVC and the corpus are **warmed into page cache identically before every level** (steady
state on a real node: the models are read once and stay cached), so no level pays a cold-read tax
another level avoided.

### 1b. What "concurrency" means here

*W* concurrent analyze **processes on one node**, which is what the deployment's `cap` ultimately
buys. They run as *W* children inside one pod rather than *W* pods. `phaze-7i0k` §6c used the same
substitution and it is sound for this question: per-process peak RSS is unchanged by co-residency
(measured there at −0.3%/+0.1%), the models are the same host files under the same page cache either
way, and pod overhead is a fixed ~10 MiB of pause container that changes no conclusion here. What
this measurement does **not** cover is Kueue admission latency or image-pull time — those are
scheduling costs, not extractor capacity.

______________________________________________________________________

## 2. The premise that has to go first: the extractor is not single-threaded

The bead, the backlog note, and `homelab`'s `backends.toml.j2` all rest on one sentence: *"that is
already phaze's architecture — one single-threaded analyze process per pod."* **One process per pod
is right. Single-threaded is wrong**, and everything downstream follows from that.

Solo, one analyze process on the otherwise idle node, node CPU sampled from the host at 1 s across
each file's own start/end window:

| file | wall | mean node CPU | **logical cores consumed** | samples below 25% CPU (single-thread-shaped) |
| ---: | ---: | ---: | ---: | ---: |
| 180 s | 93.9 s | 81.4% | **6.51** | 3% |
| 300 s | 162.0 s | 77.3% | **6.18** | 10% |
| 420 s | 234.3 s | 74.3% | **5.95** | 14% |

**One extractor consumes ~6.2 of the node's 8 logical cores.** TensorFlow sizes its intra-op pool
from the core count (`phaze-7i0k` §5 measured 8 threads as the default here), and `phaze-15sw`'s
model-major sweep runs each graph across every coarse window back to back, so the inference phase is
a wide-thread burst, not a single-threaded walk. The single-threaded portion is real but small at
these durations — it is the non-seeking `EasyLoader` decode of `phaze-esut` §8, and its share grows
with duration exactly as that section predicts (3% → 14% of samples from 180 s to 420 s).

**This is the whole story of the sweep below.** A node that is 75–81% busy with *one* job has about
a core and a half of headroom for a second. essentia's linear-scaling claim is a claim about
single-threaded extractors; phaze does not run one. (§4 shows the "8 cores" is itself generous —
there are four, plus hyperthreads.)

______________________________________________________________________

## 3. The sweep — the knee is at **2**, and the plateau is 30 files/hour

Concurrency 1 → 12, TF left at its defaults (the deployed configuration). Every row is 3*W* files
of identical mix; `files/h` is the number that matters.

| W | wall (s) | files | **files/h** | speedup | efficiency | per-file wall (s) | inflation | proc peak (GiB) | node RSS peak (GiB) | min avail (GiB) | node CPU |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 490 | 3 | **22.0** | 1.00× | 100% | 163 | 1.00× | 2.074 | 2.00 | 27.45 | 75% |
| **2** | 786 | 6 | **27.5** | **1.25×** | 62% | 262 | 1.60× | 2.103 | 3.61 | 25.87 | 93% |
| 3 | 1123 | 9 | **28.9** | 1.31× | 44% | 374 | 2.29× | 2.123 | 5.42 | 24.15 | 98% |
| 4 | 1468 | 12 | **29.4** | 1.34× | 33% | 488 | 2.99× | 2.149 | 6.54 | 23.07 | 99% |
| 6 | 2174 | 18 | **29.8** | 1.35× | 23% | 723 | 4.42× | 2.138 | 9.36 | 20.32 | 100% |
| 8 | 2878 | 24 | **30.0** | 1.36× | 17% | 957 | 5.86× | 2.151 | 12.54 | 17.23 | 100% |
| 10 | 3587 | 30 | **30.1** | 1.37× | 14% | 1193 | 7.30× | 2.140 | 13.91 | 16.09 | 100% |
| 12 | 4297 | 36 | **30.2** | 1.37× | 11% | 1428 | 8.74× | 2.151 | 16.95 | 13.10 | 100% |

```
files/hour vs concurrency (TF defaults)

  W=1  |###################################.............   22.0
  W=2  |############################################....   27.5   <- knee
  W=3  |##############################################..   28.9
  W=4  |###############################################.   29.4
  W=6  |###############################################.   29.8
  W=8  |################################################   30.0
  W=10 |################################################   30.1
  W=12 |################################################   30.2
```

**The knee is at W=2.** Of the 8.13 files/hour that concurrency buys in total, **67% arrives at
W=2** and **84% by W=3**. Going from 2 to 12 workers — six times the memory, six times the
scheduling, six times the blast radius — buys **+9.7%**. From 4 to 12 it buys **+2.5%**, which is
inside the run-to-run noise of a single level.

Three things to read off the table beyond the headline:

- **Per-file wall inflates almost exactly linearly with W past the knee.** 163 → 262 → 374 → 488 →
  723 → 957 → 1193 → 1428 s: a straight line of **+115.9 s per added worker, R² 0.9997**. That is
  the definition of a saturated queue — every extra worker is served by taking time away from the
  others, not from idle capacity. At W=12 a 7-minute file takes **34.5 minutes**.
- **The cores go flat before the throughput does.** Busy logical cores by level: 5.98, 7.46, 7.81,
  7.91, 7.97, 7.99, 7.99, 7.99 out of 8. From W=4 on the node is running **99.9% of its CPU**, and
  the only thing concurrency changes is how thinly it is sliced — 5.98 logical cores per process at
  W=1 down to **0.67 at W=12**.
- **Per-process peak RSS is flat in W**: 2.074 → 2.151 GiB across a 12× change in co-residency, a
  **3.7% spread with no trend**. `phaze-7i0k` §6c measured this pre-`phaze-15sw` at 3-way and it
  survives the restructure at 12-way. Whatever else concurrency costs, it does not cost per-process
  memory.

______________________________________________________________________

## 4. Why it plateaus at 30 — the node has **4 cores, not 8**

`nproc` says 8. `lscpu` says what actually matters:

```
Model name:          Intel(R) Xeon(R) CPU E3-1271 v3 @ 3.60GHz
CPU(s):              8
Thread(s) per core:  2          <- SMT
Core(s) per socket:  4          <- four physical cores
L3 cache:            8 MiB (1 instance)
```

**vox has four physical cores.** The bead's premise ("the node is 10 cores") is wrong twice over —
it is 8 logical, and 4 real. Every number in §3 falls out of that one fact, and the cleanest way to
see it is throughput per *busy logical core*:

| arm | W | busy logical cores | files/h | **files/h per busy logical core** |
| --- | ---: | ---: | ---: | ---: |
| intra-op 1 | 1 | 1.09 | 7.5 | **6.92** |
| intra-op 1 | 4 | 4.16 | 27.2 | **6.54** |
| intra-op 4 | 1 | 3.33 | 20.6 | **6.19** |
| intra-op 4 | 2 | 6.62 | 26.9 | 4.07 |
| baseline (8) | 1 | 5.98 | 22.0 | 3.68 |
| baseline (8) | 2 | 7.46 | 27.5 | 3.68 |
| intra-op 1 | 8 | 7.99 | 28.3 | 3.54 |
| intra-op 4 | 8 | 7.98 | 30.3 | 3.79 |
| baseline (8) | 12 | 7.99 | 30.2 | 3.77 |

The population splits cleanly in two. Every configuration that keeps its runnable threads **at or
below 4** returns **6.2–6.9 files/hour per logical core**; every configuration that fills all 8
returns **3.5–3.8** — almost exactly half. That is not a coincidence and it is not a scheduling
artifact: it is what SMT looks like measured from outside. Two hyperthread siblings on one physical
core each accumulate CPU-time at roughly half the rate of a core to themselves, so the second half
of the "8 cores" is worth about 10%, not another 100%.

**The ceiling is therefore ~4 physical cores' worth of analysis, ≈ 30 files/hour, and no thread or
concurrency setting tested moves it by more than 7%.** Every arm converges there:

| best result per arm | files/h |
| --- | ---: |
| baseline, 8 intra-op threads, W=12 | 30.2 |
| intra-op 4, W=8 | **30.3** |
| intra-op 1, W=8 | 28.3 |

A corollary worth stating because it is easy to get backwards: **per-file kernel CPU-time is not a
conserved quantity here, and must not be used to extrapolate.** The same file costs 518 CPU-seconds
run single-threaded and 971 with the default 8-thread pool, but that near-doubling is mostly SMT
accounting — a sibling thread charges wall-clock against a half-speed core — not 450 seconds of
wasted work. The quantity that predicts throughput is **physical-core occupancy**, and it is
saturated from W=2.

______________________________________________________________________

## 5. Arm B — the TF intra-op cap of 4, evaluated as a throughput lever

`phaze-7i0k` §5 adopted `TF_NUM_INTRAOP_THREADS=4` for −14.4% peak at +8.2% wall. That was a
memory verdict measured pre-`phaze-15sw`. Re-run as a **throughput** question, and re-measured
against the model-major peak:

| W | files/h (intra-op 4) | vs baseline | per-file wall | proc peak (GiB) | vs baseline peak | busy cores |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 20.6 | **−6.4%** | 174.6 s | **1.210** | −41.7% | 3.33 |
| 2 | 26.9 | −2.0% | 267.1 s | 1.207 | −42.6% | 6.62 |
| 4 | **29.6** | +0.6% | 485.4 s | 1.208 | −43.8% | 7.88 |
| 8 | **30.3** | **+0.9%** | 947.5 s | 1.211 | −43.7% | 7.98 |

**As a throughput lever it is a wash: +0.9% at the plateau, −6.4% at W=1.** The ceiling is four
physical cores (§4), and how many threads each process is allowed does not change how many cores
the node has.

**As a memory lever it is far better than advertised.** `phaze-7i0k` measured −14.4% against the
window-major peak; against the model-major peak it is **−42%, and flat in W** (1.207–1.211 GiB
across 1→8 workers). It holds at the ceiling too — re-running the `coarse_cap = 30` envelope
(§9b) with the cap set gives **1.5535 GiB against 2.5663 GiB, −39.5%**. The two changes compose
better than either predicted: 34 co-resident graphs went away with `phaze-15sw`, and what is left
is dominated by per-inference arena, which is exactly what the thread cap shrinks. **2.5 GiB is not
the floor — 1.55 GiB is**, for a 0.9% *improvement* in throughput at the operating point.

It also moves the knee: with 4 threads per process the node needs **W=4** to reach 99% CPU instead
of W=2–3, which is a strictly better place to sit — same throughput, more jobs in flight, each with
a smaller footprint.

______________________________________________________________________

## 6. Arm C — essentia's claim, run on its own terms

> *"For large-scale audio track database computations, it is more effective to run each extractor in
> a single-threaded manner and distribute these instances across available CPU cores. This approach
> **scales linearly** with the number of cores…"*

Arm C is that prescription, literally: `TF_NUM_INTRAOP_THREADS=1 TF_NUM_INTEROP_THREADS=1
OMP_NUM_THREADS=1`, one extractor per core.

| W | files/h | speedup vs W=1 | **efficiency** | per-file wall | busy logical cores | proc peak (GiB) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 7.5 | 1.00× | 100% | 477.3 s | 1.09 | 1.158 |
| **4** | **27.2** | **3.61×** | **90.3%** | 527.5 s (+10.5%) | 4.16 | 1.162 |
| 8 | 28.3 | 3.75× | 46.9% | 1014.4 s (+113%) | 7.99 | 1.163 |

**Verdict: validated up to the physical core count, contradicted past it — and irrelevant to the
outcome on this node.** Three separate findings, and they need to be kept apart:

1. **The claim is true where it is testable.** Single-threaded extractors distributed across the
   node scaled **3.61× on 4 workers — 90.3% of ideal**, with per-file wall inflating only 10.5%.
   That is linear scaling by any reasonable standard, and it is the only arm in this spike that
   produced it. essentia is right about the mechanism.
1. **"Available CPU cores" has to mean *physical* ones.** From 4 → 8 workers the same configuration
   gained **+3.9%** while per-file wall **doubled** (527 → 1014 s). Workers 5–8 land on hyperthread
   siblings and get roughly half a core each. Taking `nproc` as the instance count — the obvious
   reading of the guidance — is precisely the mistake that costs 2× per-file latency for nothing.
1. **Following the advice would not raise phaze's throughput here, because phaze is already at the
   node's ceiling by a different route.** Arm C's best is **27.2 f/h**; the deployed 8-thread
   configuration reaches **27.5 at W=2** and **30.2 at W=12**. Wide intra-op pools and many
   single-threaded processes are two ways of filling the same four cores, and the four cores are
   the constraint. The guidance is aimed at operators whose extractors would otherwise sit
   single-threaded on an idle 32-core box; phaze's problem was never idle cores.

The `phaze-esut` §8 observation this was supposed to explain — *"CPU averaged ~49.9% with 3 jobs"* —
is **not** reproduced under any arm here: 3 concurrent jobs put this node at **97.6%**. §9 takes up
what that means.

______________________________________________________________________

## 7. Which binds first — **CPU, at W=2, by a factor of six**

The bead asked the question the right way round, and the answer is not close.

| | binds at | measured evidence |
| --- | ---: | --- |
| **CPU** | **W=2** | node CPU 74.7% at W=1, **93.3% at W=2**, ≥98% from W=3, 99.9% from W=6. Throughput gains 67% of its total at W=2 and 84% by W=3. Per-file wall then rises **+115.9 s per added worker, R² 0.9997** |
| **Memory** | **W≈13** | per-process peak flat at 2.074–2.151 GiB; node RSS 2.0 GiB at W=1 → 16.95 GiB at W=12; **`MemAvailable` never fell below 13.10 GiB**; the 2 GiB abort guard never fired; swap use flat at 0.23 GiB (its idle value) at every level |

**Memory is not the constraint and is not close to being one.** At W=12 — six times past the
throughput knee — the node still had **13.1 GiB free**. Extrapolating the measured 1.41 GiB/worker
node-RSS slope against 31.31 GiB total and the ~1.9 GiB the k0s stack occupies at idle puts the
memory wall at **W≈20 by mean residency, W≈13 by worst-case per-process peak** — and CPU has been
saturated since W=2.

So the framing this whole line of work inherited from `phaze-esut` — *"the only thing that ever
blocked it was per-process memory"* — was true at 8 GiB per job and is now **false**. `phaze-15sw`
did not unlock 8–10 concurrent extractors. It unlocked **as many as the operator wants, on a node
that can usefully run about four**.

### 7a. The `phaze-wcrb` 2–4× population, re-priced

`phaze-wcrb` is open: 20 production kills at 15.27–30.41 GiB clustering near 2×/3×/4× of the
then-current ~7.7 GiB working set, mechanism unidentified. Read multiplicatively — which is how
that bead reads it — the same excursion against today's working set is:

| | working set | 2× | 3× | **4×** |
| --- | ---: | ---: | ---: | ---: |
| pre-`phaze-15sw` (what the kills were sampled from) | 7.7 GiB | 15.4 | 23.1 | **30.8** |
| post-`phaze-15sw`, TF defaults (envelope maximum) | **2.482 GiB** | 4.96 | 7.45 | **9.93** |
| post-`phaze-15sw` + intra-op 4 | **1.211 GiB** | 2.42 | 3.63 | **4.84** |

Against the recommended sizing (§9: `cap` 4, `memory_limit` 4Gi):

- **Every excursion in the population is caught, and nothing else is.** Normal work peaks at
  2.482 GiB — 62% of the 4Gi limit — while the *smallest* excursion in the population, 2×, is
  **4.96 GiB**, above it. The limit therefore separates the two populations cleanly, which is the
  property `phaze-7i0k` §7a was reaching for when it set 12Gi below the 15.27 GiB kill floor.
- **Four contained pods is 4 × 4Gi = 16 GiB**, against 31.21 GiB allocatable and ~1.9 GiB of k0s
  stack: **~13.3 GiB still free** with every pod simultaneously pinned at its ceiling.
- **The limit is load-bearing, not belt-and-braces.** Without it, four pods each free to reach a
  full 4× excursion is 4 × 9.93 = **39.7 GiB — more than the node has**, which is precisely the
  `CONSTRAINT_NONE` node-scoped kill that started this investigation. `cap = 4` is safe **because**
  `phaze-k6d5` shipped `resources.limits.memory` and the operator sets it. On a deployment that
  leaves `memory_limit` unset (the code default, per ADR-0005), the same multiplicative reading caps
  safe concurrency at **3** (3 × 9.93 = 29.8 GiB, no headroom) and realistically at 2.

The honest statement of residual risk: `phaze-wcrb` is unexplained, so the multiplicative reading
could be wrong — the excursion could be additive-and-large rather than proportional, in which case
these figures are optimistic. A pod-scoped limit covers that case too, because it bounds the pod
whatever the mechanism. **Concurrency is not the lever that governs this risk; the limit is.** The
reason `cap` is 4 rather than 12 is throughput and latency (§9a), not OOM.

______________________________________________________________________

## 8. Arm D — the decode-dominated regime, where the bead expected super-linear scaling

`phaze-esut` §8 is the reason to distrust a sweep of 3–7 minute files: `EasyLoader` does not seek,
so per-file cost is O(n_windows × duration) and the decode part is single-threaded. A long file
therefore has a much larger single-threaded share, and the bead's hypothesis was that concurrency
might **fill that idle CPU and scale better than linearly**. One 20-minute file per worker, TF
defaults:

| W | files/h | speedup | per-file wall | node CPU | proc peak (GiB) | audio-seconds analyzed per hour |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 4.64 | 1.00× | 776 s | **65.1%** | 2.168 | 5 567 |
| 4 | 6.92 | **1.49×** | 2 078 s | 94.1% | 2.301 | 8 304 |

**The direction of the hypothesis is confirmed; the magnitude is not.** A 20-minute file leaves the
node at **65.1%** CPU solo against **74.7%** for the 3–7 minute mix, exactly as a growing
single-threaded decode share predicts, and concurrency does convert some of that idle time:
**1.49× at W=4 against 1.34× for short files**. But it is not super-linear and it does not change
the ceiling. Measured in audio-seconds analyzed per hour — the mix-independent unit — the node
delivers **8 304 s/h on 20-minute files against 8 800 s/h on the short mix at the same W=4**, i.e.
within 6%. **Duration changes what the node is doing, not how much of it the node can do.**

Two things worth carrying forward:

- **Long files are more expensive per second of audio, and the gap grows.** Wall ÷ duration is
  0.52 / 0.54 / 0.56 for 180/300/420 s and **0.65 for 1200 s** — `phaze-esut` §8's
  O(n_windows × duration) showing up directly. A duration gate remains worth having for *exposure
  time*, exactly as that spike argued, and remains the wrong instrument for memory.
- **Peak RSS creeps with coarse window count, not duration.** 2.168 GiB at 1200 s against 2.151 at
  420 s, and 2.301 GiB at W=4 — still inside `phaze-15sw`'s 2.482 GiB envelope maximum, which
  remains the right ceiling to size against.

______________________________________________________________________

## 9. Recommended sizing for the 31 GB burst node

**Ready for `homelab-a2x`.** These are one package, not four independent knobs.

| setting | where it lives | **recommended** | currently |
| --- | --- | ---: | ---: |
| `cap` | `backends.toml.j2`, vox `[[backends]]` | **4** | 3 (commented out) |
| `cpu_request` | `backends.toml.j2`, `[backends.kube]` | **`1500m`** (unchanged) | `1500m` |
| `memory_request` | `backends.toml.j2`, `[backends.kube]` | **`3Gi`** | `8Gi` |
| `memory_limit` | `backends.toml.j2`, `[backends.kube]` | **`4Gi`** | unset |
| `vox_kueue_cpu_quota` | `homelab` `host_vars/vox.yml` | **`6`** | `7` |
| `vox_kueue_mem_quota` | `homelab` `host_vars/vox.yml` | **`12Gi`** | `24Gi` |
| `TF_NUM_INTRAOP_THREADS` / `TF_NUM_INTEROP_THREADS` / `OMP_NUM_THREADS` | `phaze-agent-env` ConfigMap (`envFrom`, `kube_staging.py:345`) | **`4` / `1` / `4`** | unset |

**Lockstep, both resources, both exact:**

```
memory:  cap 4 x memory_request 3Gi  = 12Gi  <= vox_kueue_mem_quota 12Gi     (a 5th pod needs 15Gi — refused)
cpu:     cap 4 x cpu_request 1500m   = 6     <= vox_kueue_cpu_quota 6        (a 5th pod needs 7.5 — refused)
```

### 9a. Why `cap = 4`

Not "as many as memory allows" — memory allows about 13 (§7), and that is the wrong question now.
`cap` should be **the smallest concurrency that reaches the node's throughput ceiling**, because
every worker past that point multiplies per-file latency for nothing:

| cap | files/h | % of node maximum | per-file wall | node RSS peak |
| ---: | ---: | ---: | ---: | ---: |
| 2 | 27.5 | 91% | 262 s | 3.6 GiB |
| 3 | 28.9 | 96% | 374 s | 5.4 GiB |
| **4** | **29.4–29.6** | **98%** | **485 s** | 3.7–6.5 GiB |
| 8 | 30.0–30.3 | 100% | 950 s | 7.6–12.5 GiB |
| 12 | 30.2 | 100% | 1 428 s | 17.0 GiB |

**4 buys 98% of the ceiling at a third of cap 12's per-file latency**, and it is the concurrency at
which the 4-thread configuration first saturates the cores (98.5% CPU at W=4 against 82.8% at W=2),
so the two halves of the recommendation reinforce each other: **four pods, four physical cores, four
TF threads each.** cap 8 is the throughput maximum and costs **+96% per-file wall for +2.4%** — a
bad trade for a burst lane whose whole purpose is to shorten the tail.

### 9b. Why `memory_request = 3Gi` and `memory_limit = 4Gi`

Sized against the **envelope maximum** — one `analyze_file` run with `coarse_cap` saturated at 30
windows, which is the ceiling no real file can exceed — and **not** against this spike's concurrency
sweep, whose 2.151 GiB is a short-file figure:

| input | value |
| --- | ---: |
| this sweep, 180–420 s files, W=1…12 | 2.074–2.151 GiB |
| this sweep, 20-minute files, W=4 | 2.301 GiB |
| `phaze-15sw` envelope maximum (recycled-window harness) | 2.482 GiB |
| **this spike's envelope maximum** (90-minute file, `coarse_cap=30`, full `analyze_file`) | **2.5663 GiB** |
| — same, with the intra-op cap of 4 | 1.5535 GiB (**−39.5%**) |
| **design peak** | **2.57 GiB** |
| `memory_request` = design peak × 1.17 | **3Gi** |
| `memory_limit` = design peak × 1.56 | **4Gi** |

The envelope was re-measured here rather than inherited, and it **independently reproduces
`phaze-15sw`'s 2.482 GiB to +3.4%** by a different route — a real 90-minute file through the whole
`analyze_file` path, against that spike's recycled-window harness. Taking the larger of the two as
the design peak is the conservative reading and costs nothing.

This lands on the same `3Gi`/`4Gi` that `phaze-7qfd`'s in-flight rewrite of `docs/k8s-burst.md`
derives from the same design peak — **two independent derivations agreeing**, which is the useful
part; the concurrency half is what this spike adds. The margin ratios follow `phaze-7i0k` §7a's rule
(request ≈ peak + ~15–20%, limit ≈ 1.5× peak).

**These numbers hold whether or not the thread-cap env is set**, and that is deliberate. The env
lives in a ConfigMap the operator owns; sizing the request against the *capped* 1.211 GiB peak would
make the deployment's memory safety depend on a setting phaze cannot see or enforce. With the cap
set, actual usage is ~1.2 GiB against a 3Gi request — the pod is Burstable, so the slack costs
nothing but Kueue bookkeeping, and §9d says when to spend it.

### 9c. Node headroom

| | GiB |
| --- | ---: |
| node capacity / allocatable | 31.31 / **31.21** |
| k0s + kubelet + containerd + coredns + kube-proxy + kube-router + metrics-server + kueue + local-path, measured at idle | **≈1.9** |
| 4 analyze pods at the **measured** peak (2.151–2.482 GiB) | 8.6–9.9 |
| 4 analyze pods at their **limit** (worst case the cgroup permits) | 16.0 |
| **free at the limit-worst-case** | **≈13.3** |

Measured against the sweep rather than the arithmetic: node `MemAvailable` at W=4 never fell below
**23.07 GiB**, and even at W=12 — three times the recommended cap — it held **13.10 GiB**. Swap use
stayed at its idle 0.23 GiB at every level; nothing ever paged. The abort guard at 2 GiB never
armed.

### 9d. When to tighten

Two follow-on conditions, both cheap to check and neither blocking:

1. **Once the thread-cap env has been live for a real drain**, `memory_request` may drop to `2Gi`
   and `memory_limit` to `3Gi` (`vox_kueue_mem_quota` → `8Gi`, `cap` unchanged at 4). The capped
   envelope maximum is **measured at 1.5535 GiB**, so 2Gi is peak × 1.29 and 3Gi is × 1.93 — the
   same margin shape one tier down. Confirm against `analyze_file`'s peak-RSS log line
   (`phaze-7qfd` added exactly that), so this is an observation, not another spike. Do **not** make
   this change and the ConfigMap change in the same deploy.
1. **Do not tighten the limit toward the request.** Equal `cpu` *and* `memory` request/limit would
   promote the pod to QoS `Guaranteed`, which this deployment deliberately avoids
   (`docs/k8s-burst.md`; pinned by
   `test_build_job_manifest_memory_limit_keeps_qos_burstable`), and the gap is what contains the
   `phaze-wcrb` population pod-scoped (§7a).

______________________________________________________________________

## 10. What this measurement does and does not support

- **Supported:** aggregate throughput, per-file wall, per-process peak RSS, node RSS and node CPU
  across concurrency 1–12 on this node, for this image + model set, under four thread
  configurations and two duration regimes. The binding resource and the concurrency at which it
  binds. The physical-core explanation, which is a `lscpu` fact plus a measured 2:1 split in
  throughput-per-logical-core.
- **Not supported: any of this transferred to a different node.** The ceiling is 4 physical Haswell
  cores; a burst node with more real cores has a proportionally higher ceiling and a knee at a
  different W. `cap` is a **per-backend** setting and each cluster in the multi-cluster mesh must
  be sized from its own hardware. What *does* transfer is the shape: CPU binds long before memory
  now, so size `cap` from cores and `memory_request` from the measured peak.
- **Not supported: any claim about `phaze-wcrb`.** §7a re-prices the risk under the two readings of
  that bead's own data; it does not identify the mechanism and did not reproduce the population.
- **Not supported: Kueue admission behaviour, image-pull time, or JuiceFS read throughput.** The
  harness runs *W* exec'd children in one pod against a page-cache-warm local corpus (§1b). Those
  are real costs in production and none of them is measured here.
- **Synthetic audio is inherited, not re-validated.** `phaze-7i0k` §6b established that peak memory
  is content-independent (real audio and sine agree to 0.9%) because it is a function of window
  *shape*. **Wall time does not automatically inherit that licence** — a sine pair is cheap for the
  beat trackers. The fine-pass tracker cost is a small share of a file (`phaze-esut` §2:
  +0.039 GiB, and the pass is decode-dominated), so the throughput figures should be read as an
  **upper bound** on real-audio throughput, tight to the extent that decode and TF inference
  dominate — which §2 and §8 show they do.
- **One known perturbation, recorded because CPU is the binding resource here.** A sampler process
  from the W=3 level was not reaped and kept running at **0.5% of one core** (0.06% of the node)
  through the remaining levels. Its own samples are excluded — every level is sliced to its own
  start/end window — and its load is two orders of magnitude below the differences the sweep
  reports, but on a node measured at 99.9% CPU it is worth stating rather than eliding.
- **A methodological note worth repeating from `phaze-7i0k` §9.** Every RSS/CPU *curve* here comes
  from a host-side sampler outside the container. A child reading its own `VmHWM` **once at exit**
  is not sampling — it is reading a kernel high-water mark — and is not subject to the GIL-starved
  sampler artifact. Do not replace it with an in-process sampler thread.

______________________________________________________________________

## 11. Recommendations

| | action | why |
| --- | --- | --- |
| 1 | **`cap = 4`, `memory_request: 3Gi`, `memory_limit: 4Gi`, `cpu_request: 1500m`, `vox_kueue_mem_quota: 12Gi`, `vox_kueue_cpu_quota: 6`** | §9. 98% of the node's throughput ceiling; both lockstep constraints exact; ~13 GiB of node headroom even at the limit-worst-case. Feeds `homelab-a2x`. |
| 2 | **Set `TF_NUM_INTRAOP_THREADS=4 TF_NUM_INTEROP_THREADS=1 OMP_NUM_THREADS=4` in the `phaze-agent-env` ConfigMap** | §5. **−42% per-process peak** (2.151 → 1.211 GiB) for **+0.9%** throughput at the operating point. `phaze-7i0k` recommended this at −14.4%; against the model-major code it is three times better. |
| 3 | **Stop treating memory as the burst-lane constraint** | §7. It binds at W≈13; CPU binds at W=2. Every future capacity argument about this node is a CPU argument. |
| 4 | **Do not raise `cap` past 4 on this node, and do not carry `cap = 4` to a different one** | §4, §10. The ceiling is 4 physical cores. `cap` is per-backend; each cluster in the mesh needs its own knee measured. Past the knee, `cap` only inflates per-file latency. |
| 5 | **Do not adopt single-threaded extractors (`TF_NUM_INTRAOP_THREADS=1`)** | §6. essentia's guidance is sound and scales at 90.3% efficiency to 4 workers, but it tops out at **27.2 f/h** against 29.6 for the recommendation, because both routes fill the same four cores. |
| 6 | **When throughput on this lane becomes the binding problem, buy cores — not RAM, and not concurrency** | §4, §7. 30 files/hour is 4 Haswell cores. The node has 13 GiB of memory it cannot use and no CPU to spare. |

______________________________________________________________________

## 12. What this changes upstream

- **`phaze-esut` §10** — *"Is any concurrency safe on a 31 GB node? No — not even 1."* Superseded
  twice over: `phaze-7i0k` corrected it to 2, and this spike measures **4 as comfortable and 12 as
  survivable**, with the binding constraint no longer memory at all.
- **`phaze-esut` §8's field observation** — *"over a 100-minute window with 3 jobs running, CPU
  averaged ~49.9%"* — **does not reproduce**: three concurrent jobs put this node at **97.6%**. The
  49.9% figure was recorded against the window-major code at ~8 GiB per job, where the node could
  not actually hold three resident analyses; whatever it was averaging over, it was not three
  concurrently-running extractors. It should not be cited as evidence of spare CPU.
- **`phaze-7i0k` §8 recommendation 1** — *"`memory_request: 9Gi`, `memory_limit: 12Gi`, concurrency
  2"* — was already flagged there as sized against a peak that no longer exists. This spike
  replaces the concurrency half (**2 → 4**) and independently re-derives the memory half to the
  same `3Gi`/`4Gi` that `phaze-7qfd`'s in-flight `docs/k8s-burst.md` rewrite arrives at.
- **`phaze-7i0k` §5's TF-thread verdict** stands and is strengthened: still the only knob that pays,
  now worth −42% rather than −14.4%.
- **The bead's own premises**, recorded because they were load-bearing and both wrong: the node is
  **4 physical cores, not 10**, and the extractor is **not single-threaded**. Neither error is
  anyone's carelessness — `nproc` says 8, and "one process per pod" reads as single-threaded until
  you measure the thread pool.

______________________________________________________________________

## Appendix — reproducing this

Five short scripts, in the shape `phaze-esut` and `phaze-7i0k` established (drive the **real**
`phaze.services.analysis`; never reimplement the pipeline):

- **`analyze_one.py`** — one file, one process. Imports `analyze_file` from a `/scratch/src` copy of
  the image's tree with `main`'s `analysis.py` overlaid, runs it against `/models`, and prints one
  JSON line carrying wall time and its own `/proc/self/status:VmHWM`, read once at exit.
- **`driver.py`** — spawns *W* worker threads, each `subprocess.run`-ing `analyze_one.py` once per
  file over its private `{180, 300, 420}` s triple (start order rotated by `w % 3`), and reports
  aggregate wall, `files/hour`, per-file wall and per-process peak. `--tf-intraop N` sets
  `TF_NUM_INTRAOP_THREADS=N TF_NUM_INTEROP_THREADS=1 OMP_NUM_THREADS=N` on the children only.
- **`sampler.py`** — runs on the **host**, outside every container, at 1 s cadence: `/proc/meminfo`,
  `/proc/stat` deltas for node CPU, and `VmRSS`/`VmHWM` for every `/proc/<pid>` whose cmdline matches
  the child marker. Trips an abort file below 2 GiB `MemAvailable`.
- **`run_level.sh`** — warms the model PVC and corpus into page cache, starts the sampler, runs one
  level in the pod, stops the sampler, idles 20 s between levels.
- **`envelope.py`** (§9b) — one `analyze_file` call with `coarse_cap=30, fine_cap=1` on a 90-minute
  file, i.e. the `coarse_cap` ceiling no real file can exceed, with the fine pass pinned to one
  window so it does not dominate the wall clock. Run once per thread configuration.

The pod is a bare `sleep infinity` pod on the burst node using the deployed job image, with the
`phaze-models` PVC mounted **read-only** and a host scratch dir, and **no Kueue queue label** so it
consumes no quota:

```sh
kubectl run/apply a pod:  image ghcr.io/simplicityguy/phaze/job:<tag>
                          volumeMounts: phaze-models (ro) at /models, scratch at /scratch
                          command: ["sleep", "infinity"]
```

The image ships the pre-`phaze-15sw` `analysis.py` (its sha256 matches `8d71a51^` exactly), so the
model-major pipeline is measured by copying `/app/src` to `/scratch/src`, overwriting
`services/analysis.py` with `main`'s, and putting `/scratch/src` first on `sys.path` — the same
overlay technique `phaze-15sw` used, and the reason §2's solo peak reproduces its published figure.

Test audio — `phaze-esut`'s generator, 36 + 8 distinct files, touching no real library:

```sh
# worker slot w, duration d: distinct frequency pair per slot so no two workers share an inode
f1=$((330 + w * 7)); f2=$((f1 + 114))
ffmpeg -loglevel error -y \
       -f lavfi -i "sine=frequency=${f1}:duration=${d}" \
       -f lavfi -i "sine=frequency=${f2}:duration=${d}" \
       -filter_complex "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]" \
       -map "[a]" -ar 44100 -c:a libmp3lame -b:a 192k "w$(printf %02d "$w")_d${d}.mp3"
```

**Three things to get right when re-running.** Sample RSS from **outside** the process
(`phaze-7i0k` §9) — but note that a child reading its own `VmHWM` once at exit is *not* sampling and
is not subject to that trap. Give every worker the **same** duration mix, or `files/hour` stops
being comparable across levels. And warm the page cache identically before each level, or the first
level pays for the models PVC and every later one does not.
