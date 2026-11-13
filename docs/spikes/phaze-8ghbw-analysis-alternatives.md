# phaze-8ghbw — faster alternatives for music analysis: a literature and licence survey

- **Bead:** `phaze-8ghbw` (spike — research faster or better alternatives to the analysis stack:
  papers, models, libraries, licences)
- **Parent epic:** `phaze-0toy3` (where analysis CPU goes and how to cut it). Sibling spikes
  `phaze-fzubr` (profiling) and `phaze-phjwi` (TensorFlow thread settings) are not duplicated here.
  Decision bead `phaze-uv8s3` consumes this document
- **Date:** 2026-11-13
- **Tree:** branch `wt/bead/issue/phaze-8ghbw`, forked off `main` at `c3718544`
- **Status:** desk research only. **No product code changed, nothing was measured, and phaze
  production was not accessed.** Every accuracy or speed figure below that is not from a phaze
  record is a **literature proxy**, not a measurement on phaze's corpus (CLAUDE.md
  "Acceptance criteria, attribution, and verification fidelity", rule 3)

______________________________________________________________________

## Question

Is there a model, library or runtime that would make phaze's per-window analysis materially
cheaper in CPU without an unacceptable accuracy or licence cost? Specifically:

1. Is there a cheaper or better replacement for the coarse-tier classifiers (eleven musicnn
   characteristic graphs plus the Discogs EfficientNet genre graph)? Can **one shared embedding**
   feed every classification head?
1. Is there a faster or more accurate beat/BPM tracker than `RhythmExtractor2013`, or a key
   estimator better than `KeyExtractor(profileType="edma")`?
1. Do ONNX Runtime, OpenVINO, int8 quantisation, batching or TFLite cut CPU on this workload?
1. What licence does each candidate carry, compared with the CC BY-NC-SA 4.0 Essentia models phaze
   runs today?

## Verdict in one paragraph

**The cheapest large lever is not a new model family. It is sharing one backbone pass across all
eleven characteristic heads, using classifier heads that MTG already publishes, under the licence
phaze already accepts.** Today each coarse window runs **twelve full backbones**: eleven separate
3,239,548-byte musicnn graphs (one per characteristic) plus `discogs-effnet-bs64-1`. MTG's own
model catalogue lists a `*-discogs-effnet-1` head and a `*-msd-musicnn-1` head **for every one of
phaze's eleven characteristics**. Those heads take a precomputed embedding, not audio, via
`TensorflowPredict2D`. phaze already computes the EfficientNet embedding for genre, so the effnet
route would drop eleven backbone passes per coarse window to **zero extra backbones**, plus eleven
small head evaluations. The only phaze measurement of this lever's shape is the **33 → 11 head cut,
which made analysis 2.025× and 2.108× faster** on the eight-file evaluation sample. That is a
neighbour of this change, not this change, so the saving here is **inferred and unmeasured**. The
accuracy cost is also unmeasured. The heads are different trained artefacts, and the last head swap
changed **65 of 208** coarse mood labels. **Everything else surveyed ranks lower.** On the fine
tier, the 10-hour profile puts **97.7%** of fine time in decode, so no beat or key tracker can save
much CPU there. Neural trackers (Beat This!, madmom, BeatNet, All-In-One) add a PyTorch or legacy
dependency and almost certainly add CPU. Runtime changes were already measured by `phaze-mqq5`:
ONNX Runtime **doubled** wall clock inside `analyze_file`, and int8 **flipped the top-1 genre
label**. Their remaining value is the arm64 image, not CPU. Bigger embeddings (MAEST, MERT, CLAP,
PaSST) promise accuracy, not speed, and none ships heads for phaze's characteristics.

______________________________________________________________________

## Method

| | |
| --- | --- |
| **Code grounding** | `src/phaze/services/analysis.py`, `src/phaze/services/analysis_models.py`, `src/phaze/scripts/download_models.py`, `Dockerfile.agent-arm64`, read at `c3718544` |
| **phaze records** | `docs/essentia-analysis.md`, `docs/design/0007-windowed-analysis.md`, `docs/analysis-evaluation.md`, and spikes `phaze-zaf2l`, `phaze-mqq5`, `phaze-ytgo.2`, `phaze-ytgo.3`, `phaze-28883` (all under `docs/spikes/`) |
| **Lacuna MCP** (papers) | `search_lacuna` and then `get_paper` on: MAEST, Discogs-EffNet contrastive pre-training, musicnn, Essentia TF models, EfficientAT (MobileNet distillation), DyMN, PaSST, MERT, CLAP, madmom, BeatNet, All-In-One, Beat Transformer, recent beat-tracking work since 2024, CNN key estimation, and post-training quantisation / ONNX / OpenVINO for audio |
| **Web** (licences, model cards, repos) | raw GitHub files for `MTG/essentia` (model catalogue `doc/sphinxdoc/models.rst`, example scripts, `key.cpp`, `rhythmextractor2013.cpp`, `COPYING.txt`), `CPJKU/beat_this`, `CPJKU/madmom`, `mjhydri/BeatNet`, `mir-aidj/all-in-one`, `fschmid56/EfficientAT`, `palonso/MAEST`, `yizhilll/MERT`, `kkoutini/PaSST`; PyPI JSON for `madmom`, `beat-this`, `BeatNet`, `librosa`; web search for two Hugging Face model-card licence tags |
| **Not reachable** | `essentia.upf.edu`, `huggingface.co`, `arxiv.org`, `zenodo.org` and the ISMIR archive were blocked from this session. Per-head evaluation metadata (the `.json` sidecars on `essentia.upf.edu`) and the Beat This! and MAEST PDFs could not be read, so their numbers appear below **only** where a reachable source quoted them, and each such use is marked |

**Lacuna coverage.** Lacuna returned papers for every embedding/tagger candidate, for madmom,
BeatNet, All-In-One and CNN key estimation. It had **nothing** for *Beat This!* (ISMIR 2024): two
targeted searches and a 2024+ "beat tracking" sweep did not surface it. It also had **nothing
audio-specific** on ONNX Runtime, OpenVINO or int8 quantisation for audio CNNs on CPU; the top hits
were vision and LLM quantisation. Lacuna does not cover licences, so **every licence below comes from
a repository file, the Essentia catalogue, or a model-card tag**.

______________________________________________________________________

## Current stack (grounded)

### What runs, per tier

| tier | window | rate | what runs per window | source |
| --- | --- | --- | --- | --- |
| **fine** | 30 s (`_DEFAULT_FINE_WINDOW_SEC`), trailing window dropped below 15 s (`_DEFAULT_FINE_MIN_SEC`) | 44,100 Hz (`_FINE_SAMPLE_RATE`) | `es.RhythmExtractor2013(method="multifeature")` → BPM, gated on `confidence > 0.0`; `es.KeyExtractor(profileType="edma")` → `"<key> <scale>"`, gated on `strength > 0.0` | `analysis.py` (`_analyze_fine_windows`, `_measure_fine_window`) |
| **coarse** | 180 s (`_DEFAULT_COARSE_WINDOW_SEC`) | 16,000 Hz (`_COARSE_SAMPLE_RATE`) | model-major sweep: **12 graphs** by default — 11 `musicnn_msd` characteristic graphs (`TensorflowPredictMusiCNN`) + `discogs-effnet-bs64-1` (`TensorflowPredictEffnetDiscogs`, 400 Discogs styles) | `analysis_models.py` (`_select_model_sets`, `GENRE_MODEL`) |

- **The eleven characteristics:** mood_acoustic, mood_electronic, mood_aggressive, mood_relaxed,
  mood_happy, mood_sad, mood_party, danceability, gender, tonal_atonal, voice_instrumental. The
  graphs are `*-musicnn-msd-2` except `voice_instrumental-musicnn-msd-1`. Rollback
  (`PHAZE_ANALYSIS_MODEL_VARIANTS=all`) restores 33 heads (adding `musicnn_mtt` and `vggish`).
- **Each characteristic graph is a whole network, not a head.** The download manifest pins every
  musicnn characteristic graph at **3,239,548 bytes** (`voice_instrumental-musicnn-msd-1`:
  3,239,625), and `phaze-ytgo.2` found the 200-d `model/dense/BiasAdd` embedding layer *inside*
  each `*-musicnn-msd-2.pb`. Each characteristic therefore re-runs the musicnn front end over
  the same 180 s of audio. The effnet graph is **18,366,619 bytes**, and the 12-graph set totals
  **54,048,897 bytes** (`phaze-28883` §1a).
- **Patch rates per 180 s coarse window** (`phaze-ytgo.2`): effnet **180 patches** (1.00 Hz),
  musicnn **119 patches** (0.66 Hz).
- **Chunking (D-07):** 60 fine / 30 coarse windows per chunk (`_FINE_CHUNK_WINDOWS`,
  `_COARSE_CHUNK_WINDOWS`). Exactly one `TensorflowPredict*` graph is resident at a time.
  TF `batchSize` is 32 (`_DEFAULT_TF_BATCH_SIZE`), except the effnet graph, whose input
  placeholder is fixed at `[64, 128, 96]`.
- **Per-window interface.** A replacement must fit `_measure_fine_window` (decoded 44.1 kHz mono
  buffer in, `FineWindow(bpm, musical_key)` out) or the coarse model-major loop (decoded 16 kHz
  buffer in, `[patches, classes]` activations out, averaged over `axis=0`). The required semantic
  outputs and their consumers are listed in `docs/analysis-evaluation.md` ("Result inventory and
  consumers"), including the 11 fixed coarse scores and the `set_projection_writer` adapter
  caveat.

### Deployment constraints

- **CPU-only.** The analysis backend `host-compute` has 8 logical CPUs, runs `cap = 4` concurrent
  pods, and sets `memory_limit 4Gi` per pod (`phaze-zaf2l` §1).
- **arm64 agent pinned to Python 3.13.** `Dockerfile.agent-arm64` builds from
  `python:3.13-slim-trixie` and builds essentia from source against the `tensorflow` aarch64
  wheel. `tensorflow` publishes no cp314 aarch64 wheel; `onnxruntime` and `ai-edge-litert` do
  (`phaze-mqq5` §6, verified against PyPI 2026-08-06).
- **Peak RSS must stay duration-independent** (CLAUDE.md "Analysis is exhaustive, chunk-bounded…").
  A swap must not reintroduce whole-tier buffers.

### What is already measured about where CPU goes

| measurement | figure | source |
| --- | --- | --- |
| production per-file wall at `cap = 4` | **1.4951×** file duration (7,119.473 s for a 4,761.835 s file) | `phaze-zaf2l` verdict |
| tier split, 3,578.964 s file | fine **7.9%**, coarse + model sweep **92.1%** | `phaze-zaf2l` §3b forward note |
| tier split, 36,182.359 s file | fine **49.3%**, coarse + model sweep **50.7%** | same |
| decode share within tier (10 h file) | fine **97.7%** decode; coarse **18.4%** decode | same |
| 33 → 11 characteristic heads (34 → 12 graphs), 8-file sample, 4-vCPU evaluation host | incumbent 195.3 / 209.0 min → 96.4 / 92.7 min, **2.025× / 2.108× faster**; **65 of 208** coarse mood labels changed | `docs/essentia-analysis.md` |
| 34-graph sweep, one 180 s window, TF batch 32 | **86.35 s** wall | `phaze-mqq5` §3 |
| effnet embedding throughput (M1 Pro, contended, CPU only) | **0.168–0.173 s per audio-minute** | `phaze-ytgo.2` E6 |

**What no phaze record contains:** a **per-graph** CPU split, meaning effnet vs one musicnn graph
on the same host and window. The 0.17 s/audio-minute effnet figure and the 86.35 s/34-graph figure
come from different hosts and different harnesses, so they cannot be divided into a per-graph
ratio. **This is the first number the profiling spike (`phaze-fzubr`) should produce** before
`phaze-uv8s3` decides anything below.

______________________________________________________________________

## Evidence

### A. Coarse tier — embeddings and classifier heads

#### A1. Essentia classification heads on a shared embedding (Discogs-EffNet or MSD-musicnn)

- **Source:** the Essentia model catalogue, `MTG/essentia` `doc/sphinxdoc/models.rst`, and its
  example scripts under `src/examples/python/models/scripts/classification-heads/`. Paper
  lineage, via Lacuna: *Pre-Training Strategies Using Contrastive Learning and Playlist Information
  for Music Classification and Similarity*
  (<https://lacuna.tiptreesystems.com/work/pre-training-strategies-using-contrastive-learning/version/art_d9916c48824e4599842f19ea0aa9341a>)
  and *Tensorflow Audio Models in Essentia*
  (<https://lacuna.tiptreesystems.com/work/tensorflow-audio-models-in-essentia>).
- **What exists.** The catalogue lists, verbatim, `<name>-discogs-effnet-1.pb` **and**
  `<name>-msd-musicnn-1.pb` for all eleven phaze characteristics: `mood_acoustic`,
  `mood_aggressive`, `mood_electronic`, `mood_happy`, `mood_party`, `mood_relaxed`, `mood_sad`,
  `danceability`, `gender`, `tonal_atonal`, `voice_instrumental`.
- **How they run** (MTG's own example, `mood_aggressive-discogs-effnet-1_predictions.py`):

  ```python
  embedding_model = TensorflowPredictEffnetDiscogs(graphFilename="discogs-effnet-bs64-1.pb", output="PartitionedCall:1")
  embeddings = embedding_model(audio)
  model = TensorflowPredict2D(graphFilename="mood_aggressive-discogs-effnet-1.pb", output="model/Softmax")
  predictions = model(embeddings)
  ```

  The musicnn variant is identical in shape: `TensorflowPredictMusiCNN(graphFilename="msd-musicnn-1.pb",
  output="model/dense/BiasAdd")`, then `TensorflowPredict2D` on the head.
- **Licence:** "All the models created by the MTG are licensed under CC BY-NC-SA 4.0" (catalogue,
  line 11). This is **the same licence** as phaze's current graphs: no change.
- **Reported accuracy:** each head's metrics live in its `.json` sidecar on `essentia.upf.edu`,
  which was **not reachable**. **No accuracy figure is claimed here.**
- **Cost:** *inferred, not read.* The backbone runs once per coarse window instead of eleven times.
  The heads consume a `[patches, 1280]` (effnet) or `[patches, 200]` (musicnn) tensor instead of
  audio. phaze **already** runs `discogs-effnet-bs64-1` for genre. `phaze-ytgo.2` measured reading
  the `PartitionedCall:1` embedding off that existing pass at **+1.6%** wall clock. So the effnet
  route adds **zero** backbone passes, and the musicnn route adds one `msd-musicnn-1` pass in
  place of eleven.
- **Per-window fit:** a direct fit for the coarse model-major loop. The loop builds one graph,
  runs it across every window of the chunk, and releases it. Under this option the effnet graph
  emits two outputs (`PartitionedCall:0` for genre, `PartitionedCall:1` for the embedding). The
  per-window embeddings, about **180 × 1280 float32 ≈ 0.92 MB** per window (arithmetic), are held
  across the chunk's 30 windows (**≈ 27.6 MB**), and then each `TensorflowPredict2D` head is swept
  over them. This leaves peak-RSS chunk-bounded (D-07).
- **CPU/arm64 fit:** same essentia `TensorflowPredict*` family, so there is no new dependency and
  no change to the arm64 position.
- **Accuracy risk — the real cost, unmeasured.** These are **different trained artefacts** from
  `*-musicnn-msd-2`, so outputs will move. Four specific traps:
  1. The example heads read `model/Softmax`. phaze's `derive_*` functions were written against
     the current graphs' outputs, so the class order and activation must be re-checked per head.
  1. The 33 → 11 cut already moved coarse mean scores by up to **±0.117** (Party) and changed
     **65/208** labels (`docs/essentia-analysis.md`).
  1. `set_projection.positive_class_vector` (`src/phaze/services/set_projection.py`) reads Essentia-shaped variant predictions, so
     the adapter caveat in `docs/analysis-evaluation.md` applies.
  1. The effnet front end was trained on Discogs styles, the musicnn front end on MSD tags. Which
     is better for *mood* on long DJ and concert sets is an empirical question. No phaze record
     answers it.

#### A2. Running the existing `*-musicnn-msd-2` graphs' backbone once — a zero-label-drift hypothesis

- **Claim status: hypothesis, unverified.** All ten `*-musicnn-msd-2.pb` graphs have the same
  byte size (3,239,548). If their convolutional front end is **byte-identical** (a frozen backbone
  with per-task dense layers), phaze could run the front end once to `model/dense/BiasAdd` and
  evaluate only the per-task tail of each graph. The outputs would match today's to float
  tolerance, with **no label drift**. If instead each graph was fine-tuned end to end, this does
  not exist. Equal file size is consistent with both cases and proves neither.
- **How to settle it cheaply:** hash the weight tensors of the eleven graphs layer by layer. This is
  a five-minute read of local `.pb` files with no inference. It belongs on the profiling harness
  before A1 is taken.
- **Licence:** unchanged. **Fit:** requires splitting a frozen graph at a named node, which
  `TensorflowPredict` supports through its `inputs` / `outputs` parameters (`phaze-ytgo.2` used
  exactly this to read `PartitionedCall:1`).

#### A3. MAEST (Music Audio Efficient Spectrogram Transformer)

- **Source:** Lacuna, *Efficient Supervised Training of Audio Transformers for Music
  Representation Learning*
  (<https://lacuna.tiptreesystems.com/work/efficient-supervised-training-of-audio-transformers/version/art_5ada5c194477456281e87712cfe21c22>);
  repo `palonso/MAEST`; Essentia catalogue section "MAEST".
- **Reported accuracy and cost (Lacuna summary of the paper, not read from the PDF):** MAEST-30s
  reaches **41.7 mAP** on MagnaTagATune against an EfficientNet-B0 baseline's **37.4**. With high
  inference-time patchout it is described as "2–3 times faster than standard CNN baselines on a
  CPU". The latter is a qualitative summary claim. **Treat it as unverified until the PDF is read.**
  The paper's best embeddings come from a **middle block (~7 of 12)**, not the output.
- **What exists:** Essentia ships `discogs-maest-{5s,10s,20s,30s}-*-2.pb` and
  `discogs-maest-30s-pw-519l-2.pb` (4M tracks, 519 styles). The TensorFlow versions run at a
  **fixed batch size of 1**, with ONNX dynamic-batch versions also provided (catalogue line 289).
  **The catalogue lists no `*-discogs-maest-*` classification heads** for any phaze characteristic.
- **Licence:** code **AGPL-3.0** (`palonso/MAEST` `LICENSE`). Weights are **CC BY-NC-SA 4.0**, from
  the Essentia catalogue rule, and a web search reports the Hugging Face card tag for
  `discogs-maest-30s-pw-129e` as CC BY-NC-SA 4.0 (the card itself was not reachable). This is the
  same weights licence as today. Using the weights through essentia does not pull in the AGPL repo.
- **Fit:** a 400- or 519-style genre replacement for `discogs-effnet-bs64-1`, not a mood
  replacement. It could serve as a stronger shared embedding **only** if phaze trained its own
  eleven heads, and phaze holds no labelled data to do that. A transformer at batch 1 is unlikely
  to beat EfficientNet on CPU without the patchout trick, which the essentia wrapper does not
  expose (inferred). **Rank: accuracy option for genre/style, not a CPU option.**

#### A4. EfficientAT / DyMN (Transformer-to-CNN distilled MobileNets)

- **Source:** Lacuna, *Efficient Large-scale Audio Tagging via Transformer-to-CNN Knowledge
  Distillation*
  (<https://lacuna.tiptreesystems.com/work/efficient-large-scale-audio-tagging-via-transformer>)
  and *Dynamic Convolutional Neural Networks as Efficient Pre-trained Audio Models*
  (<https://lacuna.tiptreesystems.com/work/dynamic-convolutional-neural-networks-as-efficient>);
  repo `fschmid56/EfficientAT`.
- **Reported accuracy and cost (repo README table, AudioSet mAP):** `mn04_as` **0.983 M params,
  0.11 GMACs, 43.2**; `mn10_as` **4.88 M, 0.54 GMACs, 47.1**; `dymn10_as` **10.57 M, 0.58 GMACs,
  47.7**; `dymn20_as` **40.02 M, 2.2 GMACs, 49.1**. The paper summary reports `mn10` at about 6%
  of PaSST's parameters and **~100× fewer MACs** than PaSST-S. Its throughput figure of 4,767 vs
  78 clips/s is **GPU (A100)** and does not transfer to phaze.
- **Licence:** code and released weights **MIT** (repo `LICENSE`; weights are published as the
  repo's GitHub Releases).
- **Fit:** poor for phaze's outputs. The models are trained on **AudioSet's 527 sound-event
  classes**, not on Discogs styles or MSD mood tags, and phaze has no labels to train eleven heads.
  The distillation recipe is the useful idea. MTG has not published a distilled Discogs model in
  the catalogue reviewed. **Rank: low. A route to a cheaper backbone only if someone trains heads.**

#### A5. MERT, CLAP, PaSST — larger general embeddings

- **MERT** (Lacuna:
  <https://lacuna.tiptreesystems.com/work/mert-acoustic-music-understanding-model-with-large-scale/version/art_06cec1c9f8104d2ab80d3338f679a946>).
  A 95M / 330M-parameter self-supervised transformer, trained on **5-second segments**, strong on
  14 MIR probes. Code is **Apache-2.0** (`yizhilll/MERT` `LICENSE`). Weights are tagged
  **CC-BY-NC-4.0** on the `m-a-p/MERT-v1-95M` Hugging Face card, per web-search metadata; the card
  was not fetched. That is *looser* than CC BY-NC-SA, since it has no share-alike, but it is still
  non-commercial. 95M parameters is far heavier than musicnn on CPU (inferred from size), and MERT
  ships no heads. **Not a CPU option.**
- **CLAP** (Lacuna:
  <https://lacuna.tiptreesystems.com/work/large-scale-contrastive-language-audio-pretraining>).
  `phaze-ytgo.3` E3 already established `laion/clap-htsat-unfused` as **Apache-2.0**, 586 MiB fp32,
  27.5M audio / 124.6M text parameters, PyTorch. Zero-shot mood by text prompt is possible in
  principle, but its accuracy on phaze's eleven characteristics is unknown. It adds a PyTorch stack.
  **Not a CPU option.**
- **PaSST** (Lacuna:
  <https://lacuna.tiptreesystems.com/work/efficient-training-of-audio-transformers-with-patchout/version/art_b74d9319c5db4ca8a09cd6ff428fef78>).
  Code is **Apache-2.0** (`kkoutini/PaSST` `LICENSE`). It is AudioSet-trained, it is the teacher
  for A4, and the A4 paper reports it at ~100× the MACs of `mn10`. **Not a CPU option.**

### B. Fine tier — beat/BPM and key

**Ceiling first.** On the 10 h 03 m file the fine tier is **97.7% decode** (`phaze-zaf2l` §3b
forward note). Replacing `RhythmExtractor2013` and `KeyExtractor` with something infinitely fast
would therefore recover at most about **2.3% of fine-tier time** on that file. On the 59 m 39 s
file, where the fine tier is 7.9% of the run, that is roughly **0.2% of the whole run**
(arithmetic on the cited shares, assuming the same decode share). Fine-tier candidates are judged on
accuracy and dependency cost, **not** CPU.

#### B1. `RhythmExtractor2013(method="degara")` — the in-place cheaper option, and why it breaks phaze

- **Source:** `MTG/essentia` `src/algorithms/rhythm/rhythmextractor2013.cpp` and
  `beattrackermultifeature.cpp`. `multifeature` builds several onset detection functions, each
  feeding a `TempoTapDegara`, and selects with `TempoTapMaxAgreement`. `degara` is a single
  tracker. That is structurally less work; **the speed difference is not measured anywhere read**.
- **Blocking integration fact:** the docstring says that for `degara` "there is no confidence
  estimation … the output confidence value is always 0". phaze stores BPM only `if confidence >
  0.0` (`_measure_fine_window`), so `degara` as-is would **null every fine-window BPM**. Using it
  would need a gate change. **Licence:** essentia library, unchanged.

#### B2. Beat This! (ISMIR 2024)

- **Source:** repo `CPJKU/beat_this` README (Lacuna does not index the paper). A web search snippet
  reports GTZAN **beat F1 89.1, downbeat F1 78.3**. That is a third-party listing, not the paper,
  so treat it as unverified.
- **Models:** `final0–2` about **78 MB** each; `small0–2` about **8.1 MB** each (README). It needs
  no DBN post-processing by design.
- **Licence:** code **and published model weights MIT** (README "License"). This is the loosest
  licence in this survey. The README warns that some *training* data is copyrighted or under
  limited Creative Commons terms, and leaves it to the user to assess.
- **Fit:** PyTorch ≥ 2 plus `einops`, `rotary-embedding-torch`, `soxr` (PyPI `beat-this` 1.1.0
  `requires_dist`). That is a new heavyweight runtime beside TensorFlow in every analysis image,
  and an arm64 cp313 PyTorch wheel would need checking. It outputs beats and downbeats, from which
  BPM would have to be derived. **Rank: accuracy upgrade with real dependency cost; no CPU case.**

#### B3. madmom (RNN/DBN beat tracking, CNN key)

- **Source:** Lacuna, *madmom: A New Python Audio and Music Signal Processing Library*
  (<https://lacuna.tiptreesystems.com/work/madmom-a-new-python-audio-and-music-signal/version/art_ab8c2b5164f74e1bb1fc72523ce5d572>):
  top MIREX ranks for beat tracking and tempo; lineage *Joint Beat and Downbeat Tracking with
  Recurrent Neural Networks*
  (<https://lacuna.tiptreesystems.com/work/joint-beat-and-downbeat-tracking-with-recurrent-neural/version/art_cecc54670cd545458420c0edbfb7f9a6>).
- **Licence:** source **BSD**; model and data files **CC BY-NC-SA 4.0** (`CPJKU/madmom` `LICENSE`).
  This is the same weights licence as today.
- **Packaging:** PyPI's latest is **0.16.1**, published as an **sdist only**. The Beat This! README
  says "the current version on PyPI only supports Python<3.10 and numpy<1.20" and points at the git
  `main` branch. **Not installable from PyPI on phaze's Python 3.14 / 3.13 images.**
- **Rank:** low. Its accuracy edge would come with a git-pinned Cython build.

#### B4. BeatNet

- **Source:** Lacuna, *BeatNet: CRNN and Particle Filtering for Online Joint Beat Downbeat and Meter
  Tracking*
  (<https://lacuna.tiptreesystems.com/work/beatnet-crnn-and-particle-filtering-for-online-joint>);
  repo `mjhydri/BeatNet`.
- **Licence:** **CC BY 4.0** (repo `LICENSE`, "Attribution 4.0 International"). It allows
  commercial use, so it is looser than today.
- **Packaging:** PyPI `BeatNet` 1.1.3 requires `numba ==0.54.1`, `madmom`, `torch`, and `pytest`
  as runtime dependencies. A numba pin from 2021 rules out Python 3.13 / 3.14 (inferred from the
  pin's age). **Rank: not deployable as packaged.**

#### B5. All-In-One (beats, downbeats, structure)

- **Source:** Lacuna, *All-In-One Metrical and Functional Structure Analysis with Neighborhood
  Attentions on Demixed Audio*
  (<https://lacuna.tiptreesystems.com/work/all-in-one-metrical-and-functional-structure-analysis/version/art_965f08de744e4f5aa118140b13ad3ced>):
  Harmonix beat F1 **0.958**, downbeat F1 **0.915**, about **300K** parameters (Lacuna summary). The
  ablation says removing demixed input "significantly degrades performance".
- **Licence:** **MIT** (repo `LICENSE`).
- **Cost:** it requires **Demucs source separation** before inference, plus NATTEN on Linux. The
  README's only speed figure is GPU: "RTX 4090 … 10 songs (33 minutes) in 73 seconds". Source
  separation of multi-hour sets on CPU would cost far more than the whole current fine tier
  (inferred). **Rank: rejected for CPU.**

#### B6. Key estimation

- **Current:** `KeyExtractor(profileType="edma")`. Essentia's `key.cpp` describes `edma` as
  "automatic profiles extracted from corpus analysis of electronic dance music … They normally
  perform better that Shaath's". This is a sensible profile for a DJ and festival archive. The same
  source offers `bgate` / `braw` (Beatport-derived) and the classic `krumhansl` / `temperley`
  family, each a free swap with no new dependency. **Accuracy differences on phaze's corpus are
  unmeasured.**
- **CNN key:** Lacuna, *Deeper Convolutional Neural Networks and Broad Augmentation Policies Improve
  Performance in Musical Key Estimation*
  (<https://lacuna.tiptreesystems.com/work/deeper-convolutional-neural-networks-and-broad-augmentation/version/art_286702dd8f0a41d68ca8f6f1415c2516>):
  InceptionKeyNet reports a weighted MIREX score of **74.46%** on a small training set, **+2.72%**
  over the previous best (Lacuna summary). No packaged release or licence was found for it.
  madmom's CNN key model inherits B3's packaging problem and CC BY-NC-SA weights.
- **Rank:** no CPU case. A profile A/B (`edma` vs `bgate`) is a free accuracy experiment, if
  anyone wants one.

#### B7. TempoCNN (inside Essentia)

- **Source:** Essentia catalogue "TempoCNN": "Tempo classification (256 BPM classes, 30-286 BPM)",
  models `deepsquare-k16-3`, `deeptemp-k4-3`, `deeptemp-k16-3`. **CC BY-NC-SA 4.0**, run by
  essentia's existing TF build.
- **Fit:** BPM only, no beat positions. phaze stores only BPM per fine window, so it would be a
  drop-in for that column. It is a neural network over audio, so it is unlikely to be cheaper than
  `RhythmExtractor2013` (inferred). **Rank: accuracy alternative, no CPU case.**

### C. Inference acceleration

**`phaze-mqq5` already measured this on phaze's own models and host. This survey does not re-argue
it.** Its findings, verbatim figures:

- ONNX Runtime reproduces the TF predictions to **1.15 × 10⁻⁵** max per-patch with **0/34** top-1
  disagreements. Inside the real `analyze_file`, however, it peaked at **1.800 GiB** against
  TF-at-batch-32's **1.501 GiB** and **doubled the wall clock**.
- int8 dynamic quantisation: aggregated Δ **0.651** on `discogs-effnet-bs64-1`, **flipped the top-1
  genre label**, and ran **2× slower** (347.96 s vs 175.99 s). fp16 gave **−5.2%** peak and ran
  slower, because the CPU execution provider has no fp16 kernels for these ops.
- Batch size: already taken (32), and it is a memory lever (**−35.2%** peak), not a CPU lever
  (**+1.1%** wall).
- TFLite: not measured. It shares ONNX's conversion cost.

**What is new since `phaze-mqq5`:** the Essentia catalogue now ships **official ONNX artefacts**,
`discogs-effnet-bsdynamic-1.onnx` (dynamic batch, catalogue line 154) and ONNX MAEST models (line
289). The catalogue says MTG could not port the dynamic-batch effnet to TensorFlow, which is why
phaze runs a fixed `bs64` graph. An MTG-published ONNX effnet removes the "phaze owns a conversion
pipeline" cost for the one graph that matters most under A1. That strengthens `phaze-mqq5`'s
"ONNX for arm64" case. It does **not** reverse its "ONNX is slower on x86 in `analyze_file`"
measurement.

**OpenVINO:** Lacuna had nothing audio-specific, and no source reached in this session reports
OpenVINO on these graphs. **No claim is made.**

______________________________________________________________________

## Ranked shortlist

Scored **CPU saving × accuracy risk × licence fit**. Every saving is **inferred, not measured on
phaze**; the "anchor" column names the nearest phaze measurement it leans on.

| # | candidate | CPU saving (inferred) | accuracy risk | licence vs today (CC BY-NC-SA 4.0) | integration | anchor |
| ---: | --- | --- | --- | --- | --- | --- |
| **1** | **A2 — run the existing musicnn front end once, if the eleven graphs share it** | **high**: 11 front-end passes → 1 per coarse window | **none** if weights are byte-identical (float tolerance) | same | split graphs at `model/dense/BiasAdd`; same loop | 33 → 11 heads: 2.025× / 2.108× |
| **2** | **A1 — effnet embedding + 11 `*-discogs-effnet-1` heads** | **highest**: 11 musicnn passes → 0 (effnet already runs); +11 small `TensorflowPredict2D` evals | **medium**, unmeasured: different artefacts, Softmax heads, Discogs-trained front end | same | two outputs from the effnet graph; heads swept per chunk; adapter for `positive_class_vector` | 33 → 11 heads; `phaze-ytgo.2` +1.6% for the embedding tap |
| 3 | A1 variant — `msd-musicnn-1` + 11 `*-msd-musicnn-1` heads | high: 11 → 1 musicnn pass | medium-low: same MSD/musicnn family, still new artefacts | same | as A1 with one extra graph | same |
| 4 | C — ONNX Runtime with MTG's `discogs-effnet-bsdynamic-1.onnx` | **none on x86** (`phaze-mqq5`: 2× wall) | none (1.15 × 10⁻⁵) | same weights | new runtime; drops the TF aarch64 / Python 3.13 pin | `phaze-mqq5` §5, §6 |
| 5 | B6 — key profile `bgate` / `braw` vs `edma` | ≈ 0 (fine tier 97.7% decode) | unknown, both directions | same (essentia) | one keyword | — |
| 6 | A3 — MAEST for genre/style | negative to neutral (transformer, TF batch 1) | lower for style per paper; no mood heads | weights same; code AGPL | swap genre graph; style label set changes (400 → 519 for `-519l`) | none |
| 7 | B2 — Beat This! | ≈ 0 to negative | likely better beats (unverified 89.1 F1 snippet) | **MIT**, looser | adds PyTorch; derive BPM | none |
| 8 | B7 — TempoCNN | ≈ 0 to negative | unknown | same | essentia TF graph; BPM only | none |
| 9 | A4 / A5 — EfficientAT, DyMN, MERT, CLAP, PaSST | n/a without new heads | n/a: no heads for phaze's 11 outputs | MIT / CC-BY-NC / Apache | needs training data phaze does not have | none |
| — | B1 `degara`, B3 madmom, B4 BeatNet, B5 All-In-One, int8 | — | — | — | **rejected**: confidence gate / not installable on 3.13–3.14 / Demucs on CPU / `phaze-mqq5` accuracy fail | — |

**Rationale for the order.** Rank 1 sits above rank 2 despite a smaller theoretical saving (one
musicnn pass remains) because it is the only option with **zero label drift**, and it is settled by
a five-minute weight hash before any inference is run. If the hash shows the front ends differ,
rank 1 is void and rank 2 leads. Ranks 2 and 3 trade the most CPU against label drift. The one
precedent, the 33 → 11 change, shipped with its label diff published as review evidence
(**65** changed coarse mood labels, `docs/essentia-analysis.md`). Rank 4 is a deployment win and nothing more on CPU. Everything below rank 4
offers accuracy or nothing, and a CPU-cut epic should not take them on that basis.

______________________________________________________________________

## Verdict

1. **The coarse model sweep is where an alternative can matter, and the lever is structural:**
   stop running eleven full backbones over the same window. Both ways to do that (A2, A1) stay
   inside essentia, inside the CC BY-NC-SA 4.0 licence phaze already accepts, and inside the
   model-major, chunk-bounded loop. Neither needs a new runtime, a new language or a training run.
1. **No beat or key tracker is a CPU lever** while decode owns **97.7%** of the fine tier. The
   neural trackers are accuracy upgrades with dependency costs. Several of them (madmom, BeatNet)
   cannot be installed from PyPI on phaze's Python versions, and All-In-One needs source separation.
1. **Runtime acceleration was settled by `phaze-mqq5`** and nothing found here overturns it.
   MTG's new official ONNX effnet makes the arm64 migration cheaper. It does not make x86 faster.
1. **Bigger embeddings are not cheaper**, and none of them ships heads for phaze's eleven outputs.
   Without labelled training data, the general-purpose models are not candidates for a CPU epic.

## Recommendation for `phaze-uv8s3`

**Consider, in this order, and measure each on the profiling spike's harness (`phaze-fzubr`)
before any swap. Nothing below is measured here.**

1. **Per-graph CPU split on one host.** On one 180 s window, at the deployed batch and thread
   settings, measure the CPU seconds for `discogs-effnet-bs64-1` against one `*-musicnn-msd-2`
   graph. Every saving in the shortlist is a multiple of this unknown ratio. Without it, "11 passes
   → 0" cannot be turned into a percentage.
1. **The A2 weight hash.** Compare the eleven `*-musicnn-msd-2` / `-msd-1` graphs' tensors
   layer by layer. If the front ends are identical, prototype a split-graph sweep and hold it to
   `phaze-mqq5`'s stated tolerance: **aggregated |Δ| ≤ 10⁻³ and zero top-1 flips**, using the
   real-model pin in `tests/analyze/services/pipeline/test_analysis_output_equivalence.py`
   (`phaze-28883`). That pin is the real consumer, per rule 3.
1. **If A2 is void, A1 (effnet heads) with a published label diff.** Use the same
   protocol `docs/essentia-analysis.md` used for 33 → 11: the paired evaluation sample, per-window
   label changes, and per-attribute mean deltas. How much label drift is acceptable is a
   question for the operator, to be asked and answered on the bead before the run, not settled
   in prose. Check
   class order and Softmax-vs-Sigmoid per head against `derive_mood` / `derive_style` /
   `derive_danceability`, and the `positive_class_vector` adapter.
1. **Blast radius (rule 4), to be written on the implementation bead.** Any of A1–A3 changes the
   coarse scores for **every file analysed after it ships**. Files already analysed stay on the old
   models unless reanalysed. That is a mixed-population hazard for `set_projection` and similarity,
   and the implementation bead must name the test that bounds it.
1. **Keep the arm64 / ONNX question separate.** If it is taken up, start from MTG's
   `discogs-effnet-bsdynamic-1.onnx` rather than a phaze-owned conversion. It is a deployment
   decision, not part of this epic's CPU budget.
1. **Do not file** fine-tier tracker swaps, int8, MERT, CLAP, PaSST or EfficientAT under this
   epic. If accuracy work is ever wanted, Beat This! (MIT) and a key-profile A/B are the cheapest
   places to start.

**Proxy warning, restated.** Every literature number above (mAP, F1, MACs, "2–3× faster on CPU") was
measured on public benchmarks and other hardware, usually on short clips. phaze's corpus is
multi-hour DJ and concert sets, decoded in 30 s / 180 s windows on a contended 8-CPU node. Under
ADR-0012 (`docs/design/0012-verification-fidelity-and-operator-attribution.md`) rule 3, none of these
figures discharges an acceptance criterion. Only a run through `analyze_file` on the real model set
and real corpus files does.

## What this document could not source

- **Per-head accuracy** of the `*-discogs-effnet-1` and `*-msd-musicnn-1` heads (the `.json`
  sidecars on `essentia.upf.edu` were unreachable).
- **The Beat This! paper's own numbers**, and its small-model accuracy (arXiv, Zenodo and the ISMIR
  site were unreachable). The 89.1 / 78.3 F1 figures come from a third-party web listing.
- **MAEST's CPU speed claim**, which comes only from Lacuna's summary. The PDF was unreachable.
- **Hugging Face licence tags** for MAEST and MERT, read from web-search metadata, not from the
  cards.
- **Any OpenVINO figure**, and **whether the eleven musicnn front ends are byte-identical** (A2),
  which needs the weight hash recommended above.
