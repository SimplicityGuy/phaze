# S1 — Per-purpose requirements rubric, and what phaze already covers

- **Bead:** `phaze-ytgo.1` (epic `phaze-ytgo` — AudioMuse-AI: clean-room vs sidecar, per purpose)
- **Date:** 2026-07-25
- **Tree:** `b051b3b`
- **Status:** investigation only. No product code, no dependency change, no migration.

> **Clean-room statement.** No AudioMuse-AI source code was read while producing this document.
> The only AudioMuse facts used are the capability list already written into the `phaze-ytgo`
> epic description. This document therefore carries nothing that would contaminate the sealed
> spikes (`phaze-ytgo.2`, `.3`, `.6`) that read it.

______________________________________________________________________

## Question

The epic renders its verdict **per purpose**, not globally:

| id | Purpose |
| -- | ------- |
| **P1** | Better dedup + rename proposals — catch near-duplicates fingerprinting misses; give the naming LLM sonic context |
| **P2** | Discovery / playlists — similar-track browsing, clustering, playlist generation |
| **P3** | Set/tracklist intelligence — window-level similarity inside long concert sets |
| **P4** | Archive exploration / QA — music map + clustering to eyeball the archive, spot mis-tagged files |

Five sibling spikes each measure a different capability and then have to score it against
those four purposes. Without a shared bar, each invents its own and `phaze-ytgo.7` (D1) has
nothing mechanical to cross-tabulate.

So, three questions:

1. **What does each purpose actually require** — granularity, interactivity, accuracy bar,
   surface, scale — measured against phaze's real code and schema rather than intuition?
2. **How far do phaze's existing features already get each purpose?** phaze persists
   `bpm` / `musical_key` / `mood` / `style` / `danceability` per file *and per window*. If a
   purpose is already largely servable, that cell is "not worth it" and the molecule should
   stop paying for it.
3. **Where is the boundary against `phaze-vprd` and the fingerprinting services?** Which parts
   of P1 and P3 are *identity* problems already owned elsewhere, and which genuinely need
   sonic *resemblance*?

______________________________________________________________________

## Method

Codebase archaeology plus requirements reasoning. Concretely:

1. **Read the analysis schema and pipeline** — `src/phaze/models/analysis.py`,
   `src/phaze/services/analysis.py`, `src/phaze/config.py`, and every consumer of
   `AnalysisWindow` found by grep.
2. **Executed the shipped windowing code directly** to derive real per-file window counts.
   `_iter_windows` and `_stride_to_cap` are pure functions; calling them with
   `AgentSettings()` defaults produces exactly the row counts `analyze_file` emits, with no
   hand-rolled arithmetic in between. Script and output in [Evidence E2](#e2--how-many-window-rows-a-file-actually-produces-executed-not-estimated).
3. **Verified the model registry against the real model metadata** — the class lists in
   `<scratch>/models-full/*.json`, to establish the true dimensionality of the
   `features` JSONB rather than assuming it.
4. **Migrated an isolated schema and ran the scale queries against it** — `just test-db-for
   rubric` then `alembic upgrade head` to revision `045`, so the SQL quoted below is validated
   against the real table shapes.
5. **Audited the schema for vector types and available extensions** on the pinned
   `postgres:18-alpine` image.
6. **Read `phaze-vprd`'s bead** (`bh work issue phaze-vprd`) and the two fingerprint sidecars
   to place the identity/resemblance boundary.

### What could NOT be measured, and why

**No populated phaze database was reachable during this spike.** The only Postgres running was
the ephemeral test harness on `localhost:5433`, whose databases are schema-only:

```console
$ docker exec phaze-test-db psql -U phaze -d phaze_rubric_test -c \
    "SELECT count(*) AS window_rows, count(DISTINCT file_id) AS files FROM analysis_window;"
 window_rows | files
-------------+-------
           0 |     0
```

Consequently, **every scale figure below is derived from the shipped windowing code (E2),
not from a row count over a real archive.** The archive file count of **200,000** is the
planner's figure from the `phaze-ytgo` epic description ("eyeball a 200K-file archive"), not a
measured `count(*)` over `files`. The SQL that would produce the true figures from a populated
archive is given in [E2](#the-query-to-run-against-a-populated-archive) and was executed against
the real migrated schema, so it is known to be valid — it simply returned zeroes here.

This distinction matters more than it first appears, and in phaze's favour: the per-file window
count is **bounded by code constants, not by data**, so E2's numbers are exact upper bounds that
a live archive cannot exceed. What a live count would add is the *distribution* — how many files
actually saturate the caps — which is what turns an upper bound into an expected value.

______________________________________________________________________

## Evidence

### E1 — What phaze persists today

Two tables, both in `src/phaze/models/analysis.py`.

**`analysis` — 1:1 with files** (`models/analysis.py:13-57`):

| column | line | note |
| ------ | ---- | ---- |
| `bpm` | `models/analysis.py:20` | median of fine-window BPMs (`services/analysis.py:337-344`) |
| `musical_key` | `models/analysis.py:21` | duration-weighted modal key (`services/analysis.py:355-361`) |
| `mood` | `models/analysis.py:22` | time-weighted dominant label (`services/analysis.py:364-371`) |
| `style` | `models/analysis.py:23` | time-weighted dominant label (same reduction) |
| `fingerprint` | `models/analysis.py:24` | `Text`, unused by the fingerprint sidecars |
| `features` | `models/analysis.py:25` | JSONB — the *longest* coarse window's features (`services/analysis.py:571-581`) |
| `fine_windows_analyzed/total`, `coarse_windows_analyzed/total`, `sampled` | `models/analysis.py:29-33` | the Phase 43 coverage contract |

**Note a real gap:** there is **no `danceability` column on `analysis`**. It exists only per
window (`models/analysis.py:93`) and inside the `features` JSONB. A per-file danceability scalar
is not directly queryable today.

**`analysis_window` — 1:many with files** (`models/analysis.py:60-94`):

| column | line | tier |
| ------ | ---- | ---- |
| `tier`, `window_index`, `start_sec`, `end_sec` | `:83-86` | both |
| `bpm`, `musical_key` | `:88-89` | fine |
| `mood`, `style`, `danceability`, `features` | `:91-94` | coarse |

`ON DELETE CASCADE` on `file_id` (`:79`) — deleting a file removes its windows.

### E2 — How many window rows a file actually produces (executed, not estimated)

The windowing is **two-tier with hard per-file caps** — `analysis_fine_window_sec=30`,
`analysis_coarse_window_sec=180`, `analysis_fine_min_sec=15`, `analysis_fine_cap=60`,
`analysis_coarse_cap=30` (`config.py:941-955`, `config.py:967-978`). Over the cap, windows are
**strided evenly across the whole file** rather than truncated (`services/analysis.py:434-458`),
which bounds per-file cost to O(1) regardless of duration — the deliberate Phase 43 fix for the
4-hour-timeout incident (`services/analysis.py:394-398`).

Running the shipped `_iter_windows` + `_stride_to_cap` directly with `AgentSettings()` defaults:

```console
$ PHAZE_AGENT_API_URL=… PHAZE_AGENT_TOKEN=… PHAZE_AGENT_SCAN_ROOTS=… uv run python window_counts.py
defaults: fine_window=30s coarse_window=180s fine_min=15s fine_cap=60 coarse_cap=30

shape                      dur_s  fine_nat  fine_rows  coarse_nat  coarse_rows   rows  fine_cov%  coarse_cov%
-------------------------------------------------------------------------------------------------------------
3 min track                  180         6          6           1            1      7      100.0        100.0
7 min track                  420        14         14           3            3     17      100.0        100.0
12 min extended mix          720        24         24           4            4     28      100.0        100.0
30 min radio edit set       1800        60         60          10           10     70      100.0        100.0
60 min DJ set               3600       120         60          20           20     80       50.0        100.0
90 min set                  5400       180         60          30           30     90       33.3        100.0
3 h festival set           10800       360         60          60           30     90       16.7         50.0
6 h day recording          21600       720         60         120           30     90        8.3         25.0

saturation points (where the cap first bites):
  fine: cap 60 first binds at duration > 1800s (30 min)
  coarse: cap 30 first binds at duration > 5400s (90 min)

archive-scale row totals (worst case = every file saturates both caps):
     1000 files -> analysis rows      1,000   analysis_window rows <=       90,000
    10000 files -> analysis rows     10,000   analysis_window rows <=      900,000
    50000 files -> analysis rows     50,000   analysis_window rows <=    4,500,000
   200000 files -> analysis rows    200,000   analysis_window rows <=   18,000,000
```

The script is a throwaway that imports `phaze.config.AgentSettings` and
`phaze.services.analysis.{_iter_windows,_stride_to_cap}`; `fine_cov%` / `coarse_cov%` are the
summed duration of the *kept* windows over the file duration.

**Three findings, in ascending order of consequence:**

1. **`analysis_window` is bounded at 90 rows per file** (60 fine + 30 coarse) under default
   config. At 200,000 files that is a hard ceiling of **18 million window rows** — and the true
   number is well below it, since nothing under 30 minutes saturates the fine cap. A per-window
   *scalar* feature set is cheap at archive scale. This is a favourable finding.

2. **There is one uncapped path.** "Deepen analysis" re-enqueues a single file with
   `fine_cap=0` / `coarse_cap=0` (`routers/pipeline.py:2216-2218`), the sentinel that disables
   striding. The wire schema caps that at `max_length=50000` windows
   (`schemas/agent_analysis.py:128`). It is operator-initiated, per file, and not an
   archive-scale path.

3. **Timeline coverage collapses on exactly the files P3 cares about.** A 3-hour festival set
   retains **16.7% fine coverage and 50% coarse coverage**; a 6-hour recording, **8.3% / 25%**.
   The existing window rows on a long set are a *strided sample*, not a timeline. This is the
   single most consequential number in this document — see [E3](#e3--why-the-existing-window-rows-cannot-serve-p3).

#### The query to run against a populated archive

Both statements below were executed against the freshly migrated schema (revision `045`) and are
valid; they returned zeroes because no archive data was reachable. Run them against a populated
database to replace E2's upper bounds with an actual distribution:

```sql
-- Actual window-row counts and per-file average.
SELECT
  count(*)                                   AS window_rows,
  count(*) FILTER (WHERE tier = 'fine')      AS fine_rows,
  count(*) FILTER (WHERE tier = 'coarse')    AS coarse_rows,
  count(DISTINCT file_id)                    AS files_with_windows,
  round(count(*)::numeric / NULLIF(count(DISTINCT file_id), 0), 1) AS rows_per_file
FROM analysis_window;

-- How much of the archive is strided (i.e. how much timeline is NOT covered).
SELECT
  count(*)                            AS analyzed_files,
  count(*) FILTER (WHERE sampled)     AS sampled_files,
  sum(fine_windows_total)             AS fine_natural_total,
  sum(fine_windows_analyzed)          AS fine_stored_total,
  sum(coarse_windows_total)           AS coarse_natural_total,
  sum(coarse_windows_analyzed)        AS coarse_stored_total
FROM analysis
WHERE analysis_completed_at IS NOT NULL;
```

The second query's `fine_stored_total / fine_natural_total` ratio is the archive-wide version of
E2's `fine_cov%` column, and is the number D1 should quote for P3 feasibility.

### E3 — Why the existing window rows cannot serve P3

Two independent structural reasons, both from the shipped code:

**(a) The rows do not cover the timeline.** Per E2, a 3-hour set stores 60 fine windows out of
360 natural ones — 30 seconds analysed, then 150 seconds skipped, repeating. Attaching an
embedding to each existing `analysis_window` row would produce a vector series with 83% of a
festival set missing. Track boundaries inside a DJ set land in the gaps roughly five times out of
six.

**(b) The coarse tier is longer than the tracks it would have to separate.**
`analysis_coarse_window_sec` is **180 seconds** (`config.py:946-950`) — longer than most tracks
in a DJ set. A coarse window therefore straddles two to four tracks, and its `mood` / `style`
labels are a blend of them, not a property of any one. `aggregate_dominant`
(`services/analysis.py:364-371`) then time-weights those blends into a single file-level label.
For a 3-hour set that label is close to meaningless.

The consequence for the molecule: **P3 cannot ride the existing analysis rows.** Any P3 capability
needs its own contiguous windowing, whose cost is O(duration) — directly contradicting the Phase
43 decision that per-file analysis cost is O(1). That contradiction is not a reason to refuse P3;
it is a cost that a P3 verdict must price explicitly.

**Illustrative P3 sizing** (derived, not measured — the set/track mix of the archive is unknown):
at a 10-second hop, a 3-hour set yields 1,080 contiguous windows against the 90 rows it stores
today. If 5% of a 200,000-file archive is multi-hour sets, that is ~10,000 sets × ~1,080 ≈
**1.1 × 10⁷ window vectors from sets alone**, versus 190,000 track-level vectors for everything
else — a ~50× multiplier concentrated entirely in P3. Replace the 5% with a real figure from
`FileMetadata.duration` before D1 quotes this.

### E4 — What the `features` JSONB actually contains

`_run_model_sets` (`services/analysis.py:461-484`) runs **11 binary model sets × 3 variants**
(`MODEL_SETS`, `services/analysis.py:85-105`) plus the `discogs-effnet` genre model
(`GENRE_MODEL`, `services/analysis.py:107-112`), and stores, per coarse window:

- `features[<set>][<variant>] = [{label, prediction}, …]` — one entry per class;
- `features["genre"]["predictions"]` — the **top 10** genre labels by confidence
  (`services/analysis.py:477-483`).

Verified against the real model metadata rather than assumed:

```console
$ cd <scratch>/models-full
mood_acoustic-musicnn-msd-2            2 ['acoustic', 'non_acoustic']
mood_relaxed-musicnn-msd-2             2 ['non_relaxed', 'relaxed']
danceability-musicnn-msd-2             2 ['danceable', 'not_danceable']
gender-musicnn-msd-2                   2 ['female', 'male']
tonal_atonal-musicnn-msd-2             2 ['atonal', 'tonal']
voice_instrumental-musicnn-msd-1       2 ['instrumental', 'voice']
discogs-effnet-bs64-1                400 ['Blues---Boogie Woogie', 'Blues---Chicago Blues', …]
```

All 11 sets are binary. So each coarse window's `features` JSONB carries **11 × 3 × 2 = 66
floats** — reducible to **33 independent floats**, or to an **11-dimensional positive-class score
vector** using the reduction the code already performs (`_positive_class_prediction`,
`services/analysis.py:197-218`; `derive_mood`, `:221-247`) — plus a **10-of-400 genre
distribution**, plus `bpm` and `musical_key` from the fine tier.

**This is the load-bearing fact of the "current coverage" section.** phaze already persists an
interpretable, clusterable score vector — per file *and* per coarse window. The epic describes
AudioMuse as clustering over "either the raw 200-d embedding **or a human-readable score
vector**". phaze already has the second of those two. What it does not have is the 200-d
embedding and an ANN index.

### E5 — The existing surfaces

`STAGE_PARTIALS` (`routers/shell.py:77-143`) is a strict whitelist of **14 rail nodes**:

`summary` (`:82`), `files` (`:95`), `discover` (`:98`), `metadata` (`:102`), `fingerprint`
(`:103`), `analyze` (`:108`), `trackid` (`:113`), `tracklist` (`:118`), `propose` (`:122`),
`rename` (`:127`), `tagwrite` (`:131`), `move` (`:132`), `dedupe` (`:137`), `cue` (`:142`).

Every one is a **pipeline stage**. There is no browse, discovery, playlist, or map surface.

Relevant existing read surfaces:

| surface | where | what it already does |
| ------- | ----- | -------------------- |
| ⌘K command palette | `routers/search.py:22-89` → `search/partials/palette_results.html` | search-as-you-type across files/tracklists/discogs; already accepts `bpm_min`/`bpm_max` (`:34-35`) and `genre`, applied in `services/search_queries.py:96-109` |
| analysis timeline | `routers/proposals.py:352-396`, `_bpm_spark:187-207`, `_ribbons:219-235` → `proposals/partials/analysis_timeline.html` | renders the per-window BPM sparkline and key/mood/style ribbons — **the only window-level visual surface that exists** |
| full record | `routers/record.py:60-88` | re-renders the same window timeline per file |
| dedupe review | `/s/dedupe` → `dedupe_workspace.html`, `routers/duplicates.py:94-272` | keeper selection + resolve/undo, keyed on `sha256_hash` throughout |
| track-id | `/s/trackid` → `trackid_workspace.html` | per-file identity table: audfprint · Panako · Tracklist · Confidence |
| LLM rename context | `services/proposal.py:155-210` | ships `bpm`, `musical_key`, `mood`, `style` and the whole `features` JSONB to the LLM (`:175-183`) |

One house rule constrains any new surface: a workspace must **not** render an unbounded row set
inline — see the `phaze-1wvb` comment in `trackid_workspace.html`, which replaced an inline loop
over "the whole corpus" with a bounded, paged `hx-get` fragment. A 200,000-point scatter rendered
inline would violate it.

### E6 — What dedup actually does

`services/dedup.py` groups **only by `sha256_hash`** — byte-identical files:

```python
# services/dedup.py:83-92
select(FileRecord.sha256_hash)
  .where(~dedup_resolved_clause())
  .group_by(FileRecord.sha256_hash)
  .having(func.count(FileRecord.id) > 1)
```

The hash is the group *identity* all the way through the surface: `find_duplicate_group_by_hash`
(`:218`), `resolve_group(session, group_hash, canonical_id)` (`:322`),
`POST /duplicates/{group_hash}/resolve` (`routers/duplicates.py:122`), and the
`dedupe_group_hash_inputs` the bulk header posts back.

**There is no near-duplicate detection of any kind.** No perceptual hash, no fuzzy metadata
match, no fingerprint-backed grouping.

Resolution is a `DedupResolution` marker row, unique on `file_id`
(`models/dedup_resolution.py:41-53`) — existence means resolved, and `undo_resolve`
(`services/dedup.py:379-449`) deletes it. **Resolution hides a file; it does not delete it.** That
is what makes a false positive here recoverable, and it is why the P1 precision bar below is
0.95 rather than 0.99.

### E7 — What fingerprinting actually does, and its single consumer

Two HTTP sidecars behind `services/fingerprint.py`: `AudfprintAdapter` (`:184-218`) and
`PanakoAdapter` (`:221-255`), weighted 0.6 / 0.4. Both emit `QueryMatch(track_id, confidence,
timestamp)` (`:84-91`), where `timestamp` is the match offset **into the reference track**
(`services/panako/app.py:231-236`, `services/audfprint/app.py:196-197`).

`FingerprintOrchestrator.combined_query` (`:284-346`) aggregates matches **by `track_id` alone**
(`:300`, `:324`), which collapses repeated occurrences of the same track. `phaze-vprd` is
explicitly building an uncollapsed `segment_query()` path for that reason.

**`combined_query` has exactly one consumer in the entire codebase:**

```console
$ grep -rn "combined_query" src/phaze/ services/ | grep -v "^.*fingerprint.py"
src/phaze/tasks/scan.py:128:        matches = await orchestrator.combined_query(payload.original_path)
```

`tasks/scan.py:120-219` — `scan_live_set`, which turns matches into `tracklist_tracks` rows with
`artist=None, title=None, timestamp=match.timestamp` (`:177-185`).

So: **fingerprinting is wired into tracklists and nothing else. It is not wired into dedup at
all.** phaze owns a same-recording matcher and does not use it for the same-recording problem.
This is the most actionable finding in the document, and it is not about AudioMuse.

Per-file fingerprint budget: `SIDECAR_HTTP_TIMEOUT_SEC` defaults to **3900 s**, sized to exceed
the sidecars' own 3600 s `SUBPROCESS_TIMEOUT` "sized for multi-hour concert sets"
(`services/fingerprint.py:20-27`).

### E8 — The vector gap, confirmed against the pinned image

Schema audit on the migrated database (`postgres:18-alpine`, `docker-compose.yml:79`):

```console
$ docker exec phaze-test-db psql -U phaze -d phaze_rubric_test -tAc "
    SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
    FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped
      AND format_type(a.atttypid, a.atttypmod) LIKE '%vector%';"
files|search_vector|tsvector
metadata|search_vector|tsvector
tracklists|search_vector|tsvector

$ … "SELECT name, default_version, installed_version FROM pg_available_extensions
     WHERE name IN ('vector','cube','pg_trgm') ORDER BY name;"
cube|1.5|
pg_trgm|1.6|1.6
```

Confirms the epic's claim, and sharpens it in a way that matters for `phaze-ytgo.6`:
**`vector` is not merely uninstalled, it is not available** in the pinned image. `cube` is
available but uninstalled (and is dimension-limited by default). Adding pgvector is therefore a
**base-image change**, not a `CREATE EXTENSION`.

______________________________________________________________________

## Verdict

### The requirements table

Every cell is grounded in the Evidence above.

| | **P1** dedup + rename | **P2** discovery / playlists | **P3** set/tracklist intelligence | **P4** archive QA |
| --- | --- | --- | --- | --- |
| **Granularity** | **Track-level.** The resemblance share is "same work, different rendition" — a whole-file property. Partial-overlap is window-level but is *identity*, owned by `phaze-vprd` (see boundary). Nothing in the dedup surface is time-aware: groups are keyed on a whole-file `sha256_hash` (E6). | **Track-level.** "Tracks like this one" is a per-file relation. One vector per file. | **Window-level and CONTIGUOUS.** The deliverable is `tracklist_tracks` rows carrying a `timestamp` (`models/tracklist.py:78-85`) — a track-level vector cannot emit an offset. And the *existing* window rows do not qualify: 16.7% fine coverage on a 3 h set, coarse windows (180 s) longer than the tracks they must separate (E2, E3). **P3 needs its own windowing.** | **Track-level.** A mis-tag or an outlier is a per-file verdict; the map is one point per file. |
| **Interactivity** | **Batch.** Candidate generation offline. Review is a paged read over precomputed groups (`services/dedup.py:72-92` LIMIT/OFFSET); budget = today's paged `find_duplicate_groups` envelope, no regression. The rename half is already batch (LLM proposals). | **Interactive.** This is a browse surface. Budget: **p95 ≤ 200 ms for k=50 at N=200,000** to sit inside the ⌘K palette's search-as-you-type contract (`routers/search.py:22-89`) without a spinner; **≤ 1 s** for a "similar tracks" panel loaded as its own `hx-get` fragment, matching every other workspace fragment. | **Batch.** `scan_live_set` is already an operator-triggered SAQ agent task (`tasks/scan.py:120`, `routers/pipeline.py:2654`). Budget: **≤ 1 h per set**, the envelope already accepted for fingerprinting multi-hour sets (`services/fingerprint.py:20-27`, 3900 s / 3600 s). | **Split.** Projection offline; the surface interactive. Budget: bounded + paged like every other workspace — the `phaze-1wvb` rule in `trackid_workspace.html` forbids rendering an unbounded set inline, so a 200k-point inline scatter is out. |
| **Accuracy bar** | **Recall ≥ 0.90 at precision ≥ 0.95** on an operator-labelled seed set of ≥ 50 near-duplicate pairs. The existing `sha256` groups **cannot** serve as the denominator — identical bytes are a degenerate positive that any method finds. Precision outranks recall because a false positive writes a `DedupResolution` marker that hides a real file; 0.95 rather than 0.99 because `undo_resolve` makes it reversible (E6). | **Subjective → blind A/B.** 20 seed tracks; top-10 from each method presented unlabelled; operator marks each "belongs in a playlist with the seed / doesn't". Bar: **mean precision@10 ≥ 0.6 AND strictly better than the existing-features baseline** below. The baseline arm is mandatory. | **Objective, and the only purpose with real ground truth.** Scraped 1001Tracklists rows (`Tracklist.source='1001tracklists'`, `TracklistTrack.position`/`timestamp`, `models/tracklist.py:39,78-85`). Bar: **≥ 70% of scraped tracks located within ±30 s**, at **≤ 1 false segment per 10 true**. ±30 s is `analysis_fine_window_sec` (`config.py:941`) — the finest temporal resolution anything else in phaze commits to. | **Subjective → yield.** Sample 100 flagged outliers; bar: **≥ 30% are genuinely mis-tagged / mis-filed on inspection**, AND better yield than the existing-features baseline. |
| **Surface** | **Existing page, new group key.** `/s/dedupe` + `routers/duplicates.py:94-272` + `services/dedup.py` all key the group on `sha256_hash` (E6) — a near-duplicate group has no hash. The review *shape* (keeper radio, resolve, undo, `DedupResolution` marker) survives unchanged; the **group identity does not**. Rename half needs **no** new surface: extend the dict in `services/proposal.py:175-183`. | **NEW.** All 14 rail nodes are pipeline stages (`routers/shell.py:77-143`); none is a browse/playlist surface. Needs a new `STAGE_PARTIALS` key + workspace + router, following the existing scaffold. The ⌘K palette is the nearest thing and is a *filter*, not a similarity, surface. | **Existing.** `/s/tracklist` + `/s/trackid` (`routers/shell.py:113,118`), `routers/agent_tracklists.py` `create_tracklist`, `tracklist_tracks` rows. Needs a new `Tracklist.source` value (`models/tracklist.py:39`) alongside `1001tracklists` / `fingerprint`. No new page. | **NEW.** No map or QA rail node. But see current coverage — much of the *value* is reachable with SQL over existing columns behind the existing files/search surfaces, without a map at all. |
| **Scale** | 1 vector/file → **200,000 rows**. At 200-d float32 ≈ 800 B → **~160 MB** raw. All-pairs is 2 × 10¹⁰ comparisons — needs ANN (`phaze-ytgo.6`). | Same as P1: **200,000** vectors. AudioMuse's native shape. | **O(duration), uncapped.** Existing rows are ≤ 90/file (E2). Contiguous windowing at a 10 s hop: 1,080 windows for a 3 h set. Derived estimate at 5% multi-hour sets: **~1.1 × 10⁷ window vectors** — ~50× P1/P2 and concentrated entirely here. Contradicts the Phase 43 O(1)-per-file decision (`services/analysis.py:394-398`). | **200,000** points + a 2-D projection column. Negligible — the smallest storage ask of the four. |

### Current coverage — how far the existing scalar features already get you

phaze already persists, per file **and per coarse window** (E1, E4): `bpm`, `musical_key`,
`mood`, `style`, `danceability` (window-only), and a `features` JSONB reducible to an
**11-dimensional interpretable score vector** plus a **10-of-400 genre distribution**.

**Define the baseline once, here, so every sibling scores against the same thing:**

> **The existing-features baseline (EFB).** Similarity computed from columns that exist today:
> equality/compatibility on `style` and `mood`, `bpm` within ±2%, key compatibility on
> `musical_key`, and cosine over the 11-d positive-class score vector reduced from `features`
> via `_positive_class_prediction` (`services/analysis.py:197-218`). Zero new dependencies,
> zero new tables, zero new models.

| | How far EFB gets you | Assessment |
| --- | --- | --- |
| **P1** | **Not far.** Two encodes of the same track have near-identical scalars — and so do 10,000 unrelated house tracks at 128 BPM in A minor. EFB has no discriminative power at the identity end; it is a **blocking key** that shrinks the candidate set, never the decision. | Genuine gap — but see the boundary below: much of P1's gap is filled by fingerprinting phaze already owns and has not wired up (E7). |
| **P2** | **A real, usable baseline.** "More like this" as style + BPM window + key compatibility is a handful of SQL predicates over columns already exposed to the search layer (`search_queries.py:96-109` already filters `genre`, `bpm_min`, `bpm_max`). Quality unmeasured, cost ~zero. | Any embedding proposal must **beat** this, not merely work. |
| **P3** | **Almost nothing, and actively misleading.** Strided coverage means the rows are not a timeline (E2); 180 s coarse windows straddle 2-4 DJ-set tracks so their labels are blends (E3); `aggregate_dominant` then flattens the blends to one file label. | Existing features are the wrong shape for P3 — not merely insufficient. |
| **P4** | **Most of the way.** Outlier detection over (`bpm`, `musical_key`, `mood`, `style`, `danceability`, genre-top-10) is clustering on data phaze already has. The classic archive-QA finds — a file tagged *Techno* whose classifier says *Blues*, a 40 BPM "House" track, a file whose per-window style ribbons are incoherent — are all reachable with SQL or scikit-learn over existing columns. Even the 2-D map is a projection of a vector phaze **already stores**. | **P4 is the strongest "not worth it" candidate in the molecule, and for the right reason.** Not "do nothing" — "do the clustering on what we already store." |

**On P4, do not soften this.** The finding is that P4's value is largely reachable today, and the
useful question for D1 is not "clean-room or sidecar" but "what does an embedding add over EFB
for P4, and is it worth an ANN index and a base-image change?" A sibling that reports P4 `SERVES`
without an EFB comparison has not answered the question that matters.

### Boundary: identity vs resemblance

Two different questions. Conflating them is the main way this molecule can waste effort.

- **IDENTITY** — *"is this the same recording/performance?"* Owned by `services/audfprint/app.py`,
  `services/panako/app.py`, `services/fingerprint.py`. Time-localized identity **across** sets is
  `phaze-vprd`, which is building `segment_matches` with `query_start` / `query_stop` /
  `match_start` / `match_stop` / `time_factor` / `freq_factor` and an uncollapsed
  `segment_query()` (its design §1-§3).
- **RESEMBLANCE** — *"does this sound like that?"* Nothing owns this. It is this molecule's actual
  scope.

| Purpose | Identity share — **already owned, out of scope** | Resemblance share — **this molecule** |
| --- | --- | --- |
| **P1** | Same recording, different encode / bitrate / container / trim. This is exactly what audfprint + Panako do, and `combined_query` is wired to **nothing but tracklists** (E7). | Live vs studio rendition of the same work; the same track from a differently-EQ'd or differently-mastered rip; covers and remixes. These degrade or defeat fingerprinting. |
| **P2** | — none — | All of it. |
| **P3** | **Most of it.** Locating a known track inside a set at an offset is precisely Panako, and precisely `phaze-vprd`. Do not re-solve the uncollapsed repeat-occurrence path either — `combined_query` collapsing by `track_id` (`services/fingerprint.py:300,324`) is a known limitation `phaze-vprd` already owns. | Narrow and specific: **(a)** segmenting a set into boundaries when the constituent tracks are **not** in the fingerprint DB (unreleased IDs, live edits, mashups); **(b)** corroborating a scraped 1001Tracklists ordering where fingerprinting returned nothing. |
| **P4** | — none — | All of it. |

**Two consequences the siblings must apply:**

1. A **P1 win that is really fingerprinting** is a false positive for this molecule. The single
   highest-value P1 action available today is wiring the existing engines into the dedup surface
   — and that is a different bead, not an AudioMuse verdict.
2. A **P3 win that is really cross-set identity** belongs to `phaze-vprd`. Score P3 only on the
   two resemblance sub-cases above.

### Structural blockers D1 must carry forward

| id | Blocker | Evidence | Owner |
| -- | ------- | -------- | ----- |
| **B1** | pgvector is **not available** in the pinned `postgres:18-alpine` image — `pg_available_extensions` lists `cube` and `pg_trgm`, no `vector`. Adding it is a base-image change, not `CREATE EXTENSION`. | E8 | `phaze-ytgo.6` |
| **B2** | Per-file analysis cost is capped at **O(1)** by deliberate decision (Phase 43). P3's contiguous windowing is **O(duration)** and contradicts it. | E2, E3, `services/analysis.py:394-398` | any P3 verdict |
| **B3** | A capability that produces **one averaged vector per track** is structurally incapable of P3 — no averaging of patch embeddings can emit a timestamp. Model quality is irrelevant to this. | P3 granularity row; epic description | `phaze-ytgo.4` (sidecar) — see below |
| **B4** | The dedup surface's group identity **is** `sha256_hash`, end to end. A near-duplicate group has no hash, so P1 needs a new group-key concept even though the review UI shape survives. | E6 | any P1 verdict |

**B3 is an immediate saving for `phaze-ytgo.4`.** The epic records that AudioMuse averages patch
embeddings into one 200-d vector per track. If the sidecar spike confirms the sidecar exposes
only per-track vectors, it can score P3 `BLOCKED` on structural grounds and **stop measuring P3
entirely** — no benchmark will change the answer.

______________________________________________________________________

## Recommendation

### The rubric — how every sibling spike scores its finding

Each of `phaze-ytgo.2` .. `.6` adds a **"Per-purpose impact"** subsection to its Verdict, using
exactly this block. Copy it verbatim and fill it in:

```markdown
### Per-purpose impact (S1 rubric, phaze-ytgo.1)

| Purpose | Verdict | Granularity delivered | vs EFB | Evidence |
| ------- | ------- | --------------------- | ------ | -------- |
| P1 dedup + rename        | SERVES / SERVES-WITH-CAVEAT / BLOCKED / REDUNDANT / UNMEASURED | track / window-contiguous / window-strided / n-a | better / same / worse / n-m | … |
| P2 discovery / playlists | …  | … | … | … |
| P3 set/tracklist         | …  | … | … | … |
| P4 archive QA            | …  | … | … | … |
```

**The five verdict tokens, and what each means:**

| token | meaning |
| ----- | ------- |
| `SERVES` | Meets this purpose's accuracy bar and granularity requirement, measured. |
| `SERVES-WITH-CAVEAT` | Meets the bar under a stated restriction (subset of the archive, extra hardware, degraded latency). Name the restriction. |
| `BLOCKED` | Structurally cannot serve this purpose. State which structural fact blocks it, not which measurement failed. |
| `REDUNDANT` | The existing-features baseline already reaches this purpose's bar; the capability adds no material delta. **This is the token that lets D1 reach "not worth it" for the right reason** — it means "use what we already store", never "do nothing". |
| `UNMEASURED` | Could not be measured (no hardware, model unavailable, licence blocked). **Mandatory over an estimate.** An honest `UNMEASURED` is usable by D1; a fabricated number is not. |

**Six scoring rules, all binding:**

1. **Score each purpose independently. Never average.** A capability that serves P2 brilliantly
   and cannot serve P3 is `SERVES` on P2 and `BLOCKED` on P3. There is no overall score.
2. **Always state the granularity delivered.** A capability delivering track-level vectors may
   **not** claim P3, regardless of measured quality (B3).
3. **Any P2 or P4 claim must report its delta against the EFB** defined above. No baseline arm
   means the cell is `UNMEASURED`, not `SERVES`.
4. **Any P1 claim must state which share it addresses** — identity or resemblance. An
   identity-share claim scores `REDUNDANT` against the fingerprint services and `phaze-vprd`.
5. **Any P3 claim must price B2** — say what happens to the Phase 43 O(1)-per-file cost cap, in
   rows and in CPU-seconds.
6. **Cite the measurement.** Sample size and file provenance for every measured number, per the
   epic's "measure on REAL archive files" rule.

### Actions this spike recommends

1. **`phaze-ytgo.4` (sidecar): resolve P3 structurally first.** If the sidecar exposes only
   per-track vectors, score P3 `BLOCKED` under B3 and skip P3 benchmarking entirely.
2. **`phaze-ytgo.6` (vector storage): lead with B1.** The question is not "pgvector vs
   alternatives" but "pgvector requires changing the pinned Postgres image" — that reframes the
   comparison against options that need no base-image change.
3. **Every sealed spike: run the EFB arm.** It costs almost nothing (existing columns, no new
   dependency) and it is the only thing that can distinguish a genuine capability gain from a
   capability phaze already has in a different costume.
4. **`phaze-ytgo.7` (D1): treat P4 as `REDUNDANT` unless a sibling shows a measured delta over
   EFB.** The evidence here is that P4's value is largely reachable today (E4, current coverage).
5. **File a separate, non-AudioMuse bead: wire the existing fingerprint engines into the dedup
   surface.** `combined_query` has exactly one consumer, `scan_live_set` (E7). phaze owns a
   same-recording matcher and does not use it for the same-recording problem. This is likely the
   largest available P1 improvement and it needs no embeddings, no ANN index, no new dependency
   and no licence analysis. It should not be bundled into this molecule's verdict, and it should
   not be blocked on it.

### Open, and deliberately left unmeasured

| # | Question | Why it is open | Who should close it |
| - | -------- | -------------- | ------------------- |
| O1 | The **real** distribution of `analysis_window` rows and the archive-wide strided fraction. | No populated database was reachable (see Method). E2's figures are exact upper bounds from the shipped code, not a measured distribution. | Anyone with archive DB access: run the two queries in E2. |
| O2 | What fraction of the archive is multi-hour sets. | Drives P3's ~50× vector multiplier, currently carried as an illustrative 5%. | Query `FileMetadata.duration`. |
| O3 | Measured EFB quality for P2 and P4. | This spike defines the baseline; measuring it needs archive data. | The first sibling to run a P2/P4 arm. |
| O4 | Whether a near-duplicate group key can reuse the `DedupResolution` marker unchanged. | Depends on the group-key design (B4), which depends on the P1 verdict. | The P1 implementation molecule, if P1 goes. |
