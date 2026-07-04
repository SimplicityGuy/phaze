# Phase 70: Multi-Kueue (N Clusters) - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-04
**Phase:** 70-multi-kueue-n-clusters
**Areas discussed:** Spillover cleanup identity, Per-cluster kube auth form, Compute agent_ref scope, Bucket selection + failure isolation

---

## Spillover cleanup identity (MKUE-04)

**Q1 — How should the (backend, bucket) that staged an object be identified at cleanup time, given one-row-per-file with in-place backend_id mutation?**

| Option | Description | Selected |
|--------|-------------|----------|
| Clean-before-flip + record bucket | Add nullable `staging_bucket` column; on spillover delete the OLD (backend_id, staging_bucket) object in the same transition BEFORE repurposing the row; active cleanup primary, TTL backstop | ✓ |
| Re-derive deterministically | No column; recompute (backend, bucket) from (backend_id, file_id) — breaks on mutation, drifts on config change | |
| TTL-only for spilled objects | Clean only the current object; let spilled-away objects expire via TTL — under-delivers active cleanup | |

**User's choice:** Clean-before-flip + record bucket (D-01)

**Q2 — Keep cloud_job one-row-per-file, or switch to one-row-per-(file, backend)? (research flag a)**

| Option | Description | Selected |
|--------|-------------|----------|
| One-row-per-file (keep) | backend_id + new staging_bucket mutated in place; consistent with Phase 69 D-06; no history table needed | ✓ |
| One-row-per-(file, backend) | Full dispatch history; reopens Phase 69 attempts model, breaks unique-file_id + in_flight_count | |

**User's choice:** One-row-per-file (keep) (D-02)

**Q3 — Who owns the clean-before-flip delete, and how defensive?**

| Option | Description | Selected |
|--------|-------------|----------|
| Reconcile owns it, best-effort | backend_id-scoped reconcile deletes the recorded (backend_id, staging_bucket) in the same transition, swallows errors, TTL backstop, never blocks re-dispatch, not in hot dispatch path | ✓ |
| Next-tick dispatch owns it | Drain deletes prior object before re-dispatch — couples S3 latency into the locked tick | |

**User's choice:** Reconcile owns it, best-effort (D-03)

**Notes:** Clean-before-flip ordering is the operator's explicit correctness requirement (Pitfall 9 — after mutation the stranded object is unidentifiable except via TTL).

---

## Per-cluster kube auth form (MKUE-01)

**Q1 — How should per-cluster kr8s clients authenticate, and what happens to the token-mutation hack?**

| Option | Description | Selected |
|--------|-------------|----------|
| Support both, retire the hack | Distinct client per backend; kubeconfig+context clean path; api_url+sa_token with proper constructor-time auth replacing the api.auth.token + _create_session() hack | ✓ |
| kubeconfig+context only | Drop api_url+sa_token; forces deployment migration | |
| Keep api_url+sa_token | Keep the token hack, per-client — carries the fragile _create_session() rebuild across N clients | |

**User's choice:** Support both, retire the hack (D-04)

**Notes:** Exact kr8s constructor form per distinct kubeconfig/context is a Phase-56-carryover live-cluster verification item — flagged for the researcher + deployment-gated E2E, not resolved on paper.

---

## Compute agent_ref scope (research flag b)

**Q1 — Should Phase 70 fix ComputeAgentBackend's agent_ref→Agent.id resolution, or defer it?**

| Option | Description | Selected |
|--------|-------------|----------|
| Defer to PROV-01 | MKUE is Kueue-only; only one compute agent (a1) exists; agent_ref gap only bites with a 2nd compute provider (PROV-01, deferred); fixing now is scope creep, can't be E2E-validated | ✓ |
| Fix it now | Wire agent_ref→Agent.id with fallback — adds compute-path work + tests to a Kueue phase, unvalidatable | |

**User's choice:** Defer to PROV-01 (D-05); latent gap noted for that future milestone.

---

## Bucket selection + failure isolation (MKUE-02/03)

**Q1 — How should the per-file bucket be selected when a Kueue backend's assigned set has multiple buckets?**

| Option | Description | Selected |
|--------|-------------|----------|
| Stable hash of file_id | index = stable_hash(file_id) mod len(sorted(bucket_ids)); sha256-based (not salted hash()); reproducible so cleanup/reconcile agree with staging | ✓ |
| Round-robin / least-loaded | Better balancing but needs shared mutable state in the locked tick + non-reproducible mapping | |
| First bucket only | Simplest but wastes multi-bucket capacity | |

**User's choice:** Stable hash of file_id (D-06)

**Q2 — Where should the per-cluster failure-isolation boundary live (Pitfall 8)?**

| Option | Description | Selected |
|--------|-------------|----------|
| Per-backend try/except in snapshot + dispatch | Wrap each backend's per-tick is_available()/in_flight_count() snapshot AND dispatch() in its own try/except; raising cluster → unavailable for that tick, others proceed | ✓ |
| Rely on existing per-candidate guard | A probe raising during the once-per-tick snapshot could still abort the whole tick (Pitfall 8) | |

**User's choice:** Per-backend try/except in snapshot + dispatch (D-07)

---

## Claude's Discretion

- Exact `cloud_job.staging_bucket` column type/name + additive migration mechanics (nullable, no meaningful backfill).
- Whether re-homed `kube_staging` functions take a `KubeConfig` param or become `KueueBackend` methods.
- Exact stable-hash primitive for D-06 + bucket-id sort.
- Exact `pg_advisory_xact_lock` scope for the clean-before-flip delete (research-flagged, carried from Phase 69).
- Confirming the presigned-GET mint reads the recorded `staging_bucket`, not a re-derive.

## Deferred Ideas

- Compute `agent_ref → Agent.id` resolution → PROV-01.
- Live multi-cluster kr8s auth verification → Phase-56-carryover live-E2E at rollout.
- N-lane UI, master revert toggle, runbook/config docs, `cloud_target`→`backends` migration → Phase 71 (BEUI).
- Duration-scaled / per-backend reconcile cron cadence split → SREF-01 (Future Requirements).
