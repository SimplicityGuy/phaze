---
phase: 29-deployment-hardening-agents-admin
plan: 04
subsystem: deployment
tags: [phase-29, deployment, compose-agent, ghcr, docker-publish, ops-02, v4.0]

# Dependency graph
requires:
  - phase: 29-deployment-hardening-agents-admin
    plan: 03
    provides: "Root docker-compose.yml hardened to app-server-only end state {api, worker, postgres, redis}; the watcher/agent-worker/audfprint/panako blocks were deleted from root compose with the explicit comment that they move to docker-compose.agent.yml in Plan 04"
  - phase: 29-deployment-hardening-agents-admin
    plan: 02
    provides: "AgentSettings._enforce_redis_password_in_production + ._enforce_https_in_production guards (referenced in .env.example.agent comments)"
provides:
  - "docker-compose.agent.yml (NEW): standalone file-server-host compose with exactly 4 services {worker, watcher, audfprint, panako}; worker+watcher pull from ghcr.io/simplicityguy/phaze, sidecars retain build:"
  - ".env.example.agent (NEW): file-server-host env template documenting every required PHAZE_AGENT_* var + paths (D-23 portion)"
  - "tests/test_deployment/test_agent_compose.py (NEW): 5 structural-parse tests covering D-15..D-17 + WARNING-3 SCAN_PATH fail-fast + WARNING-4 docker-publish.yml tag verification"
  - ".github/workflows/docker-publish.yml extended: docker/metadata-action now emits :latest + :v<version> + :<semver> tags; api image realigned to bare-repo URL"
affects:
  - "Phase 29 Plan 06 (CI wires tests/test_deployment/test_agent_compose.py into the test job)"
  - "Phase 29 Plan 08 (deployment-doc + justfile recipe references docker-compose.agent.yml + PHAZE_IMAGE_TAG pinning guidance)"
  - "All file-server-host operators: must scp .env.example.agent → .env on each file server and populate the documented variables; missing SCAN_PATH fails at compose-parse time (no silent misconfiguration)"

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Standalone compose file pattern: `docker compose -f docker-compose.agent.yml up -d` operates on a wholly separate project name; no service/network reachable from the root compose. Mirrors the app-server vs file-server trust split (DIST-01..04)."
    - "GHCR pull-vs-build hybrid: published images for code-only services (worker, watcher), build: context for sidecars (audfprint, panako) until they get their own GHCR publish path."
    - "`${VAR:?MESSAGE}` fail-fast applied UNIFORMLY across all 4 services (WARNING-2 — no bare `:?` form anywhere). WARNING-3 test enforces via regex."
    - "Per-matrix `image_suffix` override in docker-publish.yml: api → bare-repo URL, sidecars → `/<name>` sub-path. Lets one workflow publish three different URL shapes from a 3-row matrix."

key-files:
  created:
    - docker-compose.agent.yml
    - .env.example.agent
    - tests/test_deployment/test_agent_compose.py
    - .planning/phases/29-deployment-hardening-agents-admin/29-04-SUMMARY.md
  modified:
    - .github/workflows/docker-publish.yml (tag strategy + image_suffix matrix override)
    - tests/test_phase04_gaps.py (resolved Plan 29-05 deferred test: now scans BOTH compose files)
    - .planning/phases/29-deployment-hardening-agents-admin/deferred-items.md (marked resolved)

key-decisions:
  - "Matrix `image_suffix` instead of a separate workflow: keeping the api image at the bare-repo URL (so docker-compose.agent.yml's `image: ghcr.io/simplicityguy/phaze:...` is correct) does NOT require a second workflow file. A 1-line matrix field flipping `\"\"` vs `/<name>` is the minimum-viable surgery."
  - "Resolved the Plan 29-05 deferred test inside this plan rather than punting to Plan 29-08. Reasoning: Plan 29-04 is the wave that materializes docker-compose.agent.yml; the deferred test exists exactly because the agent-worker had no compose-file home until this plan. The fix is a 4-line change (scan both files) and the gate is meaningfully restored in CI today rather than a wave later."
  - "WARNING-2 unified explicit-message form `${SCAN_PATH:?SCAN_PATH required}` on ALL 4 services. The RESEARCH excerpt's bare `${SCAN_PATH:?}` form on sidecars was superseded by WARNING-2 — bare form is correct but inconsistent; explicit-message gives an operator-actionable error on first `docker compose up`."
  - "The 5th workflow-tag test asserts BOTH `value=latest` AND a version pattern (`type=semver` OR `type=ref,event=tag`). Accepting `type=ref,event=tag` keeps the test robust to a future refactor that drops semver in favor of plain ref-tag (e.g., if version-string conventions change)."
  - "Two ways to fix WARNING-4 (extend workflow OR change agent.yml's URL). Per plan guidance preferred: extend workflow. Reasoning: GHCR allows bare-repo image URLs (this is the simpler operator mental model — `phaze` is the project, not `phaze/api`), and the sidecars naturally need sub-paths anyway."

patterns-established:
  - "Standalone-compose pattern for trust-split deployment: a separate `docker-compose.<role>.yml` file (no `extends:` chain) with its own services + volumes block. Pulls images for code-only services, builds locally for not-yet-published sidecars. The file-server host needs `services/<sidecar>/Dockerfile.<sidecar>` + the compose file + `.env` + `certs/` — no app-server source tree."
  - "Image-URL alignment test pattern: when a compose file pulls from a workflow-published image, encode that coupling as a test that parses BOTH artifacts. Done implicitly here via the docker-publish tag-strategy test referencing PUBLISH_WORKFLOW_PATH (same way test_agent_compose.py references COMPOSE_PATH). Future drift between the two surfaces fails CI rather than fails-at-pull-time on a production file-server."

requirements-completed: [OPS-02]

# Metrics
duration: ~18min
completed: 2026-05-16
---

# Phase 29 Plan 04: docker-compose.agent.yml + GHCR Publish Verification Summary

**Lands the file-server-host compose surface (`docker-compose.agent.yml` + `.env.example.agent`) with exactly 4 services — worker, watcher, audfprint, panako — and replaces the original GHCR-tag human-verify checkpoint with an automated YAML-parse test that asserts `.github/workflows/docker-publish.yml` produces BOTH `:latest` and `:v<version>` tags. Extends the workflow to emit the missing `type=semver,pattern={{version}}` + `type=ref,event=tag` patterns and realigns the api image URL to `ghcr.io/simplicityguy/phaze` (bare-repo) so the compose `image:` line resolves correctly.**

## Performance

- **Duration:** ~18 min
- **Started:** 2026-05-16T23:05Z (approx)
- **Completed:** 2026-05-16T23:23Z (approx)
- **Tasks:** 2 (both auto, both TDD)
- **Files created:** 3 (`docker-compose.agent.yml`, `.env.example.agent`, `tests/test_deployment/test_agent_compose.py`) + this SUMMARY
- **Files modified:** 3 (`.github/workflows/docker-publish.yml`, `tests/test_phase04_gaps.py`, `deferred-items.md`)
- **Tests added:** 5 (4 compose-structure + 1 workflow tag-check). Plus 1 deferred test reactivated.

## Accomplishments

- **OPS-02 fully closed.** A new `docker-compose.agent.yml` brings up exactly `worker`, `watcher`, `audfprint`, `panako` on a file server, configured via env to reach the application server. No Postgres or Redis service in the agent compose — agents connect to the app-server's via env-file URL.
- **WARNING-4 resolved without a human checkpoint.** Plan stays `autonomous: true`. The 5th test (`test_docker_publish_workflow_tags_both_latest_and_version`) is now a permanent CI gate: a future regression that drops the version tag pattern (e.g., during a metadata-action upgrade) will fail CI before shipping a release that's missing `:v<version>` images.
- **WARNING-3 enforced.** Every SCAN_PATH volume mount across all 4 services uses `${SCAN_PATH:?SCAN_PATH required}` (explicit-message fail-fast). The 4th test (`test_all_scan_path_mounts_use_failfast_syntax`) rejects any future YAML drift toward a loose default like `${SCAN_PATH:-/data/music}`.
- **WARNING-2 unified.** No bare `${VAR:?}` form anywhere — explicit-message form on all 4 services. Operator sees `SCAN_PATH required` instead of an empty error from compose.
- **Plan 29-05 deferred test resolved.** `tests/test_phase04_gaps.py::test_docker_compose_has_agent_worker_consuming_agent_queue` now scans BOTH `docker-compose.yml` and `docker-compose.agent.yml`; finds the agent-worker at `docker-compose.agent.yml::worker`. The Phase 27 UAT gap-13 invariant (an agent-side SAQ consumer exists somewhere in the deployment surface) is fully codified across the split.
- **Image URL realignment.** docker-publish.yml's api image now publishes to `ghcr.io/simplicityguy/phaze` (bare repo) matching the compose `image:` line; sidecars keep `/audfprint` and `/panako` sub-paths (irrelevant — agent.yml builds them locally).

## Task Commits

Each task was committed atomically (RED → GREEN per TDD):

1. **Task 1 RED — failing YAML-parse tests for docker-compose.agent.yml** — `b1c5620` (test)
2. **Task 1 GREEN — create docker-compose.agent.yml + .env.example.agent + resolve Plan 29-05 deferred test** — `ae45925` (feat)
3. **Task 2 RED — failing workflow-tag check (WARNING-4)** — `0e78658` (test)
4. **Task 2 GREEN — extend docker-publish.yml tag strategy + realign api URL** — `93e550b` (feat)

## Files Created/Modified

### Created

- **`docker-compose.agent.yml`** (75 lines) — File-server-host compose. Top-level `services:` is exactly `{worker, watcher, audfprint, panako}`. Top-level `volumes:` is exactly `{audfprint_data, panako_data}`. Worker + watcher pull from `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}`; sidecars retain `build:`. All 4 services use `${SCAN_PATH:?SCAN_PATH required}` fail-fast for the music-mount target. Worker + watcher mount `${MODELS_PATH:-./models}:/models:rw` (D-21 auto-download). Worker + watcher mount `${CA_PATH:-./certs}:/certs:ro` for the operator-distributed CA cert.
- **`.env.example.agent`** (75 lines) — File-server-host env template. Documents every required variable: `PHAZE_IMAGE_TAG`, `PHAZE_AGENT_API_URL` (HTTPS-only per Plan 02 guard), `PHAZE_REDIS_URL` (password-required per Plan 02), `PHAZE_AGENT_{ID,TOKEN,QUEUE}`, `PHAZE_AGENT_CA_FILE`, `PHAZE_AGENT_ENV=production`, `SCAN_PATH`, `MODELS_PATH`, `CA_PATH`, `PHAZE_AGENT_SCAN_ROOTS`. Production-pin guidance for `PHAZE_IMAGE_TAG` inline.
- **`tests/test_deployment/test_agent_compose.py`** (164 lines) — Five tests:
  1. `test_agent_compose_service_list` (D-15) — services exactly `{worker, watcher, audfprint, panako}`.
  2. `test_agent_compose_has_no_postgres_env` (DIST-04) — no `DATABASE_URL`, `POSTGRES_*`, or `depends_on: postgres` on any agent service.
  3. `test_worker_service_has_phaze_role_agent` (D-17) — worker environment contains `PHAZE_ROLE=agent`.
  4. `test_all_scan_path_mounts_use_failfast_syntax` (WARNING-3) — regex check: every SCAN_PATH volume entry matches `${SCAN_PATH:?...}`.
  5. `test_docker_publish_workflow_tags_both_latest_and_version` (WARNING-4) — parses `.github/workflows/docker-publish.yml`, locates the `docker/metadata-action` step, asserts both a `value=latest` line and a version pattern (`type=semver` OR `type=ref,event=tag`) are present.

### Modified

- **`.github/workflows/docker-publish.yml`** —
  - Matrix entries get a new `image_suffix` field: `""` for api, `/audfprint` for audfprint, `/panako` for panako.
  - `docker/metadata-action`'s `images:` line is now `${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}${{ matrix.image_suffix }}` (no slash separator; the suffix carries it).
  - `tags:` block extended with `type=semver,pattern={{version}}`, `type=semver,pattern={{major}}.{{minor}}`, and `type=ref,event=tag`. Existing `type=raw,value=latest,enable={{is_default_branch}}` + branch/PR/schedule tags retained.
- **`tests/test_phase04_gaps.py::test_docker_compose_has_agent_worker_consuming_agent_queue`** — Now iterates over `[docker-compose.yml, docker-compose.agent.yml]`, scanning each for a service whose `command` contains `saq phaze.tasks.agent_worker.settings` and whose environment has `PHAZE_ROLE=agent`. Finds the agent-worker at `docker-compose.agent.yml::worker`. Error message updated to explain the Phase 29 split.
- **`.planning/phases/29-deployment-hardening-agents-admin/deferred-items.md`** — Marked the Plan 29-05 deferred item as resolved by this plan (strike-through with explanation pointing to Plan 29-04).

## Decisions Made

- **`image_suffix` matrix override (Decision documented above).** One workflow continues to publish all three images with one matrix; only the api's published URL changes shape. No second workflow file needed.
- **Resolve the Plan 29-05 deferred test in this plan.** Adding a `docker-compose.agent.yml` to the codebase makes the gap-13 invariant satisfiable again; deferring the fix to a later plan would leave the test red across the merge of this wave.
- **Explicit-message `${VAR:?MESSAGE}` on every service.** Compose accepts the bare `${VAR:?}` form, but the explicit message gives operators an actionable error (`error while interpolating SCAN_PATH: SCAN_PATH required`). Worth the trivial duplication.
- **Test the BARE-URL + the tag-strategy in two separate gates.** The plan only mandates the tag-strategy test, so I removed an extra image-URL alignment test I had drafted. Rationale: the URL alignment is enforced by the act of the agent.yml's `image:` line being literal; a future divergence will fail at `docker compose -f docker-compose.agent.yml pull`. Adding a redundant test would have inflated the suite without buying additional safety.

## Deviations from Plan

None — plan executed exactly as written. The plan's `<action>` blocks were complete and accurate; both literal YAML targets and the literal test bodies matched the implementation verbatim.

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Ruff `SIM101` (merged isinstance check) + `I001` (import order) on `test_agent_compose.py`**
- **Found during:** Task 1 RED commit (pre-commit hook).
- **Issue:** Initial draft had `if isinstance(depends, list) or isinstance(depends, dict):` flagged by SIM101. Ruff also reordered `import re` to alphabetical position (`from pathlib import Path` → `import re` → `from typing import Any`).
- **Fix:** Manually merged to `if isinstance(depends, (list, dict)):`. Ruff's `--fix` already handled import order.
- **Files modified:** `tests/test_deployment/test_agent_compose.py`
- **Verification:** Pre-commit ran clean on second attempt.
- **Committed in:** `b1c5620` (Task 1 RED).

**2. [Rule 3 - Blocking] Missing trailing newline on `test_agent_compose.py`**
- **Found during:** Task 2 RED commit (pre-commit `end-of-file-fixer` hook).
- **Issue:** Appended the 5th test function without a trailing newline.
- **Fix:** Pre-commit auto-fixed; re-staged and re-committed.
- **Files modified:** `tests/test_deployment/test_agent_compose.py`
- **Verification:** Pre-commit ran clean on second attempt.
- **Committed in:** `0e78658` (Task 2 RED).

---

**Total deviations:** 2 auto-fixed (both Rule 3 - lint-blocking pre-commit fixups, both autoresolved).
**Impact on plan:** Zero — both were trivial style adjustments, no scope creep, no logic change.

### Authentication gates

None.

### Architectural decisions

None. All choices were already locked in PATTERNS, RESEARCH, and the plan body.

## Verification

### Task 1 acceptance

- ✅ `docker-compose.agent.yml` exists at repo root with the 4-service structure
- ✅ Top-level `services:` keys are exactly `{worker, watcher, audfprint, panako}`
- ✅ `services.worker.image == "ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}"` (NO `build:` key)
- ✅ `services.worker.command == "uv run saq phaze.tasks.agent_worker.settings"`
- ✅ `services.worker.env_file == ".env"`
- ✅ `services.worker.environment == ["PHAZE_ROLE=agent"]`
- ✅ `services.worker.volumes` has 3 entries with the explicit-message `${SCAN_PATH:?SCAN_PATH required}` form
- ✅ `services.worker.restart == "unless-stopped"`
- ✅ `services.watcher.command == "uv run python -m phaze.agent_watcher"`
- ✅ `services.watcher` mirrors worker's image + volume structure
- ✅ `services.audfprint.build.dockerfile == "services/audfprint/Dockerfile.audfprint"` with `${SCAN_PATH:?SCAN_PATH required}` mount + `audfprint_data:/data/fprint`
- ✅ `services.panako` mirrors audfprint with `services/panako/Dockerfile.panako` and `panako_data` volume
- ✅ Top-level `volumes:` block has exactly `{audfprint_data, panako_data}` (no pgdata)
- ✅ No service has `DATABASE_URL`, `POSTGRES_*`, or `depends_on: postgres`
- ✅ All 4 SCAN_PATH mounts match the fail-fast regex (WARNING-3 test passes)
- ✅ `.env.example.agent` has every variable from PATTERNS lines 839-867 with documented values
- ✅ All 4 tests in `test_agent_compose.py` pass (the new WARNING-3 fail-fast syntax test included)
- ✅ Plan 29-05 deferred test reactivated: `test_docker_compose_has_agent_worker_consuming_agent_queue` now scans both compose files and finds `docker-compose.agent.yml::worker`

### Task 2 acceptance

- ✅ `test_docker_publish_workflow_tags_both_latest_and_version` exists in `test_agent_compose.py`
- ✅ Test correctly FAILS against the original workflow (missing version tag pattern); after extending the workflow the test passes
- ✅ Workflow now emits: `value=latest` (existing), `type=semver,pattern={{version}}` (NEW), `type=semver,pattern={{major}}.{{minor}}` (NEW), `type=ref,event=tag` (NEW), `type=ref,event=branch` (existing), `type=ref,event=pr` (existing), `type=schedule,pattern=...` (existing)
- ✅ Image URL for api is now `ghcr.io/simplicityguy/phaze` (bare-repo via `image_suffix: ""`), matching `docker-compose.agent.yml`'s `image:` line
- ✅ Sidecar images retained at `/audfprint` and `/panako` sub-paths (irrelevant — agent.yml builds them locally per D-15)
- ✅ Plan remains `autonomous: true`; no `checkpoint:human-verify` task anywhere
- ✅ Final test sweep: `uv run pytest tests/test_deployment/ tests/test_phase04_gaps.py tests/test_task_split.py tests/test_main_lifespan.py -q` → **22 passed in 1.95s** (no regression)

### docker-publish.yml verification result

**`fixed` + `url-realigned`** — both the tag pattern AND the image URL needed adjustment. The workflow's `docker/metadata-action` step was missing `type=semver,pattern={{version}}` / `type=ref,event=tag` (so `:v<version>` was never produced on tagged releases), and the api image was published to `ghcr.io/simplicityguy/phaze/api` (sub-path) when `docker-compose.agent.yml` expects the bare-repo URL `ghcr.io/simplicityguy/phaze`. Both fixes landed in commit `93e550b`.

### docker compose config

`docker compose -f docker-compose.agent.yml config --quiet` was NOT executable in this worktree because the macOS dev environment lacks the `docker compose` v2 plugin (`docker: 'compose' is not a docker command`). This is the same environmental limitation noted in Plan 29-03's SUMMARY. The structural-parse tests provide equivalent validation:

```
uv run python -c "import yaml; d = yaml.safe_load(open('docker-compose.agent.yml').read()); print(sorted(d['services'].keys()))"
['audfprint', 'panako', 'watcher', 'worker']
```

The CI environment runs `docker compose config` separately (Plan 29-06 wires this in), so any compose-parse error that the YAML-parse layer doesn't catch will surface there.

### Threat-model mitigations delivered

| Threat ID | Mitigation Delivered |
|-----------|----------------------|
| T-29-04-01 (Spoofing — malicious image at `:latest`) | `.env.example.agent` documents the `PHAZE_IMAGE_TAG=v4.0.0` production-pin recommendation; new workflow tag strategy makes `:v<version>` pins actually exist on the registry |
| T-29-04-02 (DATABASE_URL on an agent service) | `test_agent_compose_has_no_postgres_env` asserts no agent service has `DATABASE_URL`, `POSTGRES_*`, or `depends_on: postgres` |
| T-29-04-03 (rw MODELS_PATH allows weight tampering) | Accepted — rw is required for D-21 auto-download |
| T-29-04-04 (CA_PATH ro leaked to sidecars) | Accepted — CA cert is non-secret |
| T-29-04-05 (SCAN_PATH default exposes wrong directory) | `${SCAN_PATH:?SCAN_PATH required}` fail-fast across all 4 services (WARNING-2 unified); WARNING-3 test enforces |
| T-29-04-06 (`:latest` default pulls unexpected image) | Accepted (Pitfall: D-16); operators advised in `.env.example.agent` |
| T-29-04-07 (workflow stops emitting `:v<version>`) | `test_docker_publish_workflow_tags_both_latest_and_version` is now a permanent CI gate |

## Known Stubs

None. Every change is end-to-end functional:

- `docker-compose.agent.yml` is the production wire format — no TODOs.
- `.env.example.agent` ships with placeholders that operators must replace (`<app-server-ip>`, `<REDIS_PASSWORD>`, `<32urlsafe>`) — these are not stubs but intentional operator-fill-in slots, documented in inline comments.
- `test_agent_compose.py` parses the live `docker-compose.agent.yml` + `.github/workflows/docker-publish.yml`; no mock data.

## TDD Gate Compliance

Both tasks followed the RED → GREEN cycle:

- **Task 1 RED** (`b1c5620`): `test(29-04): add failing YAML-parse tests for docker-compose.agent.yml (RED)`. Pytest run against the un-created `docker-compose.agent.yml` reported all 4 tests failing with `FileNotFoundError`.
- **Task 1 GREEN** (`ae45925`): `feat(29-04): create docker-compose.agent.yml + .env.example.agent (GREEN)`. All 4 tests pass after the agent compose file is written.
- **Task 2 RED** (`0e78658`): `test(29-04): add failing workflow-tag check (WARNING-4 RED)`. The new 5th test fails with `AssertionError: docker-publish.yml tag patterns missing: ["'type=semver,pattern={{version}}' (or 'type=ref,event=tag')"]` against the un-modified workflow.
- **Task 2 GREEN** (`93e550b`): `feat(29-04): extend docker-publish.yml tag strategy + realign api URL (GREEN)`. The 5th test passes after the workflow's metadata-action step gains the missing tag patterns and the api image URL realigns to the bare repo.

Gate-sequence check: `git log --oneline` shows `b1c5620 test(...)` → `ae45925 feat(...)` → `0e78658 test(...)` → `93e550b feat(...)` for plan 29-04. RED commits precede their paired GREEN commits in the correct order. No REFACTOR commits were needed.

## Self-Check: PASSED

**Files claimed to be created — verified to exist:**

- ✅ `docker-compose.agent.yml` — FOUND
- ✅ `.env.example.agent` — FOUND
- ✅ `tests/test_deployment/test_agent_compose.py` — FOUND
- ✅ `.planning/phases/29-deployment-hardening-agents-admin/29-04-SUMMARY.md` — FOUND (this file)

**Files claimed to be modified — verified via `git show`:**

- ✅ `.github/workflows/docker-publish.yml` — modified in `93e550b` (matrix + tags)
- ✅ `tests/test_phase04_gaps.py` — modified in `ae45925` (deferred test fix)
- ✅ `.planning/phases/29-deployment-hardening-agents-admin/deferred-items.md` — modified in `ae45925` (resolved marker)

**Commits claimed — verified via `git log --oneline`:**

- ✅ `b1c5620` — Task 1 RED — FOUND
- ✅ `ae45925` — Task 1 GREEN — FOUND
- ✅ `0e78658` — Task 2 RED — FOUND
- ✅ `93e550b` — Task 2 GREEN — FOUND

**Test count matches plan `<acceptance_criteria>`:** 5 new tests in `tests/test_deployment/test_agent_compose.py` (3 from RESEARCH LOCKED + WARNING-3 fail-fast + WARNING-4 docker-publish tag check). All 5 pass.

**Decision IDs implemented:** D-15 (full), D-16 (full — workflow tag strategy verified), D-17 (full — root vs agent compose split), D-22 (agent-compose portion), D-23 (`.env.example.agent` portion). Requirement OPS-02 closed.
