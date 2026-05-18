---
phase: 29-deployment-hardening-agents-admin
plan: 02
subsystem: auth
tags: [phase-29, auth, redis, security, v4.0, agent-settings, model-validator]

requires:
  - phase: 26-task-code-reorg-http-backed-agent-worker
    provides: AgentSettings (role-split BaseSettings subclass)
provides:
  - AgentSettings.agent_env (D-06; Literal["dev","production"], default "dev", env alias PHAZE_AGENT_ENV)
  - AgentSettings._enforce_redis_password_in_production model_validator (D-06)
  - tests/test_config/__init__.py (pytest sub-package marker)
  - tests/test_config/test_agent_settings_redis_password.py (4 cases)
affects:
  - Phase 29 Plan 03 (docker-compose rewrite + Redis requirepass; server-side half of AUTH-03)
  - All agent worker entrypoints that construct SAQ Queue.from_url(redis_url) in production
  - .env.example.agent (Plan 03 will document `PHAZE_AGENT_ENV=production` alongside `REDIS_PASSWORD`)

tech-stack:
  added: []
  patterns:
    - "Literal-typed deployment-mode selector field with AliasChoices env alias"
    - "model_validator(mode='after') that reads multiple sibling fields (agent_env + redis_url) — the existing _enforce_required_agent_fields validator is the analog, and chains in order before this one"
    - "URL-shape guard via urllib.parse.urlparse rather than regex (handles URL-encoded passwords; degenerate URLs fall through to SAQ connect-time failure)"
    - "Dev-default opt-out (Pitfall 7): the strict guard only fires when the operator explicitly opts in to production mode"

key-files:
  created:
    - tests/test_config/__init__.py
    - tests/test_config/test_agent_settings_redis_password.py
  modified:
    - src/phaze/config.py (added Literal import; agent_env field; _enforce_redis_password_in_production model_validator)

key-decisions:
  - "D-06: agent_env defaults to 'dev' so fresh clones / Pitfall 7 work without ceremony; operator must explicitly set PHAZE_AGENT_ENV=production to engage the guard."
  - "model_validator (not field_validator) — needs access to BOTH redis_url and agent_env on the same instance; field validators on redis_url cannot read agent_env in pydantic v2."
  - "URL parsing via urllib.parse.urlparse rather than a regex — correctly handles URL-encoded passwords (`%40` for `@` in the password component, etc.). A malformed URL falls through to SAQ connect-time failure rather than a confusing pydantic validation error."
  - "Validator placed AFTER _enforce_required_agent_fields so required-field checks run first; ordering matches the natural failure mode (missing api_url is a more obvious operator error than a passworded URL mismatch)."
  - "NO separate `redis_password` field — the AUTH-03 contract is one full URL with the password embedded (`redis://default:<pw>@host:6379/0`). Adding a separate field would duplicate state and create skew risk."
  - "Field placement: between agent_token and scan_roots to keep PHAZE_AGENT_* fields contiguous in the source."

patterns-established:
  - "Linked-field validation pattern on AgentSettings: model_validator(mode='after') reading self.<field_a> and self.<field_b> with a Phase-tagged docstring and an actionable error message that references the decision ID."
  - "Test pattern for AgentSettings model contracts: pass kwargs directly to AgentSettings(...) rather than env-var monkeypatching (cleaner than the role-split tests' env-var pattern when the contract under test is the model itself, not the env-var → field mapping)."

requirements-completed: []  # AUTH-03 is partial (agent-side guard); fully closes when Plan 03 lands compose-side requirepass + LAN bind

metrics:
  duration: ~4min
  tasks_complete: 1
  files_created: 2
  files_modified: 1
  tests_added: 4
  commits: 2  # 1 RED test + 1 GREEN feat (no REFACTOR needed)

completed: 2026-05-16
---

# Phase 29 Plan 02: AgentSettings Production-Mode Redis-Password Validator Summary

**Agent-side guard that refuses passwordless Redis URLs when `agent_env=production`, closing the client half of AUTH-03. The server-side half (Redis `requirepass` + LAN-bound port) lands in Plan 03; this plan ensures a misconfigured production agent fails fast at startup rather than silently connecting to an unsecured Redis.**

## What Shipped

### Config: agent_env field + production-mode validator

- **`AgentSettings.agent_env`** (D-06): `Literal["dev", "production"]`, default `"dev"`, env alias `PHAZE_AGENT_ENV` via `AliasChoices`. Placed adjacent to the other `PHAZE_AGENT_*` fields (between `agent_token` and `scan_roots`) for source-file grouping. Docstring references Phase 29 D-06 and the matching server-side hardening.
- **`AgentSettings._enforce_redis_password_in_production`** (D-06): `model_validator(mode="after")`. When `self.agent_env == "production"`, parses `urlparse(self.redis_url)`; if `parsed.password` is falsy, raises `ValueError("agent_env=production requires a password in redis_url (Phase 29 D-06)")`. Returns `self` otherwise. Placed AFTER `_enforce_required_agent_fields` so the required-field check runs first (matches the natural operator failure-mode ordering).
- **`from typing import Annotated, Literal`** — `Literal` added to the existing import; `urlparse` was already imported at the top of `config.py` (Phase 28 added it for the `_enforce_localhost_only` field-validator) so no new import was needed for that.

No separate `redis_password` field. Per CONTEXT D-06 and RESEARCH §Pattern 4, the AUTH-03 contract is one full URL with the password embedded (`redis://default:<pw>@host:6379/0`); the validator simply parses what's already in `redis_url`.

### Tests

- **`tests/test_config/__init__.py`** — empty file; pytest sub-package marker so the new `test_config/` directory is discovered.
- **`tests/test_config/test_agent_settings_redis_password.py`** — **4 cases**:
  1. `test_production_refuses_passwordless_redis_url`: `agent_env="production"` + `redis://localhost:6379/0` raises `ValidationError`; error message contains `"requires a password in redis_url"`.
  2. `test_production_accepts_passworded_redis_url`: `agent_env="production"` + `redis://default:secret@localhost:6379/0` constructs successfully; `cfg.agent_env == "production"`.
  3. `test_dev_accepts_passwordless_redis_url`: `agent_env="dev"` + passwordless URL constructs OK; `cfg.agent_env == "dev"`.
  4. `test_default_agent_env_is_dev`: omitting `agent_env` yields `cfg.agent_env == "dev"` (existing-workflow guarantee).

Tests pass kwargs directly to `AgentSettings(...)` rather than env-var monkeypatching — cleaner than the role-split tests' env-var pattern when the contract under test is the model itself, not the env-var → field mapping. (Env-var mapping is implicitly covered by `AliasChoices` being identical to the existing patterns; a dedicated env-var test would duplicate role-split coverage.)

## Verification Results

```
uv run pytest tests/test_config/test_agent_settings_redis_password.py -x -q
4 passed in 0.01s

uv run pytest tests/test_config_role_split.py tests/test_config_worker.py \
  tests/test_config/ tests/test_task_split.py tests/test_main_lifespan.py -q
32 passed in 1.65s
```

- 4/4 new tests pass (RED → GREEN cycle: 4b95029 → a7741ff).
- 22 existing config tests pass (zero regression in `test_config_role_split.py` / `test_config_worker.py`).
- 6 task-split / main-lifespan tests pass (no import-boundary regression from the new import).
- `uv run mypy src/phaze/config.py` — clean (Success: no issues found in 1 source file).
- `uv run ruff check src/phaze/config.py tests/test_config/` — clean.
- `uv run ruff format --check src/phaze/config.py tests/test_config/` — clean (3 files already formatted).
- `uv run python -c "from phaze.config import AgentSettings; from pydantic import SecretStr; cfg = AgentSettings(agent_env='dev', redis_url='redis://localhost:6379/0', agent_api_url='https://x', agent_token=SecretStr('x'), scan_roots=['/tmp']); print(cfg.agent_env)"` → prints `dev` (matches plan `<verification>` smoke).

## Commits

| Hash    | Type | Phase | Subject                                                                                |
| ------- | ---- | ----- | -------------------------------------------------------------------------------------- |
| 4b95029 | test | RED   | add failing tests for AgentSettings agent_env + redis-password validator               |
| a7741ff | feat | GREEN | enforce passworded Redis URL on AgentSettings in production mode                       |

RED state confirmed before the GREEN commit: pytest reported `Failed: DID NOT RAISE <class 'pydantic_core._pydantic_core.ValidationError'>` on `test_production_refuses_passwordless_redis_url` because the validator did not yet exist. (The `agent_env="production"` kwarg was silently discarded by `extra="ignore"` on `SettingsConfigDict`, so no field-level error fired and no model-level validator could fire either — exactly the RED signature expected for "field missing + validator missing".) GREEN state confirmed: 4/4 tests pass.

No REFACTOR commit needed — the implementation landed in its final shape in the GREEN commit.

## Deviations from Plan

### Auto-fixed Issues

None. The plan's `<action>` block was complete and accurate — `urlparse` was already imported at line 14 (added by Phase 28 for `_enforce_localhost_only`), so the "add `from urllib.parse import urlparse`" step in the plan was a no-op as the plan itself anticipated ("if no stdlib imports exist yet" — they did exist).

### Authentication gates

None.

### Architectural decisions

None.

## Threat Flags

None. The plan's `<threat_model>` already enumerates the four surfaces this plan touches (T-29-02-01..T-29-02-04). No new surface was introduced beyond what the model anticipated:

- **T-29-02-01 / T-29-02-02** (passwordless Redis exposes queues; spoofed agent stuffs queues): mitigated by the new client-side guard. Server-side half (Plan 03) closes the full attack path.
- **T-29-02-03** (dev clones silently start with `agent_env=production` and break): mitigated by `default="dev"` — operator must explicitly opt in.
- **T-29-02-04** (typo / URL-encoding error): mitigated by `urlparse(self.redis_url).password` semantics (URL-encoded passwords resolve correctly); malformed URLs fall through to SAQ connect-time failure.

## Known Stubs

None. The validator is fully functional end-to-end:

- The field is a real `Literal` type, not a placeholder string.
- The validator raises a real `ValueError` (which pydantic wraps as `ValidationError`).
- `urlparse` is the real stdlib parser, not a stub.
- The error message contains the operator-actionable substring `"requires a password in redis_url"` plus the decision-ID reference `(Phase 29 D-06)`.

The remaining AUTH-03 work — switching `docker-compose.yml` to set `redis-server --requirepass ${REDIS_PASSWORD}` on the redis service and bind it to the LAN-only interface — lands in **Plan 03** per the plan's `<objective>` note: "The compose-side Redis hardening (`requirepass` + LAN-bound port) lives in Plan 03 alongside the docker-compose rewrite; this plan delivers the agent-side guard that refuses passwordless Redis URLs in production. Together they close AUTH-03."

## TDD Gate Compliance

The single task in this plan followed the RED → GREEN cycle:

- **RED** (4b95029): `test(29-02): add failing tests for AgentSettings agent_env + redis-password validator`. Pytest run confirmed `test_production_refuses_passwordless_redis_url` failed with `DID NOT RAISE ValidationError`.
- **GREEN** (a7741ff): `feat(29-02): enforce passworded Redis URL on AgentSettings in production mode`. All 4 tests pass after the field + validator were added.
- **REFACTOR**: not needed — the implementation matched the PATTERNS.md target shape on first GREEN.

Gate-sequence check: `git log --oneline` shows `4b95029 test(...)` immediately preceding `a7741ff feat(...)` for plan 29-02. RED and GREEN commits both present in correct order.

## Self-Check: PASSED

Files claimed to be created/modified — all present:
- `tests/test_config/__init__.py` — FOUND
- `tests/test_config/test_agent_settings_redis_password.py` — FOUND
- `src/phaze/config.py` — MODIFIED (verified via `grep -n "agent_env\|_enforce_redis_password" src/phaze/config.py` returns 6 lines covering the field + validator definitions)

Commits claimed — all present in `git log --oneline`:
- 4b95029 — FOUND
- a7741ff — FOUND

Test count matches plan's `<acceptance_criteria>`: 4 new tests in `tests/test_config/test_agent_settings_redis_password.py` covering prod+passwordless=fail, prod+passworded=ok, dev+passwordless=ok, default-is-dev.

Decision IDs implemented: D-06 (complete — agent-side half of AUTH-03).
