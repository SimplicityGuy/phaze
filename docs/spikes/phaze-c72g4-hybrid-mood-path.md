# phaze-c72g4: a hybrid mood path (EffNet head for some graphs, musicnn for the rest), on fresh held-out files

Spike, 2026-11-15 to 2026-11-16. Bead `phaze-c72g4`, follow-up to `phaze-m3t4d`. Production access as for
`phaze-m3t4d`: the database read-only (`BEGIN READ ONLY`, a statement timeout, `SELECT` only, `ROLLBACK`) on
`host-prod`, and archive audio only inside throwaway containers of the deployed image on the archive host
`host-store`. Aggregates only; no archive identifier appears here.

## Verdict: GO on accuracy for the frozen split; NO-GO for integration

**GO on accuracy for the frozen split (all graphs but mood_electronic: 25/25 on TEST, 20 by construction); NO-GO for
integration: -5.3% CPU (~3,310 of 62,702 cpu-h per pass) doesn't justify the change.**

- **The briefed hybrid failed on SELECTION and was never judged on TEST.** It took 8 scores from the head and kept
  3 graphs on musicnn (happy, gender, voice_instrumental). On the 350-file SELECTION half it passed 17 of the 25
  approved gates and failed 8. The pre-registered rule did not choose it, so it was never run on the TEST half. Its
  energy equals the head-only energy, because none of the three kept graphs feeds energy.
- **One hybrid passes all 25 on files it never saw.** It was chosen by a rule written before any result existed:
  keep 10 of the 11 graphs on musicnn and take only `mood_electronic` from the `m3t4d-v1` head. Judged once on a second
  350 fresh files, it passed 25 of 25. But **20 of those 25 pass by construction**, because they are stored values
  kept as they are. Only 5 were measured.
- **That split saves little.** It drops one graph, 8.47 of 92.84 cpu-s per coarse window. Counting the EffNet embedding
  it adds, the net saving is 4.1 to 4.9 cpu-s per window (4.4 to 5.3%), or about 2,700 to 3,300 cpu-h per archive pass
  of 62,702.
- **Recommendation: do not integrate it** (implementer's view, below). The operator decides. The -5.3% in the
  verdict counts the embedding at production threads. At `phaze-m3t4d`'s two-thread setting the saving is 4.4%.

## Question

`phaze-m3t4d` fitted a head on the Discogs-EffNet embedding to reproduce the 11 stored musicnn (msd) scores. It
failed 4 of the 25 gates the operator approved on 2026-11-13 (recorded on bead `phaze-m3t4d`) on its held-out test: mean absolute error for happy, gender and
voice_instrumental, and the energy scalar (`docs/spikes/phaze-m3t4d-effnet-mood-head-fit.md`,
`docs/design/0028-effnet-mood-head-not-adopted.md`). This spike asks whether a **hybrid** score vector meets the same
25 gates, unchanged, on files the head never saw. A hybrid takes some scores from that head and keeps the rest on the
deployed musicnn graphs. The bead first proposed keeping exactly the three failing graphs (happy, gender,
voice_instrumental). The spike also asks what the hybrid costs per coarse window against today's 11-graph sweep, and
where `energy` comes from.

## Where energy and dominant mood come from

- **Energy** is `set_projection.energy()` (`src/phaze/services/set_projection.py`, the `ENERGY_WEIGHTS` table):
  `clamp01(0.30·danceability + 0.30·mood_party + 0.15·mood_aggressive − 0.20·mood_relaxed − 0.15·mood_sad +
  0.10·bpm_z)`, on the positive-class scores. The approved gate holds the tempo term at zero on both sides.
  **Happy, gender and voice_instrumental do not enter it.** So the three-graph hybrid's energy is exactly the
  head-only energy that failed in `phaze-m3t4d` (measured here as well: the largest window-level difference between
  the two is 0.0). For a hybrid to move energy at all, it must keep at least one of the five energy inputs on musicnn.
- **Dominant mood** is `analysis_derive.derive_mood()` (`src/phaze/services/analysis_derive.py`): the argmax of the
  seven mood positive-class scores (acoustic, electronic, aggressive, relaxed, happy, sad, party). Every mood kept on
  musicnn changes it, happy included.

## Decisions on this bead

| Date | Decision | Whose | Record |
|---|---|---|---|
| 2026-11-15 | How to proceed once the energy identity above was known. Answer as given (selected option label): "Pre-registered selection (Recommended)" | operator, via the dispatcher (`AskUserQuestion`) | comment on bead `phaze-c72g4`, which quotes the question as put |
| 2026-11-16 | The selection rule: candidates, the 99% margin, the objective, the freeze | implementer (`dev/hybrid`), written before any selection-half result existed | comment on bead `phaze-c72g4` |
| 2026-11-16 | The frozen kept-set K | the rule, applied mechanically | comment on bead `phaze-c72g4`, written before any TEST-half score was read |

The thresholds are the 25 approved in `phaze-m3t4d` (2026-11-13), unchanged. Their full list is in that spike doc.

## Method

### Fresh files, and how they are shown disjoint from phaze-m3t4d

`phaze-m3t4d` deleted its file-id lists at cleanup. Its draw ran at a known time, though: its population query is
timestamped 2026-11-13 17:22:10 UTC in its session record. Re-analysis replaces a file's windows wholesale
(`routers/agent_analysis.py`), so a window's `created_at` says when its file was last analysed. Measured read-only:

| msd coarse-window files on the archive host, not missing | files |
|---|---|
| every window created before the draw | **17,133**, exactly `phaze-m3t4d`'s recorded population |
| every window created after the draw | 935 |
| windows on both sides | 0 |

The fresh pool is the 935, minus 8 files whose `analysis` row predates the draw (possible re-analysis): **927
files**. None of them can have been in `phaze-m3t4d`'s population, so none in any of its splits (train 0/1/2, VAL,
TEST). As a cross-check, none of the 927 ids appears among the 1,180 UUIDs that survive in `phaze-m3t4d`'s session
record.

**Weakness, stated.** This is a time-based proof, not an id-level one: a file in `phaze-m3t4d`'s population that was
re-analysed after the draw would look fresh. The analysis-row filter and the exact 17,133 match make that unlikely.
The larger cost is in **exchangeability**, below.

### Two halves, drawn the same way

Both halves use `phaze-m3t4d`'s allocation: file-dominant mood × five duration buckets, a per-mood floor of 15, rest
proportional. Up to 8 coarse windows per file, by a per-file seeded draw. Stored labels that disagree with their own
scores are excluded, as in `phaze-m3t4d`; there were none.

| Half | Seed | Files | Windows | Role |
|---|---|---|---|---|
| SELECTION | 20261115 | 350 | 2,289 | apply the pre-registered rule, choose K |
| TEST | 20261116 | 350 | 2,305 | judge the frozen K once |

The two halves are disjoint. Shortfalls: only one fresh acoustic file existed (`phaze-m3t4d` used all 57 others),
so SELECTION has 1 and TEST has 0. Happy: 23 and 17 (18 were left for TEST).

### The fresh files are a time-ordered tail, not a random draw

The fresh files are the ones analysed after 2026-11-13. Compared read-only with `phaze-m3t4d`'s population:

| | `phaze-m3t4d` population | fresh files |
|---|---|---|
| files | 17,133 | 981 at the time of reading (the pool keeps growing) |
| share from the storage area most fresh files come from | 41% (6,943 files) | 94% (922 files) |
| storage areas represented | 3 | 2 (none from the third) |
| stored SD: relaxed / party / tonality | 0.136 / 0.183 / 0.072 | 0.160 / 0.217 / 0.089 |
| stored mean: aggressive | 0.649 | 0.595 |

Archive weighting corrects the mood × duration mix, not differences within a cell. So the metrics below estimate
how the head does on **newly arriving material**, not on the archive as a whole. That is the material a hybrid would
meet first in production, but it is not the population `phaze-m3t4d` measured.

### Scoring, weights and intervals

The metric code is `phaze-m3t4d`'s:
- weighted Pearson r and MAE per score;
- dominant mood, its agreement where the stored top-two margin is at least 0.10, and macro recall over the classes present;
- energy MAE with the tempo term at zero.

Archive weights follow `phaze-m3t4d`'s rule against today's msd population. Intervals come from 2,000 file-level
bootstrap resamples, stratified by cell. The head is `m3t4d-v1`, loaded from its stored weights (SHA256SUMS verified),
**never refit**. For a score kept on musicnn, the hybrid value **is** the stored value, so its r and MAE gates pass
**by construction**. They are reported that way, never as measured.

### The pre-registered selection rule (implementer's decision)

Written on the bead before any selection-half result existed:
- Candidates are every subset K of the 11 graphs.
- K is admissible if every **measured** gate passes at the conservative end of its selection-half **99%** bootstrap
  interval.
- Among admissible K, the rule picks the fewest graphs, tie-broken by measured CPU.

**Why 99%.** TEST is judged on the point estimate. A gate sitting exactly at a 2.58-SE margin then passes on TEST with
probability about 0.97 before selection bias, against 0.92 at a 95% margin. The extra margin absorbs the winner's
curse of choosing among many subsets. It also hedges the fact that after these two halves the fresh pool is used up.

**Computation.** The search is exhaustive and exact over all 2,048 subsets, because the gates decompose:
- a per-score gate depends only on whether that score is kept;
- energy depends only on which of its 5 inputs are kept;
- the dominant-mood gates depend only on which of the 7 moods are kept.

### Production read path and cost

Each file goes through the deployed image's own path (`video_audio.extract_audio_track`, then
`analysis._decode_windows` at 16 kHz on the stored window bounds). The embedding is
`TensorflowPredictEffnetDiscogs(output="PartitionedCall:1")`, batch 64, mean over patches: `phaze-m3t4d`'s call,
the input the head was trained on.

Graph cost was measured the way `docs/spikes/phaze-fzubr-analysis-cpu-profile.md` measured it:
- the production primitive `analysis._sweep_one_model` (build, sweep, release), timed per graph with process CPU;
- production thread settings (TensorFlow intra/inter and OpenMP at 1);
- 22 SELECTION files, 44 windows, six container types, all five duration buckets;
- all 11 graphs on 16 of the windows, the three originally proposed graphs on all 44.

**Control:** every re-run graph output matched its stored score to **1.8 × 10⁻⁷** at most.

Containers ran one at a time with `--rm --network none --cpus 2 --memory 6g`, the archive mounted read-only, the
deployed image `2026.11.1`, and `nice -n 10`. A 1-minute load guard of 6.5 was checked before each launch, and the
guard held launches several times. Peak RSS was 988 MiB.

## Evidence

### Cost per coarse window (measured)

| Component | cpu-s per window | Source |
|---|---|---|
| All 11 msd graphs (today) | **92.8** (101.5 per 180 s of audio; 13 of 44 timed windows are short trailing windows) | this spike, 16 windows; `phaze-fzubr`: 96.8 (99.0 on full windows) |
| One msd graph | 8.40 to 8.47 (all within 1%) | this spike, 16 windows |
| The three originally proposed graphs (happy, gender, voice_instrumental) | 24.2 (27.9 per 180 s of audio) | this spike, 44 windows |
| EffNet embedding, production threads | 3.57 | this spike, 44 windows; `phaze-fzubr`: 3.71 |
| EffNet embedding, `phaze-m3t4d`'s two-thread setting | 4.40 | this spike, 2,289 windows; `phaze-m3t4d`: 4.50 |
| Head (5-member MLP) | about 6 × 10⁻⁵ | `phaze-m3t4d` (development laptop) |

Because every graph costs the same to within 1%, the CPU tie-break in the rule is close to arbitrary: in practice
the graph count decides.

### The originally proposed hybrid (three graphs kept) on the SELECTION half

The three-graph split was fixed before any fresh file was read, so the SELECTION half is a fair held-out test of it.
This half was not the judging half of the pre-registered procedure, but no choice was made on it for this split.
**It was never judged on the TEST half**: the rule did not choose it, and TEST was spent only on the frozen split.

**17 of 25 gates pass; 8 fail** (6 of the 17 pass by construction). The failures:

| Gate | Value [95% CI] | Threshold |
|---|---|---|
| r mood_acoustic | 0.852 [0.829, 0.873] | ≥ 0.90 |
| r mood_relaxed | 0.887 [0.867, 0.905] | ≥ 0.90 |
| MAE mood_aggressive | 0.0716 [0.0665, 0.0769] | ≤ 0.068 |
| MAE mood_relaxed | 0.0490 [0.0458, 0.0523] | ≤ 0.041 |
| MAE mood_party | 0.0583 [0.0547, 0.0620] | ≤ 0.055 |
| MAE tonality | 0.0240 [0.0200, 0.0289] | ≤ 0.022 |
| Macro recall | 0.621 [0.586, 0.652] | ≥ 0.70 |
| Energy MAE | 0.0379 [0.0354, 0.0405] | ≤ 0.03 |

Two notes on these failures:
- **Macro recall.** The single acoustic file's acoustic-dominant windows all came back wrong (recall 0.0). The mean
  over the other six classes is 0.72. Party recall is 0.36.
- **Head-only, on the same files:** 13 of 25.

Keeping happy on musicnn turned 54 wrongly-dominant windows right and 19 right ones wrong (window counts,
unweighted).

### The pre-registered selection (SELECTION half)

The head's own gates, judged by the rule's 99% bound, clear for **electronic alone**:

| Score | r (99% low) | MAE (99% high) | MAE threshold |
|---|---|---|---|
| mood_acoustic | 0.852 (0.821) | 0.0326 (0.0363) | 0.034 |
| mood_electronic | 0.944 (0.927) | 0.0400 (0.0438) | 0.051 |
| mood_aggressive | 0.901 (0.882) | 0.0716 (0.0782) | 0.068 |
| mood_relaxed | 0.887 (0.860) | 0.0490 (0.0532) | 0.041 |
| mood_happy | 0.897 (0.864) | 0.0578 (0.0650) | 0.046 |
| mood_sad | 0.927 (0.906) | 0.0527 (0.0571) | 0.056 |
| mood_party | 0.922 (0.905) | 0.0583 (0.0630) | 0.055 |
| danceability | 0.922 (0.887) | 0.0393 (0.0447) | 0.047 |
| gender | 0.890 (0.855) | 0.0534 (0.0589) | 0.042 |
| tonality | 0.791 (0.726), not gated | 0.0240 (0.0306) | 0.022 |
| voice_instrumental | 0.857 (0.821) | 0.0719 (0.0774) | 0.053 |

Energy needs at least one of its inputs kept: 0.0379 with none, 0.0273 (99% high 0.0298) with party alone.

**2 of the 2,048 subsets are admissible.** The fewest-graph one is unique at its size, so no tie-break applied:

> **Frozen K = 10 graphs kept on musicnn: everything except mood_electronic.** Only electronic comes from the head.

### The frozen split, judged once on the TEST half

Run once, after the frozen K was on the bead: 350 files, 2,305 windows, 0 decode errors, 0 label-mismatch
exclusions. The thresholds are unchanged.

**25 of 25 gates pass, and no threshold falls inside its 95% interval.** Only 5 of the 25 are measured, though. The
other 20 pass by construction:
- 18 are the r and MAE gates of the 9 gated scores kept on musicnn;
- 1 is tonality MAE, also kept;
- 1 is energy MAE, which is **exactly 0.0000** because electronic, the only head-valued score, is not an energy input.
  The judging script printed it among the measured gates. It is not one.

The five measured gates:

| Gate | Value [95% CI] | Threshold |
|---|---|---|
| r mood_electronic | 0.957 [0.948, 0.966] | ≥ 0.90 |
| MAE mood_electronic | 0.0379 [0.0354, 0.0405] | ≤ 0.051 |
| Dominant-mood agreement | 0.954 [0.943, 0.963] | ≥ 0.85 |
| Agreement where the stored top-two margin ≥ 0.10 | 0.999 [0.998, 1.000] | ≥ 0.95 |
| Macro recall | 0.936 [0.912, 0.955] | ≥ 0.70 |

Recall per mood: acoustic 1.0, electronic 0.96, aggressive 0.971, relaxed 0.913, happy 0.913, sad 0.94, party 0.852.
TEST holds no acoustic-dominant file; the acoustic recall rests on 2 acoustic-dominant windows inside other files.
Electronic's MAE is 0.224 of its stored standard deviation.

The dominant-mood gates measure one thing in practice: how often swapping in the head's electronic score changes
the argmax. They pass with wide room because the other six moods are the stored values.

### What the frozen split saves

Over the 2,431,446 msd coarse windows in the archive, against today's 92.84 cpu-s per window (62,702 cpu-h per
pass):

| Frozen split, counting | cpu-s per window | Saving per window | cpu-h per pass | Saving per pass |
|---|---|---|---|---|
| the 10 kept graphs only | 84.37 | 8.47 (9.1%) | 56,981 | 5,721 |
| plus the embedding at production threads (3.57) | 87.94 | 4.90 (5.3%) | 59,392 | 3,310 |
| plus the embedding at `phaze-m3t4d`'s two-thread setting (4.40) | 88.77 | 4.07 (4.4%) | 59,952 | 2,749 |

The head adds about 6 × 10⁻⁵ cpu-s per window. The first row is the saving only if the EffNet embedding is computed
for another reason anyway; today's deployed sweep does not compute it. For comparison, the three-graph hybrid would
have cost 24.2 + 3.57 = 27.8 cpu-s per window (18,756 cpu-h per pass), but it fails 8 gates.

## Recommendation

This is the implementer's recommendation (`dev/hybrid`, 2026-11-16, bead `phaze-c72g4`). Nobody has decided on
it yet.

**GO on accuracy for the frozen split (all graphs but mood_electronic: 25/25 on TEST, 20 by construction); NO-GO for
integration: -5.3% CPU (~3,310 of 62,702 cpu-h per pass) doesn't justify the change.**

**Do not integrate the frozen split.** It passes the gates, but:
- It saves 4.4 to 5.3% of mood CPU.
- To get that, production would carry a second inference stack: the EffNet graph, the head weights and their
  checksums, a second decode-to-embedding path, and a mixed-provenance score vector that every reader of
  `mood_scores` would have to know about.
- The pass rests on one score. The head's other ten scores failed the rule's margin on the SELECTION half.
- The fresh files are a time-ordered tail from mostly one storage area (see Method), so the electronic result is
  not yet shown for the archive as a whole.

**What would change this:** a reason to compute the EffNet embedding anyway. If production adopted it for another
consumer, its cost would already be paid. The frozen split would then save the full 8.47 cpu-s per window (9.1%,
about 5,700 cpu-h per pass) for the cost of the head. Even then, one graph is the most this head can replace while
meeting these gates.

**For the briefed hybrid (8 scores from the head, 3 graphs on musicnn):** none. It failed on SELECTION (17/25) and
was never judged on TEST. No rule on these gates rescues a split that keeps no energy input
on musicnn.

## Cleanup

Done on 2026-11-16, after the TEST judgement:
- **`host-store`:** 0 spike containers remained (every run used `--rm`; checked with `docker ps -a` before
  removal). The spike's remote work directory (27 MB: scripts, batch manifests, per-file embedding outputs, logs) was
  removed, and a listing afterwards found nothing. No image was built or pulled; the deployed image was used as is.
- **Development machine:** the per-bead scratch directory (30 MB, mode 0700: file lists, stored targets, embeddings,
  scripts) was removed, and a check afterwards found it absent.
- **`host-prod`:** nothing to clean. Every query ran inside `BEGIN READ ONLY` … `ROLLBACK`.

The `m3t4d-v1` head artefact belongs to `phaze-m3t4d` and was left in place.

## What this does not establish

- **How the head does on the archive as a whole.** The fresh files are a time-ordered tail. They are drawn almost
  entirely from one storage area and differ in score spread within cells. The head scores worse here than on
  `phaze-m3t4d`'s TEST, for example acoustic r 0.852 against 0.926. Whether that is the shift or a weaker head than
  `phaze-m3t4d`'s TEST suggested is not separated here.
- **Acoustic.** One fresh acoustic-dominant file existed, in SELECTION. On TEST, acoustic recall rests on 2
  acoustic-dominant windows inside files of other moods.
- **Electronic on the archive as a whole.** The one measured head score was judged on the fresh tail only.
- **A refit.** The head was reused as instructed and not refit on fresher data.
- **The 33-graph (mtt) files**, excluded by design as in `phaze-m3t4d`.
