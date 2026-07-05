---
phase: 73-per-agent-dispatch-liveness-scratch-failure-isolation
plan: 01
subsystem: cloud-compute-dispatch
tags: [backends, push-pipeline, config-registry, schema, dispatch]
requires:
  - ComputeBackend discriminated-union submodel (Phase 67)
  - PushFilePayload / _enqueue_push_file / ComputeAgentBackend.dispatch (Phase 68/72)
  - s3_staging.resolve_bucket_config inverse-lookup template (Phase 70)
provides:
  - ComputeBackend.push_host (required, id-tagged fail-fast) + optional ssh_user
  - PushFilePayload.dest_host / dest_scratch_dir / dest_ssh_user (validated, non-secret)
  - services.backends.resolve_compute_backend (D-06 authoritative backend_id -> ComputeBackend)
  - destination-stamping ComputeAgentBackend.dispatch (D-02 record-don't-rederive)
affects:
  - Plan 02 (fileserver rsync argv reads the recorded dest_*)
  - Plan 03 (/pushed + /mismatch callbacks resolve scratch via resolve_compute_backend)
tech-stack:
  added: []
  patterns:
    - union-safe getattr+cast idiom for reading BackendConfig-union fields (mirrors _agent_ref/_kube)
    - pure ORM-free inverse-lookup helper (mirrors resolve_bucket_config)
    - pydantic field_validator argv-injection defense-in-depth (mirrors _original_path_absolute)
key-files:
  created: []
  modified:
    - src/phaze/config_backends.py
    - src/phaze/schemas/agent_tasks.py
    - src/phaze/services/backends.py
decisions:
  - dest_* fields are OPTIONAL (defaulted None) this plan, not pydantic-required, so the Plan-03-owned /mismatch producer keeps constructing until it is wired (interface-first, minimal blast radius); validators still enforce on any provided value
  - dispatch reads the destination off the bound self.config (record-don't-rederive originates at the stamp) and does NOT re-lookup via resolve_compute_backend
metrics:
  tasks: 3
  source-files-modified: 3
  test-files-modified: 12
  completed: 2026-07-05
---

# Phase 73 Plan 01: Per-Agent Dispatch Destination Contracts Summary

Interface-first plan defining the three contracts Phase 73 consumes: `ComputeBackend.push_host` (+ optional `ssh_user`), the validated per-file `PushFilePayload.dest_*` destination, and the `resolve_compute_backend` inverse-lookup — plus the dispatch-side destination stamp (record-don't-rederive), the verbatim twin of Phase 70's `KueueBackend` `staging_bucket` stamp.

## What Was Built

**Task 1 — `ComputeBackend.push_host` (D-01).** Added `push_host` (optional at the type level so the validator raises an id-tagged message, then required via a new `_require_dispatch_fields` clause mirroring the `scratch_dir` guard) and an optional `ssh_user` (no fail-fast). A compute entry missing `push_host` now fails construction with `backend '<id>' (kind=compute) requires a push_host`.

**Task 2 — `PushFilePayload.dest_*` (T-73-01/D-03).** Added `dest_host`, `dest_scratch_dir`, `dest_ssh_user` (optional this plan) with argv-injection defense-in-depth validators: `dest_scratch_dir` must be absolute (same shape as `_original_path_absolute`); `dest_host` / `dest_ssh_user` reject whitespace + shell metacharacters (they land in the ssh remote spec). `extra="forbid"` preserved; non-secret only (no `SecretStr`).

**Task 3 — `resolve_compute_backend` (D-06) + dispatch stamp (D-02).** Added the pure ORM-free `resolve_compute_backend(cfg, backend_id) -> ComputeBackend | None` (mirrors `resolve_bucket_config`; `None`/unknown/non-compute → `None`). Widened `_enqueue_push_file` with keyword-only `dest_host/dest_scratch_dir/dest_ssh_user` and stamped them onto the payload. `ComputeAgentBackend.dispatch` now reads the destination off the bound `self.config` via a union-safe `getattr`+`cast` helper (`_destination`, mirroring `_agent_ref()`/`_kube()`) and passes it in. The one-row-per-file `cloud_job` upsert + `FileState.PUSHING` flip stay byte-identical (D-05: no migration, no schema change).

## Deviations from Plan

### Auto-fixed Issues (Rule 3 — blocking, all directly caused by the required-field changes)

**1. [Rule 3 - Blocking] Suite-wide compute fixtures updated for the required `push_host`**
- **Found during:** Task 1
- **Issue:** Making `push_host` required breaks every real `ComputeBackend` construction across the suite (TOML registries parsed by `ControlSettings`, direct constructors, the `_compute` factory).
- **Fix:** Added `push_host` to all ~23 real `ComputeBackend` fixtures: `test_bucket_registry.py`, `test_backend_registry.py`, `test_routing.py`, `test_pipeline.py`, `test_backends.py`, `test_compute_binding_golden.py`, `test_agent_push.py`, `test_agent_s3.py`.
- **Commit:** d8cf4f52

**2. [Rule 3 - Blocking] `test_push_file_payload_field_set` updated for the new dest fields**
- **Found during:** Task 2
- **Issue:** An exact-field-set assertion (`== {file_id, original_path, file_type, agent_id}`) fails once the three `dest_*` fields exist.
- **Fix:** Extended the expected set to include `dest_host/dest_scratch_dir/dest_ssh_user`.
- **Commit:** 02c73df2

**3. [Rule 3 - Blocking] Duck-typed compute stubs given `push_host`/`scratch_dir`**
- **Found during:** Task 3
- **Issue:** `ComputeAgentBackend.dispatch` now reads `push_host`+`scratch_dir` off `self.config`; the `SimpleNamespace` compute stubs in the staging-cron + dispatch-snapshot cells lacked them, so dispatch raised.
- **Fix:** Added `push_host`/`scratch_dir`/`ssh_user` to the compute stubs in `test_staging_cron.py` (`_StubCfg` default + two multi-backend cells) and `test_dispatch_snapshot.py` (`_StubCfg` default).
- **Commit:** dfb841d3

### Design decision (interface-first, not a defect)

`dest_host`/`dest_scratch_dir` were made **optional** (defaulted `None`), not pydantic-required. The plan's Task 2 behavior states "Existing four-field construction still validates" and "confirm both build sites supply them in Task 3 / **Plan 03**" — the `/mismatch` re-drive producer (`agent_push.py`) is wired in Plan 03. Making the fields required now would break that producer's four-field construction before Plan 03 lands. Optional-with-validators keeps `agent_push.py` untouched (minimal blast radius) while the dispatch producer supplies the fields (Task 3) and the security validators still fire on any provided value.

## Threat Model Coverage

| Threat ID | Disposition | Realized |
|-----------|-------------|----------|
| T-73-01 (dest_host/scratch tampering → ssh spec) | mitigate | `dest_scratch_dir` absolute + `dest_host`/`dest_ssh_user` whitespace/metachar validators; `extra="forbid"` preserved |
| T-73-02 (key material leak into payload) | mitigate | D-03: only non-secret host/scratch/user in `dest_*`; no `SecretStr` added |
| T-73-03 (compute entry with no push_host) | mitigate | id-tagged `_require_dispatch_fields` fail-fast at construction |
| T-73-SC (dependency installs) | accept | zero new dependencies; `pyproject` untouched |

No new security surface beyond the plan's threat register. No threat flags.

## Verification

- Plan verification: `uv run pytest tests/analyze/services/test_backends.py tests/analyze/core/test_push_pipeline.py tests/shared/config/test_bucket_registry.py -x -q` → **89 passed**.
- Whole-tree `uv run ruff check . && uv run ruff format --check . && uv run mypy .` → **clean** (196 source files, 483 files formatted).
- Broad regression (`tests/analyze tests/agents tests/shared`) → **1733 passed, 0 failures** (11 setup errors were DB-state contention from concurrent DB suites; re-confirmed green in isolation: **123 passed**).

## Known Stubs

None — every field/value introduced is real and wired.

## Self-Check: PASSED

- All modified source files exist on disk.
- All six task commits (3 RED + 3 GREEN) exist in git history.
- Key symbols present: `resolve_compute_backend`, `push_host`, `dest_scratch_dir`.
