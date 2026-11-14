# phaze-0toy3.5 — a licence-clean (clean-room) path to mood scores

Spike, 2026-11-13. No product code, no production access, no archive identifiers. This is a research record,
**not a legal opinion**; every licence statement below is what a primary page said on 2026-11-13 and the
operator's planned question to UPF decides the rest.

## Question

phaze's eleven mood and characteristic scores come from MTG Essentia `*-musicnn-msd-2` graphs. MTG's model catalogue
states CC BY-NC-SA 4.0 for all its models, but the licence file those models ship with is headed NoDerivatives and is
internally inconsistent (E1), so whether NC-SA or NC-ND governs is **unsettled**; NonCommercial is common to both, and UPF
has to settle the rest. The operator
asked, 2026-11-13, quoted: "is there a way to clean room a model that produces similar results?", and added, quoted:
"For any of the above, if theree are missing features, evaluate what the contribution guidelines are (or forking
guidelines) and evaluate whether we can address the gaps" (durable record: bead phaze-0toy3.5, epic phaze-0toy3).

So: **which routes produce mood scores without depending on a NonCommercial Essentia model, what does each cost, and
what is missing from each?** The mood *target* (which labels, which accuracy bar) is `phaze-uv8s3`'s decision, not
this spike's.

## Method

| Source | Used for |
|---|---|
| **Lacuna MCP** (`search_lacuna`, then `get_paper`) | musicnn, MARBLE, a music-emotion embedding comparison. Searches for mood-probe sample sizes and for emotion datasets as Lacuna *resources* returned nothing usable. Lacuna has no citation graph, so **no citation counts are claimed anywhere here** |
| `gh api repos/<o>/<r>/license`, repo READMEs, repo file listings | code licences, weights location, presence of CONTRIBUTING files |
| `huggingface.co/api/models/<id>` | weights `license` fields (CLAP, MERT) |
| `essentia.upf.edu/models/LICENSE`, `MTG/essentia` `doc/sphinxdoc/models.rst` | the NC terms and what the models were derived from |
| `docs/spikes/phaze-fzubr-analysis-cpu-profile.md` | phaze's measured CPU split (the denominator for every CPU claim) |
| `docs/spikes/phaze-8ghbw-analysis-alternatives.md` (and its cloud version) | **not repeated**: CLAP's 586 MiB / 27.5M-parameter figures and EfficientAT's MIT result were taken from it. Its A1 to A3 (Essentia heads on another embedding) are all CC BY-NC-SA and are out of scope here |

## Evidence

### E1. The incumbent licence is ambiguous in its own file

`essentia.upf.edu/models/LICENSE` is headed "Creative Commons Attribution-NonCommercial-NoDerivatives 4.0", its summary
says "Adapt — remix, transform, and build upon the material", and its legal-code link is the **by-nc-sa** URL. The
catalogue (`models.rst` line 11) says CC BY-NC-SA 4.0 and adds: "also available under proprietary license upon
request". The same file records provenance: "Models with MusiCNN or VGG architecture are derived from work licensed
under the ISC license terms." **That proprietary-licence offer is a route in its own right** (route 0 below).

### E2. The original musicnn code and weights are ISC

- `jordipons/musicnn` and `jordipons/musicnn-training`: GitHub licence API **ISC** (`LICENSE.md`: "Copyright (c) Music
  Technology Group, Universitat Pompeu Fabra 2019 ... Permission to use, copy, modify, and/or distribute this software
  for any purpose with or without fee is hereby granted").
- The repo's file tree carries the pre-trained checkpoints (`musicnn/MSD_musicnn/`, `MSD_musicnn_big`, `MSD_vgg`,
  `MTT_musicnn`, `MTT_vgg`: `.index` and `.data-00000-of-00001`). **The README has no separate weights licence.** So the
  weights ship under the repo's ISC.
- **Not established:** whether the Essentia `msd-musicnn-1.pb` is a byte-for-byte re-export of the ISC
  `MSD_musicnn` checkpoint. E1's provenance line suggests it derives from it, but that was not tensor-compared. If it
  is the same weights, then the MTG redistribution is NC-licensed while the underlying work is ISC; **that reading is
  the operator's and UPF's to make, not this spike's.**
- Paper (Lacuna, <https://lacuna.tiptreesystems.com/work/musicnn-pre-trained-convolutional-neural-networks-for-music/version/art_984179904e5b4d0aa0c8803d9be4adb7>):
  trained on MagnaTagATune (19,000 training songs) and the Million Song Dataset (200,000 training songs); MTT_musicnn
  reaches 90.69 ROC-AUC / 38.44 PR-AUC **on MTT tagging**; usable "as feature extractors, or for transfer learning"
  with SVMs on the extracted features. **Those figures are tagging accuracy, not mood accuracy.**

### E3. phaze's own CPU split makes the backbone the target

From `docs/spikes/phaze-fzubr-analysis-cpu-profile.md`: the 11 `musicnn_msd` graphs are **84.5%** of analysis CPU,
**about 96.8 cpu-s per 180 s coarse window, about 9.0 per graph**, **1,957 of 2,316 cpu-s per audio-hour**; the genre
EffNet is 3.2%. Each characteristic graph re-runs the musicnn front end (`phaze-8ghbw`). **Consequence:** any route that
runs one frozen backbone once and puts eleven *small* heads on it, whatever the licence, attacks the 84.5%.
**Inferred, not measured:** if a head is negligible next to the backbone, the mood sweep falls from about 97 to about
9 to 10 cpu-s per coarse window (roughly 1,957 to 180 cpu-s per audio-hour). Nobody has measured the head cost on
these embeddings.

### E4. Literature: frozen embeddings plus a shallow classifier works on small labelled sets

Lacuna returned no paper that reports sample sizes or accuracy for *mood* heads **on musicnn** embeddings. The nearest
evidence:

- *Comparison and Analysis of Deep Audio Embeddings for Music Emotion Recognition*
  (<https://lacuna.tiptreesystems.com/work/comparison-and-analysis-of-deep-audio-embeddings>)
  puts frozen L3-Net / VGGish embeddings into SVM, RF, MLP, CNN and RNN classifiers over four **small** sets: 900 clips
  (4-quadrant), **162 clips** (bi-modal), 744 of 1,000 used (Emotion in Music), 848 (RAVDESS singing). Reported: **88.0% F1
  on the 162-clip set**, 72.0% F1 on the 900-clip set, arousal/valence r² 0.671 / 0.556 on Emotion in Music (against 0.656 /
  0.462 for a convnet baseline it cites). **These are four-quadrant or valence/arousal tasks with L3-Net and VGGish,
  not eleven independent musicnn heads**, and use 80/10/10 splits on tiny sets.
- *MARBLE* (<https://lacuna.tiptreesystems.com/work/marble-music-audio-representation-benchmark-for-universal/version/art_2da8a6c1b9644afa90c7b227a4910bf5>):
  frozen backbone, light head, 12 datasets / 18 tasks, includes emotion and MagnaTagATune / MTG-Jamendo tagging.
  Its strongest baseline is MERT-v1-330M, **whose weights are CC-BY-NC-4.0 (E6)**. The finding that survives the
  licence filter is the *protocol*: a frozen backbone plus a lightweight head is the accepted way to compare
  representations.
- **Labelled-sample size needed: not found.** The honest answer from the evidence is "hundreds of clips has produced
  published four-class results; no source here bounds eleven heads". It must be measured as a learning curve on the
  operator-labelled sample `phaze-uv8s3` plans (a pilot, not a spike conclusion).

### E5. CLAP and EfficientAT

- **CLAP** (`LAION-AI/CLAP`): GitHub licence API **CC0-1.0**; Hugging Face `license` for `laion/clap-htsat-unfused`,
  `laion/clap-htsat-fused`, `laion/larger_clap_music`, `laion/larger_clap_music_and_speech` is **apache-2.0**. README:
  the music checkpoint is "trained on music + Audioset + LAION-Audio-630k", zero-shot **GTZAN 71%** (genre, with the
  prompt `This audio is a <genre> song.`); the music training set is **not released "due to copyright reasons"**; the
  audio encoder init is a **48 kHz** AudioSet HTSAT. `phaze-8ghbw`: 586 MiB fp32, 27.5M audio parameters, PyTorch.
  **No mood benchmark was found**; text-prompt mood is plausible, unmeasured.
- **EfficientAT** (`fschmid56/EfficientAT`): GitHub licence API **MIT**. Weights are in GitHub Releases; the README has
  **no separate weights licence** (the same finding as `phaze-8ghbw`). `mn10_as`: 4.88M parameters, **0.54 GMACs**,
  AudioSet mAP 47.1; `dymn10_as` 10.57M, 0.58 GMACs, 47.7. ImageNet-pretrained then AudioSet-trained. **mAP is an
  AudioSet tagging figure, not mood accuracy**; no mood result was found. Front-end sample rate was not read.

### E6. Datasets: audio and label licences, read separately

| Dataset | Code | Labels / metadata | Audio | Mood labels? |
|---|---|---|---|---|
| MTG-Jamendo | Apache-2.0 | **CC BY-NC-SA 4.0** | per-track CC (`audio_licenses.txt`) | yes, 56 mood/theme tags on 55,525 tracks |
| FMA | MIT | CC BY 4.0 | "distribute it under the license chosen by the artist"; "meant for research purposes" | **no**: genres, plus free tags |
| MagnaTagATune | n/a | not read | **not verified**: the primary page did not load | tags, not specifically mood |
| Million Song Dataset | n/a | not read | features only, audio is not in the dataset | Last.fm tags (licence not read) |
| AudioSet | n/a | labels CC BY 4.0, ontology CC BY-SA 4.0 | YouTube-sourced, not redistributed | mood-like classes: not verified here |
| Operator-labelled sample (`phaze-uv8s3`) | n/a | **owned by the operator** | the operator's own files | whatever the operator labels |

The MTG-Jamendo README ends with: "made available solely for non-commercial research and academic use. Any other use
... requires prior written authorization from Jamendo S.A." **So the only dataset with real mood labels is itself NC.**
`MERT-v1-95M` and `MERT-v1-330M` Hugging Face cards: **cc-by-nc-4.0**, although `yizhilll/MERT` code is Apache-2.0.

## Which routes are NOT clean (a risk for the operator, not a legal opinion)

- **Training on phaze's stored musicnn scores**, or any Essentia head's output, as labels: the labels are outputs of an
  NC model.
- **Distilling a student from Essentia model outputs**: same dependency, one hop further.
- **Using an Essentia mood head on any other backbone** (the `phaze-8ghbw` A1 route): the head is CC BY-NC-SA.
- **Fine-tuning or probing MERT**: the weights are NC.
- **Training on MTG-Jamendo mood tags** for anything beyond NC research: the dataset terms say so.

Whether "a head trained on a person's own labels over an ISC backbone" is derived from the MTG models at all is exactly
the UPF question; the shortlist below is ranked on **what each route does not touch**, not on a legal conclusion.

## Verdict

**Yes, conditionally: there is a route with no Essentia model in it, a frozen permissively-licensed (ISC) backbone
plus heads trained on labels the operator owns, and it is free of NC datasets *if* the data behind the backbone's
weights is.** That condition is not verified: the licences of the training data behind the `MSD_musicnn` weights (the MSD
tags, Last.fm with licence not read, and the audio musicnn was trained on, which is not part of MSD and whose source and licence were not established) and of MTT audio were not established (E6; the MagnaTagATune page did not load), so "no NC dataset
anywhere in it" is a hypothesis, not a finding. The data phaze would add itself, the operator's labels, carries no NC
term. Its other open risk is that accuracy against today's scores is **unmeasured**; nothing found bounds it. It also happens to be the CPU-cheapest shape (E3).

## Recommendation: ranked shortlist

Ranking is licence cleanliness first, then fit to phaze's pipeline, then expected cost.

| # | Route | Base model + licence | Label source + licence | Training data behind the weights | Expected accuracy (source, benchmark) | CPU cost | Gap, and contribute-vs-fork |
|---|---|---|---|---|---|---|---|
| **0** | Ask UPF for the proprietary licence for the *existing* models | n/a | n/a | n/a | identical to today | unchanged | not a build route; offered in the catalogue (E1). Cheapest if the operator is already writing to UPF |
| **1** | **ISC musicnn backbone (`MSD_musicnn`) run once, plus 11 shallow heads on operator labels** | `jordipons/musicnn`, **ISC**, weights in-repo | operator-owned (`phaze-uv8s3` sample) | MSD (200,000 training songs) tags (Last.fm, licence not read) plus the audio musicnn was trained on, which is not part of MSD (E6): **source and licence not established** | no mood result found for this combination. Nearest: frozen-embedding SVM/RF got 88.0% F1 on 162 clips and 72.0% on 900 clips (4-class, **different embeddings**, E4). Tagging only, and for a **different checkpoint**: 90.69 ROC-AUC on MTT belongs to `MTT_musicnn`, not the `MSD_musicnn` backbone this route uses; no `MSD_musicnn` figure was read (E2). **Must be measured** | inferred about 1,957 → about 180 cpu-s per audio-hour (E3); heads unmeasured | **Gap: checkpoints are TF checkpoint files, phaze runs Essentia's frozen `.pb`; need a frozen-graph or ONNX export and an embedding output node.** No CONTRIBUTING or code-of-conduct file in either repo (top-level listing), so upstream has no stated process; ISC allows a **fork** if the notice is kept. Effort: **S to M, my estimate**; a fork is clearly the right shape. Cheap prerequisite: compare the ISC checkpoint tensors with Essentia's `msd-musicnn-1.pb` |
| **2** | CLAP embedding plus a probe, or text-prompt scores | CLAP, code **CC0-1.0**, weights **Apache-2.0** (HF cards) | operator-owned for a probe; none for zero-shot | LAION-Audio-630k + AudioSet; music set **unreleased for copyright reasons**: **mixed, unclear** | zero-shot GTZAN **71%** (genre only). **No mood number found** | 27.5M audio parameters, 586 MiB, PyTorch: **unmeasured** on phaze's files, almost certainly heavier than one musicnn pass (inferred from size) | **Gaps: 48 kHz front end (phaze's coarse tier is 16 kHz), PyTorch not TF, no mood heads.** Code is CC0 so a fork is unencumbered; repo has no CONTRIBUTING file. Effort **M**: new decode path plus a new runtime in the agent image |
| **3** | EfficientAT (`mn10_as`) embedding plus a probe | **MIT** code; weights in Releases, **no separate licence statement** | operator-owned | AudioSet (labels CC BY 4.0, audio YouTube) + ImageNet init | AudioSet mAP 47.1: **tagging, not mood** | 4.88M parameters, 0.54 GMACs: the lightest candidate, **unmeasured** | **Gaps: no mood semantics (AudioSet classes only), PyTorch, front end unread.** Needs the most labels because the embedding was not trained on music tags. Fork under MIT; no CONTRIBUTING file. Effort **M** |
| **✗** | MERT; Jamendo/FMA-trained heads; distilling Essentia outputs | MERT weights **CC-BY-NC-4.0** | Jamendo NC; FMA has no mood labels; Essentia outputs are NC | n/a | MERT is MARBLE's strongest | n/a | **Excluded**, see the section above |

### What `phaze-uv8s3` should take from this

1. Route 1 is the one worth a pilot: an ISC fork export to a frozen-graph .pb or ONNX, one embedding pass per coarse window, eleven small heads,
   trained on a learning curve of the operator's labels, compared against the current scores on the same files.
2. **Measure before believing the CPU number** (E3 is an inference), and **measure the head accuracy at several
   sample sizes**; that is the only way to learn the labelled-sample size.
3. Do not generate labels from stored musicnn scores in the pilot, or the route stops being clean.
4. Route 0 costs one email and needs no engineering; the operator is already contacting UPF.

## What was not found

- A primary-page licence for MagnaTagATune audio (the page did not load), the Last.fm tag terms behind the MSD
  labels, and whether AudioSet's music-mood classes suit phaze's target.
- A weights-licence statement for EfficientAT, and the licences of the audio behind CLAP's music checkpoints.
- Any accuracy figure for mood heads on musicnn embeddings, and any sample-size source for them.
- Whether Essentia's `msd-musicnn-1.pb` equals the ISC `MSD_musicnn` weights.
- Actual CPU of any candidate on phaze's files; every non-musicnn cost above is a parameter count or MACs, **not a
  measurement**.
