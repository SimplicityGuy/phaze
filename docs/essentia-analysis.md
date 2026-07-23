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

`analyze_file()` in `analysis.py` runs **two passes per file**, decoding one short
window at a time via segmented `EasyLoader` (so no essentia algorithm ever sees more
than one window — the architecture that fixed the long-file `RhythmExtractor2013`
buffer overflow / OOM):

| Pass | Sample rate | Window | essentia algorithms | Features produced |
| ---- | ----------- | ------ | ------------------- | ----------------- |
| **FINE** (≤60 windows) | 44.1 kHz | 30 s | `RhythmExtractor2013(method="multifeature")` + `KeyExtractor(profileType="edma")` | `bpm`, `musical_key` (+ per-window time series) |
| **COARSE** (≤30 windows) | 16 kHz | 180 s | 34 TensorFlow graphs (11 sets × 3 variants + `discogs-effnet` genre) | `mood`, `style`, `danceability`, full `features` JSONB |

Per-file cost is bounded by caps (`fine_cap=60`, `coarse_cap=30`): a file whose
natural window count exceeds a cap is **strided evenly across the whole file**
instead of analyzed window-by-window, so cost is O(constant), not O(duration)
(the root-cause fix for the 4h-timeout incident).

> **Out of scope:** audio **fingerprinting** is *not* essentia — it is handled entirely by
> the `audfprint` and `panako` HTTP sidecars, which the app calls over httpx
> (`services/fingerprint.py`). There is no `pyacoustid` dependency and nothing imports it;
> `libchromaprint` / `fpcalc` survives in the images only as an `essentia-tensorflow`
> runtime dependency. This page is only about the `essentia-tensorflow` analysis stage.

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
| coverage contract | `fine/coarse_windows_analyzed/total`, `sampled` | re-deepen a sampled file later |

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

### #3 — Decode-once across the two passes *(verdict: NOT worth it standalone)*

The fine (44.1 kHz / 30 s) and coarse (16 kHz / 180 s) passes decode the source
audio's codec twice. The redundancy is real, **but eliminating it cleanly means
unifying the two window sizes — and those sizes are load-bearing in opposite
directions:** rhythm extraction needs *short* windows (long windows reopened the
`OnsetDetectionGlobal` overflow / OOM), while the TF models want *longer* windows
(shorter windows = 6× more inference calls). You cannot merge them without
reintroducing a fixed bug or adding compute. Decode is also a secondary fraction of
wall-clock behind `RhythmExtractor2013 multifeature`. **Recommendation: do not fix
in isolation.** Fold decode-sharing in only if the windowing is being restructured
for another reason.

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
