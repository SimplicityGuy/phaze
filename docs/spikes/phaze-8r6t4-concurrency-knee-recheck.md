# phaze-8r6t4 — the concurrency knee, re-measured on the current code

- **Bead:** `phaze-8r6t4` (spike — "re-measure the concurrency knee; `phaze-3j67`'s W=2 was measured
  on a decode-bound workload that no longer exists")
- **Date:** 2026-08-07
- **Tree:** branch `wt/bead/issue/phaze-8r6t4`, forked off `release/2026.8.1-prep` at `29796a9`
- **Code under test:** the deployed analyze image `ghcr.io/simplicityguy/phaze/job:2026.8.0` with
  `release/2026.8.1-prep`'s `services/analysis.py` (sha256 `a8c30496…`) **and**
  `services/analysis_sizing.py` (sha256 `d56d2bc…`) overlaid — the post-`phaze-5lop`,
  post-`phaze-rvcn`, post-`phaze-0582` pipeline — against the deployed `phaze-models` PVC
  (3.1 GB). Overlay verified by checksum **inside the container and again inside every child
  process** (§1c)
- **Status:** measurement only. **No product code changed.** vox was never returned to the phaze
  backend registry

______________________________________________________________________

## Verdict in one paragraph

**The knee has not moved. It is still W=2, and `cap = 4` stands — now on a measurement of the code
that will actually be deployed rather than on an inherited one.** Sweeping 1 → 12 concurrent
extractors on the burst node with `release/2026.8.1-prep`'s derived sizing (intra-op 4, inter-op 1,
OMP 4) moves aggregate throughput **21.5 → 30.6 files/hour**, of which **63.7% arrives at W=2 and
84.6% by W=3** — against `phaze-3j67`'s **67% and 84%** on the pre-change code. The plateau moved
**30.2 → 30.6 f/h, +1.4%**; per-file wall still inflates linearly past the knee at **+113.2 s per
added worker (R² 0.9996)** against that spike's +115.9. Every headline number reproduces inside
2%. **What did change is what one extractor costs**: solo node CPU fell **74.7% → 43.3%** and busy
logical cores **5.98 → 3.47**, because `phaze-rvcn` stopped TF sizing its pools from the core count,
and per-process peak RSS fell **2.151 → 1.332 GiB (−38.1%)**. Those are real wins and they are *not*
throughput wins: the same four physical cores still set the ceiling, and the second process's threads
simply take the hyperthread siblings the first one used to occupy — which is why the curve is
unchanged while the per-process cost halved. **The 4×-oversubscription question resolves against
throughput:** W=4 at intra-op 4 on 4 physical cores buys **+9.2% over W=2 for +83.6% per-file wall**,
so it is a latency-for-throughput trade, deliberately taken, not free capacity. The joint intra-op ×
pod-count surface was probed at four thread widths and is **flat within 3.6% at the W=4 operating
point**; the best point anywhere in the sweep is intra-op 2 at W=8, **30.9 f/h — 1.0% above the
derived default's own best**, for twice the pods. Nothing is left on the table by keeping the
derivation.
Memory is now nowhere near binding: per-process peak is flat at **1.282–1.332 GiB across the entire
sweep**, node RSS grows **+0.880 GiB per worker**, and the memory wall extrapolates to **W≈33 —
sixteen times past where CPU stops it**. At the recommended `cap = 4` the node peaked at **5.755 GiB
used with 25.554 GiB still available**. Recommendation: **`cap = 4`, unchanged, confirmed.**

______________________________________________________________________

## 1. Method — `phaze-3j67`'s harness, reused

Reproduced deliberately so the two sweeps are directly comparable; every deviation is called out.

| | |
| --- | --- |
| **Host** | `vox` — Debian 13 (trixie), Xeon E3-1271 v3, **4 physical cores / 8 logical (SMT)**, 31.31 GiB total, k0s burst node, **out of the phaze backend registry** for the whole run and left that way, otherwise idle |
| **Runtime** | deployed job image `job:2026.8.0`, Python 3.14.6, `essentia-tensorflow` 2.1-beta6-dev, numpy 2.5.1, with `release/2026.8.1-prep`'s `analysis.py` + `analysis_sizing.py` overlaid onto a `/scratch/src` copy (§1c) |
| **Models** | the deployed `phaze-models` PVC, mounted **read-only** |
| **Audio** | **synthesized with ffmpeg** — `phaze-esut`'s generator verbatim, stereo 44.1 kHz sine pairs at 192 kbps: 36 **distinct** files (12 worker slots × {180, 300, 420} s) plus 8 × 1200 s for §8 |
| **Process model** | **one exec'd child process per file**, exactly as production. Workers are independent; each runs its files sequentially |
| **Per-process peak** | the child reads its own `/proc/self/status:VmHWM` **once, at exit** — a kernel high-water mark, not a sampled curve, so it is immune to the `phaze-7i0k` §9 GIL trap |
| **Node RSS + CPU curve** | a **host-side** sampler outside every container, 1 s cadence: `/proc/meminfo`, `/proc/stat`, and `VmRSS`/`VmHWM` of every matching `/proc/<pid>` |
| **Safety** | the sampler trips an abort file if node `MemAvailable` falls below 2 GiB. It never fired; the minimum reached anywhere in the sweep was **18.74 GiB** |

**No operator media was read, copied, or referenced.** Every input is a synthesized sine pair. No
filename, path, or per-file metadata value from the library appears in this document. `k0s`,
JuiceFS and the gateway configuration were not touched.

### 1a. The design that makes levels comparable

Unchanged from `phaze-3j67` §1a. Every worker, at **every** concurrency level, does **exactly the
same work**: one 180 s, one 300 s and one 420 s file — 900 s of audio, 3 files. Level *W* runs 3*W*
files over *W* workers, so `files/hour` is directly comparable across levels with no mix effect to
correct for. Each worker owns a **distinct** copy of each duration so no two workers ever read the
same inode, and the start order is rotated per worker (`w % 3`) so the levels are not phase-locked
into all-decoding-then-all-inferring. The model PVC and the corpus are warmed into page cache
identically before every level.

### 1b. What "concurrency" means here

*W* concurrent analyze **processes on one node** — which is what the deployment's Kueue `cap`
ultimately buys. They run as *W* children inside one pod rather than *W* pods, the same substitution
`phaze-7i0k` §6c and `phaze-3j67` §1b used and sound for the same reasons. In production one pod
runs one analyze process (`analysis_sizing.derive_sizing()` returns `concurrency = 1` on this node,
§2), so *W* children ↔ *W* pods is the right mapping. What this does **not** cover is Kueue
admission latency or image-pull time.

The host sampler confirms the substitution held: `analyze_concurrency_max` equals *W* exactly at
every level of every arm — the workers really were co-resident, not accidentally serialized.

### 1c. The overlay, and the proof it took

The deployed image predates all three changes under test, so measuring it directly would answer the
wrong question — and a *silent* fallback to its own module would answer the wrong question while
looking right. That failure mode is closed at three separate points:

| check | where | result |
| --- | --- | --- |
| the image's own `analysis.py` | inside the container | `512f0743736389c2565c170698626d2bb235b2df67c82a1cb6fa7df9952e2db6` |
| the image's `analysis_sizing.py` | inside the container | **absent** — the file does not exist in `job:2026.8.0`, which is `phaze-rvcn` being post-2026.8.0, stated as a fact rather than inferred |
| the overlaid `analysis.py` | inside the container | **`a8c30496686dab41145af0d1095aaaea9ea82cb84825efb52b6b760a46838032`** — byte-identical to `release/2026.8.1-prep:src/phaze/services/analysis.py` |
| the overlaid `analysis_sizing.py` | inside the container | **`d56d2bcb7c1ab203680a4e8397ce4a156d4a2bc332dc74c6a94060c9626854e4`** — byte-identical to `release/2026.8.1-prep:src/phaze/services/analysis_sizing.py` |
| **the module object actually imported** | **inside every one of the 222 child processes** | `analysis.__file__` resolves to `/scratch/src/phaze/services/analysis.py` and re-hashes to `a8c30496…`; a child whose loaded module hashed to the image's `512f0743…`, or to anything other than the expected digest, **aborts with exit 4 instead of producing a number** |

The per-child digest is carried in every result record and folded into the level summary. **All
eighteen levels report exactly one observed digest, `a8c30496…`.** Nothing in this document was
measured against 2026.8.0 behaviour.

### 1d. Two deviations from `phaze-3j67`, both recorded

- **The sampler writes JSONL incrementally rather than dumping at exit.** The first launch of this
  sweep lost two levels' curves because bash sets `SIGINT` to `SIG_IGN` for background jobs started
  in a non-interactive script, so the reaper's `kill -INT` was a no-op and the sampler only ever
  died to the follow-up `kill -9`, taking its in-memory samples with it. The fix is both halves:
  `SIGTERM` (which is *not* ignored) plus a line-buffered incremental write, so the curve no longer
  depends on how the process dies. Those two levels were **re-run from scratch**, not patched; the
  sweep reported here is one continuous run from a settled idle node.
- **The sampler matches on a marker string the driver and the `kubectl exec` wrapper also carry.**
  Those wrappers are separated out post hoc by peak size. The two populations are an order of
  magnitude apart — analyze children peak at ≥1.28 GiB, the wrappers at ≤0.10 GiB — so the 0.3 GiB
  cut is unambiguous. It is also independently validated: the host-observed `VmHWM` maximum equals
  the child's own self-reported `VmHWM` **to the last digit at every level of every arm**.

______________________________________________________________________

## 2. The premise that changed: one extractor no longer fills the node

`phaze-3j67` §2's central finding was that the extractor is **not** single-threaded — TF sized its
intra-op pool from the core count, so one process consumed ~6.2 of 8 logical cores. `phaze-rvcn`
pinned those pools. Solo, one analyze process on the otherwise idle node, node CPU sampled from the
host at 1 s across the level's own window:

| | `phaze-3j67` (TF defaults) | **this spike (derived sizing)** | change |
| --- | ---: | ---: | ---: |
| node CPU, W=1 | 74.7% | **43.3%** | **−31.4 pp** |
| busy logical cores, W=1 | 5.98 | **3.47** | **−42.0%** |
| per-file wall, W=1 (mix mean) | 163 s | **165 s** | +1.2% |
| per-process peak RSS | 2.074 GiB | **1.308 GiB** | **−36.9%** |
| files/hour, W=1 | 22.0 | **21.5** | −2.1% |

Every child logged its own derivation, and all 222 agree:

```
physical_cores=4 (sysfs:thread_siblings_list) -> intra_op=4 inter_op=1 omp=4 concurrency=1
```

**One extractor now consumes 3.47 of the node's 8 logical cores instead of 6.2 — for the same wall
clock.** Four pinned intra-op threads land on four distinct physical cores; the eight-thread default
was spilling onto hyperthread siblings that return roughly half a core each (§4). That is a genuine
efficiency win and it is worth stating plainly, because it is also the reason the *curve* does not
move: the cores one process stopped occupying are SMT siblings, and the second process's threads go
straight back onto them.

______________________________________________________________________

## 3. The sweep — **the knee is still at 2**, and the plateau is 30.6 files/hour

Concurrency 1 → 12 on `release/2026.8.1-prep`'s derived sizing (intra-op 4 / inter-op 1 / OMP 4),
which is what a deployed pod gets with no operator env set. Every row is 3*W* files of identical
mix.

| W | wall (s) | files | **files/h** | speedup | efficiency | per-file wall (s) | inflation | proc peak (GiB) | node RSS peak (GiB) | min avail (GiB) | node CPU | busy cores |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 501 | 3 | **21.5** | 1.00× | 100% | 165 | 1.00× | 1.308 | 3.17 | 28.14 | 43.3% | 3.47 |
| **2** | 791 | 6 | **27.3** | **1.27×** | 63% | 261 | 1.58× | 1.282 | 4.09 | 27.21 | 85.4% | 6.83 |
| 3 | 1109 | 9 | **29.2** | 1.36× | 45% | 366 | 2.22× | 1.284 | 5.01 | 26.30 | 95.9% | 7.67 |
| 4 | 1448 | 12 | **29.8** | 1.38× | 35% | 478 | 2.90× | 1.330 | 5.75 | 25.55 | 98.5% | 7.88 |
| 6 | 2137 | 18 | **30.3** | 1.41× | 23% | 706 | 4.28× | 1.332 | 7.87 | 23.43 | 99.4% | 7.95 |
| 8 | 2832 | 24 | **30.5** | 1.42× | 18% | 936 | 5.67× | 1.312 | 9.58 | 21.73 | 99.7% | 7.97 |
| 10 | 3535 | 30 | **30.5** | 1.42× | 14% | 1168 | 7.08× | 1.327 | 11.43 | 19.88 | 99.7% | 7.98 |
| 12 | 4234 | 36 | **30.6** | 1.42× | 12% | 1400 | 8.49× | 1.327 | 12.57 | 18.74 | 99.8% | 7.98 |

```
files/hour vs concurrency  —  NEW (release/2026.8.1-prep, derived sizing)
                              vs phaze-3j67 (2026-08-05, TF defaults)

  W=1  new |##################################..............   21.5
       old |##################################..............   22.0
  W=2  new |###########################################.....   27.3   <- knee (both)
       old |###########################################.....   27.5
  W=3  new |##############################################..   29.2
       old |#############################################...   28.9
  W=4  new |###############################################.   29.8   <- cap
       old |##############################################..   29.4
  W=6  new |################################################   30.3
       old |###############################################.   29.8
  W=8  new |################################################   30.5
       old |###############################################.   30.0
  W=10 new |################################################   30.5
       old |###############################################.   30.1
  W=12 new |################################################   30.6
       old |###############################################.   30.2
```

**The knee is at W=2. It did not move.** The marginal gain per step collapses by a factor of three
at exactly the same place it did before:

| step | marginal gain | cumulative speedup |
| --- | ---: | ---: |
| W 1 → 2 | **+5.77 f/h (+26.8%)** | 1.268× |
| W 2 → 3 | +1.89 f/h (+6.9%) | 1.356× |
| W 3 → 4 | +0.62 f/h (+2.1%) | 1.384× |
| W 4 → 6 | +0.49 f/h (+1.6%) | 1.407× |
| W 6 → 8 | +0.19 f/h (+0.6%) | 1.416× |
| W 8 → 10 | +0.04 f/h (+0.1%) | 1.418× |
| W 10 → 12 | +0.06 f/h (+0.2%) | 1.421× |

Of the **9.07 files/hour** that concurrency buys in total, **63.7% arrives at W=2**, **84.6% by
W=3** and **91.3% by W=4** — against `phaze-3j67`'s 67% / 84%. Going from 2 to 12 workers buys
**+12.1%**; from 4 to 12 it buys **+2.6%**, which is inside a single level's run-to-run noise.

Three things beyond the headline:

- **Per-file wall still inflates linearly with W past the knee.** 165 → 261 → 366 → 478 → 706 →
  936 → 1168 → 1400 s: **+113.2 s per added worker, R² 0.9996** across all eight points, and
  **+115.2 s, R² 1.0000** over W ≥ 4 alone. `phaze-3j67` measured +115.9 s at R² 0.9997. Same
  saturated queue, same slope to within 2.3%. At W=12 a 7-minute file takes **32.8 minutes**.
- **The cores go flat before the throughput does — but later than they used to.** Busy logical
  cores by level: 3.47, 6.83, 7.67, 7.88, 7.95, 7.97, 7.98, 7.98 out of 8. The node reaches 99.9%
  of its CPU from W=6 rather than W=4, because each process now asks for half as many threads. The
  throughput curve does not care.
- **Per-process peak RSS is flat in W**: 1.282 → 1.332 GiB across a 12× change in co-residency, a
  **3.9% spread with no trend**. `phaze-7i0k` §6c measured this at 3-way, `phaze-3j67` §3 at 12-way
  on the model-major code, and it survives `phaze-0582` and `phaze-rvcn` at 12-way again. The
  *level* moved (−38.1%); the *flatness* did not.

______________________________________________________________________

## 4. Why it did not move — the node still has four physical cores

`phaze-3j67` §4's explanation survives its own premises changing, which is the strongest thing that
can be said for it. Throughput per **busy logical core**, every arm and level in this spike:

| arm | W | intra-op | busy logical cores | files/h | **files/h per busy logical core** |
| --- | ---: | ---: | ---: | ---: | ---: |
| B | 2 | 2 | 3.85 | 25.7 | **6.67** |
| D | 4 | 1 | 4.15 | 27.6 | **6.65** |
| A | 1 | 4 | 3.47 | 21.5 | **6.22** |
| B | 4 | 2 | 7.46 | 30.1 | 4.04 |
| A | 2 | 4 | 6.83 | 27.3 | 4.00 |
| B | 8 | 2 | 7.96 | 30.9 | 3.88 |
| A | 12 | 4 | 7.98 | 30.6 | 3.84 |
| A | 8 | 4 | 7.97 | 30.5 | 3.83 |
| A | 4 | 4 | 7.88 | 29.8 | 3.78 |
| C | 1 | 8 | 6.35 | 21.8 | 3.43 |

The population still splits cleanly in two, and the cut is at the **physical** core count rather
than at any concurrency. Every configuration whose runnable threads stay **at or below 4** returns
**6.2–6.7 files/hour per logical core**; everything that spills onto hyperthread siblings returns
**3.4–4.0** — almost exactly half. That is SMT measured from outside, and it is the whole
explanation for a knee at 2: **one process with 4 pinned threads already occupies the four physical
cores, and the second process's threads can only be given siblings worth about half a core each.**

The cleanest single demonstration is arm C at **W=1**: one process with an 8-wide pool spreads over
**6.35** busy logical cores and returns **3.43 f/h per core** — the *saturated* rate — while
delivering the same 21.8 f/h that arm A gets from **3.47**. Nothing about that is a concurrency
effect; it is one process paying the SMT tax on its own. Which is exactly why `phaze-rvcn`'s pinning
freed 42% of the node's logical cores (§2) without freeing any throughput to go with them.

The ceiling is therefore still ~4 physical cores' worth of analysis, and every arm swept to
saturation converges there:

| best result per arm | files/h |
| --- | ---: |
| **intra-op 2, W=8** | **30.9** |
| derived (intra-op 4), W=12 | 30.6 |
| intra-op 8 (the pre-`phaze-rvcn` width), W=4 | 29.1 |
| intra-op 1, W=4 (its only point, and not a saturating one — 51.9% node CPU) | 27.6 |

**The three widths swept to saturation span 29.1–30.9 f/h, a 6.3% band**, against `phaze-3j67`'s
7% across its own arms. The node's throughput is a property of its silicon: three code changes that
between them cut long-file decode cost 17.9×, cut per-process memory 38%, and halved the thread
footprint moved it by **+1.4%**.

______________________________________________________________________

## 5. The 4×-oversubscription question, answered

The bead's sharp version: with intra-op pinned at 4 on a 4-physical-core node, W=4 is 16 threads
against 4 cores. Does that buy throughput now the work is inference, or only latency?

**It buys some of both, and mostly latency.** Priced from the measured curve:

| from → to | throughput | per-file wall | trade |
| --- | ---: | ---: | --- |
| W=1 → W=2 (8 threads / 4 cores, 2×) | **+26.8%** | +58.0% | the knee — worth taking |
| W=2 → W=3 (12 threads, 3×) | +6.9% | +40.4% | marginal |
| W=2 → W=4 (16 threads, **4×**) | **+9.2%** | **+83.6%** | the `cap` decision |
| W=4 → W=8 (32 threads, 8×) | +2.3% | +95.6% | bad trade |
| W=4 → W=12 (48 threads, 12×) | +2.6% | +192.5% | far past useful |

So the 4× oversubscription at the recommended cap is **not free capacity** — it is a deliberate
purchase of **+9.2% aggregate throughput for +83.6% per-file latency**, made because a burst lane's
job is to drain a backlog, not to minimize any single file's turnaround. It is worth naming this
explicitly because `phaze-rvcn`'s derivation says the *non-oversubscribed* point is W=1
(`physical_cores // intra_op`), and the two numbers are answering different questions: the
derivation sets phaze's **internal lane concurrency** (threads inside one pod, where oversubscribing
is a correctness-adjacent memory risk), while `cap` sets **how many pods Kueue admits**, where the
oversubscription is bounded, measured, and paid for in latency the operator has chosen to spend.
`docs/k8s-burst.md` already says exactly this; this spike supplies the measurement it was citing
from a different workload.

What the oversubscription decidedly does **not** buy is a moved knee. Inference being
multi-threaded does not create cores.

______________________________________________________________________

## 6. The intra-op × pod-count joint surface

`phaze-rvcn` measured the intra-op knee at *fixed* concurrency; the bead asked whether a lower
intra-op with more pods might now win. Probed at four thread widths (1, 2, 4, 8) against the
derived default:

| arm | intra-op | W | threads (nominal) | files/h | vs arm A at same W | per-file wall | node CPU | busy cores |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **A** | 4 (derived) | 1 | 4 | 21.5 | — | 165 s | 43.3% | 3.47 |
| **A** | 4 | 2 | 8 | 27.3 | — | 261 s | 85.4% | 6.83 |
| **A** | 4 | 4 | 16 | 29.8 | — | 478 s | 98.5% | 7.88 |
| **A** | 4 | 8 | 32 | 30.5 | — | 936 s | 99.7% | 7.97 |
| B | 2 | 2 | 4 | 25.7 | **−6.1%** | 278 s | 48.1% | 3.85 |
| B | 2 | 4 | 8 | 30.1 | **+0.9%** | 474 s | 93.2% | 7.46 |
| B | 2 | 8 | 16 | **30.9** | **+1.3%** | 923 s | 99.5% | 7.96 |
| C | 8 | 1 | 8 | 21.8 | +1.2% | 163 s | 79.4% | 6.35 |
| C | 8 | 4 | 32 | 29.1 | **−2.5%** | 490 s | 98.5% | 7.88 |
| D | 1 | 4 | 4 | 27.6 | −7.4% | 518 s | 51.9% | 4.15 |

**Answer: the surface is essentially flat at the ceiling, and the derivation is on it.** Three
things fall out, and they should be kept apart:

1. **A lower intra-op with more pods does not beat 4-with-fewer in any way worth acting on.** The
   best point anywhere in this spike is intra-op 2 at W=8 — **30.9 f/h, +1.3%** over the derived
   default at the same W and **+1.0%** over the derived default's own best (30.6 at W=12). That is
   inside run-to-run noise, and buying it costs twice the pods.
1. **At equal nominal thread count, more processes beat wider pools — but only below saturation.**
   At 8 nominal threads: intra-op 2 × W=4 gives **30.1 f/h** against intra-op 4 × W=2's **27.3** and
   intra-op 8 × W=1's **21.8**. Independent processes fill the sync gaps inside one TF op that a
   wide pool leaves idle, which is visible as busy cores (7.46 vs 6.83 vs 6.35) rather than as any
   change in the ceiling. Once the node is at 99% CPU the effect disappears.
1. **The pre-`phaze-rvcn` width is now measurably worse at the operating point.** Arm C is the
   direct in-session control for `phaze-3j67`'s baseline: at W=1 it reproduces that spike's
   published 22.0 f/h to **−0.9%** (21.8 f/h, 163 s per file), which is the cleanest available
   evidence that the two sweeps are measuring the same node the same way. At W=4 it is **−2.5%**
   against the derived sizing, having used **+83% more busy cores at W=1** to get there.

At the operating point itself the three pinned widths span **29.07–30.11 f/h, a 3.6% spread**, with
intra-op 1 a further **8.3%** below the best of them. Whatever the operator does with threads, the
node delivers ~30 files/hour.

**Scope note.** Arm B was probed at W = 2, 4, 8 rather than the full eight-point sweep, and arms C
and D at two points and one. The full sweep was spent on the arm that decides the `cap` — the
shipping default. The joint surface is therefore *probed*, not mapped; what it establishes is that
no thread width tested moves the ceiling by more than 6.3%, nor the W=4 operating point by more
than 3.6%, which is enough to close the question the bead asked.

______________________________________________________________________

## 7. Which binds first — **CPU, at W=2, by a factor of sixteen**

| | binds at | measured evidence |
| --- | ---: | --- |
| **CPU** | **W=2** | node CPU 43.3% at W=1, **85.4% at W=2**, 95.9% at W=3, ≥98.5% from W=4, 99.8% at W=12. Throughput gains 63.7% of its total at W=2 and 84.6% by W=3. Per-file wall then rises **+113.2 s per added worker, R² 0.9996** |
| **Memory** | **W≈33** | per-process peak flat at 1.282–1.332 GiB; node RSS 3.17 GiB at W=1 → 12.57 GiB at W=12, a slope of **+0.880 GiB/worker (R² 0.9965)** on a 2.372 GiB intercept; **`MemAvailable` never fell below 18.74 GiB**; the 2 GiB abort guard never armed; swap use flat at its idle 0.1885 GiB at **every level of every arm** |

**Memory has gone from "not the constraint" to "not in the same postcode as the constraint."**
`phaze-3j67` put the wall at W≈13 by worst-case per-process peak; `phaze-0582` and `phaze-rvcn`
between them cut the per-worker node-RSS slope **1.41 → 0.880 GiB (−37.6%)**, which moves the wall
to **W≈33**. CPU has been saturated since W=2. The ratio between the two constraints is now
**16×**.

______________________________________________________________________

## 8. The long-file regime — where `phaze-5lop`'s win actually landed

`phaze-3j67` §8 measured 20-minute files pre-`phaze-5lop` and found the node only **65.1%** busy
solo, because `EasyLoader`'s non-seeking decode gave long files a large single-threaded share. That
serializer is what the bead expected to have removed the knee. One 1200 s file per worker, derived
sizing:

| W | files/h | speedup | per-file wall | node CPU | busy cores | proc peak (GiB) | node RSS peak | audio-s analyzed per hour |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 5.42 | 1.00× | 662 s | **43.4%** | 3.47 | 1.436 | 3.34 | 6 500 |
| **2** | 6.75 | **1.25×** | 1 063 s | 86.4% | 6.91 | 1.438 | 4.53 | 8 099 |
| 4 | 7.49 | 1.38× | 1 919 s | 97.4% | 7.79 | 1.441 | 6.90 | 8 988 |
| 8 | 7.59 | 1.40× | 3 780 s | 99.7% | 7.97 | 1.452 | 11.57 | 9 110 |

**The long-file knee is at W=2 as well** — **61.2%** of the total gain arrives there, and W=4 → W=8
buys **+1.4%** for **+97.0%** per-file wall (a 20-minute file takes **63.0 minutes**). Same shape,
same place, on a workload whose per-file cost is **4.01×** the short mix's.

Old vs new at the two points `phaze-3j67` measured:

| | `phaze-3j67` (pre-5lop) | **this spike** | change |
| --- | ---: | ---: | ---: |
| per-file wall, 1200 s file, W=1 | 776 s | **662 s** | **−14.7%** |
| node CPU, W=1 | 65.1% | **43.4%** | −21.7 pp |
| files/h, W=4 | 6.92 | **7.49** | +8.2% |
| speedup W=1 → W=4 | 1.49× | **1.38×** | −7.4% |
| proc peak, W=4 | 2.301 GiB | **1.441 GiB** | **−37.4%** |

**The serializer really is gone, and its disappearance is exactly why the knee did not move.** The
tell is that long files now leave the node at **43.4%** solo — *identical* to the short mix's
**43.3%** — where `phaze-3j67` measured 65.1% against 74.7%, i.e. a duration-dependent
single-threaded share. That share is now zero to measurement precision. The consequence is the
opposite of the bead's hypothesis: with no idle single-threaded time left to fill, concurrency has
**less** to recover on long files than it used to (**1.38× at W=4 against 1.49×** before), and the
long-file curve converges onto the short-file curve rather than diverging from it. Measured in the
mix-independent unit, the node delivers **8 988 audio-s/h on 20-minute files against 8 948 on the
short mix at the same W=4** — within **0.5%**, against 6% before.

Per-file cost per second of audio is correspondingly flat: solo, wall ÷ duration is **0.552 /
0.548 / 0.550** for 180/300/420 s and **0.552 for 1200 s** — a 0.7% spread across a 6.7× range of
duration, where `phaze-3j67` measured 0.52 / 0.54 / 0.56 and **0.65**. `phaze-esut` §8's
O(n_windows × duration) penalty has been removed, not merely reduced.

______________________________________________________________________

## 9. Memory at the recommended cap, and what the `phaze-6ck1` population does to it

### 9a. Node peak at W=4, with system headroom accounted

| | GiB |
| --- | ---: |
| node capacity / allocatable | 31.31 / **31.21** |
| k0s + kubelet + containerd + coredns + kube-proxy + kube-router + metrics-server + kueue + local-path, measured at idle | **≈2.0** (the node-RSS regression's intercept, 2.372, includes this harness's own driver + `kubectl exec`) |
| **measured node RSS peak at W=4** | **5.755** |
| **measured minimum `MemAvailable` at W=4** | **25.554** |
| 4 analyze pods at the **measured** per-process peak (1.330 GiB) | 5.32 |
| 4 analyze pods at their **4Gi limit** (worst case the cgroup permits) | 16.0 |
| **free at the limit-worst-case** | **≈13.2** |

At the recommended cap the node used **5.755 GiB of 31.31** and never dropped below **25.554 GiB
available** — 18% of capacity, with 82% spare. Even at W=12, three times the cap, `MemAvailable`
held **18.74 GiB**. Swap sat at its idle **0.1885 GiB** at every level of every arm; nothing ever
paged.

This spike did **not** re-measure the envelope maximum (`coarse_cap` saturated at 30 windows), so
the `memory_request: 3Gi` / `memory_limit: 4Gi` sizing is **inherited, not re-derived** — from
`phaze-5lop`'s shipped-pipeline figure of **1.7383 GiB** for a 60-minute file at saturated caps,
which is the current entry in `docs/k8s-burst.md`. The largest per-process peak measured anywhere
in this sweep is **1.4522 GiB** (arm E, 1200 s, W=8) — comfortably inside it, and 36% of the 4Gi
limit. Nothing here argues to change those two numbers, and this spike is not the instrument for
it.

### 9b. Does the `phaze-6ck1` growth population change the safe cap?

**No — and the reason is that the cap is not the lever that governs it.** Stated explicitly because
the bead asked for it either way.

N pods each capable of an excursion *is* a different risk calculation from one, but only while the
excursion is unbounded. With `resources.limits.memory: 4Gi` in place (`phaze-k6d5`), each pod's
blast radius is capped at 4 GiB **whatever the mechanism turns out to be** — proportional,
additive, or something `phaze-6ck1` has not yet named. Four such pods is **16 GiB against 31.21 GiB
allocatable and ~2 GiB of k0s stack: ~13.2 GiB still free with every pod simultaneously pinned at
its ceiling**, and a fifth is refused by the Kueue quota before it is scheduled. The failure mode
that started this line of work — `oom-kill:constraint=CONSTRAINT_NONE`, the node-scoped kill — is
therefore unreachable at `cap = 4`, and would remain unreachable at `cap = 6`.

The limit is what makes that true, and it is load-bearing rather than belt-and-braces. Without it,
four pods each free to reach a 4× multiplicative excursion on the measured 1.33 GiB working set is
4 × 5.32 = **21.3 GiB**, which fits but leaves only ~7.9 GiB; against the inherited 2.57 GiB
envelope maximum it is 4 × 10.3 = **41 GiB, more than the node has**. So: **`cap = 4` is safe
because the operator sets `memory_limit`, not because 4 is a small number.** On a deployment that
leaves `memory_limit` unset (the code default, per ADR-0005) the same reading caps safe concurrency
at 3 and realistically at 2 — unchanged from `phaze-3j67` §7a, and unchanged by anything measured
here.

Two honest qualifications. `phaze-6ck1` remains open, so the multiplicative reading could be wrong;
a pod-scoped limit covers the additive case too, which is precisely why it is the right instrument.
And this spike did not reproduce the population — 222 analyze processes ran to completion with a
per-process peak spread of 3.9% and no outliers at all, which is consistent with a ~4-in-520
incidence and constitutes no evidence either way.

______________________________________________________________________

## 10. Recommended `cap` for `homelab-a2x`

**`cap = 4`. Unchanged, and now derived from a measurement of the code that will be deployed.**

| cap | files/h | % of node maximum | per-file wall | node RSS peak (measured) | min avail (measured) |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 27.3 | 89% | 261 s | 4.09 GiB | 27.21 GiB |
| 3 | 29.2 | 95% | 366 s | 5.01 GiB | 26.30 GiB |
| **4** | **29.8** | **97%** | **478 s** | **5.75 GiB** | **25.55 GiB** |
| 6 | 30.3 | 99% | 706 s | 7.87 GiB | 23.43 GiB |
| 8 | 30.5 | 100% | 936 s | 9.58 GiB | 21.73 GiB |
| 12 | 30.6 | 100% | 1 400 s | 12.57 GiB | 18.74 GiB |

`cap` should be **the smallest concurrency that reaches the node's throughput ceiling**, because
every worker past that point multiplies per-file latency for nothing. **4 buys 97% of the ceiling
at a third of cap 12's per-file latency.** cap 6 buys **+1.6%** throughput for **+47.6%** per-file
wall and cap 8 buys **+2.3%** for **+95.6%** — both bad trades for a burst lane whose purpose is to
shorten the tail. Dropping to cap 3 would give back **2.1%** of throughput for a **23.6%** latency
improvement, which is defensible but strictly worse on the axis the lane exists to serve; cap 2
gives back 8.4% and is not.

The rest of the `phaze-3j67` §9 package is **unchanged and not re-litigated here**: `cpu_request
1500m`, `memory_request 3Gi`, `memory_limit 4Gi`, `vox_kueue_cpu_quota 6`, `vox_kueue_mem_quota
12Gi`. The two lockstep constraints still land exactly:

```
memory:  cap 4 x memory_request 3Gi  = 12Gi  <= vox_kueue_mem_quota 12Gi     (a 5th pod needs 15Gi — refused)
cpu:     cap 4 x cpu_request 1500m   = 6     <= vox_kueue_cpu_quota 6        (a 5th pod needs 7.5 — refused)
```

**The `TF_NUM_INTRAOP_THREADS` / `TF_NUM_INTEROP_THREADS` / `OMP_NUM_THREADS` ConfigMap entry that
`phaze-3j67` recommendation 2 asked for is no longer needed and should not be set.**
`phaze-rvcn` moved that derivation into the code, `apply_thread_env` runs it at import, and every
one of the 222 children in this sweep derived `4 / 1 / 4` from the host with nothing set. Setting
the env would pin vox to values the code already produces while *breaking* the portability the
derivation exists to provide on any future burst node.

______________________________________________________________________

## 11. What this measurement does and does not support

- **Supported:** aggregate throughput, per-file wall, per-process peak RSS, node RSS and node CPU
  across concurrency 1–12 on this node, for `job:2026.8.0` + the `release/2026.8.1-prep` overlay +
  this model set, under four thread configurations and two duration regimes. The binding resource
  and the concurrency at which it binds. The physical-core explanation, re-derived independently
  and reproducing `phaze-3j67`'s 2:1 throughput-per-logical-core split.
- **Supported: that the two sweeps are comparable.** Arm C at W=1 is the pre-`phaze-rvcn` thread
  width run in this session and reproduces `phaze-3j67`'s published W=1 baseline to **−0.9%** on
  throughput and **±0.0%** on per-file wall; arm D at W=4 reproduces its arm C to **+1.5%**. The
  old-vs-new comparison is not resting on a two-day-old number alone.
- **Not supported: any of this transferred to a different node.** The ceiling is 4 physical Haswell
  cores. `cap` is a **per-backend** setting and each cluster in the multi-cluster mesh must be sized
  from its own hardware. What transfers is the shape: CPU binds long before memory, so size `cap`
  from physical cores and `memory_request` from the measured peak.
- **Not supported: a re-derivation of `memory_request` / `memory_limit`.** The envelope maximum was
  not re-measured (§9a); those numbers are inherited from `phaze-5lop` and remain that spike's.
- **Not supported: any claim about `phaze-6ck1`.** §9b re-prices the risk under a bounded-limit
  reading; it does not identify the mechanism and did not reproduce the population.
- **Not supported: Kueue admission behaviour, image-pull time, or JuiceFS read throughput.** The
  harness runs *W* exec'd children in one pod against a page-cache-warm local corpus (§1b).
- **Not supported: a full map of the intra-op × pod-count surface.** It was probed at four widths
  (1, 2, 4, 8) across six points beyond the default arm (§6), which bounds the answer without
  charting it.
- **Synthetic audio is inherited, not re-validated.** `phaze-7i0k` §6b established that peak memory
  is content-independent because it is a function of window *shape*. Wall time does not
  automatically inherit that licence, so the throughput figures should be read as an **upper bound**
  on real-audio throughput — tight to the extent that TF inference dominates, which post-`phaze-5lop`
  it does more than ever (§8).
- **The node was idle and stayed out of the backend registry.** Loadavg was confirmed before each
  level and logged with it; no phaze workload was admitted at any point; k0s, JuiceFS and the
  gateway configuration were untouched. The first launch of the sweep was aborted for a harness bug
  (§1d) and every level reported here comes from the second, continuous run.

______________________________________________________________________

## 12. Recommendations

| | action | why |
| --- | --- | --- |
| 1 | **`cap = 4` — confirmed, not changed.** Proceed with `homelab-a2x` as planned | §10. 97% of the node's throughput ceiling at a third of cap 12's latency. The premise it rested on has now been re-measured on `release/2026.8.1-prep` rather than inherited from a decode-bound workload |
| 2 | **Do not set `TF_NUM_INTRAOP_THREADS` / `TF_NUM_INTEROP_THREADS` / `OMP_NUM_THREADS` in the `phaze-agent-env` ConfigMap** | §10. `phaze-rvcn` made this a runtime derivation; all 222 children derived `4 / 1 / 4` with nothing set. Pinning it would freeze vox's values onto every future burst node and undo the portability the module exists for. **This retires `phaze-3j67` recommendation 2** |
| 3 | **Stop expecting the concurrency curve to move when the analysis code gets faster** | §3, §4. Three changes cutting long-file decode 17.9×, per-process memory 38% and thread footprint 42% moved the plateau **+1.4%** and the knee **not at all**. The curve is a property of four physical cores |
| 4 | **Keep `memory_limit` set. It, not `cap`, is what bounds the `phaze-6ck1` risk** | §9b. Four pods bounded at 4Gi cannot reach a node-scoped OOM on a 31 GiB node; four unbounded pods can. The cap is a throughput/latency decision, not a safety one |
| 5 | **When throughput on this lane becomes the binding problem, buy cores** | §4, §7. 30.6 files/hour is 4 Haswell cores. Memory now binds at W≈33 against CPU at W=2 — a 16× gap. There is nothing left to win with RAM or with concurrency |
| 6 | **Re-measure the knee when the node changes, not when the code changes** | §4, §11. `cap` is per-backend and the ceiling is silicon. A burst node with more real cores has a proportionally higher ceiling and a knee at a different W |

______________________________________________________________________

## 13. What this changes upstream

- **`phaze-3j67` §3's knee (W=2) and §9a's `cap = 4`** — **both confirmed on the current code.**
  Not superseded, not amended. The plateau reads 30.6 rather than 30.2 f/h and the gain fraction at
  W=2 reads 63.7% rather than 67%; every conclusion drawn from those numbers stands.
- **`phaze-3j67` §2** — *"one extractor consumes ~6.2 of 8 logical cores"* — **superseded by
  `phaze-rvcn`, and re-measured here: 3.47.** The sentence was true of TF's defaults and is no
  longer true of the shipped code. The conclusion it supported (the extractor is not
  single-threaded, and one process fills the physical cores) survives intact.
- **`phaze-3j67` recommendation 2** (set the three thread env vars in the ConfigMap) — **retired.**
  `phaze-rvcn` moved it into `analysis_sizing.derive_sizing()`; setting it now would be a
  portability regression, not a memory win.
- **`phaze-3j67` §8** — *"long files are more expensive per second of audio, and the gap grows"* —
  **no longer holds.** Wall ÷ duration is now flat at **0.548–0.552** from 180 s to 1200 s, where
  that spike measured 0.52 → 0.65. `phaze-5lop` removed the O(n_windows × duration) decode penalty
  outright. The duration gate remains worth having for *exposure time*, on its own merits.
- **`docs/k8s-burst.md`'s "It does not re-measure concurrency"** (the `phaze-rvcn` caveat) — **now
  measured.** The concurrency half of `intra_op × concurrency ≈ physical_cores` no longer rests on
  a carried-over figure: the joint surface was probed (§6) and the derivation sits on the flat part
  of it. That section's characterisation of `cap = 4` as "a *deliberately oversubscribed* operating
  point trading per-file latency for aggregate throughput" is **exactly right**, and §5 supplies the
  price: **+9.2% throughput for +83.6% per-file wall**.
- **The bead's own hypothesis**, recorded because it was well-reasoned and the measurement went the
  other way: *"the serializer that produced the W=2 knee has largely been removed, so the curve has
  almost certainly moved."* The first clause is **confirmed** — decisively, in §8, where the
  duration-dependent single-threaded share is now zero. The second **does not follow**, because the
  serializer was never what set the knee; four physical cores were. Removing it made each file
  cheaper without making the node wider.

______________________________________________________________________

## Appendix A — every level measured

All 18 levels, 222 analyze child processes, **zero failures**, **one** observed `analysis.py`
digest (`a8c30496…`). Swap sat at its idle 0.1885 GiB in every row.

| arm | intra-op | W | mix | files | wall (s) | files/h | per-file (s) | proc peak (GiB) | node RSS peak (GiB) | min avail (GiB) | node CPU | busy cores |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 4 (derived) | 1 | 180/300/420 | 3 | 501 | **21.54** | 164.9 | 1.3075 | 3.17 | 28.14 | 43.3% | 3.47 |
| A | 4 (derived) | 2 | 180/300/420 | 6 | 791 | **27.32** | 260.5 | 1.2821 | 4.09 | 27.21 | 85.4% | 6.83 |
| A | 4 (derived) | 3 | 180/300/420 | 9 | 1109 | **29.21** | 365.7 | 1.2839 | 5.01 | 26.30 | 95.9% | 7.67 |
| A | 4 (derived) | 4 | 180/300/420 | 12 | 1448 | **29.83** | 478.5 | 1.3301 | 5.75 | 25.55 | 98.5% | 7.88 |
| A | 4 (derived) | 6 | 180/300/420 | 18 | 2137 | **30.32** | 706.4 | 1.3318 | 7.87 | 23.43 | 99.4% | 7.95 |
| A | 4 (derived) | 8 | 180/300/420 | 24 | 2832 | **30.51** | 935.8 | 1.3122 | 9.58 | 21.73 | 99.7% | 7.97 |
| A | 4 (derived) | 10 | 180/300/420 | 30 | 3535 | **30.55** | 1167.7 | 1.3269 | 11.43 | 19.88 | 99.7% | 7.98 |
| A | 4 (derived) | 12 | 180/300/420 | 36 | 4234 | **30.61** | 1399.8 | 1.3272 | 12.57 | 18.74 | 99.8% | 7.98 |
| B | 2 | 2 | 180/300/420 | 6 | 842 | **25.66** | 278.1 | 1.3031 | 4.29 | 27.02 | 48.1% | 3.85 |
| B | 2 | 4 | 180/300/420 | 12 | 1435 | **30.11** | 474.4 | 1.3079 | 6.06 | 25.25 | 93.2% | 7.46 |
| B | 2 | 8 | 180/300/420 | 24 | 2795 | **30.91** | 922.8 | 1.3074 | 9.51 | 21.80 | 99.5% | 7.96 |
| C | 8 | 1 | 180/300/420 | 3 | 496 | **21.79** | 163.0 | 1.3232 | 3.26 | 28.05 | 79.4% | 6.35 |
| C | 8 | 4 | 180/300/420 | 12 | 1486 | **29.07** | 489.9 | 1.2895 | 6.22 | 25.09 | 98.5% | 7.88 |
| D | 1 | 4 | 180/300/420 | 12 | 1564 | **27.62** | 517.7 | 1.2867 | 6.11 | 25.20 | 51.9% | 4.15 |
| E | 4 (derived) | 1 | 1200 | 1 | 665 | **5.42** | 662.4 | 1.4361 | 3.34 | 27.97 | 43.4% | 3.47 |
| E | 4 (derived) | 2 | 1200 | 2 | 1067 | **6.75** | 1063.5 | 1.4378 | 4.53 | 26.78 | 86.4% | 6.91 |
| E | 4 (derived) | 4 | 1200 | 4 | 1923 | **7.49** | 1919.3 | 1.4406 | 6.90 | 24.41 | 97.3% | 7.79 |
| E | 4 (derived) | 8 | 1200 | 8 | 3793 | **7.59** | 3780.2 | 1.4522 | 11.57 | 19.74 | 99.7% | 7.97 |

Per-duration solo wall (arm A, W=1): **99.4 s** at 180 s, **164.5 s** at 300 s, **230.9 s** at
420 s, **662.4 s** at 1200 s.

______________________________________________________________________

## Appendix B — reproducing this

Five short scripts, in the shape `phaze-esut`, `phaze-7i0k` and `phaze-3j67` established (drive the
**real** `phaze.services.analysis`; never reimplement the pipeline):

- **`analyze_one.py`** — one file, one process. Inserts `/scratch/src` at the front of `sys.path`,
  gates the overlay by sha256 **before** import and gates the loaded module object by sha256
  **after** import (aborting with exit 4 rather than measuring the wrong code), runs `analyze_file`
  against `/models`, and prints one JSON line carrying wall time, the derivation
  `analysis_sizing.derive_sizing()` produced, and its own `/proc/self/status:VmHWM` read once at
  exit.
- **`driver.py`** — spawns *W* worker threads, each `subprocess.run`-ing `analyze_one.py` once per
  file over its private `{180, 300, 420}` s triple (start order rotated by `w % 3`), and reports
  aggregate wall, `files/hour`, per-file wall, per-process peak and the **set of `analysis.py`
  digests observed** — which must be a single element. `--tf-intraop N` sets
  `TF_NUM_INTRAOP_THREADS=N TF_NUM_INTEROP_THREADS=1 OMP_NUM_THREADS=N` on the children only;
  `apply_thread_env` honours an operator-set value, so this overrides the derivation exactly the
  way a ConfigMap would.
- **`sampler.py`** — runs on the **host**, outside every container, at 1 s cadence: `/proc/meminfo`,
  `/proc/stat` deltas for node CPU, and `VmRSS`/`VmHWM` for every `/proc/<pid>` whose cmdline
  matches the child marker. Writes **JSONL incrementally** and handles `SIGTERM`; trips an abort
  file below 2 GiB `MemAvailable`.
- **`run_level.sh`** — warms the model PVC and corpus into page cache, records loadavg, starts the
  sampler, runs one level in the pod, **`SIGTERM`s and reaps** the sampler, kills any surviving
  in-pod driver on exit, idles 20 s between levels.
- **`aggregate.py`** — slices each level's sampler stream to that level's **own** start/end window
  so a sampler that outlived a level cannot contaminate the next one, and separates analyze children
  from the driver/`kubectl` wrappers by peak size (§1d).

The pod is a bare `sleep infinity` pod on the burst node using the deployed job image, with the
`phaze-models` PVC mounted **read-only**, a host scratch dir, and **no Kueue queue label** so it
consumes no quota:

```sh
kubectl -n phaze apply -f a pod:  image ghcr.io/simplicityguy/phaze/job:2026.8.0
                                  volumeMounts: phaze-models (ro) at /models, hostPath scratch at /scratch
                                  command: ["sleep", "infinity"]
```

The image ships the pre-`phaze-rvcn` tree — its `analysis.py` hashes to `512f0743…` and it has **no**
`analysis_sizing.py` at all — so the current pipeline is measured by copying `/app/src` to
`/scratch/src`, overwriting both files with `release/2026.8.1-prep`'s, and putting `/scratch/src`
first on `sys.path`. Same overlay technique `phaze-15sw`, `phaze-5lop` and `phaze-0582` used.

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

**Four things to get right when re-running.** Sample RSS from **outside** the process (`phaze-7i0k`
§9) — but note that a child reading its own `VmHWM` once at exit is *not* sampling and is not
subject to that trap; here the two agreed to the last digit at every level, which is worth
re-checking as a harness self-test. Give every worker the **same** duration mix, or `files/hour`
stops being comparable across levels. Warm the page cache identically before each level. And do not
send `SIGINT` to a sampler backgrounded from a non-interactive shell script — bash sets it to
`SIG_IGN`, and the first launch of this sweep lost two levels to exactly that (§1d).
