# ADR-0026: Storage for sub-window mood and the per-file beat grid

Date: 2026-11-10. Bead: phaze-f6eeb. Epic: phaze-7hcgn. Status: accepted.

Audio analysis computes two things at a finer resolution than it stores, then discards them. Every
mood graph returns a `[patches, classes]` matrix, and `_predict_single` in
`src/phaze/services/analysis.py` averages it into one vector per 180 s coarse window.
`RhythmExtractor2013` returns beat ticks for every 30 s fine window, and `_measure_fine_window`
drops them. This record fixes how both are stored: the sub-window length, the two storage shapes,
and what was measured to choose them. Migration `095` implements it. The rest of the epic (pooling,
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
`analysis_window`. Both tables start empty. Only a re-analysis fills them, and the archive-wide
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
- `tests/integration/test_migrations/test_095_subwindow_mood_and_beat_grid.py`, which reads a
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
