---
status: partial
phase: 70-multi-kueue-n-clusters
source: [70-01-SUMMARY.md, 70-02-SUMMARY.md, 70-03-SUMMARY.md, 70-04-SUMMARY.md, 70-05-SUMMARY.md]
started: 2026-07-04
updated: 2026-07-04
mode: agent-driven
---

## Current Test

(complete — orchestrator drove tests 1-6 against real infra; test 7 blocked, deployment-gated)

## Tests

### 1. Cold Start — operator declares N (2) Kueue backends
expected: A backends.toml declaring two KueueBackends (distinct kube contexts) + local + a shared and two cluster-specific buckets loads via PHAZE_BACKENDS_CONFIG_FILE; ControlSettings validates; resolve_backends() yields 2 distinct KueueBackends + LocalBackend; the startup registry log projection is id/kind/rank/cap only (no secret).
result: pass
evidence: "Live probe against real config: ControlSettings() loaded scratchpad/uat_backends.toml; resolve_backends() → [('local','local'),('kueue-a','kueue'),('kueue-b','kueue')], kueue backends distinct objects; log projection = id/kind/rank/cap only, neither 'token-a-secret' nor 'token-b-secret' present."

### 2. SC-1 — distinct per-cluster kube auth (concurrent dispatch)
expected: Each KueueBackend builds a DISTINCT constructor-time-authed kr8s client from its own KubeConfig (both kubeconfig+context and api_url+sa_token forms); no no-arg kr8s.api() fallback; token-mutation hack retired.
result: pass
evidence: "Live probe: _kubeconfig_dict_from produced DISTINCT dicts per cluster (kueue-a synthesized from api_url+sa_token → server kube-a; kueue-b parsed from inline kubeconfig YAML+context=cluster-b → server kube-b) — distinct dicts are kr8s's client cache-key basis. Client-level distinctness + no-token-hack + explicit-kubeconfig/context covered by green suite tests test_distinct_kubeconfigs_yield_distinct_clients / test_source_has_no_token_hack / test_api_passes_dict_kubeconfig_and_context (8 passed). Full client construction against a live API server = manual item #7."

### 3. SC-2 — deterministic per-file bucket, read-recorded, credential-free
expected: pick_bucket selects one bucket per file deterministically over a multi-bucket set, restart-stable (sha256, not salted hash); presign/delete resolve the RECORDED staging_bucket, never re-derive; presigned URLs are file_id-scoped/TTL-bounded (pods credential-free).
result: pass
evidence: "Live probe: pick_bucket over kueue-a's 2-bucket set {shared-pub,bucket-a} for 6 file_ids was identical across a fresh ControlSettings instantiation (restart-stable), both buckets used (deterministic distribution). resolve_bucket_config('bucket-b')→phaze-b, ('ghost')→None, (None)→None. staged_object_key is file_id-UUID-scoped. Credential-free/TTL presign covered by green s3_staging suite (9 passed)."

### 4. SC-3 — per-cluster failure isolation
expected: A KueueBackend whose is_available()/in_flight_count()/dispatch() raises is isolated (0 slots / clean hold, logged by backend_id only); healthy backends + local still get work; the drain tick never raises.
result: pass
evidence: "Drove tests/analyze/tasks/test_release_awaiting_cloud.py against live Postgres — 5/5 PASSED: is_available_raise_does_not_poison_tick, in_flight_count_raise_treats_backend_as_zero_slots, generic_dispatch_raise_holds_candidate_and_continues, dispatch_noactiveagent_holds_all_and_breaks, unexpected_error_rolls_back_and_never_raises (CR-02 guard)."

### 5. SC-4 — clean-before-flip cross-bucket cleanup (concurrency-safe)
expected: At-cap spillover deletes the old (backend, RECORDED staging_bucket) object UNDER the held pg_advisory_xact_lock(5_000_504) BEFORE the AWAITING_CLOUD commit; same-bucket re-dispatch preserves the new owner's object; a concurrent drain cannot destroy it.
result: pass
evidence: "Drove tests/analyze/tasks/test_reconcile_cloud_jobs.py against live Postgres — 6/6 PASSED incl. test_drain_reconcile_concurrency_delete_runs_under_advisory_lock (a real 2nd connection's pg_try_advisory_xact_lock is False during the delete, True after commit — proving the delete holds the lock), test_clean_before_flip_ordering_delete_precedes_commit_precedes_job, and test_spillover_same_bucket_redispatch_preserves_new_object."

### 6. Migration 030 round-trip / isolation
expected: migration 030 upgrades and downgrades cleanly; staging_bucket is nullable with no backfill; the migration never references saq_jobs.
result: pass
evidence: "Drove tests/integration/test_migrations/test_migration_030_staging_bucket.py against live Postgres — 3/3 PASSED: upgrade_030_adds_nullable_staging_bucket_then_downgrade_reverses, migration_never_references_saq_jobs, revision_identifiers_are_bare_numbers."

### 7. Manual-only — live 2nd real Kueue cluster + real dual-bucket S3 spillover
expected: With a real second Kueue cluster and two live bucket endpoints, a real drain tick reaches both clusters, reconcile is backend_id-scoped, and a forced cross-bucket spillover deletes the old object while the new pod's object survives.
result: blocked
blocked_by: prior-phase
reason: "No real 2nd Kueue cluster or dual live S3 bucket endpoints available in-session — deployment-gated live E2E (Phase-56/68/69 rollout carryover). The api_url-form _api() client build attempts a real network auth handshake (httpx ConnectError against a fake host), confirming this genuinely needs a reachable cluster. Verify at rollout: declare a 2nd real KueueBackend, run a real drain tick, confirm both clusters reachable + reconcile backend_id-scoped + cross-bucket spillover preserves the new object."

## Summary

total: 7
passed: 6
issues: 0
pending: 0
skipped: 0
blocked: 1

## Gaps

[none yet]
