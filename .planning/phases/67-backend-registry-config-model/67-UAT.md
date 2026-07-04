---
status: complete
phase: 67-backend-registry-config-model
source: [67-01-SUMMARY.md, 67-02-SUMMARY.md, 67-03-SUMMARY.md, 67-04-SUMMARY.md, 67-05-SUMMARY.md, 67-06-SUMMARY.md]
started: 2026-07-04
updated: 2026-07-04
---

## Current Test

[testing complete]

## Tests

<!--
Phase 67 is config/backend-only (no UI), so the operator-observable behaviors were driven directly
by the assistant (self-run UAT) rather than manual click-through: a real ControlSettings construction
per case against temp backends.toml files, plus a real in-process app cold-start (create_app + /health)
against the ephemeral test DB. Drivers: scratchpad/uat67_driver.py (T2-T10), scratchpad/uat67_coldstart.py (T1).
-->

### 1. Cold Start Smoke Test
expected: With no `backends.toml` present, the app boots from scratch without errors, resolves the zero-config implicit all-local registry, logs it at startup, and serves a live request.
result: pass
evidence: `create_app()` built clean; boot log `effective backend registry backends=[{'id':'local','kind':'local','rank':99,'cap':1}] cloud_enabled=False`; `GET /health` → 200 `{"status":"ok"}` (against ephemeral DB).

### 2. Zero-config implicit-local (REG-04)
expected: Absent `backends.toml` → registry resolves to a single `kind=local` backend; `cloud_enabled` is False (pure local analysis, no cloud activity) — no config edits required.
result: pass
evidence: `len(backends)==1`, `backends[0].kind=="local"`, `cloud_enabled is False`.

### 3. Declare backends.toml → resolved registry + startup log (REG-01, REG-04)
expected: An operator `backends.toml` (local + kueue with rank/cap + a bucket) loads as the resolved registry; the app reports `cloud_enabled=True` / `active_cloud_kind=kueue` and logs the id/kind/rank/cap projection.
result: pass
evidence: `backends=['cluster-01','local']`, `cloud_enabled=True`, `active_cloud_kind=="kueue"`.

### 4. Discriminated-union id-tagged fail-fast (REG-02)
expected: A compute backend without `agent_ref`, or a kueue backend without `[backends.kube]`, fails construction fast with a message naming the offending backend id (not an opaque index error).
result: pass
evidence: compute `oci-a1` → error contains `oci-a1` + `agent_ref`; kueue `cl-x` → error contains `cl-x`.

### 5. Bounded rank/cap (REG-01)
expected: Out-of-range `rank`/`cap` values are rejected at construction (fail-fast posture).
result: pass
evidence: `rank=-1`, `cap=0`, and `cap=5000` all rejected.

### 6. Bucket scope cardinality — shared vs cluster-specific (REG-05, D-09)
expected: A `scope="shared"` bucket may be referenced by multiple kueue backends; a `scope="cluster-specific"` bucket referenced by >1 kueue backend fails fast (naming the bucket).
result: pass
evidence: shared bucket by 2 clusters accepted; the same config with `cluster-specific` scope rejected.

### 7. Inline `*_file` secrets resolve + fail-fast (REG-03)
expected: A `sa_token_file` (or bucket `*_file`) pointer reads the secret from disk (stripped) into a `SecretStr`; a missing/unreadable path fails fast.
result: pass
evidence: `active_kube.sa_token.get_secret_value()=="SUPERSECRETTOKEN"` from a temp file; a nonexistent `sa_token_file` path failed construction.

### 8. Secret-free startup registry log (REG-04)
expected: `log_effective_registry()` emits only the `id/kind/rank/cap` projection — never SA tokens or S3 secret keys.
result: pass
evidence: log contained `cluster-01`/`kueue` but NOT `SUPERSECRETTOKEN` (SA token) or `AKIASECRET` (S3 key).

### 9. `cloud_target` + flat fields removed, no back-compat (REG-04)
expected: The `cloud_target`/`s3_*`/`kube_*`/`compute_*` fields no longer exist on `ControlSettings`; legacy `PHAZE_CLOUD_TARGET`/`PHAZE_S3_BUCKET` env vars are silently ignored (`extra="ignore"`) and do NOT re-enable cloud.
result: pass
evidence: `hasattr(s,"cloud_target")` and `hasattr(s,"s3_bucket")` both False; with both legacy env vars set, `cloud_enabled` stayed False (implicit-local).

### 10. Per-bucket SSRF `endpoint_url` guard (REG-05)
expected: A bucket `endpoint_url` that is not http(s) with a non-empty host is rejected at construction.
result: pass
evidence: `file:///etc/passwd`, `ftp://x`, `not-a-url`, and empty string all rejected.

## Summary

total: 10
passed: 10
issues: 0
pending: 0
skipped: 0

## Gaps

[none]

## Notes

- Phase 67 is config-model-only; there is no UI surface. The single item the verifier flagged as
  deployment-gated (live all-local homelab deploy boot) is here exercised at the application layer via a
  real in-process cold-start (`create_app()` + `GET /health`, no `backends.toml`) — the TRULY-live
  homelab image boot remains a deployment observation, but the app-level behavior is confirmed.
- All checks driven against a real `ControlSettings` construction (operator path), not fixture-mocked.
