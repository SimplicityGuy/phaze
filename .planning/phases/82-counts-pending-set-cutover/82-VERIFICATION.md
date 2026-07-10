---
phase: 82-counts-pending-set-cutover
verified: 2026-07-10T23:00:00Z
status: passed
score: 3/3 roadmap success criteria verified (12/12 plan-level must-haves verified)
overrides_applied: 0
---

# Phase 82: Counts & Pending-Set Cutover — Verification Report

**Phase Goal:** Rewrite the three enrich pending sets and `get_pipeline_stats` off `stage_status`, so
metadata/fingerprint/analyze each surface every not-done, not-in-flight file independent of the others —
the cross-stage deadlock dissolves — and measure the 5s poll at 200K-file scale.

**Verified:** 2026-07-10
**Status:** passed
**Re-verification:** No — initial verification

This section is the goal-backward verifier's independent verdict, added on top of the plan-04-authored
PERF-02 measurement below (preserved verbatim — see "PERF-02 Measurement" further down). Nothing below the
`---` divider was written or altered by this verification pass.

## Goal Achievement

### Observable Truths (Roadmap Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A single file can complete all three enrich stages in **any order** — each enrich pending set is derived from `stage_status` with no upstream and no `FileRecord.state` read, proven by a test running the three stages in every ordering. | ✓ VERIFIED | `eligible_clause(stage)` (`src/phaze/services/stage_status.py:231`) drift-locked to the Python `eligible()` via a 14-cell `ELIGIBLE_CASES` matrix. All three pending helpers (`get_metadata_pending_files`, `get_fingerprint_pending_files`, `get_discovered_files_with_duration`) compose `eligible_clause(stage) ∧ ~dedup_resolved_clause() ∧ file_type ∈ MUSIC_VIDEO_TYPES` — zero `FileRecord.state` reads (confirmed by an AST source-scan, mutation-tested live, see below). `test_enrich_pending_sets_are_independent` (parametrized over all 6 `permutations` of {metadata, fingerprint, analyze}) plus two deadlock-detection cells (`test_metadata_done_state_advanced_still_in_analyze_set`, `test_analyzed_but_unfingerprinted_still_in_fingerprint_set`) — re-ran live, **88 passed** (see Test Execution below). |
| 2 | `get_pipeline_stats` reports per-stage counts from output tables (linear `GROUP BY state` removed) and the DAG shows four-bucket per-stage counts (`not_started`/`in_flight`/`done`/`failed`) that sum to total, including a visible failed count per enrich stage. | ✓ VERIFIED | `grep -rn "get_pipeline_stats\|group_by(FileRecord.state)" src/phaze/` returns only historical doc-comments (no `def`/import/call). `_safe_bucket_counts` (`pipeline.py:323`) + the four-bucket enrich nodes in `get_stage_progress` (`pipeline.py:362`), reusing the LOCKED `stage_status_case`. `test_enrich_nodes_are_four_bucket_summing_to_total` and `test_seeded_failed_rows_are_visible_per_stage` pass live. Three callers (`_build_dag_context`, `build_dashboard_context`, `pipeline_stats_partial`) + `stats_bar.html` re-express the seven former keys via `_derive_stats`, keeping the Alpine OOB store keys stable. |
| 3 | The `/pipeline/stats` poll latency at 200K-file scale is measured and recorded in the phase VERIFICATION; no denormalized status column is added unless that measurement proves the derived query too slow (YAGNI is the default). | ✓ VERIFIED (deliverable produced; over-budget finding surfaced, not a phase failure — see reasoning below) | `82-VERIFICATION.md` (this file, section "PERF-02" below) records real `EXPLAIN (ANALYZE, BUFFERS)` plans on a dedicated 200K synthetic corpus at migration HEAD (036), endpoint/direct p50/p95 timings, per-query 032-partial-index evidence, and an explicit DENORM-01 NO-GO/deferred decision with reasoning. The measurement was produced honestly (including an honest "3 of 5 named indexes chosen" finding, not fabricated). |

**Score:** 3/3 roadmap success criteria verified.

### PERF-02 judgment call — over-budget number is a follow-up, not a phase failure

The measured `/pipeline/stats` p50 (~1.4s endpoint / ~1.29s `get_stage_progress` direct) is **over** the D-07
`< ~1s` soft budget. PERF-02's contractual deliverable, per REQUIREMENTS.md and the roadmap SC text, is *"the
poll latency… is measured and recorded… no denormalized status column is added unless that measurement proves
the derived query too slow"* — i.e., the deliverable is the measurement + a licensed decision, not a specific
latency number. Plan 82-04 did not silently accept the overage: it root-caused it (the three enrich
four-bucket reads run **sequentially** inside `get_stage_progress`, ~1.15s combined), verified that
`work_mem`/HashAggregate tuning does not help (the cost is per-row correlated `EXISTS` SubPlans, not a sort
spill), and made a reasoned NO-GO/deferred call on DENORM-01 — recommending `asyncio.gather`-based
parallelization of the three bucket reads (projected ≈0.5s) as the cheaper, correctness-neutral fix, to be
re-measured before DENORM-01 is pulled forward. This is exactly the engineering judgment PERF-02 exists to
produce (YAGNI: derive → measure → decide, not denormalize speculatively), and the decision explicitly says it
is "flagged for operator review" rather than a silently-accepted regression.

**Verdict:** Truth #3 is VERIFIED as a measurement-and-decision deliverable. The over-budget number and the
recommended parallelization fix are surfaced here as a **tracked follow-up**, not a phase gap:

- **Follow-up (non-blocking):** Parallelize the three `_safe_bucket_counts` reads in `get_stage_progress` via
  `asyncio.gather` on independent sessions and re-measure against the `< ~1s` budget at 200K before
  considering DENORM-01. (Source: `82-04-SUMMARY.md` / this file's "DENORM-01 go / no-go decision" section.)
- The current 5s degrade-safe poll interval absorbs the measured ~1.4s comfortably; no user-facing regression
  today.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/stage_status.py` | `eligible_clause(stage)` — SQL twin of `eligible()`, enrich-only | ✓ VERIFIED | `def eligible_clause` at line 231; table-driven off `ELIGIBLE_AFTER_FAILURE`; docstring documents the correlated-`~exists` join contract. |
| `tests/integration/test_stage_status_equivalence.py` | `ELIGIBLE_CASES` (14 cells) + `test_eligible_sql_equals_python` | ✓ VERIFIED | Present; re-ran live, all 15 `eligible`-keyed tests pass (14 matrix cells + guard). |
| `src/phaze/services/pipeline.py` | Three pending helpers cut over; `_safe_bucket_counts`; `get_pipeline_stats` deleted | ✓ VERIFIED | `get_metadata_pending_files` (:1441), `get_fingerprint_pending_files` (:1480), `get_discovered_files_with_duration` (:1151) all compose `eligible_clause`; `_safe_bucket_counts` (:323); no `def get_pipeline_stats` anywhere. |
| `tests/integration/test_enrich_pending_independence.py` | SC#1 all-orderings + A1 cloud-exclusion | ✓ VERIFIED | 7 tests incl. `test_enrich_pending_sets_are_independent` (parametrized ×6), `test_cloud_dispatched_file_absent_from_analyze_set` (parametrized over awaiting/uploading/submitted-style active statuses). |
| `tests/integration/test_pending_set_divergence.py` | Behavioral state/derived-disagreement guard | ✓ VERIFIED | 5 tests, each with a `MUTATION:` comment; live-verified one cell inverts under a hand-mutation (below). |
| `tests/shared/test_pending_set_source_scan.py` | Mutation-tested AST source scan — zero `FileState` reads | ✓ VERIFIED | 7 tests; live mutation-tested by this verifier (re-introduced a `FileState.DISCOVERED` read into `get_metadata_pending_files` → guard went RED; reverted → GREEN). |
| `src/phaze/routers/pipeline.py` | `notYetEnriched`/`build_dashboard_context`/`pipeline_stats_partial` re-expressed off `stage_progress` | ✓ VERIFIED | `_derive_stats` (:137) re-expresses the seven keys; `_build_dag_context` threads a single `get_stage_progress` read. |
| `src/phaze/templates/pipeline/partials/stats_bar.html` | Key remap; Alpine `$store.pipeline.*` keys stable | ✓ VERIFIED | Documenting comment added; six cards + three OOB `x-init` writes unchanged in key names. |
| `tests/integration/test_stage_progress_buckets.py` | Four-bucket-sums-to-total + visible failed count | ✓ VERIFIED | 4 tests; re-ran live, pass. |
| `tests/shared/services/test_pipeline.py` | Reconciled to derived semantics (stale tests deleted, `get_pipeline_stats` gone) | ✓ VERIFIED | `grep -c "get_pipeline_stats"` → 0; full file passes live (183 combined with router file). |
| `scripts/seed_perf_corpus.py` | Idempotent parameterized ~200K corpus seeder | ✓ VERIFIED | Present; `uv run ruff check` clean; referenced/exercised by `82-04-SUMMARY.md`. |
| `justfile` | `perf-seed`/`perf-explain` recipes (db group) | ✓ VERIFIED | `just perf-db-up/-down`, `perf-seed`, `perf-explain` present (flagged with a minor unquoted-interpolation warning in `82-REVIEW.md` WR-03, non-blocking). |
| `.planning/phases/82-counts-pending-set-cutover/82-VERIFICATION.md` | PERF-02 numbers + index-scan evidence + DENORM-01 decision | ✓ VERIFIED | This file (preserved section below). |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| Three pending helpers | `eligible_clause` / `dedup_resolved_clause` | `eligible_clause(Stage.{METADATA,FINGERPRINT,ANALYZE}) ∧ ~dedup_resolved_clause()` | ✓ WIRED | Confirmed by direct read of `pipeline.py:1441-1500,1151-1183`; matches the plan's `key_links` pattern exactly. |
| `get_discovered_files_with_duration` | cloud_job active-status exclusion | `~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status.in_(_ACTIVE_CLOUD_STATUSES)))` | ✓ WIRED | `pipeline.py:1179`; `_ACTIVE_CLOUD_STATUSES` defined :60; the A1 double-dispatch landmine is closed with a traced (not assumed) finding recorded in `82-02-SUMMARY.md`. |
| `get_stage_progress` enrich nodes | `stage_status_case(stage)` | One `GROUP BY` per enrich stage via a materialized inner subquery (Postgres `GroupingError` workaround) | ✓ WIRED | `pipeline.py:347-348`; reuses the LOCKED CASE ladder verbatim (no fresh `case(` in the function body). |
| `routers/pipeline.py` callers | `get_stage_progress` | `_derive_stats` re-expression, single-read threaded into `_build_dag_context` | ✓ WIRED | `routers/pipeline.py:137-164, 275-276`; no double heavy read on the poll path. |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| READ-01 | 82-01, 82-02 | Three enrich pending sets derived from `stage_status`, independent, no `FileRecord.state` read | ✓ SATISFIED | `eligible_clause` + three cut-over helpers + independence/divergence/source-scan tests, all live-verified passing. |
| READ-02 | 82-03 | `get_pipeline_stats` removed; DAG shows four-bucket per-stage counts summing to total with visible failed | ✓ SATISFIED | `get_pipeline_stats` deleted; `_safe_bucket_counts` four-bucket nodes; sum-to-total + failed-visibility tests live-verified passing. |
| PERF-02 | 82-04 | `/pipeline/stats` poll latency at 200K measured and recorded; DENORM-01 gated on the measurement | ✓ SATISFIED | Measurement + DENORM-01 decision recorded in this file (below), honest about the over-budget finding and index-usage partiality. |

No orphaned requirements: `REQUIREMENTS.md` maps exactly READ-01, READ-02, PERF-02 to Phase 82, and all three appear in a plan's `requirements:` frontmatter. (Note: `REQUIREMENTS.md`'s own checkbox/status column still shows these as "Pending" — this is a document-maintenance step, not evidence of incompleteness; the underlying code and tests are green as demonstrated above.)

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | `grep -n -E "TBD\|FIXME\|XXX"` across all phase-touched files | none found | — | No debt markers in any file this phase modified. |

`82-REVIEW.md` (already produced separately) found 3 warnings (dead `PIPELINE_STAGES` constant, a
substring-based `--reseed` destructive-op guard weaker than the suffix guard it claims to mirror in
`scripts/seed_perf_corpus.py`, and unquoted `{{N}}`/`{{ITER}}` justfile interpolation) and 3 info items. None
are must-have blockers — they are maintainability/hardening items in a **dev-only local perf-measurement
script and justfile recipes**, not in the production read path. Recorded here for traceability; not
re-litigated (see `82-REVIEW.md` for full detail and suggested fixes).

### Behavioral Spot-Checks / Mutation Verification (run live by this verifier)

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| ELIG-03 guard has teeth (drop the analyze `~failed_clause` conjunct) | Mutated `eligible_clause` to skip the analyze failed-conjunct, re-ran `pytest -k eligible` | `test_eligible_sql_equals_python[analyze-seed_analysis_failed-False]` went RED (`assert True == False`); reverted → GREEN (15 passed) | ✓ PASS |
| AST source-scan guard has teeth (reintroduce a `FileState` read) | Injected `FileRecord.state != FileState.DISCOVERED` into `get_metadata_pending_files`, re-ran `test_pending_helpers_have_zero_filestate_reads` | Guard went RED (`get_metadata_pending_files reads FileState.DISCOVERED at line 1455`); reverted → GREEN | ✓ PASS |
| SC#1 all-orderings + deadlock cells | `pytest tests/integration/test_enrich_pending_independence.py` against a private `*_test`-suffixed DB (own container, own DSN) | 7 passed | ✓ PASS |
| Four-bucket sum-to-total + visible failed | `pytest tests/integration/test_stage_progress_buckets.py` | 4 passed | ✓ PASS |
| get_pipeline_stats fully removed | `grep -rn "get_pipeline_stats" src/phaze/` (def/import/call only, not comments) | Only historical doc-comments, no executable reference | ✓ PASS |
| Full regression: DERIV-04 equivalence + independence + divergence + source-scan + four-bucket + stats-caller | `pytest tests/integration/test_stage_status_equivalence.py tests/integration/test_enrich_pending_independence.py tests/integration/test_pending_set_divergence.py tests/shared/test_pending_set_source_scan.py tests/integration/test_stage_progress_buckets.py tests/shared/routers/test_pipeline_stats.py -q` | **88 passed**, 0 skipped (private `phaze_verify82_test` DB on the shared `:5433` container, isolated from other sessions) | ✓ PASS |
| Pre-existing test files reconciled and green | `pytest tests/shared/services/test_pipeline.py tests/shared/routers/test_pipeline.py -q` | **183 passed** | ✓ PASS |
| Downstream consumers unaffected (fingerprint router, recovery, and the Phase-83-reconciled analyze stage-progress tests) | `pytest tests/fingerprint/routers/test_pipeline_fingerprint.py tests/analyze/tasks/test_recovery.py tests/analyze/core/test_stage_progress.py -q` | **66 passed** — confirms the commit `1622239e` reconciliation (old drifted analyze-done semantics → canonical `done_clause`) is correct and does not regress recovery/fingerprint consumers | ✓ PASS |
| Type/lint clean on touched production files | `uv run mypy src/phaze/services/pipeline.py src/phaze/services/stage_status.py src/phaze/routers/pipeline.py scripts/seed_perf_corpus.py scripts/perf_explain.py` + `uv run ruff check` (same set) | mypy: Success, no issues in 5 source files; ruff: All checks passed | ✓ PASS |

### Test Execution (Test Environment)

Ran against a **private, dedicated** database (`phaze_verify82_test`, suffix `_test` required by the
harness's own destructive-op guard) on the shared `phaze-test-db` container (port 5433), per the
test-environment contract. `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_QUEUE_URL` /
`PHAZE_REDIS_URL` all pointed at the private DB — no silent-skip false pass. Database dropped after
verification; the shared `phaze-test-db` container and any other session's data were never touched.

### Human Verification Required

None. All must-haves are verifiable via automated tests + direct source/grep inspection + live mutation
checks; no visual/UX/real-time/external-service behavior in this phase's scope requires human testing. The
DENORM-01 over-budget finding is a recorded engineering decision (not an open verification question) —
surfaced above as a tracked, non-blocking follow-up for operator awareness, per this phase's explicit
verification instructions.

### Gaps Summary

No gaps. All three roadmap success criteria (READ-01, READ-02, PERF-02) and all twelve plan-level
must-haves across 82-01 through 82-04 are verified against the live codebase and a live test run, not
merely against SUMMARY.md claims — including two independent mutation checks (ELIG-03 conjunct removal,
FileState-read reintroduction) performed by this verifier from scratch to confirm the anti-drift guards
have real teeth, not just passing assertions. The one open item — parallelizing the three sequential
four-bucket reads to bring `/pipeline/stats` under the D-07 `< ~1s` soft budget — is a recorded,
non-blocking follow-up licensed by the PERF-02 measurement itself, not a phase-goal failure.

---

_Verified: 2026-07-10_
_Verifier: Claude (gsd-verifier)_

---

# Phase 82 — VERIFICATION

Recorded manual/measured verifications for the Counts & Pending-Set Cutover phase. This file holds the
**PERF-02** measurement (the deliverable that licenses the DENORM-01 decision) and the **D-02** deploy-target
gate that must be re-checked at homelab rollout.

---

## PERF-02 — `/pipeline/stats` poll latency at 200K (the DENORM-01 licensing measurement)

**Requirement:** PERF-02 (D-06, D-07). Prove the derived read queries stay fast at scale — or surface that
they do not — on a corpus that carries the migration-032 partial indexes the anti-joins ride, and make the
DENORM-01 go/no-go call against the `< ~1s` budget (D-07).

### Measurement provenance (Pitfall 5 / D-06 — READ THIS FIRST)

The numbers below were taken on a **LOCAL, dedicated, synthetic corpus at migration HEAD (036)**, NOT against
live lux. This is mandatory: prod is at Alembic ~031 and **lacks** the 032 partial indexes, so a live EXPLAIN
would show pessimistic Seq Scans and misjudge the plan (D-06). The measurement is only valid where the
indexes exist.

- **DB:** dedicated ephemeral `postgres:18-alpine` container (`just perf-db-up`, host port 5545, DB
  `phaze_perf82`) — a SEPARATE container from the shared `phaze-test-db` so a concurrent `just test-db`
  recreate cannot wipe the corpus mid-measurement (observed: the shared container was recreated by a sibling
  session and dropped an earlier perf DB — hence the dedicated container).
- **Migration head:** `036` (confirmed `alembic current` → `036 (head)`; ≥036 satisfies D-02's index +
  backfill precondition).
- **Host:** Apple-Silicon dev host under colima. Absolute milliseconds are host-relative; the transferable
  finding is the RELATIVE story (which query shape dominates, and that it is sequential).
- **Harness:** `scripts/seed_perf_corpus.py` (seed) + `scripts/perf_explain.py` (EXPLAIN + timing), driven by
  `just perf-seed` / `just perf-explain`. Warm-up iteration excluded; 20 timed iterations; single connection.

### Seed parameters (D-06 selectivity profile, N=200,000)

`just perf-seed 200000` → measured row counts on the seeded DB:

| Table / predicate | Rows | Share of N |
|---|---:|---:|
| `files` total | 200,000 | 100% |
| `files` music/video (in `MUSIC_VIDEO_TYPES`) | 194,000 | 97% |
| `files` non-media (`txt`, exercises the file_type scope) | 6,000 | 3% |
| `metadata` rows | 140,000 | 70% |
| `metadata.failed_at` set (metadata FAILED bucket) | 4,000 | 2% |
| `fingerprint_results` status=`success` | 110,000 | 55% |
| `fingerprint_results` status=`failed` (failure-only) | 4,000 | 2% |
| `analysis.analysis_completed_at` set (DONE) | 80,000 | 40% |
| `analysis.failed_at` set (terminal, XOR-disjoint from DONE) | 10,000 | 5% |
| `cloud_job` rows (cycling active statuses) | 2,000 | 1% |
| `cloud_job` status=`awaiting` | 286 | ~0.14% |
| `dedup_resolution` markers | 4,000 | 2% |
| `scheduling_ledger` in-flight rows (process_file / extract_file_metadata / fingerprint_file) | 5,000 | 2.5% |
| `saq_jobs` provisioned (idle, for the endpoint queue-activity reads) | table present | — |

`FileRecord.state` was stamped to the furthest reached stage (dual-write realism). Every insert used
parameterized `unnest`-array bulk INSERTs (T-82-07 — no f-string SQL); `--reseed` is hard-gated to a
perf-named DB.

### Headline timings (authoritative — direct, warm, 20 iterations)

| Measurement | p50 | p95 | min | max | vs `< ~1s` budget (D-07) |
|---|---:|---:|---:|---:|:--|
| `get_stage_progress()` DIRECT (the DENORM-relevant DB core) | **1290.9 ms** | 1419.2 ms | 1130.7 ms | 1549.8 ms | **OVER** |
| `GET /pipeline/stats` full endpoint (ASGI, real route) | **1405.3 ms** | 1489.1 ms | 1322.1 ms | 1533.7 ms | **OVER** |

The endpoint is ~115 ms above `get_stage_progress` alone — the remaining per-node counts (awaiting / straggler /
failed / pushing / pushed / inadmissible / cloud-phase / lanes), the queue-activity reads, and the template
render. **`get_stage_progress` dominates**, and within it the three enrich four-bucket reads dominate (below).

> **Both numbers exceed the `< ~1s` D-07 budget at 200K.** This is recorded as measured — not smoothed.

### Per-query EXPLAIN (ANALYZE, BUFFERS) @ 200,000 files

Each shape was rebuilt from the REAL clause builders (`eligible_clause` / `dedup_resolved_clause` /
`stage_status_case`) and compiled to literal-bound SQL, so the plan is the ACTUAL query the app issues.

| Query shape | Exec time (ANALYZE, w/ per-node timing) | Rows out | 032 partial index used? |
|---|---:|---:|:--|
| `get_metadata_pending_files` | 103.97 ms | 53,550 | No — non-selective done anti-join → Parallel Hash Anti Join |
| `get_fingerprint_pending_files` | 202.64 ms | 79,333 | No — non-selective done anti-join → Hash Anti Join |
| `get_discovered_files_with_duration` (analyze pending + LEFT JOIN + cloud-exclusion) | 239.30 ms | 97,465 | **Yes — `ix_analysis_failed`** (Bitmap Index Scan on the ~failed(ANALYZE) branch) |
| `four_bucket[metadata]` (GROUP BY `stage_status_case`) | 726.34 ms (TIMING OFF ≈ 363–397 ms) | 4 | **Yes — `ix_metadata_failed`** (Bitmap Index Scan, failed branch); Seq Scan on files for the CASE |
| `four_bucket[fingerprint]` | 433.54 ms (TIMING OFF ≈ 441 ms) | 4 | No — success predicate (110K/114K) non-selective → Seq Scans on `fingerprint_results` |
| `four_bucket[analyze]` | 401.01 ms (TIMING OFF ≈ 339 ms) | 4 | **Yes — `ix_analysis_failed`** (Bitmap Index Scan, failed branch); Seq Scan on `analysis` for DONE |

Verbatim evidence — a 032 partial index IS chosen where selective (the failure-marker anti-joins):

```
-- get_discovered_files_with_duration (Execution Time: 239.302 ms)
Parallel Hash Right Anti Join  (actual time=119.062..135.034 rows=51566.50 loops=2)
  ->  Parallel Bitmap Heap Scan on analysis analysis_1  (actual time=0.587..1.759 rows=5000.00 loops=2)
        ->  Bitmap Index Scan on ix_analysis_failed  (actual time=0.432..0.432 rows=10000.00 loops=1)

-- four_bucket[analyze] (Execution Time: 401.013 ms)
GroupAggregate  (actual time=380.874..399.189 rows=4.00 loops=1)
  ->  Sort  (actual time=368.277..383.035 rows=194000.00 loops=1)   -- 194K-row sort dominates
        Sort Method: external merge  Disk: 3024kB
        ...  ->  Bitmap Index Scan on ix_analysis_failed  (actual time=0.417..0.417 rows=10000.00 loops=1)
```

### Index-scan finding (honest reading of the must_have)

- **`ix_analysis_failed`** — CONFIRMED used (Bitmap Index Scan) in TWO shapes (`get_discovered_files_with_duration`
  and `four_bucket[analyze]`), on the selective `~failed(ANALYZE)` branch (10K/90K analysis rows).
- **`ix_metadata_failed`** — CONFIRMED used (Bitmap Index Scan) in `four_bucket[metadata]`, on the failed branch
  (4K metadata rows).
- **`ix_fprint_success`**, **`ix_analysis_completed`** — NOT chosen. Their predicates are the *positive*
  done-clauses covering 40–57% of rows at this corpus; a partial index over that many rows is non-selective,
  so Postgres correctly prefers a Seq/Hash scan. This is optimal planning, **not** a regression.
- **`ix_cloud_job_awaiting`** — NOT chosen. `cloud_job` is a 2K-row table (Seq Scan cost 61), and the analyze
  cutover's exclusion tests the broader `_ACTIVE_CLOUD_STATUSES` set, not the `awaiting`-only predicate the
  index covers.

So the must_have's literal expectation ("Index/Index-Only Scan, not Seq Scan on all five partial indexes across
the three pending queries + the four-bucket query") is **partially met and recorded honestly**: 3 of the 5
named partial indexes are confirmed scanned on their selective failure-marker branches; the other two plus
`ix_cloud_job_awaiting` are correctly NOT chosen because their predicates are non-selective (or the target
table is trivially small) at this corpus, where a Seq/Hash scan is the optimal plan. Critically, **no query
fell back to a pathological nested-loop** — every plan is a hash/bitmap/parallel plan.

### DENORM-01 go / no-go decision

**Decision: NO-GO — DENORM-01 (the denormalized stage-bitmap column) stays DEFERRED for now. Flagged for
operator review.**

Reasoning, licensed by the measured numbers:

1. **The number is over budget** (~1.4 s endpoint p50 vs the `< ~1s` D-07 budget), so this is NOT a clean
   "within budget, YAGNI" defer. The naive gate trigger ("over budget → pull DENORM-01 forward") IS met and
   is recorded as such.
2. **But the overage has a cheaper remedy than the denormalized column.** `get_stage_progress` runs the three
   enrich four-bucket reads **SEQUENTIALLY** (~340–440 ms each ⇒ ~1.15 s combined); that sequential chain is
   the dominant cost. The cheapest lever is to run the three `_safe_bucket_counts` reads **CONCURRENTLY**
   (`asyncio.gather` on independent sessions), projected to collapse ~1.15 s → the slowest single query
   (~0.44 s) and bring the endpoint under budget — a small, correctness-neutral change that avoids DENORM-01's
   dual-write correctness/drift burden entirely. (This is a projection, not a measurement.)
3. **`work_mem` / HashAggregate tuning does NOT help** (verified read-only): even at `work_mem=256MB` the
   planner keeps Sort + GroupAggregate for the four-bucket query — the dominant cost is the per-row correlated
   `EXISTS` SubPlan probes over the full 194K corpus (inherent to the derived CASE), not the sort spill. So the
   only remaining levers are (a) parallelize the three reads, or (b) DENORM-01.

**Recommended sequence:** parallelize the three enrich four-bucket reads (a Phase-83+ follow-up) and
re-measure; pull **DENORM-01** forward ONLY if concurrent reads + targeted query tuning still miss the `~1s`
budget at 200K. Until then the 5 s degrade-safe poll (never 500s; each read rides the `_safe_count` /
`_safe_bucket_counts` degrade discipline) absorbs the current ~1.4 s comfortably.

### Reproduce

```bash
just perf-db-up                        # dedicated PG on :5545 (isolated from the shared test-db)
just perf-seed 200000                  # migrate to HEAD (>=036) + seed the D-06 corpus
just perf-explain 20                   # EXPLAIN ANALYZE + get_stage_progress + /pipeline/stats p50/p95
just perf-db-down                      # tear the dedicated container down
```

---

## D-02 — analyze pending-set flip deploy-target gate (DEPLOYMENT-GATED — re-run at homelab rollout)

**Status: NOT satisfiable from the executor; must be re-run READ-ONLY against lux at homelab rollout.**

The analyze pending-set flip (`get_discovered_files_with_duration` cutover, Plan 82-02) is trusted in prod
ONLY once the deploy target is BOTH:

1. at **Alembic ≥036** (so the 032 partial indexes exist AND Phase 80's `036` has backfilled
   `analysis.analysis_completed_at` for the analyzed corpus, READ-03/D-13), AND
2. `COUNT(files WHERE state = 'analyzed' AND analysis_completed_at IS NULL AND failed_at IS NULL) = 0`
   (no analyzed file would be re-admitted to the analyze pending set by the derived reader).

Current known state (project memory, NOT re-probed here): **prod is at ~031** — it lacks 032 and the 036
backfill, so this gate is NOT yet green in prod. Per D-06 / Pitfall 5 the executor deliberately does NOT probe
live lux for this plan (a ~031 prod's EXPLAIN would be invalid anyway, and the read is a rollout-time check per
the 82-VALIDATION Manual-Only table). At homelab rollout, after deploying ≥036, re-run read-only against lux:

```sql
-- BEGIN TRANSACTION READ ONLY;  (read-only probe recipe: ssh datum@lux.lan, direct :5432, DB phaze)
SELECT count(*) AS analyzed_null_completed
FROM files
WHERE state = 'analyzed' AND analysis_completed_at IS NULL AND failed_at IS NULL;   -- MUST be 0
SELECT version_num FROM alembic_version;                                             -- MUST be >= 036
```

Both conditions must hold before the analyze pending-set cutover is trusted live.
