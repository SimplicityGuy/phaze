# Phase 80: Recovery / Re-enqueue Cutover - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-08
**Phase:** 80-recovery-re-enqueue-cutover
**Areas discussed:** Domain-completed source, AWAITING/push sidecar map, Reconcile scope boundary, Pending-import removal

---

## Area selection

| Option | Description | Selected |
|--------|-------------|----------|
| Domain-completed source | Reuse Phase-78 `eligible()`/`FAILURE_IS_TERMINAL` vs. hand-roll per-stage done sets | ✓ |
| AWAITING/push sidecar map | How `_get_awaiting_cloud_ids` / `_select_done_push_ids` rederive from the cloud sidecar | ✓ |
| reconcile scope boundary | Read-audit + regression guard vs. pulling writer retirement forward | ✓ |
| Pending-import removal | Drop the pending-set imports; derive `done` directly (anti-double-negation) | ✓ |

**User's choice:** All four.

---

## Domain-completed source

| Option | Description | Selected |
|--------|-------------|----------|
| Reuse Phase-78 terminal predicate | `domain_completed(stage) = done OR (failed AND FAILURE_IS_TERMINAL[stage])` — one source of truth; the analyze/metadata/fingerprint asymmetry falls out automatically | ✓ |
| Hand-roll per-stage in reenqueue | Keep bespoke done-set SELECTs, swapping `FileRecord.state` columns for the new marker columns without routing through the Phase-78 abstraction | |

**User's choice:** Reuse Phase-78 terminal predicate.
**Notes:** Locks the per-stage asymmetry — `analyze: done∨failed`, `metadata: done∨failed`, `fingerprint: done` only (failed auto-retries per D-16). Prevents drift between recovery's terminal semantics and the derivation layer's.

### Follow-up: the analyze-terminal ordering hazard

Surfaced during discussion: the derived `failed(analyze)` reads `analysis.failed_at`, but the analyze failure path writes `state = ANALYSIS_FAILED` and **not** `failed_at` until Phase 81 (FAIL-01). Phase 77 backfilled the marker once; there is no live writer. Cutting the read over first narrows the belt-and-suspenders secondary net guarding the 44.5K-job over-enqueue class. Same applies to metadata (`report_metadata_failed` persists nothing until FAIL-02).

| Option | Description | Selected |
|--------|-------------|----------|
| Pull failure-marker writes into 80 | Phase 80 also cuts the analyze/metadata failure writers — coupled writer touch, overlaps FAIL-01/02 | |
| Reorder: 81 before 80 | Make Phase 81 upstream of Phase 80 so `failed_at` is written before recovery derives from it | ✓ |
| Accept temporary narrowing | Rely on the primary ledger-clear net; document the rare callback-partial-failure gap as interim | |

**User's choice:** Reorder — Phase 81 before Phase 80.
**Notes:** Establishes the governing principle: *a read-cutover phase must follow the writer-cutover phase that keeps its derived source live.* Also resolves the metadata side of D-01 for free. Requires a ROADMAP `Depends on` edit.

---

## AWAITING/push sidecar map

Verified against `main` before asking: **`CloudJobStatus.AWAITING` has no live writer** (Phase 77 added the enum value, CHECK, partial index, and a one-time backfill only). Every live writer still writes `FileRecord.state = AWAITING_CLOUD` (`agent_push.py:261`, `pipeline.py:345`, `agent_s3.py:195`, `reconcile:212`). **`PUSHED` has no sidecar status at all** — the CHECK list has no `'pushed'` member. SIDECAR-01 (Phase 83) owns the live writers, and is downstream of Phase 80.

| Option | Description | Selected |
|--------|-------------|----------|
| Reorder SIDECAR-01 before 80 | Make Phase 83 upstream too — same principle as the 81 reorder | ✓ |
| Scope-exclude cloud reads from 80 | Cut only the enrich-stage reads; leave AWAITING/PUSHED state reads until Phase 83 | |
| Derive from live facts only | Derive held/push-done from in-flight `cloud_job` + ledger + analyze-terminal, no SIDECAR-01 | |
| Pull cloud writer dual-write into 80 | Phase 80 dual-writes AWAITING/PUSHED to the sidecar, then reads it | |

**User's choice:** Reorder SIDECAR-01 before 80.
**Notes:** Second application of the same principle. Phase 80's `Depends on` becomes 78, 79, 81, 83 — it moves to after all its writer-cutover dependencies.

---

## Reconcile scope boundary

Scouting found `reconcile_cloud_jobs.py`'s read side is **already** sidecar-derived (`cloud_job WHERE status IN (SUBMITTED, RUNNING)`), with zero `FileRecord.state` reads. Its only `FileRecord` coupling is the at-cap spill-back **write** at `:212`.

| Option | Description | Selected |
|--------|-------------|----------|
| Audit + regression guard only | Verification deliverable: assert zero state reads; leave the `:212` write to Phase 83 | |
| Also retire the AWAITING write here | Phase 80 retires `:212` so its two named files are fully state-free | ✓ |

**User's choice:** Also retire the AWAITING write here.
**Notes:** Widens Phase 80 slightly beyond pure reads, into the single residual write in one of its two named files.

### Follow-up: ownership deconfliction

The above answer is in tension with the area-2 reorder — if Phase 83 is upstream and migrates cloud-routing writers, it could already cover reconcile's spill-back. Asked to nail the split so the planner doesn't double-own the line.

| Option | Description | Selected |
|--------|-------------|----------|
| 80 owns reconcile; 83 excludes it | Phase 83 migrates the other cloud writers + reads; Phase 80 owns `reconcile:212` | ✓ |
| 83 owns all cloud writers incl. reconcile | Phase 80's reconcile deliverable reverts to audit + regression guard | |

**User's choice:** 80 owns reconcile; 83 excludes it.
**Notes:** "Phase 80 owns its two named files end-to-end." Requires a ROADMAP scope-exclusion note on Phase 83. The MKUE-04 clean-before-flip ordering at `:174-219` must survive the write swap byte-for-byte.

---

## Pending-import removal

The *whether* was locked by the phase goal (this is its core rationale). Discussion focused on the *shape*: deriving `done` directly inverts the set-size characteristic — today the **pending** sets are small and bounded; the **done** set is most of a 200K corpus.

| Option | Description | Selected |
|--------|-------------|----------|
| Ledger-scoped done query | Derive `done` only for the `file_id`s in the ledger rows already read this run — `O(\|ledger\|)`, never `O(200K)` | ✓ |
| Full-corpus done sets | Materialize whole-corpus done sets, mirroring today's analyze/push SELECTs | |
| Per-row correlated EXISTS | Evaluate `done` per ledger row at filter time — N+1 queries | |

**User's choice:** Ledger-scoped done query.
**Notes:** Recovery only ever asks about files that appear in the ledger, so bounding the done-set queries to `fids` preserves the existing read-once-per-run shape while keeping the inverted set small.

---

## Claude's Discretion

- Internal shape of the ledger-scoped done-set helper (one query per stage vs. a single `stage_status_case` query bucketed in Python).
- Whether the `_ANALYZE_DONE` / `_PUSH_DONE` / `_METADATA_PENDING` / `_FINGERPRINT_PENDING` key constants get renamed to reflect the done-not-pending inversion.
- Bound-parameter chunking strategy for `id IN fids` if the ledger grows large.
- Mechanism of the "zero `FileRecord.state` reads" regression guard (AST check, import guard, or source assertion) — an existing project idiom wins.
- Test-bucket placement for the new regression tests (must pass via `just test-bucket <bucket>` in isolation).

## Deferred Ideas

- **PROV-01 (N-compute-aware orphan recovery)** — design §9 non-goal; Phase 80 touches the done-set derivation, not `select_active_agent(kind="compute")`. Re-check the overlap, do not fix.
- **Phase 82 read-before-write inversion check** — whether READ-01/READ-02 hit the same D-02/D-03 hazard. Belongs to Phase 82's discussion.
- **Retiring the other cloud-routing state writers** (`agent_push.py:261`, `pipeline.py:345`, `agent_s3.py:195`) — SIDECAR-01 / Phase 83.
- **Cloud-push lane drain (`--profile drain`) quiesce** before the destructive `033` — Phase 90's rollout runbook.
