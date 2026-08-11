# 🔬 Essentia Usage & Replacement Analysis

**Status:** Analysis / decision record — no code changes. Authored 2026-07-13.

This document answers a standing question: **can we replace `essentia-tensorflow`
with something less compute-intensive without losing any features?** It maps where
essentia is used, establishes the true compute profile, enumerates the feature
surface that any alternative must preserve, surveys the replacement landscape
(validated by web research), and gives ranked, feature-preserving recommendations.

**TL;DR:** There is **no drop-in replacement** that is both lighter *and*
feature-complete. The high-level classifiers (mood/genre/danceability) are
effectively the Essentia-models ecosystem; every alternative is either the same
weights, a slower pure-Python library, or a heavier neural net. And the compute
cost is **not** where intuition points — it is the DSP + audio decode, not the
TensorFlow models. The single high-value, feature-preserving lever is to **retune
essentia's own tempo algorithm**, not to swap the library.

______________________________________________________________________

## 🗺️ Where essentia is used

The entire essentia **compute** surface is one module: `src/phaze/services/analysis.py`.
Everything else that mentions essentia is plumbing:

| File | Role (no DSP) |
| ---- | ------------- |
| `src/phaze/scripts/download_models.py` | Fetches the `.pb`/`.json` weights from `essentia.upf.edu` (~3.1 GB) |
| `src/phaze/tasks/_shared/model_bootstrap.py` | Auto-downloads weights when `/models` is empty |
| `src/phaze/services/kube_staging.py` | Mounts the read-only models PVC at `/models` for Kueue Jobs |
| `src/phaze/schemas/agent_tasks.py`, `tasks/functions.py`, `job_runner.py` | Carry `models_path`; defer the heavy import to call time |
| `src/phaze/analysis_child.py`, `src/phaze/services/analysis_exec.py` | Phase 101 subprocess boundary: `analyze_file` now runs in a real child process (`python -m phaze.analysis_child`), spawned by the shared parent driver, so essentia's C++ never holds the parent asyncio event loop's GIL |

`analyze_file()` in `analysis.py` runs **two passes per file**, analyzing one short window
at a time (so no essentia algorithm ever sees more than one window — the architecture that
fixed the long-file `RhythmExtractor2013` buffer overflow / OOM). Since `phaze-5lop` each
pass gets its windows from **one streaming decode network per tier** —
`MonoLoader(sampleRate=tier_rate)` fanned out to a `Scale(1.0) → Trimmer` branch per window,
run once — rather than one `EasyLoader` call per window:

| Pass | Sample rate | Window | Chunk | essentia algorithms | Features produced |
| ---- | ----------- | ------ | ----- | ------------------- | ----------------- |
| **FINE** | 44.1 kHz | 30 s | 60 windows | `RhythmExtractor2013(method="multifeature")` + `KeyExtractor(profileType="edma")` | `bpm`, `musical_key` (+ per-window time series) |
| **COARSE** | 16 kHz | 180 s | 30 windows | 34 TensorFlow graphs (11 sets × 3 variants + `discogs-effnet` genre) | `mood`, `style`, `danceability`, full `features` JSONB |

### Coverage is exhaustive; memory is bounded by the CHUNK

**Every file gets every natural window of both tiers**, whatever its duration. There is no
window cap, no even stride, and no `sampled` result — operator decision 2026-08-11, implemented
by `phaze-w55w1`; see [ADR-0007](design/0007-windowed-analysis.md) for the rationale and the
cost analysis it accepted.

Phase 43 previously bounded per-file cost with `analysis_fine_cap=60` / `analysis_coarse_cap=30`
and a stride across the whole file, so anything past ~30 min (fine) / ~90 min (coarse) — i.e.
every concert set — was **sampled**. That made per-file cost `O(constant)`, which was the
root-cause fix for the 4h-timeout incident, at the price of never fully analyzing the archive's
most interesting files.

What bounds cost now is **chunking**, not discarding audio. Each tier is decoded and analyzed a
bounded number of windows at a time (`_FINE_CHUNK_WINDOWS` = 60, `_COARSE_CHUNK_WINDOWS` = 30 in
`services/analysis.py`), so peak PCM residency is a function of the chunk and **not** of the
file:

| Tier | Peak PCM per chunk | Arithmetic |
| ---- | ---: | --- |
| FINE | ~317 MB | 60 × 30 s × 44 100 Hz × 4 B |
| COARSE | ~345 MB | 30 × 180 s × 16 000 Hz × 4 B |

The chunk sizes are the old cap values **on purpose**: they reproduce exactly the per-tier
residency the capped implementation was measured at, so ADR-0005's Job memory limits stay valid
across the removal instead of needing re-derivation. The two tiers never stack (fine's chunk is
released and `malloc_trim`ed before coarse starts).

Two costs are paid for that, both deliberate and both documented in the D-07 decision record
above `_FINE_CHUNK_WINDOWS`:

- each of the 34 TF graphs is constructed once per COARSE CHUNK rather than once per file
  (the phaze-15sw model-major invariant — never more than one graph resident — is unchanged);
- each chunk needs its own decode pass, because `MonoLoader` cannot seek. Non-final chunks
  interpose a `Trimmer` directly on the loader so the decode stops at the chunk boundary,
  which brings the total from `K × duration` down to roughly `duration × (K + 1) / 2`.

> **Not yet measured on real hardware.** The table above is per-window buffer arithmetic carried
> forward from the capped implementation, plus a synthetic 12-hour bounded-memory test
> (`tests/analyze/services/pipeline/test_analysis_long_file.py`) that proves the SHAPE — peak
> RSS moves <25 MB between a 2h and a 12h file despite 6× the analyzed windows. ADR-0007
> follow-up 3's ask for a real peak-RSS/wall-clock measurement on a genuine multi-hour file
> **is still open**.

### Liveness is progress-based, never wall-clock

Exhaustive analysis means a multi-hour set legitimately runs for many hours, so nothing kills an
analysis for taking too long. The child heartbeats every unit of progress (window completions,
chunk decodes, each coarse model sweep) and the supervising layer kills it only after
`analysis_stall_timeout_sec` (default 1800 s) of **total silence**:

| Layer | Before (Phase 43) | Now (phaze-w55w1) |
| --- | --- | --- |
| in-process child | `analysis_inner_timeout_sec` = 6600 s SIGKILL | stall watchdog in `services/analysis_exec.py` → `AnalysisStalledError` |
| SAQ `process_file` job | `timeout=7200` | `timeout=0` (disabled) + `heartbeat=analysis_stall_timeout_sec`, touched by the lane |
| k8s burst pod | no bound at all | the same stall watchdog (`job_runner` passes `stall_timeout`) |

A wall clock cannot tell a long analysis from a hang — `phaze-1b39` (2026-07-28) is the incident
where trying SIGTERM'd legitimate 2–6 hour concert sets and stalled the whole burst lane. A
genuinely wedged child is still killed in bounded time, and the file gets a stored
`error_message` naming the stall and the stage it died in.

> **Removed, not essentia's concern:** audio **fingerprinting** was never part of essentia — it was
> handled entirely by the `audfprint` and `panako` HTTP sidecars, which the app called over httpx.
> Both engines and every integration point were removed (phaze-0jpe, 2026-07-28; see
> `docs/design/0002-fingerprint-removal.md`). There is no `pyacoustid` dependency and nothing
> imports it. `libchromaprint` / `fpcalc` also survive in the images, but **not** because
> `essentia-tensorflow` needs them at runtime — `ldd` on the shipped `_essentia` extension shows
> no chromaprint link and `import essentia` succeeds without it (phaze-0jpe.6 correction, tested
> against the live deployment). They have no verified consumer anywhere in this codebase; see
> `docs/design/0002-fingerprint-removal.md` for the full correction. This page is only about the
> `essentia-tensorflow` analysis stage.

______________________________________________________________________

## ⚙️ The compute profile (the counterintuitive part)

The scary-looking part of essentia — 34 models, ~3.1 GB of weights — is **cheap at
inference**. The wall-clock is dominated by **audio decode + native C++ DSP**, and
the single most expensive algorithm is `RhythmExtractor2013(method="multifeature")`,
which internally runs multiple onset-detection functions over each 44.1 kHz window.
The TensorFlow model step runs inference on short windows and is a negligible slice
of total time.

This has a sharp consequence:

- **Replacing the ML classifiers with a "lighter" library buys almost no compute.**
  It buys image size, RAM, and cold-start (real wins) — but not CPU-seconds.
- **The compute lever is the tempo/key DSP and the decode path**, not the models.

Corollaries already settled in prior investigation:

- **GPU / Coral Edge TPU do not help.** They only accelerate the negligible
  inference slice; the CPU decode/DSP critical path is unchanged. essentia ships
  full float TF graphs, not Edge-TPU-compiled TFLite.
- **The throughput lever is horizontal CPU parallelism across files** — which the
  Kueue burst / multi-compute agents already deliver.

> **⚠️ Half of this section is now falsified — measured, twice, on the Linux burst node.**
> The claim that TF inference is "a negligible slice" was never measured; it is wrong, and it
> was wrong even when written. Measured `analyze_file` on a 60-minute file at production caps
> (vox, deployed image, synthetic audio):
>
> | | before `phaze-5lop` | after `phaze-5lop` |
> | --- | ---: | ---: |
> | total wall | 3 205.05 s | **1 960.37 s** |
> | audio decode + resample | 1 370.77 s (**42.8%**) | **126.98 s (6.5%)** |
> | everything else (34 TF graphs × 20 windows, + `RhythmExtractor2013`/`KeyExtractor` × 60) | 1 834.28 s (57.2%) | 1 833.39 s (**93.5%**) |

> *(That measurement predates `phaze-w55w1` and was taken at the then-saturated caps. Its
> proportions still hold — the model step still dominates — but the absolute figures are now a
> per-60-minutes rate rather than a per-file total, since a longer file analyzes proportionally
> more windows instead of striding down to the same 60/20.)*
>
> **What survives.** Decode + native DSP really did dominate — decode alone was 43% of a
> 60-minute analyze, and `phaze-esut` §8 found why: `EasyLoader` does not seek, so every one of
> the 80–90 windows re-decoded the file from byte 0. `phaze-5lop` removed that multiplier and
> decode fell to **6.5%**. The compute lever *was* the decode path, and it has now been pulled.
>
> **What does not survive: "the TF model step is a negligible slice."** It was 57% of the wall
> before and is **93%** after — the two rightmost cells above are the same 1 834 seconds, which
> is the point: nothing about the model step changed, it simply stopped being hidden behind the
> decode. So the corollaries below are now *load-bearing in the opposite direction*: replacing
> the classifiers with something cheaper, or `#4`'s ONNX/TFLite re-export, is no longer "image
> size, RAM and cold-start but not CPU-seconds" — after `phaze-5lop` the inference **is** the
> CPU-seconds. (The GPU/Edge-TPU corollary is unaffected for a different reason: `phaze-mqq5`
> and `phaze-i93a` evaluated the runtime question directly and on its own terms.)

______________________________________________________________________

## 🔒 Feature surface that must be preserved

Any replacement must reproduce **all** of the following (traced to live consumers).
The `features` JSONB is fed verbatim to the LLM in
`proposal.py` (`build_file_context`), so nothing inside it is disposable.

Stored on `AnalysisResult` (`models/analysis.py`) and per-window on `AnalysisWindow`:

| Feature | Source algorithm | Consumed by |
| ------- | ---------------- | ----------- |
| `bpm` | `RhythmExtractor2013` (fine) | column + LLM prompt + per-window time series |
| `musical_key` | `KeyExtractor` (fine) | column + LLM prompt + per-window time series |
| `mood` | 7 mood model sets × 3 variants (coarse) | column + LLM prompt |
| `style` | `discogs-effnet` genre (coarse) | column + LLM prompt |
| `danceability` | danceability set × 3 variants | inside `features` / per-window |
| `features` (full JSONB) | all 11 sets + genre — incl. `gender`, `tonality`, `voice_instrumental` | fed verbatim to the LLM |
| progress counts | `fine/coarse_windows_analyzed/total` | the in-flight progress bar + the completion PUT (equal on a healthy file since phaze-w55w1) |

Load-bearing coupling to note: `aggregate_bpm()` **excludes windows with
`confidence == 0.0`**, and `analysis.py` unpacks `confidence` from
`RhythmExtractor2013`. Any tempo change that drops the confidence signal silently
discards every window (see recommendation #1).

______________________________________________________________________

## 🔎 Replacement landscape — researched verdict: no lighter drop-in

The Python MIR ecosystem is small and well-mapped. Each candidate was checked
against both hard constraints — *less compute* **and** *no feature loss*:

| Library | Tempo / key DSP | Mood / genre / danceability classifiers | Compute vs essentia |
| ------- | --------------- | --------------------------------------- | ------------------- |
| **Essentia** (current) | C++ `RhythmExtractor2013`, `KeyExtractor` | ✅ full pretrained TF model zoo | Baseline — literature calls it *"optimized for computational speed and low memory"* |
| **librosa** | pure NumPy `beat_track`, chroma-key | ❌ none | **Slower** (interpreted) — a regression |
| **madmom** | RNN/CNN beat tracking (most *accurate*) | ❌ none | **Heavier** (deep learning) |
| **aubio** | C, very fast tempo/onset/pitch | ❌ none | Faster DSP, but **no classifiers, no confidence** |
| **MIRFLEX** (2024) | wraps other extractors | CNN models for genre/mood/instrument | Research aggregator, **not lighter**; overlaps essentia's exact feature set |

Two decisive findings:

1. **Essentia is documented as one of the *faster, lower-memory* options in the
   field**, not a slow one — its C++ core is why. "Swap it for something lighter"
   has no obvious target: alternatives are either interpreted (librosa, slower) or
   deep-learning (madmom / MIRFLEX, heavier).
2. **The high-level classifiers are effectively the Essentia-models ecosystem.**
   The only alternatives are the *same* MusiCNN / EffNet weights repackaged, or
   newer neural embedders (PANNs, MERT, CLAP) that are *more* expensive. No lighter
   library reproduces mood / genre / danceability / gender / voice-instrumental —
   and that is exactly the `features` JSONB the LLM consumes.

**Net:** no candidate clears both bars. The only genuinely *faster* library is
**aubio**, and only for the tempo/key half — at the cost of the confidence signal
and with no classifiers. It is a fallback for the fine-tier DSP *only if* essentia's
own cheaper tempo methods prove insufficient — not a replacement.

______________________________________________________________________

## ✅ Recommendations (ranked by compute-per-risk; all preserve every feature)

### #1 — Retune essentia's tempo algorithm *(highest value, stays in-library)*

`RhythmExtractor2013(method="multifeature")` is essentia's slowest tempo method by
design. Cheaper options **in the same library** (Context7-confirmed against current
essentia docs):

- `method="degara"` — same algorithm, single onset-detection function, materially faster.
- `PercivalBpmEstimator()` — faster still, BPM-only.
- `TempoCNN` (`deeptemp-k16-*.pb`) — a small CNN at 11 kHz; ML tempo, accurate.

> ⚠️ **Load-bearing caveat — do not switch naively.** Per the essentia reference,
> `degara` **always returns confidence = 0**, and `PercivalBpmEstimator` returns no
> confidence at all. Because `aggregate_bpm()` filters on `confidence != 0.0`,
> either would make it discard *every* window — silent BPM loss. This is a small
> change to `aggregate_bpm`'s confidence handling **plus** a parity validation, not
> a one-liner.

Validate with the existing `scripts/parity/compare_analysis.py` +
`dump_analysis.py` on a real corpus before committing. **This is the first thing to
try.**

### #2 — Prune redundant classifier variants *(footprint / RAM / cold-start, not wall-clock)*

Each mood/danceability set averages 3 variants (`musicnn_msd`, `musicnn_mtt`,
`vggish`). Averaging 3 correlated classifiers is diminishing returns; going to 1–2
variants cuts the model download from ~3.1 GB toward ~1 GB and speeds
`model_bootstrap`. Because TF inference is cheap, wall-clock barely moves — but it
directly helps the OCI-free-tier / Kueue-pod memory story. Gate on parity (it
slightly changes outputs).

### #3 — Decode-once across the two passes *(verdict: NOT worth it standalone — and still true)*

The fine (44.1 kHz / 30 s) and coarse (16 kHz / 180 s) passes decode the source
audio's codec twice. The redundancy is real, **but eliminating it cleanly means
unifying the two window sizes — and those sizes are load-bearing in opposite
directions:** rhythm extraction needs *short* windows (long windows reopened the
`OnsetDetectionGlobal` overflow / OOM), while the TF models want *longer* windows
(shorter windows = 6× more inference calls). You cannot merge them without
reintroducing a fixed bug or adding compute. **Recommendation: do not fix
in isolation.** Fold decode-sharing in only if the windowing is being restructured
for another reason.

> **Measured, and the verdict holds — but it was aimed at the wrong redundancy
> (`phaze-rc1q` §8, shipped as `phaze-5lop` 2026-08-06).** Sharing ONE decode across both
> tiers was built and measured: it is **5.3% faster and costs +0.364 GiB** of peak, because
> the two tiers cannot share the expensive stage at all — their **resamplers differ**
> (44 100→44 100 is a near no-op, 44 100→16 000 is real libsamplerate work), so all a shared
> pass shares is the mp3 decode and downmix, while both tiers' PCM is live at once. Bad trade.
> phaze therefore runs **two passes, one per tier.**
>
> The redundancy that *was* worth removing is a different one this section did not see:
> **within** a tier, `EasyLoader` does not seek, so every window re-decoded and re-resampled
> the file **from byte 0** — 80–90 full passes per file, not 2. Removing that is
> **3.5×/10.9×/≈15×/≈18×** on decode at 10/60/180/720 minutes, for byte-identical buffers.
> It required exactly the windowing restructure this section says to wait for, and it left the
> two window sizes untouched.

### #4 — Convert TF graphs to ONNX Runtime / quantized TFLite *(footprint / dependency, not wall-clock)*

Exporting the 34 classifier graphs to ONNX would let us drop the heavyweight
`essentia-tensorflow` wheel (the thing pinning the project to `cp314`-only) and keep
essentia-core for DSP only. Helps RAM / startup / deploy simplicity — **not**
CPU-seconds — and is a real re-export-and-revalidate project, not a quick win.

______________________________________________________________________

## 🚫 What NOT to do

- **Don't rip out essentia for librosa.** librosa's beat tracking is pure NumPy and
  often *slower* than essentia's C++ (a compute regression), and it has **no**
  pretrained mood/genre/danceability classifiers — so you'd lose features or bolt on
  separate models anyway. Fails both bars.
- **Don't reach for GPU / Coral.** They accelerate only the negligible inference
  slice; the CPU decode/DSP critical path is unchanged.

______________________________________________________________________

## 🎯 Bottom line

"Essentia is compute-intensive" is really "`RhythmExtractor2013` multifeature +
audio decode is compute-intensive." The highest-value, feature-preserving move is
**#1 — retune the tempo algorithm within essentia** (with the confidence-filter fix
and a parity check). A full library swap would risk features and, in librosa's case,
make compute *worse*. If the real pain is memory / image-size / cold-start rather
than CPU-seconds, aim at **#2 / #4** instead — a different problem than "compute
intensive." Any of these is a separate implementation phase with parity validation;
this document only records the analysis.

> **Re-read this against the compute-profile correction above (2026-08-06).** The decode half of
> that sentence was the true half, and it has been dealt with: `phaze-5lop` cut audio decode
> from **42.8% of a 60-minute analyze to 6.5%** by removing the 80–90 redundant full-file
> decodes `EasyLoader`'s missing seek was causing — for byte-identical output, no window-size
> change, and none of the feature risk this section weighs. **The remaining answer to "essentia
> is compute-intensive" is the 34 TensorFlow graphs, which are now 93% of the wall clock.** So
> the ranking here has inverted: **#4** (ONNX / quantized TFLite re-export) and **#2** (prune
> redundant classifier variants) are now the CPU-second levers, and **#1** is a lever on ~2% of
> the wall. `phaze-mqq5` and `phaze-i93a` evaluate that ground directly and supersede this
> ranking; read them first.

______________________________________________________________________

## 📚 Sources

- [Essentia (MTG) — homepage & docs](https://essentia.upf.edu/)
- Essentia beat-detection reference (`RhythmExtractor2013` `multifeature` vs
  `degara`; `degara` outputs confidence = 0; `PercivalBpmEstimator`; `TempoCNN`) —
  <https://essentia.upf.edu/tutorial_rhythm_beatdetection.html> and
  <https://essentia.upf.edu/reference/streaming_RhythmExtractor2013.html>
  (verified via Context7, 2026-07-13)
- [Essentia: an Audio Analysis Library for MIR (ISMIR 2013)](https://ismir2013.ismir.net/wp-content/uploads/2013/09/177_Paper.pdf)
  — "optimized for computational speed and low memory"
- [Audio & Music Analysis on the Web using Essentia.js (TISMIR)](https://transactions.ismir.net/articles/10.5334/tismir.111)
  — MIR library landscape (Essentia / librosa / madmom / Yaafe / aubio)
- [MIRFLEX: Music Information Retrieval Feature Library for Extraction (arXiv 2411.00469)](https://arxiv.org/abs/2411.00469)
  · [GitHub](https://github.com/AMAAI-Lab/mirflex)
- [aubio](https://github.com/aubio/aubio) — C, fast tempo/onset/pitch (no high-level classifiers)
- [madmom (CPJKU)](https://dl.acm.org/doi/10.1145/2964284.2973795) — deep-learning beat tracking, higher accuracy / heavier

______________________________________________________________________

<div align="center">
↩️ Back to the <a href="README.md">docs index</a>
</div>
