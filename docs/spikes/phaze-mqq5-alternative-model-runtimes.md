# phaze-mqq5 — alternative model runtimes (ONNX Runtime / TFLite / quantization / batching)

- **Bead:** `phaze-mqq5` (spike — "TensorFlow arenas are the dominant remaining term")
- **Date:** 2026-08-06
- **Tree:** branch `wt/bead/issue/phaze-mqq5`, forked off `main` at `75020e8`
- **Code under test:** the deployed analyze image `ghcr.io/simplicityguy/phaze/job:2026.8.0`
  with `main`'s model-major `services/analysis.py` overlaid (sha256 `45a84a70…` — the same
  overlay `phaze-15sw`, `phaze-3j67` and `phaze-rc1q` used), against the deployed
  `phaze-models` PVC (34 graphs, 3.1 GB)
- **Status:** spike. Measurement and verdict only. **No product code changed.**

______________________________________________________________________

## Verdict in one paragraph

**A different runtime is reachable — but it is not the win, and the win does not need it.**
essentia has exactly one inference backend (the TensorFlow C API, via `TensorflowPredict`);
the `OnnxPredict` PR is open, unmerged, conflicted, and adds only a *generic* algorithm with
no `OnnxPredict{MusiCNN,VGGish,EffnetDiscogs}` composites. But `TensorflowPredict*` turns out
to be a **thin composite over reusable, TF-free essentia algorithms** — `FrameCutter →
TensorflowInput{MusiCNN,VGGish} → VectorRealToTensor → TensorToPool → TensorflowPredict` —
and only the last node is TensorFlow. Driving that inner network from Python and handing the
tensors to onnxruntime **reproduces the shipped predictions to 1.15 × 10⁻⁵ max per-patch /
8.94 × 10⁻⁷ aggregated, with 0/34 top-1 disagreements and 0/34 patch-count mismatches** —
measured, not argued. So the seam exists and is exact. **What the runtime swap is *not* is
the memory win.** The dominant term is not the runtime's identity, it is the **batch of 64
patches essentia feeds it by default**: dropping `batchSize` 64 → 32 on the *shipped
TensorFlow path* takes the 34-model sweep from **2.188 GiB to 1.417 GiB (−35.2%)**, and the
whole of `analyze_file` on a 60-minute file at saturated caps from **2.588 GiB to 1.715 GiB
(−33.7%) for +0.36% wall clock**, with model activations identical to 1.19 × 10⁻⁷ (one float32
ulp) and zero argmax flips. That is a one-keyword, zero-conversion change. ONNX Runtime at the
same batch is lower still *in isolation* (1.133 GiB, −48.2%) — but **inside the real
`analyze_file` the ordering inverts**: ORT peaks at 1.800 GiB against
TensorFlow-at-batch-32's 1.501 GiB **and doubles the wall clock**, on top of a 34-model
conversion pipeline and a permanent second copy of the model corpus. Quantization is a **red
herring here**: the peak is activations and allocator arena, not weights — fp16 buys 5% of
peak and int8 buys nothing while **flipping the top-1 genre label** (aggregated Δ 0.651).
**Recommendation: take the batching lever now on TensorFlow; keep ONNX Runtime as the answer
to the arm64 question (it is the one thing batching cannot do — §6), not to the memory
question.**

______________________________________________________________________

## 1. Method, and what it can and cannot support

| | |
| --- | --- |
| **Host** | `vox` — Debian 13 (trixie), x86_64, **8 cores, 31 GiB RAM**. Confirmed idle before the session (`load avg 0.38`, no other agent's pods); arms run **strictly one at a time**, never concurrently |
| **Placement** | a bare `sleep infinity` pod on the burst node, deployed job image, `phaze-models` PVC mounted **read-only**, host scratch dir, **no Kueue queue label** — consumes no quota, and vox stays out of the phaze backend registry |
| **essentia** | `essentia-tensorflow` 2.1-beta6-dev (the pinned wheel), Python 3.14.6 |
| **Runtimes compared** | bundled TensorFlow (essentia's own libtensorflow) vs **onnxruntime 1.28.0** |
| **Converter** | `tf2onnx` 1.17.0 on `tensorflow-cpu` 2.21.0 / `onnx` 1.22.0, opset 18, in a **separate** py3.13 pod |
| **Audio** | **synthetic**: in-process deterministic sine+noise for the sweep harness; `ffmpeg` `sine`-pair mp3 (`dur_600`, `dur_3600`) for the end-to-end arm — `phaze-esut`'s generator |
| **Peak metric** | **`VmHWM`** — the kernel's own high-water mark, read from `/proc/<pid>/status` **from the host**, outside the container (`phaze-7i0k` §9: in-process sampling is GIL-starved). Sampled `VmRSS` at 0.4 s is reported alongside as a floor |
| **Contention gate** | node CPU from `/proc/stat` deltas and `MemAvailable`, recorded per run |

**No operator media was read, copied, or referenced.** Every input is synthesized. No
filename, path, or per-file metadata value from the library appears in this document. The
models PVC was mounted **read-only** and nothing in it was modified; the `.onnx` artifacts
live in a scratch directory, never beside the `.pb` originals.

**`VmHWM` is the number to read.** The 0.4 s `VmRSS` sampler misses short spikes — e.g. the
`tf_b16` arm samples 1.176 GiB but its kernel high-water is 1.442 GiB. Every "peak" quoted
below is `VmHWM` unless stated.

**Two limitations, stated up front.** (a) The runtime sweep drives the 34-model sweep over
synthetic 16 kHz buffers, not the whole of `analyze_file`; §5 closes that with an end-to-end
arm through the real `analyze_file`. (b) essentia never configures TensorFlow's
`SessionOptions`, so the TF arm silently uses **all 8 cores** (81% node CPU) while the ORT arm
is explicitly capped; wall-clock comparisons are therefore reported with node CPU beside them
and normalised to core-seconds in §3b.

______________________________________________________________________

## 2. Reachability, answered first

The bead says: if a non-TF runtime is not reachable without reimplementing essentia's model
layer, say so and stop. **It is reachable, and the reason is that "essentia's model layer" is
much thinner than the name suggests.**

### 2a. essentia has exactly one inference backend today

`src/algorithms/machinelearning/` contains nine `tensorflowpredict*.cpp` implementations and
nothing else — no ONNX, no TFLite, no second backend of any kind. `src/wscript` gates one list
of eleven algorithm names on the presence of libtensorflow:

```python
    algos = [ 'TensorflowPredict', 'TensorflowPredictMusiCNN', 'TensorflowPredictVGGish',
              'TensorflowPredictTempoCNN', 'TensorflowPredictCREPE', 'PitchCREPE',
              'TempoCNN', 'TensorflowPredictEffnetDiscogs', 'TensorflowPredict2D',
              'TensorflowPredictFSDSINet', 'TensorflowPredictMAEST',]
    if has('tensorflow'):
        ...
    else:
        print('- Essentia is configured without Tensorflow.')
        ctx.env.ALGOIGNORE += algos
```

**`TensorflowInputMusiCNN` and `TensorflowInputVGGish` are not in that list.** They are pure
DSP (mel bands) and survive a TF-free build. That single fact is what makes everything below
possible, and it is load-bearing for §6.

There *is* an upstream ONNX effort — **PR [MTG/essentia#1488](https://github.com/MTG/essentia/pull/1488),
"New feature: OnnxPredict algorithm"** — and it is worth being precise about its state,
because it is easy to over-read:

| | |
| --- | --- |
| opened | 2025-09-01, by `xaviliz` |
| last activity | **2026-03-03** (last review comment 2025-12-23) |
| state | **open, not merged, `mergeable_state: dirty`** (conflicts with master), 70 commits |
| scope | `onnxpredict.{h,cpp}` (654 lines) + a build script that compiles **ONNX Runtime from source** + wscript wiring |
| **what it does not add** | **any `OnnxPredict{MusiCNN,VGGish,EffnetDiscogs}` composite** |

So even on the day it merges, phaze could not swap `TensorflowPredictMusiCNN` for an
`OnnxPredictMusiCNN` — no such algorithm is proposed. It would also arrive only in a **new
wheel flavour** (`essentia-onnx`) that MTG does not publish; the PR's own instructions are a
`waf configure --with-onnx` source build. **Treat #1488 as evidence of upstream direction, not
as a delivery date.**

### 2b. What `TensorflowPredict*` actually does beyond `predict`

`TensorflowPredictMusiCNN::createInnerNetwork()`, in full:

```cpp
  _signal                                  >> _frameCutter->input("signal");
  _frameCutter->output("frame")            >> _tensorflowInputMusiCNN->input("frame");
  _tensorflowInputMusiCNN->output("bands") >> _vectorRealToTensor->input("frame");
  _vectorRealToTensor->output("tensor")    >>  _tensorToPool->input("tensor");
  _tensorToPool->output("pool")            >>  _tensorflowPredict->input("poolIn");
  _tensorflowPredict->output("poolOut")    >>  _poolToTensor->input("pool");
  _poolToTensor->output("tensor")          >>  _tensorToVectorReal->input("tensor");
```

Seven nodes; **one of them is TensorFlow**. And `TensorflowPredict::compute()` is, in full:
marshal each named `Tensor<Real>` out of the input `Pool` into a `TF_Tensor`, `TF_SessionRun`,
copy the outputs back into the output `Pool`, delete the tensors. **No normalization, no
framing, no aggregation.** The framing constants live in the composite's `configure()` and are
hardcoded to the training setup:

| family | `FrameCutter` | mel algorithm | bands | `patchSize` | `patchHopSize` | `lastPatchMode` | `VectorRealToTensor.lastBatchMode` | `batchSize` |
| --- | --- | --- | ---: | ---: | ---: | --- | --- | ---: |
| musicnn | 512 / 256 | `TensorflowInputMusiCNN` | 96 | 187 | 93 | `discard` | `push` (default) | **64** |
| vggish | 400 / 160 | `TensorflowInputVGGish` | 64 | 96 | 93 | `discard` | `push` (default) | **64** |
| effnet | 512 / 256 | `TensorflowInputMusiCNN` | 96 | 128 | 62 | `discard` | **`discard`** (explicit) | **64** |

Two of those cells are easy to get wrong and both were verified on the installed wheel rather
than from the docs. `lastPatchMode` (what to do with leftover *frames*) and
`VectorRealToTensor`'s `lastBatchMode` (what to do with a partial *batch*) are different
parameters; musicnn and vggish inherit `push`, so a trailing partial batch is emitted and no
patch is lost, while `TensorflowPredictEffnetDiscogs` explicitly passes `"discard"` and
compensates in standard-mode `compute()` with the `lastBatchMode="same"` zero-pad-then-truncate
(`phaze-rc1q` §3d). A replacement that inherits the wrong one of these silently drops patches.

phaze passes **only** `graphFilename` (`_get_classifier`, `services/analysis.py:159`), so every
one of those defaults is in force in production — including `batchSize = 64`, which §3 shows is
the whole story.

**Two corrections carried forward, both already made by `phaze-rc1q` and both re-verified here
against the C++:** there is no separate "standard implementation" (standard `TensorflowPredictMusiCNN`
wraps the streaming one and runs a `scheduler::Network`), and the normalization asymmetry is
`TensorflowPredictFSDSINet`-only — none of phaze's three families normalizes in either mode.
Nothing in this spike depends on either premise being re-litigated.

### 2c. The seam is real — and exact, by measurement

`VectorRealToTensor`, `TensorToPool`, `PoolToTensor` and `TensorToVectorReal` are
**streaming-only** in the Python bindings; `FrameCutter`, `TensorflowInputMusiCNN`,
`TensorflowInputVGGish` and `TensorflowPredict` are exposed in **both** modes. So the inner
network up to (not including) `TensorflowPredict` can be built and run from Python with
essentia's own algorithms — no reimplementation of framing or mel extraction — and its output
tensor read out of a `Pool`:

```python
pool = essentia.Pool()
vi  = ess.VectorInput(audio_16k)
fc  = ess.FrameCutter(frameSize=512, hopSize=256)             # composite's own constants
ti  = ess.TensorflowInputMusiCNN()
vrt = ess.VectorRealToTensor(shape=[batch, 1, 187, 96], patchHopSize=93, lastPatchMode="discard")
vi.data >> fc.signal; fc.frame >> ti.frame; ti.bands >> vrt.frame
vrt.tensor >> (pool, "t")
essentia.run(vi)                        # pool["t"] == exactly what TensorflowPredict would get
```

Everything a replacement must replicate is then **~40 lines**: `np.squeeze` of the channel
dimension (`TensorflowPredict`'s `squeeze=true`), and — for `discogs-effnet-bs64-1` only — the
`lastBatchMode="same"` zero-pad-then-truncate that `phaze-rc1q` §3d identified. Run the
converted graph, concatenate.

**Measured against the shipped path, all 34 models, one 180 s @ 16 kHz window:**

| | ONNX Runtime fp32 vs shipped TensorFlow |
| --- | --- |
| patch-count mismatches | **0 / 34** |
| top-1 (argmax) disagreements | **0 / 34** |
| max per-patch \|Δ\| | **1.150 × 10⁻⁵** (`discogs-effnet-bs64-1`) |
| max aggregated \|Δ\| (the value phaze actually consumes) | **8.941 × 10⁻⁷** |
| mean aggregated \|Δ\| | **1.795 × 10⁻⁷** |

`np.mean(activations, axis=0)` is what `_predict_single` returns, so the aggregated column is
the one that reaches `derive_mood` / `derive_style`. **8.9 × 10⁻⁷ is float32 round-off.** The
patch counts matching exactly (119 musicnn / 193 vggish / 180 effnet) is the stronger claim:
it means the Python-driven inner network reproduced essentia's framing and patch assembly
*structurally*, not just approximately.

### 2d. Reachability verdict

**Reachable, at the cost of ~40 lines of glue plus a model-conversion pipeline — not at the
cost of reimplementing essentia's model layer.** The bead's stop condition is not met, so §3
onwards evaluates.

But one thing *is* squarely on phaze: **upstream publishes essentially no ONNX.**
`essentia.upf.edu/models.html` carries **289 `.pb` references and exactly one `.onnx`**
(`discogs-effnet-bsdynamic-1.onnx`). Every one of phaze's 34 graphs — including
`discogs-effnet-bs64-1` — returns 404 for its `.onnx` sibling. Conversion is phaze's
permanent responsibility, not a download. §7 prices that.

______________________________________________________________________

## 3. The measurement — and the lever nobody was looking at

Full 34-model sweep in phaze's production order, one 180 s @ 16 kHz window, one graph resident
at a time (`phaze-15sw` shape). `VmHWM` is the kernel high-water; sampled peak is the 0.4 s
`VmRSS` floor.

| arm | batch | ORT arena | **VmHWM (GiB)** | sampled peak | wall (s) | node CPU |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| **TensorFlow — shipped defaults** | **64** | — | **2.188** | 2.135 | **85.40** | 81.1% |
| TensorFlow | 32 | — | **1.417** | 1.325 | 86.35 | 81.1% |
| TensorFlow | 16 | — | 1.442 | 1.176 | 87.53 | 81.1% |
| TensorFlow | 8 | — | 1.444 | 1.412 | 89.71 | 80.9% |
| TensorFlow | 1 | — | 1.277 | 1.111 | 132.99 | 55.4% |
| ONNX Runtime (1 thread) | 64 | on | 2.761 | 2.761 | 280.94 | 14.0% |
| ONNX Runtime (8 threads) | 64 | on | 2.761 | 2.761 | 176.15 | 43.2% |
| ONNX Runtime (8 threads) | 64 | **off** | 1.716 | 1.565 | 187.71 | 46.6% |
| ONNX Runtime (8 threads) | 16 | on | 1.315 | 1.315 | 175.99 | 43.2% |
| **ONNX Runtime (8 threads)** | **16** | **off** | **1.133** | 1.013 | 189.82 | 46.8% |
| ONNX Runtime (8 threads) | 1 | on | 0.908 | 0.889 | 199.77 | 49.9% |
| ONNX Runtime (8 threads) | 1 | off | 0.908 | 0.904 | 200.69 | 49.7% |

### 3a. `batchSize` is the dominant term, and it is a parameter phaze already controls

The bead frames batching as "phaze feeds one window at a time; whether batching raises or
lowers peak is unknown". The premise needs one correction: **phaze is already batching — at
64.** `batchSize` defaults to 64 on all three families and phaze passes no override, so every
inference stands up a `[64, patch, bands]` input and, far more expensively, `64 ×` the
intermediate activations of a musicnn / VGGish / EfficientNet forward pass.

Moving that one parameter:

| batch | VmHWM | Δ vs shipped | wall | Δ wall |
| ---: | ---: | ---: | ---: | ---: |
| **64** (shipped) | 2.188 | — | 85.40 | — |
| **32** | **1.417** | **−35.2%** | 86.35 | **+1.1%** |
| 16 | 1.442 | −34.1% | 87.53 | +2.5% |
| 8 | 1.444 | −34.0% | 89.71 | +5.0% |
| 1 | 1.277 | −41.6% | 132.99 | +55.7% |

**The knee is between 64 and 32 and everything from 32 down to 8 is flat at ≈1.42–1.44 GiB.**
Below the knee the residual is the graph + the allocator floor, not the batch. Batch 1 finds
another 0.16 GiB but pays 56% wall clock for it — that is the wrong trade on a node
`phaze-3j67` measured **CPU-bound at W=2**.

The same lever holds with four windows resident (the `phaze-15sw` model-major shape at
`coarse_cap = 4`): **2.259 GiB at batch 64 → 1.502 GiB at batch 16, −33.5%**, wall
341.70 → 348.15 s (+1.9%). The saving is a property of the inference, not of the window count.

### 3b. Output invariance under `batchSize` — proven, not assumed

Patches are independent; nothing in these graphs is batch-coupled on the path phaze uses.
(Several graphs *do* carry a training-mode bool Placeholder — `model/Placeholder_2` on the
vggish heads and on `voice_instrumental-musicnn-msd-1` — but `isTrainingName` defaults to the
empty string and phaze never sets it, so no training-mode input is ever fed and the
batch-statistics branch is never taken.) Measured anyway, all 34 models, aggregated
activations:

| comparison | max \|Δ\| on the aggregated vector | argmax flips |
| --- | ---: | ---: |
| TF batch 64 vs TF batch 16 | **1.192 × 10⁻⁷** | **0 / 34** |
| TF batch 64 vs TF batch 1 | **1.192 × 10⁻⁷** | **0 / 34** |
| ORT batch 64 vs ORT batch 16 | 5.960 × 10⁻⁸ | 0 / 34 |

1.19 × 10⁻⁷ is one float32 ulp near 1.0. **The batch lever is numerically free.**

One model is exempt and it matters: **`discogs-effnet-bs64-1`'s input Placeholder is
`[64, 128, 96]` — a hard-coded batch of 64**, which is what the `bs64` in its name means and
why `lastBatchMode` exists at all. The lever cannot move it, and every arm above keeps it at
64. The genre model therefore keeps its own arena; the 35% is delivered by the other 33.
(MTG publishes a `discogs-effnet-bsdynamic-1` variant — that is a *model-swap* question with
its own accuracy budget, scoped as a follow-up, not assumed here.)

### 3c. ONNX Runtime's default arena is a trap, and its isolated win does not survive §5a

At the shipped batch of 64, **ORT is worse than TensorFlow — 2.761 vs 2.188 GiB (+26.2%)**.
That is entirely ORT's CPU memory arena: `enable_cpu_mem_arena=False` + `enable_mem_pattern=False`
takes the same configuration to **1.716 GiB (−21.6% vs TF)**. Any comparison that leaves ORT's
defaults on is measuring the arena, not the runtime.

With both levers (batch 16, arena off) ORT reaches **1.133 GiB — 48.2% below the shipped
baseline and 20.0% below TensorFlow at the same batch**. That is a genuine additional win of
**0.31 GiB** on top of what batching alone delivers.

It costs wall clock. ORT ran 189.82 s against TensorFlow's 87.53 s at batch 16 — but the arms
did not use the same cores (essentia never sets `SessionOptions`, so TF took 81% of 8 cores;
ORT was capped and took 47%). Normalising to core-seconds:

| arm | wall × node CPU × 8 cores | core-seconds |
| --- | --- | ---: |
| TensorFlow, batch 16 | 87.53 × 0.811 × 8 | **568** |
| ONNX Runtime, batch 16, arena off | 189.82 × 0.468 × 8 | **711** |

**ORT costs ≈25% more CPU work for the same predictions**, and ~2.2× more wall clock at the
thread settings measured. On a node that is CPU-bound at W=2, that is the expensive side of
the trade.

**Do not stop reading at 1.133 GiB.** This is the *isolated* sweep — one buffer, no fine tier.
§5a re-measures the same configuration inside the real `analyze_file` and the ordering flips:
ORT lands at **1.800 GiB against TensorFlow-at-batch-32's 1.501 GiB**. The 0.31 GiB
"additional win" above does not survive contact with the shipped pipeline.

______________________________________________________________________

## 4. Quantization — the wrong lever for this peak

Both stacks convert cleanly from the fp32 ONNX (34/34, no failures) and shrink the corpus a
lot: **3.1 GB → 1.6 GB (fp16) → 780 MB (int8 dynamic)**. Neither moves the peak, because the
peak is not weights.

| arm (batch 16) | VmHWM | Δ vs ORT fp32 same config | wall | max aggregated \|Δ\| vs TF | argmax flips |
| --- | ---: | ---: | ---: | ---: | ---: |
| ORT fp32, arena on | 1.315 | — | 175.99 | 8.94 × 10⁻⁷ | 0 / 34 |
| **ORT fp16**, arena on | **1.246** | **−5.2%** | 189.34 | **2.03 × 10⁻²** | 0 / 34 |
| **ORT int8** (dynamic), arena on | **1.278** | **−2.8%** | **347.96** | **6.51 × 10⁻¹** | **1 / 34** |

- **fp16** halves the on-disk corpus and buys **5% of peak**. Its accuracy cost is
  2.03 × 10⁻² on the aggregated genre vector and ~10⁻³ on the mood heads — small, but three
  to four orders of magnitude worse than fp32 ONNX, for a memory saving an order of magnitude
  smaller than the batching lever. It also ran **slower**, because the CPU execution provider
  has no fp16 kernels for these ops and inserts Cast nodes.
- **int8 dynamic quantization is disqualified on accuracy.** Aggregated Δ **0.651** on
  `discogs-effnet-bs64-1` and it **flips the top-1 genre label** on the corpus. Mean
  aggregated Δ across all 34 is 3.3 × 10⁻². It is also **2× slower** than fp32 (348 s vs
  176 s) — dynamic quantization inserts per-run activation quantize/dequantize on a
  Conv-heavy graph. A static/QDQ calibration pass could do better, but it would need a real
  calibration corpus and it would be attacking a term worth ≤5% of peak.

**Stated tolerance.** For this pipeline the meaningful quantity is the aggregated activation
vector (`np.mean(activations, axis=0)`) that feeds `derive_mood` / `derive_style` /
`derive_danceability`, and the decision those functions make. A reasonable budget is
**aggregated |Δ| ≤ 10⁻³ and zero top-1 flips** — tight enough that no stored `mood` / `style`
string can change, loose enough to admit a runtime swap. **fp32 ONNX passes by three orders of
magnitude (8.9 × 10⁻⁷). fp16 fails the numeric half (2.0 × 10⁻²) while passing the decision
half. int8 fails both.**

______________________________________________________________________

## 5. End to end, through the real `analyze_file`

The sweep above isolates the runtime; this arm prices it where the baseline actually lives.
`analyze_file` from `main`, unmodified, production caps (`fine_cap=60`, `coarse_cap=30`). The
only substitution is `_get_classifier` — windowing, decode, the model-major sweep, aggregation,
failure isolation and serialization are the shipped code. §5a is the 10-minute file (the
comparator for `phaze-rc1q`'s 2.218 GiB); §5c is the 60-minute file at **saturated fine cap**,
which is the comparator for `phaze-15sw`'s **2.482 GiB** envelope and `phaze-rc1q`'s 2.505 GiB.

### 5a. The 10-minute file (4 coarse windows, caps not saturated)

| arm | **VmHWM** | Δ | wall | Δ wall | `bpm` / `key` / `mood` / `style` |
| --- | ---: | ---: | ---: | ---: | --- |
| **TensorFlow, shipped (`batchSize` 64)** | **2.210 GiB** | — | 345.50 s | — | 152.0 / — / relaxed / Electronic-Experimental |
| **TensorFlow, `batchSize` 32** | **1.501 GiB** | **−32.1%** | 345.94 s | **+0.1%** | **identical** |
| TensorFlow, `batchSize` 16 | 1.518 GiB | −31.3% | 351.80 s | +1.8% | **identical** |
| ONNX Runtime, batch 16, arena off | 1.800 GiB | −18.6% | 687.74 s | **+99.1%** | **identical** |

**The baseline reproduces the prior spikes.** 2.210 GiB against `phaze-rc1q`'s 2.218 GiB for
the same file and caps — **−0.4%**.

**The runtime swap loses end to end.** ORT was the *lowest* arm in the isolated sweep
(1.133 GiB) and is the *second-highest* here (1.800 GiB), above TensorFlow at the same batch.
The isolated sweep holds one buffer; `analyze_file` holds every coarse buffer concurrently
(`phaze-15sw`) and runs a fine tier first, so the ORT arm's 288 MB VGGish session and its
per-window essentia sub-network stack on top of PCM the sweep harness never held. It also
**doubled the wall clock**. Whatever the exact allocator mechanism, the operational statement
is unambiguous: **in the real pipeline, ONNX Runtime does not beat TensorFlow-with-batching on
either axis.**

### 5b. Output equivalence — and the one place it is not byte-identical

Every categorical field is identical across all four arms: `bpm` 152.0, `key` null,
`mood` `relaxed`, `style` `Electronic/Experimental`. The **`sha256` of the serialized result
is not**, and the reason is worth stating precisely rather than glossing:

| arm | `danceability` | Δ vs shipped |
| --- | --- | ---: |
| TensorFlow, `batchSize` 64 (shipped) | `0.5625000968575478` | — |
| TensorFlow, `batchSize` 32 | `0.5625000943740209` | **2.5 × 10⁻⁹** |
| TensorFlow, `batchSize` 16 | `0.5625000894069672` | 7.5 × 10⁻⁹ |
| ONNX Runtime, batch 16 | `0.5625000496705372` | 4.7 × 10⁻⁸ |

**This is not the `phaze-15sw` bar and this document does not claim it is.** `phaze-15sw` and
`phaze-rc1q` both proved byte-identical output; a batch regrouping changes float32 summation
order, so the last ulp moves and it survives into the JSON as a float64. The delta is
**2.5 × 10⁻⁹ on a value phaze stores to full float64 precision** — nine orders of magnitude
below anything a human or a query can distinguish, and four orders below the §4 tolerance.
Any test that asserts byte-identity across this change will fail, and should be written to
assert the categorical fields plus a tolerance instead.

### 5c. The 60-minute file at saturated caps — the number that compares to 2.482 GiB

`dur_3600`, 60 fine + 20 coarse, **fine cap saturated** — the shape `phaze-15sw`,
`phaze-3j67` and `phaze-rc1q` all sized against.

| arm | **VmHWM** | sampled peak | wall | Δ peak | Δ wall |
| --- | ---: | ---: | ---: | ---: | ---: |
| **TensorFlow, shipped (`batchSize` 64)** | **2.588 GiB** | 2.583 | 3,058.55 s | — | — |
| **TensorFlow, `batchSize` 32** | **1.715 GiB** | 1.681 | 3,069.57 s | **−33.7%** | **+0.36%** |

`bpm` / `key` / `mood` / `style` identical; `danceability` `0.5625221525629361` vs
`0.562522149582704` — **Δ 3.0 × 10⁻⁹**, the §5b ulp effect again.

**The baseline lands where the prior spikes put it**, which is the cross-check that makes the
delta trustworthy:

| source | peak on this shape | this measurement vs it |
| --- | ---: | ---: |
| `phaze-15sw` envelope maximum | 2.482 GiB | **+4.3%** |
| `phaze-3j67` per-process peak | 2.566 GiB | **+0.9%** |
| `phaze-rc1q` `dur_3600` baseline | 2.505 GiB | **+3.3%** |
| **this document, `dur_3600` baseline** | **2.588 GiB** | — |

**And here is why this matters for the molecule, stated as arithmetic and not as a
measurement.** `phaze-3j67` recommends `memory_request: 3Gi`; `phaze-rc1q`'s hybrid decode
costs **+1.079 GiB** unmitigated (**+0.677** after `malloc_trim`) and therefore breaches it at
3.584 GiB. On the same file and the same caps, this change takes the base term from 2.588 to
1.715 GiB. Adding `rc1q`'s measured regression to *this* base gives **2.794 GiB unmitigated /
2.392 GiB trimmed** — both under the 3Gi request rather than over it.

**That sum is not a measurement and must not be shipped as one.** The two changes have never
run in the same process, and the whole reason `rc1q` §6 exists is that a plausible arithmetic
argument about freed transients turned out to be wrong by 1.079 GiB. It is a strong reason to
run recommendation 2 — a *joint* re-measurement — before anyone re-sizes anything, and a
strong reason to land this change before `phaze-5lop` rather than after.


______________________________________________________________________

## 6. The arm64 / Python 3.13 question — answered

`Dockerfile.agent-arm64` is pinned to Python 3.13 for one reason, stated in its own comment:
the image builds essentia **from source against the `tensorflow` aarch64 wheel** (there is no
`essentia-tensorflow` aarch64 wheel), and **TensorFlow publishes no cp314 aarch64 wheel**.

Verified against PyPI on 2026-08-06:

| package | latest | cp314 **linux aarch64** wheel? |
| --- | --- | --- |
| `tensorflow` / `tensorflow-cpu` | 2.21.0 | **NO** — cp313 is the ceiling (`manylinux_2_27_aarch64`) |
| **`onnxruntime`** | 1.28.0 | **YES** — cp314 `manylinux_2_27_aarch64.manylinux_2_28_aarch64`, continuously since **1.25.1** |
| **`ai-edge-litert`** (TFLite runtime) | 2.1.6 | **YES** — cp314 `manylinux_2_27_aarch64` since 2.1.5 |
| `onnx` | 1.22.0 | YES — cp314 `manylinux_2_26_aarch64.manylinux_2_28_aarch64` |
| `essentia` (no TF) | 2.1b6.dev1438 | **NO** — cp314 ships macOS arm64 + manylinux **x86_64** only |
| `essentia-tensorflow` | 2.1b6.dev1438 | **NO** — same three wheels |

**So: yes, ONNX Runtime removes the constraint — and TFLite would too.** The chain is:

1. Inference leaves essentia → essentia no longer needs `--with-tensorflow`.
1. §2a's wscript gate then excludes the eleven `TensorflowPredict*` algorithms, **but keeps
   `TensorflowInputMusiCNN` / `TensorflowInputVGGish`**, which is everything phaze's front end
   uses.
1. A TF-free essentia source build has **no Python-version ceiling** — and it also sheds all
   four fixups the current Dockerfile carries solely to make libtensorflow link and run: the
   `setup_from_python.sh` dangling-symlink repoint + `libtensorflow_cc.so.2` pywrap remap
   (FIX #1), the `LIBRARY_PATH` / `LD_LIBRARY_PATH` wiring (FIX #2/#3), and the dual-OpenMP
   runtime conflict the file itself labels "the one OPEN production blocker", mitigated with
   `OMP_NUM_THREADS=1` (FIX #4).
1. `onnxruntime` then supplies inference from a **cp314 aarch64 wheel** — no source build.

**Honest caveat:** this does *not* produce a wheel-only arm64 image. MTG publishes no aarch64
essentia wheel in either flavour, so the image still builds essentia from source — just a
**much simpler** source build, and one that is not version-pinned. The 3.13 pin dies; the
source build does not.

This is a real maintenance win independent of memory, and it is the strongest argument in the
ONNX column. It is also **the only thing in this spike that batching cannot deliver** — the
batch lever leaves `Dockerfile.agent-arm64` exactly where it is.

______________________________________________________________________

## 7. Conversion risk and ongoing maintenance cost

Stated as the bead asks — not just the upside.

**Conversion itself was uneventful.** All 34 `.pb` → `.onnx` at opset 18, **34/34 OK, zero
failures, ~4 minutes total**, ~3.2 MB per musicnn head, 288.6 MB per vggish head, 18.1 MB for
effnet. `tf2onnx --graphdef --inputs model/Placeholder:0 --outputs model/Sigmoid:0` also
prunes the unused training-mode placeholders these graphs carry (`model/Placeholder_1`,
`model/Placeholder_2`, and effnet's `saver_filename`). Fidelity is §2c: 8.9 × 10⁻⁷.

The costs are structural, not technical:

1. **A conversion pipeline phaze owns forever.** Upstream ships one `.onnx` out of 290 model
   artifacts (§2d). Every model refresh means re-running `tf2onnx`, re-verifying fidelity, and
   publishing a second corpus. The conversion toolchain itself needs **TensorFlow** — which is
   exactly the dependency the exercise is trying to shed — so the build host keeps a TF pin
   (and therefore a ≤3.13 pin) even after the runtime image loses one.
1. **Double the model corpus.** 3.1 GB `.pb` + 3.1 GB `.onnx` (the conversion is roughly
   size-neutral), or a cutover that strands the `.pb` path. Worth noting while looking:
   `phaze-models` requests **2 Gi** on `local-path` and currently holds **3.1 GB** — the
   provisioner does not enforce the request, so this is latent rather than broken, but any
   ONNX plan that doubles the corpus should fix the request first.
1. **~40 lines of framing glue phaze becomes responsible for.** Today `patchSize`,
   `patchHopSize`, `lastPatchMode`, the `squeeze`, and effnet's `lastBatchMode="same"` are
   upstream's problem and move with the wheel. After the swap they are transcribed constants
   in phaze, and an upstream change to any of them becomes a silent numeric drift rather than
   a version bump. §2b's table is exactly the surface that has to be kept in sync.
1. **`discogs-effnet-bs64-1` stays awkward.** Its fixed batch-64 Placeholder plus the
   zero-pad-and-truncate semantics are the one genuinely non-obvious piece of the glue, and
   `phaze-rc1q` §3d already flagged it as the one real behavioural divergence in the model set.
1. **CPU cost.** §3c: ≈25% more core-seconds for the same predictions — and §5a: **+99% wall
   clock** end to end — on a node `phaze-3j67` measured CPU-bound, not memory-bound.
1. **Upstream drift risk in the other direction.** If #1488 lands with proper composites,
   phaze's hand-rolled glue becomes the thing standing between it and a supported path.

Against that, the batching lever costs: **one keyword argument in `_get_classifier`**, no new
artifacts, no new dependency, no new toolchain, and a proven 1.19 × 10⁻⁷ output delta.

______________________________________________________________________

## 8. The options, priced

### 8a. Stay on TensorFlow, at the shipped `batchSize=64` — **reject**

It is leaving **34% of the whole-pipeline peak** on the floor for a one-keyword change whose
accuracy cost is 3.0 × 10⁻⁹ on one derived float and zero everywhere else. Given
`phaze-rc1q`'s hybrid breaches `phaze-3j67`'s 3Gi request by +1.079 GiB, declining a
**0.873 GiB** recovery that costs +0.36% wall clock is not defensible.

### 8b. Migrate the runtime to ONNX Runtime — **reject as a memory play; keep as the arm64 play**

It is reachable (§2), exact (8.9 × 10⁻⁷), and in the **isolated** sweep at batch 16 + arena
off it is the lowest peak measured (1.133 GiB). Two things kill it as a memory play:

1. **0.31 GiB of that 1.055 GiB win over the shipped baseline is the runtime; the other
   0.75 GiB is the batching lever, which TensorFlow gives away for free.**
1. **In the real pipeline the incremental 0.31 GiB is not merely small — it is negative.**
   §5a measures ORT at **1.800 GiB against TensorFlow-at-batch-32's 1.501 GiB**, with **+99%
   wall clock**. The isolated sweep was not a lie, it was a different shape: it never held
   `coarse_cap` buffers concurrently.

Paying a permanent conversion pipeline, a doubled corpus, 40 lines of transcribed framing
constants and 25%+ more CPU to end up **higher** on the metric being optimised is not a
defensible trade.

The arm64 argument (§6) is genuinely strong and is **not** a memory argument. It should be
decided on its own merits, in its own bead, against the alternative of leaving
`Dockerfile.agent-arm64` on 3.13 until TensorFlow ships cp314 aarch64.

### 8c. Quantization — **reject**

fp16 buys 5% of peak for a 10⁻² accuracy cost and negative speed. int8 flips a label. §4.

### 8d. TFLite — **not measured; deprioritised on the same reasoning**

`ai-edge-litert` ships cp314 aarch64 wheels (§6), so it clears the arm64 bar too. It was not
measured because it shares ONNX's entire cost structure — a conversion pipeline phaze owns, a
second corpus, the same 40 lines of glue — while being a *narrower* ecosystem than ONNX for
these graph shapes, and because §3a had already shown the memory question is answered without
leaving TensorFlow. If the arm64 question is ever taken up on its own, TFLite belongs in that
comparison, not in this one.

### 8e. **Adopt: set `batchSize` explicitly on the shipped TensorFlow path** — **recommended**

One keyword argument in `_get_classifier`, `batchSize=32` (or 16 — they are within 1.8% of
each other and both sit on the flat part of the curve), leaving `discogs-effnet-bs64-1` at 64
because its graph requires it.

______________________________________________________________________

## 9. Recommendations

1. **Set `batchSize=32` in `_get_classifier` for the musicnn and vggish families.** Isolated
   34-model sweep **2.188 → 1.417 GiB (−35.2%, +1.1% wall)**; end to end through the real
   `analyze_file` **2.210 → 1.501 GiB (−32.1%, +0.1% wall)** on the 10-minute file and
   **2.588 → 1.715 GiB (−33.7%, +0.36% wall)** on the 60-minute file at saturated caps — the
   shape `phaze-15sw` sized against. Keep `discogs-effnet-bs64-1` at
   64 — its Placeholder is fixed. Pin the choice with a test that asserts the parameter is
   passed, and a comment recording that batch 32/16/8 are flat and batch 1 costs 56% wall
   clock. **Write the equivalence test against the categorical fields plus a tolerance, not
   against a sha256** — §5b: `danceability` moves by 2.5 × 10⁻⁹, so this is *not* the
   byte-identical bar `phaze-15sw` and `phaze-rc1q` met, and pretending otherwise will produce
   a test that fails for the right reason and gets deleted for the wrong one.
1. **Land (1) before `phaze-5lop`, then re-price `phaze-3j67`'s `memory_request: 3Gi` /
   `memory_limit: 4Gi` from a JOINT measurement.** The two changes point opposite ways and are
   comparable in magnitude on the same file and caps: `rc1q` costs **+1.079 GiB** unmitigated
   (+0.677 after `malloc_trim`), this recovers **0.873 GiB** (§5c). The arithmetic says the
   pair fits under 3Gi where `rc1q` alone does not — **but §5c is explicit that the sum is not
   a measurement**, and `rc1q` §6 is the standing proof that this exact kind of arithmetic can
   be wrong by a gigabyte. **Do not re-open the sizing on either spike's numbers alone.**
1. **Do not convert to ONNX for memory.** File the arm64/Python-3.14 unpinning as its own
   bead with §6 as its evidence, scoped as "move inference out of essentia so the arm64 image
   can drop `--with-tensorflow`", and let it be decided on maintenance grounds. It carries the
   full §7 cost list.
1. **Do not pursue quantization.** Record §4 so it is not re-proposed: the peak is activations
   and arena, not weights.
1. **If (3) is ever taken up, spike `discogs-effnet-bsdynamic-1` first.** A dynamic-batch genre
   model would let the batch lever apply to all 34 and would remove the one awkward piece of
   glue — but it is a different model file and needs its own accuracy budget against
   `bs64-1`.
1. **Watch [MTG/essentia#1488](https://github.com/MTG/essentia/pull/1488) rather than
   forking it.** If it merges *and* grows `OnnxPredict*` composites *and* MTG publishes an
   `essentia-onnx` wheel, the entire §7 cost list collapses and (3) becomes near-free. None of
   those three has happened.

______________________________________________________________________

## 10. What this measurement does and does not support

**Supports:**

- The runtime seam exists and is numerically exact (§2c) — 0/34 patch-count mismatches, 0/34
  argmax disagreements, 8.9 × 10⁻⁷ aggregated.
- `batchSize` is the dominant controllable term in the peak — isolated *and* end to end at
  saturated caps (§3a, §5c) — the knee is between 64 and 32, and the model activations are
  output-invariant to one float32 ulp (§3b).
- The baseline reproduces `phaze-15sw` / `phaze-3j67` / `phaze-rc1q` on the same shape to
  within +0.9% … +4.3% (§5c), so the deltas are measured against a corroborated base.
- ORT's default CPU arena inverts the comparison; measured with it off, ORT is 20% below
  TensorFlow at the same batch in the isolated sweep, at 25% more CPU (§3c) — and **above**
  it end to end, at +99% wall clock (§5a).
- Quantization does not attack this peak (§4).
- `onnxruntime` and `ai-edge-litert` both publish cp314 linux-aarch64 wheels; TensorFlow does
  not (§6).

**Does not support:**

- Any claim about **GPU** or non-CPU execution providers. Every arm is CPU-only.
- Any claim about **arm64 performance or memory**. §6 is a wheel-availability and build-shape
  argument only; nothing here was measured on aarch64.
- Any claim that ORT is faster. At the thread settings measured it is ~2.2× slower in wall
  clock and ~25% more in core-seconds, and the TF arm's implicit 8-thread use makes the
  wall-clock half of that comparison soft.
- Any **static/QDQ** int8 result. Only *dynamic* quantization was measured; a calibrated static
  pass was not attempted, because §4's memory result makes it uneconomic regardless.
- **Byte-identical output.** §5b: `danceability` moves by 2.5 × 10⁻⁹ under the batch change.
  Every categorical field is identical; the serialized `sha256` is not.
- **A diagnosis of why ORT inverts between §3 and §5a.** The measurement is solid on both
  sides and the shapes differ in known ways (one buffer vs `coarse_cap` buffers, no fine tier
  vs a fine tier), but no discriminating probe was run, so the *mechanism* is stated as a
  hypothesis, not a finding. It does not change the recommendation, which follows from the
  end-to-end number regardless of cause.
- Any claim about how (1) composes with `phaze-rc1q`'s hybrid decode. They were measured
  separately, on the same host and shape, but never in the same process. Recommendation 2 is
  deliberately a *joint* re-measurement, not an arithmetic sum.
- Any statement about a real music corpus. Every input is synthetic; absolute prediction values
  are meaningless, only the **deltas between arms on identical input** are claimed.

______________________________________________________________________

## Appendix — reproducing this

Three scripts, in the shape `phaze-esut` / `phaze-7i0k` / `phaze-3j67` / `phaze-rc1q`
established — drive the **real** code, never reimplement the pipeline:

- **`runtime_bench.py`** — the 34-model sweep, `--arm tf` (shipped `es.TensorflowPredict*`)
  vs `--arm ort` (essentia's inner network + onnxruntime). `--accuracy` runs **both** arms on
  the same buffer and reports per-patch and aggregated deltas plus argmax agreement.
  `--batch-size`, `--intra`, `--no-arena`, `--onnx <dir>` select the arms in §3/§4.
- **`e2e_analyze.py`** — the end-to-end A/B. Monkeypatches exactly `_get_classifier` in the
  **real** `phaze.services.analysis` (`--arm ort`, or `--arm tf-batch` to add `batchSize` to
  the shipped construction) and then calls the **real** `analyze_file`, so §5's numbers isolate
  the substitution.
- **`sampler.py`** — runs on the **host**, outside the container, at 0.4 s: `VmRSS`/`VmHWM`
  for every `/proc/<pid>` whose cmdline is anchored on the container interpreter, plus
  `/proc/stat` node-CPU deltas and `MemAvailable`. **Anchor the match** (`^/app/\.venv/bin/python `)
  — an unanchored pattern also matches the `kubectl exec` wrapper on the host and the sampler
  then never sees the process exit.

The pod is a bare `sleep infinity` pod on the burst node using the deployed job image, the
`phaze-models` PVC mounted **read-only** at `/models`, a host scratch dir, and **no Kueue queue
label** so it consumes no quota. `main`'s `analysis.py` (sha256 `45a84a70…`) is overlaid on a
copy of `/app/src` in scratch and put first on `PYTHONPATH`; the image itself is never modified.

Conversion runs in a **separate** `python:3.13-slim` pod (TensorFlow has no cp314 wheel, so it
cannot share the analysis pod's interpreter):

```sh
# musicnn + vggish heads
python -m tf2onnx.convert --graphdef <model>.pb \
       --inputs model/Placeholder:0 --outputs model/Sigmoid:0 \
       --opset 18 --output <model>.onnx

# discogs-effnet-bs64-1
python -m tf2onnx.convert --graphdef discogs-effnet-bs64-1.pb \
       --inputs serving_default_melspectrogram:0 --outputs PartitionedCall:0 \
       --opset 18 --output discogs-effnet-bs64-1.onnx
```

Naming the inputs and outputs explicitly is load-bearing: it prunes the training-mode
placeholders (`model/Placeholder_1`, `model/Placeholder_2`) and effnet's `saver_filename`
string input, which `tf2onnx` would otherwise carry into the ONNX graph as required feeds.

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

**Four things to get right when re-running.** Read `VmHWM`, not a sampled `VmRSS` — the 0.4 s
sampler under-reads by up to 23% (§1). Anchor the sampler's process match. Turn ORT's CPU arena
**off** before comparing runtimes, or you are measuring the arena (§3c). And keep
`discogs-effnet-bs64-1` at `batchSize=64` in every arm — its Placeholder is fixed and any other
value is a configuration error, not a data point.

**Teardown.** Both pods deleted, the host scratch dir removed, and the `python:3.13-slim`
image this spike pulled removed from containerd. The `phaze-models` PVC is `Bound` and its 68
files are byte-unchanged (it was mounted read-only throughout); the 18 phaze images and all
k0s / JuiceFS / gateway configuration are untouched, and **vox remains out of the phaze backend
registry** — nothing here re-enabled it.
