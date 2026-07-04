---
phase: 67-backend-registry-config-model
plan: 04
subsystem: infra
tags: [s3, kueue, aioboto3, kr8s, config, secretstr, transitional-accessor, registry]

# Dependency graph
requires:
  - phase: 67-backend-registry-config-model
    plan: 02
    provides: "ControlSettings transitional ≤1-non-local accessors (active_bucket / active_kube) + shared backends_toml_env conftest fixture"
provides:
  - "s3_staging.py reads bucket identity/creds via the transitional active_bucket accessor (not flat s3_* fields)"
  - "kube_staging.py reads cluster connection/manifest config via the transitional active_kube accessor (not flat kube_* fields)"
  - "kept-global S3 tuning knobs (s3_presign_*_ttl_sec, s3_lifecycle_ttl_days) still read from ControlSettings (D-15)"
  - "staging-service seam tests driven off a one-kueue-backend backends.toml via backends_toml_env"
affects: [67-06, 68-backend-protocol, 69-scheduler, 70-multi-kueue]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "dispatch-service config reads routed through a ControlSettings @property reduction accessor (active_bucket/active_kube) that raises on multiplicity — the seam Phase 68 replaces with a Backend protocol"
    - "helper factories (_client, _api, build_job_manifest) take the resolved per-entry submodel (BucketConfig/KubeConfig) instead of the whole ControlSettings — narrows the read surface to the active backend"
    - "registry-driven seam tests: a single kueue backend + one bucket in backends.toml (via backends_toml_env) replaces flat PHAZE_S3_*/duck-typed kube_* env"

key-files:
  created: []
  modified:
    - src/phaze/services/s3_staging.py
    - src/phaze/services/kube_staging.py
    - tests/analyze/services/test_s3_staging.py
    - tests/analyze/services/test_kube_staging.py
    - tests/analyze/services/test_cloud_staging.py

key-decisions:
  - "_staging_config() now returns (cfg, bucket) and _kube_config() returns the KubeConfig directly — mirrors the ≤1-non-local reduction: the bucket/kube identity comes from the registry accessor while the kept-global S3 TTL/part-size knobs (D-15) stay on ControlSettings"
  - "_client takes a BucketConfig and _api/build_job_manifest take a KubeConfig (was ControlSettings) — narrows each factory to the active backend's per-entry submodel; single-arg _client keeps the existing monkeypatch seam (lambda _cfg: FakeCM()) working"
  - "fail-loud guards + missing-manifest-field messages now name the backends.toml [kube] config / active backend bucket instead of the removed PHAZE_* env vars"
  - "seam tests build a real config_backends.KubeConfig on the stub's active_kube attribute (not a duck-typed kube_* namespace) so the accessor field names are exercised end-to-end"

patterns-established:
  - "Wave-3 Class-B call-site rewire: replace flat cfg.<flat>_* reads with a local `x = cfg.active_<x>` bind + None-guard at each dispatch entry point, marked `# TRANSITIONAL — Phase 68`"

requirements-completed: [REG-04]

# Metrics
duration: 15min
completed: 2026-07-03
---

# Phase 67 Plan 04: Staging-Service Dispatch Rewire Summary

**The two DISPATCH services now read the active backend off the Plan-02 transitional accessors: `s3_staging.py` resolves bucket identity/creds through `cfg.active_bucket` and `kube_staging.py` resolves cluster connection + Job-manifest config through `cfg.active_kube` (the larger ~35-read blast radius the `cloud_target` map understated), with the kept-global S3 TTL/part-size tuning knobs (D-15) still on `ControlSettings`; their seam tests are driven off a one-kueue-backend `backends.toml` and the full three-file suite is green with the flat `s3_*`/`kube_*` fields still present (removed in Plan 06).**

## Performance

- **Duration:** ~15 min
- **Tasks:** 2 (auto)
- **Files modified:** 5 (0 created)

## Accomplishments
- `s3_staging.py`: `_staging_config()` returns `(cfg, bucket)` where `bucket = cfg.active_bucket` (fail-loud when None); `_client` takes a `BucketConfig`; every presign/upload/complete/abort/delete/lifecycle read (`bucket.bucket`, `endpoint_url`, `region`, `addressing_style`, `access_key_id`, `secret_access_key`) resolves through it. Flat-field grep == 0.
- Kept-global S3 knobs preserved: `cfg.s3_presign_put_ttl_sec`, `cfg.s3_presign_get_ttl_sec`, `cfg.s3_lifecycle_ttl_days` still read from `ControlSettings` (D-15), each tagged as a kept-global knob.
- `kube_staging.py`: `_kube_config()` returns the active `KubeConfig` via `cfg.active_kube` (fail-loud when None / api_url / namespace / local_queue unset); `_api` and `build_job_manifest` take a `KubeConfig`; all 11 flat `kube_*` reads (api_url, namespace, local_queue, sa_token, job_image, cpu_request, memory_request, workload_api_version, ca_secret_name, env_configmap_name, env_secret_name) resolve through it. Flat-field grep == 0.
- Fail-loud guards + the missing-manifest-field message now name the `backends.toml` `[kube]` config / active backend bucket instead of the removed `PHAZE_*` env vars.
- Both accessor reads carry a `# TRANSITIONAL — Phase 68` marker (1 in each service).
- Seam tests rewired off the registry: `test_s3_staging` + `test_cloud_staging` `s3_env` fixtures write a one-kueue-backend + one-bucket `backends.toml` (endpoint = moto server URL) via the shared `backends_toml_env` fixture (`test_cloud_staging` keeps `PHAZE_S3_MULTIPART_PART_SIZE_BYTES`, a kept-global knob); `test_kube_staging`'s `_StubCfg` now presents a real `config_backends.KubeConfig` on `active_kube`, and the missing-field parametrize + `build_job_manifest` calls use the accessor field names.

## Task Commits

| Task | Name | Commit |
| ---- | ---- | ------ |
| 1 | Rewire s3_staging.py bucket identity/creds to active_bucket | `6305f6a` |
| 2 | Rewire kube_staging.py connection/manifest reads to active_kube | `240bb07` |

## Files Created/Modified
- `src/phaze/services/s3_staging.py` (modified) — `_staging_config` → `(cfg, bucket)`; `_client(bucket)`; all bucket reads via `active_bucket`; TTL knobs kept global; `BucketConfig` imported under TYPE_CHECKING.
- `src/phaze/services/kube_staging.py` (modified) — `_kube_config` → `KubeConfig`; `_api(kube)`; `build_job_manifest(file_id, kube)`; all manifest/connection reads via `active_kube`; `KubeConfig` imported under TYPE_CHECKING.
- `tests/analyze/services/test_s3_staging.py` (modified) — `s3_env` fixture drives a registry `backends.toml`; missing-config test asserts implicit-local → `active_bucket is None` fail-loud.
- `tests/analyze/services/test_kube_staging.py` (modified) — `_StubCfg` presents a real `KubeConfig` on `active_kube`; accessor-field parametrize + `build_job_manifest(fid, cfg.active_kube)`.
- `tests/analyze/services/test_cloud_staging.py` (modified) — `s3_env` fixture drives a registry `backends.toml` + keeps the global part-size knob.

## Deviations from Plan

None — plan executed as written. One pre-commit `ruff-format` re-wrap of a long fail-loud message in `kube_staging.py` (auto-applied, re-staged, no behavior change).

## Threat Surface
All register threats mitigated as planned: T-67-04-01 (rewire-only, NO Backend protocol/type introduced), T-67-04-02/04 (bucket/cluster resolution goes through the Plan-02 accessors that raise on multiplicity, never silently pick), T-67-04-03 (creds stay `SecretStr` on `active_bucket`/`active_kube`; `.get_secret_value()` only at client construction, never logged). T-67-04-SC: zero new dependencies. No new security-relevant surface beyond the threat model.

## Known Stubs
None.

## Verification
- `uv run pytest tests/analyze/services/test_s3_staging.py tests/analyze/services/test_kube_staging.py tests/analyze/services/test_cloud_staging.py` → 44 passed (against ephemeral Postgres/Redis on 5433/6380).
- `grep` flat reads == 0 for both services; kept-global S3 knobs still present (3); `active_bucket`/`active_kube` present; `TRANSITIONAL` marker in each service.
- `uv run mypy src/phaze/services/s3_staging.py src/phaze/services/kube_staging.py` → clean; `uv run ruff check` on all touched files → clean.
- No external callers of the changed helper signatures (`_client`, `_api`, `build_job_manifest`, `_staging_config`, `_kube_config`) outside the two service modules.
- Staging-adjacent tests (submit_cloud_job, controller_startup_localqueue, agent_s3, staging_cron, inline_delete) monkeypatch the public service functions as spies and never touch the rewired internals — unaffected.

## Issues Encountered
- `tests/analyze/tasks/test_reconcile_cloud_jobs.py` (a spy-driven, DB-heavy file NOT in this plan's scope) throws non-deterministic asyncpg setup connection errors under colima VM pressure — a different disjoint subset fails each run and every named test passes in isolation. This is the documented "Local full-suite colima flake" (infra, not a regression from this rewire; the file never exercises the changed code paths). Out of scope per the scope boundary; logged, not fixed.

## Next Phase Readiness
- Plan 06 can remove the flat `cloud_target`/`s3_*`/`kube_*`/`compute_scratch_dir` fields (D-12) now that both dispatch services (this plan) and the Wave-3 Class-A gates (Plans 03/05) read the registry.
- Phase 68 replaces these `active_*` transitional accessors with the Backend protocol; the `# TRANSITIONAL — Phase 68` markers in both services flag the exact reduction seams.
- No blockers.

## Self-Check: PASSED

- `src/phaze/services/s3_staging.py`, `src/phaze/services/kube_staging.py`, and the three test files all present on disk with the rewire applied.
- Task commits `6305f6a` and `240bb07` present in git history.

---
*Phase: 67-backend-registry-config-model*
*Completed: 2026-07-03*
