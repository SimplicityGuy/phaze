---
phase: 67-backend-registry-config-model
plan: 01
subsystem: infra
tags: [pydantic, pydantic-settings, discriminated-union, config, secretstr, ssrf, toml]

# Dependency graph
requires:
  - phase: 55-cloud-target-selector
    provides: "flat cloud_target/s3_*/kube_*/compute_scratch_dir fields + _enforce_*_when_* validators + _validate_s3_endpoint_url (the shapes these submodels absorb per D-13/D-07)"
provides:
  - "src/phaze/config_backends.py — the typed backends.toml registry schema"
  - "LocalBackend/ComputeBackend/KueueBackend discriminated union (BackendConfig, Field(discriminator=\"kind\"))"
  - "KubeConfig + BucketConfig per-entry submodels (supersets of the former flat kube_*/s3_* blocks)"
  - "per-variant id-tagged fail-fast validators (compute→agent_ref, kueue→kube)"
  - "per-bucket endpoint_url http(s) SSRF field_validator (REG-05/V5)"
  - "shared _read_secret_file helper + inline *_file before-validators (D-04/D-06)"
  - "_default_local_registry() implicit-local factory (D-03)"
affects: [67-02, 67-03, 67-04, 68-backend-protocol, 70-multi-kueue]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "pydantic v2 discriminated union over a Literal kind tag with per-variant model_validator(mode=after) raising self.id-tagged messages"
    - "inline *_file secret resolution as a mode=before model_validator distinct from env <VAR>_FILE, sharing one strip-vs-verbatim helper"
    - "per-submodel field_validator lifting the SSRF endpoint_url guard to per-bucket scope"

key-files:
  created:
    - src/phaze/config_backends.py
    - tests/shared/config/test_backend_registry.py
    - tests/shared/config/test_backend_secret_files.py
  modified: []

key-decisions:
  - "rank bounded ge=0/lt=1000, cap bounded gt=0/lt=1000 (cap must allow ≥1 in-flight) — fail-fast per T-67-01-04"
  - "agent_ref and kube typed Optional so the per-variant validator can emit an id-tagged message instead of pydantic's index-tagged 'Field required' (Pitfall 3)"
  - "k8s object-name defaults (ca_secret_name/env_secret_name) use Field(default=...) to avoid ruff S105, mirroring config.py"

patterns-established:
  - "config_backends.py is the clean import target for downstream plans' submodels (keeps config.py from ballooning)"
  - "_read_secret_file(path, *, preserve_whitespace) is the single strip-vs-verbatim rule both env-_FILE (Plan 02) and inline TOML *_file paths adopt"

requirements-completed: [REG-01, REG-02, REG-03, REG-05]

# Metrics
duration: 20min
completed: 2026-07-03
---

# Phase 67 Plan 01: Backend Registry Config Model Summary

**New `config_backends.py` module: a pydantic v2 discriminated-union registry (Local/Compute/Kueue) with id-tagged per-variant fail-fast, KubeConfig/BucketConfig field supersets, a per-bucket endpoint_url SSRF guard, and a shared inline `*_file` secret reader — fully unit-tested in isolation, zero removals.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-07-03T22:33Z
- **Completed:** 2026-07-03T22:53Z
- **Tasks:** 3
- **Files modified:** 3 (all created)

## Accomplishments
- `BackendConfig` discriminated union over `kind` with bounded `rank`/`cap`; unknown `kind` and out-of-range values rejected at construction (REG-01).
- Per-variant `model_validator(mode="after")` on ComputeBackend/KueueBackend raising `backend {self.id!r} …` — id-tagged fail-fast replacing the three flat `_enforce_*_when_*` validators (REG-02).
- `KubeConfig` (full superset of the flat `kube_*` block, D-13) and `BucketConfig` (id/scope/endpoint_url/bucket + region/addressing_style, D-07) with `SecretStr` credential fields (T-67-01-02).
- Per-bucket `endpoint_url` http(s)+netloc SSRF `field_validator` lifted from `config.py`'s `_validate_s3_endpoint_url` (REG-05 / V5, T-67-01-01).
- Shared `_read_secret_file` helper + `_resolve_inline_secret_files` before-validators: kubeconfig verbatim, tokens/access-keys stripped, missing path fails fast (REG-03, D-04/D-06).
- `_default_local_registry()` implicit-local factory returning the single rank-99 cap-1 local backend (D-03).

## Task Commits

TDD tasks — each with a failing `test(...)` (RED) then passing `feat(...)` (GREEN) commit:

1. **Task 1: Backend union submodels + per-variant fail-fast + factory** — `3dbb10d` (test) → `56c93c1` (feat)
2. **Task 2: KubeConfig + BucketConfig + per-bucket SSRF guard** — `ce0bda6` (test) → `405ddc4` (feat)
3. **Task 3: shared `_read_secret_file` + inline `*_file` before-validators** — `af80788` (test) → `63ab548` (feat)

## Files Created/Modified
- `src/phaze/config_backends.py` (224 lines) — the registry schema: submodels, union, validators, secret helper, factory.
- `tests/shared/config/test_backend_registry.py` — REG-01/02/05 submodel parse + id-tagged fail-fast + bucket parse/scope/SSRF + SecretStr cases.
- `tests/shared/config/test_backend_secret_files.py` — REG-03 inline `*_file` strip/verbatim/fail-fast cases.

## Decisions Made
- `rank` bounded `ge=0, lt=1000`; `cap` bounded `gt=0, lt=1000` (cap must permit ≥1 in-flight). Both fail fast on out-of-range (T-67-01-04). Bounds are project-conventional (mirroring config.py bounded ints); exact ceiling is discretionary.
- `agent_ref: str | None` and `kube: KubeConfig | None` are Optional at the type level purely so the per-variant `model_validator` can raise the operator-facing **id-tagged** message (Pitfall 3) rather than pydantic's index-path "Field required".
- Directly-provided secret values win over their `*_file` sibling (mirrors config.py precedence); `*_file` keys are popped so they never reach field validation.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] ruff S106/S105 on secret-looking literals**
- **Found during:** Task 2 (commit was blocked by the pre-commit ruff hook)
- **Issue:** String-literal kwargs on secret-looking names (`sa_token=…`, `secret_access_key=…`) tripped ruff S106 in the test file, and k8s object-name defaults (`ca_secret_name=…`, `env_secret_name=…`) tripped S105 in `config_backends.py` (src is not in the tests S105 ignore set).
- **Fix:** Moved test literals into module constants (tests ignore S105 on assignments); wrapped the two src defaults in `Field(default=…)` to match `config.py`'s own `kube_*_name` house style.
- **Files modified:** src/phaze/config_backends.py, tests/shared/config/test_backend_registry.py
- **Verification:** `uv run ruff check` clean; `uv run pytest` 25/25 green; pre-commit hooks pass.
- **Committed in:** `405ddc4` (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking).
**Impact on plan:** Lint-only fix, no behavior change. No scope creep.

## Issues Encountered
- A Task 2 commit initially appeared to "succeed" (truncated hook output) but the ruff hook had failed and nothing was committed. Re-ran with visible hook output, saw the S105/S106 failures, fixed, and re-committed. Verified with `git log`/`git status` that the commit landed.

## User Setup Required
None — no external service configuration required (pure config-model code; the live all-local deploy is unaffected per D-03).

## Next Phase Readiness
- Downstream Wave-1+ plans (67-02 onward) can now `from phaze.config_backends import …` the submodels, union, factory, and `_read_secret_file` helper.
- Plan 67-02 owns: wiring `backends`/`buckets` fields onto `ControlSettings`, the tomllib env-pointer loader (Idiom B), the container `model_validator` (empty-registry fail-fast, bucket cardinality D-08/D-09), and the transitional accessors (D-14).
- No blockers. This plan added no removals; the full config suite remains green (111 passed).

## Verification
- `uv run pytest tests/shared/config/test_backend_registry.py tests/shared/config/test_backend_secret_files.py -x` → 25 passed.
- `uv run pytest tests/shared/config/` → 111 passed (additive, nothing regressed).
- `uv run mypy src/phaze/config_backends.py` → clean.
- `grep -q "discriminator=.kind"` → present; no `SECRET_FILE_FIELDS` mutation in the module.

## Self-Check: PASSED

All 3 created files exist on disk; all 6 task commits + the SUMMARY commit are present in git history.

---
*Phase: 67-backend-registry-config-model*
*Completed: 2026-07-03*
