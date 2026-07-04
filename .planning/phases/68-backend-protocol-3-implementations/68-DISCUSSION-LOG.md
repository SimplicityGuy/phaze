# Phase 68: Backend Protocol + 3 Implementations - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-03
**Phase:** 68-backend-protocol-3-implementations
**Areas discussed:** BACK-04 acceptance gate, 68↔69 in-flight boundary, kube_staging scope, backend_id backfill shape

---

## BACK-04 acceptance gate

| Option | Description | Selected |
|--------|-------------|----------|
| Golden side-effect snapshot vs current code | Baseline = current post-67 code (a1/k8s never live → no prod trace; the code is the reference). Record observable side-effect sequence over {compute,kueue,local}×{agent up/down}; snapshot on today's code, refactor, assert unchanged. Preserves compute-gate/kueue-skip asymmetry. | ✓ |
| Decision-table assertion (lighter) | Assert a per-kind decision table (gate enforced, staging path, in_flight value) instead of recorded side-effects. Cheaper, less exhaustive — misses ordering/side-effect drift. | |

**User's choice:** Golden side-effect snapshot vs current code (D-01)
**Notes:** Resolves Phase 67's D-14 hand-off — "byte-identical" is realized against the current code, not a nonexistent production trace, because the a1/k8s paths were never deployed (67 D-11). The compute-requires-live-agent vs. Kueue-skips-the-gate asymmetry is a first-class snapshot assertion (D-01a).

---

## 68↔69 in-flight boundary

| Option | Description | Selected |
|--------|-------------|----------|
| Lay + prove, defer the flip to 69 | Phase 68 adds backend_id, records compute pushes in cloud_job, defines in_flight_count(), asserts sum(in_flight_count(b)) == get_cloud_window_count(). Drain keeps the FileState window until Phase 69. No per-backend cap consumption yet → no double-count; 68 stays behavior-preserving; SCHED-02 owns the cap flip. | ✓ |
| Flip the drain to per-backend counts in 68 | Rewire stage_cloud_window to compute slots from per-backend in_flight_count now. More done in 68, but Pitfall 1 (double-count) and Pitfall 2 (unlocked reconcile race) bite here — harder to prove behavior-preserving, blurs into SCHED-02's scope. | |

**User's choice:** Lay + prove, defer the flip to 69 (D-02 / D-02a)
**Notes:** The sum-equivalence invariant IS the characterization proof for the single-backend case. The write-ordering rule (D-03, one transaction, cloud_job row before/with the FileState flip) is a Phase-68 structural requirement even in lay+prove mode (research Pitfall 4).

---

## kube_staging scope

| Option | Description | Selected |
|--------|-------------|----------|
| Pure re-home, defer per-cluster to Phase 70 | KueueBackend wraps today's single-cluster kube_staging verbatim; token-mutation hack left as-is. Per-cluster kubeconfig/context + retiring the token hack = Phase 70 (MKUE-01). Matches roadmap BACK→68 / MKUE→70. Overrides research SUMMARY §62. | ✓ |
| Parameterize per-cluster + retire token hack in 68 | Do the per-cluster kr8s refactor now. Front-loads Phase 70 risk into 68; 68 no longer a pure re-home; blurs the BACK/MKUE boundary. | |

**User's choice:** Pure re-home, defer per-cluster to Phase 70 (D-05)
**Notes:** Explicitly overrides `.planning/research/SUMMARY.md` §62's assignment of parameterized kube_staging to Phase 68 — the planner must not pull that forward.

---

## backend_id backfill shape

| Option | Description | Selected |
|--------|-------------|----------|
| Nullable, no meaningful backfill | a1/k8s never deployed → ~zero live cloud_job rows; backend_id is config-derived (migration can't know a registry id). Add nullable column; new rows stamp backend_id at dispatch. | ✓ |
| Non-null, backfill to resolved single backend id | NOT NULL + backfill existing rows to the resolved single non-local backend id / sentinel. Matches BACK-02's literal wording but couples the migration to config-derived state for zero real benefit. | |

**User's choice:** Nullable, no meaningful backfill (D-06)
**Notes:** Reinterprets BACK-02's "backfill of existing rows" — there is nothing live to backfill.

---

## Claude's Discretion

- Exact module location for the protocol + implementations (research SUMMARY suggests `services/backends.py`).
- The precise set of `CloudJobStatus` values counted as non-terminal / in-flight for `in_flight_count()`.
- Snapshot fixture shape / serialization format for the D-01 golden characterization test.
- Whether the raise-on-`>1`-non-local guard (D-07) lives in Backend resolution or stays in the scheduler.
- Whether backend implementations are instantiated per-registry-entry or resolved lazily.

## Deferred Ideas

- Drain flip to per-backend caps under the advisory lock — SCHED-02, Phase 69.
- Advisory-lock ordering between `reconcile_cloud_jobs` and the drain — Pitfall 2, Phase 69.
- Rank tiering, spillover, black-hole guard, equal-rank tie-break, single-recovery-owner-per-kind — SCHED-01/03/04/05, Phase 69.
- Per-cluster `kube_staging` parameterization + retiring the token-mutation hack — MKUE-01, Phase 70.
- Attempt-budget / per-backend cooldown split (`last_dispatched_at`) — Pitfall 5, Phase 69 (schema hooks may be prepped in 68).
- N-lane admin UI + master revert-to-all-local toggle — BEUI-01/02, Phase 71 (`cloud_enabled` is the toggle's foundation).

**Derived decision captured in CONTEXT.md (not a discussed area):** D-07 — remove the `active_cloud_kind`/`active_cap` transitional shims (`config.py:481/489`), keep `cloud_enabled` (`config.py:454`), preserve the single-non-local invariant + raise-on-`>1`-non-local guard (multiplicity is Phase 69).
