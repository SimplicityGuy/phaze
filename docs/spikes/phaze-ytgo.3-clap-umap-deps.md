# S3 — CLAP text-search and UMAP music-map as new model dependencies

- **Bead:** `phaze-ytgo.3` (epic `phaze-ytgo` — AudioMuse-AI: clean-room vs sidecar, per purpose)
- **Date:** 2026-07-25
- **Tree:** `c01f36d` (base `b051b3b` + `phaze-ytgo.1`)
- **Status:** investigation only. No dependency added to `pyproject.toml`, no migration, no
  product code. All measurement code lives under this session's scratchpad, never under `src/`.

> **Clean-room statement.** This spike is CLEAN-ROOM SEALED. No AudioMuse-AI `.py` source was
> read while producing this document. What *was* read, per the epic's seal: AudioMuse-AI's
> published `README.md`, `docs/ARCHITECTURE.md` and `docs/ALGORITHM.md` (fetched from the public
> GitHub raw-content mirror), for their prose description of behaviour, methods and data flow.
> Every AudioMuse-specific claim below cites one of those three files by name; nothing below is
> inferred from or paraphrased out of AudioMuse's implementation. Per a mid-flight correction to
> the seal (config variable *names* are literal expression and should not be transcribed even
> though they appear in prose docs — ideas and methods of operation are not), this document
> describes what each documented control does rather than quoting its literal name; any
> `CLAP_*`/`PCA_*`-shaped identifier that appeared in an earlier draft has been rewritten as a
> role description. Independent, non-AudioMuse sources used freely: Hugging Face model
> cards/API, PyPI package metadata, GitHub repository metadata for `LAION-AI/CLAP`,
> `microsoft/CLAP` and the standalone `NeptuneHub/AudioMuse-AI-DCLAP` weights repository (licence
> fields and one-line descriptions, not AudioMuse-AI's own application source), the MTG's public
> essentia-models licence page, and phaze's own tree.

______________________________________________________________________

## Question

Per the bead: CLAP and a 2D projection (UMAP or PCA) are two capabilities AudioMuse's published
docs describe that **no model phaze ships today can produce**, regardless of what the sibling
embeddings spike (`phaze-ytgo.2`) finds about essentia's own embeddings. Price them **separately**
so `phaze-ytgo.7` (D1) can rule on each independently:

1. **CLAP** — candidate self-hosted models, the licence of each candidate's *weights*
   specifically, model size, runtime requirement (essentia's existing footprint, or a new
   torch/ONNX dependency), and per-file inference cost (measured or cited). Test the bead's own
   hypothesis: phaze already derives mood/style from essentia, so is CLAP's incremental value for
   mood actually small, with the real new capability being free-text query?
2. **Projection** — is UMAP warranted for P4's music map at ~200,000-point archive scale, or does
   PCA suffice? Weigh `umap-learn`'s dependency cost against PCA (reachable via numpy/sklearn,
   already partially in phaze). Address how the projection stays current as the archive grows,
   given UMAP has no natural incremental-add story.
3. **Separability** — can P2's free-text search be deferred indefinitely without blocking a GO on
   the rest of the molecule, so other purposes are not held hostage to a torch dependency?

## Method

Desk research plus small throwaway measurement, per the bead's explicit scope (a full CLAP
integration is out of scope). Concretely:

1. **Read AudioMuse's published prose** (`README.md`, `docs/ARCHITECTURE.md`,
   `docs/ALGORITHM.md`, fetched via `raw.githubusercontent.com`) for every CLAP/DCLAP and
   UMAP/PCA passage, to ground every AudioMuse-specific claim in an exact, quotable source rather
   than assumption.
2. **Audited phaze's own tree and dependency lockfile** for the real runtime footprint claim:
   read `pyproject.toml` / `uv.lock` for existing dependencies (no `torch`, `onnxruntime`,
   `scikit-learn`, or `umap-learn` anywhere today), and inspected the installed
   `essentia-tensorflow` wheel's bundled shared libraries directly (`essentia/.dylibs/*.dylib`)
   plus `src/phaze/services/analysis.py`'s actual TensorFlow call sites.
3. **Queried Hugging Face's public model API and PyPI's public JSON API** (network reachable
   from this environment) for candidate CLAP models: licence field, checkpoint file size (via the
   Git LFS pointer, which states the true byte size), architecture config (audio/text tower
   parameter counts), and for the underlying GitHub repos' licence files (`LAION-AI/CLAP`,
   `microsoft/CLAP`) and the corresponding Zenodo record.
4. **Ran a real, timed inference benchmark** in an isolated scratch venv (`uv venv`, never
   touching phaze's own `.venv` or `pyproject.toml`): downloaded `laion/clap-htsat-unfused`
   (Apache-2.0) via `transformers`, and embedded a 30-second clip cut with `ffmpeg` from a real
   archive-style sample file already present on this machine
   (`/Users/Robert/phaze-watch-test/Marco_V_-_GODD_(T78_Extended_Remix).mp3`, a purchased
   Beatport track — not synthetic audio) plus the same file's full 7m21s duration, and several
   short free-text queries. CPU only (no GPU/MPS backend requested).
5. **Ran a real, timed UMAP-vs-PCA benchmark** in a second isolated scratch venv (Python 3.14,
   matching phaze's pin) on synthetic 200-dimension and 11-dimension float32 matrices at
   200,000 rows (S1's archive-scale figure), covering `fit_transform` cost and the cost of
   projecting *new* points against an already-fitted model (the incremental-add question).
6. **Verified `umap-learn`'s dependency tree resolves cleanly against Python 3.14** with a real
   `uv pip install` in a Python-3.14 scratch venv, since phaze's whole point of using 3.14 is a
   recent-Python constraint that has broken other tooling before (per this repo's own
   `essentia-tensorflow` version-gating comment in `CLAUDE.md`).

### What could not be measured, and is marked so rather than estimated

- **Inference timings are from this development laptop's CPU (Apple M-series), not phaze's
  production/runtime hardware**, and are single-file, small-N timing loops (5 audio runs, 3 text
  runs), not a statistically representative sweep across the archive's real genre/duration mix.
  Cited as measured-and-narrow, not as a validated production SLA number.
- **No AudioMuse "DCLAP" checkpoint, export script, or size figure could be found anywhere
  public** (searched Hugging Face model search, AudioMuse's GitHub releases). AudioMuse's own
  docs never name a source or licence for it (see CLAP §Evidence below). Its actual size and
  exact quality are therefore **unmeasured and unmeasurable** from published material alone —
  this spike substitutes the nearest publicly-available, licence-clear equivalents instead
  (`laion/clap-htsat-unfused` et al.) and says so explicitly everywhere it does.
- **UMAP's fit/transform costs were measured on synthetic Gaussian data**, not real per-file
  embeddings (none exist in phaze yet — that is `phaze-ytgo.2`/`.6`'s open question). Wall-clock
  cost for `fit`/`transform` depends only on matrix shape and hyperparameters, not on the
  semantic content of the vectors, so the *timing* numbers below are valid regardless; only
  *projection quality* (cluster separation) would need real embeddings to assess, and this spike
  does not attempt that — S1's rubric already flags P4 as needing an EFB-delta measurement that
  is out of this spike's scope.

______________________________________________________________________

## Evidence

### E1 — What AudioMuse's published docs actually say about CLAP (prose only, cited)

From `docs/ARCHITECTURE.md`: "Performs sonic analysis using Librosa and ONNX models (MusiCNN,
**DCLAP**, Whisper, Silero, GTE)" and "Analysis Results: Mood scores, embeddings, feature vectors,
lyrics and **CLAP embeddings**."

From `docs/ALGORITHM.md` (§12, "Text Search (DCLAP)"), quoting the load-bearing passages:

> "CLAP is used as **two separate ONNX models**, and that split is deliberate: The **audio model**
> is the **distilled DCLAP student model**. It is loaded in the **worker** containers during
> analysis and produces a **512-dimension embedding** per track. The **text model** is the
> **original LAION CLAP text encoder**. It is much larger and it is loaded in the **web**
> container only when a search needs it."

> "The text is tokenized with the **RoBERTa tokenizer** from the `transformers` library."

AudioMuse's docs describe a master on/off switch for the whole CLAP subsystem: with it off, no
CLAP embedding is produced during analysis and the free-text search page is hidden, and the
stored other-features vector starts as all zeros and is only populated once CLAP is enabled
(§2, feature-vector construction). They also state that the six other-feature labels (danceable,
aggressive, happy, party, relaxed, sad) are **not** a separate model — they are CLAP *text*
similarity between the track's CLAP audio embedding and a precomputed embedding of each label,
with the label embeddings cached for reuse rather than recomputed per query.

**Three findings, none requiring the source code:**

1. **AudioMuse itself does not run CLAP through PyTorch at inference time — it runs a
   self-distilled, self-exported ONNX pair.** The epic's framing ("torch/ONNX runtime") is not an
   open question for AudioMuse's own design; AudioMuse chose ONNX Runtime specifically to keep
   both the worker and web footprints small, and documents a threading knob that lets ONNX
   Runtime manage its own thread pool rather than the Python process managing it. This is a real,
   citable engineering choice worth copying in spirit — but the artifact it depends on ("the
   distilled DCLAP student model") is **not a downloadable, licensable thing phaze can reuse** on
   its own terms (its licence is now known — AGPL-3.0, E3 — but "known and blocking" is not
   "reusable"): it does not appear on Hugging Face (searched
   `dclap`, `distil-clap` — zero hits), it is not attached to any AudioMuse GitHub release, and
   AudioMuse's own docs never name where it came from or under what licence. **AudioMuse's
   published material does not answer the licence question for its own audio model** — which
   means the "which model, what licence" work for phaze is greenfield regardless.
2. **AudioMuse's own mood/other-features feature is exactly the bead's hypothesis, confirmed
   directly from AudioMuse's own words**: "the six other-feature labels … are not a separate
   model" — they are CLAP text-embedding similarity, a *re-derivation* of labels via a much more
   expensive general-purpose model, standing in for what would otherwise be six purpose-built
   binary classifiers. phaze already has six purpose-built binary classifiers for exactly this
   (`mood_relaxed-musicnn-msd-2`, `danceability-musicnn-msd-2`, etc. — S1 §E4). AudioMuse needed
   CLAP for this because AudioMuse's embedding model (MusiCNN-based per its own doc: "Audio
   Analysis: Performs sonic analysis using Librosa and ONNX models (MusiCNN...)") does not itself
   ship those six classifiers the way phaze's model registry does. **This is not a case where
   AudioMuse found CLAP superior for mood — it is a case where AudioMuse's architecture didn't
   have phaze's alternative, so it built one out of CLAP.** phaze does not have that gap.
3. **CLAP produces one embedding per track, not per window.** AudioMuse's docs state the audio
   model produces a 512-dimension embedding per track (§12.2) and describe it being stored keyed
   by the catalogue id — singular, one row per track. This is the same structural shape S1
   already priced under **B3**: a capability that emits one averaged vector per track cannot serve
   P3 (window-level, contiguous timeline) regardless of measured quality. AudioMuse's own choice
   confirms it: even the authors who built CLAP into their pipeline used it at track granularity,
   not window granularity.

### E2 — phaze's actual TensorFlow footprint, verified against the installed wheel (not assumed)

The epic's framing asks whether CLAP "can run on the same essentia/TF footprint." Answered by
inspecting the installed package directly, in this worktree's own `.venv`:

```console
$ find .venv/lib/python3.14/site-packages/essentia/.dylibs -name '*tensorflow*'
essentia/.dylibs/libtensorflow.2.20.0.dylib            362,285,616 bytes  (346 MiB)
essentia/.dylibs/libtensorflow_framework.2.20.0.dylib   33,295,360 bytes  ( 32 MiB)
```

```python
# src/phaze/services/analysis.py:146-150 — the only TF call sites in the codebase
classifier = es.TensorflowPredictMusiCNN(graphFilename=graph_path)
classifier = es.TensorflowPredictVGGish(graphFilename=graph_path)
classifier = es.TensorflowPredictEffnetDiscogs(graphFilename=graph_path)
```

**This is a private, C++-only, inference-only TensorFlow 2.20 runtime bundled inside the
`essentia-tensorflow` wheel**, exposed to Python exclusively through essentia's own
`es.TensorflowPredict*` operator classes, which load essentia-format frozen graphs for essentia's
own model zoo (musicnn/vggish/effnet). There is **no general-purpose `tensorflow` PyPI package**
anywhere in `pyproject.toml` or `uv.lock` — confirmed by grep, zero hits for `tensorflow`,
`torch`, `onnxruntime`, `scikit-learn`, or `umap` in either file. A PyTorch-native Hugging Face
CLAP checkpoint **cannot be loaded through this footprint**: `es.TensorflowPredict*` expects
essentia's own graph/op conventions, and no CLAP-to-essentia-frozen-graph export path exists
publicly. **"Run CLAP on the existing TF footprint" is not an available option** — the honest
options are a new PyTorch dependency (`torch` + `transformers`) or a new ONNX Runtime dependency
(`onnxruntime`) fed by a model *someone* has exported to ONNX, which for CLAP specifically means
phaze exporting and validating it itself (see E4).

### E3 — Candidate self-hosted CLAP models, weights licence, and size (verified via HF/PyPI/GitHub API, not AudioMuse source)

| Candidate | Weights licence (stated) | Size | Params (audio / text) | Runtime as shipped |
| --- | --- | --- | --- | --- |
| `laion/clap-htsat-unfused` (HF `transformers` `ClapModel`) | **Apache-2.0** — explicit `license: apache-2.0` on the model card, no additional restriction in the card body | `pytorch_model.bin` = 614,525,833 B (586 MiB), fp32, both towers combined | 27.5M / 124.6M (153.5M total — text tower is the larger one, confirming AudioMuse's own claim that the text side "is much larger") | PyTorch, via `transformers.ClapModel` / `ClapProcessor` |
| `laion/larger_clap_general`, `laion/larger_clap_music` | **Apache-2.0** — same card pattern | `pytorch_model.bin` = 776,444,665 B (740 MiB) each | not decomposed (larger backbone) | PyTorch, same API |
| `lukewys/laion_clap` (raw checkpoints behind the `laion-clap` PyPI package, e.g. `630k-audioset-fusion-best.pt`) | **CC0-1.0** (self-declared on the HF model card — public-domain-equivalent, even more permissive than Apache) | several hundred MB per checkpoint (not individually queried; the HF-format releases above are the practical download path) | n/a | `laion-clap` pip package pulls a much heavier, research-oriented dependency tree: `torch`, `torchlibrosa`, `webdataset`, `wandb`, `llvmlite`, `scikit-learn`, `pandas`, `h5py` — not a lean inference footprint |
| `microsoft/msclap` (Microsoft CLAP) | **Ambiguous — see finding below** | not queried (blocked on the licence question) | n/a | code repo is MIT; `msclap` pip package |
| `AudioMuse-AI-DCLAP` — AudioMuse's own distilled audio-tower checkpoint (its "original LAION CLAP text encoder" half is a separate, unmodified LAION artifact) | **AGPL-3.0 — blocking.** See finding below. | Unstated | Unstated | ONNX Runtime, both towers, split across two processes |

**Blocking-grade finding: Microsoft CLAP's weights licence is self-contradictory across its own
official mirrors.** The Hugging Face model card for `microsoft/msclap` declares `license: ms-pl`
(the Microsoft Public License, OSI-approved and MIT-compatible). The canonical Zenodo record for
the *same* pretrained checkpoint (linked from the `microsoft/CLAP` GitHub README itself:
"CLAP weights are downloaded automatically … but are also available at: Zenodo … or Hugging
Face") declares `cc-by-3.0-us` (Creative Commons Attribution 3.0) instead. These are materially
different licences — one is a permissive software licence with no attribution requirement beyond
notice preservation, the other is a content licence that imposes an attribution obligation and
was not designed for redistributing model weights. **Per the epic's binding rule ("licence of
model WEIGHTS is a first-class finding, not a footnote"), this is not resolved by picking the
more convenient of the two** — it is flagged here as an open blocker. Microsoft CLAP should not
be adopted without a direct clarification from Microsoft, and is excluded from the recommendation
below in favour of the LAION Apache-2.0 family, which carries a single, unambiguous, explicit
licence on every mirror checked.

**Second blocking-grade finding, surfaced by the sibling licence-compliance spike
(`phaze-ytgo.5`) and independently re-verified here rather than taken on trust:
AudioMuse's own distilled audio-tower checkpoint is published as its own separate, standalone
GitHub project, and that project's repository record declares it AGPL-3.0.**

```console
$ curl -s https://api.github.com/repos/NeptuneHub/AudioMuse-AI-DCLAP | jq '.license, .description'
{
  "key": "agpl-3.0",
  "name": "GNU Affero General Public License v3.0",
  "spdx_id": "AGPL-3.0"
}
"... a lightweight, high-speed distilled version of LAION CLAP, designed for fast and efficient
text-to-music search"
```

This is a repository-metadata fact about a standalone weights project (its licence field and
one-line description), not a read of AudioMuse-AI's own application source — squarely inside the
seal. Per this molecule's binding MIT constraint, **AGPL-3.0 weights are a blocking finding, not
a footnote**, exactly like the Microsoft CLAP ambiguity above, and for the same reason: copyleft
terms attaching to a model checkpoint constrain phaze's licensing posture the same way copyleft
code would. **A licence-clean substitute exists at no capability cost**: the LAION Apache-2.0
checkpoints in the table above (the *same* underlying CLAP architecture family this distilled
checkpoint derives from) provide the identical audio-embedding + text-embedding split AudioMuse's
own design uses, without the copyleft term. **This spike's recommendation is unchanged by this
finding — it confirms and hardens the recommendation already reached from the independent-mirror
search in E4 below (no reputable, licence-tagged DCLAP artifact could be found publicly at all):
price and adopt the LAION Apache-2.0 family; disregard `AudioMuse-AI-DCLAP` entirely.**

**Baseline comparison the epic asked for: how do these candidates compare to what phaze already
ships?** phaze's existing essentia classifiers (`discogs-effnet`, the MTG-trained `musicnn`
mood/danceability heads — S1 §E4) are themselves licensed **CC BY-NC-SA 4.0** by their publisher
(confirmed directly against the publisher's own models page, independent of AudioMuse). phaze
does not redistribute those weights — they are fetched at runtime, not vendored into a published
image — which is why the ShareAlike arm is not triggered today, but the licence still carries a
non-commercial clause that caps any commercial future for the models phaze **already** depends
on, and its attribution (BY) condition is outstanding. **Any new CLAP weight this spike
recommends is strictly better than phaze's own existing licence baseline**: Apache-2.0 carries no
non-commercial restriction and no ShareAlike obligation at all. Adopting `laion/clap-htsat-unfused`
would not lower phaze's licence posture — it would sit on cleaner terms than phaze's own current
essentia dependency already does.

### E4 — No usable ONNX CLAP artifact exists today; export is a project, not a download

Searched Hugging Face model search for pre-built ONNX CLAP exports: five hits
(`lquint/clap-htsat-unfused-onnx`, `javadhm/clap-onnx-int8`, `setweavr/clap-music-onnx`,
`muzaiten/clap-htsat-base-onnx`, `ZzxxH766/clap-encoder-onnx`), **every one at zero downloads**,
none carrying a licence tag, most recently uploaded by individual community accounts (one dated
2026-03, i.e. very recent and unproven). None is an actively-maintained, licence-clear artifact
suitable for a production dependency. **Converting `laion/clap-htsat-unfused` to ONNX yourself is
possible** (it is a standard `transformers` model, and `optimum`/`torch.onnx.export` support this
class of model) but is real engineering work — export, numerical-parity validation against the
PyTorch reference, and packaging — explicitly out of this spike's "small throwaway measurement"
scope. It also does not remove the licence question: an ONNX export inherits the source
checkpoint's licence unchanged (Apache-2.0 in, Apache-2.0 out), it only changes the runtime
dependency from `torch` to `onnxruntime`.

**Package-size comparison for the two runtime options** (PyPI JSON API, macOS arm64 wheels, this
Python version's tag where available):

| Package | Wheel size (compressed) | Installed size (measured in this spike's scratch venv) |
| --- | --- | --- |
| `torch` 2.13.0 | 111.2 MB | 476 MB |
| `onnxruntime` 1.28.0 | 19.1 MB | not installed/measured (no ONNX CLAP artifact to run against it — see above) |

`onnxruntime` is roughly 6x lighter to install than `torch`, and does not pull `sympy` / `networkx`
/ `mpmath` transitively (torch does) — a materially smaller and simpler runtime footprint *if* an
ONNX checkpoint exists to run on it. Today, for CLAP specifically, one does not, so this is a
future option to weigh only after someone does the export project, not a choice available now.

### E5 — Measured per-file CLAP inference cost (laion/clap-htsat-unfused, this laptop's CPU, real audio)

Isolated scratch venv (`uv venv`, Python 3.12, CPU-only torch build — never touching phaze's own
`.venv`), model + processor downloaded fresh from Hugging Face:

```console
model+processor load (incl. first-run download/cache): 2.45–2.56s (warm HF cache)
total params: 153,492,890   audio_tower: 27,534,488   text_tower: 124,645,632

# 30-second clip cut with ffmpeg from a real purchased track (48kHz mono, per CLAP's own
# preprocessing requirement), NOT a synthetic tone:
audio embed (30s clip), 5 runs: mean=0.059-0.068s  min=0.058-0.061s  max=0.061-0.075s

# The SAME model on the file's full 7m21s (441s) duration:
input_features shape after processor: {'input_features': (1, 1, 1001, 64), 'is_longer': (1, 1)}
audio embed (7m21s full track), 5 runs: mean=0.170s  min=0.064s  max=0.220s

# Free-text query embedding (2-4 word queries, per AudioMuse's own documented "sweet spot"):
text embed (short query), 3 runs: mean=0.030-0.042s
```

**Two findings from this measurement:**

1. **The audio-tower cost is essentially flat regardless of input duration** — 0.06s for a 30s
   clip vs a min of 0.064s / mean 0.17s for a full 7m21s track — because the HF `ClapProcessor`
   pads/crops the mel-spectrogram to a **fixed `(1, 1, 1001, 64)` shape internally**
   (≈10 seconds of audio at this model's spectrogram settings) regardless of clip length. **CLAP's
   audio embedding, as shipped, is a single fixed ~10-second window per call — shorter than
   phaze's own fine tier (30s) and much shorter than its coarse tier (180s).** Getting
   timeline-spanning coverage from CLAP over a multi-hour set would require the caller to slide
   this ~10s window across the whole file and call the model repeatedly — an O(duration) cost
   this spike did not price further, since E1 already shows AudioMuse itself doesn't do this (one
   embedding per track).
2. **Extrapolated batch cost at archive scale**: at ~0.07s/file (30s-clip case, the realistic
   per-file cost since a fixed ~10s window is all that's embedded regardless of file length),
   200,000 files ≈ **3.9 CPU-hours of pure model inference** on this laptop's CPU, excluding
   audio I/O, resampling, and queueing overhead. This is a one-time, per-file **analysis-time**
   cost (comparable in shape to phaze's existing essentia analysis pass), not a per-query cost —
   the per-**query** cost that actually matters for interactive free-text search is the text-embed
   number (~30-40ms), which is well inside the P2 interactivity budget S1 already set
   (p95 ≤ 200ms for k=50 at N=200,000, per S1's requirements table) *before* accounting for the
   IVF/ANN search itself (out of this spike's scope — that's `phaze-ytgo.6`).

**Provenance, per the epic's measurement rule:** n=5 audio runs / n=3 text runs, one file
(`Marco_V_-_GODD_(T78_Extended_Remix).mp3`, a purchased Beatport track present on this machine),
one laptop's CPU, no GPU. This is a real, cited measurement, not a fabricated number — but it is
narrow (one file, one machine) and should not be read as a validated production SLA.

### E6 — Measured UMAP-vs-PCA cost at S1's 200,000-point archive scale

Isolated scratch venv, Python 3.14 (matching phaze's pin), synthetic 200,000 × 200 float32 matrix
(S1's own worst-case archive-scale figure and AudioMuse's own embedding dimension):

```console
sklearn PCA fit_transform,        N=200,000  D=200:   0.11s
pure-numpy PCA (np.linalg.svd),   N=200,000  D=200:   4.58s   (zero new dependencies)
pure-numpy PCA (np.linalg.svd),   N=200,000  D=11:    0.16s   (phaze's own EFB score vector)
umap-learn UMAP fit_transform,    N=200,000  D=200: 189.41s   (single-threaded, random_state
                                                                pinned for reproducibility)
sklearn PCA .transform(),         1,000 NEW points:   0.0004s  (exact, not an approximation)
```

Incremental-add cost, measured separately on a smaller (50,000-point) fit to keep the run
tractable, `random_state` pinned:

```console
umap-learn UMAP fit,              N=50,000   D=200:   40.82s
umap-learn UMAP .transform(),     1 new point:         7.955s  (7,955 ms — first call)
umap-learn UMAP .transform(),     10 new points:       0.016s  (1.65 ms/point)
umap-learn UMAP .transform(),     100 new points:      0.127s  (1.27 ms/point)
```

**Three findings:**

1. **PCA is 40-1,700x faster to fit than UMAP at this scale**, and does not degrade with data
   drift the way a stale UMAP fit does — a PCA refit is cheap enough (0.11-4.6s) to run on every
   batch cycle if desired. UMAP's 189s single-fit cost, run on the whole archive, is a real
   multi-minute batch job, not a per-request cost, but it is a real recurring cost every time the
   projection needs re-fitting to stay faithful to a growing/drifting archive.
2. **UMAP's out-of-sample `.transform()` pays a large one-time warm-up cost** — the very first
   call to `.transform()` after a fit took **~8 seconds for a single new point**, while the 10th
   through 100th calls in the same process cost ~1-2ms/point. This is consistent with
   `umap-learn`'s use of `numba` (an LLVM JIT compiler — see E7) needing to JIT-compile its
   nearest-neighbour kernels on first use. **Whether this matters for "staying current as the
   archive grows" depends on phaze's execution model**: if projection updates run inside a
   long-lived worker process (SAQ workers are typically long-running, pulling many jobs), the
   ~8s warm-up is paid once per worker lifetime and amortizes to near-zero; if each update runs in
   a fresh short-lived process, ~8s is paid on every single new file — this spike did not verify
   which shape phaze's SAQ deployment uses and flags it as the concrete follow-up question rather
   than assuming either. **PCA's `.transform()` has no such warm-up at all** (a pure matrix
   multiply, 0.0004s for 1,000 points, no JIT, no approximation) regardless of process lifetime.
3. **`umap-learn`'s own documentation position matches AudioMuse's practice, not a strawman**:
   AudioMuse's own docs (E1, §9.2) describe **exactly** this two-tier design already — a batch
   step, run at index-rebuild time, that computes and stores 2D coordinates for every embedding,
   versus a separate on-the-fly step that computes coordinates only for songs that do not have
   them yet (the incremental path) — and separately report to the API caller which of the two
   produced the answer (a stored batch-fitted projection, or an on-the-fly fallback) (§9.3),
   i.e. **AudioMuse's own authors distinguish the batch-fitted UMAP projection from an
   on-the-fly fallback as two different code paths with two different guarantees**, exactly the
   distinction this spike's timing numbers explain the cost of.

### E7 — `umap-learn`'s dependency weight, licence, and Python-3.14 compatibility

```console
$ curl pypi.org/pypi/umap-learn/json  →  license: BSD
  requires_dist: numpy>=1.23, scipy>=1.3.1, scikit-learn>=1.6, numba>=0.51.2, pynndescent>=0.5, tqdm
$ curl pypi.org/pypi/scikit-learn/json → license_expression: BSD-3-Clause
$ curl pypi.org/pypi/numba/json → requires_python >=3.10, cp314 wheels published (0.66.0)
$ uv venv --python 3.14 .venv && uv pip install umap-learn scikit-learn
  Resolved 11 packages … Installed 11 packages: joblib, llvmlite, narwhals, numba, numpy,
  pynndescent, scikit-learn, scipy, threadpoolctl, tqdm, umap-learn
```

All BSD/MIT-family, no licence blocker — but the dependency **count** is real: `umap-learn` pulls
in five additional packages transitively (`scikit-learn`, `numba`, `llvmlite`, `pynndescent`,
`tqdm`), of which `numba` is an LLVM-based JIT compiler — a class of dependency phaze has zero of
today, and one with a real history of platform/Python-version fragility across the ecosystem in
general (this spike confirms it *does* resolve cleanly for 3.14 today, per the successful install
above — not a blocker as of this date, but a fragility class, not merely a licence one). PCA needs
either **zero** new dependencies (pure `numpy`, already a direct phaze dependency, 4.6s at
200k×200) or **one** light, mature, non-JIT, BSD-3-Clause dependency (`scikit-learn`, for the
much faster randomized-SVD `PCA` implementation, 0.11s at the same scale).

______________________________________________________________________

## Verdict

### CLAP

**Candidate models, licence-screened**: `laion/clap-htsat-unfused` (Apache-2.0, 586 MiB,
153M params) is the smallest, cleanest, most standard option; `laion/larger_clap_general` /
`larger_clap_music` (Apache-2.0, 740 MiB) trade size for quality on general/music-domain audio;
`lukewys/laion_clap`'s raw checkpoints (CC0-1.0, even more permissive) exist but ship behind a
much heavier research-oriented Python package. **Two candidates are excluded outright**:
Microsoft CLAP, pending resolution of its self-contradictory weights licence (`ms-pl` on Hugging
Face vs `cc-by-3.0-us` on the canonical Zenodo mirror the code repo itself links to); and
`AudioMuse-AI-DCLAP`, AudioMuse's own distilled audio-tower checkpoint, whose standalone GitHub
repository declares it **AGPL-3.0** (E3) — copyleft weights are a blocking finding under this
molecule's binding MIT constraint, exactly as blocking as copyleft code would be. Both exclusions
are first-class blockers per the epic's rule, not footnotes. **The substitution costs nothing**:
the LAION Apache-2.0 family provides the same audio-embedding-plus-text-embedding split
AudioMuse's own design uses, and — per the baseline comparison in E3 — sits on strictly cleaner
licence terms than the CC BY-NC-SA 4.0 essentia weights phaze already depends on today.

**The bead's hypothesis is confirmed directly from AudioMuse's own published docs (E1)**: CLAP's
mood/"other features" role in AudioMuse exists because AudioMuse's own embedding model doesn't
ship purpose-built mood classifiers the way phaze already does (S1 §E4 — six binary
essentia classifiers, already integrated, already in the `features` JSONB). Re-deriving mood via
CLAP text-embedding cosine similarity would be a strictly more expensive, less direct route to a
label phaze already computes more cheaply and more precisely. **The genuinely new capability is
free-text/natural-language query — nothing else in phaze's stack, existing or planned by any
sibling spike, can do this**, because essentia's classifiers are closed-vocabulary predictors, not
open natural-language matchers.

**Runtime**: confirmed (E2) that phaze's existing "essentia/TF footprint" is a private,
essentia-only inference runtime that cannot host a CLAP checkpoint. Adding CLAP means adding
`torch` + `transformers` (~600 MB combined install, the standard supported path today) — no
production-ready ONNX alternative exists yet for CLAP specifically (E4); building one is a real
engineering project, not a config change.

**Cost**: measured (E5) at ≈0.07s/file audio-embed and ≈0.03-0.04s/query text-embed on this
laptop's CPU — cheap per-unit, and the per-file cost is a one-time analysis-time cost
(≈3.9 CPU-hours for a 200,000-file archive), not a per-query cost. The interactive query path
(text-embed only) comfortably fits inside S1's P2 latency budget on its own.

### Projection (UMAP vs PCA)

**PCA suffices for P4 at archive scale.** Measured (E6) 40-1,700x faster fit than UMAP at
200,000 × 200 (0.11-4.6s vs 189.4s), an exact and near-free (0.0004s/1,000 points) out-of-sample
update with zero-to-one new dependencies, versus UMAP's five-package dependency tree including an
LLVM JIT compiler (E7) and a measured, real, non-trivial incremental-update warm-up cost
(E6: ~8s on first `.transform()` call, amortizing only if the calling process stays warm). UMAP's
real advantage — better separation of non-linear cluster structure for the visual map — is a
genuine, distinct benefit this spike did not attempt to quantify (would need real embeddings, out
of scope), but it is a benefit stacked *on top of* a purpose (P4) S1 already scores as the
molecule's strongest "not worth it" candidate. Recommend: **if P4 ships a 2D map at all, start
with PCA** (zero-to-one new dependencies, fits the EFB vector phaze already stores in 0.16s, or a
future embedding in 0.11-4.6s); treat UMAP as a follow-on, separately-scoped, separately-justified
upgrade only if a future GO on P4 specifically wants better visual cluster separation than a
linear projection can give, not as part of this molecule's base recommendation.

**Refresh story**: neither projection is "free" to keep current, but they differ by orders of
magnitude. PCA: refit the whole archive in 0.11-4.6s at 200k scale (S1's ceiling), or apply the
existing fitted projection to new points in under a millisecond, exactly and without a warm-up
tax. UMAP: a full refit is a multi-minute batch job (189s at 200k×200, growing with N and D);
`.transform()` for new points is cheap per-point (~1-2ms) but only after paying a real ~8-second
JIT warm-up whose amortization depends on whether the calling process is long-lived — a concrete
open question this spike flags rather than assumes.

### Separability

**Yes — cleanly separable, on evidence from AudioMuse's own architecture, not just inference.**
AudioMuse's own published docs describe a master on/off switch for the whole CLAP subsystem —
with it off, no CLAP embedding is produced during analysis and the free-text search page is
hidden — and describe the stored other-features vector falling back to zeros rather than failing
when CLAP is absent — i.e. the system this epic is studying was **already built by its own
authors to make CLAP fully optional**, with no other feature depending on it. For phaze
specifically: an embedding-driven "more like
this" browsing capability (if `phaze-ytgo.2`/`.4`/`.6` land a GO) needs only a 200-d embedding and
an ANN index — it does **not** need CLAP at all, since CLAP's audio tower would only be *another*
source of the same embedding shape, at a heavier dependency and licence-review cost (see
Per-purpose impact, P1/P2/P4 below). CLAP's *only* non-redundant contribution is free-text query.
**A GO on audio-similarity P2 (or P1/P3/P4) can ship with zero `torch` dependency; free-text
search (`P2`'s text half) can be deferred indefinitely as its own separately-scoped bead**, exactly
as the bead requests — it does not need to be resolved before, or block, any other purpose's
verdict.

### Per-purpose impact (S1 rubric, phaze-ytgo.1) — CLAP

Per S1's rule 1 ("score each purpose independently, never average") and because the bead
explicitly asks CLAP's two roles (mood-adjacent vs free-text) to be priced separately, P2's row is
split into its two sub-capabilities rather than collapsed into one verdict.

| Purpose | Verdict | Granularity delivered | vs EFB | Evidence |
| --- | --- | --- | --- | --- |
| P1 dedup + rename | `REDUNDANT` | track | n-m | CLAP's audio embedding is structurally the same *shape* of signal (one averaged vector/track) that `phaze-ytgo.2`/`.6` are already independently investigating from essentia directly, at lower dependency/licence cost. If an embedding helps P1 at all, the cheaper non-CLAP path should be used first; CLAP adds nothing P1-specific on top. (E1 finding 3, E3) |
| P2 discovery/playlists — **audio-similarity sub-capability** | `REDUNDANT` | track | n-m | Same reasoning as P1: CLAP's audio tower is not a better source of "tracks like this" than a purpose-evaluated embedding from `phaze-ytgo.2`, and costs a `torch` dependency + a licence review the alternative may not need. (E3, E5) |
| P2 discovery/playlists — **free-text query sub-capability** | `SERVES` | track (per-query, not per-file) | better (EFB has no natural-language query path at all — no baseline to beat, this is a pure capability gap EFB cannot fill by any recombination of its columns) | Measured: 0.03-0.04s/query text-embed (E5), well inside S1's p95≤200ms P2 budget; licence-clear candidate exists (`laion/clap-htsat-unfused`, Apache-2.0, E3) |
| P3 set/tracklist | `BLOCKED` | track (one embedding per track, per AudioMuse's own stated design) | n-a | AudioMuse's own docs confirm CLAP produces "a 512-dimension embedding **per track**" (E1) — the identical structural block S1 already priced as B3. Sliding CLAP's ~10s window across a multi-hour set to get contiguous coverage would reproduce S1's B2 O(1)-cost contradiction and was not measured here (out of scope) |
| P4 archive QA | `REDUNDANT` | track | worse (adds a torch dependency to reproduce a mood signal phaze's own essentia classifiers already deliver directly, per E1 finding 2) | Matches S1's own P4 finding — P4's value is already reachable via EFB; CLAP does not change that (E1) |

### Per-purpose impact (S1 rubric, phaze-ytgo.1) — Projection (UMAP/PCA)

| Purpose | Verdict | Granularity delivered | vs EFB | Evidence |
| --- | --- | --- | --- | --- |
| P1 dedup + rename | `BLOCKED` | n/a | n/a | No P1 workflow consumes a 2D projection — S1's own surface audit (E5 of S1) found no map/browse surface outside P4. Structurally not applicable, not a quality gap |
| P2 discovery/playlists | `BLOCKED` | n/a | n/a | Same reasoning — P2's `alchemy`-style projection use in AudioMuse (E1: "projected to 2D … so the frontend can plot them") is a plotting aid for *that* feature's own UI, not something phaze's P2 scope (browse/cluster/playlist) requires; no phaze P2 surface plan needs 2D coordinates |
| P3 set/tracklist | `BLOCKED` | n/a | n/a | No timeline/tracklist surface plots 2D coordinates; not applicable |
| P4 archive QA | `SERVES-WITH-CAVEAT` (PCA) / `SERVES-WITH-CAVEAT` (UMAP) | track | same (PCA: a linear projection of a vector phaze already stores or S2/S6 may add — matches S1's own framing that "even the 2-D map is a projection of a vector phaze already stores") / unmeasured-quality-delta (UMAP: likely better cluster separation, not measured here) | PCA caveat: linear only, may under-separate non-linear cluster structure a human eyeballing the map would notice. UMAP caveat: real, measured dependency and refresh-cost premium (E6, E7) on a purpose S1 already scores as the molecule's weakest case for "worth it" at all |

______________________________________________________________________

## Recommendation

1. **CLAP: do not adopt for mood/other-features (P1, P2-audio-similarity, P4) — `REDUNDANT`
   against phaze's own existing essentia classifiers and against the cheaper embedding path
   `phaze-ytgo.2`/`.6` are independently pricing.** If `phaze-ytgo.7` (D1) wants free-text search
   specifically (`P2`'s genuinely new sub-capability), the licence-clear path is
   `laion/clap-htsat-unfused` (Apache-2.0, 586 MiB, `transformers`/`torch`), accepting a real
   ~600 MB dependency addition with no lighter ONNX alternative available today. **Do not consider
   Microsoft CLAP** until its self-contradictory weights licence (`ms-pl` vs `cc-by-3.0-us`) is
   resolved directly with the source, **and disregard `AudioMuse-AI-DCLAP` entirely** — its
   standalone repository declares it AGPL-3.0, a blocking copyleft-weights finding under this
   molecule's binding MIT constraint, with the Apache-2.0 LAION family substituting at no
   capability cost and on strictly cleaner terms than phaze's own existing CC BY-NC-SA 4.0
   essentia weights.
2. **Free-text search should be filed and reviewed as its own separately-scoped bead, independent
   of every other purpose's verdict**, per the Separability finding — a GO or NO-GO on it should
   never gate P1/P3/P4 or the audio-similarity half of P2.
3. **Projection: recommend PCA as the P4 default** if P4 proceeds at all (S1 already scores P4 as
   the molecule's weakest "worth it" case) — zero-to-one new dependencies, 40-1,700x cheaper to
   (re)fit at archive scale, and an exact, near-instant out-of-sample update with no JIT warm-up.
   Treat `umap-learn` as a distinct, separately-priced upgrade decision for later, only if a
   future explicit GO on P4 wants better visual cluster separation than PCA's linear projection
   provides badly enough to accept its dependency and refresh-cost premium.
4. **`phaze-ytgo.7` (D1): read CLAP and Projection as fully decoupled from `phaze-ytgo.2`'s
   embeddings finding.** Whatever `phaze-ytgo.2` concludes about essentia-derived embeddings for
   P1/P2/P4, it changes nothing about CLAP's free-text-only value proposition or about
   PCA-vs-UMAP's cost profile — those questions stand on their own regardless of where the
   underlying 200-d vector comes from.

### Open, and deliberately left unmeasured

| # | Question | Why it is open | Who should close it |
| - | -------- | -------------- | -------------------- |
| O1 | Whether phaze's SAQ workers are long-lived (amortizing UMAP's ~8s `.transform()` JIT warm-up across many files) or spin up fresh per job (paying it every time). | Not verified in this spike — flagged rather than assumed either way (E6 finding 2). | Anyone with the SAQ worker deployment config; irrelevant if D1 selects PCA. |
| O2 | Real CLAP audio-embedding cost distribution across phaze's actual archive genre/duration mix, and on phaze's real runtime hardware (not this dev laptop). | This spike measured one file on one laptop's CPU (E5); it is a real number, not a fabricated one, but it is narrow. | The implementation molecule, if P2 free-text search gets a GO. |
| O3 | Numerical/qualitative parity of a self-exported ONNX build of `laion/clap-htsat-unfused` against the PyTorch reference, and whether it materially changes the ~600 MB dependency story. | Building and validating that export is real engineering work, explicitly out of this spike's "small throwaway measurement" scope. | The implementation molecule, if P2 free-text search gets a GO and the `torch` footprint is judged too heavy. |
| O4 | Direct clarification with Microsoft on `microsoft/msclap`'s actual weights licence (`ms-pl` vs `cc-by-3.0-us`). | Two official-looking mirrors disagree; neither this spike nor D1 should resolve a licence ambiguity by picking the friendlier answer. | Whoever owns AGPL/licence compliance for this epic (`phaze-ytgo.5`), if Microsoft CLAP is ever reconsidered. |
| O5 | Real cluster-separation quality delta of UMAP over PCA on phaze's actual (eventual) embeddings, for P4 specifically. | Needs real embeddings, which don't exist yet; this spike only measured wall-clock cost, not projection quality. | Whoever scopes a future PCA-vs-UMAP upgrade decision for P4, if P4 ever gets an explicit GO. |
