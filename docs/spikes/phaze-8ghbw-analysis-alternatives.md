# phaze-8ghbw — faster alternatives for music analysis: a literature and licence survey

- **Bead:** `phaze-8ghbw` (spike — research faster or better alternatives to the analysis stack:
  papers, models, libraries, licences). Reconciled by `phaze-0toy3.3`
- **Parent epic:** `phaze-0toy3` (where analysis CPU goes and how to cut it). Sibling spikes
  `phaze-fzubr` (profiling, `docs/spikes/phaze-fzubr-analysis-cpu-profile.md`) and `phaze-phjwi`
  (TensorFlow threads, `docs/spikes/phaze-phjwi-tf-threads.md`) are cited, not repeated. Decision
  bead `phaze-uv8s3` consumes this document
- **Date:** 2026-11-13, amended 2026-11-14 to mark A2 void per `phaze-0toy3.4`
- **Provenance:** this file merges two independent runs of the same spike. A cloud run
  (commit `816b2ff4`, grounded in phaze's own records) and a local run (commit `d00b465a`, which
  read the Beat This! and MAEST papers and every licence from a primary source). Where they
  disagreed, the disagreement is resolved from the primary source and the resolution is named in
  "Reconciliation notes"
- **Status:** desk research only. **No product code changed, nothing was measured by this spike,
  and phaze production was not accessed.** Every accuracy or speed figure below that is not from a
  phaze record is a **literature proxy**, not a measurement on phaze's corpus (CLAUDE.md
  "Acceptance criteria, attribution, and verification fidelity", rule 3). Figures labelled
  *phaze-measured* come from the cited phaze spike, not from this one

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
1. What licence does each candidate carry, compared with the Essentia models phaze runs today
   (CC BY-NC-SA 4.0 per the Essentia models page, see "Licences")?

## Verdict in one paragraph

**The CPU is in the coarse mood sweep, and the lever is structural, not a new model family.**
`phaze-fzubr` measured on real audio, with the deployed image and settings, that the 11
`musicnn_msd` mood graphs are **84.5%** of analysis CPU (about **9.0 cpu-s per graph**, **96.8
cpu-s** of a 180 s coarse window), the genre EffNet pass is **3.67 cpu-s** (3.2%), and everything
else is 12.3%. Each coarse window therefore runs eleven full musicnn backbones over the same
audio. MTG's catalogue lists a `*-discogs-effnet-1` and a `*-msd-musicnn-1` head for every one of
phaze's eleven characteristics, taking a precomputed embedding. The zero-drift way to cut the
backbone passes, **A2** (run the existing musicnn front end once), is **void**:
`docs/spikes/phaze-0toy3.4-musicnn-shared-frontend.md` (branch `wt/bead/issue/phaze-0toy3.4`,
commit `2cafd2ca`) compared all 55 pairs of the 11 deployed graphs and found **0 of 44** trainable
tensors identical, first differing at the input batch normalisation, so each graph is a full
fine-tune of `msd-musicnn-1`. What remains is **A1** (embedding plus MTG's heads) and heads fitted
to the stored scores. Both remove most of the CPU and **both change the meaning of the stored
scores**: `phaze-8y6l8` found the stock EffNet mood heads are not a drop-in for the musicnn scores,
so they are semantic changes that need the operator's call, not free optimisations. Everything else
surveyed ranks lower. Decode, rhythm and key are 12.3% of CPU in total, so no beat or key tracker
is a CPU lever. Runtime changes were measured by `phaze-mqq5`: ONNX Runtime **doubled** wall clock
inside `analyze_file` and int8 **flipped the top-1 genre label**. Bigger embeddings (MAEST, MERT,
MuQ, CLAP) buy accuracy, not speed, and none ships heads for phaze's characteristics.

______________________________________________________________________

## Method

| | |
| --- | --- |
| **Code grounding** | `src/phaze/services/analysis.py`, `src/phaze/services/analysis_models.py`, `src/phaze/scripts/download_models.py`, `Dockerfile.agent-arm64`, read at `c3718544` (cloud run) and the epic container (local run) |
| **phaze records** | `docs/essentia-analysis.md`, `docs/design/0007-windowed-analysis.md`, `docs/analysis-evaluation.md`, and spikes `phaze-fzubr`, `phaze-phjwi`, `phaze-zaf2l`, `phaze-mqq5`, `phaze-ytgo.2`, `phaze-ytgo.3`, `phaze-28883` (all under `docs/spikes/`) |
| **Lacuna MCP** (papers) | `search_lacuna` then `get_paper` for MAEST, Discogs-EffNet contrastive pre-training, musicnn, Essentia TF models, EfficientAT, Low-Complexity Audio Embedding Extractors, DyMN, PaSST, MERT, MuQ, CLAP, madmom lineage, BeatNet, Beat Transformer, All-In-One, InceptionKeyNet, music-emotion embeddings. Lacuna has no citation graph, so no citation counts are claimed |
| **Primary sources** | arXiv HTML for **Beat This!** (2407.21658) and **MAEST** (2309.16418), both read in full; the GitHub licence API (`gh api repos/<owner>/<repo>/license`) and each repository's README licence section; Hugging Face model-card `license` fields (`huggingface.co/api/models/<id>`); files hosted on `essentia.upf.edu` (`models.html`, `models/LICENSE`, the `.json` model metadata); raw GitHub files of `MTG/essentia` (`doc/sphinxdoc/models.rst`, example scripts, `key.cpp`, `rhythmextractor2013.cpp`, `COPYING.txt`), `CPJKU/beat_this`, `CPJKU/madmom`, `mjhydri/BeatNet`, `mir-aidj/all-in-one`; PyPI JSON for `madmom`, `beat-this`, `BeatNet` |

**Lacuna coverage.** One query each did not return Beat This!, MAEST, EfficientAT's code,
Discogs-EffNet or all-in-one, so those are cited from arXiv, Hugging Face and project pages.
Lacuna had nothing audio-specific on ONNX Runtime, OpenVINO or int8 quantisation for audio CNNs on
CPU. Lacuna does not cover licences, so **every licence below comes from a repository file, the
Essentia catalogue, a hosted licence file or a model-card field**.

______________________________________________________________________

## Current stack (grounded)

### What runs, per tier

| tier | window | rate | what runs per window | source |
| --- | --- | --- | --- | --- |
| **fine** | 30 s (`_DEFAULT_FINE_WINDOW_SEC`), trailing window dropped below 15 s (`_DEFAULT_FINE_MIN_SEC`) | 44,100 Hz (`_FINE_SAMPLE_RATE`) | `es.RhythmExtractor2013(method="multifeature")` → BPM, gated on `confidence > 0.0`; `es.KeyExtractor(profileType="edma")` → `"<key> <scale>"`, gated on `strength > 0.0` | `analysis.py` (`_analyze_fine_windows`, `_measure_fine_window`) |
| **coarse** | 180 s (`_DEFAULT_COARSE_WINDOW_SEC`) | 16,000 Hz (`_COARSE_SAMPLE_RATE`) | model-major sweep: **12 graphs** by default: 11 `musicnn_msd` characteristic graphs (`TensorflowPredictMusiCNN`) + `discogs-effnet-bs64-1` (`TensorflowPredictEffnetDiscogs`, 400 Discogs styles) | `analysis_models.py` (`_select_model_sets`, `GENRE_MODEL`) |

- **The eleven characteristics:** mood_acoustic, mood_electronic, mood_aggressive, mood_relaxed,
  mood_happy, mood_sad, mood_party, danceability, gender, tonal_atonal, voice_instrumental. The
  graphs are `*-musicnn-msd-2` except `voice_instrumental-musicnn-msd-1`. Rollback
  (`PHAZE_ANALYSIS_MODEL_VARIANTS=all`) restores 33 heads (adding `musicnn_mtt` and `vggish`). The
  deployed agents do not set that variable, so the 11-graph `msd` set is what runs
  (`phaze-fzubr`, Method).
- **Each characteristic graph is a whole network, not a head.** The download manifest pins every
  musicnn characteristic graph at **3,239,548 bytes** (`voice_instrumental-musicnn-msd-1`:
  3,239,625), and `phaze-ytgo.2` found the 200-d `model/dense/BiasAdd` embedding layer *inside*
  each `*-musicnn-msd-2.pb`. Each characteristic therefore re-runs the musicnn front end over
  the same 180 s of audio. The effnet graph is **18,366,619 bytes**, and the 12-graph set totals
  **54,048,897 bytes** (`phaze-28883` §1a).
- **Patch rates per 180 s coarse window** (`phaze-ytgo.2`): effnet **180 patches** (1.00 Hz),
  musicnn **119 patches** (0.66 Hz).
- **Chunking (D-07):** 60 fine / 30 coarse windows per chunk (`_FINE_CHUNK_WINDOWS`,
  `_COARSE_CHUNK_WINDOWS`). Exactly one `TensorflowPredict*` graph is resident at a time. TF
  `batchSize` is 32 (`_DEFAULT_TF_BATCH_SIZE`), except the effnet graph, whose input placeholder is
  fixed at `[64, 128, 96]`.
- **Liveness (D-08) and memory (D-09).** A new model must heartbeat per window or per sweep and
  must not add a wall-clock bound (`analysis_stall_timeout_sec` is progress-based). Peak RSS must
  stay flat across chunks on a multi-hour file; a PyTorch or ONNX model has no Essentia network to
  disconnect, so its own allocator behaviour would need its own flat-RSS test.
- **Per-window interface.** A replacement must fit `_measure_fine_window` (decoded 44.1 kHz mono
  buffer in, `FineWindow(bpm, musical_key)` out) or the coarse model-major loop (decoded 16 kHz
  buffer in, `[patches, classes]` activations out, averaged over `axis=0`). The required semantic
  outputs and their consumers are listed in `docs/analysis-evaluation.md` ("Result inventory and
  consumers"), including the 11 fixed coarse scores and the `set_projection_writer` adapter caveat.

### Deployment constraints

- **CPU-only.** The analysis backend `host-compute` has 8 logical CPUs, runs `cap = 4` concurrent
  pods, and sets `memory_limit 4Gi` per pod (`phaze-zaf2l` §1).
- **arm64 agent pinned to Python 3.13.** `Dockerfile.agent-arm64` builds from
  `python:3.13-slim-trixie` and builds essentia from source against the `tensorflow` aarch64
  wheel. `tensorflow` publishes no cp314 aarch64 wheel; `onnxruntime` and `ai-edge-litert` do
  (`phaze-mqq5` §6, verified against PyPI 2026-08-06).
- **Peak RSS must stay duration-independent** (CLAUDE.md "Analysis is exhaustive, chunk-bounded…").

### What phaze has already measured about where CPU goes

| measurement | figure | source |
| --- | --- | --- |
| per-stage CPU, six real files (6.675 audio-hours), deployed image, one TF thread | 11 mood graphs **96.8** cpu-s per coarse window (**84.5%** of total, ≈ **9.0** per graph); genre EffNet **3.67** (3.2%); fine decode **7.6%**; rhythm **1.7%**; key **0.1%**; coarse decode **2.9%**; **2,316** cpu-s per audio-hour | `phaze-fzubr` per-stage table |
| EffNet embedding output (`PartitionedCall:1`) as a separate pass | **3.71** cpu-s per coarse window, the same as the genre pass; whether one run can emit both outputs was **not measured** there | `phaze-fzubr` "EffNet embedding output" |
| tap of the embedding off the existing effnet pass | **+1.6%** wall clock | `phaze-ytgo.2` |
| 12-graph sweep, control | **104.8** cpu-s per window, consistent with fzubr's 100.5 (96.8 + 3.67) | `phaze-phjwi` control note |
| 2 TF threads in one pod | **1.81×–1.86×** windows per wall-hour on a 2-CPU budget, at **5.5%–6.7%** more CPU per window | `phaze-phjwi` |
| 33 → 11 characteristic heads (34 → 12 graphs), 8-file sample, 4-vCPU evaluation host | 195.3 / 209.0 min → 96.4 / 92.7 min, **2.025× / 2.108× faster**; **65 of 208** coarse mood labels changed; mean scores moved by up to **±0.117** (Party) | `docs/essentia-analysis.md` |
| production per-file wall at `cap = 4` | **1.4951×** file duration (7,119.473 s for a 4,761.835 s file) | `phaze-zaf2l` verdict |
| tier split of a 36,182.359 s file | fine **49.3%**, coarse + model sweep **50.7%**; within the fine tier **97.7%** is decode (10 h file) | `phaze-zaf2l` §3b forward note |
| 34-graph sweep, one 180 s window, TF batch 32 | **86.35 s** wall | `phaze-mqq5` §3 |
| EffNet-heads vs musicnn mood scores: levels shift by up to **0.29**, dominant-mood agreement **25 of 91** windows | quoted from `phaze-8y6l8` | **not re-verifiable in this tree**; see "What this document could not source" |

**Why the 97.7% and the fzubr shares both stand.** `phaze-zaf2l`'s 97.7% is decode's share *inside
the fine tier of one 10-hour file*. `phaze-fzubr`'s figures are shares of *total* CPU over six
files (fine decode 1.48 cpu-s against rhythm 0.32 and key 0.017 per fine window, so decode is about
**81%** of the fine tier there). The two samples differ and decode is file-dependent (fzubr: 35 to
352 cpu-s per audio-hour); both agree that decode, not the trackers, dominates the fine tier, and
the ceiling below uses fzubr's total-CPU shares.

______________________________________________________________________

## Evidence

### A. Coarse tier — embeddings and classifier heads

#### A1. Essentia classification heads on a shared embedding (Discogs-EffNet or MSD-musicnn)

- **Source:** the Essentia model catalogue, `MTG/essentia` `doc/sphinxdoc/models.rst`, its example
  scripts under `src/examples/python/models/scripts/classification-heads/`, and
  <https://essentia.upf.edu/models.html>. Paper lineage, via Lacuna: *Pre-Training Strategies Using
  Contrastive Learning and Playlist Information for Music Classification and Similarity*
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
- **Licence:** the models page states all MTG models are CC BY-NC-SA 4.0. See "Licences" for why
  the hosted licence file leaves ShareAlike versus NoDerivatives unsettled; either way the term
  is **the same as phaze's current graphs: no change**.
- **Reported accuracy:** each head's metrics live in its `.json` sidecar; the sidecars read
  (`EffnetDiscogs`, `MSD MusiCNN`, `mood happy`) carry **no `license` field**, and no per-head
  accuracy figure is claimed here.
- **Cost.** The backbone runs once per coarse window instead of eleven times. *phaze-measured
  anchors:* one musicnn graph is ≈ 9.0 cpu-s and the whole effnet pass 3.67 cpu-s per coarse window
  (`phaze-fzubr`). phaze **already** runs `discogs-effnet-bs64-1` for genre, and `phaze-ytgo.2`
  measured reading the `PartitionedCall:1` embedding off that existing pass at **+1.6%** wall
  clock, whereas `phaze-fzubr` priced a *separate* embedding pass at 3.71 cpu-s. Which of those two
  numbers a one-pass implementation pays is the open item (the extra 3.7 cpu-s is 3.7% of a
  window). The cost of the eleven small `TensorflowPredict2D` heads is **unmeasured**. So the
  effnet route removes eleven backbone passes (≈ 97 cpu-s of ≈ 100) at the price of one embedding
  tap plus eleven small head evaluations, and the net is an inference from those anchors, not a
  measurement.
- **Per-window fit:** a direct fit for the coarse model-major loop. Under this option the effnet
  graph emits two outputs (`PartitionedCall:0` for genre, `PartitionedCall:1` for the embedding).
  The per-window embeddings, about **180 × 1280 float32 ≈ 0.92 MB** per window (arithmetic), are
  held across the chunk's 30 windows (**≈ 27.6 MB**), and each `TensorflowPredict2D` head is then
  swept over them. This leaves peak RSS chunk-bounded (D-07).
- **CPU/arm64 fit:** same essentia `TensorflowPredict*` family, so no new dependency and no change
  to the arm64 position.
- **Accuracy risk, and it is a meaning risk:** these are **different trained artefacts** from
  `*-musicnn-msd-2`, so outputs move. `phaze-8y6l8` found the stock EffNet mood heads are **not a
  drop-in** for phaze's stored musicnn mood scores (different label space for the MTG-Jamendo
  mood/theme task, shifted levels, low dominant-mood agreement; figures quoted in the table above
  and not re-verified here), and `phaze-fzubr` repeats that finding. The intended behaviour is
  "mood scores that mean what the musicnn scores meant", not "a mood score with no error"
  (`real-fix-over-mitigation`). Four specific traps:
  1. The example heads read `model/Softmax`. phaze's `derive_*` functions were written against
     the current graphs' outputs, so class order and activation must be re-checked per head.
  1. The 33 → 11 cut already moved coarse mean scores by up to **±0.117** (Party) and changed
     **65/208** labels (`docs/essentia-analysis.md`).
  1. `set_projection.positive_class_vector` (`src/phaze/services/set_projection.py`) reads
     Essentia-shaped variant predictions, so the adapter caveat in `docs/analysis-evaluation.md`
     applies.
  1. The effnet front end was trained on Discogs styles, the musicnn front end on MSD tags. A
     better score on MTG-Jamendo says nothing about agreement with phaze's stored scores.

#### A1b. EffNet embedding plus a head fitted to reproduce the musicnn mood scores

- **Idea (from the local run).** Keep the deployed EffNet embedding and replace the *stock* mood
  heads with a small head fitted so that its output reproduces the stored musicnn mood scores,
  which addresses the meaning problem in A1 directly. The literature supports shallow heads on
  mid-level embeddings in general (EfficientAT embeddings, Low-Complexity Audio Embedding
  Extractors; Lacuna
  <https://lacuna.tiptreesystems.com/work/low-complexity-audio-embedding-extractors>, which reports
  `mn10` at an average normalised HEAR 2021 score of 86.22% and no mood-specific figure; and
  *Comparison and Analysis of Deep Audio Embeddings for Music Emotion Recognition*,
  <https://lacuna.tiptreesystems.com/work/comparison-and-analysis-of-deep-audio-embeddings/version/art_acf064a85cf845779e64a18b2296fd4c>,
  datasets of 162 to 900 clips) but gives **no evidence for this specific mapping**.
- **Status:** unproven. Which stored scores are the fitting target (the 11-graph set, the 33-graph
  ensemble, or an operator-labelled sample) is **deliberately not decided in this document**.
  The operator answered that question "Leave it to phaze-uv8s3" (see "Decisions recorded"), so the
  target belongs to `phaze-uv8s3`.
- **Second embedding to fit on (from `phaze-0toy3.4`):** the `msd-musicnn-1` embedding, the backbone
  all eleven deployed graphs were fine-tuned from, so its features are plausibly closer to the
  deployed models' than EffNet's. It costs one musicnn pass (≈ 9 cpu-s) per window instead of eleven
  (≈ 97). That agreement is unmeasured. It could be fitted in the same measurement as the EffNet head.
- **Licence gate:** whether a head fitted on these models' outputs or embeddings is Adapted Material
  under the ND reading is open (see A2, "Licence"); it is a question for the operator and UPF.
  `docs/spikes/phaze-0toy3.5-clean-room-mood.md` goes further: it lists training on the stored
  musicnn scores as **not clean**, since they are outputs of a NonCommercial model, and offers a
  clean variant, the ISC `MSD_musicnn` backbone plus heads on labels the operator owns.

#### A2. Running the existing `*-musicnn-msd-2` graphs' backbone once — VOID (refuted)

- **Status: refuted by measurement.** The cloud run proposed this as a zero-label-drift hypothesis
  conditional on the eleven graphs sharing their front end, to be settled by a weight hash.
  `docs/spikes/phaze-0toy3.4-musicnn-shared-frontend.md` (branch `wt/bead/issue/phaze-0toy3.4`,
  commit `2cafd2ca`) ran that comparison (static `GraphDef` read, no inference, all 55 pairs): **0 of 44**
  trainable tensors are identical between any two deployed graphs, the first difference is the input
  `batch_normalization` gamma/beta in every graph, and the frozen batch-norm statistics are identical
  and also match `msd-musicnn-1`. Each graph is a full fine-tune of `msd-musicnn-1`. No runtime can
  share computation across different weights, so a split at `model/dense/BiasAdd` or earlier is not
  equivalent, and feeding the backbone embedding into the deployed `dense_1`/`dense_2` heads is a
  different model (label drift), not an optimisation.
- **What is still exactly shareable:** only the non-learned mel input (`TensorflowInputMusiCNN`),
  computed eleven times per window with identical parameters. That spike measured it at **0.089
  cpu-s per 180 s window** (median of 7, 1.0% of one graph call), so computing it once saves about
  **0.89 cpu-s per coarse window, 0.9% of the 96.8 cpu-s sweep**. The compute-once path is
  bit-identical to `TensorflowPredictMusiCNN` with Essentia's default centred framing and **not**
  equivalent with `startFromZero=True` (3.9 × 10⁻³ per patch). It recommends not building it for CPU.
- **Consequence:** the zero-drift cost floor of the mood sweep stays at eleven backbone passes per
  coarse window (96.8 cpu-s, `phaze-fzubr`). Threads (`phaze-phjwi`) remain the zero-drift lever.
- **Licence, as raised by that spike:** the hosted `models/LICENSE` appends BY-NC-ND 4.0 text. Under
  ND, a privately made, never-shared derived graph appears permitted but a committed or published one
  does not, and whether a head **fitted on these models' outputs or embeddings** is Adapted Material
  is open. That question now bears on A1b below and is for the operator to put to UPF.

#### A3. MAEST (Music Audio Efficient Spectrogram Transformer)

- **Source:** arXiv 2309.16418 (ISMIR 2023), **read in full**; Lacuna
  <https://lacuna.tiptreesystems.com/work/efficient-supervised-training-of-audio-transformers/version/art_5ada5c194477456281e87712cfe21c22>;
  repo `palonso/MAEST`; Essentia catalogue section "MAEST".
- **Reported (paper):** about **87 M parameters**, 5/10/20/30 s inputs, best embeddings in the
  middle blocks (block 7 used). MTG-Jamendo mood/theme ROC-AUC / mAP: EffNet-B0 baseline
  **75.6 / 13.6**, MAEST-30s **78.1 / 15.4**; MTG-Jamendo genre **87.7 / 19.9** vs **88.2 / 21.6**.
  Speed: under **strong patchout**, MAEST-30s "overcomes the throughput of standard CNN
  architectures by two to three times while keeping higher mAP", measured on a CPU the paper does
  not name, plus "double the inference speed of an EfficientNet-B0 baseline". **No absolute
  throughput is stated**, and the patchout condition is a different operating point from
  full-length embeddings. The paper's text says MAEST beats the baseline on every task while its
  Table 5 shows one mAP cell at 32.7 vs 32.8, so treat the headline as approximate.
- **What exists:** Essentia ships `discogs-maest-{5s,10s,20s,30s}-*-2.pb` and
  `discogs-maest-30s-pw-519l-2.pb` (519 styles). The TensorFlow versions run at a **fixed batch
  size of 1**, with ONNX dynamic-batch versions also provided (catalogue line 289). **The
  catalogue lists no `*-discogs-maest-*` classification heads** for any phaze characteristic.
- **Licence:** code **AGPL-3.0** (GitHub licence API for `palonso/MAEST`; its README has no licence
  section). Weights **`cc-by-nc-sa-4.0`** on the Hugging Face card
  (<https://huggingface.co/mtg-upf/discogs-maest-30s-pw-73e-ts>). Using the weights through
  essentia does not pull in the AGPL repo.
- **Fit:** a style/genre replacement for `discogs-effnet-bs64-1`, not a mood replacement. The
  mood/theme label space is MTG-Jamendo's, **different** from phaze's musicnn characteristics.
  A transformer at TF batch 1 is unlikely to beat EfficientNet on CPU without patchout, which the
  essentia wrapper does not expose (inferred). **Rank: accuracy option for style, not a CPU option.**

#### A4. EfficientAT / DyMN (transformer-to-CNN distilled MobileNets)

- **Source:** Lacuna, *Efficient Large-scale Audio Tagging via Transformer-to-CNN Knowledge
  Distillation* (arXiv 2211.04772;
  <https://lacuna.tiptreesystems.com/work/efficient-large-scale-audio-tagging-via-transformer>),
  *Dynamic Convolutional Neural Networks as Efficient Pre-trained Audio Models*
  (<https://lacuna.tiptreesystems.com/work/dynamic-convolutional-neural-networks-as-efficient>) and
  *Low-Complexity Audio Embedding Extractors* (arXiv 2303.01879, link above); repo
  `fschmid56/EfficientAT`.
- **Reported (repo README table, AudioSet mAP):** `mn04_as` 0.983 M params, 0.11 GMACs, 43.2;
  `mn10_as` 4.88 M, 0.54 GMACs, 47.1; `dymn10_as` 10.57 M, 0.58 GMACs, 47.7; `dymn20_as` 40.02 M,
  2.2 GMACs, 49.1. The distillation paper reports 0.483 mAP, a width-1.0 model matching one PaSST
  at 4.88 M vs 87 M parameters, about 100× fewer MACs than PaSST-S, and 4,767 vs 78 clips/s on an
  **A100 GPU**, which does not transfer to phaze. The embedding paper reports the tiny `mn01` at
  about 20 M MACs per 10 s of audio, said to be comparable to EffNet-B2 (900 M MACs) and OpenL3
  (780 M MACs), and that final-layer features generalise worse than mid-level ones. Neither paper
  reports a mood figure, and the distillation paper says transfer was not evaluated.
- **Licence:** the GitHub licence API reports **MIT** for the repository. The README has no separate
  weights-licence statement and the weights are published in the repo's `v0.0.1` release, so MIT
  is the only stated term; the licences of the AudioSet and ImageNet training data were not
  examined. Third-party Core ML and MLX ports exist on Hugging Face.
- **Fit:** poor for phaze's outputs: trained on **AudioSet's 527 sound-event classes**, not Discogs
  styles or MSD mood tags. The effnet embedding already costs only 3.67 cpu-s per coarse window
  (`phaze-fzubr`), so a cheaper embedding has little left to save. **Rank: low. Hold.**

#### A5. MERT, MuQ, CLAP, PaSST — larger general embeddings

- **MERT** (Lacuna:
  <https://lacuna.tiptreesystems.com/work/mert-acoustic-music-understanding-model-with-large-scale/version/art_06cec1c9f8104d2ab80d3338f679a946>).
  95 M / 330 M-parameter self-supervised transformer on **5-second segments**, 14-task benchmark.
  Code **Apache-2.0** (`yizhilll/MERT`). Weights **CC BY-NC 4.0** on the `m-a-p/MERT-v1-95M`
  Hugging Face card. Looser than CC BY-NC-SA (no share-alike), still non-commercial. Far heavier
  than musicnn on CPU (from size) and ships no heads. **Not a CPU option.**
- **MuQ** (Lacuna:
  <https://lacuna.tiptreesystems.com/work/muq-self-supervised-music-representation-learning-with-mel/version/art_a238830c06ed4105a9051bc587fa695b>).
  Gains over MERT and MusicFM on MARBLE; MuQ-MuLan reports 79.3 ROC-AUC zero-shot on
  MagnaTagATune. The released checkpoint is ~300 M parameters trained on the Million Song Dataset
  and the repository notes it may underperform the paper's larger-data model. Code **MIT**,
  weights **CC BY-NC 4.0** (repository `LICENSE` / `LICENSE_weights`; card
  `OpenMuQ/MuQ-large-msd-iter` lists `cc-by-nc-4.0`). **Not a CPU option.**
- **CLAP** (Lacuna:
  <https://lacuna.tiptreesystems.com/work/large-scale-contrastive-language-audio-pretraining/version/art_ea3043e4e5884e708a62c3c7b4acd8fb>).
  Text-prompted zero-shot labels, HTSAT-base backbone. The GitHub licence API reports **CC0-1.0**
  for `LAION-AI/CLAP` and the card `lukewys/laion_clap` lists `cc0-1.0`. The other checkpoint,
  `laion/clap-htsat-unfused`, lists `apache-2.0` (consistent with `phaze-ytgo.3` E3, which also
  measured it at 586 MiB fp32, 27.5 M audio / 124.6 M text parameters, PyTorch). Zero-shot accuracy
  on phaze's characteristics is unknown, and it adds a PyTorch stack. **Not a CPU option.**
- **PaSST** (Lacuna:
  <https://lacuna.tiptreesystems.com/work/efficient-training-of-audio-transformers-with-patchout/version/art_b74d9319c5db4ca8a09cd6ff428fef78>).
  Code **Apache-2.0** (`kkoutini/PaSST`), AudioSet-trained, the teacher for A4 at about 100× the
  MACs of `mn10`. **Not a CPU option.**

### B. Fine tier — beat/BPM and key

**Ceiling first.** `phaze-fzubr` measured fine-tier rhythm at **1.7%** and key at **0.1%** of total
analysis CPU, so replacing `RhythmExtractor2013` and `KeyExtractor` with something infinitely fast
recovers at most **about 1.8%** of the total. Fine-tier candidates are judged on accuracy and
dependency cost, **not** CPU. (Fine decode, 7.6%, is the stage that dominates the fine tier, and no
tracker replaces it.)

#### B1. `RhythmExtractor2013(method="degara")` — the in-place cheaper option, and why it breaks phaze

- **Source:** `MTG/essentia` `rhythmextractor2013.cpp` and `beattrackermultifeature.cpp`.
  `multifeature` builds several onset detection functions, each feeding a `TempoTapDegara`, and
  selects with `TempoTapMaxAgreement`. `degara` is a single tracker. That is structurally less
  work; **the speed difference is not measured anywhere read**.
- **Blocking integration fact:** the docstring says that for `degara` "there is no confidence
  estimation … the output confidence value is always 0". phaze stores BPM only `if confidence >
  0.0` (`_measure_fine_window`), so `degara` as-is would **null every fine-window BPM**. Using it
  would need a gate change. **Licence:** essentia library, **AGPL-3.0** (`COPYING.txt`), unchanged.

#### B2. Beat This! (ISMIR 2024)

- **Source:** arXiv 2407.21658, **read in full**; repo `CPJKU/beat_this`; PyPI `beat-this`.
  Lacuna does not index the paper.
- **Reported (paper):** no DBN post-processing; about **20 M parameters**, and a **~2 M-parameter**
  variant. Input mono 22.05 kHz, 128-bin mel, 50 frames per second, 30 s excerpts; output is
  frame-wise beat and downbeat probabilities, peak-picked. GTZAN (993 pieces), mean over 3 seeds:
  **beat F1 89.1, downbeat F1 78.3** (DBN baseline, Hung et al.: 88.7 / 75.6); the ~2 M model
  scores **88.8 / 77.2**. Continuity is **worse** than the DBN system: beat CMLt 79.8 vs 81.2,
  AMLt 89.8 vs 92.0, which the paper calls "disappointing". Cross-validated beat F1: Ballroom
  97.5, Beatles 94.5, Hainsworth 91.9, Harmonix 95.8. **The paper gives no inference time, FLOPs or
  throughput, and no tempo output; `phaze-0toy3.6` has since measured the cost (below).** madmom is not a baseline in the text read.
- **Models** (README): `final0–2` about 78 MB each, `small0–2` about 8.1 MB each. ONNX conversions
  exist on Hugging Face (`musetric/beat-this-onnx`) and are third-party.
- **Licence:** the GitHub licence API reports **MIT**, and the README License section says "The
  code and the published model weights are released under the MIT license", adding that some
  training files are fully copyrighted or under limited Creative Commons licences and that the user
  must judge whether that affects their use. **The operator accepted that caveat** (see "Decisions recorded").
- **Dependencies:** `pyproject.toml` lists numpy, torch, torchaudio, einops, rotary-embedding-torch
  and soxr; PyPI `beat-this` 1.1.0 `requires_dist` agrees. **madmom is not a dependency** (see
  "Reconciliation notes"). That is a new heavyweight runtime beside TensorFlow in every analysis
  image, and an arm64 cp313 PyTorch wheel would need checking.
- **Fit:** input is 22.05 kHz while the fine tier decodes at 44.1 kHz, so a second decode or a
  resample is needed. 30 s excerpts match the 30 s fine window. BPM would have to be derived from
  inter-beat intervals and its semantics re-validated against `RhythmExtractor2013`. A flat-RSS test
  across chunks would be required (D-09). **Rank: accuracy upgrade with real dependency cost; no
  CPU case.**
- **Measured since (`docs/spikes/phaze-0toy3.6-beat-this-beat-grids.md`, `final0`, `dbn=False`,
  GuitarSet CC BY 4.0 clips and synthetic fixtures, loaded machine):** beat F1 **0.779 vs 0.591**
  against `RhythmExtractor2013` on 39 GuitarSet clips and **0.997 vs 0.644** on 9 synthetic
  fixtures; **2.338 vs 0.239 cpu-s per 30 s window** (about 9.8×), a net of about **+2.1** because
  it replaces the rhythm half of the fine window; **no RSS growth** across 100 windows. It is
  better for a stored beat grid and still **not a CPU lever**. Not shown: archive music, `small0`,
  Linux arm64.

#### B3. madmom (RNN/DBN beat tracking, CNN key) and the Beat Transformer

- **Source:** Lacuna, *madmom: A New Python Audio and Music Signal Processing Library*
  (<https://lacuna.tiptreesystems.com/work/madmom-a-new-python-audio-and-music-signal/version/art_ab8c2b5164f74e1bb1fc72523ce5d572>):
  top MIREX ranks for beat tracking and tempo; lineage *Joint Beat and Downbeat Tracking with
  Recurrent Neural Networks*
  (<https://lacuna.tiptreesystems.com/work/joint-beat-and-downbeat-tracking-with-recurrent-neural/version/art_cecc54670cd545458420c0edbfb7f9a6>).
- **Licence:** `CPJKU/madmom` `LICENSE` and README: source **BSD**, all model and data files
  **CC BY-NC-SA 4.0**. The GitHub licence API reports `NOASSERTION` because the file holds two
  licences.
- **Packaging:** PyPI's latest is **0.16.1**, published as an **sdist only**. The Beat This! README
  says "the current version on PyPI only supports Python<3.10 and numpy<1.20" and points at git
  `main`. **Not installable from PyPI on phaze's Python 3.14 / 3.13 images.**
- **Beat Transformer** (Lacuna:
  <https://lacuna.tiptreesystems.com/work/beat-transformer-demixed-beat-and-downbeat-tracking/version/art_6ecbe62709484f6cbe2560ec25fa5c6e>):
  9.29 M parameters, +4 points downbeat F-measure on GTZAN over prior state of the art, but it needs
  **Spleeter five-stem separation first**, far more CPU than `RhythmExtractor2013`. Repo MIT per
  Lacuna.
- **Rank:** low. Rejected.

#### B4. BeatNet

- **Source:** Lacuna, *BeatNet: CRNN and Particle Filtering for Online Joint Beat Downbeat and Meter
  Tracking* (arXiv 2108.03576;
  <https://lacuna.tiptreesystems.com/work/beatnet-crnn-and-particle-filtering-for-online-joint>);
  repo `mjhydri/BeatNet`.
- **Reported:** GTZAN online beat F 75.44%, downbeat 46.49%; **5.23 s to process a 30 s clip**, on the
  authors' setup, about 4× faster than ungated sampling. That is slower than real time for a 30 s
  window, and accuracy is below Beat This! on the same dataset.
- **Licence:** **CC BY 4.0** (repo `LICENSE`, "Attribution 4.0 International"), which allows
  commercial use. **Packaging:** PyPI `BeatNet` 1.1.3 requires `numba ==0.54.1`, `madmom`, `torch`
  and `pytest` at runtime; a 2021 numba pin rules out Python 3.13 / 3.14 (inferred from the pin's
  age). **Rank: not deployable as packaged, and not faster.**

#### B5. All-In-One (beats, downbeats, structure)

- **Source:** Lacuna, *All-In-One Metrical and Functional Structure Analysis with Neighborhood
  Attentions on Demixed Audio*
  (<https://lacuna.tiptreesystems.com/work/all-in-one-metrical-and-functional-structure-analysis/version/art_965f08de744e4f5aa118140b13ad3ced>):
  Harmonix beat F1 **0.958**, downbeat F1 **0.915**, about **300 K** parameters (Lacuna summary);
  removing demixed input "significantly degrades performance".
- **Licence:** the GitHub licence API reports **MIT** for the repository and for Demucs. The README
  has no weights-licence section and its install step pulls madmom from the CPJKU repository, whose
  model files are NC-SA (whether all-in-one loads them was not checked).
- **Cost:** it requires **Demucs source separation**, **NATTEN** on Linux and **madmom**. The
  README's only speed figure is GPU ("RTX 4090 … 10 songs (33 minutes) in 73 seconds"). Source
  separation of multi-hour sets on CPU would cost far more than the whole current fine tier
  (inferred). **Rank: rejected on CPU grounds before licences are reached.**

#### B6. Key estimation

- **Current:** `KeyExtractor(profileType="edma")`. Essentia's `key.cpp` describes `edma` as
  "automatic profiles extracted from corpus analysis of electronic dance music … They normally
  perform better that Shaath's". The same source offers `bgate` / `braw` (Beatport-derived) and the
  classic `krumhansl` / `temperley` family, each a free swap with no new dependency. **Accuracy
  differences on phaze's corpus are unmeasured.**
- **CNN key:** **InceptionKeyNet** (Baumann; ISMIR 2021; Lacuna
  <https://lacuna.tiptreesystems.com/work/deeper-convolutional-neural-networks-and-broad-augmentation/version/art_286702dd8f0a41d68ca8f6f1415c2516>):
  CNN on CQT; weighted MIREX score **74.46%** on a small training set, **+2.72** points over the
  prior best CNN, on GiantSteps, KeyFinder, Isophonics and RockCorpus. It is compared only against
  CNN baselines, **not against a profile-correlation method such as edma**, and gives no cost
  figure. Code <https://github.com/stefan-baumann/inceptionkeynet>: GitHub licence API **MIT**; no
  separate weights licence was looked for. MERT and MuQ report key detection too, behind 95 M–330 M
  encoders and non-commercial weights. madmom's CNN key model inherits B3's packaging problem and
  CC BY-NC-SA weights.
- **Rank:** no CPU case (key is 0.1% of CPU, `phaze-fzubr`). A profile A/B (`edma` vs `bgate`) is a
  free accuracy experiment, if anyone wants one.

#### B7. TempoCNN (inside Essentia)

- **Source:** Essentia catalogue "TempoCNN": "Tempo classification (256 BPM classes, 30-286 BPM)",
  models `deepsquare-k16-3`, `deeptemp-k4-3`, `deeptemp-k16-3`. Same CC BY-NC-SA 4.0 catalogue
  statement, run by essentia's existing TF build.
- **Fit:** BPM only, no beat positions. phaze stores only BPM per fine window, so it would be a
  drop-in for that column. It is a neural network over audio, so unlikely to be cheaper than
  `RhythmExtractor2013` (inferred), which is 0.32 cpu-s per fine window. **Rank: accuracy
  alternative, no CPU case.**

### C. Inference acceleration

**`phaze-mqq5` already measured this on phaze's own models and host, so no spike is filed for
ONNX/int8, and `phaze-zaf2l` and `phaze-fzubr` already answer the fine-tier tracker CPU question.**
`phaze-mqq5`'s findings, verbatim figures:

- ONNX Runtime reproduces the TF predictions to **1.15 × 10⁻⁵** max per-patch with **0/34** top-1
  disagreements. Inside the real `analyze_file`, however, it peaked at **1.800 GiB** against
  TF-at-batch-32's **1.501 GiB** and **doubled the wall clock**.
- int8 dynamic quantisation: aggregated Δ **0.651** on `discogs-effnet-bs64-1`, **flipped the top-1
  genre label**, and ran **2× slower** (347.96 s vs 175.99 s). fp16 gave **−5.2%** peak and ran
  slower, because the CPU execution provider has no fp16 kernels for these ops.
- Batch size: already taken (32); it is a memory lever (**−35.2%** peak), not a CPU lever
  (**+1.1%** wall).
- TFLite: not measured. It shares ONNX's conversion cost.

**What is new since `phaze-mqq5`:** the Essentia catalogue ships **official ONNX artefacts**,
`discogs-effnet-bsdynamic-1.onnx` (dynamic batch, catalogue line 154) and ONNX MAEST models (line
289). The catalogue says MTG could not port the dynamic-batch effnet to TensorFlow, which is why
phaze runs a fixed `bs64` graph. An MTG-published ONNX effnet removes the "phaze owns a conversion
pipeline" cost for the graph that matters most under A1. That strengthens `phaze-mqq5`'s "ONNX for
arm64" case. It does **not** reverse its "ONNX is slower on x86 in `analyze_file`" measurement. No
timing for any of the published ONNX files was found. No source on int8 or other post-training
quantisation of musicnn, Discogs-EffNet or the heads was found; only format conversions (ONNX,
Core ML, MLX).

**Threads are a separate, measured lever** (`phaze-phjwi`, table above): 2 TF threads per pod give
1.81×–1.86× throughput on a 2-CPU budget at 5.5%–6.7% more CPU per window. It is independent of
every row below and is decided in `phaze-uv8s3`.

**OpenVINO:** Lacuna had nothing audio-specific, and no source reached reports OpenVINO on these
graphs. **No claim is made.**

______________________________________________________________________

## Licences

**phaze's baseline.** The Essentia models page states that all MTG models are **CC BY-NC-SA 4.0**,
also available under a proprietary licence on request, and points at the hosted
`https://essentia.upf.edu/models/LICENSE`. That file is **internally inconsistent**: it is titled
"Creative Commons Attribution-NonCommercial-NoDerivatives 4.0", but its summary says "Adapt - remix,
transform, and build upon the material" and links the `by-nc-sa` legal code. The `.json` metadata
for `EffnetDiscogs`, `MSD MusiCNN` and `mood happy` carries no `license` field. **Every source
agrees on NonCommercial; ShareAlike versus NoDerivatives is not settled by anything Essentia hosts,
and only UPF can settle it.** The Essentia library itself is AGPL-3.0. A search-result summary says
`licensing_information.html` states NC-ND; that page's text was not re-read.

| candidate | code | weights / models | source |
| --- | --- | --- | --- |
| MTG `*-discogs-effnet-1`, `*-msd-musicnn-1` heads, TempoCNN, `discogs-effnet-bsdynamic-1.onnx` | essentia AGPL-3.0 | NonCommercial; SA vs ND open (above) | catalogue, `models/LICENSE`, `COPYING.txt` |
| MAEST | AGPL-3.0 | `cc-by-nc-sa-4.0` | GitHub licence API; HF card `mtg-upf/discogs-maest-30s-pw-73e-ts` |
| Beat This! | MIT | MIT, with a training-data caveat (accepted, below) | GitHub licence API; README License section |
| EfficientAT / DyMN | MIT | no separate statement; weights in the repo's `v0.0.1` release | GitHub licence API; README |
| MERT | Apache-2.0 | CC BY-NC 4.0 | `yizhilll/MERT` `LICENSE`; HF card `m-a-p/MERT-v1-95M` |
| MuQ | MIT | CC BY-NC 4.0 | repository `LICENSE` / `LICENSE_weights`; HF card `OpenMuQ/MuQ-large-msd-iter` |
| LAION-CLAP | CC0-1.0 | CC0-1.0 (`lukewys/laion_clap`); `laion/clap-htsat-unfused` Apache-2.0 | GitHub licence API; HF cards; `phaze-ytgo.3` E3 |
| PaSST | Apache-2.0 | not looked for | `kkoutini/PaSST` `LICENSE` |
| madmom | BSD | CC BY-NC-SA 4.0 (model/data files) | `CPJKU/madmom` `LICENSE` |
| BeatNet | CC BY 4.0 | same repository licence | `mjhydri/BeatNet` `LICENSE` |
| all-in-one / Demucs | MIT / MIT | no weights statement; madmom model files NC-SA | GitHub licence API; README |
| Beat Transformer | MIT per Lacuna | not looked for | Lacuna resource entry |
| InceptionKeyNet | MIT | not looked for | GitHub licence API |

Nothing here changes that phaze's current models already bind it to non-commercial use, which is a
single-operator, private-network deployment today.

______________________________________________________________________

## Ranked shortlist

Scored **CPU saving × accuracy risk × licence fit**. Every saving is **inferred, not measured on
the candidate**; the "anchor" column names the nearest phaze measurement it leans on.

| # | candidate | CPU saving (inferred) | accuracy / meaning risk | licence vs today | integration | anchor |
| ---: | --- | --- | --- | --- | --- | --- |
| **1** | **A1 — effnet embedding + 11 `*-discogs-effnet-1` heads** | **highest**: 11 musicnn passes (≈ 97 of ≈ 100 cpu-s) → 0; +1 embedding tap, +11 small head evals (unmeasured) | **high, a meaning change**: different artefacts and label space; `phaze-8y6l8` found stock heads not a drop-in | same | two outputs from the effnet graph; heads swept per chunk; `positive_class_vector` adapter | `phaze-fzubr`; `phaze-ytgo.2` +1.6% |
| **2** | **A1b — a head fitted on the effnet or `msd-musicnn-1` embedding to reproduce musicnn mood scores** | as A1 for effnet; one musicnn pass (≈ 9 cpu-s) for the `msd-musicnn-1` variant | medium, **unproven**; fitting target left to `phaze-uv8s3` | same, but whether a fitted head is Adapted Material under ND is **open** (UPF) | small TF or NumPy/ONNX head over embedding rows | `phaze-fzubr`; `phaze-0toy3.4` |
| 3 | A1 variant — `msd-musicnn-1` + 11 `*-msd-musicnn-1` heads | high: 11 → 1 musicnn pass | medium: same MSD/musicnn family, still new artefacts (the deployed graphs are fine-tunes of this backbone) | same | as A1 with one extra graph | `phaze-fzubr`; `phaze-0toy3.4` |
| 4 | Share the mel input (`TensorflowInputMusiCNN`) across the eleven graphs | **0.89 cpu-s per window, 0.9% of the sweep** (`phaze-0toy3.4`, measured) | none with centred framing; `startFromZero=True` is off by 3.9 × 10⁻³ per patch | same | patching done in phaze; needs an equivalence check against the `phaze-28883` pin | `phaze-0toy3.4` |
| 5 | C — ONNX Runtime with MTG's `discogs-effnet-bsdynamic-1.onnx` | **none on x86** (`phaze-mqq5`: 2× wall) | none (1.15 × 10⁻⁵) | same weights | new runtime; drops the TF aarch64 / Python 3.13 pin | `phaze-mqq5` §5, §6 |
| 6 | B6 — key profile `bgate` / `braw` vs `edma` | ≈ 0 (key 0.1% of CPU) | unknown, both directions | same (essentia) | one keyword | `phaze-fzubr` |
| 7 | B2 — Beat This! (`final0`) | **negative, measured**: 2.338 vs 0.239 cpu-s per 30 s window, net about +2.1; no RSS growth (`phaze-0toy3.6`) | better grid on the data measured: beat F1 0.779 vs 0.591 on 39 GuitarSet clips, 0.997 vs 0.644 on 9 synthetic fixtures; not shown on archive music | **MIT**, training-data caveat accepted | adds PyTorch; derive BPM; vendor the checkpoint | `phaze-0toy3.6`; `phaze-fzubr` |
| 8 | A3 — MAEST (30 s) for style | none or negative (~87 M parameters, batch 1) | MTG-Jamendo mood ROC-AUC 78.1 vs 75.6, a different label space | weights CC BY-NC-SA; code AGPL | swap genre graph; style label set changes (400 → 519 for `-519l`) | none |
| 9 | B7 — TempoCNN | ≈ 0 to negative | unknown | same | essentia TF graph; BPM only | none |
| 10 | A4 / A5 — EfficientAT, DyMN, MERT, MuQ, CLAP, PaSST (ranked differently for a licence-clean route in `phaze-0toy3.5`; see "Recommendation") | n/a without new heads | n/a: no heads for phaze's 11 outputs | MIT / CC BY-NC / CC0 / Apache | needs training data phaze does not have | none |
| — | **A2 (refuted, `phaze-0toy3.4`)**, B1 `degara`, B3 madmom and Beat Transformer, B4 BeatNet, B5 All-In-One, int8 | — | — | — | **rejected**: weights differ in every layer / confidence gate / not installable on 3.13–3.14 / Spleeter or Demucs on CPU / slower than real time / `phaze-mqq5` accuracy fail | — |

**Rationale for the order.** A2, the only zero-drift route, is refuted, so every remaining large
saving on the mood arm changes what the stored scores mean. Ranks 1 to 3 trade the most CPU against
that change. The one precedent, the 33 → 11 change, shipped with its label diff as review evidence
(**65** changed coarse mood labels, `docs/essentia-analysis.md`), and that change dropped heads of
the same family, whereas A1 swaps the family. A1 leads because it uses published artefacts under
the licence phaze already holds; A1b ranks next because it targets the meaning problem directly but
is unproven and carries the open fitted-head licence question. Rank 4 is an exact but negligible saving (0.9%). Rank 5
is a deployment win and nothing more on CPU. Everything below rank 5 offers accuracy or nothing, and a
CPU-cut epic should not take them on that basis.

______________________________________________________________________

## Verdict

1. **The coarse mood sweep is where an alternative can matter** (84.5% of CPU, `phaze-fzubr`), and
   the lever is structural: stop running eleven full backbones over the same window. The only
   zero-drift way to do it, A2, is refuted (`phaze-0toy3.4`: the eleven graphs share no learned
   layer). A1 stays inside essentia, inside the licence phaze already holds, and inside the
   model-major, chunk-bounded loop, with no new runtime. A1b adds a small fitted head and an
   open licence question (is a fitted head Adapted Material under ND). **Every one of them
   changes what the stored scores mean**.
1. **No beat or key tracker is a CPU lever.** Rhythm is 1.7% and key 0.1% of CPU. Beat This! is the
   one credible accuracy upgrade (MIT, caveat accepted), and `phaze-0toy3.6` confirmed it is not a
   CPU lever: it costs about 9.8× `RhythmExtractor2013` per 30 s window (2.338 vs 0.239 cpu-s, a net
   of about +2.1) with no RSS growth, and adds a PyTorch dependency; madmom,
   BeatNet and All-In-One cannot be installed or run cheaply on phaze's Pythons, and the Beat
   Transformer and All-In-One need source separation. `phaze-0toy3.6` has since measured it.
1. **Runtime acceleration was settled by `phaze-mqq5`** and nothing found here overturns it. MTG's
   official ONNX effnet makes the arm64 migration cheaper. It does not make x86 faster.
1. **Bigger embeddings are not cheaper**, and none ships heads for phaze's eleven outputs.
1. **Licence fit is neutral to worse.** Looser: Beat This! (MIT), EfficientAT (MIT repository),
   BeatNet (CC BY), LAION-CLAP (CC0). The Essentia and MAEST weights are NonCommercial, as phaze
   already accepts.

## Recommendation for `phaze-uv8s3`

**Consider, in this order. Nothing below is measured here; measure each on the profiling
harness of `phaze-fzubr` before any swap.**

1. **Drop A2.** It is refuted by `docs/spikes/phaze-0toy3.4-musicnn-shared-frontend.md` (branch
   `wt/bead/issue/phaze-0toy3.4`, commit `2cafd2ca`). The zero-drift floor of the mood sweep stays
   at eleven backbone passes per window; threads (`phaze-phjwi`) are the zero-drift wall-time
   lever.
1. **Take the embedding route as a semantic decision, not an optimisation.** Candidates: A1 (stock
   MTG heads on the effnet embedding) and A1b (a head fitted on the effnet or `msd-musicnn-1`
   embedding), measured in one pass. Pricing: whether one effnet graph run can emit both outputs
   (`phaze-fzubr` left this unmeasured; `phaze-ytgo.2` measured +1.6%) and what the eleven heads
   cost. Behaviour: the stock heads are not a drop-in (`phaze-8y6l8`), so either use them and ask the
   operator whether the different mood definition is acceptable, with a published label diff in the
   protocol that `docs/essentia-analysis.md` used for 33 → 11, or fit a head. **This document does
   not recommend a fitting target**: the operator left that to `phaze-uv8s3`. Check class order and
   Softmax-vs-Sigmoid per head against `derive_mood` / `derive_style` / `derive_danceability`, and
   the `positive_class_vector` adapter. **Licence:** put to UPF the question in A2, "Licence" (BY-NC-SA
   or BY-NC-ND, and whether a fitted head is Adapted Material) before committing a fitted head.
1. **Do not build mel-input sharing for its CPU.** `phaze-0toy3.4` measured the saving at 0.89
   cpu-s of 96.8 per coarse window (0.9%), and phaze would then own the framing and patching that
   `TensorflowPredictMusiCNN` does internally.
1. **Blast radius (rule 4), to be written on the implementation bead.** Any of A1, A1b and the
   `msd-musicnn-1` variant changes the coarse scores for **every file analysed after it ships**. Files already analysed stay on the old
   models unless reanalysed, which is a mixed-population hazard for `set_projection` and
   similarity, and the implementation bead must name the test that bounds it. The single combined
   re-analysis in `phaze-uv8s3` is where that cost is paid once.
1. **Do not spend effort on rhythm or key** under this epic (`phaze-fzubr`: 1.8% together). If
   beat-grid work is wanted, it belongs to `phaze-7hcgn` and is already assessed in
   `docs/spikes/phaze-0toy3.6-beat-this-beat-grids.md` (F1 0.779 vs 0.591 on 39 GuitarSet clips and 0.997 vs 0.644 on 9 synthetic fixtures; 2.338
   vs 0.239 cpu-s per 30 s window; no RSS growth). That document recommends an operator-annotated
   check on real archive music before the grid format is fixed. A key-profile A/B is the only
   remaining fine-tier experiment.
1. **Keep the arm64 / ONNX question separate.** If it is taken up, start from MTG's
   `discogs-effnet-bsdynamic-1.onnx` rather than a phaze-owned conversion. It is a deployment
   decision, not part of this epic's CPU budget.
1. **Do not file** int8, MERT, MuQ, CLAP, PaSST, EfficientAT, Beat Transformer, BeatNet or
   All-In-One **under this CPU epic**: none of them cuts CPU or ships heads for phaze's outputs.
   That is a different question from `docs/spikes/phaze-0toy3.5-clean-room-mood.md`, which asks
   which routes avoid NonCommercial Essentia models and ranks CLAP second and EfficientAT third
   (behind an ISC musicnn backbone with heads on operator labels) for that licence purpose. Both
   can hold: those two are not CPU options, and they are licence-clean options.
1. **Licence actions before any adoption.** Ask UPF whether NC-SA or NC-ND governs the Essentia
   models phaze already ships, and, per `docs/spikes/phaze-0toy3.5-clean-room-mood.md` (route 0),
   ask UPF for the proprietary licence the catalogue offers for the existing models: it needs no
   engineering and changes no score. Its route 1 (the ISC `MSD_musicnn` backbone run once, plus
   eleven heads on labels the operator owns) is the licence-clean variant of A1b, with the same
   unmeasured accuracy. That spike also lists **training on stored musicnn scores, or using an
   Essentia head on another backbone, as not clean**, because those outputs and heads are
   NonCommercial. So if the licence matters, the A1b fitting target must be operator-owned labels,
   which `phaze-uv8s3` decides.

**Proxy warning, restated.** Every literature number above (mAP, ROC-AUC, F1, MACs, "two to three
times faster on CPU") was measured on public benchmarks and other hardware, usually on short
clips. phaze's corpus is multi-hour DJ and concert sets, decoded in 30 s / 180 s windows on a
contended 8-CPU node. Under `docs/design/0012-verification-fidelity-and-operator-attribution.md`
rule 3, none of these figures discharges an acceptance criterion. Only a run through
`analyze_file` on the real model set and real corpus files does.

______________________________________________________________________

## Follow-up spikes

Three spikes of epic `phaze-0toy3` ran after the two source runs, and this document is updated
for each:

- `docs/spikes/phaze-0toy3.4-musicnn-shared-frontend.md`: **A2 refuted.** 0 of 44 trainable
  tensors are identical across all 55 pairs of the 11 deployed graphs, so each is a full
  fine-tune of `msd-musicnn-1`. Only the mel input is exactly shareable, and it saves 0.89 cpu-s
  per window (0.9% of the sweep).
- `docs/spikes/phaze-0toy3.5-clean-room-mood.md`: **a licence-clean route exists**: an ISC musicnn
  backbone plus eleven heads on operator-owned labels, with the proprietary-licence request to UPF
  as route 0. Accuracy and head cost are unmeasured. Training on stored scores is not clean.
- `docs/spikes/phaze-0toy3.6-beat-this-beat-grids.md`: **Beat This! gives a better beat grid
  (F1 0.779 vs 0.591 on 39 GuitarSet clips) at about +2.1 cpu-s per 30 s window**, with no RSS
  growth. It is not a CPU lever, and the archive-music check is still owed.

## Decisions recorded

Each entry carries the question as put, the answer as given, the date and the durable record
(rule 2 of `docs/design/0012-verification-fidelity-and-operator-attribution.md`).

1. **Beat This! training-data caveat.** Question as put (dispatcher report): "Beat This!: someone
   has to accept or reject the training-data caveat in its README." Answer as given: "accepted".
   Date: 2026-11-13. Durable record: the description of bead `phaze-0toy3.3`.
1. **Head-fit target.** Question: which stored musicnn scores, or which operator-labelled sample,
   a fitted EffNet head should be fitted against. Answer as given (`AskUserQuestion` selected
   label): "Leave it to phaze-uv8s3". Date: 2026-11-13. Durable record: the description of bead
   `phaze-0toy3.3`. This document therefore recommends no target.
1. **One reconciled document, one PR.** Operator instruction in chat, 2026-11-13: "Reconcile them
   and do one PR." Durable record: the description of bead `phaze-0toy3.3`.

## Reconciliation notes

Where the cloud run (`816b2ff4`) and the local run (`d00b465a`) disagreed, and how this document
resolved it:

- **Does Beat This! need madmom?** An earlier local summary said its command-line tool did. The
  package's `pyproject.toml` lists no madmom dependency, and PyPI `requires_dist` agrees. **Resolved
  from `pyproject.toml`: it does not.** The madmom install problem in B3 is real, but it does not
  apply to Beat This!.
- **Essentia model licence.** The cloud run read "CC BY-NC-SA 4.0" from the catalogue; the local
  run read the hosted `models/LICENSE`, titled NC-ND but linking the NC-SA legal code. **Resolved
  from the hosted file: NonCommercial is agreed, SA vs ND is open, and UPF has to settle it.**
- **Per-graph CPU split.** The cloud run listed it as unmeasured; the local run quoted 102 / 259 /
  3 to 4 cpu-s from the epic. **Resolved from the in-tree `phaze-fzubr` and `phaze-phjwi`:** 96.8
  cpu-s for the 11 mood graphs (≈ 9.0 each), 3.67 for the genre EffNet; the 102 / 259 figures are
  not reproduced here, and nothing below depends on them.
- **Is the effnet-heads route "unmeasured" or "not a drop-in"?** The cloud run called its accuracy
  cost unmeasured; the local run cited `phaze-8y6l8`. **Resolved by treating the qualitative
  finding as established (it is repeated in `phaze-fzubr`) and the quantitative one (0.29,
  25 of 91) as quoted, not re-verified.**
- **Fine-tier ceiling.** The cloud run used `phaze-zaf2l`'s 97.7% decode share of one file's fine
  tier; `phaze-fzubr` gives total-CPU shares over six files. Both are kept, and the ceiling uses
  the fzubr shares (see "What phaze has already measured").
- **Ranking.** The cloud run ranked A2 then A1; the local run ranked "EffNet plus a fitted head"
  first. **Resolved by the A2 refutation** (`phaze-0toy3.4`): A2 is void, A1 leads and the fitted
  head follows, with its target left to `phaze-uv8s3` per the operator.
- **A2.** The cloud run's zero-drift hypothesis, recorded here as unverified, was refuted after this
  reconciliation by `docs/spikes/phaze-0toy3.4-musicnn-shared-frontend.md` (branch
  `wt/bead/issue/phaze-0toy3.4`, commit `2cafd2ca`).
- **Claims dropped.** The cloud run's MAEST "41.7 vs 37.4 mAP on MagnaTagATune" came from a
  Lacuna summary and could not be matched to the paper text read in full, so it is not carried.
  A search-snippet source for Beat This!'s F1 is replaced by the paper's own numbers.

## What this document could not source

- **Per-head accuracy** of the `*-discogs-effnet-1` and `*-msd-musicnn-1` heads. The `.json`
  sidecars read carry no accuracy or `license` field for the models checked.
- **The `phaze-8y6l8` figures** (levels shift by up to 0.29; dominant-mood agreement 25 of 91
  windows): the record is not in this tree, so they are quoted second-hand.
- **Absolute throughput for MAEST.** Beat This! inference cost is now measured
  (`phaze-0toy3.6`: 2.338 cpu-s per 30 s window on a loaded machine, an upper bound). Not
  measured there: `small0`, Linux arm64, and any real archive music.
- **An agreement figure for any fitted head** (EffNet or `msd-musicnn-1` embedding) against the
  stored scores. The mel-input cost is measured by `phaze-0toy3.4` (0.089 cpu-s), and A2 is refuted.
- **Whether one EffNet graph run can emit both outputs**, and the CPU cost of the eleven
  `TensorflowPredict2D` heads.
- **A comparison of any neural key model against `KeyExtractor(edma)`.**
- **Any OpenVINO figure**, any CPU timing for the published ONNX models, any int8 result for musicnn
  or the heads, and whether the musicnn graphs have ONNX or distilled equivalents.
- **Licence points primary sources could not settle:** Essentia models, NC-SA vs ND (above), and the
  text of `licensing_information.html` (not re-read); EfficientAT and all-in-one weights (no
  separate weights licence stated); training-data licences for LAION-CLAP and MuQ checkpoints and
  beyond the README caveat for Beat This!; weights licences for PaSST, Beat Transformer and
  InceptionKeyNet (not looked for).

None of the above is measured on phaze audio. Nothing here uses or names archive content.
