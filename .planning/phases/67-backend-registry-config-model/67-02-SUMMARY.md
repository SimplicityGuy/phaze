---
phase: 67-backend-registry-config-model
plan: 02
subsystem: infra
tags: [pydantic, pydantic-settings, config, tomllib, discriminated-union, structlog, secretstr]

# Dependency graph
requires:
  - phase: 67-backend-registry-config-model
    plan: 01
    provides: "config_backends typed submodels (BackendConfig union, KubeConfig/BucketConfig, _default_local_registry, _read_secret_file)"
provides:
  - "ControlSettings.backends/buckets fields loaded from backends.toml via PHAZE_BACKENDS_CONFIG_FILE (Idiom-B tomllib before-validator)"
  - "implicit-local default (absent file → single kind=local backend, D-03)"
  - "_validate_registry container model_validator (empty fail-fast REG-04, bucket-ref/empty-set D-08, scope cardinality D-09)"
  - "cloud_enabled derived gate + transitional ≤1-non-local accessors (active_cloud_kind/active_cap/active_compute_scratch_dir/active_kube/active_bucket)"
  - "log_effective_registry secret-free id/kind/rank/cap projection"
  - "shared backends_toml_env conftest fixture for Wave 3 consumers"
affects: [67-03, 67-04, 67-05, 67-06, 68-backend-protocol, 69-scheduler, 70-multi-kueue]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "stdlib tomllib loaded in a model_validator(mode=before) keyed on an env pointer (Idiom B), injecting backends/buckets into data — single source, no env/JSON override (Pitfall 6)"
    - "registry-derived @property gate (cloud_enabled) + ≤1-non-local transitional reduction accessors that raise (never silently pick) on multiplicity"
    - "secret-free structlog projection: log a {id,kind,rank,cap} dict list, never a whole model/SecretStr/mount path"

key-files:
  created:
    - tests/shared/config/test_bucket_registry.py
  modified:
    - src/phaze/config.py
    - tests/conftest.py

key-decisions:
  - "Idiom B chosen (explicit tomllib before-validator) over a TomlConfigSettingsSource — keeps env-pointer + absent-file + implicit-local logic in one visible place, mirrors _resolve_secret_files (RESEARCH RESOLVED Q1)"
  - "present file → TOML is authoritative via parsed.get(..., []); a file declaring only [[buckets]] resolves backends to present-but-empty [] which _validate_registry fails fast (distinguishes absent→implicit-local from present-empty→fail-fast, Pitfall 2)"
  - "_read_secret_file adopted in _resolve_secret_files with a ValueError re-wrap so the operator-facing message still names the <VAR>_FILE var (existing test_secret_file_resolution asserts on it) while centralizing the strip-vs-verbatim rule (D-06)"
  - "KubeConfig imported under TYPE_CHECKING (only used in the active_kube return annotation); BackendConfig/BucketConfig/ComputeBackend/KueueBackend stay runtime imports (pydantic field resolution + isinstance)"

patterns-established:
  - "backends_toml_env conftest fixture is the shared registry-construction seam Wave-3 call-site rewires build their assertions on"

requirements-completed: [REG-01, REG-04, REG-05]

# Metrics
duration: 25min
completed: 2026-07-03
---

# Phase 67 Plan 02: Backend Registry Config-Model Integration Summary

**`ControlSettings` now carries the Plan-01 typed registry ADDITIVELY: a `backends`/`buckets` pair loaded from `backends.toml` via an Idiom-B stdlib-`tomllib` before-validator keyed on `PHAZE_BACKENDS_CONFIG_FILE`, an implicit single kind=local backend when no file is present, a container `model_validator` enforcing whole-registry invariants (non-empty, resolvable bucket sets, scope cardinality), a registry-derived `cloud_enabled` gate + the `# TRANSITIONAL — Phase 68` ≤1-non-local accessors, and a secret-free startup-log projection — `cloud_target` and every flat field untouched, full config suite green.**

## Performance

- **Duration:** ~25 min
- **Tasks:** 3 (all TDD: RED `test(...)` → GREEN `feat(...)`)
- **Files modified:** 3 (1 created, 2 modified)

## Accomplishments
- `backends: list[BackendConfig]` / `buckets: list[BucketConfig]` fields on `ControlSettings`, the registry validating per-variant through the Plan-01 discriminated union (REG-01).
- `_load_backend_registry` `model_validator(mode="before")` (Idiom B): reads `PHAZE_BACKENDS_CONFIG_FILE` (default `/etc/phaze/backends.toml`), `tomllib.load`s a present file and injects the `[[backends]]`/`[[buckets]]` tables as the SINGLE source; an absent file injects nothing so the `default_factory` synthesizes implicit-local — the live all-local deploy needs zero config edits (D-01/D-02/D-03).
- `_validate_registry` `model_validator(mode="after")`: empty-registry fail-fast (REG-04, the Phase-30 silent-wedge guard), unknown-bucket-ref + empty-bucket-set fail-fast naming the backend id (D-08), and `scope="cluster-specific"` ≤1-kueue-backend cardinality naming the bucket id (D-09).
- `cloud_enabled` derived property (any non-local backend) + `_single_non_local` reduction and the `active_cloud_kind`/`active_cap`/`active_compute_scratch_dir`/`active_kube`/`active_bucket` transitional accessors — each raising (never silently reducing) on >1 non-local backend (Phase 69) or >1 resolved bucket (Phase 70); every one carries a `# TRANSITIONAL — removed in Phase 68 (BACK-01)` marker (6 total, ≥5 required).
- `log_effective_registry` emits a `{id, kind, rank, cap}` projection only — verified secret-free against a `sa_token` written into the registry (Pitfall 5).
- Adopted the shared `config_backends._read_secret_file` in `_resolve_secret_files` (D-06: one strip-vs-verbatim rule, two call sites).
- Shared `backends_toml_env` conftest fixture (writes a tmp `backends.toml`, points the env pointer, clears the `get_settings` cache) for Wave-3 consumers.

## Task Commits

| Task | Name | RED (test) | GREEN (feat) |
| ---- | ---- | ---------- | ------------ |
| 1 | backends/buckets fields + Idiom-B tomllib loader + implicit-local + shared helper | `16372f8` | `3be9f27` |
| 2 | Container cross-entry validator (empty / bucket-cardinality / scope) | `7660973` | `818d387` |
| 3 | cloud_enabled + transitional accessors + secret-free startup-log projection | `bb260ba` | `b4b1da4` |

## Files Created/Modified
- `src/phaze/config.py` (modified) — registry fields, Idiom-B before-validator, container after-validator, cloud_enabled + accessors, log projection, `_read_secret_file` adoption; `import tomllib` + `import structlog` + module `logger`; `KubeConfig` under TYPE_CHECKING.
- `tests/shared/config/test_bucket_registry.py` (created) — 14 tests: implicit-local + parse (T1), empty/missing-ref/empty-set/cardinality (T2), cloud_enabled/accessors/multiplicity-raises/log-projection (T3).
- `tests/conftest.py` (modified) — `backends_toml_env` fixture.

## Deviations from Plan

None — plan executed as written. Two trivial pre-commit auto-fixups (ruff removed an unused `import pytest` and a redundant `# noqa: S105`, both re-applied before the commit landed); no behavior or scope change.

## Threat Surface
All four register threats mitigated as planned: T-67-02-01 (empty fail-fast via `_validate_registry`), T-67-02-02 (scope cardinality D-09), T-67-02-03 (secret-free `log_effective_registry`), T-67-02-04 (registry sourced only from the TOML file). No new security-relevant surface beyond the threat model.

## Known Stubs
None.

## Verification
- `uv run pytest tests/shared/config/` → 125 passed (111 pre-existing + 14 new; nothing removed — `cloud_target` untouched).
- `uv run pytest tests/shared -m "not integration"` → 556 passed (widely-imported config change regresses nothing).
- `uv run mypy src/phaze/config.py` → clean; `uv run ruff check src/phaze/config.py` → clean.
- `grep -c TRANSITIONAL src/phaze/config.py` → 6 (≥5).
- Pre-commit hooks (ruff, ruff-format, bandit, mypy) pass on every commit — no `--no-verify`.

## Next Phase Readiness
- Wave 3 (Plans 03/04/05) can now rewire the ~10 call sites: Class-A on/off gates read `settings.cloud_enabled`; Class-B dispatch forks read the transitional `active_*` accessors; Plan 05 wires `settings.log_effective_registry()` into controller startup.
- `cloud_target` and every flat `s3_*`/`kube_*`/`compute_scratch_dir` field remain in place this plan (removed in Wave 4 / Plan 06 after every call site is rewired), so the tree stays green.
- No blockers.

## Self-Check: PASSED

- `src/phaze/config.py`, `tests/shared/config/test_bucket_registry.py`, `tests/conftest.py` all present on disk.
- All 6 task commits (`16372f8`, `3be9f27`, `7660973`, `818d387`, `bb260ba`, `b4b1da4`) present in git history.

---
*Phase: 67-backend-registry-config-model*
*Completed: 2026-07-03*
