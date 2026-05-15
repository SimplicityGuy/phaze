---
phase: 28
plan: 01
subsystem: config / schemas / test-infrastructure
tags: [wave-0, scaffolding, fingerprint-locality, sub-batch-index, tdd]
dependency_graph:
  requires: []
  provides:
    - "tests/test_template_helpers/ package"
    - "src/phaze/templates/_partials/ directory"
    - "BaseSettings._enforce_localhost_only (D-12)"
    - "ExecuteApprovedBatchPayload.sub_batch_index (D-10)"
    - "Eight Phase 28 test-file anchors (Wave 1+ implementations)"
  affects:
    - src/phaze/config.py
    - src/phaze/schemas/agent_tasks.py
tech_stack:
  added: []
  patterns:
    - "Pydantic @field_validator class-method on BaseSettings (inherits to subclasses)"
    - "Module-level urllib.parse.urlparse import (PLC0415 compliance)"
    - "Module-level pytest.skip(..., allow_module_level=True) for Wave 0 anchor stubs"
key_files:
  created:
    - tests/test_template_helpers/__init__.py
    - tests/test_template_helpers/test_progress_partial.py
    - tests/test_template_helpers/test_cross_fs_fingerprint_notice.py
    - tests/test_routers/test_agent_exec_batches.py
    - tests/test_routers/test_execution_dispatch.py
    - tests/test_services/test_execution_dispatch_grouping.py
    - tests/test_services/test_fingerprint_locality.py
    - tests/test_services/test_agent_client_exec_batch_progress.py
    - tests/test_schemas/test_agent_exec_batches.py
    - tests/test_tasks/test_execute_approved_batch_progress.py
    - src/phaze/templates/_partials/.gitkeep
  modified:
    - src/phaze/config.py
    - src/phaze/schemas/agent_tasks.py
decisions:
  - "Validator lives on BaseSettings (not on ControlSettings or AgentSettings) so both inherit the guard via the existing class hierarchy"
  - "Allow-list set chosen verbatim from PATTERNS S9 + RESEARCH Focus Area 5: {localhost, 127.0.0.1, audfprint, panako}"
  - "urlparse imported at module top-level (PLC0415 compliance) -- deviation from plan action text which proposed local import"
  - "Wave 0 stubs use module-level pytest.skip(..., allow_module_level=True) per VALIDATION.md scaffolding contract; each skip message cites the implementing plan (28-02..28-06)"
metrics:
  duration_seconds: 632
  duration_human: "~10.5 min"
  tasks_completed: 1
  files_changed: 13
  commits: 2
  completed_date: "2026-05-15"
---

# Phase 28 Plan 01: Wave 0 Test Scaffolding + Fingerprint Locality Validator + sub_batch_index Summary

Wave 0 unblocker for Phase 28: created the eight test-file anchors Nyquist sampling needs (seven module-level `pytest.skip` stubs + one fully-implemented fingerprint-locality test module), the two new directories later waves depend on (`tests/test_template_helpers/`, `src/phaze/templates/_partials/`), and landed the two single-file production changes that have no other dependencies — the `audfprint_url`/`panako_url` allow-list validator (D-12 / TASK-04) and `ExecuteApprovedBatchPayload.sub_batch_index: int = 0` (D-10).

## What Was Built

### TDD RED → GREEN sequence

- **RED commit `3ed23b6`** (`test(28-01): add Wave 0 test scaffolding + failing fingerprint-locality tests`): created 10 test files + `_partials/.gitkeep`. Two `test_fingerprint_locality.py` reject tests failed (the validator did not yet exist); four accept tests passed (defaults already match the allow-list); 8 stub files SKIPPED cleanly.
- **GREEN commit `814085f`** (`feat(28-01): add fingerprint URL allow-list validator + sub_batch_index field`): added the `@field_validator` on `BaseSettings` and the `sub_batch_index: int = 0` field on `ExecuteApprovedBatchPayload`. All six locality tests now PASS; all stubs continue to SKIP cleanly; pre-commit hooks (ruff/ruff-format/bandit/mypy) green on both touched files.

### Production logic

**`src/phaze/config.py`** — Added `_enforce_localhost_only` classmethod under `BaseSettings.audfprint_url`/`panako_url` field definitions:

```python
@field_validator("audfprint_url", "panako_url")
@classmethod
def _enforce_localhost_only(cls, value: str) -> str:
    parsed = urlparse(value)
    allowed_hosts = {"localhost", "127.0.0.1", "audfprint", "panako"}
    if parsed.hostname not in allowed_hosts:
        msg = (
            f"audfprint_url/panako_url must point to a host on the agent's "
            f"local Compose network (got host={parsed.hostname!r}; allowed="
            f"{sorted(allowed_hosts)}). Cross-file-server fingerprint matching "
            f"is not supported in v4.0 -- see XAGENT-01."
        )
        raise ValueError(msg)
    return value
```

**Exact regex/allow-list used in the validator** (recorded for future audits per the plan's `<output>` requirement):
- Mechanism: `urllib.parse.urlparse(value).hostname` membership check (no regex — Python stdlib URL parser handles scheme/port/auth correctly).
- Allow-list set: `{"localhost", "127.0.0.1", "audfprint", "panako"}`.
- Rejection error message contains the strings `"local Compose network"`, `"XAGENT-01"`, and the offending hostname.

**`src/phaze/schemas/agent_tasks.py`** — Added `sub_batch_index: int = 0` as the last field of `ExecuteApprovedBatchPayload` with the inline comment `# Phase 28 D-10 -- 0-based; default preserves legacy callers`. `extra="forbid"` was already set on the class so this is a wire-format change; default `0` keeps single-chunk callers (and any latent Phase 26 test fixtures) compatible.

### Test scaffolding (Wave 0 contract from VALIDATION.md)

| File | State | Implementing plan |
|------|-------|-------------------|
| `tests/test_services/test_fingerprint_locality.py` | **IMPLEMENTED** (6 tests pass) | 28-01 (this plan) |
| `tests/test_schemas/test_agent_exec_batches.py` | stub `pytest.skip` | 28-02 |
| `tests/test_routers/test_agent_exec_batches.py` | stub `pytest.skip` | 28-02 |
| `tests/test_services/test_agent_client_exec_batch_progress.py` | stub `pytest.skip` | 28-02 |
| `tests/test_services/test_execution_dispatch_grouping.py` | stub `pytest.skip` | 28-03 |
| `tests/test_routers/test_execution_dispatch.py` | stub `pytest.skip` | 28-04 |
| `tests/test_template_helpers/test_progress_partial.py` | stub `pytest.skip` | 28-04 |
| `tests/test_tasks/test_execute_approved_batch_progress.py` | stub `pytest.skip` | 28-05 |
| `tests/test_template_helpers/test_cross_fs_fingerprint_notice.py` | stub `pytest.skip` | 28-06 |

Each stub uses `pytest.skip(<msg>, allow_module_level=True)` so `pytest -x` collects the module without raising `ModuleNotFoundError` and without burning collection time on a real test body. The skip message cites the implementing plan.

### Directory anchors

- `tests/test_template_helpers/__init__.py` (empty) — anchors a new Python test package so the stubs in this directory are discovered by pytest's package-style collection.
- `src/phaze/templates/_partials/.gitkeep` (empty) — anchors a new Jinja partial directory that the banner partial (Plan 28-06) and any future cross-page partials live in. The directory did not exist before this plan (verified by `find`, per RESEARCH Pitfall 6).

## 28-V-NN Test ID Status

| Test ID | Status | Plan |
|---------|--------|------|
| **28-V-22** (audfprint/panako reject external hosts) | **GREEN** | 28-01 |
| **28-V-23** (audfprint/panako accept localhost + Compose names) | **GREEN** | 28-01 |
| 28-V-01..28-V-03 (template-helper partials) | anchored, stubbed | 28-04 / 28-06 |
| 28-V-06..28-V-08 (dispatch grouping unit) | anchored, stubbed | 28-03 |
| 28-V-10..28-V-17 (router/schema/agent-client contracts) | anchored, stubbed | 28-02 |
| 28-V-18..28-V-21 (controller dispatch integration) | anchored, stubbed | 28-04 |
| 28-V-25 (agent-side per-proposal progress POSTs) | anchored, stubbed | 28-05 |

Every Wave 1+ plan can now `pytest -k <test_name>` without `ModuleNotFoundError` and without inventing scaffolding mid-stream.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Tooling] Module-level `urllib.parse.urlparse` import (ruff PLC0415)**
- **Found during:** Task 1 pre-commit on `src/phaze/config.py`.
- **Issue:** The plan's `<action>` block proposed `from urllib.parse import urlparse` as a "lazy/local import inside the function." Ruff's project-wide PLC0415 (`'import' should be at the top-level of a file`) rejected that placement.
- **Fix:** Moved `from urllib.parse import urlparse` to the module's top-level import block. Functional behavior is identical; the import is cold-loaded once at module import time instead of on every validator invocation. The validator runs at Settings construction (process-startup) so the cost difference is unmeasurable.
- **Files modified:** `src/phaze/config.py` (import block + validator body).
- **Commit:** `814085f`.

**2. [Rule 1 - Tooling] ruff-format reflowed the multi-line `assert` in `test_fingerprint_locality.py`**
- **Found during:** Pre-commit on the RED commit.
- **Issue:** ruff-format restructured the four-line `assert (a or b or c), f"..."` form into a single-line `assert a or b or c, (f"...")` form. Functional behavior is identical (Python parser treats the two forms equivalently for `assert`).
- **Fix:** Re-staged the reformatted file before commit (pre-commit auto-applied the change). No semantic change.
- **Files modified:** `tests/test_services/test_fingerprint_locality.py`.
- **Commit:** `3ed23b6` (RED).

No Rule 2 (missing critical functionality), Rule 3 (blocker), or Rule 4 (architectural) deviations occurred.

## Auth Gates

None. This plan touched no HTTP endpoints, no credentials, no external services.

## Threat Surface Scan

No NEW threat surface introduced. The two production changes both CLOSE prior threats:
- `_enforce_localhost_only` validator structurally mitigates T-28-01-S (Spoofing) and T-28-01-I (Information Disclosure) per the plan's `<threat_model>`. A forged env var pointing at an external host now raises `ValidationError` at construction time, before the app boots.
- `ExecuteApprovedBatchPayload.sub_batch_index: int = 0` keeps `extra="forbid"` intact (T-28-01-V5 input-validation mitigation preserved).

No `## Threat Flags` section needed.

## Known Stubs

The eight scaffolding stubs are **intentional** anchors for Wave 1+ implementations; their existence is the plan's explicit contract (D-18). They are not blocking stubs — they SKIP at module level with a message that cites the implementing plan. The "Wave 0" comment in each plan plus the citation in each skip message provides the audit trail to the verifier. No data-rendering UI components are stubbed by this plan.

## Plan Verification

Executed the plan's `<automated>` command:

```bash
uv run pytest \
  tests/test_services/test_fingerprint_locality.py \
  tests/test_schemas/ \
  tests/test_routers/test_agent_exec_batches.py \
  tests/test_template_helpers/ -x
```

Result: **85 passed, 4 skipped, 0 failed**.

`<done>` criteria:
- `grep -c "_enforce_localhost_only" src/phaze/config.py` → 1 (✓ ≥ 1)
- `grep -c "sub_batch_index" src/phaze/schemas/agent_tasks.py` → 1 (✓ ≥ 1)
- `test -d tests/test_template_helpers && test -d src/phaze/templates/_partials` → both exist (✓)
- Pre-commit on changed files (ruff / ruff-format / bandit / mypy) → green (✓)

**Full-suite `uv run pytest -x` is NOT green in this worktree** — but the 10 failures and 399 errors are 100% pre-existing PostgreSQL-connection failures (`OSError: Connect call failed ('127.0.0.1', 5432)`). No Postgres is running in the worktree environment. None of the failures touch files this plan modified; the failures occur on `tests/test_services/test_search_queries.py`, `test_proposal_queries.py`, `test_pipeline.py`, etc. — DB-backed integration tests that require a live PostgreSQL instance.

To confirm scope: ran `uv run pytest tests/test_schemas/ tests/test_services/test_fingerprint_locality.py tests/test_config_role_split.py tests/test_config_worker.py tests/test_constants.py tests/test_task_split.py tests/test_base_html_sri.py` (the non-DB tests this plan could plausibly affect) → **124 passed, 1 skipped, 0 failed**. Plan 28-01 introduces zero regressions to the non-DB test surface.

## TDD Gate Compliance

- RED gate (`test(...)` commit `3ed23b6`): created the failing tests + stub anchors. ✓
- GREEN gate (`feat(...)` commit `814085f`): minimal implementation that flips the failing tests to passing. ✓
- REFACTOR gate: not required — the validator and the field-addition are both minimal-surface implementations with no follow-up cleanup needed.

Gate sequence verified in `git log --oneline -3`:
```
814085f feat(28-01): add fingerprint URL allow-list validator + sub_batch_index field
3ed23b6 test(28-01): add Wave 0 test scaffolding + failing fingerprint-locality tests
fc2397e docs(phase-28): begin phase execution
```

## Self-Check: PASSED

Verified all 13 file paths and both commit hashes exist on this branch.

- File check: `tests/test_template_helpers/__init__.py`, `tests/test_template_helpers/test_progress_partial.py`, `tests/test_template_helpers/test_cross_fs_fingerprint_notice.py`, `tests/test_routers/test_agent_exec_batches.py`, `tests/test_routers/test_execution_dispatch.py`, `tests/test_services/test_execution_dispatch_grouping.py`, `tests/test_services/test_fingerprint_locality.py`, `tests/test_services/test_agent_client_exec_batch_progress.py`, `tests/test_schemas/test_agent_exec_batches.py`, `tests/test_tasks/test_execute_approved_batch_progress.py`, `src/phaze/templates/_partials/.gitkeep`, `src/phaze/config.py`, `src/phaze/schemas/agent_tasks.py` → all present.
- Commit check: `3ed23b6` (RED), `814085f` (GREEN) → both on `worktree-agent-a04084d9a0fd6ae03`.
