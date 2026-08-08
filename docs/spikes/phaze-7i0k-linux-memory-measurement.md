# phaze-7i0k — the Linux measurement `phaze-esut` §7 asked for

- **Bead:** `phaze-7i0k` (follow-up **C** of spike [`phaze-esut`](phaze-esut-analysis-memory-profile.md))
- **Date:** 2026-08-05
- **Tree:** branch `wt/bead/issue/phaze-7i0k`, forked off `main` at `9f33a44`
- **Code under test:** the deployed analyze image `ghcr.io/simplicityguy/phaze/job:2026.8.0`,
  unmodified, against the deployed `phaze-models` PVC (34 graphs, 3.1 GB)
- **Status:** measurement only. **No product code changed.**

______________________________________________________________________

## Verdict in one paragraph

**Linux is not worse than macOS — it is 25–39% better, and there is no ratchet.** The same
3.3-minute file that peaks at **9.734 GiB** on macOS peaks at **5.948 GiB** on Linux/glibc; the
30-coarse-window envelope maximum falls from **10.39 GiB** to **7.987 GiB** (repeated: 7.963).
**§7(a) is refuted on both of its halves, by trace and not by assertion.** Instantaneous RSS on
Linux is *not* monotone — a host-side 2-second trace of a 30-window run finds it below its own
running maximum in **96%** of 1042 samples, shedding up to **1.485 GiB** at a time and swinging
5.314 ↔ 7.962 GiB. And it does not climb toward the envelope maximum: the high-water **saturates
at 7.9 GiB by window 10** and moves +1.1% over the next twenty windows, with free-but-retained
arena bytes flat. Linux oscillates more shallowly than macOS (2.6 GiB against 4.9) with a higher
floor — glibc *does* retain more — but retention here makes Linux **cheaper**, not more expensive.
Three live production analyses of real audio on the same image, sampled from outside the process,
independently peaked at **7.919 / 7.928 / 7.956 GiB** with the same sawtooth shape — within 0.9%
of the synthetic figure, which is what makes a sine-wave measurement admissible for sizing. The
production **15.3–30.4 GiB** kill values are real (20 kernel OOM records) but are **not** produced
by the mechanism §7 proposed, and were not reproduced under any variant tested. §7(b)'s thread
hypothesis survives in reduced form: capping TF intra-op threads saves **14.4%**, and — unlike
macOS, where 1 thread cost 5.1× wall time — **the entire saving is available at 4 threads for
+8.2% wall time**.

______________________________________________________________________

## 1. Method

| | |
| --- | --- |
| **Host** | `vox` — Debian 13 (trixie), kernel 6.12.100, **glibc 2.41**, x86_64, **8 cores, 31.2 GiB allocatable**, k0s burst node, taken out of the phaze backend for the duration |
| **Runtime** | the deployed job image `job:2026.8.0` verbatim — Python 3.14.6, `essentia-tensorflow` 2.1-beta6-dev, the same wheel `pyproject.toml` pins |
| **Models** | the deployed `phaze-models` PVC mounted read-only: 34 graphs, 3.1 GB of `.pb` |
| **Audio** | **synthesized with ffmpeg** — the spike's sine-pair generator at 200 / 600 / 3600 / 43200 s, plus one onset-dense pink-noise + click-train file to test content dependence |
| **Peak metric** | `/proc/self/status:VmHWM`, cross-checked against `resource.getrusage(RUSAGE_SELF).ru_maxrss` (**on Linux `ru_maxrss` is KiB**, not bytes as on Darwin — the two agree to the byte in every run) |
| **Instantaneous RSS** | two independent samplers: in-process `/proc/self/statm`, and a **host-side sampler** at a fixed 2 s cadence (the in-process one is GIL-starved because essentia's bindings hold the GIL through inference — see §9) |
| **Allocator state** | `malloc_info(0, …)` XML parsed per mark: arena count, per-arena system bytes, free-but-retained bytes |
| **THP** | `AnonHugePages` from `/proc/self/smaps_rollup`; disabled per-process with `prctl(PR_SET_THP_DISABLE)` — **no host-wide `/sys` setting was touched** |

**No operator media was read, copied, or referenced.** Every input is synthesized. No filename,
path, or per-file metadata value from the library appears in this document. The production
figures in §6 are process-level RSS and kernel OOM records only.

The harness drives the **real** `phaze.services.analysis` internals — `_probe_duration_sec`,
`_analyze_fine_windows`, `_iter_windows`, `_stride_to_cap`, `_get_classifier`, `_predict_single`,
`_run_model_sets`, `analyze_file` — exactly as the spike's did (appendix).

______________________________________________________________________

## 2. Baseline peak — Linux against macOS

### 2a. Full pipeline, production caps (`fine_cap=60`, `coarse_cap=30`)

| file | duration | **macOS peak** | **Linux peak** | Linux ÷ macOS |
| --- | ---: | ---: | ---: | ---: |
| `dur_200` | 3.3 min | 9.734 GiB | **5.948 GiB** | **0.61×** |
| `dur_600` | 10.0 min | 9.165 GiB | **6.836 GiB** | **0.75×** |
| `dur_600_dense` (onset-dense) | 10.0 min | — | **6.670 GiB** | — |
| `dur_3600` | 60.0 min | 11.739 GiB | **7.949 GiB** | **0.68×** |
| `dur_43200`, caps 2 (2/1440 fine, 2/240 coarse) | 720.0 min | 9.033 GiB | **6.463 GiB** | **0.72×** |

The Linux peak is **lower at every point measured**, by 25–39%. The gap §7 set out to explain
does not exist in the direction it was assumed to run. Note in particular the row that carries the
macOS *maximum*: `dur_3600` is the most expensive file in the spike's sweep at 11.739 GiB, and it
is the file whose Linux peak (7.949 GiB) is **32% lower** — the excess was never a property of the
input.

Duration-independence survives unchanged, and the spike's discriminating test reproduces. Across a
**216× duration span** (200 s → 43200 s) the Linux peak moves within **5.9–8.0 GiB**, in no
consistent direction: the 12.1-hour file at 2 coarse windows peaks **lower** (6.463 GiB) than the
60-minute file at 20 (7.949 GiB), and the 3.3-minute file (5.948 GiB) is the cheapest of all. The
peak tracks *coarse window count*, not duration — exactly as on macOS, where the same four files
span 9.03–11.74 GiB in the same non-monotone order.

### 2b. Window count isolated at constant duration (the spike's §3c, rerun)

The same 600-second file, recycled through N production-shaped 180 s coarse inferences:

| n coarse windows | 1 | 2 | 3 | 5 | 10 | 16 | 20 | **30** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **macOS peak (GiB)** | 8.55 | 9.53 | 9.80 | — | 10.00 | 10.15 | 10.15 | **10.39** |
| **Linux peak (GiB)** | **5.809** | 6.578 | 6.847 | 7.109 | **7.898** | 7.898 | 7.911 | **7.987** |

Same logarithmic shape, same saturation, **2.4–2.7 GiB lower at every N**. One coarse window costs
5.81 GiB on Linux against 8.55 GiB on macOS; the entire 1→30 operating range adds **+2.18 GiB**
(macOS: +1.84 GiB) and **96% of that is paid by window 10**.

Refitting the spike's §3d predictor on these seven points:

```
peak_GiB  ≈  6.06  +  0.64 × ln( min( ceil(duration_sec / coarse_window_sec), coarse_cap ) )
```

R² 0.93, max |error| 0.37 GiB, range over every possible input **5.8–8.0 GiB** — against the
macOS form's 8.5–10.5 GiB.

### 2c. Stage attribution (the spike's §2, rerun on the 3.3-minute file)

| stage | macOS Δ high-water | **Linux Δ high-water** |
| --- | ---: | ---: |
| `import essentia` + TensorFlow | +0.228 | **+0.197** |
| `_probe_duration_sec` | +0.000 | **+0.000** |
| FINE pass, all 7 windows | +0.039 | **+0.034** |
| coarse `EasyLoader`, 180 s @ 16 kHz | +0.014 | **+0.000** |
| **`_run_model_sets`, window 0** | **+9.433** | **+5.420** |
| `_run_model_sets`, window 1 | +0.000 | **+0.000** |
| **total peak** | **9.734** | **5.670** |

**Structure is identical; only the magnitude of the one dominant stage differs.** One stage owns
**95.6%** of the Linux peak (macOS: 96.9%). Decode across both tiers contributes 0.034 GiB (0.6%).

### 2d. Per-model attribution (the spike's §2a, rerun)

| step | macOS | **Linux** |
| --- | ---: | ---: |
| all 34 graphs constructed, **zero inference** | **+4.090 GiB** (2.0 s) | **+3.995 GiB** (2.7 s) |
| first musicnn inference | +2.359 | **+0.869** |
| first vggish inference | +2.898 | **+0.000** |
| remaining 31 models | +1.9 combined | **+1.26 combined**, none > +0.263 |

**The avoidable term is platform-independent.** Graph residency — the 34 co-resident
`TensorflowPredict*` sessions that follow-up A removes — costs **+3.995 GiB on Linux against
+4.090 GiB on macOS**, a 2.3% difference. What *is* platform-dependent is the per-architecture
inference arena: 5.26 GiB on macOS versus 0.87 GiB on Linux. That single line accounts for
essentially the whole macOS-to-Linux gap, and it explains why Linux's floor is lower while its
*avoidable* fraction is proportionally larger.

______________________________________________________________________

## 3. The ratchet — **refuted**, on both halves, by the trace

§7(a) predicted that glibc's per-thread arenas would turn macOS's oscillation into "a **monotone
ratchet toward the envelope maximum**, then accumulate fragmentation." Both halves were testable
and both fail. The evidence is a 2-second-cadence, host-side RSS trace of a 30-window run — 1042
samples over 2137 s — not an assertion.

### 3a. Monotone: **refuted** — Linux oscillates too, just less deeply

| | macOS | **Linux (host-side trace)** |
| --- | ---: | ---: |
| instantaneous RSS range after window 1 | **3.256 ↔ 8.205 GiB** | **5.314 ↔ 7.962 GiB** |
| peak-to-trough swing | 4.949 GiB | **2.649 GiB** |
| largest single give-back (drawdown from running max) | ≈ 4.9 GiB | **1.485 GiB** |
| samples strictly below the running max | — | **96%** |

RSS on Linux spends **96% of the run below its own running maximum** and repeatedly sheds up to
**1.485 GiB** at once. Sampled every 90 s the curve reads 5.44, 5.26, 6.04, 6.77, 6.32, 6.21,
7.18, 6.69, 7.30, 6.40, 7.12, 6.47, 6.91, 6.45, 6.73, 7.45, 7.20, 6.43, 7.67, 6.72, 6.47, 7.48,
7.73 — a sawtooth, not a staircase. This is `munmap` of the large per-inference tensors, which
clear the dynamic mmap threshold and never enter an arena at all.

Two independent confirmations that the sawtooth is the real behaviour, not a harness artifact:
the three live **production** analyses of real audio (§6b) show the same shape with drawdowns of
**1.20 / 1.44 / 1.47 GiB**, and pinning the mmap threshold low (`MALLOC_MMAP_THRESHOLD_`, §5)
moves the peak by only −4.2% — there is little left for it to reclaim.

**The difference from macOS is one of depth, not of kind.** Linux's trough is **2.06 GiB higher**
(5.31 against 3.26) while its crest is slightly lower (7.96 against 8.21). That is what "glibc
retains more in arenas" looks like once it is *quantified*: about 2 GiB of extra retained floor,
not a monotone climb. One further asymmetry is worth noting: the Linux sampled crest (7.962 GiB)
is within **0.3%** of the true high-water (7.987 GiB), whereas the macOS sampled crest (8.205) sits
**2.2 GiB below** its high-water (10.39) — macOS's transients are sharper and shorter, which is
precisely the memory Linux's arenas hold instead of re-faulting.

### 3b. Toward the envelope maximum: **refuted**

| window | 1 | 2 | 3 | 5 | 7 | **10** | 15 | 20 | 25 | **30** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| RSS after (GiB) | 4.917 | 5.276 | 5.510 | 5.829 | 6.197 | **6.432** | 6.440 | 6.424 | 6.478 | **6.483** |
| VmHWM (GiB) | 5.809 | 6.578 | 6.847 | 7.109 | 7.567 | **7.898** | 7.898 | 7.911 | 7.986 | **7.987** |

The climb is **asymptotic, not linear**. Between window 10 and window 30 — two thirds of the
entire run — the high-water moves **+0.089 GiB (1.1%)**. The allocator reaches a working set and
reuses it. There is no fragmentation accumulation either: `malloc_info` reports free-but-retained
bytes flat at **0.59 GiB** from window 10 to window 30, with arena count constant at 20.

The whole 30-window run was repeated end to end and reproduced to **0.3%** (7.987 then 7.963 GiB).

**The correct statement is the opposite of the inference.** Retention is why Linux is *cheaper*
here: the ~2 GiB glibc holds back is ~2 GiB it does not have to re-fault, and macOS's 10.39 GiB
high-water counts transients that Linux's arena reuse absorbs. §7(a) had the right mechanism
pointed in the wrong direction, and attached to it a magnitude — "toward the envelope maximum",
i.e. 16–30 GiB — that the trace excludes by a factor of three.

### 3c. Why the first version of this measurement said the opposite

An in-process sampler thread reading `/proc/self/statm` reported a maximum drawdown of **0.081
GiB** — a clean monotone ratchet, exactly as predicted. It was wrong. essentia's Python bindings
hold the GIL across an inference call, so the sampler thread only ran *between* models: 280
samples in 2148 s, all taken at the same phase of the allocation cycle. The host-side sampler at a
fixed 2 s cadence sees 1042 samples and the sawtooth. **The confirmatory result was an artifact of
sampling the process from inside itself**, and it is recorded here because it would have produced
a confident, wrong "confirmed" verdict on the load-bearing question.

______________________________________________________________________

## 4. Transparent huge pages — measured, and not the mechanism

THP is `always` on this host and **5.55 GiB of the 6.48 GiB final RSS is `AnonHugePages`**, which
makes it the obvious suspect for a Linux-only inflation that macOS cannot have. It is not.

| | peak | wall |
| --- | ---: | ---: |
| THP as configured (`always`) — N=30 | **7.987 GiB** | 2148 s |
| THP disabled per-process — N=30 | **7.921 GiB** | 2380 s |

Disabling THP moves the peak by **−0.8%** and costs **+10.8% wall time**. At N=8 the sign even
reverses (7.557 vs 7.488 GiB). THP changes how the pages are *backed*, not how many are held.
**Not worth adopting**, and worth recording so it is not re-suspected later.

______________________________________________________________________

## 5. Mitigations — measured, memory saved against wall-clock cost

Identical shape for every row: the 600 s file, 8 production-shaped 180 s coarse windows, one
process per run, node otherwise idle.

| variant | peak (GiB) | Δ peak | wall (s) | Δ wall | RSS after last window (GiB) | arenas |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **baseline** (8 intra-op threads, glibc defaults) | 7.488 | — | 594 | — | 6.129 | 20 |
| `MALLOC_ARENA_MAX=4` | 7.379 | −1.5% | 594 | −0.0% | 6.024 | 4 |
| `MALLOC_ARENA_MAX=2` | 7.272 | −2.9% | 595 | +0.0% | 5.910 | 2 |
| `MALLOC_ARENA_MAX=1` | 7.220 | −3.6% | 596 | +0.3% | 5.872 | 1 |
| `MALLOC_MMAP_THRESHOLD_=131072` | 7.172 | −4.2% | 617 | +3.8% | 5.734 | 20 |
| `malloc_trim(0)` after every window | 7.490 | +0.0% | 595 | +0.1% | **5.698** | 20 |
| THP off (`prctl`) | 7.557 | +0.9% | 652 | +9.6% | 6.215 | 20 |
| **TF intra-op 4** | **6.412** | **−14.4%** | 643 | **+8.2%** | 5.941 | 9 |
| TF intra-op 2 | 6.413 | −14.4% | 1011 | +70.0% | 5.936 | 7 |
| TF intra-op 1 | 6.399 | −14.5% | 1847 | +210.6% | 5.925 | 6 |
| THP off + trim every window | 7.409 | −1.1% | 657 | +10.5% | 5.754 | 20 |
| THP off + `MALLOC_ARENA_MAX=4` | 7.390 | −1.3% | 656 | +10.4% | 5.997 | 4 |

**`MALLOC_ARENA_MAX` — not worth adopting.** Capping 20 arenas to 1 costs nothing in wall time
and buys **0.27 GiB (3.6%)**. That the number is so small is itself the finding: per-thread arena
proliferation is **not** where the footprint lives, so the first half of §7(a)'s reasoning —
right about retention — was pointing at the wrong pool.

**Periodic `malloc_trim` — not worth adopting for the peak; mildly useful for the floor.** It is
effectively free (+0.1% wall, no thrash) and returns **0.43 GiB (7%)** of steady-state RSS, but it
moves the peak by **+0.002 GiB**. A limit is enforced against the peak, so trimming does not
change any admission or OOM outcome. It does not thrash; it simply is not aimed at the binding
quantity.

**`MALLOC_MMAP_THRESHOLD_` — not worth adopting.** Pinning the dynamic mmap threshold at 128 KiB
(so large TF tensors go to `mmap` and are `munmap`ed on free rather than retained) is the best of
the allocator knobs at **−4.2%**, for +3.8% wall. Real, and an order of magnitude too small to
matter.

**TF intra-op thread cap — worth adopting, at 4.** This is the only knob with a material effect,
and the knee is unambiguous:

| intra-op threads | 8 (default) | **4** | 2 | 1 |
| --- | ---: | ---: | ---: | ---: |
| peak (GiB) | 7.488 | **6.412** | 6.413 | 6.399 |
| wall vs default | — | **+8.2%** | +70.0% | +210.6% |

**Every byte of the available saving is captured at 4 threads.** Going to 2 buys 0.001 GiB for
another 62 points of wall time; going to 1 buys nothing at all for 211%. macOS's 5.1× penalty was
measured only at 1 thread and does not generalize — on Linux the memory/time curve has a corner
at half the core count.

This is worth adopting for a second reason the memory number understates: the burst node runs
2–3 analyses concurrently on 8 cores, so an 8-thread intra-op pool per process is already
oversubscribed 3:1, and the spike's §8 shows the workload is serialized on single-threaded decode
anyway. `TF_NUM_INTRAOP_THREADS=4 TF_NUM_INTEROP_THREADS=1 OMP_NUM_THREADS=4` on the analyze
container is a 14% memory reduction for 8% of a wall clock that is not the constraint.

> **The knob this table is missing — `batchSize`. Shipped 2026-08-06 (`phaze-0582`).** Every row
> above holds `TensorflowPredict*`'s `batchSize` at essentia's default of **64**, because phaze
> passed no override and the parameter was not in scope here. `phaze-mqq5` found it, and
> `phaze-0582` re-measured it end to end on this node with this harness's discipline (host-side
> `VmHWM`, synthetic audio, one arm per process, node idle, the two arms differing only in
> `analysis.py`):
>
> | file / shape | batch 64 | **batch 32** | Δ peak | wall | Δ wall |
> | --- | ---: | ---: | ---: | ---: | ---: |
> | `dur_3600`, fine cap saturated (60 fine + 20 coarse) | 2.4445 GiB | **1.6206 GiB** | **−33.7%** | 3039.97 → 3052.09 s | **+0.40%** |
> | `dur_600` (20 fine + 4 coarse) | 2.1743 GiB | **1.4111 GiB** | **−35.1%** | 344.27 → 345.72 s | **+0.42%** |
>
> **It is a bigger lever than anything in the table above, at a twentieth of the wall-clock
> cost** — −33.7% against the intra-op cap's −14.4%, for +0.4% wall against +8.2%. The two are
> independent (one sizes the thread pool, the other the input tensor) and the intra-op
> recommendation is unaffected; sizing it from the host is `phaze-rvcn`.
>
> `discogs-effnet-bs64-1` stays at 64 — its Placeholder is `[64, 128, 96]` — so the saving comes
> from the other 33 graphs. Output is **not** byte-identical and is not claimed to be: max
> |Δ| **1.79 × 10⁻⁷** over the whole serialized result (3702 leaves), **0/714** top-1 flips,
> every categorical field identical, `danceability` moving 2.98 × 10⁻⁹. Running the same patched
> code at `PHAZE_ANALYSIS_TF_BATCH_SIZE=64` reproduces the pre-change output byte-identically,
> which is what attributes the whole delta to the batch.

______________________________________________________________________

## 6. What the production 16.7–30.2 GB numbers actually are

### 6a. Their provenance

They are kernel OOM records. Reconstructed from `journalctl -k` across the three relevant boots,
every `analysis_child` killed between 2026-07-24 and 2026-08-04 — **20 kills**, whose distinct
`anon-rss` values in GiB were:

```
15.27  15.65  15.70  16.18  16.20  16.49  16.63  16.71  17.11  18.47
20.87  21.22  23.17  23.36  23.43  30.15  30.18  30.38  30.41
```

The spike's list (16.7, 17.1, 18.5, 20.9, 21.2, 23.4, 23.4, 30.2) is a subset of these. They are
genuine single-process anonymous RSS at kill time, every one of them
`oom-kill:constraint=CONSTRAINT_NONE` — global, node-scoped, which is exactly what ADR-0005
exists to stop.

### 6b. Live production measurement on the same image

Three real-audio analyses were sampled from the host at 10 s while they drained, using
`job:2026.8.0` on the current kernel — the same binary and node this document measures:

| | runtime | RSS range | **VmHWM** | max drawdown |
| --- | ---: | ---: | ---: | ---: |
| analysis A | 9185 s | 6.44–7.64 GiB | **7.919 GiB** | 1.20 GiB |
| analysis B | 7871 s | 6.39–7.82 GiB | **7.928 GiB** | 1.44 GiB |
| analysis C | 9522 s | 6.26–7.87 GiB | **7.956 GiB** | 1.47 GiB |

Three different real files, 2.2–2.6 hours each, peaking within **0.5% of each other** and within
**0.9% of the synthetic 30-window figure (7.987 GiB)**. Their pod cgroups carried
`memory.high=max` and `memory.max=max`, so nothing external clipped them.

**This is the load-bearing cross-check.** It is what licenses using a sine-wave measurement to
size a production limit: real audio and synthesized audio produce the same peak, because the peak
is TF graph residency and inference arenas, whose sizes are fixed by window *shape*, not content.
A deliberately onset-dense synthetic file (pink noise + click train, to stress the beat trackers
a pure sine cannot) peaked at **6.670 GiB** against the pure sine's 6.836 GiB at the same caps —
**−2.5%** — and at 12 recycled coarse windows the two agree to **+0.4%** (7.929 against 7.898 GiB).

### 6c. Concurrency does not inflate the per-process peak

Production runs 2–3 analyses co-resident, so the obvious next suspect is that co-residency itself
changes allocator behaviour. Three harness processes were run simultaneously on the idle node,
6 production-shaped windows each:

| | peak | wall | per-window wall |
| --- | ---: | ---: | ---: |
| solo, same shape (6 windows) | 7.371 GiB | — | 74 s |
| concurrent A | 7.347 GiB | 1153 s | 192 s |
| concurrent B | 7.374 GiB | 1166 s | 194 s |
| concurrent C | 7.381 GiB | 1170 s | 195 s |

**Per-process peak is unchanged (−0.3% to +0.1%);** only wall time moves, by 2.6× from CPU
contention on 8 cores. Node total was 19 GiB — the three peaks simply add. Concurrency is a
throughput and node-arithmetic question, not a per-process memory question.

> **Confirmed at 12-way post-`phaze-15sw` (`phaze-3j67` §3):** per-process peak is flat at
> **2.074–2.151 GiB across W=1…12**, a 3.7% spread with no trend. The "only wall time moves" finding
> also survives and sharpens — per-file wall rises **+115.9 s per added worker, R² 0.9997**. Note
> that "8 cores" here is 8 *logical*: vox is a Xeon E3-1271 v3 with **4 physical cores** plus SMT,
> which is what sets the ~30 files/hour node ceiling that spike measures.

### 6d. What the OOM distribution is not

> **SUPERSEDED IN PART — 2026-08-06, [`phaze-wcrb`](phaze-wcrb-oom-multiplier-forensics.md).**
> The negative claim below stands: this is not a ratchet. The *positive* reading — "a small number
> of co-resident copies of the whole working set" — does **not**. It was drawn from the one-line
> `Out of memory: Killed process` records; the kernel also dumps a full **task table** before each
> kill, and those tables show one pod running **one** `analysis_child` (never two) alongside two
> ordinary analyses at 6.4–7.8 GiB. Summed over every task, all 22 dumps report **30.76–30.82
> GiB** — node capacity. The victim's `anon-rss` is therefore the *residual*, `30.80 − Σ(others)`,
> and the "2× / 3× / 4× clusters" are 2, 1 and 0 healthy neighbours subtracted. **The hard floor
> at 15.27 GiB is `30.80 − 2 × 7.81`**: Kueue admits at most 3 analyze pods, so at most two
> neighbours can be subtracted, and a runaway below that size never fills the node and so is never
> recorded. The distribution is left-censored, not multiplicative. The real fault is **one process
> in unbounded growth on 4 files out of ~520**, which `phaze-wcrb` leaves open with three further
> rule-outs. Read §6d below as the shape of the *evidence available at the time*, not as a finding.

If the kills were "a ratchet sampled at kill time", the values would form a continuum rising from
the floor. They do not:

- **There is a hard floor at 15.27 GiB.** Not one kill lands between the measured 8 GiB working
  set and 15.27 GiB, across 20 records and 12 days.
- **The values cluster near integer multiples of the measured unit** — 15.3–16.7 ≈ 2×,
  23.2–23.4 ≈ 3×, 30.2–30.4 ≈ 4× of ~7.7 GiB — with a handful of intermediates.

That is a *multiplicative* signature, not an additive drift. It is what a small number of
co-resident copies of the whole ~7.7 GiB working set would look like, and it is not what any
allocator-retention mechanism produces. §7(a) is therefore not merely too small to explain the
gap — **it is the wrong shape.**

Nothing in the current code produces such a duplication: `analysis_child` is `exec`'d once per
file, calls `analyze_file` once, and `_classifier_cache` is never cleared or rebuilt. The 23.36
GiB kill on 2026-08-04 11:45 occurred on the *same kernel* as these measurements, and
`analysis.py` is byte-identical between `2026.7.11` and `2026.8.0`, so neither a kernel nor an
analysis-code change separates the two populations.

**This is left open, deliberately, rather than fitted to a story.** The measurement rules out the
proposed mechanism; it does not identify the real one. What it does establish is the operational
consequence: the outlier is real, it is 2–4× the working set, and it is not reachable by tuning —
which is precisely the case for a *limit* as a backstop rather than a tighter request.

______________________________________________________________________

## 7. What this calibrates

### 7a. `KubeConfig.memory_limit` — proposed default **`12Gi`**

| input | value |
| --- | ---: |
| measured Linux peak, 30 coarse windows (synthetic) | 7.987 GiB |
| measured Linux peak, real audio in production (3 runs) | 7.919–7.956 GiB |
| measured Linux peak, onset-dense synthetic | 6.670 GiB |
| **design peak** | **8.0 GiB** |
| **proposed `memory_limit`** | **12Gi** (design peak × 1.5) |
| **proposed `memory_request`** | **9Gi** (design peak × 1.13) |

The reasoning, in the order it binds:

1. **The request must cover the measured peak, because the peak is flat.** There is no tail to
   trade against: every file pays 5.7–8.0 GiB, and the maximum across **24 synthetic runs and 3
   production runs** is **7.987 GiB**. 8Gi is *below* that for a three-minute file and must
   rise. **9Gi** covers the measured maximum with 13% margin.
1. **The limit is a backstop against §6d's outlier, not a bound on normal work.** A tight limit
   (say 9Gi) would OOMKill on ordinary variance; a loose one (16Gi) admits a 2× duplication before
   acting. **12Gi** is 1.5× the design peak — above anything ever measured on Linux, below the
   15.27 GiB floor of the pathological population, so it converts *exactly* the pathological runs
   into pod-scoped kills and nothing else.
1. **The request is what makes the limit node-safe.** Kueue's quota is 24Gi. At the current 8Gi
   request it admits **3** concurrent jobs, whose worst case under a 12Gi limit is 36 GiB — more
   than the node has. At **9Gi** it admits **2**, worst case 24 GiB, leaving ~7 GiB for
   k0s/kubelet/coredns/metrics-server on a 31.2 GiB node. Raising the request is not
   bookkeeping; it is the load-bearing half of the change.
1. **Ship the code default as `None`, publish 12Gi as the operator value.** ADR-0005 locked
   "absent ⇒ no `limits` key" for backward compatibility and that should stand — a shipped default
   that silently starts OOMKilling somebody's cluster is the wrong direction for an opt-in knob.
   The calibrated number belongs in `docs/k8s-burst.md` and in the operator's config.

Steady state at this sizing: 2 concurrent jobs × ~8 GiB actual = ~16 GiB of 31.2 GiB, versus the
~21–22 GiB the node currently runs at with 3.

### 7b. The interim `12Gi` / `16Gi` — **half confirmed, half corrected**

The spike sized `memory_request: 12Gi`, `memory_limit: 16Gi` from "this host's 8.5–10.5 GiB floor
plus the production ratchet". The ratchet term was wrong, so both numbers inherit the error:

- **`memory_request: 12Gi` → correct to `9Gi`.** 12Gi over-reserves by 50% against a measured
  8.0 GiB peak. Its real cost is admission: 24Gi ÷ 12Gi = 2 jobs, the same concurrency 9Gi buys,
  while making a future quota increase read as room for 2 rather than 2.7 jobs.
- **`memory_limit: 16Gi` → correct to `12Gi`.** 16Gi was chosen to sit above an assumed ratchet.
  There is no ratchet to sit above, and 16Gi is *above* the 15.27 GiB floor of the pathological
  population — it would let the 2× runs through and only catch the 3× and 4× ones. 12Gi catches
  all of them.
- **Concurrency 1 → 2 is safe** at 9Gi/12Gi (worst case 24 GiB + ~4 GiB system on 31.2 GiB), and
  the spike's own §10 conclusion that "not even 1 is safe" was derived from the same unexplained
  p100 that §6d shows is not the operating footprint.

### 7c. Follow-up A's 3–4 GiB estimate — **survives, and removes a larger fraction**

> **SHIPPED AND MEASURED — 2026-08-05, `phaze-15sw`.** Model-major iteration landed. Re-run of
> **this** harness, on this node, against the deployed image (`job:2026.8.0`, whose
> `analysis.py` is byte-identical to the tree the change was made on) and the deployed models
> PVC. The baseline column was independently re-measured here, not copied: it reproduces this
> spike to **0.01%** on the envelope maximum and **0.3%** on graph residency.
>
> | measurement | window-major | model-major | Δ |
> | --- | ---: | ---: | ---: |
> | 34 graphs resident, zero inference | 4.007 GiB† | **0.806 GiB** | −79.9% |
> | 1 coarse window | 5.713 GiB† | **2.126 GiB** | −62.8% |
> | **30 coarse windows (envelope maximum)** | **7.986 GiB†** | **2.482 GiB** | **−68.9%** |
> | `dur_200` (3.3 min), production caps | 5.675 GiB† | **2.074 GiB** | −63.5% |
> | `dur_600` (10 min), production caps | 6.793 GiB† | **2.141 GiB** | −68.5% |
> | `dur_3600` (60 min), production caps | 7.949 GiB‡ | **2.489 GiB** | −68.7% |
>
> † re-measured by `phaze-15sw`. ‡ this spike's figure, not re-run: its full pipeline costs ~51
> min per run, dominated by §8's non-seeking decode. The re-measured rows validate the harness
> against this spike to **0.01%** on the envelope maximum and **0.3%** on graph residency, which
> is what licenses citing the one row that was not repeated.
>
> **Duration-independence survives the restructure**, and tightens: 2.074 / 2.141 / 2.489 GiB
> across an 18× duration span, against 5.675 / 6.793 / 7.949 before. What is left is close to a
> constant plus the ≤`coarse_cap` buffers, which is the shape the change predicted.
>
> **The estimate was beaten, not met**: 2.1–2.5 GiB against a predicted 3.5–4.5, and −69% of the
> peak against a predicted −50%. The error was in the remainder term, not the removed one —
> §2d's arena figures were read as "one architecture *family* stands behind the resident graph",
> but sweeping one model at a time never stands the vggish arena up on top of the musicnn one.
> **Wall clock: +2.1%** at 30 windows (2115.0 → 2159.0 s) and within noise on the full pipeline
> (`dur_200` 110.5 → 110.3 s, `dur_600` 345.2 → 345.2 s); model constructions per file unchanged
> at 34; `analyze_file` output **byte-identical** (sha256 match + `cmp`) on both files. The
> host-side sampler (14 283 samples) puts instantaneous RSS at 0.754–2.482 GiB, below its own
> running maximum in **100%** of samples — the same sawtooth as §3, one third the height.


Graph residency measures **+3.995 GiB on Linux** (§2d) against a 7.99 GiB envelope maximum, so
model-major iteration removes **50%** of the peak on Linux, not the 40% estimated on macOS. The
predicted remainder
— import (0.20) + one architecture family's arena (0.87 measured for musicnn) + the ≤30 coarse
buffers (0.35) — lands at **≈3.5–4.5 GiB**, and combining it with the 4-thread cap (§5) would put
it near **3.0–3.9 GiB**. The estimate survives the platform change with room to spare; the
*fraction* it removes is larger on Linux because the inference-arena term it leaves behind is 6×
smaller here.

______________________________________________________________________

## 8. Recommendations

| | action | why |
| --- | --- | --- |
| 1 | ~~**`memory_request: 9Gi`, `memory_limit: 12Gi`, concurrency 2** on a 31 GB node~~ **SUPERSEDED 2026-08-05 (`phaze-3j67`)** — re-measured post-`phaze-15sw` as **`memory_request: 3Gi`, `memory_limit: 4Gi`, `cap` 4**; see [the capacity sweep](phaze-3j67-concurrent-extractor-capacity.md) §9 | §7a. The 9Gi/12Gi replaced the interim 12Gi/16Gi, which was sized from an assumed ratchet; §7c then removed the 8 GiB peak it was itself sized against. The **concurrency** half was raised 2 → 4 for a different reason: the node stopped being memory-bound and is now CPU-bound at W=2, so `cap` is now set from cores, not GiB. |
| 2 | **`TF_NUM_INTRAOP_THREADS=4 TF_NUM_INTEROP_THREADS=1 OMP_NUM_THREADS=4`** on the analyze container | §5. −14.4% peak for +8.2% wall on a path that is not wall-clock bound. The only knob that pays. **Re-measured against the model-major code (`phaze-3j67` §5): −41.7% peak (2.151 → 1.211 GiB) for +0.9% throughput at the operating point** — the two changes compose, because what the cap shrinks is the per-inference arena `phaze-15sw` left behind. |
| 3 | **Do not adopt** `MALLOC_ARENA_MAX`, periodic `malloc_trim`, `MALLOC_MMAP_THRESHOLD_`, or a THP opt-out | §4, §5. Each is ≤4.2% on the peak, and `malloc_trim` is 0.0% on the quantity a limit enforces. |
| 4 | ~~**Proceed with follow-up A**~~ **DONE 2026-08-05 (`phaze-15sw`)** — envelope maximum **7.986 → 2.482 GiB (−68.9%)** for **+2.1%** wall clock, output byte-identical | §7c. It was the only change measured to move the peak by more than 15%, and it removed ~69% of it — more than the 50% predicted. **Recommendation 1's `9Gi`/`12Gi` is now sized against a peak that no longer exists** and should be re-derived from 2.5 GiB (tracked as `phaze-7qfd`); this bead deliberately did not touch `docs/k8s-burst.md`. |
| 5 | **File the §6d anomaly as its own investigation** | The 2–4× population is real, unexplained, and unreachable by tuning. The limit contains it; it does not explain it. |

______________________________________________________________________

## 9. What this measurement does and does not support

- **Supported:** peak RSS, its stage and per-model attribution, the shape of the RSS-over-time
  curve, and the effect of each mitigation — all on Linux/glibc, on the deployed image, against
  the deployed model set, cross-checked against three real-audio production runs (§6b).
- **Not supported:** any claim about *why* 20 production kills sat at 2–4× the working set. That
  population was not reproduced. §6d states the shape of the anomaly and stops there.
- **A measurement artifact worth recording (§3c):** an in-process RSS sampler thread reports a
  monotone ratchet — max drawdown 0.081 GiB — because essentia's bindings hold the GIL through
  inference, so the thread only samples between models, always at the same phase. That is a
  *false confirmation of the hypothesis under test*. Every ratchet conclusion here is drawn from
  the **host-side** sampler. Anyone re-running this harness should not trust an in-process
  sampler for this workload.
- **Synthetic audio is validated for this question, not licensed in general.** §6b establishes
  that peak memory is content-independent (real audio and sine agree to 0.9%; onset-dense and
  sine to 0.4% at equal window count, 2.5% over the full pipeline) because it is a function of
  window *shape*. A question about *wall time* or about
  the beat trackers' own behaviour would not inherit that license.

______________________________________________________________________

## Appendix — reproducing this

The harness is a single script driving the real `phaze.services.analysis` internals with five
subcommands mirroring the spike's four (`stages`, `sweep`, `windows`, `models`, `decode`), marking
`VmHWM` / `ru_maxrss` / `AnonHugePages` / `malloc_info` at stage boundaries, plus a host-side
sampler. Mitigations are environment-driven (`MALLOC_ARENA_MAX`, `MALLOC_MMAP_THRESHOLD_`,
`TF_NUM_INTRAOP_THREADS`, `TF_NUM_INTEROP_THREADS`, `OMP_NUM_THREADS`) except `malloc_trim`
(`ctypes` call after every N inferences) and the THP opt-out (`prctl(PR_SET_THP_DISABLE)`).

It runs in a bare pod on the burst node using the deployed job image and the deployed models PVC
mounted read-only, with no Kueue queue label so it consumes no quota:

```sh
kubectl run/apply a pod:  image ghcr.io/simplicityguy/phaze/job:<tag>
                          volumeMounts: phaze-models (ro) at /models, scratch at /scratch
                          command: ["sleep", "infinity"]
```

Test audio, regenerated in minutes and touching no real library — the spike's generator, plus the
onset-dense variant used in §6b:

```sh
# the spike's sine pair (durations 200 / 600 / 3600 / 43200)
ffmpeg -f lavfi -i "sine=frequency=440:duration=600" \
       -f lavfi -i "sine=frequency=554:duration=600" \
       -filter_complex "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]" \
       -map "[a]" -ar 44100 -c:a libmp3lame -b:a 192k dur_600.mp3

# onset-dense: pink noise + kick/hat click trains + a sustained tone
ffmpeg -f lavfi -i "anoisesrc=color=pink:duration=600:sample_rate=44100:amplitude=0.35" \
       -f lavfi -i "sine=frequency=60:duration=600" \
       -f lavfi -i "sine=frequency=330:duration=600" \
       -f lavfi -i "sine=frequency=2400:duration=600" \
       -filter_complex "[1:a]tremolo=f=8:d=1.0,volume=1.6[kick]; \
                        [3:a]tremolo=f=16:d=1.0,volume=0.5[hat]; \
                        [0:a]volume=0.6[nz]; \
                        [nz][kick][hat][2:a]amix=inputs=4:duration=shortest:normalize=0, \
                        alimiter=limit=0.95,aformat=channel_layouts=stereo[a]" \
       -map "[a]" -ar 44100 -c:a libmp3lame -b:a 192k dur_600_dense.mp3
```

**Two things to get right when re-running.** `ru_maxrss` is **KiB on Linux and bytes on Darwin** —
comparing the spike's numbers to these without that conversion produces a 1024× error. And sample
instantaneous RSS from *outside* the process (§9).
