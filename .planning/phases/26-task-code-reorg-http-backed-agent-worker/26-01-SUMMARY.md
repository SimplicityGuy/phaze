---
phase: 26
plan: 01
subsystem: foundation
tags: [python, pydantic-settings, alembic, enums, deps]
requires: []
provides:
  - "tenacity (runtime dep) ready for PhazeAgentClient retry decorator (Plan 02)"
  - "respx (dev dep) ready for PhazeAgentClient contract tests (Plan 02)"
  - "mypy strict overrides for phaze.services.agent_client + phaze.services.agent_task_router"
  - "phaze.config.Role / BaseSettings / ControlSettings / AgentSettings / get_settings()"
  - "AgentSettings.{agent_api_url, agent_token, scan_roots} with fail-fast model_validator"
  - "ProposalStatus.{EXECUTED, FAILED}"
  - "FileState.{MOVED, UNCHANGED}"
affects:
  - "Plan 02: PhazeAgentClient — uses tenacity decorator + respx tests"
  - "Plan 08: agent_proposals PATCH router — references the new enum values"
  - "Plan 10: agent_worker startup — reads AgentSettings from get_settings()"
  - "Plan 11: execute_approved_batch — uses AgentSettings.scan_roots for path containment"
tech_stack_added:
  - "tenacity>=8.5.0 (resolved 9.1.4)"
  - "respx>=0.21.1 (resolved 0.23.1)"
tech_stack_patterns:
  - "pydantic-settings: Annotated[list[str], NoDecode] + @field_validator(mode='before') for comma-split env vars"
  - "pydantic AliasChoices(...) per field for env-var naming"
key_files_created: []
key_files_modified:
  - "pyproject.toml — +tenacity dep, +respx dev dep, +2 strict mypy overrides"
  - "src/phaze/config.py — full rewrite: Role/Base/Control/Agent settings + get_settings()"
  - "src/phaze/models/proposal.py — ProposalStatus + EXECUTED + FAILED"
  - "src/phaze/models/file.py — FileState + MOVED + UNCHANGED"
  - "uv.lock — regenerated after pyproject.toml changes"
decisions:
  - "D-11: tenacity added to [project].dependencies (runtime retry decorator)"
  - "D-14: settings split into BaseSettings + ControlSettings + AgentSettings + get_settings() factory"
  - "D-28: ProposalStatus and FileState extended with the terminal states the PATCH router will emit"
  - "D-31: respx added to dev deps"
  - "D-33: mypy strict overrides scoped to two new services modules (phase 26 future plans)"
  - "Local: pydantic-settings v2 does NOT comma-split list[str] env vars natively — used Annotated[..., NoDecode] + @field_validator(mode='before') as the canonical workaround"
  - "Local: explicit AliasChoices(PHAZE_AGENT_*, <field>) per field — pydantic-settings reads env vars by field name absent an env_prefix, but the documented env names use a PHAZE_AGENT_ prefix"
  - "Local: kept module-level `settings: ControlSettings` annotation so existing call sites that read `settings.llm_*` / `settings.discogs_match_concurrency` still type-check"
  - "Local: kept `Settings = ControlSettings` back-compat alias so test files that import the legacy class name continue to work"
metrics:
  duration_minutes: 35
  completed_at: "2026-05-12T21:11:56Z"
---

# Phase 26 Plan 01: Wave 0 Foundation Summary

**One-liner:** Added tenacity + respx, split phaze.config into role-specific settings classes (BaseSettings / ControlSettings / AgentSettings with scan_roots + fail-fast validator), and extended ProposalStatus + FileState enums with the D-28 transition targets — Wave 0 foundation for Phase 26's HTTP-backed agent worker.

## What Was Done

### Task 1: pyproject.toml (commit `47817c1`)
- Added `tenacity>=8.5.0` to `[project].dependencies` (resolved 9.1.4).
- Added `respx>=0.21.1` to `[dependency-groups].dev` (resolved 0.23.1).
- Added two `[[tool.mypy.overrides]]` blocks scoped to `phaze.services.agent_client` and `phaze.services.agent_task_router`, each opting into strict checking (`disallow_untyped_defs`, `check_untyped_defs`, `warn_return_any`, `strict_equality`) despite the global `services/` exclude. The new code in those modules (Plan 02) will be fully type-checked.
- Regenerated `uv.lock` (`uv lock`) and synced (`uv sync`).
- The global mypy `exclude = "^(tests/|prototype/|services/)"` line is unchanged so the rest of `services/` continues to iterate without strict gates.

### Task 2: src/phaze/config.py (commit `3fe4f7f`)
- Added `Role(StrEnum)` with values `"control"` and `"agent"`.
- Split `Settings` into:
  - `BaseSettings(PydanticBaseSettings)` — shared fields used by both roles (database_url, redis_url, debug, scan_path, models_path, output_path, worker_*, audfprint_url, panako_url, discogsography_url, api_host, api_port, agent_token_prefix, agent_file_chunk_max).
  - `ControlSettings(BaseSettings)` — application-server fields: discogs_match_concurrency + the LLM block (openai_api_key, anthropic_api_key, llm_model, llm_max_rpm, llm_batch_size, llm_max_companion_chars).
  - `AgentSettings(BaseSettings)` — file-server fields: `agent_api_url: str`, `agent_token: SecretStr`, `scan_roots: list[str]`. A `model_validator(mode="after")` raises `ValueError` at construction time if any is missing/empty, fulfilling the threat-model T-26-01-T2 mitigation: empty `scan_roots` → fail-fast at boot rather than silent "stuck queue" via path-traversal rejections at runtime.
- Added `@lru_cache(maxsize=1) def get_settings() -> BaseSettings:` — single dispatch point that reads `PHAZE_ROLE` env and returns the right subclass.
- Replaced `settings = Settings()` with a module-level `settings: ControlSettings = _build_default_settings()`. The function-wrapped construction (instead of inline `settings = get_settings()`) was necessary so the module-level type is `ControlSettings` (not `BaseSettings`), keeping every existing call site that reads `settings.llm_*` / `settings.discogs_match_concurrency` type-checking under strict mypy. The agent worker (Plan 10) will not read this singleton — it will call `get_settings()` / `AgentSettings()` directly.
- Added `Settings = ControlSettings` back-compat alias for test files that import the legacy class name.

### Task 3: Enum extensions (commit `ded297a`)
- `ProposalStatus` (`src/phaze/models/proposal.py`) now has `EXECUTED = "executed"` and `FAILED = "failed"` in addition to the prior `PENDING / APPROVED / REJECTED`.
- `FileState` (`src/phaze/models/file.py`) now has `MOVED = "moved"` and `UNCHANGED = "unchanged"` in addition to the existing values. The pre-existing `EXECUTED` and `FAILED` FileState values are retained for Phase 25-era execution-log emit paths.
- No alembic migration needed: `Proposal.status` is `String(20)` ("executed"/"failed" fit) and `FileRecord.state` is `String(30)` ("moved"/"unchanged" fit). StrEnum values store as plain strings — adding values widens the accepted set without DDL change.

## Verification Results

| Command | Result |
|--------|--------|
| `uv run pytest -x -q --no-cov` | **828 passed**, 58 deprecation warnings (unrelated) |
| `uv run mypy .` | **Success: no issues found in 95 source files** |
| `uv run ruff check .` (touched files) | **All checks passed** |
| `uv run ruff format --check` (touched files) | **3 files already formatted** |
| `pre-commit run --all-files` | **All hooks Passed** (no `--no-verify` used) |
| `uv run python -c "import tenacity, respx"` | OK |
| `[e.value for e in ProposalStatus]` | `['pending', 'approved', 'rejected', 'executed', 'failed']` |
| `[e.value for e in FileState]` | 12 values including `'moved'` and `'unchanged'` |
| `type(settings).__name__` | `ControlSettings` (default PHAZE_ROLE=control) |

## Deviations from Plan

### Rule 2 — auto-add missing critical functionality

**1. pydantic-settings does not natively comma-split `list[str]` env vars.**

The plan claimed "pydantic-settings comma-splits the list natively when the field type is `list[str]`." It does not. pydantic-settings v2 expects a JSON-encoded list and raises `json.decoder.JSONDecodeError` on bare strings like `"/a,/b"`.

**Fix:** Annotated the `scan_roots` field as `Annotated[list[str], NoDecode]` so pydantic-settings skips the JSON-decode step, then added a `@field_validator("scan_roots", mode="before")` classmethod `_split_scan_roots` that comma-splits string input while passing native list input through unchanged.

**2. pydantic-settings reads env vars by field name absent `env_prefix`.**

The plan's acceptance criteria used env-var names like `PHAZE_AGENT_API_URL`, `PHAZE_AGENT_TOKEN`, `PHAZE_AGENT_SCAN_ROOTS`. Without an `env_prefix`, pydantic-settings reads env vars by the bare field name (case-insensitive), so the field `agent_api_url` would map to `AGENT_API_URL`, not `PHAZE_AGENT_API_URL`.

**Fix:** Added a per-field `validation_alias=AliasChoices("PHAZE_AGENT_*", "<bare_field_name>")` so both the documented env-var form (preferred at runtime) and direct kwargs / bare field names (convenient for pytest monkeypatch) work.

### Rule 1 — auto-fix bug introduced by Task 2

**3. Module-level `settings` singleton type widening broke 37+ call sites.**

The plan's literal text replaced `settings = Settings()` with `settings = get_settings()`. `get_settings()` returns `BaseSettings`, which does not have `discogs_match_concurrency` / `llm_*` fields — those live on `ControlSettings`. Mypy flagged 9 errors across `src/phaze/tasks/`, `src/phaze/routers/pipeline.py`, etc.

**Fix:** Replaced the module-level line with `settings: ControlSettings = _build_default_settings()` where `_build_default_settings()` always returns a `ControlSettings`. The agent worker (Plan 10) will not read this singleton — it will call `get_settings()` / `AgentSettings()` directly per D-14. Documented intent in the function docstring.

Trade-off: the literal acceptance grep `grep -c "^settings = get_settings()"` no longer matches. The plan's semantic intent — "preserve back-compat for `from phaze.config import settings`" — is fully preserved; the type-system layer just needs the narrower annotation.

### Rule 1 — auto-fix test regression

**4. `Settings` class name was used by 9 test imports.**

Tests in `tests/test_config_worker.py`, `tests/test_phase01_gaps.py`, `tests/test_services/test_fingerprint.py`, and `tests/test_services/test_proposal.py` import the legacy class via `from phaze.config import Settings`. Removing the class name without a back-compat alias broke these tests.

**Fix:** Added `Settings = ControlSettings` alias at module end. The legacy class is functionally identical to `ControlSettings` (which has every field the old monolithic class had), so this is a precise alias, not an approximation. Migrating those tests to import `ControlSettings` directly is out of scope for this plan.

## Surprises / Notes

- **pydantic-settings v2 has subtle env-var quirks.** Two consecutive issues (JSON-decode of `list[str]`, no automatic `PHAZE_*` prefix) means downstream agent operators will need exact env-var names. Documented inline in the field descriptions so future code-readers don't repeat the trip.
- **`.env` file safety: `extra="ignore"` covers cross-role env files.** A single `.env` file can contain both `PHAZE_AGENT_*` keys and `PHAZE_ROLE=control` without `ControlSettings` raising on the unrelated `PHAZE_AGENT_*` keys, because `BaseSettings` model_config sets `extra="ignore"`.
- **No alembic migration created.** The most recent migration is still `014_add_last_status_to_agents.py`. Confirmed `String(20)` (Proposal.status) and `String(30)` (FileRecord.state) accommodate the new StrEnum values.
- **Worktree onboarding step:** the agent worktree was branched from `9647212` (before Phase 26 planning landed on main). I checked out the planning files (`.planning/phases/26-task-code-reorg-http-backed-agent-worker/` + updated `STATE.md` / `ROADMAP.md` / `REQUIREMENTS.md`) from `main` and committed them as `chore(26): sync phase 26 planning artifacts into worktree` (`3bd02e8`) before starting per-task work.

## Self-Check: PASSED

- [x] `pyproject.toml` contains `"tenacity>=8.5.0"` and `"respx>=0.21.1"`
- [x] `pyproject.toml` contains both `[[tool.mypy.overrides]]` for `phaze.services.agent_client` and `phaze.services.agent_task_router` with strict-checking knobs
- [x] `uv.lock` regenerated (committed in `47817c1`)
- [x] `src/phaze/config.py` exports `Role`, `BaseSettings`, `ControlSettings`, `AgentSettings`, `get_settings`, module-level `settings`, and back-compat `Settings` alias
- [x] `src/phaze/config.py` `AgentSettings` has `agent_api_url`, `agent_token: SecretStr`, `scan_roots: list[str]` (Annotated with NoDecode) with `_split_scan_roots` field validator and `_enforce_required_agent_fields` model validator
- [x] `src/phaze/models/proposal.py` `ProposalStatus` has `EXECUTED` + `FAILED`
- [x] `src/phaze/models/file.py` `FileState` has `MOVED` + `UNCHANGED`
- [x] Per-task commits exist: `47817c1`, `3fe4f7f`, `ded297a`
- [x] Full `uv run pytest -x -q --no-cov` passes (828 tests)
- [x] Full repo `uv run mypy .` clean
- [x] `pre-commit run --all-files` all hooks Passed
