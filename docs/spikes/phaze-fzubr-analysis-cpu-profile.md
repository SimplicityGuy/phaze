# phaze-fzubr: where one analysis window's CPU goes, measured on real archive audio

Part of the spike molecule `phaze-0toy3` (where analysis CPU goes and how to cut it). This document
carries **aggregates only**. The six archive files appear as `set-01` to `set-06`; no paths, ids,
titles or other identifying values are recorded here, and the production hosts are named by role.
It is the stage breakdown that `phaze-8y6l8` (which measured only the mood arm) left open.

## Question

On real archive audio, with the deployed image and the deployed settings, how is the CPU of one
analysis split across the stages of one coarse (180 s) window and one fine (30 s) window: decode,
fine-tier rhythm/BPM, key, the genre EffNet pass, the mood graph set, result assembly, and (against a
test seat, not production) the database write? And is the instrumented run a faithful copy of what
production stored (the control)?

## Method

- **Selection.** Six already-analysed files chosen by the dispatcher, spread over duration, all mp3
  (128 to 320 kbps): 457 s, 1 781 s, 2 086 s, 3 396 s, 6 772 s and 9 538 s (6.675 audio-hours in all).
  Window counts from the stored rows: 135 coarse windows, 748 stored fine windows (see the
  control note on `set-04`).
- **Stored values.** Read from production Postgres through read-only sessions (`BEGIN READ ONLY`,
  statement timeout, `SELECT` only, `ROLLBACK`): window rows, file metadata and the owning agent's
  path. Nothing was written to the database.
- **Which variant set the deployed agents use.** Read from the deployed analyze agent's container
  environment, read-only and filtered to the analysis settings: `PHAZE_ANALYSIS_MODEL_VARIANTS` is
  **not set**, so the code default applies (`msd`: one `musicnn_msd` graph per characteristic, **11 mood
  graphs, plus the genre EffNet = 12 graphs per coarse window**). The agent runs `TF_NUM_INTRAOP_THREADS=1`,
  `TF_NUM_INTEROP_THREADS=1`, `OMP_NUM_THREADS=1` and `PHAZE_LANE_ANALYZE_CONCURRENCY=4` on an 8-core host.
  The stored windows are mixed: 101 of the 135 coarse windows (`set-02`, `set-05`, `set-06`) hold only the
  `msd` variant, 34 (`set-01`, `set-03`, `set-04`) hold the 33-graph ensemble. Every file was profiled
  with the deployed `msd` set, so the ensemble files are compared on their stored `msd` graph.
- **Audio.** One throwaway container at a time on the analysis host, the deployed image
  (`ghcr.io/simplicityguy/phaze:2026.11.1`): `docker run --rm --network none --cpus 2 --memory 6g
  --read-only`, the archive mount bound `:ro`, models `:ro`, the harness `:ro`, one `:rw` output
  directory, `nice -n 10` inside, the three thread variables above. The host's one-minute load was
  checked before each run (a driver would have waited while it was 7 or more on 8 cores; it never waited)
  and stayed between **4.57 and 6.36** across the runs, because production analysis runs there too. Nothing
  was written to the archive.
- **Instrumentation.** A harness inside the image calls the **production functions** and only wraps
  them with timers (`process_time`, which counts every thread of the process, and `perf_counter`):
  `_analyze_fine_windows` unchanged (so the production 60-window chunking and gated decodes run as
  deployed; the wrapped calls are the decode, `RhythmExtractor2013` and `KeyExtractor`),
  `_decode_windows` for every coarse chunk of every file, `_run_model_sets_over_windows` unchanged
  (wrapped at graph construction and at each graph's call), `_derive_coarse_window`, and
  `_assemble_analysis_result`.
- **Sampling, stated.** The fine tier ran on **every** window. Coarse **decode** ran on every coarse chunk
  of every file. Coarse **inference** (the 12 graphs) ran on 49 of the 135 coarse windows: every window of
  `set-01` to `set-03` (25) and 8 evenly spaced windows (first and last included) of each of `set-04` to
  `set-06` (24). Coarse inference for the other 86 windows is **extrapolated** from the per-window mean of
  the same file; the spread between full-length windows is narrow (98.0 to 101.6 cpu-s for all 11 mood
  graphs, 43 windows), so the extrapolation error is small next to the file-to-file decode spread below.
  Windows were decoded with the production decode and the inference buffers kept; graph construction was
  once per file rather than once per 30-window production chunk, and is reported separately.
- **Extra stage (not in production).** The EffNet **embedding** output (`PartitionedCall:1`) was run on
  the same 49 buffers, to price emitting embeddings.
- **DB write.** Measured **locally against my own test seat** (a dedicated Postgres database and Valkey
  index, not production): the real `put_analysis` route function, called with the stored windows of each
  file as the payload (5 repeats per file, commit included, scheduling-ledger step included with a
  stand-in for the queue table). Local Mac, local database container, so it is a lower bound for the
  network round trip, not a production figure.

## Evidence

### Control: the instrumented run reproduces what production stored

Tolerance stated up front: BPM within 0.05, key and every genre label exact, every score within **1e-6**.

| quantity | result |
|---|---|
| fine windows compared (`set-01` to `set-06`) | 748, **0** BPM mismatches, **0** key mismatches |
| coarse windows compared | 49 |
| max difference, any of the 11 `musicnn_msd` scores | 2.4e-7 |
| `danceability` / derived `mood` / derived `style` (the 26 windows stored as `msd` only) | max 1.8e-7; 0 mood and 0 style mismatches |
| genre top-10 labels, in order | identical on 49 of 49 windows |
| genre confidences | within 1.5e-7 on 48 of 49 windows |
| genre confidences, the exception | **one window** (the trailing window of `set-04`), max difference **2.4e-3** |

The control passes everywhere except one genre window, which is outside the 1e-6 tolerance (labels and
order unchanged). The cause is not established here; that window is the last, partial one of the file
whose stored window end (3 395 s) is 0.87 s short of the probed duration (3 395.87 s), which is a
candidate, not a finding. `set-04` also stores only 60 of its 113 natural fine windows (an older, strided
run); the 53 unstored windows were measured but not compared. The mood scores themselves reproduce to
float precision, and the run was one TF thread as deployed, so the profile below is a valid reference.

### Per-stage cost

CPU seconds, summed over the six files. "Per audio-hour" divides by 6.675 h. Wall time equalled cpu time
to 0.05% (wall/cpu 1.0000 to 1.0014 per file), so the machine was not starving the container.

| stage | cpu-s per fine window (30 s) | cpu-s per coarse window (180 s) | cpu-s per audio-hour | share |
|---|---|---|---|---|
| 11 `musicnn_msd` mood graphs (extrapolated from 49 windows) | n/a | **96.8** (99.0 on full windows, about 9.0 per graph) | 1 957 | **84.5%** |
| genre EffNet (`discogs-effnet-bs64-1`) | n/a | 3.67 | 74 | 3.2% |
| fine-tier decode (44.1 kHz) | 1.48 (0.29 to 2.93 by file) | n/a | 177 | 7.6% |
| fine-tier rhythm/BPM | 0.32 | n/a | 39 | 1.7% |
| fine-tier key | 0.017 | n/a | 2 | 0.1% |
| coarse-tier decode (16 kHz) | n/a | 3.27 (1.44 to 4.34 by file) | 66 | 2.9% |
| derive, assemble, graph build and release, trim | | under 0.01 | under 1 | under 0.1% |
| **Total** | | | **2 316** | 100% |

Reading it: a coarse window costs about 100 cpu-s of inference, of which the 11 mood graphs are 97%
and the genre EffNet 3%; the six fine windows that cover the same 180 s cost about 4 to 20 cpu-s between
them, mostly decode. Graph construction is 0.01 to 0.06 cpu-s per graph (12 graphs: 0.13 s released per
sweep), so the per-30-window chunk rebuild the production chunking pays is below 0.2 cpu-s per chunk.
Peak resident memory per container was 0.82 to 1.07 GiB.

Per file (cpu-s per audio-hour, all stages): `set-01` 2 224, `set-02` 2 219, `set-03` 2 218, `set-04` 2 203,
`set-05` 2 450, `set-06` 2 305. The mood arm is the near-constant (1 910 to 2 031 cpu-s per audio-hour);
decode is what moves.

### Decode is the variable stage

Fine-tier decode, cpu-s per audio-hour of the file: `set-01` 35, `set-02` 96, `set-03` 92, `set-04` 92,
`set-05` **352**, `set-06` 123. Coarse decode: 34, 36, 35, 36, 72, 87. Fine decode spans **about 10 times**
across the six files (352 / 35 = 10.1). `set-05` costs about 3.7 times the median file in fine decode
(352 / 94, the median of the six being (92 + 96) / 2 = 94), and it is not the longest file, nor the highest bitrate (226 kbps against
`set-01`'s 320 kbps, which was cheapest). Within `set-05` the per-chunk fine decode fell from 228 s to
107 s across its four chunks, so the cost is not simply duration or chunk position. The cause was not
isolated in this spike. On `set-02` to `set-06`, fine decode is about 2.3 to 8.6 times the rhythm and key
extractors it feeds.

### EffNet embedding output

A separate embedding-output pass on the same 49 buffers cost **3.71 cpu-s per window** (graph build
0.05 s), the same as the genre pass in the production sweep (3.73 on full windows). Whether one graph run
can emit both outputs was not measured.

### DB-write cost (own test seat, local)

| file | windows (coarse + fine) | payload | `put_analysis` median wall |
|---|---|---|---|
| `set-01` | 3 + 15 | 18 KiB | 19 ms |
| `set-02` | 10 + 59 | 34 KiB | 29 ms |
| `set-03` | 12 + 70 | 72 KiB | 34 ms |
| `set-04` | 19 + 60 | 105 KiB | 44 ms |
| `set-05` | 38 + 226 | 127 KiB | 81 ms |
| `set-06` | 53 + 318 | 179 KiB | 108 ms |

Against 282 to 6 106 cpu-s of analysis per file, the write is 0.002% to 0.007% of the file's CPU. Production adds a network round trip that was not measured.

### Archive-scale projection (an extrapolation)

2 316 cpu-s per audio-hour times the 121 572.3 audio-hours recorded in `phaze-tx202` is about 78 000
cpu-hours at one TF thread (74 400 to 82 700 across the per-file range 2 203 to 2 450). The mood arm
alone projects to about 66 100 cpu-hours, higher than the roughly 58 000 recorded in the epic from a
different sample; this run does not reconcile the two.

## Verdict

**The mood graph set is the CPU. Nothing else is a first-order target.**

- The 11 `musicnn_msd` graphs are **84.5%** of analysis CPU (about 97 cpu-s of a coarse window's 100),
  the genre EffNet 3.2%. Together 87.7%.
- Everything else, decode, rhythm, key, assembly, graph build and the DB write, is **12.3%** in total
  (7.6 + 1.7 + 0.1 + 2.9). An optimisation that left the mood sweep untouched could recover at most
  **15.5%** (100 − 84.5: that 12.3% plus the genre EffNet's 3.2%), and rhythm, key, assembly and the DB
  write are under 2% between them.
- Decode is the only secondary stage with real weight (10.5%), and fine decode is file-dependent by a
  factor of **about 10** per audio-hour (35 to 352 cpu-s, 352 / 35 = 10.1) for a cause not found here.
- The instrumented run reproduces production's stored values (748/748 fine windows exact, 48/49 coarse
  windows within 1e-6, one trailing-window genre difference of 2.4e-3), so these proportions describe the
  deployed code.

Limits: six files, all mp3, 49 sampled coarse windows with the rest extrapolated; the archive also holds
other containers and video, so the decode shares do not generalise to them; load on the host was 4.6 to
6.4 throughout; one TF thread (the deployed setting, so the thread effect in `phaze-8y6l8` is not in
these numbers); DB write is a local-seat lower bound.

## Recommendation

1. Aim the performance plan at the mood sweep. Options to weigh in `phaze-uv8s3`: fewer or shared-trunk
   graphs, the EffNet heads (which `phaze-8y6l8` found are not a drop-in replacement and would need to be
   a new, separately calibrated feature set), TF thread settings (`phaze-phjwi`) and the alternatives
   survey (`phaze-8ghbw`). Anything that does not reduce those 11 graphs is capped at about 15.5% of total (100 − 84.5).
2. Do not spend effort on the rhythm extractor, key extractor, result assembly or the DB write.
3. Treat decode as a follow-up, not a target for this plan: find why fine decode costs 35 to 352
   cpu-s per audio-hour across these files before deciding anything; a 44.1 kHz decode that feeds a 1.8%
   consumer is the obvious place to look. File it as its own bead.
4. For the combined re-analysis: emitting EffNet embeddings costs one more EffNet pass, about 3.7 cpu-s per
   coarse window (3.7% of a window), unless one graph run can emit both outputs; measure that before
   planning the pass.
5. A larger, differently drawn sample (other containers, and the unsampled long-file coarse windows) is
   needed before quoting decode shares for the whole archive.

## Cleanup

Each container ran with `--rm`; `docker ps -a` on the analysis host showed no `fzubr-*` containers
after the last run. The remote workdir (job files, harness, outputs), the local scratch copies of the
selection, stored windows, metadata, results and the harness, and the test seat's database rows were
removed after the figures above were taken (the commands and their output are in the bead report).
