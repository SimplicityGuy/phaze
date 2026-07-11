# Phase 82: Counts & Pending-Set Cutover - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-10
**Phase:** 82-counts-pending-set-cutover
**Areas discussed:** Pending-set home & shape, Dedup exclusion, Stats / DAG four-bucket, PERF-02 measurement

---

## Pending-set home & shape

| Option | Description | Selected |
|--------|-------------|----------|
| `eligible_clause(stage)` in `stage_status.py` | SQL twin of Python `eligible()`, drift-locked via Phase-78 DERIV-04 harness; all 3 pending sets compose it ∧ `~dedup_resolved_clause()` ∧ `file_type IN MUSIC_VIDEO_TYPES` | ✓ |
| Hand-compose inline in `pipeline.py` | Each pending set builds primitive clauses directly; no equivalence lock, drift risk in 3 places | |

**User's choice:** `eligible_clause(stage)` in `stage_status.py`
**Notes:** Single source of truth; extends the anti-drift harness Phase 78 built. Analyze set keeps its `LEFT JOIN metadata.duration` for the cloud router; `in_flight(analyze)` already spans `cloud_job` (83); analyze-failed excluded as terminal (ELIG-03).

---

## The analyze-corpus over-enqueue guard (036 backfill)

| Option | Description | Selected |
|--------|-------------|----------|
| Data-only backfill migration in this phase | Stamp `analysis_completed_at` for pre-existing analyzed rows before the reader flip | |
| Out of scope — separate data-repair + hard gate | Flag as pre-existing issue, VERIFICATION-gate only | |
| Accept re-analysis | Let 1001 files re-enqueue | |
| **Verify-only: assert 036 applied, no new migration** *(added after discovery)* | Phase 80's `036` already backfills; Phase 82 asserts ≥036 + zero analyzed-NULL rows before trusting the flip | ✓ |

**User's choice:** Verify-only: assert 036 applied, no new migration
**Notes:** Mid-discussion discovery — `036_backfill_analysis_completed_at.py` already shipped in **Phase 80** (PR #229, `bd551bff`, READ-03/D-13). My initial "add a backfill migration" recommendation was stale; corrected on the spot. The matched todo `analysis-completed-at-backfill.md` is resolved upstream. The auto-memory note "nothing in 032-035 backfills it" is stale — `036` is outside that range and does it.

---

## Dedup exclusion (Phase 84 hand-off)

| Option | Description | Selected |
|--------|-------------|----------|
| Apply `~dedup_resolved_clause()` in all 3 pending sets; keep `eligible()` dedup-agnostic | Dedup composed at the query level; primitives stay dedup-free | ✓ |
| Bake dedup into `eligible()`/`eligible_clause()` | Add a dedup notion inside the eligibility primitives | |

**User's choice:** Apply `~dedup_resolved_clause()` in all 3 pending sets; keep `eligible()` dedup-agnostic
**Notes:** Consummates Phase 84's explicitly-deferred hand-off (READ-01). Dedup is a file-level predicate, not a `Stage`; keeping it out of the primitives keeps the DERIV-04 harness clean.

---

## Stats / DAG four-bucket

| Option | Description | Selected |
|--------|-------------|----------|
| Extend `get_stage_progress` | Enrich nodes return `{not_started, in_flight, done, failed, total}`; downstream stays `{done, total}` | ✓ |
| New dedicated function | Separate `get_enrich_stage_buckets()` | |

**User's choice:** Extend `get_stage_progress`
**Notes:** It already derives every node from output tables with `_safe_count`; reuse it via `stage_status_case` GROUP BY.

| Option | Description | Selected |
|--------|-------------|----------|
| Remove `get_pipeline_stats` fully; derive all counts from output tables now | No `state`-keyed GROUP BY survives; tail counts via row existence (as `get_stage_progress` already does); `notYetEnriched` re-expressed | ✓ |
| Enrich-scoped: leave the tail on `state` until Phase 86 | Cut over enrich only; leaves a linear GROUP BY | |

**User's choice:** Remove it fully; derive all counts from output tables now
**Notes:** Satisfies SC#2 literally. Front-runs nothing in Phase 86 (86 changes proposals-status *authority*, not count derivation). `stats_bar.html` key remap + return shape → planning.

---

## PERF-02 measurement

| Option | Description | Selected |
|--------|-------------|----------|
| Local synthetic-seed 200K at HEAD + EXPLAIN ANALYZE | Seed local PG at ≥036 (with 032 indexes), EXPLAIN ANALYZE the pending + four-bucket queries, time the endpoint | ✓ |
| Live read-only lux probe | Prod lacks 032 indexes → invalid plans | |
| Both: local plan + live row-count sanity | Local authoritative + live distribution check | |

**User's choice:** Local synthetic-seed 200K at HEAD + EXPLAIN ANALYZE
**Notes:** Landmine — prod/lux is at Alembic ~031, missing the 032 partial indexes the derived anti-joins ride; a live probe would exercise a pessimistic/invalid plan.

| Option | Description | Selected |
|--------|-------------|----------|
| `<1s` endpoint budget; DENORM-01 only if over budget | Record the number regardless; YAGNI on the denorm column | ✓ |
| `<5s` (poll interval) budget | Looser, no headroom | |
| You decide / set at plan-time | Threshold from measured baseline | |

**User's choice:** `<1s` endpoint budget; DENORM-01 only if measured over budget
**Notes:** Leaves headroom under the 5s poll and concurrent load. The recorded measurement is what licenses the YAGNI decision to skip DENORM-01.

---

## Claude's Discretion

- Four-bucket return shape (nested vs flat), constrained to sum-to-total + `_safe_count` degrade.
- `stats_bar.html` template key remap.
- The 200K synthetic-seed harness shape / reuse of existing seed fixtures.
- Manual-trigger endpoint alignment (route through the narrowed pending helpers, Phase-42 precedent).
- Plan/PR decomposition (4 natural seams).

## Deferred Ideas

- DENORM-01 denormalized stage-bitmap column — only if PERF-02 fails the budget.
- Per-file stage matrix / failure-retry UI / eligibility trace / priority stepper — Phase 87.
- `get_pushing_count` / `get_pushed_count` unowned gap — carried from 83/84.
- `find_duplicate_groups` nondeterministic pagination — pre-existing quick task.
- Reviewed-not-folded todo: `analysis-completed-at-backfill.md` — resolved upstream by Phase 80's `036`.
