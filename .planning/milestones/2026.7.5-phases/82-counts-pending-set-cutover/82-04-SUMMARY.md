---
phase: 82-counts-pending-set-cutover
plan: 04
subsystem: perf-measurement
tags: [perf, explain-analyze, pipeline-stats, four-bucket, denorm-decision, seed-harness]
requires:
  - "phaze.services.pipeline.get_stage_progress / _safe_bucket_counts (Plan 82-03 — the four-bucket derived stats path being measured)"
  - "phaze.services.stage_status.{eligible_clause,dedup_resolved_clause,stage_status_case} (Phase 78 / Plan 82-01 — the LOCKED clause builders)"
  - "migration 032 partial indexes (ix_fprint_success / ix_analysis_completed / ix_analysis_failed / ix_metadata_failed / ix_cloud_job_awaiting) + 036 backfill"
provides:
  - "scripts/seed_perf_corpus.py — idempotent parameterized ~200K synthetic corpus seeder (D-06 selectivity profile)"
  - "scripts/perf_explain.py — EXPLAIN(ANALYZE,BUFFERS) + get_stage_progress + /pipeline/stats p50/p95 bench"
  - "just perf-db-up/-down + perf-seed + perf-explain recipes (group db)"
  - "82-VERIFICATION.md — recorded PERF-02 numbers, index-scan evidence, DENORM-01 NO-GO call, D-02 deployment gate"
affects:
  - "DENORM-01 decision (deferred; concurrent four-bucket reads recommended before the denorm bitmap column)"
  - "homelab rollout — the D-02 read-only lux re-check (>=036 + zero analyzed-NULL) is gated here"
tech-stack:
  added: []
  patterns:
    - "Perf corpus seeding = parameterized unnest-array bulk INSERT + ON CONFLICT DO NOTHING (fast AND idempotent, no f-string SQL)"
    - "Faithful endpoint EXPLAIN = rebuild the hot query from the REAL clause builders + compile literal-bound, never hand-rewrite"
    - "Dedicated ephemeral PG on its own port so a concurrent test-db recreate cannot wipe the corpus mid-measurement"
key-files:
  created:
    - "scripts/seed_perf_corpus.py"
    - "scripts/perf_explain.py"
    - ".planning/phases/82-counts-pending-set-cutover/82-VERIFICATION.md"
  modified:
    - "justfile"
key-decisions:
  - "DENORM-01 = NO-GO/deferred despite the ~1.4s endpoint p50 being OVER the ~1s budget: the overage is three SEQUENTIAL four-bucket reads; concurrent asyncio.gather is the cheaper remedy than a dual-write denorm bitmap column. Flagged for operator review."
  - "Recorded the index-scan reality honestly: 3 of 5 named 032 partial indexes are used (Bitmap Index Scan on their selective failure-marker branches); the other 2 + ix_cloud_job_awaiting are correctly NOT chosen (non-selective / tiny table) — no fabricated index usage."
  - "Used a DEDICATED ephemeral PG container (own port 5545) not the shared phaze-test-db, after a concurrent test-db recreate wiped an earlier perf DB."
patterns-established:
  - "just perf-db-up/-seed/-explain: reproducible 200K bench isolated from the shared integration-test DB"
requirements-completed: [PERF-02]
duration: ~65min
completed: 2026-07-10
---

# Phase 82 Plan 04: PERF-02 Counts Latency Measurement Summary

**Measured the derived `/pipeline/stats` path at a real 200K synthetic corpus (migration 036): endpoint p50
≈1.4s / get_stage_progress p50 ≈1.29s — OVER the `< ~1s` D-07 budget — and made the DENORM-01 NO-GO call
(defer the denorm bitmap column; parallelize the three sequential four-bucket reads first).**

## Performance

- **Duration:** ~65 min
- **Tasks:** 2/2
- **Files created:** 3 (2 scripts + VERIFICATION), modified: 1 (justfile)

## Accomplishments

- **Task 1 (`feat`, 37158047):** `scripts/seed_perf_corpus.py` — an idempotent, `uv run` standalone that
  bulk-seeds ~N music/video `FileRecord`s plus output-table rows to the D-06 selectivity profile
  (70% metadata / 55% fp-success / 40% analysis-completed / 5% analyze-failed / 1% cloud / 2% dedup /
  ~2.5% ledger in-flight / 3% non-media) via **parameterized `unnest`-array bulk INSERTs** (T-82-07 — no
  f-string SQL) with deterministic `uuid5` ids + `ON CONFLICT DO NOTHING` (fast AND idempotent); `--reseed`
  hard-gated to a perf-named DB. Plus `scripts/perf_explain.py` (the bench the `perf-explain` recipe
  delegates to) and the `just perf-db-up/-down` + `perf-seed` + `perf-explain` recipes in `group('db')`.
- **Task 2 (`docs`, cd2b868d):** ran the full 200K bench and wrote `82-VERIFICATION.md` with the seed
  parameters, every measured number, the per-query EXPLAIN index evidence, the pass/fail vs the `< ~1s`
  budget, the **DENORM-01** go/no-go call, and the **D-02** deployment-gated note.

## The measured result (the deliverable)

Dedicated `postgres:18-alpine` on port 5545, DB `phaze_perf82` at `036 (head)`, 200,000 files (194,000
music/video), warm, 20 iterations:

| Measurement | p50 | p95 | vs `< ~1s` (D-07) |
|---|---:|---:|:--|
| `get_stage_progress()` DIRECT (DENORM-relevant DB core) | 1290.9 ms | 1419.2 ms | **OVER** |
| `GET /pipeline/stats` full endpoint | 1405.3 ms | 1489.1 ms | **OVER** |

Per-query EXPLAIN (ANALYZE): pending queries are fast (metadata 104 ms / fingerprint 203 ms / discovered
239 ms); the **three enrich four-bucket `GROUP BY stage_status_case` reads dominate** (metadata 726 / fp 434 /
analyze 401 ms, run **sequentially** inside `get_stage_progress`).

Index evidence (honest): `ix_analysis_failed` (in `get_discovered` + `four_bucket[analyze]`) and
`ix_metadata_failed` (in `four_bucket[metadata]`) are CONFIRMED used (Bitmap Index Scan) on their selective
failure-marker branches; `ix_fprint_success` / `ix_analysis_completed` / `ix_cloud_job_awaiting` are correctly
NOT chosen (non-selective positive predicates over 40–57% of rows, or a 2K-row table) — a Seq/Hash scan is the
optimal plan there, not a regression. No query fell back to a pathological nested-loop.

## DENORM-01 call: NO-GO (deferred) — flagged for operator review

The number is over budget, so this is NOT a clean "within-budget YAGNI" defer. But the overage is the three
**sequential** four-bucket reads (~1.15 s combined), and `work_mem`/HashAggregate tuning was verified NOT to
help (the planner keeps Sort+GroupAggregate; the cost is the per-row correlated `EXISTS` SubPlans inherent to
the derived CASE). The cheapest remedy is running the three `_safe_bucket_counts` **concurrently**
(`asyncio.gather`, projected ≈0.5 s), which avoids DENORM-01's dual-write correctness/drift burden. Recommended
sequence: parallelize + re-measure; pull DENORM-01 forward ONLY if that still misses `~1s`. The 5 s
degrade-safe poll absorbs the current 1.4 s comfortably. See `82-VERIFICATION.md` for full detail.

## Deviations from Plan

### Auto-fixed / adjustments

**1. [Rule 3 — Blocking issue] Dedicated perf-DB container instead of the shared `phaze_test`**
- **Found during:** Task 2 (first 200K attempt)
- **Issue:** A sibling session's `just test-db` recreated the shared `phaze-test-db` container mid-run and
  dropped an earlier `phaze_perf82` DB I had created there (the documented shared-DB teardown race).
- **Fix:** Added `just perf-db-up/-down` — a SEPARATE `postgres:18-alpine` container on its own port (5545)
  so the corpus survives any `test-db` recreate. The test-environment contract's "own private DB" intent is
  honored more strongly (own container, not just own DB name).

**2. [Rule 3 — Blocking issue] Added `scripts/perf_explain.py` (not in the plan's Task-1 `<files>`)**
- **Issue:** The plan folded the EXPLAIN/bench logic conceptually into "the `perf-explain` recipe", but the
  project rule is "recipes delegate to `uv run` — no inline business logic". A delegating recipe needs a
  script.
- **Fix:** Authored `scripts/perf_explain.py` as the bench the recipe calls; committed with Task 1.

**3. [Rule 2 — Missing critical functionality] Provision `saq_jobs` for a faithful endpoint number**
- **Issue:** `/pipeline/stats` fans out reads against `saq_jobs`, which SAQ owns (NOT Alembic-managed), so a
  freshly-migrated perf DB lacks it — the endpoint's queue-activity reads took the degrade path and the first
  timing measured error-handling overhead, not DB cost.
- **Fix:** `perf_explain.py` idempotently provisions `saq_jobs` via the project queue seam
  (`build_pipeline_queue(...).connect()`) so the endpoint measures an idle-queue DB cost. Also added a direct
  `get_stage_progress()` timing as the clean DENORM-relevant core (independent of ASGI/Redis noise).

**4. [Honest recording] Index-scan must_have partially met**
- The must_have expected all five 032 partial indexes as Index Scans across the pending + four-bucket queries.
  Reality: 3 are used on their selective branches; 2 + `ix_cloud_job_awaiting` are correctly not chosen
  (non-selective / tiny table). Recorded as measured — no fabricated index usage (per the test-env contract).

## Notes

- **Measurement provenance (Pitfall 5 / D-06):** local dedicated synthetic corpus at migration HEAD (036),
  NOT live lux. Absolute ms are Apple-Silicon/colima host-relative; the transferable finding is the RELATIVE
  story (four-bucket dominates; sequential; pending queries fast).
- **D-02 deploy gate:** recorded DEPLOYMENT-GATED (≥036 + `COUNT(analyzed ∧ completed_at NULL ∧ failed_at
  NULL)=0`), to be re-run READ-ONLY against lux at homelab rollout — NOT probed here (prod ~031; its EXPLAIN
  would be invalid). The exact read-only SQL is in `82-VERIFICATION.md`.
- **No production code touched:** this plan added only `scripts/` + `justfile` + planning docs; the seeder/bench
  are read-mostly and the seeder is hard-gated against non-perf DBs.
- **Cleanup:** the dedicated `phaze-perf-db` container was torn down (`just perf-db-down`); the shared
  `phaze-test-db` was never mutated by this plan.

## Threat Flags

None — no new network endpoint, auth path, or schema change. The seeder is a local dev/CI utility with
parameterized-only inserts (T-82-07 mitigated) and a perf-DB-name TRUNCATE guard (T-82-08); no DDL authored
(T-82-DDL accept).

## Self-Check: PASSED

- FOUND: scripts/seed_perf_corpus.py
- FOUND: scripts/perf_explain.py
- FOUND: .planning/phases/82-counts-pending-set-cutover/82-VERIFICATION.md
- FOUND: just perf-seed / perf-explain / perf-db-up / perf-db-down (group db)
- FOUND commit 37158047 (feat: seeder + bench + recipes), cd2b868d (docs: VERIFICATION)
