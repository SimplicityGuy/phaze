# Spike phaze-8ghbw: faster or better alternatives for phaze's music analysis stack

Status: research only. No product code was written, nothing was run on phaze audio, and nothing here was
measured on phaze's own corpus. Every accuracy and cost figure below is **as the cited source states it, on the
benchmark that source names**. Anything phaze has measured itself is labelled "phaze-measured" and comes from the
parent epic (phaze-0toy3), not from this spike.

Date: 2026-11-13. Parent epic: phaze-0toy3. Decision bead that consumes this: phaze-uv8s3.

## Question

Which newer or cheaper models and libraries could replace, or sit beside, the parts of phaze's per-window analysis
that cost CPU or limit accuracy, and which of them fit phaze's licence position?

Parts in scope:

- the mood graphs (musicnn, 11 in the default set, 33 in the ensemble), run on coarse windows;
- the Discogs-EffNet embedding and its heads (genre, and the heads that are *not* a drop-in for mood);
- `RhythmExtractor2013` (BPM and beats) on fine windows;
- `KeyExtractor` (key) on fine windows.

## How phaze's analysis is shaped (what any alternative has to plug into)

Read from `src/phaze/services/analysis.py`. Decision records live in the code and in
`docs/design/0007-windowed-analysis.md`.

| Tier | Window | Sample rate | What runs per window | Where |
|---|---|---|---|---|
| Fine | 30 s (min 15 s) | 44.1 kHz | `es.RhythmExtractor2013(method="multifeature")` and `es.KeyExtractor(profileType="edma")`, each constructed once per file and reused | `_analyze_fine_windows`, `_measure_fine_window` |
| Coarse | 180 s | 16 kHz | Essentia `TensorflowPredict*` graphs (`musicnn`, `vggish`, `effnet_discogs`) | `_analyze_coarse_windows`, `_run_model_sets_over_windows`, `_infer_one_window` |

Properties an alternative must respect:

- **D-07, exhaustive and chunk-bounded.** Every natural window of every file is analysed, however long the file.
  Fine decodes 60 windows per chunk and coarse 30 (`_FINE_CHUNK_WINDOWS`, `_COARSE_CHUNK_WINDOWS`); live PCM is
  bounded by the chunk, not the duration. No window cap, no stride, no sampled mode.
- **Model-major inference.** One `TensorflowPredict*` graph is resident at a time. It is built once, swept across
  every live window of the chunk, then released (`_sweep_one_model`, `_release_classifier`). A replacement
  embedding model keeps this shape or improves on it; it must not need more than one resident network.
- **D-08, progress-based liveness.** Nothing is killed for running long. The child heartbeats window completions,
  chunk decodes and model sweeps; only silence (`analysis_stall_timeout_sec`) kills it. A new model must emit a
  heartbeat per window or per sweep, and must not introduce a wall-clock bound.
- **D-09, peak RSS is chunk-bounded.** Essentia's streaming network must be disconnected after each chunk decode.
  A PyTorch or ONNX model has no Essentia network, but its own allocator behaviour must be shown flat across
  chunks on a multi-hour file. Duration-linear growth is a bug, not a sizing input.
- **Per-file interface.** Results are assembled by `_assemble_analysis_result` from per-window values. A beat
  tracker that emits frame-wise probabilities has to be reduced to what phaze stores today (BPM, beat positions).

Phaze-measured context from the parent epic (not re-measured here): the default 11-graph musicnn mood set costs
about 102 cpu-s per 180 s coarse window at one TF thread, the 33-graph ensemble about 259 cpu-s, and the
Discogs-EffNet embedding plus 11 heads about 3-4 cpu-s. EffNet heads are not a drop-in for mood (levels shift by up
to 0.29; dominant-mood agreement 25 of 91 windows). Two TF threads nearly halved wall time with unchanged results.
The decode / rhythm / key / genre / write share of a window is **not yet measured** (phaze-fzubr).

## Method

1. Lacuna (the operator's designated primary route): `search_lacuna` over papers and resources for audio
   embeddings, beat and downbeat tracking, key estimation, distillation, CLAP, music emotion; `get_paper` on the
   papers that mattered. Lacuna has no citation graph, so **no citation counts are claimed anywhere below**.
2. Web search and primary pages for what Lacuna does not hold: licences, model cards, ONNX and port availability,
   the Essentia model catalogue, and two papers read in full from arXiv (Beat This!, MAEST).
3. Rank by expected CPU saving x accuracy risk x licence fit, with integration notes against the interface above.

**Coverage gaps, stated plainly.** One query each did not return Beat This!, MAEST, EfficientAT's code, Discogs-EffNet
or allin1 from Lacuna, so those are cited from arXiv, Hugging Face and project pages instead. I did not find
trustworthy evidence on INT8 or other post-training quantisation of music-tagging models; see "What was not found".
Licences were then re-checked against primary sources on 2026-11-13: the GitHub licence API
(`gh api repos/<owner>/<repo>/license`) and each repository's README licence section, Hugging Face model-card
`license` fields (`huggingface.co/api/models/<id>`), and the files hosted on essentia.upf.edu. The remaining
unresolved licence points are listed under "What was not found".

## Evidence

### E1. Distilled and low-complexity embedding extractors (CPU cost)

- **Efficient Large-scale Audio Tagging via Transformer-to-CNN Knowledge Distillation** (EfficientAT; Schmid,
  Koutini, Widmer; arXiv 2211.04772). Lacuna:
  <https://lacuna.tiptreesystems.com/work/efficient-large-scale-audio-tagging-via-transformer>.
  MobileNetV3 students distilled from a PaSST ensemble; reports 0.483 mAP on AudioSet (single-model state of the art
  at publication), a width-1.0 model matching one PaSST at 4.88 M vs 87 M parameters, roughly 100x fewer MACs than
  PaSST-S, and 4,767 vs 78 clips/s on an A100 GPU. The paper says downstream transfer and embedding quality were not
  evaluated there.
- **Low-Complexity Audio Embedding Extractors** (same authors; arXiv 2303.01879). Lacuna:
  <https://lacuna.tiptreesystems.com/work/low-complexity-audio-embedding-extractors>.
  Embeddings from MobileNetV3 mid-level blocks plus a time-averaged log-mel vector, evaluated on the HEAR 2021
  benchmark (19 tasks). `mn10` (4.88 M parameters) reaches an average normalised score of 86.22%. The tiny `mn01`
  needs about 20 M MACs per 10 s of audio and is said to perform comparably to EffNet-B2 (900 M MACs) and OpenL3
  (780 M MACs). Final-layer features generalise worse than mid-level ones, and scores plateau near `mn30`. The
  paper reports no mood-specific figure.
- Code and weights: <https://github.com/fschmid56/EfficientAT>. The GitHub licence API reports **MIT** for the
  repository. The README has no separate weights-licence statement, and the pre-trained weights are published in
  the repository's `v0.0.1` release, so MIT is the only stated term; the AudioSet and ImageNet data the weights were
  trained on were not examined. Third-party Core ML and MLX ports exist on Hugging Face (for example
  `schism-audio/efficient-at-mn40-coreml`).

Read-across: the literature supports "one small CNN embedding, many cheap heads" as a way to cut CPU by an order of
magnitude or more against a per-task graph ensemble. It does not say a given small embedding reproduces phaze's
musicnn mood scores; that is untested.

### E2. Music-specific foundation models (accuracy, not CPU)

- **MERT** (Li et al.; ICLR 2024). Lacuna:
  <https://lacuna.tiptreesystems.com/work/mert-acoustic-music-understanding-model-with-large-scale/version/art_06cec1c9f8104d2ab80d3338f679a946>.
  Transformer, 95 M and 330 M parameter models, 14-task benchmark including genre (GTZAN), key (Giantsteps),
  emotion (Emomusic) and beat tracking. Trained on 5 s segments. Weights `m-a-p/MERT-v1-95M` carry a **CC BY-NC 4.0**
  tag on Hugging Face (<https://huggingface.co/m-a-p/MERT-v1-95M>).
- **MuQ** (Zhu et al.; IEEE TASLP). Lacuna:
  <https://lacuna.tiptreesystems.com/work/muq-self-supervised-music-representation-learning-with-mel/version/art_a238830c06ed4105a9051bc587fa695b>.
  Reports gains over MERT and MusicFM on MARBLE tasks (genre, key, emotion, tagging, structure) and a MuQ-MuLan
  music-text model with 79.3 ROC-AUC zero-shot on MagnaTagATune. Repository states code MIT and weights CC BY-NC
  4.0 (<https://github.com/tencent-ailab/MuQ>); the released checkpoint is ~300 M parameters trained on the Million
  Song Dataset and the repository notes it may underperform the paper's larger-data model. Licence, from the
  repository README and `LICENSE` / `LICENSE_weights` files: code MIT, weights CC-BY-NC 4.0; the Hugging Face card
  `OpenMuQ/MuQ-large-msd-iter` lists `cc-by-nc-4.0`.
- **LAION-CLAP** (Wu et al.; ICASSP 2023). Lacuna:
  <https://lacuna.tiptreesystems.com/work/large-scale-contrastive-language-audio-pretraining/version/art_ea3043e4e5884e708a62c3c7b4acd8fb>.
  Text-prompted zero-shot labels. The music checkpoint is an HTSAT-base backbone. The GitHub licence API reports
  **CC0-1.0** for `LAION-AI/CLAP`, and the Hugging Face card `lukewys/laion_clap` lists `cc0-1.0`
  (<https://huggingface.co/lukewys/laion_clap>). A different checkpoint, `laion/clap-htsat-unfused`, lists
  `apache-2.0`. The licences of the audio the checkpoints were trained on were not examined.
- **Layer-wise investigation of large-scale self-supervised music models** (Lacuna:
  <https://lacuna.tiptreesystems.com/work/layer-wise-investigation-of-large-scale-self-supervised/version/art_988a46f457864c42822bb733430eea05>)
  was found but not read in depth; recorded as a lead for choosing which layer to pool if a SSL model is ever
  adopted.

These are accuracy upgrades with 95 M-330 M-parameter transformers. None is a CPU saving against a ~4 M-parameter
EffNet or MobileNet, and the music-specific ones carry non-commercial weights.

### E3. Discogs-EffNet and MAEST (the incumbent family)

- Discogs-EffNet and its heads are Essentia models; the models page lists them, and says some are also published as
  TensorFlow.js and ONNX, with an ONNX Discogs-EffNet of dynamic batch size (<https://essentia.upf.edu/models.html>).
  The models page states that all MTG models are **CC BY-NC-SA 4.0**, also available under a proprietary licence on
  request, and points at the hosted `https://essentia.upf.edu/models/LICENSE`. That file is read and is
  **internally inconsistent**: it names "Creative Commons Attribution-NonCommercial-NoDerivatives 4.0", but its
  summary says "Adapt - remix, transform, and build upon the material" and links the `by-nc-sa` legal code. The
  `.json` metadata for `EffnetDiscogs`, `MSD MusiCNN` and `mood happy` carries **no `license` field**. What every
  source agrees on is NonCommercial; whether the models are ShareAlike or NoDerivatives is not settled by anything
  Essentia hosts, and only UPF can settle it. The Essentia library itself is AGPL-3.0 (`MTG/essentia`,
  `COPYING.txt`). A search-result summary says the separate licensing page (`licensing_information.html`) states
  NC-ND; I did not re-read that page's text.
- **MAEST** (Alonso-Jimenez et al.; ISMIR 2023, arXiv 2309.16418; read in full via the arXiv HTML). About 87 M
  parameters, 5/10/20/30 s inputs, best embeddings in the middle blocks (block 7 used). Reported ROC-AUC / mAP on
  MTG-Jamendo mood/theme: EffNet-B0 baseline 75.6 / 13.6, MAEST-30s 78.1 / 15.4; MTG-Jamendo genre 87.7 / 19.9 vs
  88.2 / 21.6. Speed claims: MAEST-30s "overcomes the throughput of standard CNN architectures by two to three times
  while keeping higher mAP" under strong patchout, measured on a CPU that the paper does not name, and "double the
  inference speed of an EfficientNet-B0 baseline". **No absolute throughput is stated**, and the strong-patchout
  condition is a different operating point from full-length embeddings. Weights: `cc-by-nc-sa-4.0` on the Hugging Face card
  (<https://huggingface.co/mtg-upf/discogs-maest-30s-pw-73e-ts>). Code: the GitHub licence API reports **AGPL-3.0**
  for `palonso/MAEST`, which is consistent with the PyPI inference package; the repository README has no licence
  section.
  Note the paper's own text says MAEST beats the baseline on every task while its Table 5 shows one mAP cell at
  32.7 vs 32.8; treat the headline as approximate.
- The paper's parent embedding task here is MTG-Jamendo mood/theme, a **different label space** from phaze's
  musicnn mood graphs; a better score there says nothing about agreement with phaze's stored mood scores.

### E4. Beat and downbeat trackers (replacing `RhythmExtractor2013`)

- **Beat This!** (Foscarin, Schluter, Widmer; ISMIR 2024, arXiv 2407.21658; read in full via arXiv HTML). No DBN
  post-processing. About 20 M parameters, and a ~2 M-parameter variant. Input: mono 22.05 kHz, 128-bin mel, 50 frames
  per second, 30 s excerpts; output is frame-wise beat and downbeat probabilities, peak-picked. Test on GTZAN (993
  pieces), mean over 3 seeds: beat F1 89.1, downbeat F1 78.3 (the DBN baseline, Hung et al., 88.7 / 75.6); the ~2 M
  model scores 88.8 / 77.2. Continuity is **worse** than the DBN system: beat CMLt 79.8 vs 81.2, AMLt 89.8 vs 92.0,
  and the paper calls this "disappointing". Cross-validated beat F1 on Ballroom 97.5, Beatles 94.5, Hainsworth 91.9,
  Harmonix 95.8. **The paper gives no inference time, FLOPs or throughput, and no tempo output.** madmom is not a
  baseline in the text I read. Package: <https://pypi.org/project/beat-this/>; ONNX conversions exist on Hugging
  Face (<https://huggingface.co/musetric/beat-this-onnx>) and are third-party. Licence: the GitHub licence API reports **MIT**
  for `CPJKU/beat_this`, and the README's License section says "The code and the published model weights are
  released under the MIT license", adding that some training files are fully copyrighted or under limited Creative
  Commons licences and that the user must judge whether that affects their use case. The package's `pyproject.toml`
  dependencies are numpy, torch, torchaudio, einops, rotary-embedding-torch and soxr; it does not mention madmom
  (an earlier summary said its command-line tool needs madmom; that was not borne out by the file).
- **Beat Transformer** (Zhao, Xia, Wang; ISMIR 2022). Lacuna:
  <https://lacuna.tiptreesystems.com/work/beat-transformer-demixed-beat-and-downbeat-tracking/version/art_6ecbe62709484f6cbe2560ec25fa5c6e>.
  9.29 M parameters, linear-complexity dilated attention, +4 points downbeat F-measure on GTZAN over prior state of
  the art. It needs **Spleeter source separation into five stems first**, which is far more CPU than
  `RhythmExtractor2013`. Repo MIT per Lacuna
  (<https://lacuna.tiptreesystems.com/resource/zhaojw1998-beat-transformer/art_f47e7bec20a54318af3e2e415e4ec80c>).
- **BeatNet** (Heydari, Cwitkowitz, Duan; arXiv 2108.03576). Lacuna:
  <https://lacuna.tiptreesystems.com/work/beatnet-crnn-and-particle-filtering-for-online-joint>. CRNN plus particle
  filtering. GTZAN online beat F 75.44%, downbeat 46.49%; 5.23 s to process a 30 s clip, about 4x faster than
  ungated sampling. That is slower than real time for a 30 s window on the authors' setup, and the accuracy is below
  Beat This! on the same GTZAN. Not a CPU saving.
- **Joint Beat and Downbeat Tracking with RNNs** (Bock, Krebs, Widmer; ISMIR 2016), the madmom lineage: Lacuna
  <https://lacuna.tiptreesystems.com/work/joint-beat-and-downbeat-tracking-with-recurrent-neural/version/art_cecc54670cd545458420c0edbfb7f9a6>.
  The `CPJKU/madmom` `LICENSE` file and README say source code is BSD and all model and data files are
  **CC BY-NC-SA 4.0** (<https://github.com/CPJKU/madmom>); the GitHub licence API reports `NOASSERTION` because the
  file holds two licences.
- **allin1** (mir-aidj/all-in-one): tempo, beats, downbeats and segments, but it requires Demucs demixing, NATTEN
  and madmom (<https://github.com/mir-aidj/all-in-one>); the GitHub licence API reports **MIT** for the repository
  and for Demucs (`facebookresearch/demucs`). The README has no licence section for the weights, and its install
  step pulls madmom from the CPJKU repository, whose model files are NC-SA (whether allin1 loads them was not
  checked). Demixing makes it strictly more
  expensive than the current rhythm path, so it is out on CPU grounds before licences are reached.

### E5. Key estimation (replacing `KeyExtractor(edma)`)

- **InceptionKeyNet** (Baumann; ISMIR 2021). Lacuna:
  <https://lacuna.tiptreesystems.com/work/deeper-convolutional-neural-networks-and-broad-augmentation/version/art_286702dd8f0a41d68ca8f6f1415c2516>.
  CNN on CQT; weighted-average MIREX score 74.46% on a small training set (+2.72 points over the prior best CNN) on
  GiantSteps, KeyFinder, Isophonics and RockCorpus. Compared only against CNN baselines, **not against a
  profile-correlation method such as edma**. No cost figure is given. Code: <https://github.com/stefan-baumann/inceptionkeynet>
  (the GitHub licence API reports **MIT** for `stefan-baumann/inceptionkeynet`; no separate weights licence was looked for).
- MERT and MuQ report key detection on Giantsteps, but behind 95 M-330 M-parameter encoders and non-commercial
  weights; not a saving.
- Phaze's key work is a per-30 s fine-window `KeyExtractor`. Whether it is a large share of the window is the open
  profiling question (phaze-fzubr); until that is answered there is no case for replacing it.

### E6. Mood from cheaper embeddings

- **Comparison and Analysis of Deep Audio Embeddings for Music Emotion Recognition** (Koh, Dubnov; AffCon @ AAAI
  2021). Lacuna:
  <https://lacuna.tiptreesystems.com/work/comparison-and-analysis-of-deep-audio-embeddings/version/art_acf064a85cf845779e64a18b2296fd4c>.
  Shallow classifiers on L3-Net and VGGish embeddings beat MFCCs across four small datasets (for example 88.0% with
  L3-Net + SVM on the bi-modal emotion set, 72.0% with RF on the 4Q set). Supports "embedding + shallow head" for
  mood in general; the datasets are 162-900 clips and are not a stand-in for phaze's musicnn label space.
- MTG-Jamendo mood/theme heads on Discogs-EffNet (mood ROC-AUC 75.6 in the MAEST paper's EffNet-B0 baseline) are
  what the Essentia catalogue offers; phaze's own comparison already shows they are not equivalent to its stored
  musicnn scores.

## Verdict

1. **There is no drop-in external replacement that is cheaper and licence-clean for the musicnn mood sweep.** Every
   credible alternative embedding either is a larger transformer (MERT, MuQ, MAEST, CLAP) or needs a head phaze does
   not have (EfficientAT and similar). The CPU saving available is the one phaze already measured: moving the mood
   work onto the Discogs-EffNet embedding it already runs (about 41x less over 91 windows, phaze-measured).
2. **The real problem on that path is the head, not the embedding.** The EffNet heads answer a different question
   (different label space, shifted levels, 25 of 91 dominant-mood agreement). The intended behaviour is "mood
   scores that mean what the musicnn scores meant", not merely "a mood score with no error", so a cheaper head only
   counts as a saving if it reproduces that meaning. The literature here supports shallow heads on mid-level
   embeddings generally (E1, E6) but gives no evidence for this specific mapping; it has to be fitted and measured
   on phaze data, by the owner of that data.
3. **For rhythm, one credible alternative exists: Beat This!**, with the caveats that its inference cost and tempo
   output are not reported, its continuity metrics are worse than a DBN system, and it adds a PyTorch (or ONNX)
   runtime beside Essentia. Whether it saves anything depends on the rhythm share of a fine window, which is not
   measured.
4. **For key, there is no justified replacement yet.** InceptionKeyNet was not compared with edma and has no cost
   figure.
5. **Licence fit is mostly neutral to worse.** Phaze's Essentia models are NonCommercial: CC BY-NC-SA 4.0 per the
   models page, while the hosted LICENSE file contradicts itself between NC-ND and NC-SA (see E3). MAEST weights are
   CC BY-NC-SA 4.0 and its code AGPL-3.0; MERT and MuQ weights are CC BY-NC 4.0 (no ShareAlike, equally
   non-commercial), MuQ code MIT. Looser: Beat This! (code and weights MIT, per the repository README, with the
   training-data caveat) and EfficientAT (MIT repository). Loosest: LAION-CLAP, CC0-1.0 repository and checkpoint.
   Nothing here changes that phaze's current models already bind it to non-commercial use, which is a
   single-operator, private-network deployment today.

## Ranked shortlist

Ranking is by expected CPU saving x accuracy risk x licence fit, on a qualitative High/Medium/Low scale. "Saving" is
relative to phaze's present stack and is **expected, not measured** unless it says phaze-measured.

| # | Candidate | Replaces | Expected CPU saving | Accuracy risk | Licence fit vs CC BY-NC-SA 4.0 | Verdict |
|---|---|---|---|---|---|---|
| 1 | **Discogs-EffNet embedding (already deployed) + a mood head fitted to reproduce the musicnn mood scores** | 11- or 33-graph musicnn sweep | High (about 41x on the model sweep, phaze-measured for the embedding plus Essentia heads) | Medium: the Essentia heads fail; a fitted head is unproven (agreement was 25/91 with the stock heads) | Same family and licence as today (no change); any labels used to fit the head need their own licence check | **Adopt as the primary path, gated on a head-fit measurement** |
| 2 | **Beat This!** (full, or the ~2 M variant) | `RhythmExtractor2013` (BPM and beats) | Unknown: no cost reported; rhythm's share of a window unmeasured | Medium: beat/downbeat F1 on GTZAN is higher than a DBN system, continuity worse; no tempo output, so BPM must be derived and re-validated | Looser: MIT code and weights (README), training-data caveat; no madmom dependency in `pyproject.toml` | **Shortlist; benchmark on a rhythm-cost profile first; couples to the sub-window beat-grid work (phaze-7hcgn)** |
| 3 | **EfficientAT MobileNetV3 (`mn10`, `mn04`, or `mn01`) as an alternative cheap embedding** | Discogs-EffNet (if EffNet itself proves a cost) | Low-Medium: EffNet is already only ~3-4 cpu-s per window; the gain over it is not shown | High for mood: needs a head trained on labels phaze does not own; AudioSet-trained | Looser: MIT repository; weights have no separate stated licence; label data licence is a new question | Hold; revisit only if the profile shows EffNet is a material cost |
| 4 | **MAEST (30 s)** | Discogs-EffNet embedding and heads | None or negative: ~87 M parameters; speed claims only vs CNNs under strong patchout, hardware unnamed | Low-Medium: MTG-Jamendo mood ROC-AUC 78.1 vs 75.6 for the baseline | Weights CC BY-NC-SA 4.0 (same as today); code AGPL-3.0 | Accuracy option, not a CPU saving; defer |
| 5 | **InceptionKeyNet** | `KeyExtractor(edma)` | Unknown | Unknown (no edma comparison) | MIT repository | Defer until the key share is profiled |
| 6 | **MERT / MuQ / CLAP** | embeddings / zero-shot mood | Negative (95 M-330 M parameters, HTSAT-base) | Low for quality, but mood label space differs | MERT and MuQ weights CC BY-NC 4.0; CLAP CC0-1.0 | Reject on CPU grounds |
| 7 | **Beat Transformer, BeatNet, allin1, madmom RNN+DBN** | `RhythmExtractor2013` | Negative or none (Spleeter or Demucs demix; 5.23 s per 30 s clip for BeatNet; DBN) | Higher or equal | madmom model files CC BY-NC-SA 4.0 (code BSD); allin1 MIT but depends on madmom; Beat Transformer MIT | Reject |

Ordering note: #1 outranks #2 because the saving is large and already evidenced on phaze's own audio; #2 outranks
the rest because it is the only external model with a stated accuracy gain on the rhythm path and a permissive
licence, but its CPU effect is unknown, so it cannot rank higher until the profile exists.

## Integration notes per entry

1. **EffNet + fitted mood head.** No new runtime: the `effnet_discogs` branch of `_construct_classifier` already
   runs `TensorflowPredictEffnetDiscogs`, and D-07/D-08/D-09 are already satisfied for it. The new piece is a small
   head over the embedding; it can be a TF graph (kept inside `TensorflowPredict2D`) or a NumPy/ONNX head applied to
   the embedding rows, which would avoid a second TF session. Targets for fitting are the stored musicnn mood
   scores, so the head's job is to reproduce an existing, auditable signal; the acceptance test is agreement and
   level error against the stored scores on held-out windows, not "no error". Both variant sets exist in stored
   rows (11-graph and 33-graph); a decision is needed on which set is the target. Re-analysis cost is combined with
   the other changes in phaze-uv8s3.
2. **Beat This!** Runs on 22.05 kHz mono at 50 fps; phaze's fine tier decodes at 44.1 kHz, so a second decode or a
   resample of the fine window is needed. The model sees 30 s excerpts, which matches the 30 s fine window, so
   per-window integration is natural. Output is frame-wise; phaze would need a peak-picking step and a BPM
   derivation from inter-beat intervals, and a decision on tempo semantics versus `RhythmExtractor2013`'s output.
   A PyTorch dependency is a new wheel in the platform matrix (including the arm64 agent image pinned to 3.13); the
   ONNX route would avoid that but the available ports are third-party. No D-09 behaviour is known for it: it needs
   a flat-RSS test across chunks like the existing gated-decode tests.
3. **EfficientAT.** Would be a second embedding runtime beside TF. Mean-pooled mid-level blocks plus the log-mel
   vector (per the paper) give a fixed-size per-window vector. Not worth the runtime unless #1's profile shows
   EffNet to be a hot spot.
4. **MAEST.** Essentia exposes it, so integration would be lowest-risk of the transformers, but patchout settings
   decide whether it is faster than EffNet at all; none of that is demonstrated for a 180 s window on CPU.
5. **InceptionKeyNet.** Needs a CQT front end and a PyTorch or TF model; per-30 s window; no cost data.

## Recommendation

1. **Proceed with #1 in the decision bead (phaze-uv8s3), scoped as a head-fit problem.** Measure agreement and level
   error of a head fitted on EffNet embeddings against stored musicnn scores, before committing to the re-analysis.
   If it cannot be made to agree acceptably, ask the operator whether the semantic change (a different mood
   definition) is acceptable rather than shipping a lower-cost head as if it were equivalent.
2. **Do not adopt any external replacement for rhythm or key until the profile (phaze-fzubr) says those parts matter.**
   If rhythm is material, benchmark Beat This! (full and ~2 M) on phaze-owned synthetic or licensed fixtures for
   time, RSS and BPM agreement, and confirm its licence from the repository file.
3. **Drop MERT, MuQ, CLAP, Beat Transformer, BeatNet and allin1 from consideration** for this cost problem.
4. **Licence actions before any adoption:** ask UPF which of NC-SA or NC-ND governs the Essentia models phaze already
   ships (the hosted LICENSE contradicts itself), and, for Beat This!, have the operator accept or reject the
   training-data caveat in its README. The other repository and model-card licences above are now read from
   primary sources.
5. Cheap non-model levers already measured by the epic (TF thread count; model-major batching) are independent of
   this list and should be decided first.

## What was not found

- Any source on INT8 or other quantisation of musicnn, Discogs-EffNet or the heads, or its accuracy effect on
  mood. Only format conversions (ONNX, Core ML, MLX ports) were found, and for Discogs-EffNet an ONNX model with
  dynamic batch size is listed by Essentia; no CPU timing for any of them.
- Inference cost of Beat This! (the paper reports training cost only).
- Absolute throughput for MAEST.
- A comparison of any neural key model against `KeyExtractor(edma)`.
- Whether the musicnn graphs have ONNX or distilled equivalents.

Licence points that primary sources could not settle:

- Essentia models, NC-SA vs NC-ND: the hosted `models/LICENSE` contradicts itself and the model metadata has no
  license field (E3). Only UPF can resolve it. I did not re-read the text of `licensing_information.html`.
- EfficientAT and allin1 pre-trained weights: no separate weights licence is stated in either README, so the
  repository MIT is the only stated term; the licences of their training data were not examined.
- Training-data licences for Beat This!, LAION-CLAP and MuQ checkpoints: not examined beyond the Beat This! README
  caveat quoted in E4.

None of the above is measured on phaze audio. Nothing here uses or names archive content.

## Sources

Lacuna entities (canonical URLs are inline above): MERT, MuQ, LAION-CLAP, EfficientAT distillation, Low-Complexity
Audio Embedding Extractors, InceptionKeyNet, music emotion embeddings comparison, Beat Transformer (paper and
repository), BeatNet, Bock et al. 2016.

Primary and project pages: Beat This! arXiv 2407.21658 and `beat-this` on PyPI; MAEST arXiv 2309.16418 and the
`mtg-upf` Hugging Face cards; Essentia models and licensing pages; madmom repository; MuQ repository;
EfficientAT repository; `m-a-p/MERT-v1-95M`; `lukewys/laion_clap`; all-in-one repository.
