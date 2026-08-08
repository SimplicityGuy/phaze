# phaze-i93a — would rewriting the analyze pipeline in modern C++ pay for itself?

- **Bead:** `phaze-i93a` (spike — "essentia exposes a C++ API in both standard and streaming modes;
  are there performance gains from writing phaze's analysis portion in modern C++?")
- **Date:** 2026-08-06
- **Tree:** branch `wt/bead/issue/phaze-i93a`, forked off `main` at `75020e8`
- **Code under test:** the deployed analyze image `ghcr.io/simplicityguy/phaze/job:2026.8.0` with
  `main`'s post-`phaze-15sw` `services/analysis.py` overlaid (sha256 `45a84a70…` — byte-for-byte
  the same overlay `phaze-15sw`, `phaze-3j67`, `phaze-rc1q` and `phaze-mqq5` used), against the
  deployed `phaze-models` PVC (34 graphs)
- **Upstream read:** `MTG/essentia@master` — `src/examples/` and `src/algorithms/extractor/`
- **Status:** spike. Measurement and verdict only. **No product code changed.**

______________________________________________________________________

## Verdict in one paragraph

**No. The measured ceiling on a C++ rewrite is 1.12% of a 10-minute file and 0.29% of a 60-minute
one, and 0% of throughput.** Wrapping every essentia / TensorFlow / numpy / json / gc entry point
the real `analyze_file` calls and treating the leftover wall clock as Python puts phaze's own
orchestration at **0.164 s of a 307.9 s run (0.053%)** and **0.197 s of a 1 793.6 s run (0.011%)**
against the **improved** baseline — flat at **0.0087–0.0547% across six runs** spanning a 41% change
in wall clock, a 33% change in peak memory and two entirely different decode compositions. Add every
other Python-attributable cost — `gc.collect()`, essentia's *own* Python binding layer (`standard.py`
`__init__`, measured by cProfile at 1.897 s and the single largest Python cost center anywhere in the
process), and the interpreter boot + import an exec'd child pays once per file (**1.208 s**) — and the
total is **3.46 s (1.12%)** and **5.18 s (0.29%)**. The GIL is the one place the skeptical prior could
have been wrong, and it is not: essentia holds the GIL for **99.86% of wall clock** (longest single
stall **9.7 s**; a probe thread got **6** iterations in 18 s), and two Python threads running decode
and inference achieve **exactly zero** overlap (54.076 s threaded vs 54.090 s serial, efficiency
**−0.09%**). But two *processes* running the same two tasks achieve **98%** (29.61 s against a
29.11 s perfect-overlap floor) — and one exec'd process per file is already what phaze does,
deliberately, for exactly this reason: `services/analysis_exec.py`'s own docstring says it execs the
child *"because essentia's C++ holds the GIL of the process it runs in"*. **The only measurable cost
of the GIL was found and closed by a design that already ships, at zero rewrite cost.** Even if
in-job overlap were free it would be worth **≤7.1%** on the improved baseline and **0% at the
production operating point**, where `phaze-3j67` measured 99.9% node CPU from W=4. There is no hot
Python path for a narrow native extension: the per-model prediction-dict assembly — the largest
construct in `analysis.py` — costs **0.98 ms per file**, and no phaze function exceeds **0.16 s** of
exclusive time under a profiler. The largest Python-side win that exists is **not to leave Python**:
hoisting `RhythmExtractor2013` and `KeyExtractor` out of the per-window loop cuts the fine tier
**31.50 → 23.93 s (−24.0%) with 0/60 output mismatches** — 38× the entire orchestration residual,
from moving two lines. Against that, the cost side is a second cross-arch C++ toolchain on an image
whose Python pin is *already* hostage to a TensorFlow wheel and that already carries four numbered
link fixups, the loss of 369 lines of measured, docstring-dense Python defended by 2 149 lines of
direct tests, and an upstream reference pipeline (`MusicExtractor`, 650 lines of C++) that **decodes
each file three times** — less efficient than the streaming hybrid `phaze-rc1q` designed in Python.
**Recommendation: NO-GO, and close the question rather than deferring it.**

______________________________________________________________________

## 1. Method

| | |
| --- | --- |
| **Host** | `vox` — Debian 13 (trixie), kernel 6.12.100, glibc 2.41, Xeon E3-1271 v3, **4 physical cores / 8 logical (SMT)**, 31.3 GiB, k0s burst node, **out of the phaze backend registry and left that way**, otherwise idle (verified before the session; gated per run — §1c) |
| **Runtime** | deployed job image `job:2026.8.0` in a bare `sleep infinity` pod, **no Kueue queue label** (consumes no quota), `phaze-models` PVC mounted **read-only**, a host scratch dir for synthetic audio, Python 3.14.6, `essentia-tensorflow` 2.1-beta6-dev |
| **Code** | the **real** `phaze.services.analysis.analyze_file` from `main`, imported out of a copy of the image's tree with `analysis.py` overlaid (sha256 `45a84a70…`) and patched **in memory** — no product code was modified |
| **Audio** | **synthesized with ffmpeg** — the `phaze-esut` generator, stereo 44.1 kHz sine pairs at 192 kbps, `dur_600` and `dur_3600` |
| **Per-process peak** | `/proc/self/status:VmHWM`, read **once at exit** — a kernel high-water mark, not a sampled curve, so it is immune to the `phaze-7i0k` §9 GIL trap (which §5 re-derives here from the other direction) |
| **Contention gate** | node CPU from host-side `/proc/stat` deltas across each run, recorded per run and reported in the appendix |

**No operator media was read, copied, or referenced.** Every input is a synthesized sine pair. No
filename, path, or per-file metadata value from the library appears in this document. vox was **not**
re-enabled in the phaze backend registry; k0s, JuiceFS and the gateway config were not touched; the
models PVC was mounted **read-only** and is intact. The only artefacts created are one scratch
directory and one bench pod, both removed (appendix).

### 1a. The instrument: seam accounting

"How much of this is Python?" has an exact answer if you can name every door out of the interpreter.
`services/analysis.py` has few, and all of them are enumerable by inspection:

| seam | what it is |
| --- | --- |
| `es.MetadataReader` | duration probe |
| `es.EasyLoader` | per-window decode (both tiers) |
| `es.RhythmExtractor2013`, `es.KeyExtractor` | the fine tier's two extractors |
| `es.TensorflowPredict{MusiCNN,VGGish,EffnetDiscogs}` | graph construction + inference |
| `np.mean` | `_predict_single`'s activation aggregation |
| `json.load` | the 34 label sidecars |
| `gc.collect` | `_release_classifier`'s insurance |

Each is wrapped with a `perf_counter` pair — **construction and call timed separately** — via a proxy
installed on the module's `es` / `np` / `json` / `gc` names. **Everything outside those brackets is
Python bytecode.** The instrument costs ~1 800 `perf_counter` pairs per file, under 0.5 ms — four
orders of magnitude below the residual it measures.

The residual is deliberately a **generous upper bound on phaze's Python layer**: it also absorbs the
CPython runtime's refcounting, allocation and the destructor calls that free essentia objects. What
it does **not** include is the Python inside essentia's *own* bindings, which sits between the proxy
and the C++ — §4 measures that separately with a profiler and adds it back, because a C++ rewrite
would eliminate it too.

### 1b. What "the improved baseline" is, and why the comparison must be against it

The bead was filed when the pipeline peaked at 7.986 GiB and a 12-hour file spent 6.15 hours in
decode. Both motivating problems have since been solved **in Python**, and a C++ proposal has to beat
what is left, not what was there. Four arms, all through the real `analyze_file`:

| arm | what it is |
| --- | --- |
| `base` | the shipped configuration — `batchSize` at its default 64, one `EasyLoader` per window |
| `batch32` | + `phaze-mqq5` recommendation 1: `batchSize=32` on the musicnn and vggish families (`discogs-effnet-bs64-1`'s Placeholder is a hard `[64,128,96]` and stays at 64) |
| `hybrid` | + `phaze-rc1q` recommendation 1: ONE streaming fan-out network per tier — `AudioLoader → MonoMixer → Resample`, fanned out to `Scale(1.0) → Trimmer → Pool` per window — plus its recommendation 4, one `malloc_trim(0)` per tier |
| **`improved`** | **`batch32` + `hybrid` — the baseline a C++ rewrite has to beat** |

The hybrid arm replaces **only** the decode loop inside `_analyze_fine_windows` /
`_analyze_coarse_windows`. Window geometry (`_iter_windows`, `_stride_to_cap`), the model-major
sweep, failure isolation, derivation and assembly are `main`'s code, called unmodified.
`phaze-rc1q`'s recommendation 3 (pre-sized `numpy` sinks instead of an essentia `Pool`) is **not**
applied — this is the "rec 3 not yet landed" configuration, which makes the peak figures here
conservative.

Both spikes' recommendations are unmerged, so this is also the first time they have run **in the same
process** — which `phaze-mqq5` §5c explicitly asked for and refused to assume (§2c).

### 1c. Admissibility

Every run is solo; nothing in this spike ran concurrently with anything else. Node CPU across the
four `dur_600` runs and the `dur_3600` improved run sat at **71.2–81.0%**, the band `phaze-3j67` §2
measured for one extractor (74.3–81.4%, ~6.2 of 8 logical cores). The `dur_3600` base run sits lower
at **52.9%** — as it must, because 45% of its wall clock is the single-threaded `EasyLoader` decode
`phaze-esut` §8 identified, and that is itself a confirmation rather than a contamination. Full gate
table in the appendix.

______________________________________________________________________

## 2. The cheap baseline, measured first — and it reproduces

The bead's first acceptance criterion is that the free Python wins are quantified before any C++ work
is contemplated. `phaze-3j67`, `phaze-rc1q` and `phaze-mqq5` did that; this spike's job is to confirm
they land in the same place under one harness, because everything downstream is a ratio against them.

### 2a. `dur_600` — 10 min, 20 fine + 4 coarse windows

| arm | wall (s) | vs base | **peak `VmHWM` (GiB)** | vs base | decode (s) | result sha256 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| **base** | **345.46** | — | **2.094** | — | 53.02 | `eb18b125…` |
| batch32 | 345.02 | **−0.13%** | **1.411** | **−32.6%** | 52.93 | `1e1072df…` |
| hybrid | 306.39 | **−11.3%** | 2.510 | +19.8% | **15.09** | `eb18b125…` |
| **improved** | **307.88** | **−10.9%** | **1.788** | **−14.6%** | **15.09** | `1e1072df…` |

Four cross-checks, none of which depends on this spike's own instrument:

- **The base arm reproduces the family.** 345.46 s against `phaze-rc1q`'s 344.93 s (**+0.15%**) and
  `phaze-15sw`'s 345.2 s (**+0.08%**) on the same file and caps — and its result sha256 prefix
  `eb18b125…` is **the one `phaze-rc1q` §5 published**.
- **`phaze-rc1q`'s identity claim reproduces.** The hybrid arm returns sha256 `eb18b125…` — the base
  arm's result, byte for byte — from a completely different decode composition. Independent
  confirmation, on a separately written harness, of that spike's central correctness claim.
- **`phaze-mqq5`'s batch lever reproduces to the digit.** `danceability` comes back
  `0.5625000968575478` at batch 64 and `0.5625000943740209` at batch 32 — **the exact two values that
  spike's §5b tabulates**. Peak −32.6% against its −32.1%.
- **`phaze-rc1q`'s decode speedup reproduces.** 24 `EasyLoader` calls costing **53.02 s** collapse to
  two `essentia.run()` calls costing **15.09 s** — **3.5×**, which is that spike's §4 figure for
  `dur_600` exactly.

### 2b. `dur_3600` — 60 min, 60 fine + 20 coarse, **fine cap saturated**

The shape `phaze-15sw`, `phaze-3j67`, `phaze-rc1q` and `phaze-mqq5` all sized against.

| arm | wall (s) | **peak (GiB)** | decode (s) | result sha256 | `danceability` |
| --- | ---: | ---: | ---: | --- | --- |
| **base** | **3 043.83** | **2.466** | **1 367.83** | `d7fde10d…` | `0.5625221525629361` |
| **improved** | **1 793.57** | **2.812** | **127.45** | `146673c5…` | `0.562522149582704` |
| **Δ** | **−41.1%** | **+14.0%** | **10.7× faster** | — | Δ 3.0 × 10⁻⁹ |

Again the baseline lands exactly where the family put it, and again by hashes rather than by
argument:

| source | `dur_3600` baseline | this measurement vs it |
| --- | ---: | ---: |
| `phaze-15sw` envelope maximum | 2.482 GiB | **−0.6%** |
| `phaze-3j67` per-process peak | 2.566 GiB | −3.9% |
| `phaze-rc1q` `dur_3600` baseline | 2.505 GiB | −1.6% |
| `phaze-mqq5` `dur_3600` baseline | 2.588 GiB | −4.7% |
| **this document** | **2.466 GiB** | — |

- **Wall clock: 3 043.83 s against `phaze-rc1q`'s 3 044.87 s — −0.03%**, and the result sha256
  `d7fde10d…` is **the prefix that spike published for this exact file and caps**.
- **Decode: 1 367.83 s → 127.45 s.** `phaze-rc1q` measured 1 376.70 → 126.85 s. **−0.7% / +0.5%.**
- **`danceability` `0.562522149582704`** is `phaze-mqq5` §5c's batch-32 value, to the digit.

### 2c. The joint measurement `phaze-mqq5` §5c asked for — and the arithmetic was wrong

That spike computed, and explicitly refused to ship as a measurement, the sum of its own base-term
reduction and `phaze-rc1q`'s peak regression. The `improved` arm **is** that sum, measured:

| | `dur_3600` peak |
| --- | ---: |
| base (this document) | **2.466 GiB** |
| `phaze-rc1q` hybrid, unmitigated | 3.584 GiB (breaches the 3Gi request; 90% of the 4Gi limit) |
| `phaze-rc1q` hybrid + `malloc_trim` | 3.181 GiB (still above the 3Gi request) |
| `phaze-mqq5` §5c's **predicted** joint figure | 2.392 GiB |
| **measured joint (`improved`)** | **2.812 GiB** |

**The prediction was optimistic by 0.420 GiB (+17.6%) — and the conclusion survives anyway.** That is
exactly why `phaze-mqq5` refused to ship the sum, and it is the second time in this molecule that a
plausible arithmetic argument about freed transients missed (`phaze-rc1q` §6 was the first, by
1.079 GiB). The operational statement, measured:

- **The batch lever pays for the hybrid's regression.** Together they land at **2.812 GiB — under
  `phaze-3j67`'s `memory_request: 3Gi` and at 70% of its `memory_limit: 4Gi`** — where the hybrid
  alone breaches the request at 3.181–3.584 GiB.
- **And the pair is 41.1% faster.** On a node `phaze-3j67` measured **CPU-bound at W=2 with 13.1 GiB
  of memory it cannot use**, that is the right direction on both axes.
- **This is still not permission to skip `phaze-rc1q` recommendation 7.** Its recommendation 3 is not
  applied here, and the sizing must be re-confirmed against `analyze_file`'s peak-RSS log line after
  the real implementations land. What this measurement establishes is that the two changes **compose
  in the favourable direction**, which is what was in doubt.

> **Recommendation 7 discharged, and this section's 2.812 GiB superseded — `phaze-5lop`,
> 2026-08-06.** The real implementation landed and was measured end to end on the same node,
> same file, same caps, through the shipped `analyze_file` rather than a reimplemented
> `improved` arm:
>
> | | `dur_3600` wall | `dur_3600` peak |
> | --- | ---: | ---: |
> | this document's `base` (batch 64, per-window decode) | 3 043.83 s | 2.466 GiB |
> | this document's `improved` (batch 32 + hybrid decode, **without** `phaze-rc1q` rec. 3/4) | 1 793.57 s | **2.812 GiB** |
> | `main` before `phaze-5lop` (batch 32 + `phaze-rvcn` threads + `phaze-ap8y`) | 3 205.05 s | **1.3999 GiB** |
> | **shipped (`phaze-5lop`, with rec. 3 + rec. 4)** | **1 960.37 s** | **1.7383 GiB** |
>
> §2c's central claim survives and strengthens: the two changes compose favourably, and the
> joint peak lands **under** `phaze-3j67`'s 3Gi request. It lands **1.074 GiB further under it**
> than this section measured, for two reasons that are worth separating — `phaze-rvcn`'s
> host-derived thread pinning (which arrived after this document and moved the *baseline* down
> to 1.3999 GiB) and `phaze-rc1q` recommendations 3 and 4 (which the `improved` arm here
> deliberately did not apply, and which are worth the difference between +1.078 and +0.338 GiB
> over that baseline).
>
> The wall-clock figures moved too, and in the less flattering direction: **3 043.83 → 3 205.05 s**
> on the baseline (+5.3%), because `phaze-rvcn` trades wall clock for a hardware-independent
> peak. So `phaze-5lop`'s end-to-end saving reads **−38.8%** where this section measured −41.1%
> — the decode saving is the same, the denominator grew. `docs/k8s-burst.md`'s sizing table now
> carries `1.7383 GiB` as the current row.

______________________________________________________________________

## 3. Python orchestration as a fraction of wall clock — the ceiling

This is the number the bead says can end the spike on its own.

| arm | file | wall (s) | native seams (s) | `gc.collect` (s) | **Python residual (s)** | **% of wall** |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| base | `dur_600` | 345.46 | 345.06 | 0.216 | **0.186** | **0.0538%** |
| batch32 | `dur_600` | 345.02 | 344.61 | 0.216 | **0.189** | **0.0547%** |
| hybrid | `dur_600` | 306.39 | 305.96 | 0.269 | **0.160** | **0.0522%** |
| **improved** | `dur_600` | 307.88 | 307.44 | 0.274 | **0.164** | **0.0532%** |
| base | `dur_3600` | 3 043.83 | 3 043.35 | 0.213 | **0.265** | **0.0087%** |
| **improved** | `dur_3600` | **1 793.57** | 1 793.07 | 0.300 | **0.197** | **0.0110%** |

**phaze's Python orchestration is one twentieth of one percent of the pipeline, and the figure does
not move.** Across a 41% change in wall clock, a 33% change in peak memory, two entirely different
decode compositions and a 6× change in window count, the residual stays inside **0.0087–0.0547%**,
and it stays *absolutely* flat in seconds (0.160–0.265 s) because the interpreter's job — name a
graph, hand over a buffer, copy 34 float vectors back — does not grow when the native work does.

Broken out by phase, `improved` on `dur_3600` (the saturated shape):

| phase | wall (s) | native (s) | **Python (s)** |
| --- | ---: | ---: | ---: |
| duration probe | 0.0003 | 0.0002 | **0.0000** |
| fine tier (fan-out decode + BPM + key, 60 windows) | 115.533 | 115.492 | **0.041** |
| coarse tier, of which: | 1 678.037 | 1 677.881 | **0.156** |
| — the 34-model sweep over 20 windows | 1 634.832 | 1 634.677 | **0.155** |
| assembly + aggregation + return | — | — | **≈0.001** |

**The entire Python cost of the pipeline lives in one place**: `_run_model_sets_over_windows` building
`[{"label": …, "prediction": float(…)}, …]` for 34 models × 20 windows. §6 prices it as a native
extension target and finds it is worth **0.98 ms**.

### 3a. An independent estimate, from a profiler rather than from seams

cProfile with C-call tracking over a full `analyze_file` on `dur_600` at production caps — a
completely different instrument, and one that inflates the Python side because its per-event overhead
lands on Python frames:

| | tottime (s) | share |
| --- | ---: | ---: |
| built-in / native (`~`) | **342.062** | **99.39%** |
| Python-defined functions (all of them, including essentia's bindings) | **2.104** | **0.611%** |
| profiled wall | 344.166 | — |

**0.611% is the generous upper bound**, and it is generous three times over: it includes the
profiler's own overhead, it counts essentia's binding layer (which the seam accounting attributes to
native), and it is measured on the *short* file where the fixed costs weigh most. The seam accounting
puts phaze's own share at 0.053% on the same file. **Both instruments agree the answer is "under one
percent"; they disagree only about which fraction of one percent.** §4 splits the difference
explicitly.

______________________________________________________________________

## 4. The full Python tax, in four tiers — the honest larger number

The residual is not the whole Python story and it would be a weak spike that stopped there. Four
things are attributable to being in Python, and a C++ binary would avoid all of them:

1. **phaze's own orchestration** — §3's residual.
1. **The CPython runtime's own work** — `gc.collect()` in `_release_classifier`, which `phaze-15sw`
   documents as insurance rather than mechanism.
1. **essentia's Python binding layer.** cProfile's largest Python cost center anywhere in the process
   is not phaze's code: it is `essentia/standard.py:39(__init__)` at **1.897 s tottime over 99
   algorithm constructions** (against 3.132 s of native `__configure__` in the same calls). That is
   **37.7% of construction time spent in the binding**, and the seam accounting counts it as native
   because the proxy brackets the whole constructor. Estimated per arm by applying that fraction to
   the measured per-arm construction total.
1. **Interpreter boot + import.** phaze execs **one child process per file**
   (`services/analysis_exec.py` → `python -m phaze.analysis_child`), so this is paid once per file.

Measured (median of 3, on the bench pod):

| | wall (s) | `VmHWM` |
| --- | ---: | ---: |
| bare interpreter (`python -c pass`) | **0.017** | **9.41 MiB** |
| `import numpy` | 0.299 | 32.36 MiB |
| **`import phaze.services.analysis`** (numpy + essentia + TF + phaze) | **1.208** | **217.15 MiB** |

Adding up, against the **improved** arm:

| tier | `dur_600` (wall 307.88 s) | | `dur_3600` (wall 1 793.57 s) | |
| --- | ---: | ---: | ---: | ---: |
| 1 — phaze orchestration | 0.164 s | 0.053% | 0.197 s | 0.011% |
| 2 — + `gc.collect()` | 0.438 s | 0.142% | 0.497 s | 0.028% |
| 3 — + essentia's binding layer | 2.247 s | 0.730% | 3.969 s | 0.221% |
| **4 — + interpreter boot & import** | **3.455 s** | **1.122%** | **5.177 s** | **0.289%** |

**Tier 4 is the number a C++ proposal gets to claim: 1.12% of a 10-minute file, 0.29% of a
60-minute one.** It is an upper bound in every term — tier 1 absorbs CPython runtime work a C++
program also does, tier 3 is a profiler-derived fraction, and tier 4's import includes `dlopen`ing
libessentia and libtensorflow, which a C++ binary also pays.

### 4a. The same question for memory, which is where a rewrite could plausibly do better

A C++ binary would still map libessentia and libtensorflow. What it unambiguously avoids is the
interpreter itself; what it *might* avoid is some of the Python module objects. The import's
217.15 MiB splits **64.29 MiB file-backed** (shared-library RSS — a C++ program pays this too) and
**146.77 MiB anonymous** (interpreter heap **plus** TensorFlow's and essentia's own initialization
allocations, which a C++ program also pays, and which this spike cannot separate without building
the C++ program). So:

| | saving | vs `dur_600` improved peak (1.788 GiB) | vs `dur_3600` improved peak (2.812 GiB) |
| --- | ---: | ---: | ---: |
| **floor** — the bare interpreter, unambiguous | **9.41 MiB** | **0.51%** | **0.33%** |
| **ceiling** — the entire import footprint, an overstatement | 217.15 MiB | 11.9% | 7.5% |

The peak is dominated by TensorFlow arenas and per-inference activations (`phaze-mqq5` §3–4), which
are the same in either language and which `batchSize` already moved by **33.7%** — two orders of
magnitude more than the floor and three times the ceiling. **Memory is not a C++ argument here; it is
a `batchSize` argument, and that lever is already measured and free.**

______________________________________________________________________

## 5. The GIL, measured in the architecture phaze actually uses

The bead asks for this specifically, and warns against assuming it is nonzero. It is not zero — it is
*total* — and it costs nothing, for a reason that turns out to already be in the codebase.

### 5a. essentia holds the GIL for 99.86% of wall clock

A probe thread sleeping in 5 ms bursts, alongside a main thread doing one real 180 s coarse decode
and three real musicnn inferences. Every millisecond of the probe's lateness is a millisecond the GIL
was held by native code:

| segment | wall (s) | **GIL denied to the probe (s)** | **%** |
| --- | ---: | ---: | ---: |
| `EasyLoader` decode, 180 s @ 16 kHz | 9.706 | 9.700 | **99.9%** |
| `TensorflowPredictMusiCNN` construction | 0.018 | 0.014 | 77.5% |
| inference #1 | 2.784 | 2.779 | **99.8%** |
| inference #2 | 2.702 | 2.696 | **99.8%** |
| inference #3 | 2.810 | 2.805 | **99.8%** |
| **whole run** | **18.02** | **17.995** | **99.86%** |

The probe completed **6 iterations in 18 seconds**; its longest single stall was **9.7 s** — the
entire decode. This converts `phaze-7i0k` §9's "the in-process sampler is GIL-starved" from a
methodological warning into a measured quantity: **neither essentia's algorithms nor
`TensorflowPredict` release the GIL around their native work.**

### 5b. Threads buy zero overlap; processes buy 98%

So what does that cost? The decisive experiment: decode and inference, single-threaded TF
(`TF_NUM_INTRAOP_THREADS=1 TF_NUM_INTEROP_THREADS=1 OMP_NUM_THREADS=1`) so four physical cores are
not the limiter, over identical work, three ways.

| arm | wall (s) | **overlap efficiency** |
| --- | ---: | ---: |
| solo decode | 24.941 | — |
| solo inference | 29.111 | — |
| sum — no overlap at all | 54.052 | 0% |
| perfect-overlap floor `max(d, i)` | 29.111 | 100% |
| **serial** — one thread, back to back | **54.090** | — |
| **threaded** — one Python thread each | **54.076** | **−0.09%** |
| **two processes** — released from a barrier | **29.610** | **98.0%** |

**Python threads deliver exactly nothing**: 54.076 s against a serial 54.090 s, a ratio of 0.9997.
The GIL serializes them completely, as §5a predicts. **Two processes deliver 98%** of the ideal, on
the same hardware, through the same libraries, on the same tasks.

### 5c. phaze already bought the fix, and wrote down why

This is what settles the question. `services/analysis_exec.py`'s module docstring, shipped in
Phase 101 (`phaze-bo3p.2`):

> Because essentia's C++ holds the GIL of the process it runs in, moving it out of the parent keeps
> the parent's asyncio event loop free: `progress_cb` fires ON the loop as protocol lines arrive, so
> progress POSTs go out mid-analysis (the OBS-03 fix for the 0→100% bar jump).

**The one measurable cost of the GIL — that no other Python code in the process can run — was
identified and closed by an exec boundary that already ships.** The C++ pitch here ("escape the GIL")
is a proposal to solve a problem phaze solved with `create_subprocess_exec`. §5b is the exchange rate
between the two solutions: **0% for the one C++ would replace, 98% for the one phaze has.**

### 5d. What in-job overlap would be worth even if it were free

Suppose it were free anyway. On the improved baseline the decode that could overlap with inference is
**127.45 s of a 1 793.57 s run**, so perfect decode-∥-inference overlap saves at most **7.1%** — and
**4.9%** on `dur_600`. That is the *ceiling*, and it requires a free core to run the decode on.

**At the production operating point there is no free core.** `phaze-3j67` measured this node at 93.3%
CPU with two concurrent extractors and **99.9% from W=4**, which is the `cap` it recommends. Work
moved off one job's critical path lands on cores another job is already using. The throughput ceiling
is four physical Haswell cores ≈ **30 files/hour**, and every thread configuration tested converged
there within 7%. **In-job parallelism on a saturated node is a latency re-arrangement, not a
throughput gain, and it is worth 0.**

______________________________________________________________________

## 6. Is there a hot path for a narrow native extension?

The bead asks for this as the cheap alternative to a rewrite, and it is the right question — if a hot
Python path survived, a 200-line extension module would be far better value than a port. §3's phase
table says the only candidate is the prediction-dict assembly. Priced directly against the real label
counts, read from the shipped `.json` sidecars (33 characteristic heads carrying **66 labels between
them** — they are almost all binary — plus the genre model's **400**):

| | |
| --- | --- |
| assembly for a whole file at `coarse_cap = 30` (33 heads + genre, per window, plus the genre top-10 sort) | **0.98 ms** |
| the same without the genre sort | 0.93 ms |

**0.98 ms per file.** A native extension for it would be worth **0.0003%** of the improved
`dur_3600` wall clock, and would have to marshal 34 numpy arrays across a boundary to collect it.

The profiler agrees there is nothing else. Exclusive time, `dur_600` at production caps:

| function | tottime (s) | what it is |
| --- | ---: | --- |
| `Algorithm.__compute__` | **338.706** | **native — 98.4% of wall** |
| `Algorithm.__configure__` | 3.132 | native |
| `essentia/standard.py:39(__init__)` | **1.897** | **essentia's own binding layer, not phaze's** |
| `gc.collect` | 0.213 | CPython runtime |
| `analysis.py:208(_release_classifier)` | 0.159 | **the largest phaze-owned frame in the profile** |
| `analysis.py:623(_analyze_fine_windows)` | 0.015 | |
| everything else phaze owns | < 0.003 each | |

**No function phaze wrote exceeds 0.16 s of exclusive time**, and that one is a `dict.pop` plus the
call frame around `gc.collect`. There is no target.

### 6a. The largest Python-side win that exists — and it is still Python

The profile does name a real cost, and it is not in phaze's *logic*, it is in how often phaze asks
essentia to build things. `_analyze_fine_windows` constructs `RhythmExtractor2013` and `KeyExtractor`
**inside the per-window loop**, and neither takes a per-window parameter. On `dur_3600` that is 60
constructions costing **7.55 s**. Three arms over the same 60 decoded buffers:

| arm | fine-tier wall (s) | construction (s) | **output mismatches vs `fresh`** |
| --- | ---: | ---: | ---: |
| **`fresh`** — construct per window (what `main` does) | **31.504** | 7.550 | — |
| **`hoist`** — construct once, reuse | **23.932 (−24.0%)** | 0.000 | **0 / 60** |
| `reset` — construct once, reuse, `reset()` between windows | 23.941 (−24.0%) | 0.000 | **0 / 60** |

Mismatches are compared on the full `(window_index, bpm, "key scale", confidence)` tuple for all 60
windows. **`reset()` is not required**, which is worth recording because it is the thing a reader
would reasonably worry about.

**7.57 s on a 1 793.57 s improved run is 0.42% of wall — 38× the entire Python orchestration residual
(0.197 s) — and it comes from moving two lines out of a loop.** It is offered as an observation, not
a recommendation: it is a product change and belongs to a bead, and it should be re-verified against
real audio and against the failure-isolation semantics (a raising extractor is currently discarded
with the window; a reused one would survive it) before anyone ships it. But it makes the point this
spike exists to make in the sharpest available form: **the largest Python-attributable cost in the
pipeline is fixed by writing slightly different Python.**

______________________________________________________________________

## 7. The two questions this spike is not allowed to re-litigate

Both are settled upstream of here, by measurement, and both are recorded so a future reader does not
reopen them in C++.

### 7a. essentia's "single-threaded instances across cores" guidance

`phaze-3j67` §6 ran it literally — `TF_NUM_INTRAOP_THREADS=1`, one extractor per core:

- **Validated to the physical core count**: 3.61× on 4 workers, **90.3% efficiency**.
- **Contradicted past it**: 4 → 8 workers gained **+3.9%** while per-file wall **doubled**, because
  workers 5–8 land on hyperthread siblings. "Available CPU cores" has to mean *physical* ones.
- **Irrelevant to the outcome here**: its best is 27.2 files/hour against 29.6 for the recommended
  configuration, because both routes fill the same four physical cores.

The premise the C++ pitch rests on — *"phaze runs one single-threaded extractor per pod, so escaping
the GIL would let it parallelize"* — is **false in both halves**. `phaze-3j67` §2 measured **one
extractor consuming ~6.2 of 8 logical cores**: TensorFlow sizes its intra-op pool from the core count
and phaze never overrides it. **There is no serial process to parallelize.** §5b's process arm shows
what the parallelism that *does* exist is worth, and phaze already has it.

### 7b. Standard vs streaming mode

`phaze-rc1q` §2 settled this by reading upstream's source, and it dissolves rather than resolves:
standard `EasyLoader` **is** a `scheduler::Network` running the streaming `EasyLoader` inside a
private adapter, and all three `TensorflowPredict*` families are `VectorInput >> «the streaming
algorithm» >> Pool`. **phaze already runs 80–90 streaming networks per file.** There is no faster
native mode to switch to, in Python or in C++ — the documentation's "slight performance cost" of
standard mode is the per-call adapter, measured there at **0.55 s (fine) / 26.9 s (coarse) per
call**, and the hybrid removes it by paying the adapter twice per file instead of ninety times.

A C++ port would face **the same choice with the same two options**, and `phaze-rc1q` has already
made it, measured it, and proven the output byte-identical — which §2a and §2b reproduce here by
sha256.

______________________________________________________________________

## 8. What a C++ port would actually look like — read from upstream's own extractors

The bead's note points at `MTG/essentia@master` `src/examples/` as the ground truth for shape and
cost. It is, and it argues the same way the measurements do.

**`streaming_musicnn_predict.cpp` (179 lines) is the canonical single-model extractor.** Strip the
argument parsing and the pipeline is:

```cpp
Algorithm* audio = factory.create("MonoLoader", "filename", audioFilename, "sampleRate", 16000.0);
Algorithm* tfp   = factory.create("TensorflowPredictMusiCNN", "graphFilename", graphName, "output", outputLayer);
audio->output("audio")     >> tfp->input("signal");
tfp->output("predictions") >> PC(pool, "predictions");
Network n(audio);
n.run();
// ... then a standard-mode PoolAggregator with defaultStats {"mean"}
```

Set that beside what `analysis.py` does today (`_get_classifier` + `_predict_single`):

```python
classifier = es.TensorflowPredictMusiCNN(graphFilename=graph_path)
activations = classifier(audio_16k)
return np.mean(activations, axis=0)
```

**These are the same program.** Same factory, same graph, same `scheduler::Network`, same mean. The
C++ version is not a different algorithm expressed more efficiently — it is the identical native call
sequence with the binding removed, which is exactly why §3 and §4 measure the binding at 0.05% and
the whole Python tax at ~1%.

**`streaming_extractor_music` — essentia's reference pipeline — is the scale marker, and it cuts the
other way.** Two things about it are worth stating precisely:

- Despite the name, the 159-line `main()` is `using namespace essentia::standard;` and delegates to
  the **`MusicExtractor` algorithm**: **650 lines of `musicextractor.cpp`** plus a 7.6 KB header, for
  a pipeline of comparable scope to phaze's analyze. phaze's equivalent is **369 lines of code** in
  `analysis.py` (862 total: 278 docstring, 52 comment) — already written, already tested.
- **It decodes each file three times.** `musicextractor.cpp` stands up an `AudioLoader` for metadata
  (line 468), an `EasyLoader` + `scheduler::Network` for the low-level/rhythm stage (238/256), and
  `loader_2` + `network_2` for the beats-loudness/tonal stage that depends on the first (262/274).
  **Upstream pays a full re-decode to avoid holding two stages live at once** — the same structural
  tension `phaze-rc1q` §9b identified between single-pass decode and single-graph residency, resolved
  by upstream in the *more* expensive direction. **A C++ port would inherit a reference design already
  beaten by the Python one it replaced.**

Two further honest notes on shape:

- **essentia's claim that Python streaming code ports straightforwardly to C++ is true**, and
  `phaze-rc1q` §9c records it as a *portability* benefit of the hybrid. It cuts both ways: if the port
  is mechanical, the port buys mechanical results, and §4 measures those at ~1%.
- **The examples are single-model, single-pass programs.** None implements what phaze needs on top:
  two tiers, window striding to a cap, per-window failure isolation, model-major graph residency, the
  five-field coverage contract, the progress protocol, the `phaze-zibn` total-decode-failure floor.
  Those are the 369 lines — and they are precisely the part measured at 0.05%.

______________________________________________________________________

## 9. The cost side

A recommendation that reports only a speedup is incomplete; here the speedup is ~1%, so the cost side
is the whole decision.

### 9a. Cross-arch build burden, on an image whose Python pin is already hostage

`Dockerfile.agent-arm64` is pinned to **Python 3.13 — the one place in the repo that is not on
3.14** — because no `essentia-tensorflow` aarch64 wheel exists, so the image builds essentia **from
source** against the aarch64 `tensorflow` wheel, and TensorFlow publishes **no cp314 aarch64 wheel**.
(`phaze-mqq5` §6 verified this against PyPI on 2026-08-06: `tensorflow` 2.21.0 tops out at cp313,
while `onnxruntime` 1.28.0 and `ai-edge-litert` 2.1.6 both ship cp314 aarch64.)

That file already carries **four numbered fixups** to make libtensorflow link and run — a dangling
symlink repoint, a `libtensorflow_cc.so.2` pywrap remap, `LIBRARY_PATH`/`LD_LIBRARY_PATH` wiring, and
a dual-OpenMP runtime conflict the file itself labels *"the one OPEN production blocker"*, mitigated
with `OMP_NUM_THREADS=1`.

**A C++ component sits alongside all of that and adds its own.** It needs essentia's *headers* and a
linkable libessentia on **both** architectures — which the x86 path gets today only as an opaque
manylinux wheel, so **x86 would acquire a source build it does not currently have**. The ABI
constraint the Dockerfile already documents (`-D_GLIBCXX_USE_CXX11_ABI` must match between essentia
and libtensorflow — essentia issue #977) would then bind a third binary, and the OpenMP conflict a
fourth. This is the single largest cost in the column, and it is paid on **every** image build,
forever.

Note the asymmetry with `phaze-mqq5`'s finding: a runtime swap *removes* the 3.13 pin. A C++
component removes nothing and adds a second cross-arch toolchain.

### 9b. Loss of the Python ecosystem at that layer, and the `essentia-tensorflow` coupling

- **The model layer is `essentia-tensorflow`-shaped either way.** `phaze-mqq5` §2b showed
  `TensorflowPredict*` is a seven-node composite whose framing constants (`patchSize`,
  `patchHopSize`, `lastPatchMode`, `VectorRealToTensor.lastBatchMode`, `batchSize`) live in
  `configure()` and **move with the wheel**. In C++ they move with a pinned source SHA instead — the
  same exposure, with a slower upgrade path and no dependabot.
- **numpy goes.** `np.mean(activations, axis=0)` plus the float conversions are three lines today; in
  C++ they are `PoolAggregator` plus manual marshalling, which is what upstream's example does and
  part of why its 15-line pipeline needs 179 lines around it.
- **The result boundary already exists in Python.** `analyze_file`'s dict is serialized to JSON,
  validated by pydantic schemas the rest of the repo shares, and already crosses a **JSON-over-pipe**
  boundary at `analysis_child` (`phaze-bo3p.2`, 158 lines). A C++ analyze's best case is to reproduce
  that protocol exactly — i.e. to re-implement a boundary phaze already has, in a language nothing
  else in the repo speaks.

### 9c. Maintainability and debuggability against a codebase that is Python throughout

| | |
| --- | --- |
| code that would be rewritten | **369 lines** (`analysis.py`, excluding 278 docstring + 52 comment lines) |
| tests directly defending it | **2 149 lines** across 6 files (`test_analysis.py` 855, `test_analysis_model_major.py` 396, `test_analysis_exec.py` 307, `test_analysis_long_file.py` 207, `test_analysis_enqueue.py` 201, `test_analysis_child.py` 183) |
| test files referencing the module or `analyze_file` | **23** |
| C or C++ currently in the repo (outside `.venv`) | **none** |
| repo Python | 52 424 lines |

The docstrings are not decoration — **they are where this molecule's measurements live.**
`_release_classifier`'s docstring records the 3.751 → 0.263 GiB residency measurement that justified
`phaze-15sw`; `_sweep_one_model`'s records *why* the caught exception is not retained (a retained
traceback pins `_predict_single`'s frame and with it the classifier, re-creating exactly the
co-residency that restructure removed). **A rewrite discards the written record of five spikes'
findings along with the code**, or re-transcribes it into a language nobody else in the repo writes.

Debuggability is the practical cost. Today a bad window is a Python traceback in `structlog` with
`exc_info=True`, and the failure-isolation semantics (`phaze-zibn`'s total-decode-failure floor, the
per-window kill list, the `finally` that bounds graph residency) are ordinary `try`/`except`/`finally`.
In C++ they are error handling across an essentia `Network` whose scheduler owns the call stack, and
a crash is a core file in a container.

### 9d. Where the *actual* remaining wins are, for comparison

| change | measured effect | cost |
| --- | --- | --- |
| `phaze-mqq5` `batchSize=32` | **−33.7% peak** for +0.36% wall | **one keyword argument** |
| `phaze-rc1q` hybrid decode | **−41.1% wall** at saturated caps, output byte-identical (reproduced here) | ~20 lines, one function per tier |
| `phaze-3j67` `TF_NUM_INTRAOP_THREADS=4` | −42% per-process peak for +0.9% throughput | one ConfigMap entry |
| §6a hoisting two constructors out of a loop | −24.0% of the fine tier, 0/60 output mismatches | two lines, and a bead to verify it |
| **a C++ rewrite** | **≤1.12% wall, 0% throughput, ≤0.51% peak** | **a second cross-arch toolchain, forever** |

______________________________________________________________________

## 10. The options, priced

### 10a. Full rewrite of the analyze pipeline in C++ — **reject**

Buys **≤3.46 s per 10-minute file / ≤5.18 s per 60-minute file** (§4) and **0 throughput** (§5d,
§7a). Costs a cross-arch C++ toolchain on an image already fighting a TensorFlow ABI (§9a), the loss
of 369 lines of measured, documented Python and its 2 149 lines of tests (§9c), and a permanent
divergence from a repo that is Python throughout. **The upside is smaller than the difference between
two arms of this spike that did identical native work** (base and batch32 differ by 0.44 s of wall).

### 10b. Rewrite only the decode path in C++ — **reject; already solved better in Python**

The decode was the strongest candidate: `phaze-esut` §8 measured `EasyLoader`'s non-seeking
O(n_windows × duration) behaviour, and a 12-hour file spending 6.15 hours there is exactly the shape
that justifies going native. **`phaze-rc1q` fixed it in Python** — 10.7× on a 60-minute file
(reproduced here at 1 367.83 → 127.45 s), ≈18× on a 720-minute one, output byte-identical — because
the fix was **compositional, not linguistic**. A C++ version of the same fan-out would run the same
`scheduler::Network`; §3's phase table measures the Python side of the whole hybrid fine tier at
**0.041 s**.

Worse, essentia exposes no seek in either language (`phaze-rc1q` §7a), so the residual per-window copy
term survives a port. C++ would inherit the same asymptotics with the same constant — and upstream's
own reference extractor re-decodes three times (§8).

### 10c. A narrow native extension for one hot path — **reject, for want of a target**

This was the option most likely to survive, and it does not, because §6 finds no hot path. The best
candidate is **0.98 ms per file**; the largest phaze-owned frame in a full profile is **0.159 s** and
is a `dict.pop` around `gc.collect`. The largest Python cost center anywhere in the process is
**essentia's own `standard.py:__init__`** — not phaze's code, and not extensible without forking
essentia's bindings. And the thing that construction cost actually calls for is §6a's two-line hoist,
which is worth **38× more than the entire orchestration residual** and stays in Python.

### 10d. Do nothing about C++; ship the Python wins — **adopt**

`phaze-mqq5`'s `batchSize` and `phaze-rc1q`'s hybrid are worth **33.7% of peak and 41.1% of wall
clock**, and §2c measures for the first time that they compose in the favourable direction — landing
at 2.812 GiB, **under** `phaze-3j67`'s 3Gi request, where the hybrid alone breaches it. That is where
the remaining engineering value is, by two to three orders of magnitude.

______________________________________________________________________

## 11. Recommendations

| | action | why |
| --- | --- | --- |
| 1 | **NO-GO on a C++ rewrite of the analyze pipeline, in whole or in part. Close the question rather than deferring it.** | §4: the entire Python tax is **1.12%** of a 10-minute file and **0.29%** of a 60-minute one, and ≤0.51% of peak. §5: the GIL costs 0 in phaze's process model. §7a: there is no serial extractor to parallelize. §9: the cost is a permanent second cross-arch toolchain. |
| 2 | **Record that "escape the GIL for in-job parallelism" is answered, and how.** | §5. essentia holds the GIL **99.86%** of wall; Python threads get **0%** overlap and two processes get **98%**; `analysis_exec.py` already execs a child *for this exact reason* and says so in its docstring. Any future proposal on these grounds has to argue against a measurement **and** a shipped design. |
| 3 | **Stop carrying the "one single-threaded analyze process per pod" premise.** | §7a / `phaze-3j67` §2: one extractor consumes ~6.2 of 8 logical cores. The sentence appears in this bead, in the backlog note, and in `homelab`'s `backends.toml.j2`; it is wrong in both halves, and it is what makes the C++ case look plausible on paper. |
| 4 | **Ship `phaze-mqq5` recommendation 1 (`batchSize=32`) and `phaze-rc1q` recommendation 1 (hybrid decode). §2c is the joint measurement `phaze-mqq5` §5c asked for: they compose favourably — 2.812 GiB, under the 3Gi request, at −41.1% wall.** | §2b, §2c. Note the sum `phaze-mqq5` declined to ship was **optimistic by 0.420 GiB**; the conclusion survives but the arithmetic did not, which vindicates that refusal. `phaze-rc1q` recommendation 3 is **not** applied here, so this is a conservative reading, and its recommendation 7 (re-measure before re-sizing) still stands. |
| 5 | **File the §6a constructor hoist as a product bead, not as part of this spike.** | §6a: **−24.0% of the fine tier, 0/60 output mismatches, `reset()` not required** — 0.42% of a saturated-cap file, 38× this spike's entire orchestration residual, from two lines. Needs verification against real audio and against per-window failure isolation before it ships. |
| 6 | **If analyze throughput becomes the binding problem, buy physical cores.** | §5d, §7a, `phaze-3j67` §11 recommendation 6. 30 files/hour is four Haswell cores. No language change moves it. |
| 7 | **If a Python-side cost ever does appear, re-run this spike's harness before proposing a rewrite.** | §1a. Seam accounting is ~120 lines and answers the question in one run. The instrument, not the conclusion, is the reusable part. |

______________________________________________________________________

## 12. What this measurement does and does not support

- **Supported:** the native/Python split of `analyze_file` on this node and image, for four
  configurations across two files, by exhaustive seam instrumentation and independently by cProfile;
  the fixed per-file interpreter + import tax in wall clock and RSS, and its file-backed/anonymous
  split; the GIL-hold fraction under real essentia and TensorFlow calls; the serial / threaded /
  two-process overlap comparison; the joint composition of the `batchSize` and hybrid-decode changes;
  the constructor-hoist wall and output identity; independent reproduction of `phaze-15sw`,
  `phaze-rc1q` and `phaze-mqq5` baselines — two of them **by result sha256** — under one harness.
- **Not supported: that a C++ implementation would run the native layer at the same speed.** It is
  *assumed*, and the assumption favours C++. The libraries are the same, so the same essentia
  algorithms and the same `TF_SessionRun` calls execute either way — but no C++ implementation was
  built, and none should be on this evidence.
- **Not supported: anything about a C++ implementation that changes the *algorithms*.** This spike
  prices *translating* the pipeline. A different pipeline (fewer models, a `bsdynamic` genre graph,
  seek-based extraction) is a different question with a different answer, and belongs to
  `phaze-mqq5` §3b and `phaze-rc1q` §7a.
- **Not supported: that the tier-1 residual is exactly Python and nothing else.** §1a is explicit
  that it is an **upper bound** on phaze's layer — it absorbs CPython refcounting, allocator work and
  native destructors. Tier 3's binding-layer term is a *profiler-derived fraction* applied to measured
  construction time, not a direct measurement; it is the least precise number in §4 and it is
  deliberately generous.
- **Not supported: the §6a hoist as a shippable change.** It is measured on synthetic audio, on the
  fine tier alone, without the per-window failure-isolation path exercised. Recommendation 5 is to
  file it, not to land it.
- **Not supported: transfer to a different node.** The absolute walls are Haswell-and-this-image
  specific. What transfers is the ratio — the native layer would have to shrink by three orders of
  magnitude before the Python layer became visible.
- **Synthetic audio is validated for peak and inherited for wall time**, per `phaze-7i0k` §6b and
  `phaze-3j67` §10. A sine pair is cheap for the beat trackers, so wall-clock totals are an **upper
  bound** on real-audio throughput — which if anything *understates* the native share, since real
  audio makes the native layer more expensive and leaves Python's constant work unchanged.
- **One methodological note.** §5a is the first direct measurement of the artefact `phaze-7i0k` §9
  warned about: an in-process sampler thread would have received the GIL **6 times in 18 seconds**
  here. Every peak in this document is `VmHWM` read once at exit; every CPU figure is host-side. Do
  not substitute an in-process sampler.

______________________________________________________________________

## 13. What this changes upstream

- **The bead's own framing.** Its four skeptical priors were right in substance and wrong in two
  load-bearing specifics: the extractor is **not single-threaded** (`phaze-3j67` §2), and the decode
  bottleneck it expected `phaze-5lop` to fix **has already been designed away in Python** by
  `phaze-rc1q`. So the C++ case is measured against a baseline **41.1% faster** than the one it was
  filed against, and it loses by more than it would have then.
- **`phaze-rc1q` §12's handoff to this bead** — *"the C++ evaluation should be scoped against the
  hybrid, not against today's code, and should note that upstream's own `MusicExtractor` re-decodes
  rather than widening its network"* — is honoured on both counts (§1b, §8). Its portability
  observation is confirmed and **inverted in significance**: a mechanical port buys mechanical
  results, and §4 measures those at ~1%.
- **`phaze-mqq5` §5c's refusal to ship an arithmetic sum** is honoured and answered: §2c measures the
  joint configuration and finds the arithmetic was **optimistic by 0.420 GiB**, with the conclusion
  intact. Its `batchSize` result reproduces to the digit on `danceability`, twice.
- **`phaze-3j67` §11 recommendation 6** — *"when throughput becomes binding, buy cores, not RAM and
  not concurrency"* — gains a third clause: **and not a language**.
- **`phaze-7i0k` §9's GIL-starved-sampler warning** is upgraded from methodology to a measured
  quantity: **99.86% of wall, longest stall 9.7 s, 6 probe iterations in 18 s**.
- **`services/analysis_exec.py`** turns out to be the most important prior art in this molecule: the
  shipped answer to the only real GIL cost, with the reason already written in its docstring. Nothing
  about it needs to change; it needs to be *cited* the next time someone proposes escaping the GIL.

______________________________________________________________________

## Appendix — reproducing this

Five short scripts, in the shape `phaze-esut` / `phaze-7i0k` / `phaze-3j67` / `phaze-rc1q` /
`phaze-mqq5` established (drive the **real** `phaze.services.analysis`; never reimplement the
pipeline):

- **`orchbench.py`** — installs a timing proxy over the module's `es` / `np` / `json` / `gc` names,
  then calls the **real** `analyze_file`. Reports wall, per-seam native seconds (construction and
  call separately), the per-phase native/Python split, `VmHWM` read once at exit, and the result
  sha256. `--arm base|batch32|hybrid|improved` selects the configuration: `batch32` injects
  `batchSize=32` at construction for the musicnn/vggish families only; `hybrid` swaps in
  `phaze-rc1q`'s per-tier fan-out decode plus one `malloc_trim(0)` per tier, leaving every other line
  of `main`'s code intact.
- **`gilbench.py --exp A`** — a probe thread sleeping in 5 ms bursts alongside real decode and
  inference; every millisecond of lateness is a millisecond the GIL was held by native code.
- **`gilb.py`** — the overlap experiment. The same two tasks run serially, in two Python threads, and
  in two **re-exec'd** child processes released from a file barrier. Children are re-execs, not
  `fork()`: forking a process with TensorFlow loaded hangs (observed here, and the reason the first
  attempt was discarded and re-written).
- **`profbench.py`** — cProfile with C-call tracking over a full `analyze_file`, plus a
  microbenchmark of the per-model prediction-dict assembly against the **real** label counts, read
  from the shipped `.json` sidecars.
- **`hoist.py`** — the §6a arm: `fresh` / `hoist` / `reset` over the same decoded buffers, comparing
  the full `(window_index, bpm, key, confidence)` tuple for every window.

The pod is a bare `sleep infinity` pod on the burst node using the deployed job image, with the
`phaze-models` PVC mounted **read-only** and a host scratch dir, and **no Kueue queue label** so it
consumes no quota:

```sh
kubectl apply a pod:  image ghcr.io/simplicityguy/phaze/job:2026.8.0
                      volumeMounts: phaze-models (ro) at /models, hostPath scratch at /scratch
                      command: ["sleep", "infinity"]
```

The image ships the pre-`phaze-15sw` `analysis.py`, so the shipped model-major pipeline is measured
by copying `/app/src` to a scratch tree, overwriting `services/analysis.py` with `main`'s (sha256
`45a84a70…` — verified equal to the overlay `phaze-15sw`, `phaze-3j67`, `phaze-rc1q` and
`phaze-mqq5` used) and putting that tree first on `sys.path`.

Test audio — `phaze-esut`'s generator, touching no real library:

```sh
for d in 600 3600; do
  ffmpeg -loglevel error -y \
         -f lavfi -i "sine=frequency=440:duration=$d" \
         -f lavfi -i "sine=frequency=554:duration=$d" \
         -filter_complex "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]" \
         -map "[a]" -ar 44100 -c:a libmp3lame -b:a 192k "dur_$d.mp3"
done
```

**Node-CPU gate, per run** (host-side `/proc/stat` deltas across each run; §1c):

| run | node CPU | elapsed |
| --- | ---: | ---: |
| `dur_600` base | 71.3% | 347 s |
| `dur_600` batch32 | 71.2% | 347 s |
| `dur_600` hybrid | 78.4% | 308 s |
| `dur_600` improved | 78.3% | 310 s |
| `dur_3600` base | **52.9%** | 3 046 s |
| `dur_3600` improved | 81.0% | 1 796 s |
| `dur_600` cProfile | 71.3% | 345 s |

The `dur_3600` base run's 52.9% is the expected value, not an anomaly: 45% of its wall clock is the
single-threaded `EasyLoader` decode, which `phaze-rc1q` §1b measured at 12.5–13.6% node CPU.

**Four things to get right when re-running.** Enumerate the seams from the source, not from memory —
a missed seam inflates the Python residual and the whole conclusion depends on it being small. Run
everything **strictly one at a time**: `phaze-3j67` measured this node saturating at W=2, so a second
process does not perturb a wall-clock measurement, it invalidates it. Do not `fork()` a process with
TensorFlow loaded. And measure the interpreter-and-import tax and essentia's own binding layer
**separately** from the residual — together they are seven times the residual, and they are the two
Python costs a seam-only instrument silently omits.
