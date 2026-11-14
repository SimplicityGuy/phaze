# phaze-kssjb: analysis performance and mood scores, the current baseline against every option evaluated

**Living document.** Last updated **2026-11-14**. The fitted-head row in table A is **pending `phaze-m3t4d`**;
that bead's held-out results fill it in, and any later measurement edits the row it belongs to. Update the
last-updated line with every edit.

Operator request 2026-11-13 (chat), verbatim: "for all of the investigation to improve performance and mood
model comparisons, please make sure we have a doc that compares current values against all of the evaluated
options". This document gathers the evidence; it does **not** recommend an option (the decision bead is
`phaze-uv8s3`) and it does **not** recommend a head-fit target (the operator left that to `phaze-uv8s3`).

## How to read the numbers

Every value carries a **tag** and a **source**.

| tag | meaning |
|---|---|
| **M-real** | measured, on real archive audio |
| **M-synth** | measured, on synthetic audio (cost transfers; agreement does not) |
| **X** | extrapolated or derived arithmetic from a measurement, not measured at that scale |
| **L** | literature figure, as the cited paper states it, on that paper's benchmark; never measured on phaze audio |
| **P** | pending: a bead that has not reported |

**Host load** is given per source because cpu-seconds inflate under oversubscription. Compare ratios inside one
source, not absolutes across sources. Archive scale throughout is the **121,572.3 audio-hours, 101,000 files**
recorded in `docs/spikes/phaze-tx202-effnet-mood-heads.md` (a production read on 2026-11-11).

**Where the sources live.** The spike docs are not on `main` yet: `phaze-fzubr`, `phaze-phjwi` and
`phaze-8ghbw` are on `wt/bead/epic/phaze-0toy3`; `phaze-webi9`, `phaze-cmsr0` and `phaze-tx202` on
`wt/bead/epic/phaze-vizce`; `phaze-8y6l8` on `wt/bead/issue/phaze-8y6l8`. Paths below are given as
`docs/spikes/<bead>-*.md`. `phaze-l5mzm`'s fused-embedding figures are in the commit message and module docstring of
`66569980` on `wt/bead/issue/phaze-l5mzm` (not merged; the bead carries no comment with them).

## Current baseline

The deployed set: **`msd`, 11 `musicnn_msd` mood graphs plus the genre EffNet = 12 graphs per coarse window**,
image `2026.11.1`. `PHAZE_ANALYSIS_MODEL_VARIANTS` is unset in every deployed lane, so the code default applies
(`phaze-fzubr` and `phaze-phjwi`, container environments read read-only). The *stored* data is mixed: older
windows hold the 33-graph ensemble (101 of 135 coarse windows in the `phaze-fzubr` sample are `msd` only, 34 are
the ensemble).

### Cost per window, per audio-hour, per archive

Source: `docs/spikes/phaze-fzubr-analysis-cpu-profile.md`. **M-real**: six mp3 files, 6.675 audio-hours, 135
coarse and 748 fine windows, deployed image, one TF thread, one container at a time (`--cpus 2`, `nice 10`), host
one-minute load **4.57 to 6.36** on 8 cores (production analysis runs there too). Fine tier measured on every
window; the 12 graphs measured on 49 of 135 coarse windows, the other 86 **X** from the same file's mean (full-window
spread 98.0 to 101.6 cpu-s for the 11 graphs).

| stage | cpu-s per fine window (30 s) | cpu-s per coarse window (180 s) | cpu-s per audio-hour | share |
|---|---|---|---|---|
| 11 `musicnn_msd` mood graphs | n/a | **96.8** (99.0 on full windows; about 9.0 per graph) | 1,957 | **84.5%** |
| genre EffNet (`discogs-effnet-bs64-1`) | n/a | 3.67 | 74 | 3.2% |
| fine-tier decode (44.1 kHz) | 1.48 (0.29 to 2.93 by file) | n/a | 177 | 7.6% |
| fine-tier rhythm/BPM (`RhythmExtractor2013`) | 0.32 | n/a | 39 | 1.7% |
| fine-tier key (`KeyExtractor`, edma) | 0.017 | n/a | 2 | 0.1% |
| coarse-tier decode (16 kHz) | n/a | 3.27 (1.44 to 4.34 by file) | 66 | 2.9% |
| derive, assemble, graph build/release | | under 0.01 | under 1 | under 0.1% |
| **Total** | | | **2,316** (2,203 to 2,450 by file) | 100% |

- **Archive scale (X):** 2,316 cpu-s per audio-hour times 121,572.3 h is about **78,000 cpu-hours** at one TF thread
  (74,400 to 82,700 across the per-file range). The mood arm alone is about 66,100 cpu-hours.
- **Peak RSS** per container 0.82 to 1.07 GiB (M-real, same source).
- **DB write** (M-real, local test seat, lower bound): 19 to 108 ms per file, 0.002% to 0.007% of the file's CPU.
- **Decode is the one variable stage:** fine-tier decode 35 to 352 cpu-s per audio-hour across the six files (a
  factor of about 3.8 between the heaviest file and the median); the cause is not isolated.
- **Control (valid reference):** the instrumented run reproduces production's stored values: 748 of 748 fine
  windows exact, 48 of 49 coarse windows within 1e-6, one trailing window's genre confidences 2.4e-3 off.
- Other measurements of the same quantity, for orientation: `docs/spikes/phaze-8y6l8-effnet-mood-real-audio.md`
  gives 5,725 cpu-s over 56 `msd` windows = **102.2 cpu-s per window** for the 11 graphs and 9,061 cpu-s over 35
  windows = **258.9** for the 33-graph ensemble (M-real, load 5.2 to 6.7; the ensemble ran at 2 threads, cpu
  inflated slightly by thread overhead, see table C). `phaze-phjwi`: 104.8 cpu-s for the 12-graph sweep (M-real,
  4 windows, load 5.0 to 6.7). `phaze-tx202`: 86.1 (11 graphs) and 243.8 (33 graphs) per window (M-synth, Apple
  Silicon, load 7.6 to **22.9**, so less comparable). The first three agree within about 5%.
- **Unreconciled:** `phaze-tx202` extrapolates the default to about 58,160 cpu-hours (synthetic audio, loaded Mac,
  1,722 cpu-s per audio-hour); `phaze-fzubr`'s mood arm extrapolates to about 66,100 (real audio, deployed image).
  `phaze-fzubr` states it does not reconcile the two. This document uses the real-audio, deployed-image figure.

### Thread and worker settings per lane

Sources: `docs/spikes/phaze-phjwi-tf-threads.md` (container environments and compose files read; the burst side was
read live by the dispatcher on 2026-11-13 and relayed to that spike, not read by its author).

| lane | threads (intra / inter / OMP) | concurrency | CPU budget | variants |
|---|---|---|---|---|
| local analyze (8-core host) | 1 / 1 / 1 | 4 workers | no container limit | `msd` |
| cloud compute agent | derived at import (`min(4, physical cores)` intra-op) | 1 | n/a | `msd` |
| burst pods (8-CPU node, up to 4 pods) | not set; derived at import: intra 4, OMP 4, inter 1 | 1 per pod | request 1500m, **no CPU limit** | `msd` |

## Comparison tables

### A. Mood model options

"Cost" is cpu-s per 180 s coarse window for the **model sweep that the option replaces or adds**, and the archive
projection is **X** (cost times 20 windows per audio-hour times 121,572.3 h), with the genre EffNet pass excluded
from the replaced part where the source did so. "Agreement" is against the stored musicnn scores unless stated.

| option | cost per coarse window | archive cpu-h | agreement or accuracy | licence | status |
|---|---|---|---|---|---|
| **musicnn, 11 graphs (deployed)** | 96.8 (M-real, `phaze-fzubr`) | about 66,100 (X) | the reference; control reproduces stored scores to 2.4e-7 (M-real, 91 windows, `phaze-8y6l8`) | CC BY-NC-SA 4.0 per the Essentia models page; the hosted LICENSE file names NC-ND in its title and links the NC-SA text, so ShareAlike vs NoDerivatives is unsettled and only UPF can settle it (`phaze-8ghbw`, L) | deployed |
| musicnn, 33 graphs (ensemble, rollback) | 258.9 (M-real, `phaze-8y6l8`, 35 windows, 2 threads); 243.8 (M-synth, `phaze-tx202`) | about 164,700 (X, from the synthetic figure; about 174,900 from 258.9, X) | against the 11-graph set on synthetic audio: Pearson 0.81 to 0.98 for ten scores but **-0.75 for tonality**, dominant mood changes on 19 of 24 windows (M-synth, `phaze-tx202`: today's own two sets disagree) | same | rollback only (`PHAZE_ANALYSIS_MODEL_VARIANTS=all`); the default was cut 33 to 11 |
| **stock EffNet heads** (11 `*-discogs-effnet-1` heads over one embedding pass) | 2.88 (M-synth, `phaze-tx202`, 24 windows, load 7.6 to 8.4 for this arm); about 3.99 per window over 91 windows (M-real, `phaze-8y6l8`: 363 cpu-s, 1 and 2 threads) | about 1,944 (X, synthetic basis); about 2,700 (X, real basis, 3.99 s) | **not a replacement.** Real audio, 91 windows, 7 files: Pearson 0.41 to 0.86 per score (aggressive 0.48, happy 0.63, sad 0.59, tonality 0.41); mean level shift up to 0.29; dominant mood equals the stored label on **25 of 91** windows (chance is about 13; M-real, `phaze-8y6l8`). Synthetic: heads saturate (electronic 0.991, sd 0.006) and pick `electronic` for 24 of 24 clips (M-synth, `phaze-tx202`) | CC BY-NC-SA 4.0 (same family as today) | evaluated, **NO-GO as a drop-in**; could be a separate feature set with its own keys and thresholds (`phaze-8y6l8` rec. 2) |
| **fitted EffNet head** (a head trained on the EffNet embedding to reproduce the stored musicnn scores) | **P** (the embedding costs what the stock row costs; the head itself was 0.13 to 0.14 cpu-s per 180 s window for each stock head, M-synth) | **P** | **P** pending `phaze-m3t4d`. Operator-approved thresholds (2026-11-13, durable record on `phaze-m3t4d`), all required, on 350 held-out files, decided on the point estimate with any threshold inside its CI flagged: Pearson r >= 0.90 (>= 0.85 for gender and voice_instrumental; tonality reported, not gated); MAE <= 0.3 x stored SD per score; dominant mood matches >= 85% of windows, >= 95% where the stored top-two margin is >= 0.10, macro recall >= 0.70; energy MAE <= 0.03. Control passed on image `2026.11.1`: deployed `msd` graphs re-run against stored scores, max abs difference 1.79e-7 over 10 windows, 10 files, 11 scores (M-real). Pilot container runs: operator-approved to continue as is, one container at a time, 1-minute load guard under 6.5 | same family as the stock heads if the embedding is Discogs-EffNet; any labels used need their own licence check (`phaze-8ghbw`) | **in progress**: `phaze-m3t4d`. Which scores to fit to (stored musicnn vs an operator-labelled sample) is `phaze-uv8s3`'s question |
| **shared musicnn front end** (one backbone pass feeding the 11 heads) | **P** | **P** | **P** pending `phaze-0toy3.4` (checks whether the 11 graphs' front ends are byte-identical; tolerance aggregated abs diff <= 1e-3, zero top-1 flips). Expected zero label drift **if** identical; not measured | as today | open |
| **clean-room route** (own embedding, no Essentia code, models or methods) | **P** | **P** | **P** pending `phaze-0toy3.5`. Operator statement 2026-11-13 (on `phaze-m3t4d`): "as long as we're not using their code or methods (e.g. clean room), we can develop a system that provides similar results for each mood". Training on the stored musicnn scores, or distilling Essentia model outputs, still learns from non-commercial model outputs; `phaze-0toy3.5` records that as a risk, not a legal opinion | to be established by `phaze-0toy3.5`; candidate bases named there include permissively licensed repositories (checked from the licence API, not yet verified end to end) | open |
| research shortlist, #3 EfficientAT MobileNetV3 (`mn10`, `mn04`, `mn01`) as an embedding | not measured; EffNet is already about 3 to 4 cpu-s per window, so the gain over it is not shown | n/a | L: 0.483 AudioSet mAP; `mn01` about 20 M MACs per 10 s and said comparable to EffNet-B2 on HEAR 2021; no mood figure; needs a head on labels phaze does not own | MIT repository; no separate weights licence stated | hold (`phaze-8ghbw`) |
| research shortlist, #4 MAEST (30 s) | none or negative: about 87 M parameters; speed claimed only against CNNs under strong patchout, hardware unnamed, no absolute throughput | n/a | L: MTG-Jamendo mood/theme ROC-AUC 78.1 vs 75.6 for the EffNet-B0 baseline (a different label space from the stored scores) | weights CC BY-NC-SA 4.0; code AGPL-3.0 | defer |
| research shortlist, #6 MERT, MuQ, LAION-CLAP | negative (95 M to 330 M parameters, or an HTSAT-base backbone) | n/a | L: accuracy upgrades on their own benchmarks; no mood agreement with phaze | MERT and MuQ weights CC BY-NC 4.0; CLAP CC0-1.0 | reject on CPU grounds |

**Reading the table.** The cost saving of any EffNet-embedding route is real and measured (about 30x on the model
sweep, `phaze-tx202`; about 41x over 91 real windows, `phaze-8y6l8`). The unresolved question for every such
route is whether the scores **mean what the musicnn scores meant**, which only the fitted-head row can answer.
Two caveats limit what the real-audio rows prove: `phaze-8y6l8` used 7 purposively chosen files (windows within a
file are strongly correlated), and the stored `mood` label disagrees with the argmax of its own stored scores on
20 of 25 ensemble-stratum windows there, so "agreement with the stored label" is not a clean target in that
stratum (cause not established).

### B. Embedding options

The 1280-d `discogs-effnet-bs64-1` embedding is `PartitionedCall:1`, float32, one row per patch (about one patch
per second); one pooled vector per window (`phaze-webi9`, M-synth: seeded noise, load 171 to 229, Apple Silicon;
the graph's own sidecar declares the output).

| option | added cost per 180 s window | added archive cpu-h | size per window | notes and source |
|---|---|---|---|---|
| none (today) | 0 | 0 | 0 | deployed |
| **embedding replaces the genre logits** | +0 (cpu 2.99 vs 3.05, M-synth, `phaze-webi9`) | 0 | n/a | the node sits before the classification head; only useful if genre logits are dropped |
| **second wrapper pass** (logits, then embedding) | about +3.0 (both-pass 5.92 vs 3.05, 1.9x; M-synth, `phaze-webi9`); +3.71 on real windows (M-real, 49 windows, load 4.6 to 6.4, `phaze-fzubr`) | about **2,060** (X, `phaze-webi9`, 61 cpu-s per audio-hour); about 2,500 on the real basis (X) | 5,120 B float32 | +4.5% of the 12-graph pass (X, `phaze-webi9`); +3.7% of a window (M-real, `phaze-fzubr`) |
| **fused** logits + embedding in one session (`FusedEffnetDiscogs`, low-level `TensorflowPredict`, `phaze-l5mzm`) | **about 0**: 2.86 to 3.02 cpu-s vs 2.79 to 2.86 for one wrapper pass and 5.68 to 6.07 for two | about 0 (X) | 5,120 B float32 | M-synth: seeded noise and a tone mix, essentia-tensorflow 2.1b6.dev1438, arm64 Darwin, 3 windows x 3 repetitions x 2 rounds, load **9 to 14**. Both outputs `np.array_equal` to the wrapper's (max abs diff 0.0) at 2.1 to 200.5 s. Real-essentia equality and flat-RSS tests exist on the branch. Source: commit `66569980`, **branch not merged, bead has no comment with these figures** |
| **embedding as a by-product of an EffNet mood route** | 0 beyond the route | 0 beyond the route | as above | any route in table A that runs the EffNet pass makes the embedding free (`phaze-tx202`) |

**Not measured anywhere:** the fused route on the amd64 app image or the arm64 agent image, and cross-arch
bit-equality of the vectors (`phaze-webi9` records the vectors bit-identical across processes on one host only).

**Storage** (`docs/spikes/phaze-cmsr0-embedding-storage.md`; row sizes **M-synth** in a throwaway PostgreSQL 18.6
with pgvector 0.8.1, heavily loaded host, sizes are content-independent; totals **X** at 121,572.3 h; decimal GB).

| shape | bytes per row | coarse tier (2,431,446 rows) | fine tier (14,588,676 rows) | index | query notes |
|---|---|---|---|---|---|
| `bytea`, float16 | 2,890.1 | **7.03 GB** | 42.16 GB | none | per-file fetch p50 7.4 ms; exact kNN only app-side (6.22 GB resident matrix at coarse, about 0.66 s) |
| `halfvec(1280)` | 2,890.1 | 7.03 GB | 42.16 GB | HNSW 4,096.4 B/row (9.96 GB coarse, 59.76 GB fine) | kNN top-11 p50 7.7 ms with HNSW; recall@10 0.900 on synthetic vectors |
| `vector(1280)` (float32) | 6,326.3 | 15.38 GB | 92.29 GB | HNSW 8,192.4 B/row (19.92 GB coarse) | kNN p50 14.3 ms; recall@10 0.885 |
| `real[]` | 6,326.3 | 15.38 GB | 92.29 GB | none | per-file fetch p50 40.4 ms; no SQL kNN |
| parquet float16 (zstd) | 2,414.5 | 5.87 GB (uncompressed float16) | 35.25 GB | none | offline load only |
| file-level pooled vector | 2,890.1 | 0.29 GB for 101,000 files | n/a | exact scan about 37 ms in SQL | dedup at file level about 92 s exact |

`phaze-cmsr0` conclusions that matter here: coarse-first holds on size; exact window-level kNN at the coarse tier is
about 0.9 s in SQL, which misses a 200 ms budget, so interactive window similarity needs `halfvec` plus HNSW
(7.03 GB data + 9.96 GB graph = 17.0 GB); `bytea` converts losslessly to `halfvec` later. Its free-space figure
(47 GB on the production volume, recorded 2026-07-26) is stale and must be re-read before any tier is chosen.

### C. Throughput settings

Source: `docs/spikes/phaze-phjwi-tf-threads.md`. **M-real**: 4 full 180 s windows from three files, decoded once and
fed to every configuration, the deployed 12-graph sweep, deployed image, **2-CPU container budget** unless noted,
host load **5.0 to 6.7** on 8 cores, **no configuration repeated** (the only noise estimate is about 3%, the spread
among three 2-thread variants).

| setting | wall-clock windows per hour | windows per cpu-hour | result difference vs control |
|---|---|---|---|
| 1 worker, 1 thread (deployed per worker; control) | 34.3 | **34.4** | 0 |
| 1 worker, 2 threads (intra 2 / inter 1 / OMP 2) | 62.1 (+81%) | 32.5 (-5.5%) | 1.2e-7 |
| 2 workers, 1 thread each | **63.7** (+86%) | 32.1 (-6.7%) | 0 |
| 1 worker, 4 threads (2x oversubscribed) | 56.7 | 28.5 (-17%) | 1.2e-7 |
| 4 workers, 1 thread each (2x oversubscribed) | 58.0 | 29.1 (-15%) | 0 |
| 1-CPU budget, 2 threads (2x oversubscribed, 2 windows) | 32.4 | 32.4 | 1.2e-7 |

Real-audio corroboration at 2 threads vs 1 thread: `docs/spikes/phaze-8y6l8-effnet-mood-real-audio.md` ran the
ensemble at 2 threads and saw the same near-halving of wall time (M-real; not a controlled comparison).
`src/phaze/services/analysis_sizing.py` records a single process at 4 physical cores going from 475.8 s at 1 thread
to 174.4 s at 4 (2.7x), cited by `phaze-phjwi`, not re-measured.

| lane change | expected gain | tag and basis |
|---|---|---|
| local analyze: raise `PHAZE_LANE_ANALYZE_CONCURRENCY` from 4 to 5 or 6, threads stay 1 | **+25% to +50%** windows per wall-hour if the cores are free; 6 workers would leave under 1 core for everything else | **X**, projected from the 2-CPU matrix, not measured at 8 cores |
| burst pods: pin `TF_NUM_INTRAOP_THREADS=2` and `OMP_NUM_THREADS=2` in `phaze-agent-env` | **+10% to +14%** windows per wall-hour while all 4 pods are busy | **X**, from the 2-CPU arm; not measured on that node; contradicts the current advice in `docs/k8s-burst.md` |
| cloud compute agent | none (one worker already fills its cores) | argument, no extra measurement |
| inter-op or OMP changes beyond the above | none (228, 232, 235 s for the three variants) | M-real, no repeats |

Threads never improve CPU efficiency: 5% to 7% worse per cpu-hour at 2 threads, 14% to 17% worse when
oversubscribed. They buy wall-clock only where the budget has idle CPUs. Both lane changes are operator changes with an agent restart (`phaze-phjwi`); this
document did not check whether either has been applied.

**Not in this table, not measured:** a burst-pin run of the node itself; a 4-CPU or 8-CPU arm; the effect of
model-major batching, which `phaze-8ghbw` calls a measured, non-model lever decided separately; no figure for it
was found in the sources read here.

### D. Rhythm and key alternatives

Today's rhythm and key together are **2.0%** of analysis CPU (0.32 + 0.017 cpu-s per 30 s fine window against 1.48
for decode; M-real, `phaze-fzubr`). Fine-tier decode is 7.6%. Nothing here is a CPU lever on its own: replacing
rhythm and key entirely would recover at most about 2%.

| option | replaces | cost | accuracy | licence | status |
|---|---|---|---|---|---|
| `RhythmExtractor2013(multifeature)` (deployed) | n/a | 0.32 cpu-s per 30 s window, 39 cpu-s per audio-hour (M-real, `phaze-fzubr`) | stored values reproduce exactly on 748 of 748 fine windows | Essentia, AGPL-3.0 | deployed |
| `KeyExtractor(edma)` (deployed) | n/a | 0.017 cpu-s per window (M-real) | as above | as above | deployed |
| **Beat This!** (full, about 20 M parameters; small, about 2 M) | `RhythmExtractor2013` | **not measured**; the paper reports training cost only; no tempo output | L: GTZAN beat F1 89.1 / downbeat 78.3 (small: 88.8 / 77.2) vs 88.7 / 75.6 for a DBN system; continuity worse (CMLt 79.8 vs 81.2; AMLt 89.8 vs 92.0) | MIT code and weights per the README, with a training-data caveat the operator accepted 2026-11-13 (`phaze-0toy3.3`) | **P** `phaze-0toy3.6` (accuracy on beat-annotated audio, cost, grid stitching); couples to `phaze-7hcgn` |
| Beat Transformer, BeatNet, allin1, madmom RNN + DBN | `RhythmExtractor2013` | L: BeatNet 5.23 s for a 30 s clip on the authors' setup; the others need Spleeter or Demucs demixing | higher or equal, or below Beat This! on the same GTZAN | madmom model files CC BY-NC-SA 4.0 (code BSD); others MIT | rejected (`phaze-8ghbw`) |
| InceptionKeyNet | `KeyExtractor(edma)` | not reported | L: weighted MIREX 74.46% on its own sets; **not compared with edma** | MIT repository | defer; the key stage is 0.1% of CPU, so there is no case |
| fine-tier **decode** (44.1 kHz) | n/a | 1.48 cpu-s per window, 35 to 352 cpu-s per audio-hour by file | n/a | n/a | the one secondary stage with real weight (10.5% with coarse decode); `phaze-fzubr` recommends finding why it varies 3.8x before deciding anything, as its own bead |

## Reconciliation with the open research beads

- **`phaze-0toy3.3`** (reconcile the two `phaze-8ghbw` documents and land one by PR). This document **does not
  restate** the survey's licence analysis, per-paper figures beyond those cited in tables A and D, or its
  integration notes; it cites the survey at the figures above. It was written against the version on
  `wt/bead/epic/phaze-0toy3` (the local run). The survey's other version, the cloud run (commit `816b2ff4`), which
  carries the `phaze-mqq5` ONNX/int8 measurements and the `phaze-zaf2l` fine-tier decode share, was **not read for
  this document**. Once `phaze-0toy3.3` lands, re-check the table A and D rows that cite `phaze-8ghbw` against the
  reconciled text, and replace its "phaze-measured" 102 and 259 cpu-s figures with this document's sources where they
  differ. `phaze-8ghbw`'s statement that the stage share of a window is "not yet measured" is superseded by
  `phaze-fzubr`.
- **`phaze-0toy3.4`, `.5`, `.6`** are all open with expired leases. Their rows are marked **P** above and need
  filling when each reports; none of their results is known here.
- **`phaze-m3t4d`** fills the fitted-head row (this bead or a follow-up, per the acceptance criteria). Until then the
  row states only the approved thresholds and the control.

## Gaps this document found

1. No real-audio agreement figure exists for any route except the stock heads (7 files, 91 windows).
2. The fused embedding figures (`phaze-l5mzm`) are synthetic-audio, Apple Silicon, load 9 to 14, and are not on any
   bead comment; they should be recorded there.
3. The archive-scale cost of the default differs by about 14% between two sources (58,160 vs 66,100 cpu-hours) and is
   not reconciled.
4. No throughput measurement above a 2-CPU budget, none repeated, and the burst-pod gain is carried across to a
   different scheduler regime.
5. `phaze-cmsr0`'s storage free-space reference is stale (2026-07-26).
6. The stored `mood` label disagrees with its own stored scores on 20 of 25 ensemble-stratum windows in the
   `phaze-8y6l8` sample; the cause is open and it affects what "agreement with stored" means.
