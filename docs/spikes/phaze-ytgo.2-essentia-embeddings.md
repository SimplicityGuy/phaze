# S2 — Can phaze's existing essentia models emit usable embeddings?

- **Bead:** `phaze-ytgo.2` (epic `phaze-ytgo` — AudioMuse-AI: clean-room vs sidecar, per purpose)
- **Date:** 2026-07-25
- **Tree:** branch `wt/bead/issue/phaze-ytgo.2`, forked at `b051b3b`
- **Depends on:** `phaze-ytgo.1` — [S1 purpose rubric](phaze-ytgo.1-purpose-rubric.md), whose
  EFB baseline, accuracy bars, identity/resemblance boundary and blocker **B2** this document
  scores against.
- **Status:** investigation only. No product code, no dependency change, no migration.
  `pyproject.toml` untouched; every measurement script lives in the session scratchpad.

> ## Clean-room statement
>
> **No AudioMuse-AI source code was read while producing this document.** No `.py` file from
> that project was opened, listed, greped, or quoted. The only AudioMuse facts used are the two
> already written into the `phaze-ytgo` epic description — that it produces a **200-dimension**
> averaged per-track vector from a dedicated ONNX embedding model, and that it preprocesses with
> `StandardScaler` + PCA before clustering — and both are used **only for contrast**, never as a
> design input.
>
> The session scratchpad is shared with three concurrently-running sibling spikes, and by the
> time this spike started it already contained sibling-fetched artifacts (an `audiomuse/`
> directory, `ALGORITHM.md`, licence texts, HTML). **This spike created its own isolated
> subdirectory and did not open any file it did not itself write**, precisely so that the seal
> could be asserted without qualification. The one thing worth flagging to `phaze-ytgo.5` and to
> D1: *a shared scratchpad is a clean-room hazard*, because the unsealed sibling (`phaze-ytgo.4`)
> legitimately downloads AGPL source into the same tree that the sealed siblings work in.

______________________________________________________________________

## Question

phaze already instantiates all three essentia TensorFlow wrappers
(`services/analysis.py:146-150`):

```python
classifier = es.TensorflowPredictMusiCNN(graphFilename=graph_path)        # :146
classifier = es.TensorflowPredictVGGish(graphFilename=graph_path)         # :148
classifier = es.TensorflowPredictEffnetDiscogs(graphFilename=graph_path)  # :150
```

If those already-shipped, already-running models can emit embeddings, the clean-room option
costs one new column and an index. If they cannot, clean-room means adopting a whole new model
stack and the sidecar looks far better. Four sub-questions:

1. **Which** of the three can emit embeddings, at what output node, at what dimensionality?
   `effnet_discogs` is the most likely to yield a strong general-purpose music embedding —
   confirm or refute.
2. **Marginal cost** per file *on top of the analysis phaze already performs*, priced against
   S1's blocker **B2** (per-file analysis cost is capped at O(1) by the deliberate Phase 43
   decision, `services/analysis.py:394-398`).
3. **Window-level feasibility** at phaze's existing fine/coarse tiers, plus a defensible
   track-level aggregate (mean-pool vs medoid).
4. **Quality, measured two ways** — near-duplicate separability (P1) and semantic
   neighbourliness (P2) — as cosine-distance distributions, not adjectives, and each with the
   **EFB arm S1 rule 3 makes mandatory**.

______________________________________________________________________

## Method

### Hardware and environment

| | |
| --- | --- |
| Machine | Apple **M1 Pro**, 10 cores, 32 GiB RAM |
| OS | macOS 26.5.2, arm64 |
| Runtime | Python 3.14, `essentia` **2.1-beta6-dev** (`essentia-tensorflow`), all via `uv run` |
| Accelerator | **none** — CPU only. No GPU or Metal path was used or available to essentia's TF build. |
| Contention | **Three sibling spikes ran concurrently on the same machine throughout.** This inflates absolute wall-clock. Every headline cost figure below is therefore reported as a **ratio to phaze's own baseline measured in the same process, back-to-back on the same audio buffer**, which cancels shared load. |

### Model provenance — these are phaze's production models, verified

The measurements used `<scratch>/models-full`. Every file was checked against the
byte-size manifest baked into phaze's own downloader (`src/phaze/scripts/download_models.py`):

```console
$ stat -f "%z %N" .../discogs-effnet-bs64-1.pb .../discogs-effnet-bs64-1.json \
                  .../mood_acoustic-musicnn-msd-2.pb .../mood_acoustic-vggish-audioset-1.pb
18366619  discogs-effnet-bs64-1.pb          # _PB_DISCOGS_EFFNET        = 18366619   ✓
   14990  discogs-effnet-bs64-1.json        # _JSON_SIZES[...]          = 14990      ✓
 3239548  mood_acoustic-musicnn-msd-2.pb    # _PB_MUSICNN_COMMON        = 3239548    ✓
288629030 mood_acoustic-vggish-audioset-1.pb# _PB_VGGISH_AUDIOSET       = 288629030  ✓
```

All four match exactly. **No new model file was introduced**, per the bead's design constraint.

### Corpus — real archive files, with provenance

The machine has no populated phaze archive. Eight genuinely-recorded files were reachable; all
eight were used. Dropbox subfolders that appeared to hold more music (`Craig Connelly/`, the
Ørjan Nilsen interview) are **0-byte online-only placeholders** and could not be read.

| id | file | duration | source encoding | role |
| -- | ---- | -------- | --------------- | ---- |
| `W1` | `<track-01>.mp3` | 301 s | mp3 320 kbps | individual track |
| `W2` | `<track-02>.mp3` | 277 s | mp3 320 kbps | individual track |
| `W3` | `<track-03>.wav` | 257 s | wav 24-bit | individual track |
| `W4` | `<track-04>.mp3` | 441 s | mp3 ~365 kbps | individual track |
| `W5` | `<track-05>.wav` | 287 s | wav 24-bit | individual track |
| `W6` | `<track-06>.wav` | 439 s | wav 16-bit | **same work as W5, different edit** |
| `S1` | `<set-01>.mp3` | 4,721 s (1 h 19 m) | mp3 320 kbps | **long set** |
| `S2` | `<set-02>.mp3` | 22,098 s (6 h 08 m) | mp3 128 kbps | **long set** (top-100 countdown) |

**Constructed variants for the P1 measurement.** No known encode-duplicate pair existed in the
archive, so — as the bead's design instructs — they were constructed. For each of `W1..W4`,
five variants via `ffmpeg`:

| variant | command | class |
| ------- | ------- | ----- |
| `-mp3-128` | `-c:a libmp3lame -b:a 128k` | encode-duplicate |
| `-mp3-64` | `-c:a libmp3lame -b:a 64k` | encode-duplicate (aggressive) |
| `-opus-96` | `-c:a libopus -b:a 96k` (`.ogg`) | encode-duplicate |
| `-aac-96` | `-c:a aac -b:a 96k` (`.m4a`) | encode-duplicate |
| `-master` | `equalizer(70 Hz +6 dB), equalizer(5.5 kHz −5 dB), acompressor(-18 dB, 4:1), volume 2.0` → mp3 192k | **differently-mastered rip** |

`libvorbis` is absent from this ffmpeg build, so Opus stands in for the Ogg family. **These four
lossy re-encodes are constructed, not operator-labelled** — see [what could not be measured](#what-could-not-be-measured).

### Extraction pipeline — phaze's own windowing, unmodified

Every measurement decodes through **phaze's real coarse tier**, by importing the shipped
functions rather than reimplementing them:

```python
from phaze.services.analysis import (_iter_windows, _stride_to_cap, _run_model_sets,
                                     _positive_class_prediction, MODEL_SETS, _probe_duration_sec)
# coarse tier: 16 kHz, 180 s windows, cap 30 (config.py:946-978; services/analysis.py:386,391,398)
buf = es.EasyLoader(filename=path, sampleRate=16000, startTime=s, endTime=e)()
```

**78 coarse windows** across 50 corpus items were extracted. Per window: effnet 1280-d, musicnn
200-d, vggish 128-d, and the **EFB 11-d positive-class score vector** computed with phaze's own
`_positive_class_prediction` over `MODEL_SETS` — i.e. S1's baseline, from the same buffer, in the
same pass. Track-level vectors are the mean over that item's coarse windows.

**One deviation, stated plainly.** For `S1`/`S2`, `EasyLoader`'s deep seek into a multi-hour mp3
dominated runtime (measured: **259 s to decode a 600 s excerpt at offset 3600 s** in the 6 h file).
The 12 sampled windows per set were therefore pre-cut with `ffmpeg -ss … -ar 16000 -ac 1` to wav
and fed to the identical code path at `startTime=0`. Same PCM, same 16 kHz mono; only the decode
container differs. Window offsets were chosen by running `_iter_windows` + `_stride_to_cap` with
phaze's real defaults and then evenly sub-sampling the strided set to 12 (`S1`: 27 natural → 27
strided → 12; `S2`: 123 natural → 30 strided → 12).

### What could NOT be measured

This section is load-bearing. Per the epic's rule, an honest "unmeasured" is usable by D1 and an
estimate is not.

1. **S1's P1 provenance bar is not met.** S1 requires "≥ 50 **operator-labelled** near-duplicate
   pairs". This spike has **45 positive pairs, of which 40 are constructed re-encodes and 4 are
   constructed remasters**. Exactly **one** positive pair (`W5`↔`W6`) is a naturally-occurring,
   same-work-different-rendition pair. No operator labelled anything. The P1 numbers below are
   *directional*, on a proxy positive set that is easier than the real one.
2. **S1's P2 bar cannot be attempted at all.** It requires a blind A/B over **20 seed tracks**
   with an operator marking top-10 results. This corpus contains **six distinct works**. There is
   no operator and no plausible seed set. P2 quality is **unmeasured**, exactly as S1's open
   question O3 anticipated.
3. **P3's accuracy bar is unmeasured.** S1 requires "≥ 70% of scraped 1001Tracklists tracks
   located within ±30 s". Neither long set has scraped tracklist ground truth, and no populated
   phaze database was reachable. The P3 evidence below measures the *shape and cost* of the
   signal, never its accuracy.
4. **No GPU/Metal figure.** CPU only; a GPU would change the cost ratios and was not available.
5. **Archive-scale extrapolations are per-file measurements multiplied by the planner's 200,000
   figure** from the epic description, not a `count(*)` over a real `files` table (S1 open
   question O1 is still open).
6. **The negative sets are small and stylistically narrow** — six works spanning house /
   progressive / trance / hardcore, all electronic. A real 200,000-file archive is far more
   homogeneous *within* a genre and far more diverse *across* genres than this. The
   "hard negatives" arm below is an attempt to model the first of those; nothing here models the
   second.

______________________________________________________________________

## Evidence

### E1 — All three families emit embeddings, and the metadata phaze already ships says so

The answer was already sitting in the `.json` sidecars phaze downloads next to every `.pb`
(`scripts/download_models.py:230-231` writes both). Each declares an `outputs` schema with an
explicit `output_purpose`:

```console
$ python -c "import json; print(json.load(open('discogs-effnet-bs64-1.json'))['schema'])"
{'inputs':  [{'name': 'serving_default_melspectrogram', 'shape': [64, 128, 96]}],
 'outputs': [{'name': 'PartitionedCall:0', 'shape': [64,  400], 'op': 'Sigmoid',
              'output_purpose': 'predictions'},
             {'name': 'PartitionedCall:1', 'shape': [64, 1280], 'op': 'Flatten',
              'output_purpose': 'embeddings'}]}
```

Confirmed empirically by instantiating each wrapper with `output=<node>` on a real 180 s coarse
window (`W1`, 0–180 s, 16 kHz):

| family | model file phaze ships | embeddings node | **dim** | patches / 180 s | rate | predictions node | pass (s) |
| ------ | ---------------------- | --------------- | ------- | --------------- | ---- | ---------------- | -------- |
| **effnet_discogs** | `discogs-effnet-bs64-1.pb` | **`PartitionedCall:1`** | **1280** | **180** | **1.00 Hz** | `PartitionedCall:0` (400) | **0.78** |
| musicnn (msd) | `*-musicnn-msd-2.pb` | `model/dense/BiasAdd` | 200 | 119 | 0.66 Hz | `model/Sigmoid` (2) | 1.03 |
| musicnn (mtt) | `*-musicnn-mtt-2.pb` | `model/dense/BiasAdd` | 200 | 119 | 0.66 Hz | `model/Sigmoid` (2) | 1.02 |
| vggish | `*-vggish-audioset-1.pb` | `model/vggish/fc2/BiasAdd` | 128 | 193 | 1.07 Hz | `model/Sigmoid` (2) | 1.81 |

Penultimate layers are also exposed — `model/dense_1/BiasAdd` (musicnn, 100-d) and
`model/fully_connected/BiasAdd` (vggish, 100-d) — but these are the *classifier's* 100-unit hidden
layer, i.e. task-specific, and are not general-purpose. They were probed and then dropped.

**Answer to "confirm or refute effnet_discogs":** ***confirmed, on four independent grounds.***
It is the highest-dimensional (1280 vs 200 vs 128); it is the only one trained as a general music
representation (Discogs-4M, 3.3M tracks used, per its own model card) rather than fine-tuned on a
few hundred mood excerpts; it is the **fastest** of the three per window; and — decisively — it is
the one model phaze **already runs** on every coarse window as `GENRE_MODEL`
(`services/analysis.py:107-112`, invoked at `:477`).

**One caveat on musicnn/vggish that matters for D1.** phaze holds no standalone MusiCNN or VGGish
backbone. Its 22 musicnn and 11 vggish graphs are each a *classifier* — `mood_acoustic`,
`danceability`, … — whose backbone was fine-tuned for that task. There is no single canonical
"phaze musicnn embedding"; there are 22 slightly-divergent ones, and picking one is arbitrary. The
effnet embedding has no such ambiguity: there is exactly one graph and it is the genre model.

### E2 — Marginal cost: the embedding is a near-free by-product, and free if fused

Measured over **13 coarse windows from 5 files** (2 tracks, 1 track ×3 windows, both long sets
×3 windows), each arm timed back-to-back on the same decoded buffer in one process.

**Baseline** is phaze's real per-coarse-window cost: `_run_model_sets` = 11 model sets × 3
variants + the genre model = **34 forward passes** (`services/analysis.py:461-484`).

| arm | n | min | **median** | mean | max | **vs baseline** |
| --- | - | --- | ---------- | ---- | --- | --------------- |
| `_run_model_sets` — phaze today | 13 | 25.97 s | **47.86 s** | 46.39 s | 58.64 s | — (1.41 s / model pass) |
| **+ second wrapper pass** for `PartitionedCall:1` | 13 | 0.59 s | **0.77 s** | 0.80 s | 1.16 s | **+1.61 %** |
| mel-spectrogram front-end alone | 13 | 0.040 s | 0.089 s | 0.081 s | 0.103 s | +0.19 % |
| **fused** — both nodes, one session | 13 | −0.132 s | **−0.004 s** | −0.011 s | +0.048 s | **−0.009 % (zero within noise)** |

Two integration shapes, two prices:

- **The safe shape** — construct a second `TensorflowPredictEffnetDiscogs(output="PartitionedCall:1")`
  and call it on the same buffer `_run_model_sets` already receives. Costs **+1.6 %** wall clock.
  No reimplementation, no change to how phaze calls essentia.
- **The free shape** — essentia's low-level `TensorflowPredict` takes `outputs` as a
  **`vector_string`**, so one session run returns predictions *and* embeddings:

  ```python
  es.TensorflowPredict(graphFilename=G, inputs=["serving_default_melspectrogram"],
                       outputs=["PartitionedCall:0", "PartitionedCall:1"], squeeze=True)
  ```

  Measured: predictions-only **0.404 s** vs predictions+embeddings **0.398 s** (medians, 3 reps
  each per window). The 1280-d tensor rides out of a forward pass phaze is already computing, for
  **nothing**.

  *Honest caveat on the fused arm.* It requires reimplementing the wrapper's front-end
  (`TensorflowInputMusiCNN` → 128-frame patches, hop 62, zero-padded to the fixed batch of 64).
  The spike's reimplementation is not byte-identical to the wrapper: per-patch alignment differs,
  giving a max absolute element difference of 0.15–0.34 against the wrapper's output. The **cost**
  conclusion stands (it is a property of the graph, not of the front-end), but a production fused
  path would need its framing validated against the wrapper first. **The +1.6 % second-pass number
  is the one to plan against; treat "free" as the achievable ceiling.**

**Memory**, measured as real RSS at each step in one process:

| step | RSS |
| ---- | --- |
| interpreter + essentia imported | 241.9 MiB |
| one 180 s coarse window decoded @ 16 kHz | 268.3 MiB |
| **phaze baseline: 34 classifiers loaded, steady state** | **8,190.0 MiB** |
| + second `TensorflowPredictEffnetDiscogs` instance | 8,190.3 MiB (**+0.3 MiB**) |
| + embeddings computed, steady state | 8,251.3 MiB (**+61.3 MiB total, +0.75 %**) |

**Against S1 blocker B2 — and this is the finding that decides the clean-room option.** B2 says
per-file analysis cost is capped at O(1) by the Phase 43 decision. The track-level embedding
**rides the coarse tier, which is already capped at 30 windows per file** (`_DEFAULT_COARSE_CAP`,
`services/analysis.py:398`). It therefore inherits the cap and is **O(1) per file by construction —
it does not touch B2 at all**:

```
per file, worst case (a file that saturates coarse_cap=30):
  phaze today            30 × 47.86 s = 1 436 s
  + embeddings           30 ×  0.77 s =    23 s   (+1.6 %)
  + embeddings (fused)                 =     0 s
```

**Storage** (mean-pooled float32):

| | per file | 1,000 | 10,000 | 50,000 | 200,000 |
| --- | --- | --- | --- | --- | --- |
| track-level (1 × 1280-d) | 5,120 B | 0.005 GB | 0.051 GB | 0.256 GB | **1.02 GB** |
| window-level (30 × 1280-d) | 153,600 B | 0.15 GB | 1.54 GB | 7.68 GB | **30.7 GB** |

For contrast only: at AudioMuse's published 200 dimensions the track-level figure would be
0.16 GB. The 6.4× is the price of using the model phaze already runs. `phaze-ytgo.6` should treat
**1280-d** as the input dimension, and note that PCA to ~200-d is available if the index needs it.

### E3 — Window-level feasibility: native ~1 Hz, but the tier matters

The embedding is not window-level *by configuration* — it is **patch-level by construction**.
`TensorflowPredictEffnetDiscogs` defaults to `patchSize=128`, `patchHopSize=62`, "which
corresponds to a prediction rate of 1.008 Hz" (its own algorithm description). Measured on 600 s
excerpts: **603 patches, 1.005 Hz**. Every 180 s coarse window in the corpus yielded exactly 180
patch vectors (or fewer for short trailing windows: 119, 96, 80, 76 …).

So embeddings are available at **three** granularities, at three very different prices:

| granularity | how | cost | verdict |
| ----------- | --- | ---- | ------- |
| **track** (1 vector/file) | mean-pool the coarse-window vectors | **+1.6 %, O(1)** — E2 | **free ride** |
| **coarse window** (≤ 30/file) | keep each coarse window's mean-pool | **+1.6 %, O(1)** — same passes | **free ride** |
| **fine window** (≤ 60/file) | — | **new cost** | see below |
| **contiguous 1 Hz** | decode the whole file, no striding | **O(duration)** — E5 | **prices B2** |

**The fine tier is not a free ride, and this is easy to get wrong.** phaze's fine tier decodes at
**44.1 kHz** and runs *no neural model at all* — only `RhythmExtractor2013` and `KeyExtractor`
(`services/analysis.py:385,517-521`). Attaching embeddings there means adding a second 16 kHz
decode *and* a forward pass to a tier that currently has neither. That is a new cost line, not a
marginal one. **Put embeddings on the coarse tier.**

**Track-level aggregate — mean-pool wins, measured.** Both aggregates were computed over each
item's coarse windows and scored on the same P1 separability task (positives = 40 encode-duplicate
pairs; negatives = 298 different-work pairs):

| aggregate | AUC | max positive distance | min negative distance | **hard margin** |
| --------- | --- | --------------------- | --------------------- | --------------- |
| **mean-pool** | 1.0000 | 0.0620 | 0.1373 | **+0.0752** |
| medoid | 1.0000 | 0.1280 | 0.1562 | +0.0282 |

Mean-pool more than doubles the margin. The mechanism is plain: the medoid *is* one window and
inherits that window's idiosyncrasy, so a re-encode whose medoid lands on a different window pays
the full inter-window distance; mean-pool averages window variance away. **Recommendation:
two-level mean — mean-pool patches into a window vector, mean-pool window vectors into a track
vector.** Store the window vectors too; they are the same 30 rows phaze already writes.

**S1's E2/E3 caveat survives intact and must be carried forward.** Coarse windows on a long set
are a *strided sample*: 50 % timeline coverage at 3 h, 25 % at 6 h. A series of coarse-window
embeddings on a festival set is therefore **not a timeline**, no matter how good each vector is.
Nothing in this spike changes that.

### E4 — Near-duplicate separability (P1), with the mandatory EFB arm

**Design.** 45 positive pairs and two negative sets. Distances are cosine over mean-pooled
track vectors.

| class | n | construction |
| ----- | - | ------------ |
| **A** encode-duplicate | 40 | 4 works × C(5,2) over {original, mp3-128, mp3-64, opus-96, aac-96} |
| **B** remaster | 4 | `W1..W4` ↔ their EQ'd/compressed `-master` variant |
| **C** same work, different rendition | **1** | `W5` ↔ `W6` — *natural*, the only one in the archive |
| **D/E** different work | 298 | all cross-work pairs, incl. every track × every set window |
| **F** hard negatives | 132 | pairs of 180 s windows from **the same long set** — different tracks in a countdown, but sharing master and encoder |

**Distance distributions, `effnet` vs `efb` (S1's baseline):**

| class | | n | min | p05 | median | p95 | max |
| ----- | - | - | --- | --- | ------ | --- | --- |
| A encode-duplicate | effnet | 40 | 0.0003 | 0.0007 | **0.0090** | 0.0591 | 0.0620 |
| | efb | 40 | 0.0000 | 0.0001 | 0.0006 | 0.0026 | 0.0033 |
| B remaster | effnet | 4 | 0.0638 | — | **0.0801** | — | 0.0831 |
| | efb | 4 | 0.0023 | — | 0.0101 | — | 0.0110 |
| C rendition (n=1) | effnet | 1 | — | — | **0.0215** | — | — |
| | efb | 1 | — | — | 0.0132 | — | — |
| D/E different work | effnet | 298 | **0.1373** | 0.3040 | 0.4674 | 0.5450 | 0.5732 |
| | efb | 298 | 0.0028 | 0.0141 | 0.0399 | 0.0830 | 0.1068 |
| F same-set hard | effnet | 132 | 0.0192 | 0.0266 | 0.0660 | 0.3058 | 0.3842 |
| | efb | 132 | 0.0007 | 0.0020 | 0.0069 | 0.0494 | 0.0765 |

**Scored against S1's P1 bar (recall ≥ 0.90 at precision ≥ 0.95):**

| positives | space | vs **diverse** negatives (n=298) | vs **+ hard** negatives (n=430) |
| --------- | ----- | -------------------------------- | ------------------------------- |
| **(i) identity share** (A, n=40) | **effnet** | AUC 1.0000 · R@P95 **1.00 PASS** · margin **+0.0752** | AUC 0.9741 · R@P95 0.60 **FAIL** |
| | musicnn | AUC 1.0000 · R@P95 1.00 PASS · margin +0.0019 | AUC 0.9998 · R@P95 **1.00 PASS** |
| | vggish | AUC 0.9964 · R@P95 0.70 FAIL | AUC 0.9773 · R@P95 0.47 FAIL |
| | **efb** (baseline) | AUC 0.9999 · R@P95 **1.00 PASS** | AUC 0.9951 · R@P95 0.82 FAIL |
| **(ii) resemblance share** (A+B+C, n=45) | **effnet** | AUC 1.0000 · R@P95 **1.00 PASS** · margin **+0.0541** | AUC 0.9619 · R@P95 0.53 FAIL |
| | musicnn | AUC 0.9916 · R@P95 0.89 FAIL | AUC 0.9747 · R@P95 0.89 FAIL |
| | vggish | AUC 0.9965 · R@P95 0.87 FAIL | AUC 0.9786 · R@P95 0.49 FAIL |
| | **efb** (baseline) | AUC 0.9978 · R@P95 **0.93 PASS** | AUC 0.9779 · R@P95 0.73 FAIL |

**Four readings, in ascending order of consequence.**

1. **effnet is the only space with a positive hard margin** — a real gap between the worst
   positive and the best negative, on both the identity (+0.0752) and resemblance (+0.0541) sets.
   Every other space, including EFB, already overlaps and depends on a threshold that tolerates
   false positives.

2. **On the identity share, EFB matches effnet against diverse negatives** (both R@P95 = 1.00).
   This is unsurprising and it is not an argument for embeddings: two encodes of one track produce
   near-identical *anything*. Per S1 rule 4 this share is **REDUNDANT** — it is fingerprinting's
   job, and S1's E7 finding is that phaze already owns audfprint + Panako and has them wired only
   to tracklists.

3. **The remaster class is where EFB actually breaks, and effnet does not.** This is the clearest
   measured delta in the document:

   | pair | effnet | efb | musicnn | vggish |
   | ---- | ------ | --- | ------- | ------ |
   | `W1` ↔ `W1-master` | 0.0831 | 0.0023 | 0.0304 | 0.0059 |
   | `W2` ↔ `W2-master` | 0.0801 | 0.0110 | 0.0369 | 0.0057 |
   | `W3` ↔ `W3-master` | 0.0660 | 0.0101 | 0.0646 | 0.0079 |
   | `W4` ↔ `W4-master` | 0.0638 | 0.0036 | 0.0187 | 0.0219 |
   | *different-work minimum* | **0.1373** | **0.0028** | 0.0051 | 0.0135 |

   effnet places all four remasters at 0.064–0.083, entirely **below** its different-work floor of
   0.1373 — a clean 0.054 margin. EFB places them at 0.0023–0.0110, which straddles its
   different-work floor of 0.0028: `W1`↔`W1-master` is *closer* under EFB than some genuinely
   different works are. A differently-mastered rip is named explicitly in S1's resemblance column,
   and **EFB cannot express it while effnet can**. The mechanism is visible in phaze's own genre
   output: `W1` classifies as `Electronic/House`, `W1-master` as `Electronic/Deep House` and
   `Electronic/Tropical House` — the scalar features *moved*, while the embedding did not.

4. **Nothing clears the bar against homogeneous negatives, and that is the honest headline.**
   Class F — two 180 s windows from the same hardcore countdown, i.e. genuinely different tracks
   in the same style and production era — has an effnet minimum of 0.0192, well inside class A's
   range. Recall at precision 0.95 collapses from 1.00 to 0.60. A 200,000-file archive is far more
   F-like than D/E-like. *Two confounds, pulling opposite ways:* F pairs share a master and an
   encoder, which biases them artificially **close** and makes this arm pessimistic; but they are
   also only 180 s excerpts rather than whole tracks, and the D/E set spans four genres, which
   makes the diverse arm **optimistic**. The truth is between, and this corpus cannot locate it.

### E5 — Semantic neighbourliness (P2): unmeasured against S1's bar

**What was attempted and why it does not count.** A mean-average-precision retrieval test over
group labels returned MAP ≈ the random baseline (`effnet` 0.6125, `musicnn` 0.6975, `vggish`
0.5965, `efb` 0.5704, random 0.6417; 28 queries). **That measurement is invalid, not negative.**
The labels were wrong: both long sets were labelled one group, but phaze's own genre classifier
separates them cleanly and correctly — `S1` reads `Electronic/Psy-Trance` and
`Electronic/Progressive Trance`, `S2` reads `Electronic/Hardcore` on every one of its 12 windows.
Two different genres under one label makes the metric meaningless. It is reported here so D1 does
not later find the number and read it as evidence.

**What can be honestly reported.** A label-free retrieval test whose ground truth is objective —
for each of the 24 long-set windows, is its nearest neighbour among the other 23 from the same
set file?

| space | same-set NN accuracy | chance |
| ----- | -------------------- | ------ |
| effnet | **24 / 24 = 1.000** | 0.478 |
| vggish | 24 / 24 = 1.000 | 0.478 |
| musicnn | 23 / 24 = 0.958 | 0.478 |
| **efb** | 23 / 24 = 0.958 | 0.478 |

All four are at ceiling, so **this does not discriminate between methods**, and it is confounded
by the album effect (same-file windows share master and encoder). It establishes a necessary
condition — the embedding is not noise, and it does group psy-trance apart from hardcore — and
nothing more.

**The one structural observation worth carrying to D1** is the dynamic range, which is what makes
a space usable for thresholded retrieval:

| space | same-recording (mean) | different-work (median) | **ratio** | full span |
| ----- | --------------------- | ----------------------- | --------- | --------- |
| **effnet** | 0.01771 | 0.46747 | 26.4× | **[0.0003, 0.5732]** |
| musicnn | 0.00079 | 0.25872 | 325.7× | [0.0000, 0.6437] |
| vggish | 0.01280 | 0.26385 | 20.6× | [0.0003, 0.4043] |
| **efb** | 0.00079 | 0.03989 | 50.2× | **[0.0000, 0.1068]** |

EFB's entire universe of cosine distance is **0 → 0.107**; effnet's is 0 → 0.573, 5.4× wider.
Ratio alone flatters EFB (and musicnn) because both sit near zero for everything; the span is what
a threshold has to live in. **This is an argument about robustness, not about measured quality,
and it must not be presented as the latter.** Per S1 rule 3, no baseline-beating claim is made for
P2: **the cell is `UNMEASURED`.**

### E6 — The P3 signal: shape and price, not accuracy

The native 1 Hz patch series was extracted over three 600 s / 441 s excerpts and scored with a
30 s-lag novelty function (cosine distance between the mean of the preceding 30 patches and the
mean of the following 30 — the standard boundary-detector shape).

| excerpt | patches | rate | **embed time** | **per audio-minute** | decode | adjacent-patch median | novelty median | novelty max | **peak : median** |
| ------- | ------- | ---- | -------------- | -------------------- | ------ | --------------------- | -------------- | ----------- | ----------------- |
| `S2` hardcore set, 3600–4200 s | 603 | 1.005 Hz | 1.73 s | **0.173 s** | 259.1 s | 0.0438 | 0.0472 | 0.4128 | **8.74×** |
| `S1` set, 1200–1800 s | 603 | 1.005 Hz | 1.68 s | **0.168 s** | 47.6 s | 0.0266 | 0.1016 | 0.3484 | 3.43× |
| `W4` single track, 0–441 s | 443 | 1.005 Hz | 1.24 s | **0.169 s** | 2.6 s | 0.0178 | 0.0707 | 0.2204 | 3.12× |

Three findings:

1. **Contiguous embedding is cheap in compute: 0.17 s per audio-minute.** A 3 h set costs **31 s**
   of inference; a 6 h set, **62 s**. This is the number that prices S1's blocker **B2**: the
   Phase 43 O(1) cap exists because `RhythmExtractor2013` on long buffers caused a 4-hour timeout,
   and against that history 31 s for a 3 h set is negligible. **B2's cost objection does not
   survive contact with this measurement — for inference.**
2. **Decode, not inference, is the real O(duration) cost.** 259 s to reach and decode 600 s at
   offset 3600 s in a 6 h mp3. But note the direction: a *contiguous* pass decodes the file once,
   sequentially, whereas phaze's current strided coarse pass performs up to 30 deep seeks into the
   same file. A contiguous P3 pass may well be **cheaper** than what phaze already does. This is a
   concrete, testable claim that `phaze-ytgo.6` should verify rather than assume.
3. **Storage, not compute, is P3's blocker.** At 1 Hz × 1280-d × float32: **55.3 MB per 3 h set**,
   110.6 MB per 6 h set. S1's illustrative 5 %-of-200,000 figure (~10,000 multi-hour sets) gives
   **~0.6 TB** for set embeddings alone, against 1.02 GB for every track-level vector in the
   archive. **P3's vector storage is ~540× the track-level ask for the entire archive.** Any P3
   verdict must reduce dimension, coarsen the hop, or both.
4. **The boundary signal exists and is sharper on a real DJ set** — peak-to-median 8.74× on the
   hardcore countdown (frequent track changes) versus 3.12× within a single continuous track.
   That is a genuine 2.8× contrast in the expected direction. **It is not validated against ground
   truth**: no scraped tracklist exists for these files, so whether the peaks land on real track
   boundaries within ±30 s is **unmeasured** (S1's actual P3 bar).

### E7 — Where each purpose's evidence actually lives

A compact index, because the per-purpose table below compresses hard:

| finding | evidence |
| ------- | -------- |
| embeddings obtainable, node + dimension per family | E1 |
| marginal wall-clock and memory | E2 |
| B2 untouched at track level | E2 |
| window granularity, fine-tier warning, mean-pool vs medoid | E3 |
| near-duplicate separability, EFB arm, remaster delta | E4 |
| semantic neighbourliness (invalid MAP, label-free ceiling, dynamic range) | E5 |
| contiguous 1 Hz cost, novelty shape, P3 storage | E6 |

______________________________________________________________________

## Verdict

**Yes — phaze's existing essentia models can emit embeddings, and the good one is nearly free.**

The `discogs-effnet-bs64-1` graph phaze already loads and already runs on every coarse window
exposes a **1280-dimension embedding at `PartitionedCall:1`**, at a native **1.005 Hz** patch rate,
for **+1.6 % wall clock and +0.75 % RSS** over the analysis phaze performs today — **zero** if the
predictions and embeddings are pulled from a single fused session. Because it rides the coarse
tier, which is already capped at 30 windows per file, it is **O(1) per file by construction and
does not touch blocker B2**. The clean-room option's central technical premise is **sound**.

The quality picture is narrower than the capability picture, and the gap between them is where D1
must be careful. effnet is the only one of the three families with a genuine margin between
same-recording and different-work distances; it is the only space that correctly handles a
differently-mastered rip, which is a named part of P1's resemblance share and which EFB provably
cannot express. But the corpus is **six distinct works**, only **one** positive pair is naturally
occurring, S1's ≥ 50-operator-labelled-pairs bar is not met, S1's 20-seed blind A/B is not
attemptable at all, and against homogeneous same-genre negatives recall at precision 0.95 falls to
0.60. **The capability is established; the quality is directional.**

### Per-purpose impact (S1 rubric, phaze-ytgo.1)

| Purpose | Verdict | Granularity delivered | vs EFB | Evidence |
| ------- | ------- | --------------------- | ------ | -------- |
| P1 dedup + rename | **SERVES-WITH-CAVEAT** — *restriction:* clears recall ≥ 0.90 @ precision ≥ 0.95 **only against stylistically diverse negatives**; falls to R@P95 = 0.60 against same-genre negatives (class F). Positives are **45, of which 44 are constructed** — S1's "≥ 50 operator-labelled pairs" provenance bar is **not met**; treat as directional. Per S1 rule 4 the **identity share is REDUNDANT** (audfprint/Panako own it, S1 E7); the claim here is on the **resemblance share** only. | **track** (mean-pool of ≤ 30 coarse windows) | **better**, on one specific and decisive sub-case: remasters at 0.064–0.083 sit entirely below effnet's different-work floor 0.1373, while EFB puts them at 0.0023–0.0110 astride its floor 0.0028. On the identity share, **same** (both R@P95 = 1.00). | E1, E3, E4 |
| P2 discovery / playlists | **UNMEASURED** | **track** (available and cheap — E2, E3) | **n-m** — no baseline-beating claim is made. S1 rule 3 makes an EFB delta mandatory for any P2 claim and none was obtained. | E5 |
| P3 set/tracklist | **SERVES-WITH-CAVEAT** — *restriction:* the **granularity blocker is removed** (native 1.005 Hz, so **B3 does not apply to the clean-room path** — contrast the sidecar's one-vector-per-track shape) and **B2's compute objection is priced and small** (0.17 s per audio-minute → 31 s for a 3 h set). But S1's actual accuracy bar (≥ 70 % of scraped tracks within ±30 s) is **unmeasured** — no tracklist ground truth was reachable — and **storage is the real blocker: ~0.6 TB** at 1 Hz × 1280-d for S1's illustrative 10,000 multi-hour sets. Existing coarse rows **cannot** serve P3 (S1 E3 stands: strided, 50 % coverage at 3 h). | **window-contiguous** at 1 Hz, *if and only if* a new contiguous pass is added; the **existing** rows are **window-strided** | **better** — EFB has no window-level resemblance capability at all, and S1 scores it "almost nothing, and actively misleading" for P3. | E1, E3, E6 |
| P4 archive QA | **UNMEASURED** | **track** | **n-m** — same reason as P2. S1's prior that P4 is `REDUNDANT` is **not contradicted** by anything measured here. | E5 |

**Two things this table deliberately does not say.** It does not claim P2 or P4, because S1 rule 3
forbids claiming them without an EFB delta and no operator was available to produce one. And it
does not score P1 `SERVES`, because the positive set is 98 % constructed and the negative set is
easier than a real archive's.

______________________________________________________________________

## Recommendation

### For `phaze-ytgo.7` (D1 — the verdict matrix)

1. **Treat "can the clean-room path produce embeddings?" as settled YES**, at a measured
   +1.6 % marginal cost, on the model phaze already runs, with zero new dependencies and zero new
   model files. This was the linchpin question and it resolves in clean-room's favour. If the
   sidecar is chosen for any cell, it is now being chosen *despite* a nearly-free native path,
   not because a native path is unavailable.
2. **Do not resolve P2 or P4 from this spike.** Both are `UNMEASURED` and the missing measurement
   is named and specific: **S1's 20-seed blind A/B with an operator, run over a corpus of at least
   a few hundred distinct works, with the EFB arm alongside.** That is a half-day of an operator's
   time against a populated archive, and it is the single highest-value unblocking action left in
   this molecule.
3. **On P1, separate the two shares before scoring.** The identity share is `REDUNDANT` and the
   highest-value P1 action remains S1's recommendation 5 — wire the existing fingerprint engines
   into the dedup surface — which needs no embeddings at all. The resemblance share has one real
   measured win (remasters) and n = 1 natural rendition pair.
4. **On P3, the blocker moved from compute to storage.** B2's cost objection is priced at 31 s per
   3 h set and is small. B3 (one-vector-per-track) **does not apply to the clean-room path**,
   which is a genuine structural asymmetry between the two options and should be visible in the
   matrix. What replaces both is **~0.6 TB** of vectors, which is `phaze-ytgo.6`'s problem.

### For `phaze-ytgo.6` (vector storage and ANN)

- **Input dimension is 1280, not 200.** Plan the index for it. Track-level at 200,000 files is
  **1.02 GB** raw (float32) — small. Window-level (30/file) is **30.7 GB** — significant.
  Contiguous 1 Hz for P3 is **~0.6 TB** — the dominant term by ~540×, and concentrated in one
  purpose.
- **PCA to ~200-d is a legitimate lever** and would bring the numbers to 0.16 GB / 4.8 GB /
  0.1 TB. This spike did not measure the quality cost of that reduction; doing so is cheap
  (`windows.jsonl` in the scratchpad has all 78 × 1280-d vectors) and should be scoped into `.6`.
- **Verify the decode direction claimed in E6/3**: a single contiguous decode of a long set may be
  cheaper than the ≤ 30 deep seeks phaze already performs. If true, the contiguous P3 pass is
  cheaper than assumed on *both* axes and only storage is left.
- B1 (pgvector absent from the pinned `postgres:18-alpine`) is unchanged by this spike.

### If a clean-room implementation molecule is filed

- **Use the second-pass shape first** (`TensorflowPredictEffnetDiscogs(output="PartitionedCall:1")`
  on the buffer `_run_model_sets` already holds — `services/analysis.py:461-484`). It is +1.6 %,
  needs no reimplementation of essentia's front-end, and is a ~10-line change. Treat the fused
  `TensorflowPredict(outputs=[...])` path as a later optimisation, and validate its patch framing
  against the wrapper before adopting it (E2's caveat).
- **Aggregate by two-level mean-pool**, not medoid: measured margin +0.0752 vs +0.0282 (E3).
  Persist the per-coarse-window vectors as well; they are the same ≤ 30 rows `analysis_window`
  already carries.
- **Do not put embeddings on the fine tier.** It is 44.1 kHz and neural-free; embeddings there are
  a new cost, not a marginal one (E3).
- **Store raw, unstandardised vectors.** Standardisation is corpus-dependent (E4 measured both);
  computing it at query time keeps the stored vector stable as the archive grows.

### Open, and deliberately left unmeasured

| # | Question | Why it is open | Who should close it |
| - | -------- | -------------- | ------------------- |
| S2-O1 | Does effnet separability hold on **homogeneous same-genre negatives** at archive scale? | R@P95 fell 1.00 → 0.60 against the class-F proxy, whose confounds pull both ways (E4/4). | The P1 implementation molecule, against a populated archive. |
| S2-O2 | Measured P2/P4 quality, embedding vs EFB. | No operator, six distinct works (Method). Closes S1's O3 too. | An operator + S1's 20-seed blind A/B. |
| S2-O3 | Does the 1 Hz novelty peak land within ±30 s of a real track boundary? | No scraped tracklist ground truth for either long set (E6/4). | A P3 spike against files that have `Tracklist.source='1001tracklists'` rows. |
| S2-O4 | Quality cost of PCA 1280-d → ~200-d. | Not attempted; decides whether P3 storage is 0.6 TB or 0.1 TB. | `phaze-ytgo.6`. |
| S2-O5 | Is a contiguous single decode cheaper than ≤ 30 strided deep seeks on a multi-hour file? | Observed 259 s for one deep-seek excerpt (E6) but not measured head-to-head. | `phaze-ytgo.6`. |
| S2-O6 | Do the 22 fine-tuned musicnn backbones diverge enough to matter? | phaze holds no canonical MusiCNN backbone, only classifiers (E1 caveat). Only relevant if effnet is rejected. | Only if a 200-d native option is needed. |
