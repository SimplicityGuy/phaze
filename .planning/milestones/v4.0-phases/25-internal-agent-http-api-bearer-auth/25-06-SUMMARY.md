---
phase: 25
plan: 06
subsystem: api
tags:
  - integration
  - routers
  - settings
  - openapi
  - bearer-auth
dependency_graph:
  requires:
    - "25-03"
    - "25-04"
    - "25-05"
  provides:
    - "main.py registers all 5 phase-25 internal-agent routers"
    - "settings.agent_token_prefix (default 'phaze_agent_')"
    - "settings.agent_file_chunk_max (default 1000)"
    - "FileUpsertChunk chunk-cap sourced from settings (literal removed)"
    - "OpenAPI components.securitySchemes.bearerAuth emitted on production app"
  affects:
    - "src/phaze/main.py"
    - "src/phaze/config.py"
    - "src/phaze/schemas/agent_files.py"
tech_stack:
  added: []
  patterns:
    - "module-level constant resolved at import-time from pydantic-settings (`_CHUNK_MAX = settings.agent_file_chunk_max`)"
    - "parenthesized multi-line `from phaze.routers import (...)` for >12 modules (ruff isort split-on-trailing-comma)"
    - "grouped `include_router` block with explanatory comment for cohort wiring"
key_files:
  created: []
  modified:
    - "src/phaze/main.py"
    - "src/phaze/config.py"
    - "src/phaze/schemas/agent_files.py"
decisions:
  - "Chunk cap is bound at module import (not per-request) — operator changes to `AGENT_FILE_CHUNK_MAX` require a process restart. Acceptable for a single-user app and matches every other settings-driven knob."
  - "Env-var override prefix is the BARE field name (`AGENT_TOKEN_PREFIX`, `AGENT_FILE_CHUNK_MAX`), not `PHAZE_AGENT_*`. The `Settings` class does not declare `env_prefix='PHAZE_'`. The plan's acceptance-criteria env names used the `PHAZE_` prefix and are corrected here."
  - "`agent_token_prefix` carries an inline `# noqa: S105  # nosec B105` because ruff flags any string literal containing `agent_` (or any password-like name) as S105 — false positive: it's a token-namespace identifier, not a secret."
metrics:
  duration: "10m 52s"
  completed: "2026-05-12T00:48:39Z"
  tasks_completed: 3
  files_modified: 3
  tests_passing: 823
  phase_25_router_tests: 32
---

# Phase 25 Plan 06: main.py wiring + config knobs Summary

Integration plan that wires the five phase-25 internal-agent routers (built in Wave 3
Plans 03/04/05) into the production FastAPI app via `phaze.main:create_app`, and adds
two env-configurable `Settings` fields (`agent_token_prefix`, `agent_file_chunk_max`)
that the routers + schemas now read.

## One-liner

All five `/api/internal/agent/*` routers reach production traffic; OpenAPI `bearerAuth`
lock icon renders on the production app; Plan-03's `test_missing_auth_returns_401`
transitions from Wave-3 404 to a strict 401 + `WWW-Authenticate: Bearer`.

## What was built

### Task 1 — `src/phaze/config.py` (commit `1321b07`)

Appended two fields to `Settings`:

```python
# Internal agent API (Phase 25)
agent_token_prefix: str = "phaze_agent_"  # noqa: S105  # nosec B105 -- token-namespace prefix, not a password
agent_file_chunk_max: int = 1000
```

Phase 29's `just generate-agent-token` tooling will read `agent_token_prefix`. The chunk-cap
setting is consumed by `schemas/agent_files.py` (Task 2) and the production app's files
router.

### Task 2 — `src/phaze/schemas/agent_files.py` (commit `078439e`)

Replaced the literal `Field(min_length=1, max_length=1000)` in `FileUpsertChunk` with a
module-level constant resolved from settings at import time:

```python
from phaze.config import settings

_CHUNK_MAX: int = settings.agent_file_chunk_max
"""Server-side cap on chunk size. Configurable via AGENT_FILE_CHUNK_MAX env var.

Resolved at module-import time; env override at runtime requires a process restart.
"""

# ...

class FileUpsertChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[FileUpsertRecord] = Field(min_length=1, max_length=_CHUNK_MAX)
```

Plan-03's `test_chunk_cap_exceeded_422` (1001-record body) remains green because the
default is preserved at 1000.

### Task 3 — `src/phaze/main.py` (commit `cd690cf`)

Two surgical edits to `phaze.main`:

1. Expanded `from phaze.routers import companion, cue, ...` to a parenthesized
   multi-line import alphabetically including the five new modules
   (`agent_execution`, `agent_files`, `agent_fingerprint`, `agent_heartbeat`,
   `agent_metadata`). **`agent_auth` intentionally omitted** per D-09 — it exports
   `get_authenticated_agent` as a helper, not a router.

2. Added five grouped `app.include_router(...)` calls after the existing 12, under
   a single comment `# Phase 25 internal-agent routers (D-10)`.

Total `include_router` calls: 12 → 17.

## Verification

| Gate | Result |
|------|--------|
| `grep -c 'app.include_router' src/phaze/main.py` | **17** (expected 17) |
| `grep -c 'agent_auth' src/phaze/main.py` | **0** (expected 0 — helper, not router) |
| `grep -F 'agent_file_chunk_max: int = 1000' src/phaze/config.py` | matches |
| `grep -c 'max_length=1000' src/phaze/schemas/agent_files.py` | **0** (literal removed) |
| `grep -c 'max_length=_CHUNK_MAX' src/phaze/schemas/agent_files.py` | **1** |
| Production app smoke: 6 phase-25 routes registered | pass (all `agent_*` paths reachable) |
| OpenAPI bearerAuth scheme present (`type=http, scheme=bearer`) | pass |
| Strict 401 + `WWW-Authenticate: Bearer` on `POST /api/internal/agent/files` | pass |
| `tests/test_routers/test_agent_files.py::test_missing_auth_returns_401` | pass (was acceptable-404 in Wave 3, now strict 401) |
| Full test suite | **823 passed** in 1m43s |
| `uv run mypy` on all 3 modified files | `Success: no issues found in 3 source files` |
| `uv run ruff check` on all 3 modified files | `All checks passed!` |
| Pre-commit on all 3 modified files | all hooks pass |

## OpenAPI bearerAuth on production app

```json
{
  "type": "http",
  "description": "Per-agent bearer token. Format: phaze_agent_<32 urlsafe-base64 bytes>.",
  "scheme": "bearer"
}
```

The Swagger lock icon now renders against the production app's `/docs`.

## Settings environment-variable reference (Phase 29 docker-compose)

| Setting field | Env var (this codebase) | Default |
|---|---|---|
| `agent_token_prefix` | `AGENT_TOKEN_PREFIX` | `phaze_agent_` |
| `agent_file_chunk_max` | `AGENT_FILE_CHUNK_MAX` | `1000` |

**Important:** `Settings` does **not** declare `env_prefix='PHAZE_'`. Env vars must be set
without the `PHAZE_` prefix. The plan's acceptance-criteria env names used the `PHAZE_`
prefix and are corrected above. The existing docker-compose `PHAZE_DEBUG=true` override
is a pre-existing no-op (it is silently ignored by pydantic-settings because the field
is named `debug` and there is no `env_prefix`). That inconsistency is out of scope for
this plan and tracked separately if/when it matters operationally.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Critical correctness] Document the actual env-var names**

- **Found during:** Task 1 verification
- **Issue:** The plan's Task 1 acceptance criterion asserted that
  `PHAZE_AGENT_FILE_CHUNK_MAX=500 uv run python -c '...'` would print `500`. It does not,
  because pydantic-settings reads bare-field names by default (no `env_prefix`). The
  smoke test would technically fail as written.
- **Fix:** The actual env-var name is `AGENT_FILE_CHUNK_MAX`. Verified via
  `AGENT_FILE_CHUNK_MAX=500 uv run --quiet python -c "from phaze.config import Settings; print(Settings().agent_file_chunk_max)"` printing `500`. Documented above + in
  Task 1 commit message + in this Summary for Phase 29 docs to consume the correct names.
- **Files modified:** none — purely a docs correction; no code change needed.
- **Commit:** documented in `1321b07` commit message.

**2. [Rule 1 - Bug / lint] Added `noqa: S105` to `agent_token_prefix`**

- **Found during:** Task 1 ruff check
- **Issue:** Ruff S105 (hardcoded-password) fired on
  `agent_token_prefix: str = "phaze_agent_"`. The value is a token-namespace identifier
  (compare `ghp_`, `slack_`), not a password.
- **Fix:** Added `# noqa: S105  # nosec B105 -- token-namespace prefix, not a password`
  inline. Matches the existing style for the `api_host: str = "0.0.0.0"  # noqa: S104  # nosec B104`
  line directly above.
- **Files modified:** `src/phaze/config.py`.
- **Commit:** `1321b07`.

No architectural changes; no Rule-4 checkpoints needed.

## Authentication gates

None — this is wiring, not new auth surface. The auth dependency already existed in
`src/phaze/routers/agent_auth.py` (Plan 02) and was wired into the per-router handlers
in Plans 03/04/05. Plan 06 only registers those routers in the app.

## Test count delta

- Phase 25 added **32 router tests** total across Plans 02-05 (6 auth + 9 files + 3
  metadata + 3 fingerprint + 7 execution-log + 4 heartbeat).
- Plan 01 added the `seed_test_agent` + `authenticated_client` fixtures (consumed by
  every router test) and migration 014 (`agents.last_status` JSONB + token_hash partial
  index).
- Plan 06 adds **0 new tests** — it activates a Plan-03 test (`test_missing_auth_returns_401`)
  that was written to accept Wave-3 404 and now lights up the strict 401 path.
- Full suite: **823 passed** in 1m43s (no regression).

## Threat model — post-implementation

All declared `mitigate` dispositions in `<threat_model>` are addressed by this plan's
acceptance gates:

- **T-25-06-T (boot-time AttributeError on `agent_auth.router`):** mitigated by the
  acceptance-criteria assertion `grep -c "agent_auth" src/phaze/main.py` returns 0.
- **T-25-06-E (operator-route prefix overlap):** all five new routers use the explicit
  `/api/internal/agent/<resource>` prefix; existing operator routers use disjoint
  prefixes (`/api/v1`, `/api/companion`, etc.). The grouped `# Phase 25 internal-agent routers (D-10)`
  comment in `main.py` makes the boundary visually obvious for operator review.

No new threat surface was introduced beyond what the plan's `<threat_model>` declared.

## Self-Check: PASSED

Verified via file-existence + git-log scan:

- `src/phaze/config.py` — modified, contains `agent_file_chunk_max: int = 1000`
- `src/phaze/schemas/agent_files.py` — modified, contains `max_length=_CHUNK_MAX`
- `src/phaze/main.py` — modified, contains 17 `app.include_router(...)` calls
- Commit `1321b07` (Task 1) — present in `git log`
- Commit `078439e` (Task 2) — present in `git log`
- Commit `cd690cf` (Task 3) — present in `git log`
- This SUMMARY file at `.planning/phases/25-internal-agent-http-api-bearer-auth/25-06-SUMMARY.md` — created
