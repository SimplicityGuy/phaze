# phaze-rc1q — essentia streaming mode vs standard mode for the analyze pipeline

- **Bead:** `phaze-rc1q` (spike — "is it worth exploring both essentia modes?")
- **Date:** 2026-08-05
- **Tree:** branch `wt/bead/issue/phaze-rc1q`, forked off `main` at `75020e8`
- **Code under test:** the deployed analyze image `ghcr.io/simplicityguy/phaze/job:2026.8.0` with
  `main`'s post-`phaze-15sw` `services/analysis.py` overlaid (sha256 `45a84a70…`), against the
  deployed `phaze-models` PVC (34 graphs)
- **Upstream under test:** `essentia-tensorflow` 2.1-beta6-dev (the wheel `pyproject.toml` pins),
  read against `MTG/essentia@master` source for the algorithms it composes
- **Status:** spike. Measurement and verdict only. **No product code changed.**

______________________________________________________________________

## Verdict in one paragraph

**The question dissolves on inspection, and the answer the measurements give is a large win that
has nothing to do with picking a mode.** phaze's decode is *already* streaming mode: standard
`EasyLoader` is a wrapper that runs the streaming `EasyLoader` composite inside a private
`scheduler::Network` and catches its output in a `VectorOutput`. So are all three model families —
each standard `TensorflowPredict*::compute()` is `VectorInput >> «the streaming algorithm» >> Pool`.
There is no separate standard implementation to be faster or slower; there is one implementation
plus an adapter. What phaze actually pays for is **arity**: it stands up **80–90 separate one-sink
networks per file**, and because `EasyLoader` does not seek, each decodes and resamples from byte 0
(`phaze-esut` §8). Rebuilding that as **one network per tier with N `Trimmer` sinks** — same
algorithms, same parameters, one `essentia.run()` — collapses 90 passes into 2 and is **3.5× /
10.9× / ≈15× / ≈18× faster on decode** for 10 / 60 / 180 / 720-minute files. **End to end at
saturated caps that is −41.7% wall clock — 3 044.9 s → 1 775.9 s — for byte-identical output**
(sha256 + `cmp` on the whole `analyze_file` result, on two files, in three hybrid runs, plus **136
individually hashed buffer comparisons** across five decode runs, including the last window of a
12-hour file). It costs **+1.079 GiB of
peak**, of which **0.403 GiB is glibc retention that one `malloc_trim(0)` removes for +0.13% wall
(measured)** and 0.677 GiB is an essentia-`Pool` double-hold the implementation can avoid outright —
so the design does not double-hold, this prototype's `Pool` usage does. The bead's two warnings both
survive contact but land differently than expected: the *performance* trade is real and is the
**per-branch fan-out copy**, which is why the win is a large constant factor rather than an
asymptotic one; the *`TensorflowPredict` normalization* hazard is real upstream but belongs to
**`TensorflowPredictFSDSINet` alone** — phaze runs none of it, and the three families phaze does run
normalize in neither mode. (phaze *does* have one mode-dependent divergence, and it is **one** graph
and batch padding, not 34 and normalization — §3d.) **Recommendation: adopt the hybrid — a streaming
decode network per tier, model sweep unchanged in standard mode.** Do not go wholesale: a single-pass
fan-out to all 34 models would require all 34 graphs co-resident, which is precisely the 4 GiB
configuration `phaze-15sw` removed.

______________________________________________________________________

## 1. Method

| | |
| --- | --- |
| **Host** | `vox` — Debian 13 (trixie), kernel 6.12.100, glibc 2.41, Xeon E3-1271 v3, **4 physical cores / 8 logical (SMT)**, 31.3 GiB, k0s burst node, **out of the phaze backend registry and left that way**, otherwise idle (0 pods in `phaze`, ~29 GiB available; verified before the run and enforced by an idle gate between every measurement) |
| **Runtime** | deployed job image `job:2026.8.0` in a bare `sleep infinity` pod, **no Kueue queue label** (consumes no quota), `phaze-models` PVC mounted **read-only**, a host scratch dir for synthetic audio |
| **Window geometry** | driven by the **real** `phaze.services.analysis` internals — `_probe_duration_sec`, `_iter_windows`, `_stride_to_cap` — so both arms analyze exactly the windows production would |
| **Audio** | **synthesized with ffmpeg** — the `phaze-esut` appendix generator, stereo 44.1 kHz sine pairs, libmp3lame 192 kbps, at 600 / 3600 / 10800 / 43200 s |
| **Per-process peak** | `/proc/self/status:VmHWM`, read **once at exit** — a kernel high-water mark, not a sampled curve, so it is immune to the `phaze-7i0k` §9 GIL trap |
| **RSS/CPU curve + contention guard** | a **host-side** sampler outside the container at 0.5–1 s cadence (`phaze-7i0k` §9: an in-process sampler is GIL-starved and reports a falsely clean curve). Node CPU is recorded per run and used as an admissibility gate — see §1b |
| **Identity metric** | sha256 over each decoded buffer's raw float32 bytes, plus sha256 + `cmp` on the whole `analyze_file` result — the bar `phaze-15sw` met |

**No operator media was read, copied, or referenced.** Every input is a synthesized sine pair. No
filename, path, or per-file metadata value from the library appears in this document. vox was **not**
re-enabled in the phaze backend registry; k0s, JuiceFS and the gateway config were not touched; the
models PVC was mounted read-only and is intact. The only artefacts created are a scratch directory
and one bench pod, both removed (appendix).

### 1a. What the two arms are

- **ARM STANDARD** — exactly what `services/analysis.py` does today: one
  `es.EasyLoader(filename, sampleRate, startTime, endTime)()` call per window, `fine_cap=60`
  windows at 44.1 kHz plus `coarse_cap=30` at 16 kHz.
- **ARM STREAMING** — one `essentia.streaming` network per tier: `AudioLoader → MonoMixer →
  Resample(native→tier_rate)`, fanned out to one `Scale → Trimmer → Pool` branch per window, run
  once with `essentia.run(loader)`.

§3 explains why the streaming arm branches at `MonoMixer` rather than `MonoLoader`, and why the
per-branch `Scale` is load-bearing rather than decorative. Both are correctness requirements found
in upstream source, not style choices.

### 1b. A contention gate, and what it caught

Decode is single-threaded, so a clean run on this node sits at **12.5–13.6% node CPU** (one of eight
logical cores). Every run's host-side CPU mean was checked against that band afterwards, and **eight
runs were discarded and re-measured** because they read 26–89%: background `ffmpeg` still generating
the long test files, and — twice — a duplicate copy of the harness the driver had launched
concurrently. On a node `phaze-3j67` measured saturating at W=2, a second process does not perturb a
wall-clock measurement, it invalidates it. Every number in this document comes from a run inside the
band (or, for the five `analyze_file` runs, at the **53–81%** that a legitimately multi-threaded TF
inference produces — `phaze-3j67` §2 measured one extractor consuming ~6.2 of 8 logical cores).

The harness is independently validated against a published figure: ARM BASELINE's `analyze_file` on
`dur_600` measures **344.93 s** here against `phaze-15sw`'s **345.2 s** — **0.08%**.

______________________________________________________________________

## 2. The framing has to change first: there is no "standard implementation"

The bead asks whether it is "worth exploring both modes". Upstream's own source answers that at the
level phaze uses them, the two are not alternatives.

**`EasyLoader`, the algorithm phaze's decode is built on** (`src/algorithms/io/easyloader.h`):

```cpp
namespace essentia { namespace streaming {
class EasyLoader : public AlgorithmComposite {
  Algorithm* _monoLoader;  Algorithm* _trimmer;  Algorithm* _scale;
};
}}
namespace essentia { namespace standard {
// Standard non-streaming algorithm comes after the streaming one as it depends on it
class EasyLoader : public Algorithm {
  streaming::Algorithm* _loader;
  streaming::VectorOutput<AudioSample>* _audioStorage;
  scheduler::Network* _network;
};
}}
```

The comment is upstream's. Standard `EasyLoader::compute()` points the `VectorOutput` at a caller
vector and calls `_network->run()`. **phaze is already running a streaming network 80–90 times per
file**, paying a full `scheduler::Network` construction, connection and teardown for each.

**The same holds for the 34 model graphs.** Standard `TensorflowPredictMusiCNN::compute()` and
`TensorflowPredictVGGish::compute()` are, in full: reject an empty signal,
`_vectorInput->setVector(&signal)`, `_network->run()`, read `predictions` out of a `Pool`, `reset()`.
`TensorflowPredictEffnetDiscogs::compute()` adds one conditional (§3c) and is otherwise the same.
The wrapped object is the streaming algorithm of the same name.

So the documentation's "slight performance cost" is the cost of *the adapter* — a `VectorInput` push
and a `Pool` read — not of a separate, slower engine. §7 prices it and it is not where phaze's time
goes.

**What phaze pays for is arity, not mode.** One network per window, 90 windows, and a loader that
does not seek.

______________________________________________________________________

## 3. Four mode-related traps, all found in upstream source

A streaming fan-out is not a mechanical transformation. Four specific things will silently corrupt
it or its output, and all four were found by reading `MTG/essentia@master` rather than by
measurement. The first three bind on the recommended change; the last two are the bead's Finding 2
re-scoped, and bind only on a wholesale port.

### 3a. `MonoLoader` always inserts a `Resample`, even at ratio 1.0

`src/algorithms/io/monoloader.cpp` builds `AudioLoader → MonoMixer → Resample` unconditionally and
configures the resampler `inputSampleRate = native`, `outputSampleRate = parameter("sampleRate")`.
There is no ratio-1.0 short circuit.

The obvious fan-out — take `MonoLoader(sampleRate=44100)`, use it for the fine tier, hang a
`Resample(44100→16000)` off it for the coarse tier — therefore runs the coarse tier through **two**
libsamplerate passes (44100→44100→16000) where `EasyLoader(sampleRate=16000)` runs one. That is not
the same signal. **The fan-out must branch at `MonoMixer`**, giving each tier its own single
`Resample(native→tier_rate)` — bit-for-bit each `MonoLoader`'s internal chain. That is the shape
upstream itself uses in `MusicExtractor` (`loader → demuxer → resampleL/R → muxer → trimmer → …`)
and in `streaming_extractor_la-cupula.cpp`
(`audioStereo → monoMixer → {frameCutter, realAccumulator, humDetector}`).

### 3b. A streaming `Trimmer` shuts down its parent when it reaches `endTime`

`src/algorithms/standard/trimmer.cpp`, streaming `process()`:

```cpp
  // optimization: we should also tell the parent (most of the time an
  // audio loader) to also stop, to avoid decoding an entire mp3 when only
  // 10 seconds are needed
  if (_consumed >= _endIndex) {
    // FIXME: does still still work with the new composites?
    shouldStop(true);
    const_cast<SourceBase*>(_input.source())->parent()->shouldStop(true);
  }
```

Hung directly off a **shared** `Resample`, the earliest-ending window would tell the shared decode
branch to stop, truncating every other consumer. Interposing a **per-branch algorithm** between the
shared source and each `Trimmer` gives each `Trimmer` a private parent to shut down, and the shared
branch keeps running. `EasyLoader` already contains exactly such an algorithm — `Scale` — so the fix
costs nothing new: `Resample → Scale(factor=1.0) → Trimmer → Pool` per window.

Both variants were measured (`--variant scale` with the interposer, `--variant bare` without) and
both produced identical, untruncated buffers on this wheel — upstream's own `FIXME` doubt is
warranted and the propagation does not currently bite. **The interposer is kept anyway**: it is
upstream's own composition, it is cheap relative to the win, and building a production decode on a
code path upstream marks `FIXME` as possibly-not-working is not a foundation. §7c prices it at 13%
of the fine tier so the choice can be revisited with a number.

That same `FIXME` is the mechanism behind `phaze-esut` §8. Standard `EasyLoader` wraps `MonoLoader` —
a composite — so `parent()->shouldStop(true)` never reaches the inner `AudioLoader`, and
`EasyLoader(startTime=t, endTime=t+180)` decodes the whole file regardless of `t`. §8 measured the
symptom; this is the line that causes it.

### 3c. The `TensorflowPredict` normalization hazard is real, and it is not phaze's

The bead's Finding 2 quotes upstream accurately, but the sentence belongs to **one** algorithm.
`src/algorithms/machinelearning/tensorflowpredictfsdsinet.cpp`:

```cpp
  "Note: The FSD-SINet models were trained on normalized audio clips. "
  "Clip-level normalization is only implemented in standard mode since in streaming there is no "
  "access to the entire audio clip. In the streaming case, the user is responsible for controlling "
  "the dynamic range of the input signal. Ideally, the signal should be zero-mean (no DC) and "
  "normalized to the full dynamic range (-1, 1).\n\n"
```

and it is backed by real standard-mode-only code — `normalizeFSDSINet()` subtracts the mean and calls
`normalizeAbs(x, 0.005)` before the network runs, guarded by a `normalize` parameter that defaults
true. That is the **only** `TensorflowPredict*` algorithm in essentia with a mode-dependent input
transform. Sweeping every `src/algorithms/machinelearning/tensorflowpredict*.cpp` for normalization
turns up nothing else except `TensorflowPredictTempoCNN`'s `TensorNormalize` node, which lives
*inside* its streaming network and therefore applies in both modes.

**phaze constructs `TensorflowPredictMusiCNN`, `TensorflowPredictVGGish` and
`TensorflowPredictEffnetDiscogs` and nothing else** (`_get_classifier`, `services/analysis.py:159`).
None of the three normalizes in either mode — their standard `compute()` bodies are described in §2
and contain no transform. **So "a naive port silently changes every one of 34 predictions" is not
true of phaze's model set.** The hazard that does exist is a standing reason never to adopt
`TensorflowPredictFSDSINet` in streaming mode, which phaze has no plan to do.

### 3d. The divergence phaze *does* have is `EffnetDiscogs` batch padding, not normalization

Sweeping the same ground for mode-dependent behaviour turns up one that lands squarely on phaze's
model set — a different one from the bead's.

`TensorflowPredictEffnetDiscogs::compute()` (standard mode only) zero-pads the signal to fill the
final batch and then erases the padded predictions:

```cpp
  if (_batchSize > 0) {
    if (_lastBatchMode == "zeros" || _lastBatchMode == "same") {
      paddingPatches = padSignal(*signal, paddedSignal);
      if (paddingPatches) signal = &paddedSignal;
    }
  }
  ...
    if (_lastBatchMode == "same") {
      predictions.erase(predictions.end() - paddingPatches, predictions.end());
    }
```

On the pinned wheel the defaults are `batchSize = 64` and **`lastBatchMode = "same"`**, and phaze
passes neither, so **the padding branch is taken on every window of `discogs-effnet-bs64-1`**. The
**streaming** `TensorflowPredictEffnetDiscogs` has no `lastBatchMode` parameter at all — verified on
the installed wheel — so it cannot reproduce this. A streaming port of the genre model would either
drop the trailing partial batch or return predictions computed over zero padding. **Either changes
the output.**

**The §11 recommendation keeps every model in standard mode**, so neither §3c nor §3d binds on it.
Both are recorded because they are what a *wholesale* port would have had to handle, and because the
bead's framing — 34 graphs at risk from normalization — should not be carried into `phaze-i93a`
uncorrected. The accurate statement is: **1 graph is at risk, from batch padding, not 34 from
normalization.**

______________________________________________________________________

## 4. The decode measurement — the number `phaze-esut` §8 asked for

`fine_cap=60`, `coarse_cap=30`, production window sizes (30 s @ 44.1 kHz, 180 s @ 16 kHz). Both arms
produce the same window set on the same file. "ARM STREAMING" is the **one-network-per-tier** shape
(two `essentia.run()` calls); §8 prices the single-shared-pass alternative.

| file | duration | windows (fine + coarse) | **ARM STANDARD** | **ARM STREAMING** | **speedup** |
| --- | ---: | ---: | ---: | ---: | ---: |
| `dur_600` | 10 min | 20 + 4 | **53.4 s** | **15.1 s** | **3.5×** |
| `dur_3600` | 60 min | 60 + 20 | **1 376.7 s** | **126.6 s** | **10.9×** |
| `dur_10800` | 180 min | 60 + 30 | **5 510.6 s** † | **350.0 s** ‡ | **15.7×** |
| `dur_43200` | 720 min | 60 + 30 | **22 141.6 s** † | **1 153.7 s** ‡ | **19.2×** |

† **Extrapolated, and the extrapolation is calibrated.** ARM STANDARD on the two long files is
measured per window at `--fine-cap 2 --coarse-cap 2` (which keeps the first *and last* window of
each tier, so the strided endpoints are exercised) and multiplied by the production window count —
running it out would take 92 minutes for the 180-minute file and **over six hours** for the
720-minute one. The method was calibrated against ground truth on `dur_3600`, where both are
available: the cap-2 average predicts the measured 20-window coarse average to **+1.0%**
(52.63 vs 52.12 s) and *under*-predicts the 60-window fine average by **21%** (4.40 vs 5.57 s).
Weighted by each tier's share of the standard total (coarse 76%, fine 24%) the method
**under**-states ARM STANDARD by **≈4.3%**, so these two figures are conservative and the speedups
are if anything understated.

‡ These two rows are the single-shared-pass shape rather than two per-tier passes (the long files
were run before §8 settled the choice). Per §8 that shape is 5% *faster*, so these speedups are also
marginally optimistic on the streaming side by about that much; 15.7× and 19.2× read as ≈15× and
≈18× for the recommended two-pass shape.

### 4a. `phaze-esut` §8 reproduces on this node, to within 1%

Wall time for one 180-second coarse window at 16 kHz — §8's exact quantity, remeasured here:

| total file duration | §8 (macOS) | **vox (measured)** | s per minute of total file | linear prediction from the 60-min point |
| ---: | ---: | ---: | ---: | ---: |
| 10 min | 6.6 s | **7.91 s** | 0.791 | — |
| 60 min | 45.9 s | **52.12 s** | 0.869 | — |
| 180 min | 143.6 s | **157.48 s** | 0.875 | 156.35 s (**+0.7%**) |
| **720 min** | **600.2 s** | **632.36 s** | 0.878 | 625.40 s (**+1.1%**) |

§8's central claim — per-window decode cost is linear in **total** file duration and independent of
window length — is confirmed on this node and this image across a 72× duration span, to within
1.1% of a straight line. The fine tier behaves the same way at 44.1 kHz but **7–12× cheaper per
window** (1.09 / 5.57 / 13.10 / 52.85 s at 10 / 60 / 180 / 720 min), because `Resample(44100→44100)`
is near-free while `Resample(44100→16000)` is real libsamplerate work. **That asymmetry is why the
coarse tier is 76% of ARM STANDARD's wall time on the 60-minute file** despite having a third as
many windows — and, per §7, why the streaming rewrite pays so much more there.

§8's extrapolated "≈5.7 hours of mostly single-threaded resampling for a 724-minute file" is
confirmed and slightly exceeded: **22 142 s = 6.15 hours** of pure decode at production caps.

______________________________________________________________________

## 5. Output identity — proven, not assumed

Every decoded buffer was hashed as raw float32 bytes and compared window by window between arms.

| file | windows compared | **mismatches** |
| --- | ---: | ---: |
| `dur_600`, fan-out `--variant scale` | 24 | **0** |
| `dur_600`, fan-out `--variant bare` | 24 | **0** |
| `dur_3600` | 80 | **0** |
| `dur_10800`, strided endpoints (first + last window of each tier) | 4 | **0** |
| `dur_43200`, strided endpoints | 4 | **0** |
| **end-to-end `analyze_file` result, `dur_600`** | sha256 `eb18b125…` both arms, `cmp` identical | **0** |
| **end-to-end `analyze_file` result, `dur_3600`, caps saturated** | sha256 `d7fde10d…` both arms, `cmp` identical | **0** |
| **end-to-end, `dur_3600`, hybrid + `malloc_trim` variant** | sha256 `d7fde10d…`, `cmp` identical | **0** |

Buffer identity is the stronger claim and the one that matters: the 34 `TensorflowPredict*` graphs
and the two fine-tier extractors are pure functions of the buffer, so identical PCM implies identical
predictions by construction. The end-to-end runs (§6) confirm it anyway at the `analyze_file`
return-value level, which is the bar `phaze-15sw` met.

The identity is not an artefact of short files. `dur_10800` and `dur_43200` compare the **last**
window of each tier — fine window 359 / 1439 and coarse window 59 / 239 — which is the case a
truncating fan-out (§3b) would fail first and loudest. And no window came back `MISSING`: all 60
fine and 30 coarse sinks of the 12-hour file produced full-length buffers from a single pass.

______________________________________________________________________

## 6. End to end — the same output, 42% faster, 43% more peak

`analyze_file` from `main`, unmodified, against the deployed models. The HYBRID arm monkeypatches
exactly the two functions that own decode (`_analyze_fine_windows`, `_analyze_coarse_windows`) with
copies whose only difference is the decode loop, then calls the **real** `analyze_file` — so
assembly, aggregation, failure isolation and `phaze-15sw`'s model-major sweep are the shipped code
and an output difference could only come from the decode.

| `dur_600` — 10 min, 20 fine + 4 coarse (caps not saturated) | wall | peak RSS | result sha256 |
| --- | ---: | ---: | --- |
| **BASELINE** (`main`) | **344.93 s** | **2.218 GiB** | `eb18b125…` |
| **HYBRID** | **304.75 s** | **2.561 GiB** | `eb18b125…` |
| **Δ** | **−11.7%** | **+15.5%** | **identical** (`cmp`) |

| `dur_3600` — 60 min, **60 fine + 20 coarse (fine cap saturated)** | wall | peak RSS | result sha256 |
| --- | ---: | ---: | --- |
| **BASELINE** (`main`) | **3 044.87 s** | **2.505 GiB** | `d7fde10d…` |
| **HYBRID** | **1 775.89 s** | **3.584 GiB** | `d7fde10d…` |
| **Δ** | **−41.7%** | **+43.1%** | **identical** (`cmp`) |

**Output identity holds at the `analyze_file` return value, on both files, by sha256 and by `cmp`.**
That is the bar `phaze-15sw` met, met again here.

HYBRID's internal breakdown on `dur_3600`: fine decode **83.85 s** (ARM STANDARD: 334.35 s, −74.9%),
fine extract 31.24 s, coarse decode **43.00 s** (ARM STANDARD: 1 042.34 s, **−95.9%**), coarse
inference 1 617.80 s. Total decode **126.85 s against 1 376.70 s — 10.9×**, reproducing §4's
decode-only figure (126.60 s) to 0.2%. The wall-clock saving of 1 268.98 s against a predicted decode
saving of 1 249.85 s closes the accounting to **1.5%**: the speedup is the decode and nothing else.
The baseline's 2.505 GiB peak also independently reproduces `phaze-15sw`'s 2.482 GiB envelope
maximum (+0.9%) and `phaze-3j67`'s 2.566 GiB (−2.4%).

### 6a. The peak regression is real, and it is not the PCM

**+1.079 GiB at saturated caps.** It is not the buffers: `dur_3600` holds 317 MB of fine and 230 MB
of coarse PCM, and — decisively — the fine tier's PCM is released *before* the coarse decode, which
is released *before* the model sweep, so on a naive reading the fan-out transients should sit
entirely under the 2.5 GiB the sweep needs anyway.

They do not, and §8's per-tier deltas say why. The fan-out's transient is ~2× its PCM
(0.630 GiB held for 317 MB; 0.301 GiB for 230 MB) because an essentia `Pool` grows its
`vector<Real>` by doubling and the Python-side extraction copies out of it. Those bytes are *freed*
before the sweep — but glibc does not trim by default, and TF's arena allocations do not reuse the
retained pages, so the sweep stacks on top of them. **2.505 + 0.630 + 0.301 = 3.436 GiB against a
measured 3.584** — the two-transient model accounts for 86% of the regression.

### 6b. The discriminating probe — and the regression is two thirds engineerable today

One `malloc_trim(0)` after each tier's buffers are dropped, same file, same caps, nothing else
changed:

| `dur_3600`, saturated caps | wall | **peak RSS** | result sha256 |
| --- | ---: | ---: | --- |
| BASELINE | 3 044.87 s | **2.505 GiB** | `d7fde10d…` |
| HYBRID | 1 775.89 s | **3.584 GiB** (+1.079) | `d7fde10d…` |
| **HYBRID + `malloc_trim(0)` per tier** | **1 778.24 s** (+0.13%) | **3.181 GiB** (+0.677) | `d7fde10d…` |

**`malloc_trim` recovers 0.403 GiB — 37% of the regression — for 2.4 seconds of a 30-minute run**,
and output stays byte-identical. That is a clean confirmation: a third of the regression is glibc
declining to return pages the fan-out freed, exactly the retention `phaze-7i0k` §4 characterised
(where it was worth 0.0% against the *old* workload, because that workload had no large freed
transient to return).

The residual **+0.677 GiB survives a trim, so it is memory genuinely still live during the model
sweep.** The most likely holder is the essentia `Pool` itself: `pool[key]` hands Python an array
backed by — or copied from — the `Pool`'s storage, and the extracted buffers keep the `Pool` alive
through `_run_model_sets_over_windows`. That would mean the coarse tier's 230 MB of PCM is resident
**twice**, once in the `Pool` and once in the list handed to the sweep, plus the `Pool`'s doubling
slack. 230 MB × 2 + slack lands in the right place.

**That is a literal double-hold — the one the bead asked about — and it is in the harness's `Pool`
usage, not in the design.** It is what recommendation 3 removes: build the sink as a pre-sized
`numpy` array per window (every length is known before the network is constructed) so the buffer
handed to the sweep is the *only* copy. Between that and the trim, all 1.079 GiB is accounted for
and both parts have a concrete fix. **Neither fix was applied here, so §11 asks `phaze-5lop` to
apply them and re-measure rather than assuming they land.**

### 6c. What the regression means operationally, unfixed

`phaze-3j67` §9b recommends `memory_request: 3Gi`, `memory_limit: 4Gi`, sized on a 2.57 GiB design
peak. **An unmitigated hybrid at 3.584 GiB breaches that request and sits at 90% of that limit.**
With the trim alone it is 3.181 GiB — still above the 3Gi request. So this is not a footnote:
shipping `phaze-5lop` without recommendation 3 would force `phaze-3j67`'s sizing up a tier
(4Gi/6Gi), which on a 31 GiB node costs a concurrency slot at `cap = 4`. Fixing the double-hold is
cheaper than paying for it.

The one consolation is that the direction is the right one for this node: `phaze-3j67` measured vox
**CPU-bound at W=2 with 13.1 GiB of memory it cannot use**. Trading 1.1 GiB of a resource with 13 GiB
of slack for 42% of the wall clock on the binding resource is a good trade even unmitigated — it is
just an unnecessary one.

______________________________________________________________________

## 7. Where the streaming arm's own cost lives — a two-term model

The bead is right that streaming buys convenience at a price. The price is measurable and it is the
**per-branch fan-out copy**: every window's `Scale → Trimmer` pair consumes the entire resampled
stream and discards what falls outside its range. Sweeping the fan-out width on `dur_3600` isolates
it. Every point is a separate clean run.

**FINE tier (44.1 kHz), `dur_3600`:**

| windows | 1 | 5 | 15 | 30 | 60 |
| --- | ---: | ---: | ---: | ---: | ---: |
| wall (s) | 4.66 | 10.85 | 25.66 | 41.06 | 83.74 |

> `wall ≈ 3.91 + 1.319 × n_windows`  (R² **0.997**)

**COARSE tier (16 kHz), `dur_3600`:**

| windows | 1 | 5 | 10 | 20 |
| --- | ---: | ---: | ---: | ---: |
| wall (s) | 38.80 | 39.74 | 40.64 | 42.86 |

> `wall ≈ 38.61 + 0.212 × n_windows`  (R² **0.999**)

Read against ARM STANDARD's per-window cost on the same file — **5.57 s** fine, **52.12 s** coarse:

| | fixed cost (one decode + resample pass) | marginal cost per window | vs ARM STANDARD's per-window cost |
| --- | ---: | ---: | ---: |
| fine, 44.1 kHz | 3.91 s | **1.319 s** | 5.57 s → **4.2× cheaper** |
| coarse, 16 kHz | 38.61 s | **0.212 s** | 52.12 s → **246× cheaper** |

**This is the whole story of §4.** The fan-out does not abolish the `n_windows × duration` term — it
moves it out of libsamplerate and into a memory copy, and moves the resampling into the fixed term.
Per-file decode cost goes from

```
  n_windows × (decode + resample + copy)          [ARM STANDARD]
```

to

```
  (decode + resample)  +  n_windows × copy        [ARM STREAMING]
```

which is why the coarse tier — where the resample dominates — gains 246× per window while the fine
tier, whose "resample" is a ratio-1.0 no-op, gains only 4.2×. It also predicts the shape of §4's
table: the win grows with duration because the fixed term grows linearly while the marginal term
grows as `n_windows × duration` **only up to the cap**, after which `n_windows` is pinned at 60/30
and the marginal term stops growing relative to the fixed one.

### 7a. What that means for `phaze-5lop`

The fan-out is a **large constant-factor** improvement, not an asymptotic one. Per-file decode stays
`O(n_windows × duration)` in the copy term. If `phaze-5lop` wants genuine `O(duration)`, the copy
term has to go too — which means a **seeking** loader (essentia has none; `AudioLoader` exposes no
seek) or a single sink that emits windows without one branch per window. Neither is available in
essentia's Python streaming API today. The fan-out is what *is* available, it is worth 3.5–19×, and
it is compatible with a later seek-based change rather than in tension with it.

### 7b. The adapter cost the documentation warns about, measured

Standard mode's overhead over streaming, isolated: ARM STANDARD's cost for a **single** window is
4.46 s (fine) / 65.46 s (coarse) against the streaming fixed terms of 3.91 s / 38.61 s. The gap is
the per-call `scheduler::Network` construction, the composite wiring, the `VectorOutput` copy and a
cold-ish first read. It is **0.55 s / 26.9 s per call** — real, and at one call per file it would be
irrelevant. At 90 calls per file it is most of §4's table.

### 7c. The `Scale` interposer (§3b) costs 13% of the fine tier

| `dur_3600`, fine tier, 60 windows | wall | peak |
| --- | ---: | ---: |
| `--variant scale` (per-branch `Scale`, recommended) | 83.74 s | 1.018 GiB |
| `--variant bare` (`Trimmer` straight off the shared `Resample`) | **72.67 s (−13.2%)** | **0.758 GiB (−25.5%)** |

Both produce byte-identical buffers on this wheel. The 13% is the price of not depending on a code
path upstream flags `FIXME`; on the recommended two-pass shape it is 11.1 s of a 126.6 s stage —
**0.6% of a `dur_3600` `analyze_file`**. **Pay it.** Revisit only if a future essentia release resolves the
`FIXME` in a documented direction.

______________________________________________________________________

## 8. The `phaze-15sw` interaction: does anything double-hold?

`phaze-15sw` deliberately holds up to `coarse_cap` decoded 16 kHz buffers concurrently
(30 × 180 s × 16 kHz × 4 B ≈ **345 MB**) so the model-major sweep can run one graph across every
window. A fan-out decode also materialises its windows. The bead asks whether the two compose into
two copies.

| shape (`dur_3600`, 60 fine + 20 coarse) | wall | **peak RSS** | Δ over the 0.388 GiB decode baseline |
| --- | ---: | ---: | ---: |
| streaming, FINE tier only (317 MB PCM) | 83.74 s | 1.018 GiB | +0.630 |
| streaming, COARSE tier only (230 MB PCM) | 42.86 s | 0.689 GiB | +0.301 |
| streaming, **both tiers off one shared pass** (80 sinks) | **119.93 s** | **1.382 GiB** | +0.994 |
| **two passes, one per tier** (sum of wall / max of peak) | 126.60 s | **1.018 GiB** | +0.630 |
| ARM STANDARD, same file (one buffer live at a time) | 1 376.70 s | 0.388 GiB | — |

**The single-shared-pass design double-holds; the two-pass design does not.** Sharing one decode
across both tiers is **5.3% faster** (119.93 vs 126.60 s — the saving is the one mp3 decode and
downmix the two tiers can share) and costs **+0.364 GiB** of peak, because both tiers' PCM is live
simultaneously. Two passes release the fine tier's 317 MB before the coarse tier decodes at all.

**On the arithmetic alone, neither design should change the end-to-end peak**, because neither *is*
the peak: the model sweep's 2.482 GiB (`phaze-15sw`) / 2.566 GiB (`phaze-3j67`) dominates a fan-out
transient of at most 1.38 GiB, and by the time `_run_model_sets_over_windows` starts, the two-pass
hybrid holds exactly the same ~345 MB of coarse PCM `main` holds today.

**§6 measured that reasoning wrong, and §6b says why.** The transients do not vanish when they are
freed — a third is retained by glibc and two thirds is still live inside the `Pool` — so they stack
on the sweep instead of sitting under it, for a measured **+1.079 GiB** at saturated caps. The two
per-tier deltas in the table above (0.630 + 0.301 = 0.931 GiB) account for 86% of it, which is what
makes the mechanism legible rather than mysterious.

The table's own numbers are the fix's specification: **the transient is ~2× its PCM** (0.630 GiB for
317 MB, 0.301 GiB for 230 MB). Sinking into pre-sized `numpy` arrays instead of an essentia `Pool`
should take both to ~1×, which is the ≈345 MB `phaze-15sw` already budgets and this document's whole
double-hold answer: **the design does not double-hold; this prototype's `Pool` usage does.**

______________________________________________________________________

## 9. The three options, priced

### 9a. Stay standard — **reject**

Costs 3.5–18× on decode for no benefit. The peak advantage is illusory: ARM STANDARD's 0.388 GiB
decode peak is irrelevant next to a 2.5 GiB model sweep that both designs share. And per
`phaze-3j67` the node is **CPU-bound at W=2 with 13 GiB of memory it cannot use** — trading wall
clock for memory is exactly the wrong direction now.

### 9b. Go wholesale streaming — **reject, on structure, not on speed**

Not measured, and deliberately: it is ruled out by a contradiction with `phaze-15sw` that no
measurement could rescue.

A wholesale streaming pipeline means the models consume the decoded stream directly —
`MonoLoader >> TensorflowPredictMusiCNN`, upstream's own recommended pipeline and the shape of
`streaming_musicnn_predict.cpp`. To get the single-decode-pass benefit, **all 34 graphs must be
connected to that stream at once**, because a streaming source is consumed once and every sink must
be live to see it. That is precisely the 34-co-resident-graph configuration `phaze-15sw` removed, at
a measured **+4.007 GiB** (`phaze-7i0k` §7c: 4.007 → 0.806 GiB for graph residency; envelope maximum
7.986 → 2.482 GiB). Streaming fan-out and model-major iteration are directly opposed: one decodes
once and requires every consumer live, the other keeps one consumer live and re-reads buffers.
**You can have single-pass decode or single-graph residency — the only way to have both is to
materialise the windows between them, which is the hybrid.**

Three further blockers, recorded so they are not rediscovered:

1. **Per-window granularity is lost.** phaze needs one prediction set per `CoarseWindow`. Streaming
   `TensorflowPredict*` emits patch-wise predictions over a whole stream on a patch grid that does
   not align with window boundaries; mapping patches back to windows is not an identity-preserving
   operation.
1. **The genre model's output would move** (§3d). Standard `TensorflowPredictEffnetDiscogs` pads the
   final batch and trims the padded predictions under its default `lastBatchMode="same"`; the
   streaming variant has no such parameter. This is the one real mode-dependent divergence in
   phaze's model set, and it is not the one the bead flagged.
1. **Upstream itself does not do this.** `MusicExtractor` — essentia's reference pipeline — has two
   stages where the second depends on the first, and its answer is to **construct a second
   `EasyLoader` and decode the file again** (`musicextractor.cpp`, `loader_2` / `network_2`), rather
   than buffer the signal or hold both stages live. If the maintainers pay a full re-decode to avoid
   a wide co-resident network, phaze should not assume the wide network is free.

### 9c. Hybrid — streaming decode per tier, standard model sweep — **adopt**

| | |
| --- | --- |
| decode wall | **3.5× / 10.9× / ≈15× / ≈18×** faster at 10 / 60 / 180 / 720 min |
| end-to-end wall | **−41.7%** at saturated caps (`dur_3600`); −11.7% on a 10-minute file whose caps are not saturated and where decode is only 15% of the work |
| output | **byte-identical** — sha256 + `cmp` on the `analyze_file` result on both files and in all three hybrid runs, plus **136 buffer comparisons, 0 mismatches** |
| peak | **+1.079 GiB measured** at saturated caps. 0.403 GiB of that is glibc retention, removable by one `malloc_trim(0)` at 0.13% wall cost (**measured**); the remaining 0.677 GiB is a `Pool`-materialisation double-hold that recommendation 3 removes (**not yet measured — `phaze-5lop` must confirm**) |
| `phaze-15sw` | fully preserved — the model sweep is untouched, and the coarse buffers it holds are the same buffers, produced differently |
| correctness surface | four traps, all identified with upstream citations, three closed by construction (§3a–c) and the fourth (§3d) avoided by keeping the models in standard mode |
| C++ portability (`phaze-i93a`) | improved — the fan-out is idiomatic streaming, which is the mode essentia says ports straightforwardly, and it is the shape of `streaming_extractor_la-cupula.cpp` |

**The trade in one line:** −42% on the resource `phaze-3j67` measured as binding (CPU / wall clock),
+1.1 GiB on the resource it measured as having 13.1 GiB of unusable slack — and of that +1.1 GiB,
**37% is already measured away by a one-line `malloc_trim`** and the other 63% is diagnosed with a
concrete fix (§6b).

______________________________________________________________________

## 10. What this measurement does and does not support

- **Supported:** decode wall time and peak RSS for both arms at production window geometry across a
  72× duration span; end-to-end `analyze_file` wall and peak on two files, one with the fine cap
  saturated; buffer-level and `analyze_file`-level output identity; the two-term cost model in §7 and
  its coefficients on this node/image; the double-hold arithmetic in §8; the `malloc_trim` split of
  the peak regression in §6b; the reproduction of `phaze-esut` §8 on Linux.
- **Not supported: that recommendation 3 recovers the remaining 0.677 GiB.** §6b identifies the
  `Pool` as the holder from a subtraction (a trim removes the rest) and from the per-tier deltas in
  §8, and the fix follows from that diagnosis — but the fix was **not built or measured**.
  `phaze-5lop` must measure it before anyone re-sizes the pod (recommendation 7).
- **Supported by source, not by measurement:** every claim in §2 and §3, and §9b's rejection of
  wholesale streaming. These are readings of `MTG/essentia@master` plus one already-measured number
  (`phaze-15sw`'s graph-residency cost). They are cited to file and symbol so they can be checked.
- **Not supported: that the speedup transfers to a different node.** The coefficients in §7 are
  Haswell-and-this-image specific. What transfers is the *shape*: a fixed decode term plus a per-window
  copy term, replacing a per-window decode term.
- **Not supported: the long-file ARM STANDARD figures as direct measurements.** They are calibrated
  extrapolations; §4's footnote gives the calibration and its direction (conservative by ~4%).
- **Not supported: anything about wholesale streaming's actual performance.** §9b rejects it on
  structure. If someone wants the number, it has to be measured.
- **Synthetic audio is validated for peak and inherited for wall time.** `phaze-7i0k` §6b established
  content-independence of peak RSS. Decode wall time is a function of container, sample rate and
  duration, not of content — a sine pair and real audio decode identically — so the decode figures
  carry directly. The `analyze_file` totals in §6 include beat-tracking and inference on a sine pair
  and should be read, as `phaze-3j67` §10 says, as an **upper bound** on real-audio throughput.
- **One methodological note worth repeating.** Every RSS/CPU *curve* here is from a host-side sampler
  outside the container; per-process peaks are `VmHWM` read once at exit. Do not substitute an
  in-process sampler thread (`phaze-7i0k` §9). And check node CPU per run — §1b discarded eight runs
  on that basis, and two of them would have changed a conclusion.

______________________________________________________________________

## 11. Recommendations

| | action | why |
| --- | --- | --- |
| 1 | **`phaze-5lop`: replace the per-window `EasyLoader` calls with ONE streaming fan-out network PER TIER** — `AudioLoader → MonoMixer → Resample(native→rate)` fanned out to `Scale(1.0) → Trimmer → sink` per window, run once with `essentia.run()`. Keep the model sweep in standard mode, unchanged. | §4, §6, §9c. **10.9× on decode and −41.7% end-to-end at saturated caps**, output byte-identical by sha256 + `cmp`. This is the spike's deliverable and it supplies `phaze-5lop`'s approach. |
| 2 | **Two passes, not one.** Do not fan both tiers off a single shared decode. | §8. One shared pass is only 5.3% faster — the two tiers cannot share the expensive stage, because their resamplers differ — and holds **+0.364 GiB** more. Bad trade in a change whose one weakness is peak. |
| 3 | **Sink into pre-sized `numpy` arrays, not an essentia `Pool`.** **Load-bearing, not a nicety.** | §6b, §8. The `Pool` keeps a second copy of every window live through the model sweep; that is **+0.677 GiB** of the +1.079 GiB regression and it survives a `malloc_trim`. Every window length is known before the network is built, so the sink can be exact-sized. Without this, `phaze-3j67`'s `memory_request: 3Gi` has to rise. |
| 4 | **Call `malloc_trim(0)` once after each tier's buffers are dropped.** | §6b. **Measured: −0.403 GiB peak for +0.13% wall**, output unchanged. `phaze-7i0k` §4 correctly found `malloc_trim` worthless against the *old* workload; this change creates the large freed transient that makes it pay. |
| 5 | **Keep the per-branch `Scale(factor=1.0)`.** | §3b, §7c. It is `EasyLoader`'s own composition, it reproduces the replayGain preamp exactly (`db2amp(-6+6) == 1.0`), and it gives each `Trimmer` a private parent so the `shouldStop` propagation upstream marks `FIXME` cannot truncate the shared decode. Costs 13% of the fine tier — 0.6% of a full `analyze_file`. |
| 6 | **Do not go wholesale streaming, and correct the "34 graphs at risk" framing before it reaches `phaze-i93a`.** | §3c, §3d, §9b. The normalization hazard is `TensorflowPredictFSDSINet`-only and phaze runs none of it; the real divergence is **one** graph (`discogs-effnet-bs64-1`, batch padding). Wholesale streaming is blocked by a structural conflict with `phaze-15sw`, not by either hazard. |
| 7 | **Re-measure peak at saturated caps after 3 and 4 land**, against `analyze_file`'s peak-RSS log line (`phaze-7qfd`), before touching `phaze-3j67`'s sizing. | §6c. Recommendations 3+4 are predicted to return the peak to roughly baseline; 4 is measured, 3 is not. Do not size the pod against a prediction. |
| 8 | **`phaze-esut` §8's duration gate remains worth having, and this change shrinks what it is for.** | §4a. A 12-hour file's decode falls from **6.15 hours to ~20 minutes**. The exposure-time argument for gating long files does not disappear — inference is still 30 × 34 model runs — but the dominant term in it does. |

______________________________________________________________________

## 12. What this changes upstream

- **`phaze-esut` §8** — confirmed on Linux across a 72× duration span (§4a), the mechanism identified
  (§3b: `Trimmer`'s `shouldStop` cannot cross the `MonoLoader` composite), and its "≈5.7 hours"
  estimate for a 12-hour file measured at **6.15 hours**. Its follow-up **D** ("a single decode pass
  feeding both tiers") is answered: yes for the decode, **no** for "both tiers off one pass" — two
  passes, one per tier.
- **`phaze-esut` §11 row D / `phaze-5lop`** — this spike supplies the approach and the numbers.
  `phaze-5lop` should not implement seek-based extraction: essentia exposes no seek, and the fan-out
  captures most of the win without one (§7a).
- **`phaze-15sw`** — untouched and preserved. §9b records why: single-pass decode and single-graph
  residency are directly opposed, and the hybrid is the only shape that keeps both.
- **`phaze-3j67`** — its "the node is CPU-bound at W=2, wall clock is the scarce resource" finding is
  what makes this change worth making at all, and its `memory_request: 3Gi` / `memory_limit: 4Gi` is
  the thing recommendations 3, 4 and 7 exist to protect: an **unmitigated hybrid measures 3.584 GiB,
  above that request and at 90% of that limit** (§6c). Nothing in its sizing needs to change *yet* —
  `phaze-5lop` has not shipped — but it must not ship without the re-measurement.
- **`phaze-7i0k` §4's `malloc_trim` verdict** — "0.0% on the quantity a limit enforces" was correct
  against the workload it tested and does not survive this change: against a pipeline that frees a
  ~0.9 GiB transient before the model sweep, one trim is worth **−0.403 GiB for +0.13% wall** (§6b).
  The knob did not change; the workload did.
- **`phaze-i93a`** — gated on this result, and unblocked. The recommended shape is idiomatic
  streaming composition, which is the mode essentia says ports straightforwardly to C++ and the shape
  of `streaming_extractor_la-cupula.cpp`. The C++ evaluation should be scoped against the *hybrid*,
  not against today's code — and should note that upstream's own `MusicExtractor` re-decodes rather
  than widening its network.
- **The bead's two findings** — both survive, both relocated. Finding 1's performance trade is real
  and is the **per-branch copy term** (§7), not an engine difference (§2): standard mode is not a
  faster engine, it is the same engine behind a per-call adapter. Finding 2's correctness hazard is
  real upstream and **does not apply to phaze** — it is `TensorflowPredictFSDSINet`-only (§3c) — but
  its *instinct* was right: sweeping for the same class of bug turned up a real mode-dependent
  divergence in phaze's set, `EffnetDiscogs` batch padding (§3d). One graph, not 34, and a different
  mechanism. Both are moot under the recommendation, which keeps every model in standard mode.

______________________________________________________________________

## Appendix — reproducing this

Three short scripts, in the shape `phaze-esut` / `phaze-7i0k` / `phaze-3j67` established (drive the
**real** `phaze.services.analysis`; never reimplement the pipeline):

- **`decodebench.py`** — `standard` and `streaming` arms over the same window geometry, obtained by
  calling `_probe_duration_sec` / `_iter_windows` / `_stride_to_cap` directly. Hashes every decoded
  buffer (sha256 over raw float32) and reports per-tier wall plus its own `VmHWM` read once at exit.
  `--variant scale|bare` selects the §3b interposer; `--tiers both|fine|coarse` selects the §8 shape;
  `--fine-cap/--coarse-cap` drive the §7 width sweep.
- **`hybrid_analyze.py`** — the end-to-end A/B. Monkeypatches `A._analyze_fine_windows` and
  `A._analyze_coarse_windows` with copies whose only difference is the decode loop, then calls the
  **real** `analyze_file`, so §6's identity claim isolates the decode substitution. `--trim` adds a
  `ctypes` `malloc_trim(0)` after each tier's buffers are dropped — the §6b probe.
- **`sampler.py`** — runs on the **host**, outside the container, at 0.5–1 s: `/proc/meminfo`,
  `/proc/stat` deltas for node CPU, and `VmRSS`/`VmHWM` for every `/proc/<pid>` matching the harness.
  Its node-CPU series is the §1b admissibility gate, not decoration.

The pod is a bare `sleep infinity` pod on the burst node using the deployed job image, with the
`phaze-models` PVC mounted **read-only** and a host scratch dir, and **no Kueue queue label** so it
consumes no quota:

```sh
kubectl run/apply a pod:  image ghcr.io/simplicityguy/phaze/job:<tag>
                          volumeMounts: phaze-models (ro) at /models, scratch at /scratch
                          command: ["sleep", "infinity"]
```

The image ships the pre-`phaze-15sw` `analysis.py`, so the shipped model-major pipeline is measured
by copying `/app/src` to `/scratch/src`, overwriting `services/analysis.py` with `main`'s (sha256
`45a84a70…`, the same overlay `phaze-15sw` and `phaze-3j67` used) and putting `/scratch/src` first on
`sys.path`.

Test audio — `phaze-esut`'s generator, touching no real library:

```sh
for d in 600 3600 10800 43200; do
  ffmpeg -loglevel error -y \
         -f lavfi -i "sine=frequency=440:duration=$d" \
         -f lavfi -i "sine=frequency=554:duration=$d" \
         -filter_complex "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]" \
         -map "[a]" -ar 44100 -c:a libmp3lame -b:a 192k "dur_$d.mp3"
done
```

**The fan-out network, in full** — this is the whole change, and it is 20 lines:

```python
pool   = essentia.Pool()
loader = ess.AudioLoader(filename=path, computeMD5=False)
mixer  = ess.MonoMixer(type="mix")
loader.audio          >> mixer.audio
loader.numberChannels >> mixer.numberChannels
loader.md5 >> None; loader.bit_rate >> None; loader.codec >> None; loader.sampleRate >> None

# ONE resampler per tier, hung off the mixer -- NOT off a MonoLoader (see s3a)
rs = ess.Resample(inputSampleRate=native_sr, outputSampleRate=tier_rate, quality=1)
mixer.audio >> rs.signal

for idx, start, end in windows:                       # windows from _stride_to_cap
    sc = ess.Scale(factor=1.0)                        # EasyLoader's replayGain preamp,
    tr = ess.Trimmer(sampleRate=tier_rate,            #   AND each Trimmer's private
                     startTime=start, endTime=end)    #   parent (see s3b)
    rs.signal >> sc.signal
    sc.signal >> tr.signal
    tr.signal >> (pool, f"w.{idx}")

essentia.run(loader)                                  # ONE pass, N windows out
```

**Four things to get right when re-running.** Branch at `MonoMixer`, not `MonoLoader` (§3a).
Interpose a per-branch algorithm before each `Trimmer` (§3b). Sample RSS from **outside** the process
(`phaze-7i0k` §9). And record node CPU per run and throw away anything outside the single-threaded
band — §1b discarded eight contaminated runs, and two of them pointed the opposite way from the
truth.
