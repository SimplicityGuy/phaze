# phaze-bk9el.19 — post-molecule health ledger for iteration 2

- **Bead:** `phaze-bk9el.19` (epic `phaze-bk9el`, "Code quality iteration 2"). The only bead in the
  molecule permitted to claim the molecule improved anything.
- **Baseline measured against:** `phaze-bk9el.1`, commit
  `65e5af46e5812969445b1d6c4aaf09a8b1a4b5d7`, artifacts
  [`phaze-bk9el.1-health-baseline-2026-08-21.md`](./phaze-bk9el.1-health-baseline-2026-08-21.md) /
  [`.json`](./phaze-bk9el.1-health-baseline-2026-08-21.json).
- **This measurement's pinned commit:** `16d27f9aa7d5878d8b2ae1f0cb2bcb9e6096a9eb`
  (`wt/bead/epic/phaze-bk9el`, "chore(merge): bead phaze-bk9el.15" — the epic tip after all 23
  sibling beads merged, and the commit PR #513 proposes for `main`).
- **Machine-readable "after" side:**
  [`phaze-bk9el.19-health-after-2026-08-22.json`](./phaze-bk9el.19-health-after-2026-08-22.json),
  produced by the same `scripts/health_baseline.py` the baseline used, so the two files diff
  directly on every key.
- **Status:** measurement and reporting only. No file under `src/` is modified by this bead.
- **Scope of every figure below unless stated otherwise:** the 258 → 270 `src/phaze/**.py` files.
  `src/phaze/static/js/analysis_timeline.js` is under `src/` but is not Python and is excluded, as
  it was from the baseline's headline.

## 0. The headline, without editorialising

| Bucket | Before | After | Δ | Findings before | after | Δ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **REACHABLE** | 198.041 | 172.194 | **−25.847** | 623 | 575 | −48 |
| — genuinely reachable (less the 24.0 artifact, §2) | **174.041** | **148.194** | **−25.847** (−14.85%) | 611 | 563 | −48 |
| **HISTORY-DERIVED** | 273.170 | 264.497 | **−8.673** | 409 | 385 | −24 |
| — attributable to this epic's edits (§4) | | | **−0.465** | | | |
| performance | 0.000 | 0.000 | 0.000 | 91 | 90 | −1 |
| unclassified | 2.037 | 1.437 | −0.600 | 5 | 4 | −1 |
| **total deduction** | **473.248** | **438.128** | **−35.120** | 1128 | 1054 | −74 |

**The two buckets are reported separately and are not added together anywhere in this document.**
The reachable figure is the epic's result. The history-derived figure is mostly not, and §4
measures exactly how much of it is.

Four more repo-wide figures, same scope:

| Figure | Before | After |
| --- | ---: | ---: |
| `src/phaze/**.py` files | 258 | 270 |
| mean `score` over those files | 8.165 | 8.377 |
| files under the 6.0 score floor (all of `src/`, 382 → 394 files) | 42 | 31 |
| worst-scoring file in `src/` | `services/analysis.py` 2.55 | `services/analysis.py` **2.55** |

Repo-wide coverage, from this bead's own full-suite run (7531 passed, 2 skipped, 180 deselected,
19:00):

| Metric | Before | After |
| --- | ---: | ---: |
| Statements | 98.7789% (17554/17771) | **99.0132%** (17759/17936) |
| Branches | 95.2923% (3684/3866) | **95.8376%** (3707/3868) |
| Combined | 98.1559% (21238/21637) | **98.4498%** (21466/21804) |

### What got worse

Stated here rather than buried, per criterion 8.

| Thing | Before | After | Δ |
| --- | ---: | ---: | ---: |
| `prior_defect` deduction repo-wide | 140.896 | 142.952 | **+2.056** (168 → 170 findings) |
| `change_entropy` deduction repo-wide | 45.410 | 48.220 | **+2.810** (43 → 43 findings) |
| `error_handling` deduction repo-wide | 23.536 | 23.537 | **+0.001** (205 → 201 findings) |
| `routers/agent_metadata.py` score | 8.58 | **6.26** | −2.32 |
| `main.py` score | 7.38 | **6.00** | −1.38 |
| `routers/pipeline/dashboard_stats.py` score | 8.70 | 8.20 | −0.50; reachable 1.297 → **1.801**; `duplication_pct` 19.55 → **20.15** |
| `schemas/agent_scan_batches.py` score | 8.53 | 7.62 | −0.91 (its reachable deduction went 0.150 → 0.000) |
| `services/analysis_wire.py` `duplication_pct` | 0.00 | **19.44** | +19.44 |
| `services/analysis_derive.py` `duplication_pct` | n/a (new) | **27.78** | new `dry_violation` |
| `routers/shell/__init__.py` `duplication_pct` | n/a (new) | **16.83** | new `dry_violation` |
| `routers/pipeline_scans.py` + `routers/scan.py` history-derived (sum) | 3.500 | 3.801 | +0.301 |
| `services/tracklist_matcher.py` score — **not touched by this epic** | 7.28 | **6.01** | −1.27, pure index drift (§4) |

## 1. The measurement was recomputed before it was read (the mandatory first step)

`repowise update` **never runs the health fold** and **never re-ingests coverage**; both halves were
measured by `phaze-bk9el.1` and are recorded on this bead as a warning. A bulk read like this one is
exactly the shape that silently serves a pre-refactor tree. So the five-step
`scripts/repowise-coverage.sh` refresh ran first, at the pinned commit, and its freshness is
evidenced below rather than assumed.

```console
$ git checkout --detach 16d27f9a          # in the DURABLE checkout — see below
$ just repowise-coverage ledger
1/5 🔁 repowise update (reindex at 16d27f9aa7d5878d8b2ae1f0cb2bcb9e6096a9eb)
     8d1bf08c..16d27f9a · 106 changed · 72 modified, 33 added, 1 deleted
2/5 🧪 pytest --cov=phaze --cov-context=test
     7531 passed, 2 skipped, 180 deselected, 96 warnings in 1140.37s (0:19:00)
3/5 📥 repowise coverage add .coverage
     Built the test-to-code map: 34972 test->file record(s).
4/5 📥 coverage xml + repowise coverage add coverage.xml
     Ingested coverage for 270 file(s) (270 exact, 0 resolved).
5/5 🩺 repowise health
     3225 marker findings
✅ repowise coverage refresh verified at 16d27f9aa7d5: 270 file(s) at 99.01% line coverage,
   34972 test->file pair(s) from 13783 test(s).
```

**Ingested counts, recorded as criterion 1 requires:**

| Quantity | Baseline (`65e5af46`) | This measurement (`16d27f9a`) |
| --- | ---: | ---: |
| Test-to-code records (`.coverage`, per-test contexts) | 31150 | 34972 |
| Distinct test records behind that map, per the gate | 13495 | 13783 |
| Files with per-file line coverage (`coverage.xml`) | 258 (258 exact, 0 resolved) | **270 (270 exact, 0 resolved)** |
| Health findings rewritten by step 5, repo-wide | 3290 | 3225 |
| `untested_hotspot` findings after the refresh | 0 | **0** |
| Suite behind it | 7365 passed, 2 skipped, 19:24 | 7531 passed, 2 skipped, 19:00 |

`repowise coverage add` exits 0 even when it maps nothing, so the exit code proves nothing and was
not relied on: `scripts/repowise_coverage_gate.py` re-reads repowise's own stored state and fails
closed on a partial ingest, a commit skew, or a report whose files did not all map. 270-of-270
exact is the line that matters.

### Why the stored rows are fresh

```console
$ sqlite3 .repowise/wiki.db "SELECT count(*), min(updated_at), max(updated_at) FROM health_file_metrics"
2374|2026-08-22 05:47:24.720627|2026-08-22 05:47:24.789446
$ sqlite3 .repowise/wiki.db "SELECT count(*), min(updated_at), max(updated_at) FROM health_findings"
3225|2026-08-22 05:47:24.852165|2026-08-22 05:47:24.999223
$ sqlite3 .repowise/wiki.db "SELECT count(*), max(ingested_at) FROM coverage_files"
270|2026-08-22 05:46:43.440291
$ sqlite3 .repowise/wiki.db "SELECT count(DISTINCT substr(updated_at,1,16)) FROM health_file_metrics"
1
```

Every metric row carries one fold timestamp and every findings row carries an `updated_at` inside
the same second, both postdating this bead's fold start and both **five and a half hours after the
baseline's fold** (`2026-08-22 00:20:45`). One difference from the baseline is worth recording
rather than glossing: the baseline's fold had `created_at == updated_at` on all 3290 findings — it
recreated every row — whereas this fold recreated 2399 of 3225 and updated the other 826 in place.
That is a difference in how the fold treated surviving finding identities, not a freshness gap:
`updated_at` is fresh on all 3225.

### Three-route cross-check

| Cross-check | Scope | Mismatches |
| --- | --- | ---: |
| `repowise health --file <path> --format json` vs the committed JSON | 4 files × 9 fields (`score`, `max_ccn`, `max_nesting`, `nloc`, `duplication_pct`, `line_coverage_pct`, `branch_coverage_pct`, total deduction, finding count) = **36 values** | **0** |

The four are `services/analysis.py` (the worst file in `src`, and a split origin),
`routers/shell/__init__.py` (a new split product), `tasks/execution.py` (a wave-2 target) and
`config.py` (a split origin). `repowise health --file` **recomputes** the named file in-process, so
agreement with the stored rows is the check that the stored rows are what a recompute would
produce — which is the only thing that makes a bulk read trustworthy. It is not a cache bypass and
does not refresh what anyone else reads; that distinction is `phaze-bk9el.1` §9's, measured, and
this bead relied on it rather than re-deriving it.

### Where this run deviated from a single-directory run, and why

`scripts/repowise-coverage.sh` must run in the durable checkout: `.repowise/` is gitignored and
keyed on the checkout's absolute path, so a `bh` worktree has no index and the script refuses there
by design. `phaze-bk9el.1` split the halves — suite in its worktree, ingest in the durable
checkout — and could do so soundly **only because `65e5af46:src` and `8d1bf08c:src` were the same
tree object**. That is not true here: the epic tip carries 23 beads of `src/` change the durable
checkout's `main` does not. Findings carry line numbers and coverage carries line numbers, so
splitting the halves would have described two different trees.

So the durable checkout was detached onto `16d27f9a` for the whole run and restored to `main`
afterwards. This was safe at this point in the molecule and not earlier: all 23 sibling beads had
merged, no seat was live, and the checkout was clean of tracked and untracked changes both before
and after. The script's own commit-pairing guard (`HEAD MOVED DURING THE RUN`) did not fire.

## 2. The 24.0 points nothing can reach — unchanged, and subtracted

`phaze-bk9el.1` established that **12 empty `__init__.py` files each carry a 2.0
`coverage_gradient` deduction — 24.0 points — because Cobertura reports `line-rate="0"` for a file
with zero statements**. No test can remove any of it.

Re-derived here from the mechanism rather than from the baseline's path list (`num_statements == 0`
∧ `line_coverage_pct == 0.0` ∧ a `coverage_gradient` deduction), so the same rule applies to both
sides and would catch a new instance:

| | Before | After |
| --- | ---: | ---: |
| Files matching | 12 | **12** |
| Deduction | 24.000 | **24.000** |

Identical files, identical points. The epic did not create or remove any. **Every "genuinely
reachable" figure in this document has these 24.0 points subtracted from both sides**; the
subtraction cancels in the delta, which is why the reachable Δ is −25.847 either way, but it
changes the denominator: −25.847 is **14.85%** of the 174.041 genuinely-reachable points, not
13.05% of 198.041.

## 3. Where the reachable movement came from, by biomarker

| Biomarker | Bucket | n before | n after | Δn | Deduction before | after | Δ |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `prior_defect` | history-derived | 168 | 170 | +2 | 140.896 | 142.952 | **+2.056** |
| `complex_method` | reachable | 77 | 65 | −12 | 60.570 | 51.218 | −9.352 |
| `change_entropy` | history-derived | 43 | 43 | 0 | 45.410 | 48.220 | **+2.810** |
| `co_change_scatter` | history-derived | 40 | 37 | −3 | 37.739 | 34.709 | −3.030 |
| `coverage_gradient` | reachable | 71 | 70 | −1 | 31.045 | 30.473 | −0.572 |
| `nested_complexity` | reachable | 32 | 21 | −11 | 29.592 | 18.876 | −10.716 |
| `error_handling` | reachable | 205 | 201 | −4 | 23.536 | 23.537 | **+0.001** |
| `primitive_obsession` | reachable | 129 | 120 | −9 | 22.321 | 21.668 | −0.653 |
| `function_hotspot` | history-derived | 39 | 25 | −14 | 21.170 | 13.075 | −8.095 |
| `dry_violation` | reachable | 67 | 65 | −2 | 16.850 | 16.150 | −0.700 |
| `churn_risk` | history-derived | 33 | 27 | −6 | 13.902 | 11.698 | −2.204 |
| `large_method` | reachable | 22 | 18 | −4 | 8.527 | 6.522 | −2.005 |
| `knowledge_loss` | history-derived | 61 | 60 | −1 | 7.506 | 7.504 | −0.002 |
| `hidden_coupling` | history-derived | 24 | 22 | −2 | 6.261 | 6.053 | −0.208 |
| `low_cohesion` | reachable | 6 | 4 | −2 | 2.850 | 1.900 | −0.950 |
| `bumpy_road` | reachable | 14 | 11 | −3 | 2.750 | 1.850 | −0.900 |
| `complex_conditional` | unclassified | 4 | 4 | 0 | 1.437 | 1.437 | 0.000 |
| `brain_method` | unclassified | 1 | 0 | −1 | 0.600 | 0.000 | −0.600 |
| `ownership_risk` | history-derived | 1 | 1 | 0 | 0.286 | 0.286 | 0.000 |
| `io_in_loop` | performance | 65 | 64 | −1 | 0.000 | 0.000 | 0.000 |
| `serial_await_in_loop` | performance | 24 | 23 | −1 | 0.000 | 0.000 | 0.000 |
| `hot_path_sync_io` | performance | 1 | 2 | +1 | 0.000 | 0.000 | 0.000 |
| `nested_loop_with_io` | performance | 1 | 1 | 0 | 0.000 | 0.000 | 0.000 |
| **total** | | **1128** | **1054** | **−74** | **473.248** | **438.128** | **−35.120** |

### The four workstreams the epic was scoped on

The epic was scoped on four workstreams by an operator decision of 2026-08-21, recorded in
full on the epic bead `phaze-bk9el` (`AskUserQuestion`; the four selected option labels are
quoted verbatim there). Their measured results:

| Workstream | Biomarkers | n before → after | Deduction Δ | Share of the −25.847 reachable |
| --- | --- | ---: | ---: | ---: |
| **A. DRY** | `dry_violation` | 67 → 65 | **−0.700** | 2.7% |
| **B. error_handling** | `error_handling` | 205 → 201 | **+0.001** | −0.0% |
| **C. primitive_obsession** | `primitive_obsession` | 129 → 120 | **−0.653** | 2.5% |
| **D. residual complexity** | `complex_method`, `nested_complexity`, `large_method`, `bumpy_road` | 145 → 115 | **−22.973** | **88.9%** |
| (not a named workstream) | `coverage_gradient`, `low_cohesion` | 77 → 74 | −1.522 | 5.9% |

**Workstreams A, B and C together account for −1.352 points, 5.2% of the reachable movement.**
Workstream D — the biomarkers `phaze-vu88k` (iteration 1) already worked, re-aimed at the files
vu88k's three structural buckets structurally missed — accounts for 88.9%.

**`error_handling` is the plainest result in this ledger and the least flattering.** It was the
epic's single largest named bucket, 205 findings. Nine beads reviewed it. Four findings went
away and the deduction moved by **+0.001 points**. That is not a failure of the work: those nine
beads reviewed 77 of the 205 broad `except Exception` sites, concluded in every case that the
breadth was the mechanism rather than an oversight, and recorded a written reason at each site
(§6a). But **writing
a reason at a site does not remove its finding**, and a future iteration that scopes
`error_handling` expecting score movement should read this row first.

## 4. History-derived: how much of the −8.673 this epic actually caused

The 216 `src/phaze/**.py` files this epic **never touched** are the control. Their movement is
index drift — the trend window advanced by 41 commits — and nothing else.

| Population | n | Δ reachable | Δ history-derived | Δ total |
| --- | ---: | ---: | ---: | ---: |
| Touched by this epic, present on both sides | 41 | **−27.735** | −0.465 | −28.800 |
| **Not touched by this epic** | **216** | **+0.000** | **−5.309** | −5.309 |
| New paths (13 files) | 13 | +3.628 | +0.600 | +4.228 |
| Deleted path (`routers/shell.py`) | 1 | −1.740 | −3.499 | −5.239 |
| **repo-wide** | | **−25.847** | **−8.673** | **−35.120** |

Both columns reconcile to the repo-wide figure exactly.

Two things follow, and they point in opposite directions:

1. **Not one reachable point moved on a file this epic did not edit.** Δ reachable on the 216
   untouched files is **exactly +0.000**. The entire reachable result is attributable to the epic's
   own diffs — there is no drift component to discount.

2. **Most of the history-derived movement is not the epic's.** Decomposed:

   | Component | Δ | Is it this epic's doing? |
   | --- | ---: | --- |
   | Index drift on 216 untouched files | −5.309 | **No** — nobody edited them |
   | `routers/shell.py`'s history erased by the path change | −3.499 | **No** — artifact, see §5 |
   | New paths picking up new history findings | +0.600 | Artifact of the same kind, opposite sign |
   | Net on the 41 touched files | **−0.465** | Yes, and even this is drift-contaminated |

   **−0.465 of −8.673.** History-derived biomarkers decay only as a file stays stable, and this
   epic did the opposite of leaving files alone — it committed to 55 of them. The payoff is
   prospective, which every wave-3 bead said in its own commit message and which this measurement
   confirms.

The single largest untouched-file movement is `services/tracklist_matcher.py`, **worse** by +1.272
(score 7.28 → 6.01) with no change to the file whatsoever. That is the size of the noise floor on
any history-derived per-file number in this document.

## 5. The split files: their history-derived biomarkers reset because the paths are new

**This is an artifact of the split and not a gain.** Four files were split. Three kept their
original path and one did not, and that difference alone determines whether the history-derived
number moves.

Every row below is the **SUM across the resulting files** versus the original single file.

### 5a. `routers/shell.py` → `routers/shell/` (4 modules) — `phaze-bk9el.16`

The original path no longer exists. Its history-derived deduction did not decay; it was **erased**.

| | Before (1 file) | After (4 files) | Δ |
| --- | ---: | ---: | ---: |
| `score` (min / mean) | 4.76 / 4.76 | 8.43 / 9.48 | |
| `maintainability_score` (min) | 8.5 | 8.8 | |
| `performance_score` (min) | 10.00 | 10.00 | |
| `max_ccn` | 9 | 9 | 0 |
| `max_nesting` | 3 | 3 | 0 |
| `duplication_pct` (max) | 2.33 | **16.83** | **+14.50** |
| **reachable** | 1.740 | **2.078** | **+0.338 (worse)** |
| **history-derived** | 3.499 | **0.000** | **−3.499 — ARTIFACT** |
| total findings | 14 | 9 | −5 |

Findings before: `change_entropy` 1, `churn_risk` 1, `co_change_scatter` 1, `complex_method` 1,
`coverage_gradient` 1, `error_handling` 2, `function_hotspot` 1, `hidden_coupling` 2,
`primitive_obsession` 3, `prior_defect` 1.
After: `complex_method` 1, `coverage_gradient` 2, `dry_violation` 1, `error_handling` 2,
`primitive_obsession` 3.

**Read that as: all seven history-derived findings vanished because the path is new, and the
reachable bucket got 0.338 points worse.** The reachable regression is a new `dry_violation` on
`shell/__init__.py` (0.150, `duplication_pct` 16.83 across 4 clone pairs — the re-export facade
matching itself) plus `coverage_gradient` rising 0.143 → 0.331 as the 12 uncovered statements
redistributed across four smaller files. `routers/shell.py` was the busiest file in the repo — 34
commits in 90 days, 22 bug fixes, `change_entropy` in the top 0% — and none of that record was
paid down. It was renamed out of view and will re-accrue against the new paths.

Coverage, summed across the package, is the check that the move was a move:

| | Before | After |
| --- | ---: | ---: |
| Statements | 96.43% (324/336) | 96.59% (340/352) |
| Branches | **91.67% (66/72)** | **91.67% (66/72)** |

Bit-identical branch coverage, exactly as `phaze-bk9el.16` claimed. Per-file, though, the branch
figure is now very uneven: `shell/__init__.py` 100.00%, `shell/summary.py` 94.23%,
`shell/stage_context.py` **62.50%**, `shell/stage_maps.py` no branches. The package total conceals
a module three points below the repo's worst pre-split file.

### 5b. `services/analysis.py` → 4 new modules + the original — `phaze-bk9el.15`

The original path survives, so its history is intact and this row is **not** distorted.

| | Before (1 file) | After (5 files) | Δ |
| --- | ---: | ---: | ---: |
| `score` (min / mean) | 2.55 / 2.55 | **2.55** / 8.44 | min unchanged |
| `maintainability_score` (min) | 4.1 | **4.1** | 0 |
| `performance_score` (min) | 9.85 | 9.85 | 0 |
| `nloc` (sum) | 1161 | 1259 | +98 |
| `max_ccn` (max) | 7 | 7 | 0 |
| `max_nesting` (max) | 4 | 4 | 0 |
| `duplication_pct` (max) | 0.00 | **27.78** | **+27.78** |
| **reachable** | 3.710 | **4.060** | **+0.350 (worse)** |
| **history-derived** | 3.499 | 3.498 | −0.001 |
| total findings | 24 | 25 | +1 |

**`services/analysis.py` itself is unchanged on every health metric that matters.** Score 2.55 →
2.55, maintainability 4.1 → 4.1, reachable deduction 3.710 → 3.710, `nested_complexity` 4 findings
/ 2.260 points and `primitive_obsession` 5 / 0.950 both untouched — while `nloc` fell 1161 → 844
(−27%). The file that was #1 on every metric the epic measured is still the worst-scoring file in
`src` at exactly the score it started at. The 527 lines that left were declarations and pure
reductions, which carried no findings with them.

The group's reachable deduction went **up** 0.350: a new `dry_violation` on
`services/analysis_derive.py` (`duplication_pct` 27.78). Group branch coverage improved 93.97% →
96.55% (112/116), and `analysis.py` alone 93.97% → 98.21% (+4.25), which is real and is the
strongest thing this split delivered.

### 5c. `config.py` → `config.py` + `config_secrets.py` + `config_redis.py` — `phaze-bk9el.18`

Original path survives.

| | Before (1 file) | After (3 files) | Δ |
| --- | ---: | ---: | ---: |
| `score` (min / mean) | 5.15 / 5.15 | 5.90 / 8.58 | |
| `maintainability_score` (min) | 7.3 | 8.8 | +1.5 |
| `performance_score` (min) | 9.30 | 9.30 | 0 |
| `nloc` (sum) | 912 | 966 | +54 |
| `max_ccn` / `max_nesting` (max) | 8 / 3 | 8 / 3 | 0 |
| `duplication_pct` (max) | 0.00 | **7.43** | +7.43 |
| **reachable** | 1.350 | **0.750** | **−0.600** |
| **history-derived** | 3.500 | 3.500 | 0.000 |
| total findings | 12 | 9 | −3 |

The cleanest of the four: `low_cohesion` halved (2 findings / 1.200 → 1 / 0.600) and
`hidden_coupling` went 3 findings → 1, which is what splitting along an LCOM4=3 boundary is
supposed to do. History-derived is flat at 3.500 in total but redistributed across the three
paths. **Group branch coverage is flat at 98.53% (67/68)** even though `config.py` alone improved
98.53% → 100.00%, because the new `config_redis.py` arrives at 83.33%.

### 5d. `routers/pipeline_scans.py` → itself + `routers/scan.py` — `phaze-bk9el.17`

Original path survives.

| | Before (1 file) | After (2 files) | Δ |
| --- | ---: | ---: | ---: |
| `score` (min / mean) | 5.00 / 5.00 | 6.05 / 7.35 | |
| `maintainability_score` (min) | 7.1 | 7.9 | +0.8 |
| `nloc` (sum) | 580 | 610 | +30 |
| `max_ccn` / `max_nesting` (max) | 6 / 2 | 6 / 2 | 0 |
| `duplication_pct` (max) | 8.74 | **14.71** | +5.97 |
| **reachable** | 1.500 | **1.500** | **0.000** |
| **history-derived** | 3.500 | **3.801** | **+0.301 (worse)** |
| total findings | 12 | 13 | +1 |

**Reachable deduction did not move at all**: `dry_violation` 1, `error_handling` 2 and
`primitive_obsession` 4 are byte-identical before and after — the same findings, redistributed
across two files. History-derived went up because `routers/scan.py` arrived carrying its own
`prior_defect` finding (0.300); repowise attributes prior-defect history to moved lines, so a new
path does **not** reliably start at zero, which is the counterexample to the `shell.py` row above
and the reason the two are reported separately rather than as one rule.

Group coverage flat at 100% statements / 100% branches.

## 6. Every finding deliberately left unfixed

A bead that cleared everything is audited, not celebrated. All 23 sibling beads recorded their
non-fixes; this table consolidates them, with the bead that left each one and the reason as that
bead stated it.

### 6a. Left broad with a written reason at the site (`error_handling`)

| Findings | File(s) | Bead | Reason as recorded |
| ---: | --- | --- | --- |
| 15 | `backends/kueue.py`, `backends/compute_agent.py`, `backends/lane_snapshot.py` | `.2` | Hot-poll degrade-safe guards (T-71-03) and per-row reaper-loop guards. Narrowing any would break four named tests that pin a generic `RuntimeError` being swallowed (`test_admission_degrades_to_empty_on_db_error`, `test_admission_degrades_when_rollback_also_fails`, `test_probe_one_swallows_a_rollback_failure_after_a_failed_probe`, `test_snapshot_degrades_when_rollback_also_fails`). Inline "why broad" comment added at every site. |
| **7** | `services/analysis_decoder.py` | `.3` | D-09's deliberate leak-prevention defensive catches; not in `.3`'s acceptance criteria; "narrowing them is a behaviour question for its own bead". **This handoff was never executed — see §7.** |
| 8 | `services/review.py` | `.4` | Already carried inline reasons tied to the module's degrade-safe contract ("the hot render/poll path can NEVER 500"); nothing changed. |
| 8 | `services/tracklist_render.py` | `.5` | Best-effort-cleanup or filter-and-reraise patterns; four that lacked a reason got one, including `launch()`'s `except BaseException` (deliberate, `phaze-ccm02`: must roll back on `asyncio.CancelledError` too). None narrowed. |
| 9 of 10 | `tasks/execution.py` | `.7` | HTTP-reporting catches after the move already committed, or `_execute_one`'s per-proposal isolation boundary. The tenth — `_atomic_cross_fs_copy`'s `except BaseException: cleanup(); raise` — became `finally:` and its finding is gone. |
| 5 | `tasks/reenqueue.py` | `.7` | Three per-row isolation catches, one T-45-14 degrade-safe SAVEPOINT read, one (`_regenerate_s3_upload`) that cannot become `finally` because its success path skips the cleanup its `except` performs. |
| 2 | `routers/cue.py:163`, `routers/tags.py:713` | `.10` | Heterogeneous enqueue failure modes surfaced as one operator-facing toast; one bad candidate must not abort the rest of a bulk batch. |
| 11 | `routers/pipeline/**` | `.11` | Reviewed; every site already carried an explicit reason. None changed. |
| 10 | `job_runner.py` | `.12` | `job_runner` is the one-shot pod that must translate ANY failure mode into the module's documented exit-code contract (D-01), so a broad catch is the mechanism. Reason comment added at each site; none narrowed. |
| 2 | `agent_watcher/__main__.py` | `.13` | One guards a documented `Poster.post_one` no-throw contract; the other is the top-level per-tick guard for an unattended sweep loop with no operator watching stdout. |

That is 77 of the 205 `error_handling` findings reviewed and deliberately kept, which is most of
the explanation for the **+0.001** row in §3.

The reasons above are quoted as each bead recorded them; measured at the epic tip, the files
behaved as those reasons predict, with two places where a finding went away as a side effect of
unrelated work rather than by narrowing:

| File | `error_handling` findings | Deduction |
| --- | ---: | ---: |
| `job_runner.py` | 10 → **10** | 0.500 → 0.500 |
| `services/review.py` | 8 → **8** | 0.496 → 0.496 |
| `services/tracklist_render.py` | 8 → **8** | 0.496 → 0.496 |
| `tasks/reenqueue.py` | 5 → **5** | 0.500 → 0.500 |
| `services/analysis_decoder.py` | 7 → **7** | 0.497 → 0.497 |
| `agent_watcher/__main__.py` | 2 → **2** | 0.300 → 0.300 |
| `tasks/execution.py` | 10 → 9 | 0.500 → 0.504 |
| `services/backends/lane_snapshot.py` | 10 → **7** | 0.500 → 0.497 |

`tasks/execution.py` lost one because `.7` converted `_atomic_cross_fs_copy`'s
`except BaseException: cleanup(); raise` — which never swallowed anything — into `finally:`; its
deduction still rose 0.004. `lane_snapshot.py` lost three because `.2` collapsed a 4×-duplicated
"rollback, swallow, log" block into one `_rollback_and_log` helper: the catches did not become
narrower, there are simply three fewer places doing it. Those are the only two of the ten rows
where the count moved at all.

### 6b. Ruled a false positive or not-debt

| Finding | Bead | Ruling |
| --- | --- | --- |
| `backends/local.py` ↔ `backends/compute_agent.py`, 17-line clone | `.2` | Import / `TYPE_CHECKING` / logger / docstring-open boilerplate, not shared logic. False positive. |
| `services/scan_deletion.py` ↔ `tests/discovery/services/test_scan_deletion.py`, 18 lines / 40.9% | `.6` | `_EXPECTED_COUNTS` necessarily restates the cascade's tablenames so the test catches a table silently dropping out of `delete_scan_cascade`'s ordered list. That is the test doing its job; left uncoupled. |
| `routers/agent_tag_writes.py` ↔ `tasks/proposal.py`, 12 lines | `.9` | The two files' import blocks. No shared logic between an HTTP callback router and a SAQ task. Clone-detector false positive. |
| `routers/pipeline/__init__.py` ↔ `services/pipeline/__init__.py`, 55 lines (the two highest `duplication_pct` readings in `src`) | `.11` | Barrel boilerplate. The two facades re-export disjoint symbol sets for unrelated packages; the shared shape is forced by the re-export discipline both already document. Documented in both docstrings. |
| `routers/pipeline/files.py`, 22.1% duplication | `.11` | Repeated FastAPI `Query()` parameter scaffolding across 4 routes plus `routers/search.py`. Acceptable framework idiom; extracting it would touch an out-of-scope file. |
| `_authorize_scan_root`'s containment gate — tests every configured root, not the submitted one | `.17` | Preserved **verbatim** and pinned CHARACTERIZED-NOT-ENDORSED by `test_authorize_scan_root_accepts_a_path_under_a_DIFFERENT_configured_root`. Not live today (`trigger_scan` always derives `joined` from the submitted root). Tightening it is a behaviour change → **filed as `phaze-4jvy1`**. |

### 6c. Reviewed and left as-is (`primitive_obsession` and standing extract plans)

| Finding | Bead | Reason |
| --- | --- | --- |
| `analysis_decoder.py`'s `stop_at_sec`, `on_beat`, `on_skip`, `watchdog_enabled` left as independent parameters; `_decode_windows` / `_decode_windows_streaming` keep their positional signature | `.3` | Documented on `DecodeTarget` why they do not belong in it; `services/analysis.py` calls the two functions positionally and was out of `.3`'s scope. (`primitive_obsession` on this file did move: 8 → 6 findings, 1.501 → 1.100.) |
| `tag_write_disk.py` has no dedicated test file | `.6` | Recorded as covered-by-integration, with the covering tests named, rather than adding a redundant file. |
| `_execute_one` (8 params), `_reclaim_or_refuse_existing_destination` (5), `_is_orphaned` (5), `_replay_agent_rows_by_owner` (5) | `.7` | Reviewed; none is a primitive cluster. |
| `_classify_saq_job_row`'s extract-method plan | `.7` | Already rejected with a recorded reason under `phaze-as6xh` (CCN 10 is inherent to the row shape); carried forward unchanged. |
| `dispatch_approved_batch` keeps its five keyword-only parameters | `.14` | The module's stable external call boundary (`routers/execution.py` plus 14 keyword-arg test call sites). Criterion 2 discharged as "addressed", not "zeroed" — an explicit implementer decision, not a metric miss. |
| `reconcile_cloud_jobs.py`'s `name` / `job` / `workload` / `node_loss_reason` left out of `_RowReconcile` | `.8` | Each varies within a single row's reconcile, so folding them in would blur "this call's environment" with "what this call computed". |
| D-07, D-08 and D-09 and every function implementing them | `.15` | Outside the scope of the attribution. The Q2 answer of 2026-08-21 — selected option label, verbatim: "Include and refactor it" — authorises working on `services/analysis.py`; it does not authorise changing those invariants, which were named in the option text as the reason for the standing ruling. Durable record: the operator-decision comment on bead `phaze-bk9el`. `_peak_rss_gib`'s nesting stays because it dispatches on a Darwin-bytes vs Linux-KiB difference that is 1024× wrong if collapsed. |

### 6d. Carried forward from iteration 1 (`phaze-vu88k`), reason re-verified not re-derived

| Findings | File(s) | Bead | Reason |
| ---: | --- | --- | --- |
| residual `io_in_loop` / `serial_await_in_loop` | `backends/kueue.py`, `backends/compute_agent.py` | `.2` | `phaze-vu88k.2`'s reason still holds; untouched. |
| 7 `io_in_loop` / `serial_await_in_loop` | `services/tracklist_drain.py` | `.5` | `phaze-vu88k.9` §S5c's reason re-verified: `.3`'s criteria protect the drain ordering and arm/disarm state machine ("a reordering here is a behaviour change, not a refactor"). Additionally, `_load_added_at`'s loop chunks `file_ids` under asyncpg's 32767 bind-parameter cap over **one** `AsyncSession`, and concurrent awaits against one session are unsafe — so a gather-based fan-out is a different bug, not a fix. |

All 91 performance findings deducted **0.000** at the baseline and **0.000** now, on 90 findings.
`phaze-vu88k.2` already cleared the ones that scored. A future bead that fixes performance
findings to move a score will measure nothing; that is a property of the scoring model.

### 6e. Narrowed scope, remainder filed as its own bead

Four seats independently found the same defect in how the epic's beads were written — the context
paragraph named more files than the numbered criteria did — flagged the narrowing before submit
per CLAUDE.md rule 1, and the dispatcher filed the remainder. This is the behaviour the rule asks
for and it worked four times out of five.

| Bead | Remainder left | Filed as |
| --- | --- | --- |
| `.4` | `services/proposal.py`, `proposal_queries.py`, `cue_review.py` | `phaze-bk9el.26` |
| `.5` | `tracklist_scraper.py` (CCN 12), `tracklist_priority.py` (CCN 13), `tracklist_drain.py`'s 6 `primitive_obsession` incl. `drain_once`'s 11-parameter signature | `phaze-bk9el.28` |
| `.6` | `dedup.py`, `search_queries.py`, `companion.py`, `agent_task_router.py` | `phaze-bk9el.25` |
| `.8` | `release_awaiting_cloud.py`, the reapers, `_shared/deterministic_key.py` | `phaze-bk9el.27` |
| `.22` | The coarse tier's 34-graph TensorFlow model sweep is unpinned (no model set in repo or CI, so `_get_classifier` raises and the equivalence recording carries `coarse_windows_analyzed == 0`) | `phaze-28883` |

`.22`'s disclosure is worth reading in full: it could have supplied a fake `models_dir` or mocked
`TensorflowPredict*` and produced a green test over the coarse path, and refused on the grounds
that this would be "a harness that looks like coverage and proves nothing" — discharge-by-proxy,
the exact failure ADR-0012 rule 3 exists to prevent.

### 6f. Dead code found and deleted rather than documented

`routers/tags.py`'s `isinstance(after, dict)` guard in `_validate_tag_review_token` turned out to
be genuinely unreachable rather than a coverage gap (`.10`). It was deleted, on review feedback
that a code-quality epic should not leave dead code standing on the strength of "provably
unreachable" — that conclusion is deletion, not annotation. Recorded here because it is the one
finding in this epic resolved by removal rather than by extraction or by a reason.

## 7. Dropped work — the audit criterion 7 asks for

Three handoffs were asserted in commit messages across the molecule. **Two were executed. One was
not.**

| Handoff, as asserted | Bead | Executed? |
| --- | --- | --- |
| "tightening it is a behaviour change belonging to its own bead" (`_authorize_scan_root`'s containment gate) | `.17`, commit `0010dc49` | ✅ `phaze-4jvy1` |
| "left untouched — reported to the dispatcher rather than silently expanded into" (`tracklist_scraper`, `tracklist_priority`, drain's `primitive_obsession`) | `.5`, commit `b2674f74` | ✅ `phaze-bk9el.28` |
| **"narrowing them is a behaviour question for its own bead"** (`services/analysis_decoder.py`'s 7 broad `except Exception`) | **`.3`, commit `fd1041a9`** | ❌ **never filed** |

**The dropped one, in full.** `phaze-bk9el.3` wrote:

> The 7 broad `except Exception:` catches (error_handling) are left untouched: they are D-09's
> deliberate leak-prevention defensive catches, not in this bead's acceptance criteria, and
> narrowing them is a behaviour question for its own bead.

No such bead was filed. A search of all 2401 beads for `analysis_decoder` returns only
`phaze-bk9el.3` itself, `phaze-bk9el.21` and the epic — none covering it. This is the same defect
class `phaze-vu88k.9` caught on `services/proposal.py` at commit `a6c3435a`, and it is a **fifth**
instance of the bead-writing defect §6e describes: the epic's own description names
`services/analysis_decoder.py` in workstream B as one of the six densest `error_handling` clusters
in `src` (7 findings), and `.3`'s five numbered acceptance criteria cover `analysis_sizing`
nesting, `extract_audio_track` CCN, the `es.MetadataReader` real-consumer verification,
`analysis_decoder` **primitive obsession**, and `just check` — `error_handling` appears in none of
them. The four other instances got a remainder bead; this one got a commit-message sentence.

Measured at the epic tip, `services/analysis_decoder.py` still carries `error_handling` **7
findings / 0.497 deduction** — byte-for-byte the baseline figure. The seat's reason was sound and
is not overturned here; these are D-09's guards around the streaming decode network, and narrowing
them genuinely is a behaviour question.

**Filed as `phaze-bk9el.29`** (P3, parent `phaze-bk9el`), with "reviewed and left broad with
reasons" as an explicitly acceptable outcome.

## 8. Confounds — measured, and there are none of the `phaze-vu88k.9` kind

`phaze-vu88k.9` found that an unrelated concurrent epic (`phaze-1i0h6`) had landed on `main` in
the same window and touched seven of the same files, which is why numbers moved on files vu88k
never edited. This bead was required to flag any file in the same position. **There are none, and
that is checkable rather than asserted:**

```console
$ git merge-base 16d27f9a main
8d1bf08cf1bdb7a6075784872c45404c09cbe897
$ git rev-parse main
8d1bf08cf1bdb7a6075784872c45404c09cbe897
```

`main` is an **ancestor** of the pinned commit and has not moved since the epic branched. Every one
of the 41 commits between `65e5af46` and `16d27f9a` is a `phaze-bk9el` bead's own work or its merge
commit; the epic branch never merged `main` back in because there was nothing to merge. No other
epic's commits are in the range.

**Intra-epic overlap** — which this bead checked as well, since two sibling beads editing one file
is the same measurement problem at smaller scale — is a single file: `src/phaze/main.py`, touched
by `.12` (collapsing the 71-line `include_router` self-clone into a `_ROUTERS` tuple) and by `.17`
(registering the new `routers/scan.py`). The other 54 `src/` files each have exactly one bead. Its
row is therefore reported against both beads and not attributed to either.

The one thing that **is** a confound is index drift, and §4 measures it directly rather than
flagging files: −5.309 history-derived points moved on files nobody touched, and `Δ` reachable on
those files is exactly zero.

## 9. Per-file ledger, files touched by the epic

All 41 files present on both sides, ordered by score change. Split products are in §5 and are not
repeated here. `reach` / `hist` are the reachable and history-derived deduction; read them
separately, per criterion 3.

| File (`src/phaze/`) | Bead | score | maint | perf | CCN | nest | dup% | reach | hist |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `routers/agent_metadata.py` | `.9` | 8.58→**6.26** | 8.8→8.8 | 10.00→10.00 | 3→3 | 2→1 | 25.93→27.67 | 0.600→0.600 | 0.820→3.137 |
| `main.py` | `.12`,`.17` | 7.38→**6.00** | 9.0→9.0 | 9.30→9.30 | 4→4 | 3→3 | 35.99→31.70 | 0.500→0.500 | 2.120→3.500 |
| `schemas/agent_scan_batches.py` | `.14` | 8.53→**7.62** | 9.7→10.0 | 10.00→10.00 | 4→4 | 1→1 | 19.77→0.00 | 0.150→0.000 | 1.320→2.377 |
| `routers/pipeline/dashboard_stats.py` | `.11` | 8.70→**8.20** | 9.1→**7.9** | 10.00→10.00 | 14→14 | 2→2 | 19.55→20.15 | 1.297→**1.801** | 0.000→0.000 |
| `schemas/agent_execution.py` | `.14` | 9.23→9.18 | 9.3→10.0 | 10.00→10.00 | 3→3 | 1→1 | 26.53→0.00 | 0.350→0.000 | 0.420→0.820 |
| `routers/agent_analysis.py` | `.9` | 5.75→5.75 | 8.5→8.5 | 9.18→9.18 | 6→6 | 2→2 | 26.52→25.82 | 0.750→0.750 | 3.500→3.500 |
| `routers/cue.py` | `.10` | 5.27→5.27 | 7.8→7.8 | 10.00→10.00 | 7→7 | 2→2 | 14.18→13.84 | 1.229→1.229 | 3.499→3.500 |
| `routers/pipeline/__init__.py` | `.11` | 9.65→9.65 | 9.3→9.3 | 10.00→10.00 | 1→1 | 0→0 | 49.72→46.31 | 0.350→0.350 | 0.000→0.000 |
| `routers/pipeline/files.py` | `.11` | 8.70→8.70 | 7.7→7.7 | 10.00→10.00 | 8→8 | 3→3 | 22.15→20.45 | 1.296→1.296 | 0.000→0.000 |
| `routers/pipeline/tracklists.py` | `.11` | 9.70→9.70 | 9.4→9.4 | 10.00→10.00 | 4→4 | 3→3 | 22.67→10.91 | 0.300→0.300 | 0.000→0.000 |
| `services/analysis.py` | `.15` | 2.55→2.55 | 4.1→4.1 | 9.85→10.00 | 7→6 | 4→4 | 0.00→1.71 | 3.710→3.710 | 3.499→3.498 |
| `services/backends/compute_agent.py` | `.2` | 8.30→8.30 | 9.1→9.1 | 8.00→8.00 | 12→12 | 3→3 | 12.37→12.24 | 1.700→1.700 | 0.000→0.000 |
| `services/pipeline/__init__.py` | `.11` | 9.65→9.65 | 9.3→9.3 | 10.00→10.00 | 1→1 | 0→0 | 41.67→40.86 | 0.350→0.350 | 0.000→0.000 |
| `services/tracklist_drain.py` | `.5` | 4.55→4.55 | 7.1→7.1 | 8.00→8.00 | 13→13 | 2→2 | 4.19→4.15 | 1.952→1.952 | 3.501→3.500 |
| `tasks/reenqueue.py` | `.7` | 4.49→4.49 | 7.9→7.9 | 10.00→10.00 | 10→10 | 3→3 | 0.75→0.73 | 2.013→2.013 | 3.500→3.500 |
| `job_runner.py` | `.12` | 4.30→4.45 | 6.5→6.7 | 10.00→10.00 | **22→10** | 3→3 | 2.14→2.35 | 2.203→2.049 | 3.501→3.500 |
| `routers/tags.py` | `.10` | 5.91→6.20 | 9.4→9.4 | 8.60→8.60 | 8→8 | 3→3 | 4.76→4.68 | 0.587→0.300 | 3.500→3.500 |
| `database.py` | `.12` | 8.83→9.18 | 9.3→10.0 | 10.00→10.00 | 2→2 | 1→1 | 26.60→**0.00** | 0.350→0.000 | 0.820→0.820 |
| `services/tracklist_render.py` | `.5` | 8.35→8.70 | 6.3→7.0 | 10.00→10.00 | 6→6 | 3→3 | 1.18→1.11 | 1.346→0.996 | 0.300→0.300 |
| `tasks/controller.py` | `.12` | 5.70→6.05 | 8.4→9.1 | 10.00→10.00 | 6→6 | 3→3 | 12.78→7.92 | 0.800→0.450 | 3.500→3.500 |
| `services/analysis_decoder.py` | `.3` | 6.71→7.11 | 5.3→5.3 | 10.00→10.00 | 6→6 | 4→4 | 0.00→0.00 | 2.986→2.583 | 0.300→0.300 |
| `routers/request_guards.py` | `.9` | 8.00→8.53 | 10.0→9.7 | 10.00→10.00 | 6→6 | 2→2 | 0.00→0.00 | 0.000→0.150 | 2.000→1.320 |
| `routers/pipeline/skip.py` | `.11` | 8.08→8.67 | 9.1→9.1 | 9.30→9.30 | **22→9** | 2→2 | 14.07→13.29 | 1.917→1.325 | 0.000→0.000 |
| `routers/agent_scan_batches.py` | `.9` | 7.14→7.76 | 10.0→10.0 | 10.00→10.00 | **19→13** | 2→2 | 4.73→4.28 | 1.537→0.924 | 1.320→1.320 |
| `services/execution_dispatch_protocol.py` | `.14` | 8.45→9.10 | 7.1→8.8 | 10.00→10.00 | 6→6 | 3→3 | 0.00→0.00 | 1.550→0.600 | 0.000→0.300 |
| `config.py` | `.18` | 5.15→5.90 | 7.3→8.8 | 9.30→10.00 | 8→7 | 3→3 | 0.00→0.00 | 1.350→0.600 | 3.500→3.500 |
| `services/kube_staging.py` | `.6` | 4.30→5.14 | 9.0→9.0 | 10.00→10.00 | 9→8 | 4→4 | 0.00→0.00 | 2.203→1.355 | 3.499→3.500 |
| `tasks/reconcile_cloud_jobs.py` | `.8` | 4.85→5.71 | 8.0→8.7 | 10.00→10.00 | 8→8 | 3→3 | 3.87→**0.00** | 1.651→0.789 | 3.500→3.500 |
| `tasks/execution.py` | `.7` | 4.47→5.47 | 6.7→7.0 | 9.30→9.30 | **12→8** | 3→3 | 7.20→6.85 | 2.029→1.032 | 3.500→3.500 |
| `routers/pipeline_scans.py` | `.17` | 5.00→6.05 | 7.1→9.1 | 10.00→10.00 | 6→6 | 2→2 | 8.74→14.71 | 1.500→0.450 | 3.500→3.501 |
| `routers/proposals.py` | `.10` | 4.79→5.90 | 8.8→8.8 | 10.00→10.00 | 9→8 | 3→3 | 6.89→6.73 | 1.705→0.600 | 3.500→3.501 |
| `services/review.py` | `.4` | 3.46→4.60 | 6.7→7.1 | 8.00→8.00 | 11→11 | 4→3 | 5.63→3.92 | 3.033→1.892 | 3.500→3.500 |
| `services/analysis_sizing.py` | `.3` | 5.85→7.13 | 7.1→8.3 | 9.30→9.15 | 13→12 | **5→4** | 0.00→0.00 | 4.150→2.873 | 0.000→0.000 |
| `services/backends/kueue.py` | `.2` | 6.15→7.90 | 6.6→8.2 | 8.00→8.00 | **20→15** | **5→3** | 8.33→7.02 | 3.855→2.096 | 0.000→0.000 |
| `services/video_audio.py` | `.3` | 7.33→9.30 | 7.5→9.4 | 10.00→10.00 | **18→7** | 3→3 | 0.00→0.00 | 1.766→0.401 | 0.300→0.300 |
| `services/reanalysis_backfill.py` | `.6` | 6.36→8.46 | 8.4→8.7 | 9.30→9.30 | **22→7** | 4→4 | 4.88→4.36 | 3.338→1.238 | 0.300→0.300 |
| `services/analysis_wire.py` | `.3` | 7.11→9.43 | 9.3→9.7 | 10.00→10.00 | 9→7 | 4→3 | 0.00→**19.44** | 2.588→0.150 | 0.300→0.420 |
| `services/tag_write_disk.py` | `.6` | 6.63→9.03 | 9.0→9.7 | 10.00→10.00 | **15→7** | 4→3 | 7.14→6.87 | 2.674→0.274 | 0.700→0.700 |
| `services/backends/lane_snapshot.py` | `.2` | 5.84→8.41 | 5.2→8.0 | 8.00→8.00 | 10→10 | 4→3 | 0.00→0.00 | 4.161→1.585 | 0.000→0.000 |
| `agent_watcher/__main__.py` | `.13` | **2.29→6.53** | 6.9→8.1 | 10.00→10.00 | 13→13 | **5→4** | 0.00→0.00 | 4.213→2.649 | 3.500→0.820 |
| `tasks/metadata_extraction.py` | `.8` | **4.44→8.73** | 7.9→9.1 | 10.00→10.00 | 6→4 | **5→2** | 14.29→13.10 | 2.058→0.450 | 3.500→0.820 |

Two rows in that table have a history-derived component large enough to distort the score reading
and are called out for it: `agent_watcher/__main__.py` and `tasks/metadata_extraction.py` both
show history-derived falling 3.500 → 0.820 (−2.680), which is roughly two-thirds of each file's
total improvement. Their reachable movement — −1.564 and −1.608 — is the part attributable to the
refactor. `agent_watcher/__main__.py` was the worst-scoring file in the repo at 2.29 and is now
6.53; the reachable half of that is real and the history half is the trend window moving.

### New files created by the epic

| File (`src/phaze/`) | From | score | maint | nloc | CCN | nest | dup% | reach | hist | findings |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `config_redis.py` | `.18` split | 10.00 | 10.0 | 56 | 5 | 1 | 0.00 | 0.000 | 0.000 | — |
| `config_secrets.py` | `.18` split | 9.85 | 9.7 | 124 | 8 | 3 | 7.43 | 0.150 | 0.000 | `io_in_loop` 1, `primitive_obsession` 1 |
| `routers/scan.py` | `.17` split | 8.65 | 7.9 | 302 | 6 | 2 | 5.96 | 1.050 | 0.300 | `error_handling` 2, `primitive_obsession` 2, `prior_defect` 1 |
| `routers/shell/__init__.py` | `.16` split | 9.85 | 9.7 | 186 | 7 | 1 | **16.83** | 0.150 | 0.000 | `dry_violation` 1 |
| `routers/shell/stage_context.py` | `.16` split | 9.64 | 9.7 | 420 | 7 | 3 | 6.62 | 0.359 | 0.000 | `coverage_gradient` 1, `error_handling` 1 |
| `routers/shell/stage_maps.py` | `.16` split | 10.00 | 10.0 | 72 | 2 | 0 | 0.00 | 0.000 | 0.000 | — |
| `routers/shell/summary.py` | `.16` split | 8.43 | 8.8 | 496 | 9 | 2 | 2.78 | 1.569 | 0.000 | `complex_method` 1, `coverage_gradient` 1, `error_handling` 1, `primitive_obsession` 3 |
| `schemas/wire_mixins.py` | `.14` extraction | 9.70 | 10.0 | 29 | 2 | 1 | 0.00 | 0.000 | 0.300 | `prior_defect` 1 |
| `services/agent_upsert.py` | `.9` extraction | 10.00 | 10.0 | 39 | 1 | 0 | 0.00 | 0.000 | 0.000 | — |
| `services/analysis_derive.py` | `.15` split | 9.65 | 9.3 | 86 | 7 | 3 | **27.78** | 0.350 | 0.000 | `dry_violation` 1 |
| `services/analysis_models.py` | `.15` split | 10.00 | 10.0 | 91 | 6 | 2 | 0.00 | 0.000 | 0.000 | — |
| `services/analysis_probe.py` | `.15` split | 10.00 | 10.0 | 123 | 6 | 2 | 0.00 | 0.000 | 0.000 | `hot_path_sync_io` 1 |
| `services/analysis_windows.py` | `.15` split | 10.00 | 10.0 | 115 | 5 | 2 | 0.00 | 0.000 | 0.000 | — |

### The DRY cluster the epic opened with

`phaze-bk9el.9`'s target — `routers/agent_analysis.py` ↔ `routers/agent_metadata.py`, 32 shared
lines across 32 clone pairs, described in the epic as "the clearest genuine DRY cluster in the
repo" — is the one result worth naming individually because it is the workstream the epic led with.
Two commits went into it: the first extracted the vanished-file guard (a real but *different*
clone, 5 call sites), and the second, `153f5465`, extracted the actual flagged line range
(`build_field_lww_set_clause`) after the seat noticed the first had not touched it. After both:

| | Before | After |
| --- | ---: | ---: |
| `agent_analysis.py` `duplication_pct` | 26.52 | 25.82 |
| `agent_metadata.py` `duplication_pct` | 25.93 | **27.67** |
| `agent_analysis.py` `dry_violation` | 1 finding / 0.600 | 1 / 0.600 |
| `agent_metadata.py` `dry_violation` | 1 finding / 0.600 | 1 / 0.600 |
| `agent_analysis.py` score | 5.75 | 5.75 |
| `agent_metadata.py` score | 8.58 | **6.26** |

Both `dry_violation` findings survive at their full deduction, one file's duplication went up, and
`agent_metadata.py`'s score fell 2.32 on history-derived findings its own commits created. The
extraction is real — `services/agent_upsert.py` exists and both handlers call it — and the
biomarker did not move. `phaze-bk9el.9`'s own criterion 4 anticipated this exactly: "A number that
did not move is a finding to report, not to hide."

## 10. Things bearing on these figures that this bead did not chase

**Five CI checks fail on PR #513 and all five pass locally at the same commit.** Recorded because
a reader comparing this ledger against CI will otherwise conclude the tree is red:

| Failing check | Test | Why it is environment-specific |
| --- | --- | --- |
| `test / Tests (analyze-svc-pipeline)` | `test_real_extraction_output_is_readable_by_the_real_consumer_es_metadatareader` | `es.MetadataReader` reads duration 0 from the `.mka` the runner's ffmpeg produced. **This is `phaze-bk9el.3`'s own real-consumer test doing precisely what it was written to do** — it is the `phaze-3ea41` condition, reproduced on the runner's ffmpeg. |
| `test / Tests (shared-core)` | `test_control_settings_resolve_to_recorded_defaults`, `test_agent_settings_resolve_to_recorded_defaults` | The `phaze-bk9el.22` characterization golden picks up runner environment that the local scrubbed environment does not. |
| `test / Tests (shared-rest)` | `test_push_ssh_key_from_secret_file_is_accepted_by_openssh` | The runner's `ssh-keygen` says `error in libcrypto` where the local one says `invalid format`; the assertion pins the string. Again a test **this epic added** (`.18`). |
| `security / Semgrep CE Scan`, `aggregate-results` | — | Downstream of the above. |

Two of the three root causes are tests this epic introduced to close verification-fidelity gaps.
They are being handled separately and none of them changes a health figure in this document: the
fold's own suite ran 7531 passed / 2 skipped / 0 failed at `16d27f9a`, and a red suite would have
aborted the ingest before step 3.

**The `.repowise` index in the durable checkout is left synced to `16d27f9a`**, one epic ahead of
`main`'s working tree, which is what every number here was read from. It becomes correct for `main`
the moment PR #513 lands.

## 11. Reproducing this ledger

```bash
# 1. the fold — MUST run before any bulk read; `repowise update` alone does neither half
git checkout --detach 16d27f9a          # in the durable checkout, which is the only one with .repowise/
just repowise-coverage ledger           # 5 steps, ~25 min, fails closed on a silent no-op

# 2. the "after" artifact, from the same script the baseline used
uv run coverage json -o coverage.json --fail-under=0
uv run scripts/health_baseline.py \
    --db .repowise/wiki.db --coverage-json coverage.json \
    --baseline-commit 16d27f9aa7d5878d8b2ae1f0cb2bcb9e6096a9eb \
    --index-commit   16d27f9aa7d5878d8b2ae1f0cb2bcb9e6096a9eb \
    --out docs/spikes/phaze-bk9el.19-health-after-2026-08-22.json

# 3. every figure in this document is a diff of the two committed JSONs
jq -S . docs/spikes/phaze-bk9el.1-health-baseline-2026-08-21.json > /tmp/before.json
jq -S . docs/spikes/phaze-bk9el.19-health-after-2026-08-22.json   > /tmp/after.json
diff /tmp/before.json /tmp/after.json

# 4. the split-group coverage sums in §5
just branch-check --file src/phaze/services/analysis.py --file src/phaze/config.py ...
```

**No comparison script is committed.** The two JSON artifacts carry every input to every table
above — per-file `score`, `maintainability_score`, `performance_score`, `max_ccn`, `max_nesting`,
`nloc`, `duplication_pct`, coverage, `deduction` split by bucket and `deduction_by_biomarker` — so
each figure is re-derivable from them by inspection, and the derivation this bead used was ad hoc
rather than a new tool that would owe a test file. The one derivation worth restating because it is
not a plain sum is §2's zero-statement rule: `num_statements == 0` ∧ `line_coverage_pct == 0.0` ∧ a
non-zero `coverage_gradient` entry in `deduction_by_biomarker`, which selects exactly the 12 files
`phaze-bk9el.1` listed, on both sides.

## 12. Provenance: which tool produced each figure

| Figures | Route | Cached? |
| --- | --- | --- |
| Statement / branch / combined coverage, repo-wide and per split group | `coverage.json` from coverage.py, this bead's own suite run at `16d27f9a` | no — not a repowise artifact |
| Per-file branch coverage vs the `.coverage-baseline.json` baseline | `just branch-check` → `scripts/branch_coverage_check.py` | no |
| Ingest counts (34972 records, 270 files) | `repowise coverage add` stdout + `scripts/repowise_coverage_gate.py` re-reading `repowise coverage status --format json` and `repowise status --format json` | no — the gate re-reads stored state rather than trusting an exit code |
| `score`, `maintainability_score`, `performance_score`, `max_ccn`, `max_nesting`, `nloc`, `duplication_pct`, `line_coverage_pct`, `branch_coverage_pct` | `scripts/health_baseline.py` → **direct sqlite read** of `.repowise/wiki.db` (`health_file_metrics`) | no — reads the stored rows, below any tool layer |
| Every finding, its `health_impact`, its `details_json` | same, `health_findings` | same |
| Freshness timestamps | `sqlite3` against the same tables | no |
| The 4×9 cross-check | `repowise health --file <path> --format json` (an in-process **recompute**) | no |
| Commit ranges, file→bead attribution, handoff audit | `git log` / `git diff` against the epic branch; `bh work issue` / `bh work list` for bead state | n/a |

**No figure in this document came from the MCP tools.** `get_health`, `get_context` and `get_risk`
were not used to produce any of it. That is not because the MCP layer is untrustworthy — it serves
the same stored rows as everything else, which `phaze-bk9el.1` §9 measured field by field — but
because a single documented route is easier to re-run than three.
