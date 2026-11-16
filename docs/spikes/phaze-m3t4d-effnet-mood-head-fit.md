# phaze-m3t4d — a mood head on the EffNet embedding, measured against the stored musicnn scores

Spike, 2026-11-13 to 2026-11-16. Bead `phaze-m3t4d`, epic `phaze-t04h6`. Production access as approved for the
molecule: the database read-only (`BEGIN READ ONLY`, a statement timeout, `SELECT` only, `ROLLBACK`) on `host-prod`,
and archive audio only inside throwaway containers of the deployed image on the archive host `host-store`.
Aggregates only; no archive identifier appears here.

## Verdict: FAIL, 21 of 25 approved gates

A NumPy MLP head on the Discogs-EffNet embedding, trained on **1,651 files / 11,712 coarse windows**, reproduces the
stored 11-graph musicnn scores on **351 held-out files / 2,455 windows** well enough to pass 21 of the 25
operator-approved gates. It fails four, each with its whole 95% confidence interval on the wrong side of the
threshold: mean absolute error for **happy**, **gender** and **voice_instrumental**, and the **energy** scalar.
Under the approved rule (all gates required, decided on the point estimate), **the head is not adopted.** The FAIL is
recorded in `docs/design/0028-effnet-mood-head-not-adopted.md`.

| | |
|---|---|
| Gates passed | 21 / 25 (10 of 10 correlation, 8 of 11 MAE, 3 of 3 dominant-mood, 0 of 1 energy) |
| Gates failed (CI wholly past the threshold) | MAE happy, MAE gender, MAE voice_instrumental, energy MAE |
| Passed with the threshold inside the CI (flagged, as approved) | r happy, r gender, MAE relaxed, MAE party, dominant mood |
| float16 embedding instead of float32 | no effect: largest metric change 3.3 × 10⁻⁶ |

## Approvals, as recorded on the bead before the held-out look

Every decision below is an `AskUserQuestion` put by the dispatcher; the durable record of each is a comment on bead
`phaze-m3t4d`, which quotes the question in full.

| Date | Question (abridged; full text on the bead) | Answer as given (selected option label) |
|---|---|---|
| 2026-11-13 | the proposed pass thresholds, "all on 350 held-out test files and ALL required … Approve these?" | "Approve as proposed (Recommended)" (decided on the point estimate; any threshold inside its CI is flagged) |
| 2026-11-13 | the pilot container runs on the archive host | "GO, up to 2 extra rounds (Recommended)" |
| 2026-11-13 | the pilot "now looks like 15-30 h of wall time, not the ~8.5 h you approved … How should it proceed?" | "Continue as is (Recommended)" |
| 2026-11-14 | the VAL learning curve and its projection, options A test now / B one more round / C both rounds | "C: both rounds" |
| 2026-11-15 | round 2 stalled behind production load on the archive host (1,651 of 1,801 training files done) | "Stop at 1,651, test now (Recommended)" |

The thresholds were approved on 2026-11-13. The held-out targets were first read from the database on 2026-11-16,
immediately before the single evaluation. Nothing was tuned after that read.

### The approved thresholds

All on the held-out files, archive-weighted point estimate, all required:

- **Pearson r** ≥ 0.90 for the seven moods and danceability; ≥ 0.85 for gender and voice_instrumental; tonality r is
  reported, not gated.
- **MAE** ≤ 0.3 × the score's stored standard deviation: acoustic 0.034, electronic 0.051, aggressive 0.068,
  relaxed 0.041, happy 0.046, sad 0.056, party 0.055, danceability 0.047, gender 0.042, tonality 0.022,
  voice_instrumental 0.053.
- **Dominant mood** (argmax of the seven mood scores against the stored label): ≥ 85% of windows; ≥ 95% where the
  stored top two moods differ by ≥ 0.10; macro recall ≥ 0.70 over the seven classes.
- **Energy** (`set_projection` energy with the tempo term held at zero on both sides): MAE ≤ 0.03.

For calibration when they were proposed: the two deployed musicnn variants (msd against mtt) agree with each other at
r 0.44 (tonality) to 0.92 (aggressive) over 107,327 windows, and 48.5% of stored windows have a top-two mood margin
under 0.10, so a small score error flips the dominant mood on about half the archive.

## Data

### The population

Targets are the stored **11-graph (msd) coarse-window scores**, the deployed set. Measured read-only on 2026-11-13:
the msd stratum is **17,133 files / 330,660 coarse windows / 16,181.0 h**, all on the archive host. 33-graph rows are
excluded (no file mixes the two). In this stratum, 0 windows have a stored label that disagrees with the argmax of
their own scores, so the `phaze-8y6l8` exclusion removed nothing. Files by stored dominant mood: electronic 9,225,
aggressive 5,168, party 849, relaxed 693, sad 597, happy 544, acoustic 57.

### The split: by file, stratified

Files, never windows, were split, stratified over stored file-dominant mood × five duration buckets (< 10 min,
10–30 min, 30–60 min, 1–2 h, > 2 h), with a per-mood floor, in this order from one fixed seed: TEST, then VAL, then
three 600-file training rounds. Up to 8 windows per file were sampled with a per-file seeded draw.

| Split | Files | Windows | Audio hours (whole files) | Role |
|---|---|---|---|---|
| TEST | 351 | 2,455 | 311.1 | the single held-out evaluation |
| VAL | 349 | 2,448 | 294.8 | head selection and every learning curve |
| train round 0 | 601 | 4,220 | 537.1 | |
| train round 1 | 600 | 4,238 | 540.0 | |
| train round 2 (stopped) | 450 of 600 | 3,254 | 375.7 | stopped by operator decision 2026-11-15, recorded on bead phaze-m3t4d |
| **training total** | **1,651** | **11,712** | | |

Every file embedded cleanly: 2,351 of 2,351 attempted files, 0 windows skipped, 0 errors. The three splits are
pairwise disjoint by file id.

**Round 2's truncation unbalanced the training set.** Within a round, the manifest was ordered by mood, so the 450
files that finished are aggressive (168), electronic (249) and happy (33). All of round 2's party, relaxed and sad
files were among the 150 that never ran. Final training composition by file-dominant mood: electronic 802,
aggressive 493, happy 100, party 84, relaxed 76, sad 70, acoustic 26 (the archive has only 57 acoustic files, and
TEST and VAL take 31 of them). TEST and VAL are unaffected; they were drawn first. This does not move the verdict:
of the four failed gates, happy MAE did get round-2 files, and gender, voice_instrumental and energy are not mood
classes.

### The read path reproduces production

Each file went through the deployed image's own path: `video_audio.extract_audio_track`, then
`analysis._decode_windows` at 16 kHz on the **stored** window bounds. The embedding is
`TensorflowPredictEffnetDiscogs` on `discogs-effnet-bs64-1`, output `PartitionedCall:1`, batch 64, mean over patches
(the reduction `_predict_single` applies): 1,280 values per window. As a **control**, the deployed msd graphs were
re-run in the same containers on 10 windows from 10 files spanning six container types, including three windows more
than 30 windows into multi-hour files. Against the stored scores, the largest difference over all 11 scores was
**1.79 × 10⁻⁷**, with no NaN. The harness reads what production wrote.

Container pattern on the archive host, one at a time: `--rm --network none --cpus 2 --memory 6g`, the archive
mounted read-only, the deployed image `2026.11.1`, `nice -n 10`, and a 1-minute load guard of 6.5. Peak RSS was
1.03 GiB.

## Method

**Heads tried, on the training files only, scored on VAL only:**

- ridge regression on logit targets (standardised inputs, λ = 1,000);
- a NumPy MLP, 1280 → hidden (ReLU) → 11 sigmoid, binary cross-entropy against the soft stored scores, Adam,
  early stopping on a file-level inner split of the training files.

**Selected on VAL:** MLP with 1,024 hidden units, weight decay 10⁻³, averaged over 5 seeds. At 600 training files on
VAL, against the untuned 256-unit MLP, ensembling added +0.03 to +0.04 r on the weakest scores (lowest gated r 0.840 → 0.870, gender 0.815 → 0.855). Ridge plateaus well
below the MLP (at 600 files on VAL: relaxed r 0.833, happy r 0.829, dominant mood 0.802).

**Weighting.** Every metric is archive-weighted. A window's weight is its cell's population over its cell's sample
(cell = dominant mood × duration bucket), times its file's window count over the windows sampled from that file. So
each figure estimates the archive's window distribution, not the sample's.

**Confidence intervals.** 95% percentile intervals from 2,000 file-level bootstrap resamples, stratified by cell.

**Run-to-run noise of the head** (3 independent fits at the full training set, on VAL): r spread ≤ 0.006, dominant
mood spread ≤ 0.004.

## Learning curve (VAL only)

Selected head, mean of 2 fits per size (3 at the full set), training files drawn from all completed rounds:

| Train files | r acou | r elec | r aggr | r relx | r hapy | r sad | r party | r dance | r gend | r voice | dominant | macro recall | energy MAE | gates passed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 300 | 0.885 | 0.934 | 0.913 | 0.849 | 0.856 | 0.913 | 0.877 | 0.905 | 0.829 | 0.846 | 0.811 | 0.674 | 0.0423 | 6 / 25 |
| 600 | 0.902 | 0.944 | 0.920 | 0.876 | 0.879 | 0.926 | 0.895 | 0.918 | 0.861 | 0.871 | 0.819 | 0.685 | 0.0390 | 13 / 25 |
| 900 | 0.915 | 0.948 | 0.930 | 0.898 | 0.889 | 0.938 | 0.911 | 0.927 | 0.871 | 0.881 | 0.827 | 0.714 | 0.0359 | 16 / 25 |
| 1,200 | 0.923 | 0.952 | 0.931 | 0.903 | 0.895 | 0.941 | 0.918 | 0.930 | 0.882 | 0.889 | 0.836 | 0.725 | 0.0352 | 18 / 25 |
| 1,500 | 0.925 | 0.954 | 0.937 | 0.913 | 0.905 | 0.942 | 0.921 | 0.933 | 0.887 | 0.892 | 0.837 | 0.725 | 0.0345 | 19 / 25 |
| 1,651 | 0.929 | 0.955 | 0.938 | 0.914 | 0.905 | 0.944 | 0.924 | 0.933 | 0.889 | 0.893 | 0.842 | 0.733 | 0.0338 | 19 / 25 |

MAE as a fraction of the stored SD at 1,651 files (threshold 0.30): acoustic 0.28, electronic 0.22, aggressive 0.26,
relaxed 0.30, happy 0.34, sad 0.25, party 0.29, danceability 0.25, gender 0.37, tonality 0.28, voice 0.34.

**What the curve said, and how well its projections held.** Doubling from 300 to 600 files still gained up to 0.03 r
(relaxed, happy, gender), so the plan's stop rule (stop when 300 → 600 gains less than 0.01 r and 0.005 MAE on every
gate) called for more data. A power-law fit to the 600-file curve projected relaxed r 0.889 at 1,200 files; the
measured value was 0.903, so the projection undershot by 0.014. From 1,200 to 1,651 files, the curve gained one gate
(19 of 25 on VAL). Beyond 1,200 files, the four failing MAE and energy gaps narrowed by 0.01 SD or less per 300 files. The last 150 files
of round 2 were the party, relaxed and sad files (see Data), so the cut is not evidence of a plateau for those classes.

## Held-out result (TEST, evaluated once)

Selected head, trained on all 1,651 training files. TEST: 351 files / 2,455 windows. Point estimate [95% CI].

| Score | Pearson r | gate | MAE | gate |
|---|---|---|---|---|
| mood_acoustic | 0.926 [0.912, 0.938] | ≥ 0.90 PASS | 0.0282 [0.0263, 0.0302] | ≤ 0.034 PASS |
| mood_electronic | 0.950 [0.931, 0.964] | ≥ 0.90 PASS | 0.0349 [0.0325, 0.0377] | ≤ 0.051 PASS |
| mood_aggressive | 0.930 [0.916, 0.942] | ≥ 0.90 PASS | 0.0616 [0.0574, 0.0660] | ≤ 0.068 PASS |
| mood_relaxed | 0.913 [0.903, 0.923] | ≥ 0.90 PASS | 0.0408 [0.0384, 0.0434] | ≤ 0.041 PASS, threshold inside CI |
| mood_happy | 0.909 [0.894, 0.923] | ≥ 0.90 PASS, threshold inside CI | **0.0514 [0.0486, 0.0543]** | ≤ 0.046 **FAIL** |
| mood_sad | 0.935 [0.916, 0.950] | ≥ 0.90 PASS | 0.0467 [0.0438, 0.0501] | ≤ 0.056 PASS |
| mood_party | 0.927 [0.915, 0.937] | ≥ 0.90 PASS | 0.0531 [0.0499, 0.0564] | ≤ 0.055 PASS, threshold inside CI |
| danceability | 0.933 [0.914, 0.949] | ≥ 0.90 PASS | 0.0361 [0.0336, 0.0388] | ≤ 0.047 PASS |
| gender | 0.872 [0.849, 0.892] | ≥ 0.85 PASS, threshold inside CI | **0.0562 [0.0532, 0.0593]** | ≤ 0.042 **FAIL** |
| tonality | 0.870 [0.847, 0.890] | reported, not gated | 0.0170 [0.0155, 0.0187] | ≤ 0.022 PASS |
| voice_instrumental | 0.875 [0.857, 0.893] | ≥ 0.85 PASS | **0.0661 [0.0620, 0.0704]** | ≤ 0.053 **FAIL** |

| Window-level gate | Value [95% CI] | Gate | Result |
|---|---|---|---|
| Dominant mood agreement | 0.854 [0.834, 0.873] | ≥ 0.85 | PASS, threshold inside CI |
| … where the stored top-two margin ≥ 0.10 | 0.967 [0.955, 0.978] | ≥ 0.95 | PASS |
| Macro recall, 7 classes | 0.736 [0.707, 0.767] | ≥ 0.70 | PASS |
| Energy MAE | **0.0328 [0.0308, 0.0354]** | ≤ 0.03 | **FAIL** |

Per-class recall of the dominant mood: acoustic 0.983, electronic 0.919, aggressive 0.869, sad 0.762, happy 0.578,
party 0.554, relaxed 0.489. The head ranks windows well (every gated r passes). The failures are **level**
errors on the two characteristic scores (gender, voice_instrumental) and on happy, and energy inherits them.

TEST ran slightly better than VAL on most scores (for example dominant mood 0.854 against 0.842). The two are
independent draws of about 350 files, and the differences sit inside the CIs.

## Cost

Measured in the harness on the archive host (2 CPUs per container, niced, alongside production load):

| Step | Cost | Source |
|---|---|---|
| EffNet embedding | **4.50 cpu-s per coarse window** (9,123 pilot windows) | this spike |
| Head (5-member MLP, 6.6 M parameters) | 5.6 × 10⁻⁵ cpu-s per window, measured on a development laptop, not on the archive host | this spike |
| The 11 deployed msd graphs (control) | about 114 cpu-s per window (10 windows) | this spike |
| The 11 deployed msd graphs in production | about 96.8 cpu-s per window, 84.5% of analysis CPU | `docs/spikes/phaze-fzubr-analysis-cpu-profile.md` |

So the EffNet path costs about **21–25× less** than the musicnn mood sweep per window. And since `phaze-l5mzm` already
emits this embedding in the analysis pass, the head's marginal cost would be close to zero. Scaled to the archive
(121,572.3 h on 2026-11-11, 20 coarse windows per audio-hour; arithmetic on the measured per-window figures, not a
run): the musicnn sweep is about **65,000 cpu-h** per archive pass; embedding plus head is about **3,000 cpu-h**. That
saving is what a PASS would have bought. With a FAIL, the musicnn graphs stay, and so does their 84.5% share.

## The two licence routes

The operator's statement, 2026-11-13, verbatim (bead `phaze-m3t4d`): "as far as license for Essentia, as long as
we're not using their code or methods (e.g. clean room), we can develop a system that provides similar results for
each mood". The licence judgement is the operator's; this section states what each route implies and is **not a legal
opinion**.

| | EffNet-embedding route (what this spike measured) | Clean-room route (not measured here) |
|---|---|---|
| What it is | a small head on Essentia's Discogs-EffNet embedding, trained to reproduce the stored musicnn scores | a backbone and feature extraction with **no Essentia code, models or methods**, plus heads trained on labels phaze owns |
| Licence | **Inherits Essentia's model terms twice.** The input is an Essentia model (catalogue: CC BY-NC-SA 4.0; the shipped licence file is headed NoDerivatives and is internally inconsistent, `docs/spikes/phaze-0toy3.5-clean-room-mood.md` E1). The training targets are outputs of the NC musicnn models. Not clean under the operator's statement | **Clean only if every input is.** Training on the stored musicnn scores, or distilling any Essentia output, is excluded (`phaze-0toy3.5`, "Which routes are NOT clean"). Candidate backbones there: the ISC musicnn checkpoints (whether a checkpoint published by the same group counts as their "methods" is the operator's call), CLAP (code CC0-1.0, weights Apache-2.0), EfficientAT (MIT). The data behind each backbone's weights is not verified |
| Cost | measured above: 4.50 cpu-s per window for the embedding, which the analysis pass already computes; the head is negligible | **unmeasured.** ISC musicnn run once: inferred about 9–10 cpu-s per window (`phaze-0toy3.5` E3). CLAP: 27.5 M audio parameters, PyTorch, 48 kHz front end. EfficientAT: 0.54 GMACs, PyTorch. Each non-musicnn backbone adds a new runtime and decode path to the agent image. Labelling cost: the operator's time |
| Accuracy against today's scores | **measured: 21/25 gates, FAIL** on 1,651 training files of dense soft targets | **unknown.** Without the stored scores as labels, the head cannot be fitted to them. Agreement with today's scores becomes an evaluation, not an objective, and the labels are fewer and coarser than the 11.7 k dense targets used here. **Inference, not measured:** expect agreement with today's scores below this spike's unless the backbone is clearly better for mood |
| What it would change | nothing new to license; mood still depends on Essentia models | removes the Essentia-model dependency from mood, and changes what "mood" means from "musicnn's output" to "the operator's labels" |

A third route sits outside both: the proprietary licence MTG's catalogue offers for the existing models (route 0 in
`phaze-0toy3.5`). It changes no code and no accuracy.

## The head artefact

Stored outside the archive and outside the repository, as the bead's design asks: float32 weights for the 5-member
ensemble (24 MiB, compressed; predictions within 7 × 10⁻⁷ of the float64 fit), plus a count-only manifest. The
manifest holds version `m3t4d-v1`, the embedding (`discogs-effnet-bs64-1`, `PartitionedCall:1`, mean over patches,
1,280-d), the head shape and training counts (1,651 files / 11,712 windows). It runs in NumPy; no second TensorFlow
session. With a FAIL, nothing consumes it. It is kept for a later round of the same measurement.

## What this does not establish

- **Whether more stratified data would pass.** The curve was still rising at 1,651 files. The last 150 files, the
  party, relaxed and sad ones, never ran. Projections from this curve undershot once (see the learning curve).
- **Whether a different head on the same embedding would pass.** Ridge and a 1-hidden-layer MLP (256 or 1,024 units,
  ensembled) were tried. No per-score calibration, no multi-window context, no other embedding layer.
- **Anything about the 33-graph (mtt) files**, about 6,600 files. They were excluded by design.
- **The clean-room route's accuracy or cost.** Not measured.
