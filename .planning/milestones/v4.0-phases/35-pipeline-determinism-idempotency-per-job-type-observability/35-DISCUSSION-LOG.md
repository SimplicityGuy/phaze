# Phase 35: Pipeline Determinism, Idempotency & Per-Job-Type Observability - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-11
**Phase:** 35-pipeline-determinism-idempotency-per-job-type-observability
**Areas discussed:** DAG view scope, Progress data source, Counter trust/durability, generate_proposals re-run semantics

---

## DAG view scope (vs Phase-34 UI)

| Option | Description | Selected |
|--------|-------------|----------|
| Replace fully | DAG becomes the single pipeline UI; trigger buttons fold into nodes; Phase-34 action cards + aggregate processing card removed | ✓ |
| DAG primary, keep cards below | DAG main view; keep Phase-34 cards as fallback control strip | |
| Augment only | Keep Phase-34 layout; add Metadata card + separate per-job-type section; no full DAG canvas | |

**User's choice:** Replace fully
**Notes:** Operator wants one coherent graph, not graph + legacy cards. Accepted the larger blast radius on freshly-shipped Phase 34 code. → D-01.

---

## Progress data source (reconciling locked decision B with SAQ hook reality)

| Option | Description | Selected |
|--------|-------------|----------|
| DB-state done + maintained in-flight | 'done' = DB stage count; 'remaining/active' = maintained Redis counter | |
| Fully maintained counters | Track enqueued + completed per function in Redis; derive everything from counters | ✓ |
| DB-state only for done, queue depth for active | No new completion counters; queue depth for active | |

**User's choice:** Fully maintained counters
**Notes:** Pure expression of locked decision B. Pinned alongside the counter-trust answer below — "fully maintained + reconcile from DB on read" resolves the SAQ-no-after-process-hook constraint by making the completion increment best-effort. → D-02.

---

## Counter trust / durability (given the 2026-06-11 doubling incident)

| Option | Description | Selected |
|--------|-------------|----------|
| Reconcile from DB on read | Counters are a fast cache; reconcile 'done' against DB stage counts on each read so purge/restart can't leave UI permanently wrong | ✓ |
| Trust Redis, manual rebuild | Counters source of truth between events; operator 'rebuild from DB' action | |
| Ephemeral in-flight only | Counters track only live queued+active; cumulative 'done' always from DB | |

**User's choice:** Reconcile from DB on read
**Notes:** Self-healing. Key design pin: SAQ 0.26.x has only `register_before_enqueue` (no after-process hook), so completion-side increment may be best-effort because the DB reconcile is the backstop. → D-03.

---

## generate_proposals re-run semantics

| Option | Description | Selected |
|--------|-------------|----------|
| Upsert per file, skip non-pending | One active proposal per file; re-run overwrites PENDING in place, never touches APPROVED/EXECUTED | ✓ |
| Upsert per file, overwrite all | Re-run overwrites whatever exists regardless of status | |
| Keyed by (file_id, batch_index) | Conflict target is batch identity; same file in a different batch still duplicates | |

**User's choice:** Upsert per file, skip non-pending
**Notes:** Idempotent re-runs AND protects human-approved decisions. Planner must confirm proposals schema supports a file_id-scoped conflict target + a status guard enforced in the upsert. → D-04.

---

## Claude's Discretion

- Natural-id choice per task for deterministic keys (single-file vs batch-hash for `generate_proposals`, tracklist_id vs file_id for tracklist tasks).
- Where central key enforcement lives — `before_enqueue` hook setting `job.key` vs. a key-builder in `enqueue_router`/`agent_task_router` (verify SAQ 0.26.x can set the key in a before_enqueue hook; else router-level builder).
- Completion-increment mechanism for maintained counters (worker hook / task-side INCR / none + DB reconcile).
- DAG canvas Tailwind/SVG/Alpine specifics; reuse `$store.pipeline` + OOB-swap + 5s poll.
- `tag_write_log` idempotency: audit, add upsert only if a gap is found (execution_log already idempotent).

## Deferred Ideas

- Per-stage triggers for the tracklist sub-chain beyond existing endpoints (no net-new endpoints unless a scoped item requires).
- Animated edge/flow effects on the DAG canvas (visual polish).
