# Phase 92: Milestone-Close Tech-Debt Cleanup - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-13
**Phase:** 92-milestone-close-tech-debt-cleanup
**Areas discussed:** Cleanup appetite, PERF-02 parallelization, Test-hermeticity fix

---

## Cleanup appetite (framing)

| Option | Description | Selected |
|--------|-------------|----------|
| Surgical / minimal | Do exactly the 3 audit items with the smallest blast radius; fix flakes at bucket level, not shared conftest; prioritize a fast, low-risk close. | |
| Root-cause where cheap | Surgical by default, but take a root-cause fix if it's nearly the same effort and clearly lower long-term risk. | |
| Thorough | Fix root causes properly even if broader — whole-suite hermetic, parallelize all reads, re-measure at 200K; accept more regression surface. | ✓ |

**User's choice:** Thorough
**Notes:** This framing drove the broader options in the two subsequent areas (all-reads parallelization + global conftest fix + full 200K re-measurement).

---

## PERF-02 parallelization

**Q1 — Parallelization scope**

| Option | Description | Selected |
|--------|-------------|----------|
| All independent reads | Gather every independent read in get_stage_progress (~7-9 concurrent sessions/poll). Biggest win; planner confirms pool headroom. | ✓ |
| The 3 enrich reads only | Audit's literal ask: gather just the 3 `_safe_bucket_counts`. +2 connections/poll; others stay sequential. | |
| Bounded concurrency | Parallelize all but cap with an `asyncio.Semaphore`. Middle path. | |

**User's choice:** All independent reads
**Notes:** Semaphore cap retained in CONTEXT (D-03) as an acceptable fallback only if the pool-headroom check comes back tight.

**Q2 — Measurement / DENORM-01 gate**

| Option | Description | Selected |
|--------|-------------|----------|
| Re-run full 200K measurement | Rebuild/reuse the PERF-02 harness, measure before/after, record in VERIFICATION. | ✓ |
| Re-measure + explicit DENORM-01 verdict | Full re-measurement AND a written revive/kill decision record for DENORM-01. | |
| Lightweight proof, defer full measure | Prove reads overlap without the 200K harness; defer the definitive measurement. | |

**User's choice:** Re-run full 200K measurement
**Notes:** The measured result informs the DENORM-01 revisit naturally (SC1: "revisited only if this proves insufficient"); no separate formal verdict record mandated.

---

## Test-hermeticity fix

**Q1 — Mechanism**

| Option | Description | Selected |
|--------|-------------|----------|
| Transactional rollback per test | Session-scoped engine + per-test transaction/SAVEPOINT rolled back on teardown. Strongest isolation, biggest conftest rewrite. | ✓ |
| Truncate shared tables per test | Keep per-test create_all/drop_all but TRUNCATE the polluting tables between tests. Smaller change. | |
| You decide (planner picks) | Let research/planning evaluate both and pick. | |

**User's choice:** Transactional rollback per test
**Notes:** Inherently a global (session-scoped) conftest change; collides with the `get_session`-never-commits / commit-then-read pattern.

**Q2 — De-risk approach**

| Option | Description | Selected |
|--------|-------------|----------|
| Full-suite green gate + SAVEPOINT recipe | Join-external-transaction + restart-SAVEPOINT-after-commit so commit-then-read tests pass; gate on full ~1750-test suite green under per-bucket CI isolation. | ✓ |
| Incremental: prove buckets first | Land the change but validate bucket-by-bucket, staged rollout. | |
| Spike the conftest change first | Throwaway spike proving the fixture works with commit-then-read, then plan the rollout. | |

**User's choice:** Full-suite green gate + SAVEPOINT recipe
**Notes:** Nothing merges until the whole suite is hermetic under `just test-bucket`, not just the 2 named buckets (analyze, agents).

---

## Claude's Discretion

- Exact `asyncio.gather` structuring, per-read session-acquisition helper shape, and whether a `Semaphore` cap is needed (gated on the D-03 pool-headroom check).
- Precise `conftest.py` fixture wiring for the transactional-rollback isolation (engine/transaction/connection fixtures + savepoint-restart hook), subject to the commit-then-read constraint and the full-suite gate.
- Whether the 200K measurement harness is rebuilt fresh or the prior PERF-02 harness is reused.
- Cosmetic doc fixes (CLEAN-03) — not discussed, auto-decided: delete the duplicated `backends.py:563-566` block, fix the stale `agent_files.py:133` comment.

## Deferred Ideas

None new. Pre-existing deferrals (DENORM-01, P85 WR-01..04, P81 WR-01/02, 83-06, live-corpus deploy rehearsal) remain out of scope per the ROADMAP "Out of scope" line — not pulled into Phase 92.
