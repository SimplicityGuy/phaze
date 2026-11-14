# ADR-0026: Storage for sub-window mood and the per-file beat grid

Date: 2026-11-10. Bead: phaze-f6eeb. Epic: phaze-7hcgn. Status: accepted.

Audio analysis computes two things at a finer resolution than it stores, then discards them. Every
mood graph returns a `[patches, classes]` matrix, and `_predict_single` in
`src/phaze/services/analysis.py` averages it into one vector per 180 s coarse window.
`RhythmExtractor2013` returns beat ticks for every 30 s fine window, and `_measure_fine_window`
drops them. This record fixes how both are stored: the sub-window length, the two storage shapes,
and what was measured to choose them. Migration `098` implements it. The rest of the epic (pooling,
stitching, the agent wire, track segments, the timeline) builds on these shapes.

Every choice below was made by the implementing seat, inside the design space the bead states, and
is open to revision at review.

## 1. Measured patch geometry of the mood graphs

Measured on the REAL graphs (`~/essentia-models`, the pinned manifest), essentia 2.1-beta6-dev,
macOS arm64. The buffers were seeded synthetic noise at 16 kHz, not archive audio. A graph's patch
count depends only on the buffer's sample count, never on its content: the framing comes before the
network. The geometry is the closed form below, pinned by
`tests/analyze/services/pipeline/test_subwindow_and_beat_grid_storage.py::test_each_deployed_mood_graph_emits_patches_on_the_stated_hop`.

`frames = (samples - frame_size) // frame_hop + 1`; `patches = (frames - patch) // hop + 1`.

| Graph (classifier) | Frame size / hop (samples @ 16 kHz) | Patch | Patch hop | Patches at 30 / 60 / 179.5 / 180 s |
|---|---|---|---|---|
| `*-musicnn-msd-2` (musicnn) | 512 / 256 (16 ms) | 187 frames = 2.992 s | 93 frames = **1.488 s** | 19 / 39 / 119 / 119 |
| `*-musicnn-mtt-2` (musicnn) | 512 / 256 (16 ms) | 187 frames = 2.992 s | 93 frames = **1.488 s** | 19 / 39 / 119 / 119 |
| `*-vggish-audioset-1` (vggish) | 400 / 160 (10 ms) | 96 frames = 0.96 s | 93 frames = **0.93 s** | 32 / 64 / 192 / 193 |

The formula holds from one patch length upwards. A shorter buffer still yields one patch: a 3 s
buffer gave 1 musicnn patch where the formula gives 0. That is the short-buffer case `_predict_single`
already normalizes with `np.atleast_2d`.

This agrees with the batch-size check recorded beside `_construct_classifier` (musicnn 402, VGGish
645 patches on one buffer). Both counts give a buffer of about 600 s under these formulas. The default
deployment runs only the `musicnn_msd` head of each mood set (`PHAZE_ANALYSIS_MODEL_VARIANTS=msd`).
The `all` rollback adds `musicnn_mtt` and `vggish`, averaged per set as the coarse vector already is.

## 2. Sub-window length: 15 s, with patches attributed by centre

**No length divides cleanly, so the choice turns on attribution.** Patches overlap: a 2.992 s musicnn
patch advances 1.488 s. So some patch straddles any boundary, whatever the length. Lengths that are
a whole number of hops for both graphs (the LCM of 1.488 s and 0.93 s is 7.44 s, so 7.44 s and
14.88 s) still leave patches straddling. They also fail to divide the 180 s coarse window (24.19 and
12.10 slices), which leaves a 1.44 s fragment at the end of every coarse window.

**Attribution rule.** A patch belongs to the one sub-window that holds its centre, half-open
`[start, end)`, the same midpoint rule the track segments already apply to coarse windows. Patch `k`
of a coarse window's buffer is centred at `window.start_sec + k * hop + patch / 2`: musicnn
`1.496 + 1.488 k`, VGGish `0.48 + 0.93 k`. Every patch is counted exactly once, so for each variant
the patch-count-weighted mean of a coarse window's sub-window vectors equals that window's mean. That
is the discriminating test the pooling bead owes. Under the `all` rollback, variants are averaged per
set after pooling, so recovery across variants holds only within the tolerance that bead states. A
full 15 s slice holds 10 or 11 musicnn centres (16 or 17 VGGish). The last slice of a full 180 s
window holds 9 musicnn centres; the last centre sits at 177.08 s.

**A slice with no centre stores NULL mood**, a recorded gap and never an invented vector. This
happens only in the trailing few seconds of a short trailing coarse window. Example: in a 16 s window
the last musicnn centre is at 13.40 s, so the `[15, 16)` slice is empty. The audio after the last
patch of any window is already uncovered today, so this is not a new loss.

**Why 15 s.** It is the bead's ceiling, and it divides both the 180 s coarse window (12 slices) and
the 30 s fine window (2 slices). So with the default window lengths every boundary lands on one
file-wide 15 s grid that lines up with the fine windows, and therefore with the BPM, the key and the
beat grid. Slices are laid from each coarse window's own start
(`src/phaze/services/analysis_windows.py::iter_subwindows`), because a patch belongs to one coarse
window's buffer. A `coarse_window_sec` that is not a multiple of 15 therefore still yields correct
slices. Every coarse window yields `ceil(length / 15)` slices, the last one clipped.

Rejected lengths:

- **10 s.** It also divides 180 and 30. It costs 1.5× the rows (360/h against 240/h) and leaves 6 or 7
  musicnn patches per slice. The consumers' need is met at 15 s: the case the epic names is a track
  under 180 s, and a 60 s track gets 4 slices.
- **7.44 s / 14.88 s (whole hops).** See above: they buy nothing, because patches overlap, and they
  break the alignment with the coarse and fine windows.

## 3. Sub-window mood: a child table, `analysis_subwindow`

One row per slice, keyed `(file_id, window_index, sub_index)`. `window_index` is the coarse window's
own `analysis_window.window_index`, and `sub_index` counts slices inside it from 0. `start_sec` and
`end_sec` are file seconds, stored so no reader re-derives them and a later change of length cannot
re-time stored rows. `mood_scores` is a `float8[]` positional in
`src/phaze/services/set_projection.py::MOOD_ORDER`, the `set_profile.mean_vector` precedent. It is
NULL per §2. The FK to `files` uses `ON DELETE CASCADE`. There is no FK to `analysis_window`, because
`_replace_analysis_windows` deletes and re-inserts those rows, so their `id` changes on every
re-analysis. The table has no surrogate id and no timestamps: rows are written once and replaced
wholesale, never updated.

**Sizes, measured.** I loaded 1,000 synthetic files × 1 h each (240 slices/h, 11-value vectors of
random float64) into PostgreSQL 18.6 on the test harness, then read `pg_total_relation_size` (table +
TOAST + primary key). I scaled the per-hour figures to the archive as it stands on 2026-11-11: 121,572.3 h of audio across
101,000 files with a duration (a read-only production query,
`SELECT count(*), sum(duration)/3600 FROM metadata WHERE duration IS NOT NULL`). Row count = hours ×
240. The per-hour measurements are the ones this record was first written with; only the scale was restated.
An earlier revision scaled them to 11,492.2 h across 11,412 files
(`docs/design/0002-fingerprint-removal.md`, measured 2026-07-28) and to a 120,429.1 h set-like inventory
(`docs/spikes/phaze-5rhjq-collapse-ratio.md`); the archive is about 10.6× the first of those and has
overtaken the second, so neither is used here. The ranking of the shapes is the same at every scale,
because every figure is linear in hours, so the choice below is unchanged.

| Shape | Per hour of audio | At 121,572.3 h (29,177,352 rows) |
|---|---|---|
| **Child table, `float8[]` (chosen)** | **59.2 KB** | **7.2 GB** |
| Child table, `float8[]` + `created_at`/`updated_at` | 63.4 KB | 7.7 GB |
| Child table, JSONB array | 93.4 KB | 11.4 GB |
| Child table, `real[]` (float32) | 52.5 KB | 6.4 GB |
| JSONB array of 12 vectors on the coarse `analysis_window` row (added bytes only) | 39.9 KB | 4.9 GB |

At this scale the chosen table is about 29 million rows, not the 2.8 million the earlier scale gave.

Rejected shapes:

- **A JSONB array on the coarse row.** It is the smallest, but it costs in the wrong place. At 1,660 B
  per coarse row it stays below the 2 KB TOAST threshold, so it is stored inline and widens every
  `analysis_window` heap page. That is the table whose `mood_scores` column exists precisely to keep
  reads narrow (`AnalysisWindow`'s docstring). Every full-entity `select(AnalysisWindow)` (the
  proposals and record pages, the set-projection backfill) would also carry it. And a track segment
  could not select sub-windows by time in SQL: it would have to load every coarse row and expand it.
  The child table answers `file_id = :f AND start_sec < :end AND end_sec > :start` from its primary-key
  prefix, at most about 2,900 rows for the longest file.
- **JSONB array in the child table.** It is 1.6× the size of `float8[]` for the same values.
- **`real[]` in the child table.** It saves 11%, but the writer computes float64 means, and the
  coarse `mood_scores` stores them at float64. float32 sub-window vectors would round differently from
  the coarse vector they are tested against, and a value the agent sends would not read back unchanged.

## 4. Beat grid: a 1:1 table, `analysis_beat_grid`, `real[]`

One row per file: `beats` holds every beat as `real[]` (float4), in file-relative seconds, strictly
ascending, stitched across fine-window seams. `beat_count` is stored beside it, with a CHECK
`cardinality(beats) = beat_count`. `gaps` is a JSONB list of `[start_sec, end_sec]` spans the
stitcher recorded instead of filling (silent or failed windows). It defaults to `[]`. The FK to
`files` uses `ON DELETE CASCADE`, and the table carries `TimestampMixin`.

**float32 is enough, measured.** `RhythmExtractor2013(method="multifeature")` returns float32 ticks
quantized to its 512-sample hop at 44.1 kHz, which is 11.61 ms. The float32 spacing at the longest
analyzed file (12.07 h) is 3.9 ms, so rounding moves a beat by at most 1.95 ms, under a sixth of the
tick resolution. Pinned by `test_real_beat_ticks_are_float32_on_the_512_sample_grid_at_44_1_khz` and
`test_float32_moves_a_position_by_at_most_two_milliseconds_up_to_the_longest_file`. The writer rounds
with `src/phaze/services/beat_grid.py::stored_beat_grid` before the grid goes on the wire, so what an
agent sends is what a read returns, bit for bit.

**Sizes, measured** the same way: 1,000 files × 1 h at 128 BPM, so 7,680 beats per file. The real
density depends on tempo; 128 BPM is the bead's own reference. A 2 h set at 128 BPM is 15,360 beats,
and the longest file is about 92,700.

| Shape | Per hour of audio | At 121,572.3 h |
|---|---|---|
| **`real[]` (chosen)** | **33.3 KB** | **4.0 GB** |
| float32 `bytea` | 33.8 KB | 4.1 GB |
| float64 `bytea` | 64.4 KB | 7.8 GB |
| JSONB array | 66.5 KB | 8.1 GB |

Rejected shapes:

- **JSONB array.** It is twice the size of float32, for precision the ticks do not have.
- **float32 `bytea`.** It is the same size as `real[]` but opaque: it needs a bespoke codec on every
  reader and cannot be read or unnested in SQL. `real[]` round-trips through asyncpg as a list of
  floats and admits the `cardinality` CHECK.
- **A column on `analysis`.** Every query that loads whole `AnalysisResult` entities would read a
  grid of up to about 370 KB it never uses. A separate 1:1 table keeps that read explicit, the
  `set_profile` precedent.

## 5. Lifecycle

Both tables are analysis output of a file's bytes, and they move with the rest of it:

- They cascade on file deletion.
- `invalidate_content_state` (`src/phaze/services/scan_deletion.py`) deletes them when a file's
  content changes.
- `_merge_children` (`src/phaze/services/stale_rows.py`) moves them with a completed analysis that
  wins a duplicate merge, and drops the kept row's copies.

Re-analysis replaces them: the wire bead deletes and re-inserts them in the same transaction as
`analysis_window`.
The grids belong to the run that wrote the windows (phaze-ek9zf). `put_analysis` therefore replaces
both tables whenever it replaces the windows: written if the payload carries `subwindows` /
`beat_grid`, DELETED if it omits them. Omission means "this run produced none", never "keep the old
ones", so a re-analysis by an agent on the previous image leaves no stale grid beside its new windows.
An aggregate-only PUT (no `windows`) touches them only if it carries them.

**Rollout order: server first, then agents and burst images.** The two payload fields are optional, so an
OLDER agent on a NEWER server is valid. The reverse is not: the previous server's `AnalysisWritePayload` is
`extra="forbid"`, so a NEWER agent's completion PUT is answered 422, and `FAILURE_IS_TERMINAL[ANALYZE]` makes
that permanent, discarding a finished multi-hour analysis. Never start an agent or burst image that sends
grids against a control plane that has not been upgraded. See `docs/deployment.md` ("Upgrading: server
first, then agents"). Both tables start empty. Only a re-analysis fills them, and the archive-wide
re-analysis is deliberately held out of this epic.

## 6. Blast radius

This change creates two empty tables and touches no existing table, column or row. Two existing
paths gain statements over the new tables only: content invalidation and the duplicate merge. Nothing
writes the tables yet, so on the deployed system those statements delete zero rows.

What currently works that this could break:

- the existing `analysis` / `analysis_window` schema;
- the content-invalidation path;
- the duplicate-merge path.

The tests that prove each still works:

- `tests/shared/models/test_analysis_subwindow_beat_grid.py::test_existing_analysis_tables_keep_exactly_their_columns`;
- `tests/integration/test_migrations/test_098_subwindow_mood_and_beat_grid.py`, which reads a
  pre-existing `analysis_window` row back unchanged after upgrade and after downgrade;
- the baseline suite's autogenerate-drift and constraint-definition tests;
- `tests/discovery/services/test_moved_file_state.py::test_invalidate_content_state_drops_exactly_the_state_derived_from_the_old_bytes`;
- `tests/discovery/services/test_stale_rows.py::test_a_completed_analysis_on_the_duplicate_replaces_a_failed_one_on_the_kept_row`.

## 7. The general form

The bead asked for a sub-window length that each graph's patches "divide into cleanly". That framing
assumed patches tile the audio. Overlapping windows never tile anything, so the useful question for
any pooling of overlapping frames is an attribution rule that counts each frame exactly once. With
such a rule, the coarse value is exactly recoverable from its parts, and that recovery is the test.
The rule is written down here and in `iter_subwindows`' docstring, so it applies to any later pooling
of per-patch output (for example, per-window embeddings).

## 8. What the pooling and stitching beads settled

Written by phaze-x3gak and phaze-0296l (batch `analysis-outputs`), after running the real extractor and the real graphs.

**Beat seams: pre-roll.** `RhythmExtractor2013` never ticks a beat in its buffer's first samples, so a fine
window decoded from exactly its own start loses the beat that sits on the seam, about one beat per seam.
Operator decision, 2026-11-11, recorded on phaze-0296l. Question as put: "Beat grid (phaze-0296l), a
real-essentia finding: RhythmExtractor2013 never places a beat in a window's first samples. So when the
per-window beat ticks are stitched into one per-file grid, each window seam loses about 1 beat. The bead's
acceptance asks for no missing beat at a seam. How should seams be handled?" Answer as given: "Fix at
source: pre-roll (Recommended)".

The fine pass therefore decodes each window `BEAT_PREROLL_SEC` = 4 s early (never before 0) and runs the
tracker on that longer buffer for TICKS only. `bpm` and `musical_key` still come from the window's own samples,
so they are unchanged (the recorded equivalence tests pass untouched). The length is from measurement, on a
120 BPM click track with windows starting at 30 s and 60 s: with 0, 1 or 2 s of pre-roll the seam beat is absent
from at least one window; with 3, 4, 6 and 8 s it is present in both. 4 s is one second over the smallest length
that worked. A window keeps ticks from `SEAM_TOLERANCE_SEC` = 0.1 s before its start, because the seam beat is
ticked a quantum (11.61 ms) early; a beat seen from both sides is dropped by the half-interval duplicate rule.
Cost: one extra tracker call on a 34 s buffer for every window but the first, and a 4 s overlap of decode per
window, so the chunk's PCM bound is unchanged for practical purposes. `gaps` stays for real gaps: silence and
undetectable sections.

**The price of the pre-roll, measured (phaze-7hcgn.4).** On the real fine pass over a 600 s synthetic file
(three signals: steady, ramping and wobbling tempo; essentia 2.1-beta6-dev, macOS arm64), CPU time was 14.40 /
14.43 / 14.45 s with the second tracker call and 9.65 / 9.62 / 9.64 s with one call on the pre-rolled buffer: the
tick call costs about 50% of the fine tier. One call cannot keep bpm byte-identical, because bpm measured on the
pre-rolled buffer is the tempo of `[start - 4, end)`, not of the window: with one call the equivalence goldens failed
3 of 28 (a trailing window 150.1 → 150.0, a file-level bpm 128.0 → 128.1). The dispatcher's decision, not the
operator's (relayed 2026-11-13 on phaze-7hcgn.4): keep the two calls. Its reason: the phaze-fzubr profile put
rhythm, key, assembly and the database write together at under 2% of total analysis CPU and the mood graphs at
84.5%, so the one-call saving is under about 1% of the whole, too little to change what stored bpm means. The tick
call runs only when the run asks for grids (`_analyze_fine_windows(beats=...)`, from `include_grids`); without them
the fine pass decodes no pre-roll and calls the tracker once per window.

**Tempo changes between windows (phaze-7hcgn.4).** The stitcher judges a window's beats against that window's own
median interval, and at a seam against both tempos. Judging them against the beats already accepted dropped every
other beat after a tempo jump of more than 2x (a real change, or a tracker octave error) and recorded each step of a
slower window as a gap. On real ticks, a 70 → 150 BPM jump lost 15 of 185 beats and a 150 → 70 BPM drop recorded
9 gaps that do not exist. Tests: `test_a_tempo_jump_of_more_than_2x_at_a_seam_does_not_thin_the_grid`,
`test_a_tempo_drop_of_more_than_2x_at_a_seam_records_no_fake_gaps`. Tests: `test_real_ticks_lose_and_double_nothing_at_each_seam`, and
`test_without_the_pre_roll_the_same_seams_lose_a_beat` as its control.

**`MIN_BEAT_CONFIDENCE` = 1.0 (the implementer's decision, open to review).** A window whose tracker
confidence is below 1.0 keeps its `bpm` but contributes no beats, and so leaves a recorded gap. 1.0 is
essentia's own boundary for "low" confidence on the extractor's [0, 5.32] scale. The measurement behind it: on a
window holding only a few leaked edge samples (a click track with its middle 30 s zeroed), the extractor returned
39 confident-looking beats at tempo 80.3 BPM with confidence 0.19, while the clean windows of the same file scored
3.94. What it drops: beats from sparse or ambient sections where the tracker is unsure. Those sections show as
gaps rather than as a plausible-looking grid that is not there. What it does not touch: stored `bpm`. The threshold
rests on this one measured pair; the first archive-wide re-analysis is the place to check it against real sets.

**Silent coarse windows have no sub-window rows.** A coarse window with no analyzable signal runs no mood graph,
so there is no patch matrix to pool: it contributes no `analysis_subwindow` rows at all, rather than rows of NULL
mood. A NULL row (section 2) means "a slice with no patch centre inside a window that was analyzed".

**The patch matrix never crosses a process boundary.** `_predict_single` returns its mean as a `PatchMean`
carrying the matrix, and `_run_model_sets_over_windows` pools it into plain floats in the same process, inside the
sweep, before anything is returned. Only plain lists and floats reach the JSON line the child writes. The new output
is opt-in (`analyze_file(include_grids=True)`); the child must request it.

## 9. Rendering: the sub-window river and the beat ruler (phaze-jvo7i)

Written by the implementing seat of phaze-jvo7i; open to review.

**The river uses sub-window steps when they exist, and the coarse river otherwise.** When at least one
`analysis_subwindow` row yields usable geometry, the mood river draws one flat band step per slice, from
the slice's own start to its own end, so it changes where the mood was measured to change. Otherwise the
coarse 180 s river is drawn exactly as before, and its aria-label is unchanged. A NULL slice or a time
hole ends a run, as a coverage gap does: the band is never bridged across it. The stacking follows
`MOOD_NAMES` and the hues come from `MOOD_HUES`, so the 7 bands sum to the lane height at every slice.
A coarse window with no sub-window rows (a silent window, §8) is a gap in the sub-window river, which is
the honest reading.

**The beat ruler is one tick per beat, thinned to the canvas.** Ticks are kept at least
`BEAT_TICK_SPACING_PX` = 4 canvas px apart, earliest first, so a canvas `W` px wide carries at most
`floor(W / 4) + 1` ticks whatever the beat count (`beat_tick_budget`). Recorded `gaps` are drawn as hatched
spans, and no tick is drawn inside one. A file with no beat grid row gets no ruler at all, not an empty one.
The ruler adds no JavaScript: its readout is its own `aria-label`.

**Bounded work per render (phaze-7hcgn.4).** The next tick is found by binary search: the first beat at least
one spacing after the last drawn one. So a render reads O(ticks × log beats) values instead of every beat
(about 92,700 on the longest file). This relies on the grid being strictly ascending and finite, which
`BeatGridPayload` enforces at write. Recorded gaps whose start falls in the same 4 px column are drawn as one
span, so at most `floor(W / 4) + 1` spans are drawn, not up to 50,000. Every recorded gap is in exactly one span,
and the summary counts all of them. As first shipped, gaps were chained into one span whenever each began within
one spacing of the last, and that chain could run a "no beats" span across minutes of real beats; §10 replaces it
with a rule that never claims silence over a beat. One helper,
`load_timeline_grids`, loads the sub-window rows and the beat grid for both the record page and the proposal row.

**Tests (one per acceptance point):** `tests/shared/services/test_analysis_subwindow_river_and_beat_ruler.py`.
The river geometry is asserted on the polygon corners, and the rendered-markup tests render the shipped
partial. The existing timeline lane, JS-static and browser suites still pass unchanged.

## 10. A grid failure never fails an analysis (phaze-7hcgn.5)

Written by the implementing seat of phaze-7hcgn.5, after the second review of PR #802; open to review.

**The rule.** Sub-window mood and the beat grid are secondary display data. A failure on their path degrades to
"no grids for this window or file": the grid is left out, the drop is logged, and it is counted on
`phaze.analysis.grids.dropped{grid_stage}`. The windows, bpm, key and mood are exactly as complete as without
grids. Each site keeps its own narrow `try` around the grid work alone, not a blanket `except` around the PUT, and
`services/grid_drops.record_grid_drop` is the one shared report:

| `grid_stage` | what fails | what is dropped | what is kept |
| --- | --- | --- | --- |
| `fine_ticks` | the tick-only tracker call on a pre-rolled buffer | that window's beats (a recorded gap in the grid) | its bpm and key |
| `coarse_pooling` | pooling one model's patches, or combining a window's pools | all of that window's sub-window mood, never a partial set of variants | its mood, style and danceability |
| `assembly` | building `subwindows` or `beat_grid` from the windows | that grid for the file | the other grid, and the whole result |
| `payload` | validating a grid on the agent, through `AnalysisWritePayload`'s own fields | a malformed sub-window row's whole coarse window; the sub-windows past the wire cap; a malformed beat grid | everything else in the PUT |
| `server_capability` | the control plane does not advertise `analysis_grids` | both grids from the PUT | the PUT |

`analyze_file` also writes one WARNING per run with the count by stage. A dropped key is absent, not `null`: an
absent key means "this run produced none" to the writer, which deletes the stored rows with the windows (§5).

**The counter, not only a log line.** The dispatcher's decision, not the operator's. Question as put (to the
dispatcher, 2026-11-14): option 1, a counter `phaze.analysis.grids.dropped{grid_stage}` costing 5 series per
instance and a re-pricing of the cited series ceilings, or option 2, a count in the WARNING line and on the file
span with the metric left as a follow-up. Answer as given (dispatcher, verbatim excerpt): "option 1, the counter
phaze.analysis.grids.dropped{grid_stage}, with your 5 stages (my decision as dispatcher; record it as such).
Reason: degrading to "no grids" is only safe if it is visible, and a log line or a span attribute is not something
anyone watches; +5 series is cheap." Durable record: bead phaze-7hcgn.5. The ceilings moved with it: per instance
8,612 → 8,617, analysis block 2,290 → 2,295, ceiling 24,642 → 24,682
(`docs/telemetry/metric-catalogue.md` §3, `docs/telemetry/exporter.md`,
`docs/design/0017-telemetry-export-topology.md` §8b).

**A newer agent against an older server: a capability, not a retry.** A control plane that predates the grids
forbids unknown fields, so a grid is a 422 there, and `FAILURE_IS_TERMINAL[ANALYZE]` makes that a discarded
analysis on any rollout that upgrades an agent before its server. GET whoami now lists `capabilities`
(`["analysis_grids"]`); its response model is loose, so an older agent ignores the key, and a body without it reads
as no capabilities. `PhazeAgentClient.put_analysis` asks whoami before every PUT that carries a grid and leaves the
grids out unless the capability is listed, or when whoami cannot be read. It asks fresh on every such PUT, so a
server upgrade or rollback is followed, and it costs one GET per completed analysis. A retry-without-grids on 422
was rejected: a 422 does not say which field was refused, and retrying on it would also hide a real validation
defect in the core fields. This design was approved by the dispatcher (2026-11-14).

**The beat ruler never claims silence over a beat.** A span is drawn as the hatched "no beats" claim only when no
beat lies strictly inside it. A gap's own endpoints are the beats that bound it, so two recorded gaps can never
share one silence span unless they overlap. A span that takes in beats between its gaps is a `cluster`: drawn as a
faint dotted underline, labelled "N recorded gaps from A to B, with K beats among them". The DOM bound is now the
column rule in §9, and the beats inside a cluster are still ticked.

**A track's mood counts its whole measured time.** With partial sub-window coverage, a segment's mood is the
mean over its slices that carry a mood *and* over its coarse windows that no such slice covers. Coverage is
decided by geometry (a mooded slice whose midpoint lies in the coarse window), not by `window_index`. Each
uncovered window stands in for the slices it would have produced: its scores weighted by its slice count. With
full coverage nothing changes, and a segment with no mooded slice keeps the coarse answer, as before.

**Tests.** `tests/analyze/services/pipeline/test_grid_failure_isolation.py` (A, B, C, assembly, and the
acceptance test that each of a validation error, a tick-call exception and a pooling exception leaves the core
analysis complete, end to end through `analyze_file` and both payload builders);
`tests/agents/services/test_agent_client.py` and `tests/agents/routers/test_agent_identity.py` (D, old- and
new-server paths); `tests/shared/services/test_analysis_subwindow_river_and_beat_ruler.py` (E);
`tests/shared/services/test_track_segments.py` (F);
`tests/analyze/services/pipeline/test_subwindow_and_beat_grid_storage.py` (G: a non-finite `mood_scores` value
is refused on the wire, as every other wire float already was).

**The general form (rule 5).** Secondary output gets its own failure scope, and that scope sits inside the
primary output's success, never around it. A `try` shared by primary and secondary work lets the secondary
failure take down the primary. This is now written down here; every call site reports through `services/grid_drops.py`, whose docstring
points at this section.

## 11. An embedding failure never fails an analysis either (phaze-l5mzm)

Written by the implementing seat of phaze-l5mzm; open to review.

The per-window Discogs-EffNet embedding (`analyze_file(include_embeddings=True)`, off by default) is secondary output
too, and §10's rule applies to it unchanged. One difference makes it more than a reporting change: with the option on,
the genre sweep runs a different classifier (`FusedEffnetDiscogs`, which reads the logits and the embedding from one
TensorFlow session). So a failure on the embedding path could otherwise cost a window its genre, or the window itself.
Each stage reports through `analysis_embeddings.record_embedding_drop` and is counted on
`phaze.analysis.embeddings.dropped{embedding_stage}`:

| `embedding_stage` | what fails | what is dropped | what is kept |
| --- | --- | --- | --- |
| `inference` | the fused genre call on a window | that window's embedding | the window and its genre: it is re-swept on the plain genre classifier once the fused graph is released, so one graph is resident at a time, and only a failure there kills it |
| `encode` | a pooled vector float16 cannot hold | that window's embedding | everything else, with no second genre pass |
| `assembly` | building the file's `embeddings` key | the key for the file | the whole rest of the result |

A window with no patch (2.0 s or less) has no embedding, and that is an absence, not a drop. With the option off, the
genre sweep is the plain wrapper it always was and the result has no `embeddings` key.

**The counter and the isolation design are the dispatcher's decision, not the operator's.** Question as put (to the
dispatcher, 2026-11-13, excerpt): "'Counted' the way 7hcgn.5 counts means a catalogued metric. That forces an edit to
docs/telemetry/metric-catalogue.md and SERIES_CEILING ... My recommendation: a separate counter
phaze.analysis.embeddings.dropped with label embedding_stage = {inference, encode, assembly} ... OK to add the metric +
doc row + ceiling bump?" Answer as given (dispatcher, verbatim excerpt): "approved, all of it (my decision as
dispatcher; record it as such): a separate counter phaze.analysis.embeddings.dropped{embedding_stage=inference|encode|assembly},
plus the catalogue row and the SERIES_CEILING bump ... The isolation design is approved too: on a fused-call failure,
re-sweep that window with the plain wrapper after releasing the fused graph (one graph at a time, genre unchanged);
drop per window on an encode failure; drop the file key on an assembly failure; and with the option off, the exact
current path." Durable record: bead phaze-l5mzm. The ceilings moved with it: per instance 8,617 → 8,620, analysis
block 2,295 → 2,298, ceiling 24,682 → 24,706 (`docs/telemetry/metric-catalogue.md` §3, `docs/telemetry/exporter.md`,
`docs/design/0017-telemetry-export-topology.md` §8b).

**Tests.** `tests/analyze/services/pipeline/test_embedding_failure_isolation.py`: one test per stage, the re-sweep's
one-graph residency, the option-off path, and the end-to-end acceptance test through `analyze_file`. Each was seen to
fail against a hand-planted version of the defect it guards: no private kill list, the retry built while the fused
graph was still resident, an unguarded encode, an unguarded assembly, and the option off running the fused classifier.
`tests/analyze/services/pipeline/test_embeddings_real_models.py`: real graphs, per-window equality with a direct graph
call, and flat peak RSS across repeated multi-chunk runs with the option on.

**The general form** is §10's, with one addition. When turning secondary output on changes the code that produces
primary output, the fallback for a secondary failure must be the primary path as it was without the option, not a
skipped window.
