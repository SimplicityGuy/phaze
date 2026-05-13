---
phase: 27-watcher-service-user-initiated-scan
plan: 07
subsystem: deployment-and-docs
tags:
  - deployment
  - docs
  - compose
requires:
  - phaze.agent_watcher.__main__ (Phase 27 Plan 05 -- entry point uv run python -m phaze.agent_watcher)
  - phaze.config.AgentSettings.watcher_* + scan_chunk_size (Phase 27 Plan 01 -- four optional env vars)
  - docker-compose.yml api/worker/audfprint/panako service blocks (Phase 25, 26)
provides:
  - "docker-compose.yml 'watcher' service block (D-19) -- runs uv run python -m phaze.agent_watcher with PHAZE_ROLE=agent, SCAN_PATH:/data/music:ro volume mount, depends_on api: service_started, restart: unless-stopped"
  - "src/phaze/agent_watcher/README.md per-service README (memory rule: feedback_readme_per_service / D-24)"
  - ".env.example documents four optional watcher tunables (PHAZE_WATCHER_SETTLE_SECONDS=10, PHAZE_WATCHER_MAX_PENDING_SECONDS=3600, PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS=2, PHAZE_SCAN_CHUNK_SIZE=500)"
  - "STATE.md Phase 27 decision accumulation (9 entries across plans 27-01 to 27-07; D-24 STATE.md surface)"
affects:
  - docker-compose.yml (new service block inserted at lines 47-64, between worker and postgres)
  - .env.example (8-line block appended after SCAN_PATH=)
  - src/phaze/agent_watcher/README.md (new file)
  - .planning/STATE.md (9 new Phase 27 bullets under ## Accumulated Context > Decisions)
tech_stack:
  added: []
  patterns:
    - "Compose service block analog: copy structure of `worker:` (Dockerfile build, env_file, environment list, volumes, depends_on, restart) and diff only the command + role + dependency conditions per D-19"
    - "depends_on api: service_started (NOT service_healthy) for any service waiting on the api -- the api service has no healthcheck (Phase 25 invariant); Pitfall 6 budget absorbs the ~63s uvicorn boot via whoami_with_retry on the watcher side"
    - "Volume mount :ro for fileless-write services (DIST-04 invariant on the watcher; no MODELS_PATH / OUTPUT_PATH because the watcher only reads files for SHA-256 + stat)"
    - "Optional env-var documentation pattern in .env.example: '# explanatory comment' + '# VAR=default' (commented-out line == opt-in override)"
    - "STATE.md decision-accumulation: append-only to ## Accumulated Context > Decisions in the per-plan executor; orchestrator owns frontmatter / Current Position / last_updated at merge time"
key_files:
  created:
    - src/phaze/agent_watcher/README.md
  modified:
    - docker-compose.yml
    - .env.example
    - .planning/STATE.md
decisions:
  - "Per-plan executor wrote the STATE.md ## Accumulated Context > Decisions appendix only (9 new [Phase 27-*] bullets); frontmatter, Current Position, last_updated, last_activity, and progress fields are orchestrator-owned at merge time per the per-plan exception encoded in the plan's <objective>"
  - "docker compose config validation runnable in the dev environment (Docker Desktop available + temporary .env copy from .env.example) -- exit 0; the watcher service resolves to the expected command list, depends_on map, and volume mount"
  - "PHAZE_AGENT_QUEUE removed from the inline comment in docker-compose.yml watcher block (Phase 26 D-10 deprecates the env var; queue name is derived from the token-encoded agent_id). The plan's PATTERNS.md reference still listed it; intentionally dropped because it's a no-op as of Phase 26"
metrics:
  duration_minutes: 4
  completed_date: 2026-05-13
  tasks_completed: 3
  commits: 3
  tests_added: 0
  tests_passing: 0  # plan is docs/config only -- no test surface
  files_created: 2  # src/phaze/agent_watcher/README.md + this SUMMARY.md
  files_modified: 3  # docker-compose.yml + .env.example + .planning/STATE.md
---

# Phase 27 Plan 07: Deployment & Docs Summary

Phase 27 lands `docker compose up watcher` -- the watcher service block is now part of the root `docker-compose.yml`, the per-service `src/phaze/agent_watcher/README.md` documents the operator-facing surface (entry point, env vars, import-boundary invariant, Phase 29 migration plan, operational notes), `.env.example` covers the four new optional tunables landed by Plan 27-01, and `.planning/STATE.md` accumulates 9 Phase 27 decisions for the next planner. Phase 27 is operationally complete.

## What Was Built

**Three atomic commits, one per task:**

| Commit  | Task | Description |
| ------- | ---- | ----------- |
| 6287255 | 1    | docker-compose.yml: new `watcher:` service block at lines 47-64 (3 comment lines + 14 YAML lines) inserted between `worker:` and `postgres:`. Build context same as worker, `command: uv run python -m phaze.agent_watcher`, `environment: PHAZE_ROLE=agent`, single `${SCAN_PATH:-/data/music}:/data/music:ro` mount, `depends_on api: condition: service_started`, `restart: unless-stopped`. No `redis` / `postgres` dependency (DIST-04 invariant). No `MODELS_PATH` / `OUTPUT_PATH` (watcher is fileless-write). .env.example: 8-line documentation block appended after `SCAN_PATH=/data/music`, each tunable's default value commented out so the operator opts-in to override. |
| d5b2866 | 2    | src/phaze/agent_watcher/README.md (41 lines, ASCII-clean): H1 + 7 sections covering Purpose, Entry point, Required env vars, Optional tunable env vars, Import-boundary invariant, Phase 29 migration note, Operational notes (restart-loop diagnostics, NFS/FUSE PollingObserver fallback, no-catch-up-on-startup rationale per D-04). Closes memory rule `feedback_readme_per_service`. |
| ad7159d | 3    | .planning/STATE.md: 9 new bullets appended to ## Accumulated Context > Decisions, one per major Phase 27 decision domain (27-01 _shared.agent_bootstrap + 4 AgentSettings tunables, 27-02 FileUpsertChunk.batch_id optional + LIVE-sentinel resolution, 27-03 PATCH state machine, 27-04 scan_directory chunking + Postgres-free import boundary, 27-05 thread bridge + stuck-file cap + chunk-of-1 batch_id omission, 27-06 HTMX terminal-state halt + N+1-avoidance ORM attrs, 27-07 compose service + liveness mechanism). Frontmatter, Current Position, last_updated, last_activity untouched -- orchestrator owns them at merge time per the per-plan override in the plan's <objective>. |

## Output Asks Resolved

The plan's `<output>` block asked six specific questions:

1. **Exact line range of the watcher service block in docker-compose.yml** -> Lines **47-64** (3 comment lines at 47-49 + service block at 50-64). Inserted between the existing `worker:` block (ending at line 45) and the `postgres:` block (now at line 66, was line 47 pre-edit).
2. **Whether `docker compose config` was runnable in the dev environment** -> **YES** (Docker Desktop is available on this macOS dev host). Verification flow: `cp .env.example .env && docker compose config > /dev/null && rm .env` -> exit 0. The watcher service resolves to the expected command list (`[uv, run, python, -m, phaze.agent_watcher]`), `depends_on.api.condition: service_started`, and the single `:ro` SCAN_PATH bind mount. No deviation surfaced.
3. **Final line count of src/phaze/agent_watcher/README.md** -> **41 lines** (≥ 30 acceptance threshold). Includes H1 + 7 sections + spacing.
4. **Number of `[Phase 27` decisions accumulated in STATE.md** -> **9 entries** (target was ≥ 5). Covers all seven Phase 27 plans (one bullet per plan, with two double-bullets for 27-01 and 27-05 where multiple decision domains landed in the same plan).
5. **CLAUDE.md unchanged confirmation (Phase 27 D-24)** -> **CONFIRMED.** `git diff HEAD~3 HEAD -- CLAUDE.md` returns 0 lines. ROADMAP.md is also unchanged in this plan's diff (orchestrator owns that file).
6. **Phase 27 final progress percent** -> The per-plan executor did NOT increment STATE.md `progress.completed_phases` -- per the plan's per-phase orchestrator override, that field is owned at merge time. Pre-merge: `progress.completed_phases = 3`, `total_phases = 6`, `total_plans = 33`, `completed_plans = 26` (Phase 26 finalization). Post-merge target (orchestrator's responsibility): `completed_phases = 4`, `completed_plans = 33`, `percent = round(33/33*100, 0) = 100` if v4.0 is exactly Phase 27 sized, or recomputed against the canonical v4.0 plan count if more phases remain. The 9 STATE.md decision entries are the canonical Phase 27 closure deliverable; the percent recalc is mechanical.

## Verification

The plan's full `<verification>` block:

- `uv run python -c "import yaml; data=yaml.safe_load(open('docker-compose.yml').read()); assert 'watcher' in data['services']"` -> **exit 0**
- `grep -q "PHAZE_WATCHER_SETTLE_SECONDS" .env.example` -> **exit 0**
- `grep -q "\[Phase 27" .planning/STATE.md` -> **exit 0** (9 matches)
- `grep -q "phaze.database" src/phaze/agent_watcher/README.md` -> **exit 0** (import-boundary section)
- pre-commit hooks ran on every commit (no `--no-verify`); all skipped/passed (no language-specific files in this plan that would invoke ruff/mypy/bandit)
- `docker compose config > /dev/null` (with a temporary `.env` copied from `.env.example`) -> **exit 0**

## Acceptance Criteria -- Grep Confirmations

**Task 1 (docker-compose.yml + .env.example):**

| Criterion | Required | Actual |
| --------- | -------- | ------ |
| `grep -c "^  watcher:" docker-compose.yml` | `= 1` | **1** |
| `grep -c "uv run python -m phaze.agent_watcher" docker-compose.yml` | `= 1` | **1** |
| `grep -c "PHAZE_ROLE=agent" docker-compose.yml` | `>= 1` | **1** |
| `grep -c "restart: unless-stopped" docker-compose.yml` | `>= 1` | **3** (watcher + audfprint + panako) |
| `grep -c "PHAZE_WATCHER_SETTLE_SECONDS" .env.example` | `= 1` | **1** |
| `grep -c "PHAZE_WATCHER_MAX_PENDING_SECONDS" .env.example` | `= 1` | **1** |
| `grep -c "PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS" .env.example` | `= 1` | **1** |
| `grep -c "PHAZE_SCAN_CHUNK_SIZE" .env.example` | `= 1` | **1** |
| no `redis:` or `postgres:` under watcher depends_on | invariant | **OK** (asserted in YAML python check) |
| no `MODELS_PATH` / `OUTPUT_PATH` in watcher env or volumes | invariant | **OK** (asserted in YAML python check) |
| all watcher volume mounts contain `:ro` | invariant | **OK** (asserted in YAML python check) |
| `uv run python -c "import yaml; yaml.safe_load(...)"` | exit 0 | **0** |
| `docker compose config > /dev/null` (with temp .env) | exit 0 | **0** |

**Task 2 (src/phaze/agent_watcher/README.md):**

| Criterion | Required | Actual |
| --------- | -------- | ------ |
| file exists | yes | **yes** |
| `wc -l < src/phaze/agent_watcher/README.md` | `>= 30` | **41** |
| 4 required env vars present (PHAZE_ROLE, PHAZE_AGENT_API_URL, PHAZE_AGENT_TOKEN, PHAZE_AGENT_SCAN_ROOTS) | yes | **all 4** |
| 4 tunable env vars present | yes | **all 4** |
| "Phase 29" appears | yes | **yes** |
| "phaze.database" appears | yes | **yes** |
| entry-point command "uv run python -m phaze.agent_watcher" appears verbatim | yes | **yes** |
| no emojis (ASCII-only) | yes | **yes** (`LANG=C grep -P '[^\x00-\x7F]'` returns no matches) |

**Task 3 (.planning/STATE.md):**

| Criterion | Required | Actual |
| --------- | -------- | ------ |
| `grep -c "\[Phase 27" .planning/STATE.md` | `>= 5` | **9** |
| YAML frontmatter parses via `yaml.safe_load` | yes | **yes** |
| CLAUDE.md unchanged in this plan's diff | yes | **yes** (0 lines diff) |
| ROADMAP.md unchanged in this plan's diff | yes | **yes** (0 lines diff -- orchestrator-owned) |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocker] STATE.md frontmatter / Current Position / progress fields NOT updated in-place**

- **Found during:** Task 3, reading the plan's `<objective>` per-phase override.
- **Issue:** The plan's `<action>` step in Task 3 prescribes updates to STATE.md frontmatter (status, stopped_at, last_updated, last_activity, progress.completed_phases, progress.total_plans, progress.completed_plans, progress.percent) and the `## Current Position` section. The orchestrator override in the spawning prompt explicitly states: "do NOT modify frontmatter, Current Position, last_updated, last_activity, or any other orchestrator-controlled fields ... You may also append new entries under STATE.md's 'Decisions' section (under '## Accumulated Context')." A naive read of the plan would have me overwrite orchestrator-owned fields and create a merge conflict.
- **Fix:** Restricted Task 3 to the append-only ## Accumulated Context > Decisions edit. The 9 new [Phase 27-*] bullets capture the decision trail per the plan's verification gate (`grep -c "\[Phase 27" >= 5`). Frontmatter and Current Position are left for the orchestrator's merge-time STATE.md update step. The plan's `<verification>` block's only mandatory acceptance check on STATE.md is the `[Phase 27` grep (≥ 5 hits) and YAML-parses-cleanly, both of which pass after this restricted edit.
- **Files modified:** `.planning/STATE.md` (only the Decisions section under ## Accumulated Context)
- **Commit:** ad7159d

**2. [Rule 1 - Bug] PHAZE_AGENT_QUEUE removed from watcher's environment-list comment**

- **Found during:** Task 1, cross-referencing the PATTERNS.md compose block reference (line 915 lists PHAZE_AGENT_QUEUE alongside the other agent env vars).
- **Issue:** The PATTERNS.md block at lines 902-922 lists `PHAZE_AGENT_QUEUE` in the comment under the watcher's `environment:` list. Phase 26 D-10 / Phase 26 P-10 explicitly deprecated PHAZE_AGENT_QUEUE -- the queue name is now derived from the token-encoded agent_id (see Plan 27-01's _shared.agent_bootstrap construct_agent_client + the Phase 26 D-13 startup banner with auth_id_prefix=). Including the deprecated env var in the comment would mislead a future operator into setting an inert variable.
- **Fix:** Dropped `PHAZE_AGENT_QUEUE` from the inline comment. Final list: `PHAZE_AGENT_API_URL, PHAZE_AGENT_TOKEN, PHAZE_AGENT_SCAN_ROOTS`. Verified against Plan 27-05 SUMMARY's `__main__.py` description (which does NOT consume PHAZE_AGENT_QUEUE) and Plan 27-01 SUMMARY's AgentSettings field list (which has no `queue_name` field).
- **Files modified:** `docker-compose.yml` (one comment line)
- **Commit:** 6287255

### Out-of-scope discoveries

None. No `deferred-items.md` entries written. The plan touched exactly the four declared files (docker-compose.yml, .env.example, src/phaze/agent_watcher/README.md, .planning/STATE.md) and nothing else.

## Known Stubs

None. The watcher block is production-ready (Phase 27 deliverable; Phase 29 moves it but doesn't change semantics). The README documents the actual operator-facing surface as of Phase 27. The .env.example tunables match Plan 27-01's AgentSettings field defaults byte-for-byte (10 / 3600 / 2 / 500). The STATE.md decisions capture the actual decisions made in the seven Phase 27 plans (cross-checked against each plan's SUMMARY frontmatter).

## Threat Flags

None new beyond the plan's `<threat_model>`. The three documented mitigations are all in place:

- **T-27-04 (bearer token in .env.example)** -> mitigated. `.env.example` documents the env-var NAMES only (PHAZE_AGENT_TOKEN appears in the README, not in .env.example). The .env.example does NOT contain any `PHAZE_AGENT_TOKEN=phaze_agent_<value>` line, real or example. `grep -c "PHAZE_AGENT_TOKEN=phaze_agent_" .env.example` -> 0.
- **(operational) DoS via watcher restart loop on bad token** -> mitigated (inherited). Plan 27-01's `_shared.agent_bootstrap.whoami_with_retry` short-circuits on `AgentApiAuthError`. README documents the operator troubleshooting flow (`docker compose logs watcher` -> AgentApiAuthError).
- **(file-mount) tampering on /data/music** -> mitigated. Watcher's single volume mount is `:ro` (read-only). Asserted in the YAML python check (`all(':ro' in v for v in w['volumes'])`).

## Self-Check: PASSED

**Files exist:**

- FOUND: docker-compose.yml (modified)
- FOUND: .env.example (modified)
- FOUND: src/phaze/agent_watcher/README.md (created)
- FOUND: .planning/STATE.md (modified)

**Commits exist (on `worktree-agent-a36b1b5f219194d03`):**

- FOUND: 6287255 -- feat(27-07): add watcher service to docker-compose.yml and document env vars
- FOUND: d5b2866 -- docs(27-07): add per-service README for phaze.agent_watcher
- FOUND: ad7159d -- docs(27-07): accumulate Phase 27 decisions into STATE.md

**Verification gates:**

- docker compose config (with temp .env from .env.example) -> exit 0
- watcher service block has no redis / postgres in depends_on -> OK
- watcher service block has no MODELS_PATH / OUTPUT_PATH env or volume -> OK
- all watcher volume mounts contain :ro -> OK
- 9 [Phase 27 entries in STATE.md -> OK (>= 5 acceptance threshold)
- CLAUDE.md unchanged -> OK
- ROADMAP.md unchanged -> OK (orchestrator-owned)
