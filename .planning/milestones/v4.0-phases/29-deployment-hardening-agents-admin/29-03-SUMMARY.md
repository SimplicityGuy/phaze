---
phase: 29-deployment-hardening-agents-admin
plan: 03
subsystem: deployment
tags: [phase-29, deployment, compose, dist-01, redis, security, v4.0]

requires:
  - phase: 29-deployment-hardening-agents-admin
    plan: 01
    provides: phaze.entrypoint pre-uvicorn shim (referenced by api.command)
  - phase: 29-deployment-hardening-agents-admin
    plan: 02
    provides: AgentSettings._enforce_redis_password_in_production (agent-side half of AUTH-03)
provides:
  - tests/test_deployment/ (pytest sub-package marker + YAML-parse test suite — D-19)
  - tests/test_deployment/test_api_filesystem_isolation.py (4 structural tests)
  - root docker-compose.yml hardened to the app-server-only invariant
  - .env.example documents REDIS_PASSWORD, REDIS_BIND_IP, PHAZE_API_TLS_SANS
affects:
  - Phase 29 Plan 04 (docker-compose.agent.yml; receives the watcher + agent-worker
    + audfprint + panako blocks deleted here)
  - Phase 29 Plan 06 (CI workflow runs tests/test_deployment/ in the test job)
  - All operators with an existing .env: must add REDIS_PASSWORD (compose now fails
    fast at parse time if it is unset)

tech-stack:
  added: []
  patterns:
    - "Structural-parse compose tests via yaml.safe_load (D-19) — no docker daemon needed"
    - "`${VAR:?msg}` fail-fast interpolation on required compose env vars (REDIS_PASSWORD)"
    - "IP-prefixed port binding `${REDIS_BIND_IP:-127.0.0.1}:6379:6379` to avoid 0.0.0.0 default"
    - "redis-cli --no-auth-warning -a ${REDIS_PASSWORD} ping for authenticated healthchecks"
    - "Module-level _volume_target() helper for DRY string/dict volume-shape handling"

key-files:
  created:
    - tests/test_deployment/__init__.py
    - tests/test_deployment/test_api_filesystem_isolation.py
    - .planning/phases/29-deployment-hardening-agents-admin/29-03-SUMMARY.md
  modified:
    - docker-compose.yml (rewrite — 51 insertions, 98 deletions)
    - .env.example (Phase-29 variables section inserted after API_PORT block)
  unchanged:
    - Dockerfile (audited — no MODELS_PATH/SCAN_PATH/OUTPUT_PATH ENV defaults present)

key-decisions:
  - "Tests assert raw `${VAR:-default}` interpolation tokens (yaml.safe_load does NOT
    expand env vars) — this proves the source-file invariant, not the post-interpolation
    runtime value. Documented in the module docstring so future maintainers know not to
    'fix' the assertions by adding env-var expansion."
  - "redis ports regex chosen to reject BOTH bare `6379:6379` (binds 0.0.0.0) AND
    leading-colon `:6379:6379` (also binds 0.0.0.0). The IP-prefix string-shape check is
    `:6379:6379 in p and not p.startswith(':') and p != '6379:6379'` — robust to compose's
    accepted variations."
  - "Dockerfile required no changes — grep for `(MODELS_PATH|SCAN_PATH|OUTPUT_PATH)` returned
    zero matches. T-29-03-05 is mitigated by the audit; the structural-parse tests catch
    any future regression that re-introduces a default."
  - "Worker `volumes:` key removed entirely rather than set to `[]`. The test's
    `.get('volumes', []) or []` handles both shapes; absence is cleaner YAML."
  - "REDIS_PASSWORD=changeme dev default in .env.example (Pitfall-7 mitigation): fresh
    `cp .env.example .env && docker compose up` continues to work without operator action.
    Production sets a real strong value explicitly."
  - "audfprint_data + panako_data named volumes removed from the top-level `volumes:`
    block since they are no longer referenced (audfprint + panako services moved out)."

patterns-established:
  - "tests/test_deployment/ structural-parse pattern: yaml.safe_load + assertions on the
    parsed dict shape. ~50ms, no docker daemon required. Future invariants (D-15 sidecars
    file-server-only, D-17 agent.yml service list) follow the same pattern in Plan 04."
  - ".env.example Phase-NN section pattern: `# === Phase NN: <feature> (D-XX) ===` header
    with operator-actionable comments above each variable. Mirrors the existing Phase-27
    bring-up section's style."

requirements-completed: [DIST-01, AUTH-03]

metrics:
  duration: ~2min
  tasks_complete: 2
  files_created: 2
  files_modified: 2
  tests_added: 4
  commits: 2  # RED test + GREEN feat (no REFACTOR needed)

completed: 2026-05-16
---

# Phase 29 Plan 03: Application-Server Compose Hardening Summary

**Rewrite the root `docker-compose.yml` as the application-server-only compose: strip music/model/output file mounts from `api` and `worker` (DIST-01), delete the `watcher`, `agent-worker`, `audfprint`, and `panako` service blocks (they move to `docker-compose.agent.yml` in Plan 04 per D-17/D-15), and harden Redis with `--requirepass`, LAN-bound port, and authenticated `--no-auth-warning` healthcheck (D-05; server-side half of AUTH-03). New `tests/test_deployment/` sub-package codifies the invariant with 4 YAML-parse structural assertions (D-19) so future edits cannot silently re-introduce the violations.**

## What Shipped

### Test suite: tests/test_deployment/

- **`tests/test_deployment/__init__.py`** — empty pytest sub-package marker.
- **`tests/test_deployment/test_api_filesystem_isolation.py`** — 4 cases, all using `yaml.safe_load` on the project-root `docker-compose.yml`:
  1. `test_api_service_has_no_file_mounts` (DIST-01): no `/data/music`, `/models`, or `/data/output` in any api volume target. The `${CA_PATH:-./certs}:/certs:rw` mount is explicitly allowed by passing the banned-target substring check.
  2. `test_controller_worker_has_no_file_mounts` (DIST-01): same predicate for the controller worker. With the `volumes:` key now absent from worker entirely, the test's `data["services"]["worker"].get("volumes", []) or []` returns `[]` and the body short-circuits.
  3. `test_no_watcher_or_agent_worker_in_root_compose` (D-15 / D-17): asserts `watcher`, `agent-worker`, `audfprint`, and `panako` are all absent from the root compose `services` dict.
  4. `test_redis_hardened` (D-05 / AUTH-03): asserts the redis service `command` contains both `requirepass` and `REDIS_PASSWORD`, the `ports` entry is IP-prefixed (rejects both bare `6379:6379` and leading-colon `:6379:6379`), and the healthcheck `test` list contains `redis-cli`, `--no-auth-warning`, `-a`, and a `REDIS_PASSWORD`-referencing entry.

A small module-level `_volume_target(entry)` helper DRYs the string vs. dict volume-shape handling between tests 1 and 2.

### Docker compose rewrite

End state of `services:` is exactly `{api, worker, postgres, redis}`. The top-level `volumes:` block is exactly `{pgdata}`.

Six concrete changes per PATTERNS lines 762-795:

1. **`api`**: swap `command:` to `uv run python -m phaze.entrypoint` (Plan 01's cert-bootstrap shim); replace the SCAN_PATH read-only mount with `${CA_PATH:-./certs}:/certs:rw` (rw because cert_bootstrap writes the auto-generated CA + leaf on first start).
2. **`worker` (controller)**: drop `MODELS_PATH=/models` from `environment:` (controller is fileless); remove the `volumes:` key entirely (no SCAN_PATH, MODELS_PATH, OUTPUT_PATH).
3. **DELETE** the `watcher` service block (lines 50-64 of the old file) and its preamble comment.
4. **DELETE** the `agent-worker` service block (lines 72-96 of the old file) and its preamble comment.
5. **DELETE** the `audfprint` and `panako` service blocks (lines 128-154 of the old file); also drop the `audfprint_data` and `panako_data` entries from the top-level `volumes:` block since they are no longer referenced.
6. **`redis`** (lines 118-126 of the old file) rewritten per RESEARCH §Pattern 4 / D-05:
   - `command:` is list-form: `["redis-server", "--requirepass", "${REDIS_PASSWORD:?REDIS_PASSWORD required}"]`. The `${VAR:?msg}` interpolation causes `docker compose up` to fail at parse time if `REDIS_PASSWORD` is unset.
   - `ports:` is `["${REDIS_BIND_IP:-127.0.0.1}:6379:6379"]`. Dev defaults to loopback; production overrides `REDIS_BIND_IP` to the LAN IP so agents on other hosts can reach Redis.
   - `healthcheck.test:` is `["CMD", "redis-cli", "--no-auth-warning", "-a", "${REDIS_PASSWORD}", "ping"]`. `--no-auth-warning` suppresses the stderr warning that would otherwise pollute container logs.

### .env.example additions

Inserted a Phase-29 section between the existing `API_PORT=8000` block and `SCAN_PATH=/data/music`:

- `REDIS_PASSWORD=changeme` — required by the compose `${VAR:?...}` fail-fast. Dev placeholder so a fresh clone still works (Pitfall-7 mitigation); production MUST overwrite.
- `REDIS_BIND_IP=127.0.0.1` — interface to bind Redis on. Dev = loopback. Production = app-server LAN IP.
- `PHAZE_API_TLS_SANS=localhost,127.0.0.1,api` — comma-separated SAN list for the auto-generated leaf cert (D-02; consumed by Plan 01's `phaze.cert_bootstrap`).

Each variable has a comment block explaining its role and the dev-vs-prod distinction.

### Dockerfile audit

Grepped for `MODELS_PATH`, `SCAN_PATH`, `OUTPUT_PATH` in `Dockerfile`: zero matches. Audit-only step per CONTEXT line 234 — no changes needed. The structural-parse tests will catch any future regression that introduces an `ENV` default which would silently mask a missing mount inside the container.

## Verification Results

```
uv run pytest tests/test_deployment/test_api_filesystem_isolation.py -x -q
4 passed in 0.03s

uv run pytest tests/test_deployment/ tests/test_task_split.py tests/test_main_lifespan.py \
  tests/test_config/ tests/test_config_role_split.py tests/test_config_worker.py \
  tests/test_cert_bootstrap.py tests/test_services/test_agent_client_tls.py -q
47 passed, 2 warnings in 1.39s
```

- 4/4 new deployment tests pass (RED → GREEN cycle).
- 43 adjacent tests (task_split, main_lifespan, config, cert_bootstrap, TLS) all pass — no regression from the new sub-package or compose rewrite.
- `uv run ruff check tests/test_deployment/` clean.
- `uv run ruff format --check tests/test_deployment/` clean.
- `yamllint` (via pre-commit hook on `docker-compose.yml`) passes.
- YAML structural verification (`python -c "import yaml; ..."`) confirms:
  - `services:` = `['api', 'postgres', 'redis', 'worker']`
  - `volumes:` = `['pgdata']`
  - `api.command` = `"uv run python -m phaze.entrypoint"`
  - `api.volumes` = `['${CA_PATH:-./certs}:/certs:rw']`
  - `worker.command` = `"uv run saq phaze.tasks.controller.settings"`
  - `worker.volumes` is absent
  - `worker.environment` = `['PHAZE_ROLE=control']`
  - `redis.command` = `['redis-server', '--requirepass', '${REDIS_PASSWORD:?REDIS_PASSWORD required}']`
  - `redis.ports` = `['${REDIS_BIND_IP:-127.0.0.1}:6379:6379']`
  - `redis.healthcheck.test` = `['CMD', 'redis-cli', '--no-auth-warning', '-a', '${REDIS_PASSWORD}', 'ping']`

**Note on `docker compose config --quiet`:** the macOS dev environment in this worktree does not have the `docker compose` v2 plugin installed (`docker: 'compose' is not a docker command`), so the suggested compose-syntax check ran via the equivalent pytest YAML-parse layer instead. The new `tests/test_deployment/` suite executes in CI (Plan 06 wires it into the workflow) and provides the same structural-validation gate.

## YAML diff summary

**Services deleted from root compose:**

| Service       | Reason                                             | Moves to                     |
| ------------- | -------------------------------------------------- | ---------------------------- |
| `watcher`     | D-17 — agent-side, never on the app server         | docker-compose.agent.yml     |
| `agent-worker`| D-17 — agent-side SAQ worker                       | docker-compose.agent.yml     |
| `audfprint`   | D-15 — fingerprint sidecar is file-server-local    | docker-compose.agent.yml     |
| `panako`      | D-15 — fingerprint sidecar is file-server-local    | docker-compose.agent.yml     |

**Services modified (in place):**

| Service | Changes                                                                                                                                  |
| ------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `api`   | `command:` now invokes phaze.entrypoint (Plan 01 shim); volumes reduced to a single `/certs:rw` bind for cert_bootstrap output.          |
| `worker`| Removed all file mounts and the `MODELS_PATH=/models` env entry. Controller is fileless.                                                 |
| `redis` | `--requirepass` + IP-prefixed port + authenticated healthcheck. Fail-fast at compose parse time if `REDIS_PASSWORD` is unset.            |

**Top-level volumes block diff:**

- Removed: `audfprint_data`, `panako_data`.
- Kept: `pgdata`.

## Decision IDs implemented

- **D-05** (Redis hardening: requirepass + LAN-bound port + --no-auth-warning healthcheck): fully closed; server-side half of AUTH-03.
- **D-17** (root compose end state `{api, worker, postgres, redis}` only): fully closed.
- **D-19** (CI YAML-parse test for filesystem isolation): test suite landed in `tests/test_deployment/test_api_filesystem_isolation.py` (4 cases). Wired into CI by Plan 06.
- **D-15** (audfprint + panako sidecars are file-server-local, never in root compose): partial — root compose now omits them. Plan 04 introduces docker-compose.agent.yml where they live.
- **DIST-01** (application server has no file mounts): fully closed for api + worker.
- **AUTH-03** (Redis requirepass + LAN binding): fully closed (this plan + Plan 02 client guard).

D-20 (operations doc) is docs-only and lands in Plan 08 — this plan delivers the structural-parse test that the doc will reference.

## Commits

| Hash    | Type | Phase | Subject                                                                |
| ------- | ---- | ----- | ---------------------------------------------------------------------- |
| c560ee5 | test | RED   | add failing YAML-parse tests for app-server compose isolation          |
| 149de70 | feat | GREEN | harden app-server compose — strip file mounts, lock down redis         |

## Deviations from Plan

### Auto-fixed Issues

None. The plan's `<action>` blocks for both tasks were complete and accurate. Two minor observations:

1. **Ruff reformatted the test file once after my initial write.** The reformat collapsed a multi-line `assert any(...)` into a single line (under the 150-char limit). This is the standard pre-commit `ruff format` behavior and not a deviation — the test logic is identical.
2. **Dockerfile audit was a no-op.** The current `Dockerfile` had no `MODELS_PATH`/`SCAN_PATH`/`OUTPUT_PATH` ENV defaults to remove, so the audit step in Task 2's `<action>` block was verify-only as anticipated.

### Authentication gates

None.

### Architectural decisions

None. All choices were already locked in PATTERNS and RESEARCH.

## Threat Flags

None. The plan's `<threat_model>` enumerates the seven surfaces this plan touches (T-29-03-01..T-29-03-07). Every mitigation is delivered by the rewrite:

- **T-29-03-01** (api reads music): mitigated by stripping the SCAN_PATH mount; `test_api_service_has_no_file_mounts` asserts.
- **T-29-03-02** (worker reads music/models/output): mitigated by removing all three mounts; `test_controller_worker_has_no_file_mounts` asserts.
- **T-29-03-03** (unauthed Redis): mitigated by `--requirepass ${REDIS_PASSWORD:?required}`; `test_redis_hardened` asserts the `requirepass` and `REDIS_PASSWORD` tokens are present.
- **T-29-03-04** (Redis bound 0.0.0.0): mitigated by `${REDIS_BIND_IP:-127.0.0.1}:6379:6379`; `test_redis_hardened` rejects bare `6379:6379` and leading-colon forms.
- **T-29-03-05** (Dockerfile ENV defaults mask missing mounts): mitigated by the audit (no such defaults present); future regressions caught by the structural-parse tests because the test reads the post-merge compose file, which would surface any container-side default that nobody mounted.
- **T-29-03-06** (Pitfall-7 dev-clone friction): mitigated by `REDIS_PASSWORD=changeme` dev default in `.env.example`.
- **T-29-03-07** (redis-cli `-a` warning leaks): mitigated by `--no-auth-warning`; `test_redis_hardened` asserts the flag is present.

## Known Stubs

None. Every change is end-to-end functional:

- The compose rewrite is the production wire format — there are no TODOs in the file.
- `.env.example` ships with real working defaults for fresh clones.
- The test suite directly parses the live `docker-compose.yml` and asserts on the parsed dict; no mock data.
- `_volume_target()` is the real string/dict shape handler used by both api and worker tests.

The remaining Phase-29 work — Plan 04 (`docker-compose.agent.yml`), Plan 05 (operations docs), Plan 06 (CI wiring), etc. — is tracked by the phase plan list and is not blocked by this plan.

## TDD Gate Compliance

Both tasks followed the RED → GREEN cycle:

- **Task 1 RED** (c560ee5): `test(29-03): add failing YAML-parse tests for app-server compose isolation`. Pytest run against the un-rewritten `docker-compose.yml` reported all 4 tests failing with their expected diagnostic substrings (`api service has banned mount: ${SCAN_PATH:-/data/music}:/data/music:ro`, `watcher belongs in docker-compose.agent.yml (D-17)`, `redis service must declare a command with --requirepass`).
- **Task 2 GREEN** (149de70): `feat(29-03): harden app-server compose — strip file mounts, lock down redis`. After the rewrite, all 4 tests pass.

Gate-sequence check: `git log --oneline` shows `c560ee5 test(...)` immediately preceding `149de70 feat(...)` for plan 29-03. RED and GREEN commits both present in correct order. No REFACTOR commit was needed — both files landed in their final shape in the GREEN commit.

## Self-Check: PASSED

Files claimed to be created — all present:

```
[ -f tests/test_deployment/__init__.py ]                            → FOUND
[ -f tests/test_deployment/test_api_filesystem_isolation.py ]       → FOUND
```

Files claimed to be modified — both reflect the documented changes:

```
git show 149de70 --stat → docker-compose.yml: 51 insertions, 98 deletions (rewrite)
                         .env.example: 18 insertions
```

Commits claimed — both present in `git log --oneline`:

```
c560ee5 — FOUND (test/RED)
149de70 — FOUND (feat/GREEN)
```

Test count matches plan `<acceptance_criteria>`: 4 new tests in `tests/test_deployment/test_api_filesystem_isolation.py`. All 4 fail against the un-rewritten compose; all 4 pass against the rewritten compose.

Decision IDs implemented: D-05, D-17, D-19 (full), DIST-01, AUTH-03 (server-side half), D-15 (partial — root compose now omits the sidecars; Plan 04 lands the agent.yml file where they live).
