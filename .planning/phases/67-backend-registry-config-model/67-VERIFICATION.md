---
phase: 67-backend-registry-config-model
verified: 2026-07-04T00:34:41Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
---

# Phase 67: Backend Registry & Config Model Verification Report

**Phase Goal:** Operator can declare the full set of execution backends (and their S3 staging buckets) in `backends.toml` as the single source of truth; `cloud_target` + the flat `s3_*`/`kube_*`/`compute_*` fields are REMOVED with no back-compat shim (~10 call sites rewired to registry-derived reads); the all-local deploy keeps working unchanged with zero config edits via a zero-config implicit all-local registry (not a shim). Config-model-only: no dispatch/scheduler/protocol change this phase.
**Verified:** 2026-07-04T00:34:41Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Operator can declare a `backends:` list (id/kind/rank/cap); app boots and logs the effective registry (id/kind/rank/cap only) at startup | ✓ VERIFIED | `config_backends.py` `LocalBackend`/`ComputeBackend`/`KueueBackend` discriminated union (`Field(discriminator="kind")`); `config.py:525-533` `log_effective_registry()` emits `{id,kind,rank,cap}` projection only; wired into `controller.py:79` (`cfg.log_effective_registry()`) — confirmed live in `startup()`, not just defined. Regression test `test_startup_logs_effective_registry_secret_free` passes. |
| 2 | A misconfigured backend entry (kueue missing `[kube]`, compute missing `agent_ref`) fails fast at startup with the offending entry `id` in the message | ✓ VERIFIED | Live construction test: `KueueBackend(kind="kueue", id="my-cluster", rank=1, cap=2)` → `ValueError: backend 'my-cluster' (kind=kueue) requires a [kube] config table`; `ComputeBackend(...)` without `agent_ref` → `ValueError: backend 'my-compute' (kind=compute) requires an agent_ref`. `tests/shared/config/test_backend_registry.py` covers both id-tagged paths (part of 40 passing tests). |
| 3 | No `backends.toml` (no pointer) → implicit single `kind=local` registry, zero config edits; `cloud_target`/flat `s3_*`/`kube_*`/`compute_*` no longer exist; no back-compat shim | ✓ VERIFIED | Live construction with `PHAZE_BACKENDS_CONFIG_FILE` unset: `ControlSettings().backends == [LocalBackend(kind='local', id='local', rank=99, cap=1)]`, `cloud_enabled == False`. `grep -rn "cloud_target"` / `settings\.s3_(bucket|endpoint_url|access_key_id|secret_access_key|region|addressing_style)` / `settings\.kube_\*` / `compute_scratch_dir` (as a live field) across `src/phaze/` returns **zero** hits outside comments/docstrings referencing the old names historically. A present-but-empty `backends = []` TOML file was constructed live and raised `ValueError: backend registry resolved to empty — refusing to start (REG-04)` at `ControlSettings()` construction (fails at the module-level singleton import, i.e., truly at process boot). |
| 4 | Operator can declare an S3 bucket registry (shared/public vs cluster-specific), assign buckets to Kueue backends; cluster-specific bucket referenceable by ≤1 kueue backend; empty/unknown bucket set fails fast; flat global S3 config removed | ✓ VERIFIED | `BucketConfig` (`config_backends.py:174-217`) with `scope: Literal["shared","cluster-specific"]`, per-bucket http(s) SSRF-guarded `endpoint_url`, inline `*_file` creds. `ControlSettings._validate_registry` (`config.py:417-451`) enforces unknown-bucket-id fail-fast, empty-resolved-set fail-fast, and cluster-specific >1-referrer fail-fast (D-08/D-09), each naming the offending backend/bucket id. `tests/shared/config/test_bucket_registry.py` (part of 40 passing) exercises all three. |
| 5 | Per-backend secrets (kube tokens/kubeconfigs, S3 creds, agent tokens) resolve via the existing `<VAR>_FILE` convention, scoped per entry | ✓ VERIFIED | `config_backends._read_secret_file` + `_resolve_inline_secret_files` used by both `KubeConfig` (`kubeconfig_file`/`sa_token_file`) and `BucketConfig` (`access_key_id_file`/`secret_access_key_file`), verbatim-vs-strip rule mirrored from `config.py`'s existing env-`_FILE` resolver. `SECRET_FILE_FIELDS` on `ControlSettings` trimmed to control-plane secrets only (`database_url`, `redis_url`, `queue_url`, `openai_api_key`, `anthropic_api_key`) per REG-04/D-12 — per-backend secrets are no longer flat fields. `tests/shared/config/test_backend_secret_files.py` covers strip/verbatim/fail-fast (part of 40 passing). |

**Score:** 5/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/config_backends.py` | Discriminated-union submodels + KubeConfig/BucketConfig + secret helper + implicit-local factory | ✓ VERIFIED | 232 lines; `LocalBackend`/`ComputeBackend`/`KueueBackend`/`KubeConfig`/`BucketConfig` all present with per-variant `model_validator` fail-fast; `_default_local_registry()` factory present and wired as `ControlSettings.backends` default_factory. |
| `src/phaze/config.py` | Registry integration: TOML loader, cardinality validator, `cloud_enabled`, transitional accessors, secret-free log | ✓ VERIFIED | `_load_backend_registry` (before-validator, WR-02-fixed `.env`-aware path resolution), `_validate_registry` (after-validator), `cloud_enabled` property, `_single_non_local`/`active_cloud_kind`/`active_cap`/`active_compute_scratch_dir`/`active_kube`/`active_bucket` transitional accessors, `log_effective_registry()`. Flat `cloud_target`/`cloud_max_in_flight`/`compute_scratch_dir`/flat `s3_*`/`kube_*` fields and the 3 `_enforce_*_when_*` validators are gone (confirmed by grep + successful construction/tests). |
| `src/phaze/routers/pipeline.py`, `tasks/release_awaiting_cloud.py` | Class A/B/C rewire off `cloud_target` | ✓ VERIFIED | `cloud_enabled` gates routing/backfill (Class A); `active_cloud_kind`/`active_cap` fork staging cron (Class B); neutral `cloud_lane_kind` template context key (Class C), asserted by `test_dashboard_context_binds_cloud_lane_kind`. Zero `cloud_target` strings remain in either file. |
| `src/phaze/services/s3_staging.py`, `services/kube_staging.py` | Reads routed to `active_bucket`/`active_kube` | ✓ VERIFIED | `_staging_config()` reads `cfg.active_bucket`; `_kube_config()` reads `cfg.active_kube`; D-15 global TTL/part-size knobs (`s3_presign_put_ttl_sec` etc.) confirmed still read directly off `ControlSettings` (correctly NOT moved to per-bucket). |
| `src/phaze/routers/agent_s3.py`, `routers/agent_push.py`, `tasks/controller.py` | Callback + probe-gate rewire + startup registry log wiring | ✓ VERIFIED | `agent_push.py:122` builds scratch path from `active_compute_scratch_dir` (now safe post-WR-01 fix — `ComputeBackend` requires `scratch_dir`, so a `None` path can no longer be constructed); `controller.py` LocalQueue probe reads `active_cloud_kind` wrapped in a try/except (CR-01 fix) so a premature multi-cluster registry degrades gracefully instead of aborting boot; `log_effective_registry()` call confirmed present in `startup()`. |
| `.env.example`, `docs/configuration.md` | backends.toml surface documented; removed vars called out as breaking | ✓ VERIFIED | `.env.example:167-199` has an explicit `>>> BREAKING REMOVAL IN 2026.7.1 <<<` callout naming every removed flat var; `docs/configuration.md:89-123` documents the registry and marks the old flat-field table rows "Superseded... removed with no shim" as historical reference only. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `config_backends.py` inline `*_file` before-validator | `_read_secret_file(path, preserve_whitespace=...)` | shared whitespace helper | ✓ WIRED | `KubeConfig._resolve_inline_secret_files` / `BucketConfig._resolve_inline_secret_files` both call it. |
| `ControlSettings` TOML before-validator | `PHAZE_BACKENDS_CONFIG_FILE` → `tomllib.load` | Idiom-B `model_validator(mode="before")` | ✓ WIRED | `_load_backend_registry`, WR-02-fixed to resolve via `.env`-aware `_resolution_env` (confirmed by reading the current source, not the pre-fix version). |
| `controller.startup` | `settings.log_effective_registry()` | boot-time registry log | ✓ WIRED | Confirmed call site at `controller.py:79`, unconditional (runs regardless of role/registry shape). |
| `s3_staging` / `kube_staging` | `cfg.active_bucket` / `cfg.active_kube` | transitional accessor | ✓ WIRED | Both confirmed by direct source read; TTL/part-size knobs correctly NOT rerouted (D-15). |
| `agent_push.report_pushed` | `settings.active_compute_scratch_dir` | transitional accessor | ✓ WIRED | Confirmed at `agent_push.py:122`; `ComputeBackend.scratch_dir` is now a required field (WR-01 fix), so this can no longer resolve to a broken `"None/..."` path. |

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
|-------------|-----------------|-------------|--------|----------|
| REG-01 | 67-01, 67-02 | Declarative `backends:` list replacing `cloud_target` Literal | ✓ SATISFIED | Discriminated union + `ControlSettings.backends` field, live-constructed and tested. |
| REG-02 | 67-01 | Per-kind fail-fast validation, consolidating 3 `_enforce_*_when_*` validators into one per-entry validator | ✓ SATISFIED | Per-variant `model_validator`s on `ComputeBackend`/`KueueBackend`; old 3 validators confirmed removed from `config.py`. |
| REG-03 | 67-01 | Per-backend secrets via `<VAR>_FILE` convention, scoped per entry | ✓ SATISFIED | Inline `*_file` TOML mechanism, distinct from but consistent with the existing env-`_FILE` convention; tested. |
| REG-04 | 67-02, 67-03, 67-04, 67-05, 67-06 | Removal of `cloud_target`/flat fields/3 validators, no shim, ~10 call sites rewired, implicit-local default, empty-registry fail-fast, secret-free startup log | ✓ SATISFIED | All flat fields/validators confirmed removed via grep + source read; every planned call site (pipeline.py, release_awaiting_cloud.py, s3_staging.py, kube_staging.py, agent_s3.py, agent_push.py, controller.py) rewired and tested; empty-registry fail-fast reproduced live. |
| REG-05 | 67-01, 67-02 | S3 staging-bucket registry (shared/public vs cluster-specific), fail-fast cardinality/reference validation | ✓ SATISFIED | `BucketConfig` + container cross-entry validator (D-08/D-09) confirmed in source and covered by `test_bucket_registry.py`. |

No orphaned requirements — REQUIREMENTS.md maps only REG-01..05 to Phase 67 and all five appear in at least one plan's `requirements:` frontmatter. (Note: REQUIREMENTS.md's traceability table still shows all five as "Pending" status — a documentation bookkeeping lag, not a code gap; expected to be updated at milestone/phase close.)

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `docker-compose.yml` | 25, 52 | `PHAZE_CLOUD_TARGET=${PHAZE_CLOUD_TARGET:-local}` still passed as an env var to `api`/`worker`, with comments describing it as a live "control-plane-only, ControlSettings" field | ℹ️ INFO (non-blocking) | `BaseSettings.model_config` sets `extra="ignore"`, so this is silently dropped at construction — functionally inert, does not break the "zero config edits" claim or reintroduce a shim. It IS stale/misleading: an operator reading `docker-compose.yml` would believe `cloud_target` is still a live knob. Not in any plan's `files_modified` list (only `.env.example`/`docs/configuration.md` were in scope for Wave 4's doc sweep), so this is a pre-existing gap in the removal wave's blast-radius, not a regression from what was planned. Recommend a follow-up cleanup (Phase 68 or a quick fix) to delete these two lines. |

No debt markers (`TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER`) found in any of the 9 files touched by this phase's plans. No stub patterns (empty handlers, hardcoded empty returns) found.

### Code Review Disposition (67-REVIEW.md)

- **CR-01 (critical, boot-safety):** FIXED on branch (commit `7ce7fef`) — `controller.startup`'s `active_cloud_kind` read wrapped in try/except so a schema-valid `>1 non-local` registry degrades to "skip Kueue probe" instead of aborting boot. Regression test `test_multi_backend_registry_does_not_abort_boot` passes. The reviewer's alternative fix (reject `>1 non-local` at construction) was correctly NOT taken, since it would make the D-09 multi-cluster bucket-sharing validation dead code before the milestone needs it.
- **CR-01b (residual, lower severity):** Explicitly deferred to Phase 69 per `deferred-items.md` — `pipeline.py`/`agent_s3.py`/`release_awaiting_cloud.py` still raise (rather than gracefully degrade) on the same premature multi-cluster registry at non-boot-fatal call sites. This only triggers if an operator configures >1 non-local backend before Phase 69 ships dispatch support — out of scope for the all-local deploy this phase must preserve, and reasonably deferred given Phase 69's transitional-accessor removal makes the fix moot there.
- **WR-01 (compute scratch_dir):** FIXED (commit `7ce7fef` + `fed0c92` for test fixtures) — `ComputeBackend` now requires `scratch_dir`, verified live (see truths table).
- **WR-02 (.env pointer):** FIXED (commit `7ce7fef`) — `_load_backend_registry` now resolves `PHAZE_BACKENDS_CONFIG_FILE` via the `.env`-aware `_resolution_env` map, verified by reading current source.
- **IN-01/IN-02 (info):** Not fixed (extra="forbid" / dead-branch cleanup) — both are info-severity, non-blocking, and appropriately left open.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Implicit-local zero-config default | `ControlSettings()` with no `PHAZE_BACKENDS_CONFIG_FILE` | `[LocalBackend(kind='local', id='local', rank=99, cap=1)]`, `cloud_enabled=False` | ✓ PASS |
| Empty-registry fail-fast | `ControlSettings()` with `backends.toml` containing `backends = []` | `ValidationError: backend registry resolved to empty — refusing to start (REG-04)` | ✓ PASS |
| Kueue entry missing `[kube]` fails fast, id-tagged | `KueueBackend(kind="kueue", id="my-cluster", ...)` w/o `kube` | `ValueError: backend 'my-cluster' (kind=kueue) requires a [kube] config table` | ✓ PASS |
| Compute entry missing `agent_ref` fails fast, id-tagged | `ComputeBackend(kind="compute", id="my-compute", ...)` w/o `agent_ref` | `ValueError: backend 'my-compute' (kind=compute) requires an agent_ref` | ✓ PASS |
| Full targeted test suite (all rewired call sites) | `pytest` across 13 test files touched by this phase's plans | 244 passed, 0 failed | ✓ PASS |
| Static analysis | `ruff check` (9 touched files) + `uv run mypy .` | ruff: all checks passed; mypy: no issues in 188 source files | ✓ PASS |

### Human Verification Required

None. This phase is config-model-only (no dispatch/scheduler/protocol/UI-behavior change); every truth is grep/construction/test verifiable, and the one presentation-layer change (`cloud_lane_kind` template context) is covered by an automated assertion (`test_dashboard_context_binds_cloud_lane_kind`) rather than requiring visual inspection.

### Gaps Summary

No blocking gaps. All 5 ROADMAP success criteria and all 5 REG requirements are verified against live code (not just SUMMARY claims) via direct construction, grep sweeps for the removed flat fields, and the full targeted regression suite (244 tests across the 13 files this phase's plans touched, plus mypy/ruff clean). The 3 code-review defects (1 critical, 2 warning) found at the gate were fixed on the branch and are confirmed fixed in the current source, with regression tests locking each fix. One non-blocking informational item was found during verification and was NOT part of any plan's declared scope: `docker-compose.yml` still passes a now-inert `PHAZE_CLOUD_TARGET` env var with stale comments — harmless (silently ignored by `extra="ignore"`) but worth a follow-up cleanup pass.

---

_Verified: 2026-07-04T00:34:41Z_
_Verifier: Claude (gsd-verifier)_
