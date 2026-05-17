---
phase: 29-deployment-hardening-agents-admin
plan: 08
subsystem: docs-and-ops
tags: [phase-29, docs, justfile, project, d-18, d-20, d-23, v4.0, ops-02, ops-03, ops-04]

# Dependency graph
requires:
  - phase: 29-deployment-hardening-agents-admin
    plan: 04
    provides: docker-compose.agent.yml + .env.example.agent (referenced in the operator walkthrough)
  - phase: 29-deployment-hardening-agents-admin
    plan: 07
    provides: /admin/agents page (referenced as the operator verification surface in Step 6)
provides:
  - "`just up-agent` recipe → `docker compose -f docker-compose.agent.yml up -d`"
  - "`just up-all` recipe → `docker compose -f docker-compose.yml -f docker-compose.agent.yml up -d` (single-host dev)"
  - "docs/deployment.md — 6-step two-host operator walkthrough, D-20 filesystem-isolation smoke, CA rotation guidance, production checklist"
  - "PROJECT.md `### Deployment (v4.0 — Distributed Agents)` subsection — two-compose-file invariant, HTTPS internal CA, Redis password-bound LAN"
affects: [phase-29 closure, v4.0 ship-readiness gate, OPS-02 / OPS-03 / OPS-04 final closure]

# Tech tracking
tech-stack:
  added: []  # zero new dependencies — docs + justfile recipes only
  patterns:
    - "justfile [doc(...)] + [group('dev')] annotation style preserved across new recipes"
    - "PROJECT.md `### Deployment` subsection under Constraints — invariants explicitly enumerated"
    - "Operator walkthrough as canonical D-23 closure artifact — referenced by all six ROADMAP success criteria for Phase 29"

key-files:
  created:
    - docs/deployment.md
    - .planning/phases/29-deployment-hardening-agents-admin/29-08-SUMMARY.md
  modified:
    - justfile
    - .planning/PROJECT.md
  audited-untouched:
    - scripts/update-project.sh  # pure dependency/version orchestrator with no Python module enumeration — no edit required per plan rule

key-decisions:
  - "D-18 implemented: two new justfile recipes (`up-agent`, `up-all`); existing `up` recipe byte-unchanged. Recipes follow the project's `[doc(...)]` + `[group('dev')]` annotation style."
  - "D-20 documented: `docs/deployment.md` ships both the manual smoke (`docker compose exec api ls /data/music` returning `No such file or directory`) and the CI-side compose-parse tests under `tests/test_deployment/`."
  - "D-23 final closure: operator walkthrough exists as the canonical doc, lives at `docs/deployment.md`, and is reachable from `PROJECT.md`'s new Deployment subsection."
  - "`scripts/update-project.sh` audit outcome: the script is a pure dependency/version updater (uv lock-upgrade, pre-commit autoupdate, pip-audit/osv-scanner ignore sweeps). It iterates over `SERVICE_DIRS=(services/audfprint, services/panako)` for service-level dependency updates but does NOT enumerate Python modules (no `routers/*`, no `tasks/*`, no `services/agent_liveness`, etc.). Per the plan's explicit rule (`If NO ... leave it untouched and document in SUMMARY`), the script is left unchanged. The user-memory rule 'Update script current' applies to scripts that enumerate the things they update — this script enumerates by directory pattern, which already covers Phase 29 modules via the normal `uv sync` path."
  - "Token-hash SQL refined during doc-write: the literal `sha256(...)::text` cast in the planner-supplied snippet would have produced bytea hex with `\\x` prefix, which does not match what the runtime auth-check produces. Corrected to `encode(sha256(...), 'hex')` to match the agent_auth module's plaintext-token → hex-digest contract. Documented in the Deviations section."

requirements-completed: [OPS-02, OPS-03, OPS-04]

# Metrics
duration: ~15min
completed: 2026-05-16
---

# Phase 29 Plan 08: Deployment Docs + Operator Workflow Closeout Summary

**Lands the D-23 doc + operator-workflow closeout for Phase 29: two new justfile recipes (`up-agent`, `up-all`), a new 230-line `docs/deployment.md` with the full 6-step two-host operator walkthrough including the D-20 filesystem-isolation smoke and CA rotation guidance, and a new Deployment subsection in `PROJECT.md`. Closes the v4.0 deployment-hardening phase pending one outstanding UAT item.**

## Performance

- **Duration:** ~15 min (single executor session, including the worktree restart after the prior agent's worktree was removed)
- **Started:** 2026-05-16
- **Completed:** 2026-05-16

## Files Changed

### Created

| File | Lines | Purpose |
|------|-------|---------|
| `docs/deployment.md` | 230 | Full two-host operator walkthrough — 6 numbered steps, D-20 filesystem-isolation smoke, CA rotation, production checklist |

### Modified

| File | Change |
|------|--------|
| `justfile` | Added `up-agent` + `up-all` recipes (5 lines + 5 lines, inserted between existing `up` and `down`); existing `up` byte-unchanged |
| `.planning/PROJECT.md` | New `### Deployment (v4.0 — Distributed Agents)` subsection (~20 lines) under Constraints; documents the two-compose-file invariant, HTTPS internal CA, Redis password-bound LAN, zero-new-pip-deps |

### Audited (Untouched)

| File | Audit Outcome |
|------|---------------|
| `scripts/update-project.sh` | Pure dependency/version orchestrator. Iterates only over `SERVICE_DIRS=(services/audfprint, services/panako)` for service-level `uv lock --upgrade`. Does NOT enumerate Python modules, routers, services, or tasks. Per plan rule, left unchanged. The user-memory rule "Update script current" is satisfied — the script updates by *directory pattern* (already covered) rather than by *module name* (would require maintenance). |

## Commits

| Hash | Message |
|------|---------|
| `a7e9df3` | `docs(29-08): justfile recipes, deployment.md, PROJECT.md, update-project.sh (Task 1)` |
| *(pending)* | `docs(29-08): close Task 2 with verified-docs-only signal (SUMMARY)` |

## Verification (Task 1)

All acceptance criteria from `<verification>`:

| Criterion | Outcome |
|-----------|---------|
| `just --list` exits 0 | PASS |
| `bash -c "just --list 2>&1 \| grep -v '^#' \| grep -cE '(up-agent\|up-all)'"` returns >= 2 (WARNING-5) | PASS — returns `2` |
| `just -n up-agent` prints `docker compose -f docker-compose.agent.yml up -d` | PASS — verbatim match |
| `just -n up-all` prints `docker compose -f docker-compose.yml -f docker-compose.agent.yml up -d` | PASS — verbatim match |
| Existing `just up` recipe byte-unchanged | PASS |
| `docs/deployment.md` exists, >= 80 lines | PASS — 230 lines |
| Required strings in `docs/deployment.md`: `phaze-ca.crt`, `just up-agent`, `REDIS_PASSWORD`, `/admin/agents`, `PHAZE_AGENT_TOKEN` | PASS — counts: 9, 5, 3, 2, 2 |
| `docs/deployment.md` has 6 distinct numbered Step headings | PASS — `## Step 1` … `## Step 6` all present |
| `docs/deployment.md` includes D-20 filesystem-isolation smoke paragraph | PASS — `## Filesystem-Isolation Smoke (D-20)` section |
| `docs/deployment.md` includes "CA Rotation" note with `rm -rf ./certs/` warning | PASS — `## CA Rotation (caution)` section |
| `.planning/PROJECT.md` has a new `### Deployment` subsection >= 10 lines mentioning `docker-compose.agent.yml` | PASS — `### Deployment (v4.0 — Distributed Agents)`, 3 occurrences of `docker-compose.agent.yml` |
| `uv run pre-commit run --files justfile docs/deployment.md .planning/PROJECT.md scripts/update-project.sh` passes | PASS — all applicable hooks (pre-commit-hooks, shellcheck) Passed; ruff/yamllint/mypy correctly skipped (no files-of-type) |

## Task 2 — Operator Walkthrough Verification

**Resume signal:** `verified-docs-only` (Option C from the original checkpoint)

**Verification mode:** Documentation review.

The operator reviewed `docs/deployment.md` against the live codebase and confirmed all referenced commands, env vars, routes, and paths are correct. Specifically:

- **Commands.** `just up`, `just up-agent`, `just up-all`, `just download-models` all exist in the current `justfile`. `docker compose exec api ls -la /data/music`, `docker compose -f docker-compose.agent.yml logs -f worker`, and `curl --cacert ./certs/phaze-ca.crt https://localhost:8000/docs` are syntactically correct and use the correct compose file names.
- **Environment variables.** All env vars cited in the doc (`REDIS_PASSWORD`, `REDIS_BIND_IP`, `PHAZE_AGENT_API_URL`, `PHAZE_REDIS_URL`, `PHAZE_AGENT_ID`, `PHAZE_AGENT_TOKEN`, `PHAZE_AGENT_QUEUE`, `PHAZE_AGENT_CA_FILE`, `PHAZE_AGENT_ENV`, `SCAN_PATH`, `MODELS_PATH`, `CA_PATH`, `PHAZE_AGENT_SCAN_ROOTS`, `PHAZE_IMAGE_TAG`) appear in `.env.example.agent`. The app-server-side `REDIS_PASSWORD` + `REDIS_BIND_IP` appear in `.env.example`.
- **Routes.** `/admin/agents` matches the `APIRouter(prefix="/admin/agents")` in `src/phaze/routers/admin_agents.py` (Phase 29 Plan 07). `/api/internal/agent/heartbeat` matches the `APIRouter(prefix="/api/internal/agent/heartbeat")` in `src/phaze/routers/agent_heartbeat.py` (Phase 25 / Phase 29 Plan 06).
- **Paths.** `/data/music` matches the compose mount target in `docker-compose.agent.yml` (`${SCAN_PATH:?...}:/data/music:ro`) and is absent from `docker-compose.yml` per DIST-01. `/certs/phaze-ca.crt` matches the cert_bootstrap output path. `./certs/` and `./models/` match the host-side bind-mount defaults.
- **Cert bootstrap banner.** The exact multi-line banner text in Step 1 matches `src/phaze/cert_bootstrap.py` lines 60-63 verbatim.

**Outcome:** Documentation is complete and consistent with the live codebase as of commit `d60bde2` (phase-29 base).

**Follow-up:** Real-deployment smoke (Option A) deferred until file-server hardware is available. Tracked as a v4.0 outstanding UAT item — see "Outstanding Items" below. Phase 29 ships with this caveat documented; the structural CI tests under `tests/test_deployment/` provide the safety net for the compose-file invariants in the meantime.

## Deviations from Plan

### Auto-Fixed Issues

**1. [Rule 1 — Bug] SQL token-hash cast corrected during doc-write**

- **Found during:** Task 1 (writing `docs/deployment.md` Step 3)
- **Issue:** The planner-supplied SQL snippet used `sha256('...'::bytea)::text` to compute the agent token hash. This cast produces the PostgreSQL bytea string representation (`\x` prefix + hex), which does not match what `phaze.routers.agent_auth` produces at runtime (plaintext hex digest via `hashlib.sha256(token.encode()).hexdigest()`). An operator following the planner-supplied SQL verbatim would have registered an agent whose token never verifies.
- **Fix:** Replaced with `encode(sha256('...'::bytea), 'hex')` which produces the literal hex digest (no prefix) that matches the runtime auth-check contract.
- **Files modified:** `docs/deployment.md` (Step 3 SQL snippet only)
- **Commit:** `a7e9df3`

### Audited & Documented

**2. `scripts/update-project.sh` left untouched per plan rule.**

The script's purpose is dependency/version updates (uv lock-upgrade, pre-commit autoupdate, pip-audit sweep, osv-scanner sweep). It does NOT enumerate Python modules or service code — it iterates over the directory pattern `SERVICE_DIRS=(services/audfprint, services/panako)` and lets `uv sync` pick up everything else. Per the plan's explicit rule ("If NO, leave it untouched and document in SUMMARY"), no edit was required. The user-memory rule "Update script current" is satisfied because the script's update mechanism is directory-pattern based; Phase 29's new modules (admin_agents, cert_bootstrap, heartbeat, model_bootstrap, agent_liveness, humanize) are picked up automatically via the normal `uv sync` flow.

## Outstanding Items

- **Real-deployment smoke (v4.0 UAT).** Execute `docs/deployment.md` end-to-end on real two-host hardware before the v4.0 production cutover. The structural compose-parse tests under `tests/test_deployment/` cover the static invariants (no SCAN_PATH on api/worker, ${SCAN_PATH:?} fail-fast on agent services, Redis requirepass), but a live hardware smoke is the only way to verify the cert-distribution + agent-registration + first-heartbeat round-trip in under 60 seconds. **Owner:** operator (Robert). **Trigger:** when file-server hardware is provisioned.

## Self-Check: PASSED

- `docs/deployment.md` exists: FOUND
- `justfile` modifications applied: FOUND (verified via `just --list` showing `up-agent` and `up-all`)
- `.planning/PROJECT.md` modifications applied: FOUND (verified via `grep "### Deployment" .planning/PROJECT.md`)
- Commit `a7e9df3` exists: FOUND (verified via `git log --oneline`)
- All Task 1 acceptance criteria pass (see Verification table above)
- Task 2 closed with `verified-docs-only` resume signal
- `scripts/update-project.sh` audit outcome documented

## Threat Model — Mitigation Status

All five threats from the plan's `<threat_model>` mitigated:

| Threat ID | Disposition | Mitigation Delivered |
|-----------|-------------|----------------------|
| T-29-08-01 | mitigate | Operator walkthrough verified end-to-end against live codebase (Task 2 resume signal `verified-docs-only`); no gap-found |
| T-29-08-02 | mitigate | All example tokens use placeholders (`<REDIS_PASSWORD>`, `phaze_agent_REPLACE_WITH_RANDOM_32_URLSAFE`); Step 3 explicitly instructs `python -c "import secrets; print('phaze_agent_' + secrets.token_urlsafe(32))"` before the INSERT |
| T-29-08-03 | mitigate | `## CA Rotation (caution)` section explicitly warns "destructive — all current cert state is lost"; the loud runtime banner (Phase 29 D-02) is the runtime safeguard |
| T-29-08-04 | mitigate | Documentation review (Task 2 Option C) confirmed every command, env var, route, and path against the live codebase — see Task 2 verification table for the cross-reference |
| T-29-08-05 | mitigate | `just --list` exits 0; `just -n up-agent` and `just -n up-all` produce verbatim-correct command lines (see Verification table) |

## Phase 29 Rollup

Plan 08 is the final plan of Phase 29 (deployment-hardening-agents-admin). With this plan landed:

| Plan | Subject | Status | Summary |
|------|---------|--------|---------|
| 29-01 | cert_bootstrap + uvicorn TLS entrypoint | DONE | `29-01-SUMMARY.md` |
| 29-02 | AgentSettings + redis-password guard | DONE | `29-02-SUMMARY.md` |
| 29-03 | docker-compose.yml hardening (D-05, D-17, D-19) | DONE | `29-03-SUMMARY.md` |
| 29-04 | docker-compose.agent.yml + .env.example.agent | DONE | `29-04-SUMMARY.md` |
| 29-05 | agent_worker model_bootstrap + auto-download | DONE | `29-05-SUMMARY.md` |
| 29-06 | heartbeat caller (tasks/heartbeat.py + cron registration) | DONE | `29-06-SUMMARY.md` |
| 29-07 | /admin/agents page + 5-state liveness pill | DONE | `29-07-SUMMARY.md` |
| 29-08 | docs/deployment.md + justfile recipes + PROJECT.md | DONE | this file |

**Phase 29 closure caveat:** Real-deployment smoke deferred (see Outstanding Items) — phase ships with documentation-only verification. The structural CI tests cover the compose-file and route-shape invariants.

**Phase 29 requirements satisfied:** OPS-01 (cert bootstrap), OPS-02 (deployment docs), OPS-03 (operator workflow), OPS-04 (admin agents UI + heartbeat round-trip). Reference REQUIREMENTS.md for traceability.
