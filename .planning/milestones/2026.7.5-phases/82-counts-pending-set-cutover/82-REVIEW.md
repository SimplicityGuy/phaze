---
phase: 82-counts-pending-set-cutover
reviewed: 2026-07-10T00:00:00Z
depth: standard
files_reviewed: 14
files_reviewed_list:
  - src/phaze/services/stage_status.py
  - src/phaze/services/pipeline.py
  - src/phaze/routers/pipeline.py
  - src/phaze/templates/pipeline/partials/stats_bar.html
  - scripts/seed_perf_corpus.py
  - scripts/perf_explain.py
  - justfile
  - tests/integration/test_stage_status_equivalence.py
  - tests/integration/test_enrich_pending_independence.py
  - tests/integration/test_pending_set_divergence.py
  - tests/integration/test_stage_progress_buckets.py
  - tests/shared/test_pending_set_source_scan.py
  - tests/shared/routers/test_pipeline_stats.py
  - tests/shared/services/test_pipeline.py
findings:
  critical: 0
  warning: 3
  info: 3
  total: 6
status: issues_found
---

# Phase 82: Code Review Report

**Reviewed:** 2026-07-10
**Depth:** standard
**Files Reviewed:** 14
**Status:** issues_found

## Summary

Phase 82 cuts the three enrich pending sets (metadata / fingerprint / analyze) over from
`FileRecord.state` to the derived SQL layer (`eligible_clause`), extends `get_stage_progress`'s three
enrich nodes to four-bucket counts via `GROUP BY stage_status_case(stage)`, removes the linear
`get_pipeline_stats` (re-expressed as `_derive_stats` off `get_stage_progress`), and adds a
standalone perf-measurement harness (`seed_perf_corpus.py` / `perf_explain.py` + justfile recipes).

I focused adversarial attention on the four items called out in the task, and traced each:

- **`eligible_clause` vs `eligible()` drift** — the SQL twin (`stage_status.py:231-273`) mirrors the
  Python truth exactly: `~inflight ∧ ~done`, plus a table-driven `~failed` conjunct for analyze via
  `ELIGIBLE_AFTER_FAILURE`. The load-bearing analyze carve-out (ELIG-03, the 44.5K over-enqueue
  guard) is present and locked by the `(ANALYZE, seed_analysis_failed, False)` cell in
  `test_stage_status_equivalence.py`. No drift.
- **A1 cloud double-dispatch guard** — `~exists(cloud_job in _ACTIVE_CLOUD_STATUSES)`
  (`pipeline.py:1179`) correctly excludes every non-`FAILED` cloud status; `FAILED` is deliberately
  re-admittable. The status list matches the test copy and is parametrized across all six active
  statuses. Correct.
- **Four-bucket `stage_status_case` GROUP BY** — the materialized-subquery form
  (`_safe_bucket_counts`, `pipeline.py:347-348`) correctly sidesteps the Postgres "ungrouped column"
  GroupingError by projecting the per-row label first, then grouping the scalar. `Status` enum values
  align with the hardcoded `["done"]` key access in `_derive_stats`. Sum-to-total invariant tested.
- **`_derive_stats` re-expression + router callers** — the seven keys map to the correct
  `get_stage_progress` outputs; both callers (`build_dashboard_context`, `pipeline_stats_partial`)
  pass the single `get_stage_progress` read through to `_build_dag_context`, avoiding a double read.
  No dangling `get_pipeline_stats` references survive.

The derived SQL is correct and comprehensively drift-locked. **No correctness blocker was found.**
Findings are limited to maintainability, a weaker-than-claimed destructive-op guard, and a shell-
quoting defect in dev-only justfile recipes.

## Warnings

### WR-01: `PIPELINE_STAGES` is now dead production code

**File:** `src/phaze/services/pipeline.py:71-80`
**Issue:** `get_pipeline_stats` was the only production consumer of `PIPELINE_STAGES`, and it was
removed this phase. Grep confirms the constant is now referenced only by its own definition, a
docstring comment (`pipeline.py:1130`), and a single test assertion (`test_pipeline.py:146` — "not in
PIPELINE_STAGES"). The retention comment (`pipeline.py:90-92`) claims it is "still consumed by the
ANALYSIS_FAILED-bucket invariant test + the `get_analysis_failed_count` docstring," which is
literally true but overstates it: no runtime code path reads it. It is a dead constant kept alive by
a test that asserts a property about it.
**Fix:** Either delete `PIPELINE_STAGES` and rewrite `test_pipeline.py:146` to assert the invariant
directly (e.g. against the enum members it cares about), or leave it but downgrade the retention
comment to state plainly that it is test-only. Preferred:
```python
# test_pipeline.py — assert the invariant without resurrecting a dead constant
LINEAR_STAGES = {FileState.DISCOVERED, FileState.METADATA_EXTRACTED, FileState.FINGERPRINTED,
                 FileState.ANALYZED, FileState.PROPOSAL_GENERATED, FileState.APPROVED,
                 FileState.DUPLICATE_RESOLVED, FileState.EXECUTED}
assert FileState.ANALYSIS_FAILED not in LINEAR_STAGES
```

### WR-02: `--reseed` destructive guard uses a substring match, weaker than the `*_test` suffix guard it claims to mirror

**File:** `scripts/seed_perf_corpus.py:167-169`
**Issue:** The `--reseed` path runs `TRUNCATE ... RESTART IDENTITY CASCADE` and is gated by
`if "perf" not in str(db_name)`. The module docstring (line 17) states this "mirrors the `*_test`
destructive-DB guard," but the integration tests use a strict SUFFIX check
(`_TARGET_DB.endswith("_test")`, e.g. `test_enrich_pending_independence.py:80`). A substring check is
strictly weaker: any operator-supplied `--dsn` whose database name merely *contains* `perf`
(`performance_prod`, `imperfect_main`, `superperf`) passes the guard and gets TRUNCATEd. Because the
`--dsn` is fully operator-controlled and the operation is irreversible data loss, the guard should be
as strict as the one it claims to copy.
**Fix:** Match on a suffix (or exact name), not a substring:
```python
if not str(db_name).endswith("_perf") and "perf" not in str(db_name).split("_"):
    raise SystemExit(f"--reseed refused: {db_name!r} is not a perf DB ...")
# or simplest: require the exact expected perf DB name (perf_db_name / phaze_perf82).
```

### WR-03: justfile `perf-seed` / `perf-explain` interpolate `{{N}}` / `{{ITER}}` unquoted into the shell command

**File:** `justfile:527-534`
**Issue:** `perf-seed N='200000'` emits `... --n {{N}} --dsn "..." --reseed` and `perf-explain
ITER='20'` emits `... --iterations {{ITER}}`. The `{{N}}` / `{{ITER}}` tokens are interpolated
unquoted into a recipe line that `sh` executes, so a value containing whitespace or shell
metacharacters splits into extra args or injects commands (`just perf-seed "1 --reseed --dsn <prod>"`
or worse). This is a dev-only convenience recipe with numeric defaults, so severity is low, but the
same recipe pairs an unquoted parameter with a destructive `--reseed` flag (see WR-02), which is
exactly where arg-splitting is most dangerous.
**Fix:** Quote the interpolations and/or validate they are integers:
```make
perf-seed N='200000':
    PHAZE_DATABASE_URL="{{perf_db_sa_dsn}}" uv run alembic upgrade head
    uv run python scripts/seed_perf_corpus.py --n "{{N}}" --dsn "{{perf_db_dsn}}" --reseed
```

## Info

### IN-01: `_DONE_FP` is duplicated across the two twin modules

**File:** `src/phaze/services/stage_status.py:88` and `src/phaze/enums/stage.py:56`
**Issue:** The fingerprint done-vocabulary is defined twice — `frozenset({"success", "completed"})`
in the Python twin and `("success", "completed")` in the SQL twin. They currently agree, and the
DERIV-04 equivalence test would catch a divergence, but two independent definitions of the same
domain constant are a latent drift surface that does not need to exist.
**Fix:** Define it once (e.g. export the `enums.stage._DONE_FP` frozenset) and import it in
`stage_status.py`; `.in_(...)` accepts a frozenset.

### IN-02: The "Discovered" stats card silently changed to count ALL files (including non-media)

**File:** `src/phaze/routers/pipeline.py:159` (`discovered → discovery.done`), rendered at
`src/phaze/templates/pipeline/partials/stats_bar.html:11`
**Issue:** `discovery.done` is `COUNT(FileRecord.id)` over ALL rows (`pipeline.py:401`), including
non-media `txt`/etc. types, whereas the sibling enrich cards (Fingerprinted / Analyzed) are
music/video-scoped via `_safe_bucket_counts`. Post-cutover the "Discovered" card therefore counts a
different population than the cards beside it. The code carefully documents the `metadata_extracted`
semantic shift (`stats_bar.html:6-8`, `pipeline.py:150-152`) but does NOT call out that `discovered`
now includes non-media files — an operator comparing the top-row numbers may read the mismatch as a
bug.
**Fix:** No code change strictly required (the mapping is defensible — "discovery" == ingested), but
add a one-line note in `_derive_stats` / `stats_bar.html` that `discovered` is the all-files count
(non-media included) so it is not read as inconsistent with the music/video-scoped enrich cards.

### IN-03: `get_proposal_pending_batches` still couples to `FileRecord.state`, against the phase's own thesis

**File:** `src/phaze/services/pipeline.py:1536`
**Issue:** The phase thesis is that `FileRecord.state` is single-valued and cannot represent parallel
stage completion, so the enrich pending sets were cut over to derived predicates. The proposal
pending set is explicitly OUT of this phase's scope (it is not one of the three enrich sets), but it
still gates on `.where(FileRecord.state.in_([ANALYZED, METADATA_EXTRACTED]))` in addition to the
metadata + completed-analysis `exists()` conjuncts. This is not a bug today (a completed-analysis
file lands in `ANALYZED` state under the linear writer), but it carries the same latent
parallel-stage divergence the phase set out to dissolve elsewhere — a file that satisfies the
convergence `exists()` gates but whose `state` sits outside `{ANALYZED, METADATA_EXTRACTED}` would be
silently excluded.
**Fix:** Track as a follow-up: the `state.in_(...)` conjunct is redundant with (and strictly
narrower than) the two `exists()` convergence gates below it and can be dropped, letting the
convergence predicate alone define membership — consistent with the enrich cutover.

---

_Reviewed: 2026-07-10_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
