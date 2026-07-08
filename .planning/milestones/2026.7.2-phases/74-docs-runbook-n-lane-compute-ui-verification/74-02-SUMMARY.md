---
phase: 74-docs-runbook-n-lane-compute-ui-verification
plan: 02
subsystem: deployment
tags: [compose, cloud-agent, arm64, x86, guard-test, MCOMP-07]
requires:
  - docker-compose.cloud-agent.yml (Phase 51 cloud compute-agent compose)
provides:
  - "Single cloud-agent compose serving both arm64 A1 (default) and x86 spill (override) compute agents"
  - "PHAZE_CLOUD_AGENT_IMAGE / PHAZE_CLOUD_AGENT_CMD operator override vars"
affects:
  - docker-compose.cloud-agent.yml
  - tests/agents/deployment/test_cloud_agent_compose.py
tech-stack:
  added: []
  patterns:
    - "docker-compose ${VAR:-default} substitution with nested default preserving arm64 image/command"
    - "raw-YAML guard test asserts the un-interpolated DEFAULT still renders arm64"
key-files:
  created: []
  modified:
    - docker-compose.cloud-agent.yml
    - tests/agents/deployment/test_cloud_agent_compose.py
decisions:
  - "Used PHAZE_CLOUD_AGENT_IMAGE (full image) not PHAZE_CLOUD_AGENT_TAG (tag-only) per PLAN spec and must_haves — the x86 operator sets the complete standard tag with no -arm64 suffix."
  - "Guard test strips the ${PHAZE_CLOUD_AGENT_CMD:-…} wrapper to inspect the DEFAULT tokens rather than tokens[:3] on the raw string, so uv-forbidden + python3 -m saq intent is preserved on the default."
metrics:
  duration_minutes: 6
  completed: 2026-07-05
  tasks: 2
  files_modified: 2
---

# Phase 74 Plan 02: Parametrize cloud-agent compose for N-lane compute (arm64 default + x86 override) Summary

One compose file (`docker-compose.cloud-agent.yml`) now serves both the arm64 OCI A1 agent (unchanged default) and a real x86 spill compute agent (via `PHAZE_CLOUD_AGENT_IMAGE` / `PHAZE_CLOUD_AGENT_CMD` overrides), with the guard test relaxed to the `${VAR:-default}` form so it still proves the arm64 default renders. No new compose file (MCOMP-07, D-05, R-1).

## What Was Built

- **Task 1 — Parametrized compose (`8af0502d`):** The two arm64-hardcoded lines are now `${VAR:-default}` forms:
  - `image: ${PHAZE_CLOUD_AGENT_IMAGE:-ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64}` — default preserves the `ghcr.io/simplicityguy/phaze:` prefix, the `PHAZE_IMAGE_TAG` pin, and the `-arm64` suffix.
  - `command: ${PHAZE_CLOUD_AGENT_CMD:-python3 -m saq phaze.tasks.agent_worker.settings}` — default preserves the system-python launcher.
  - Block header comment extended to document both override vars (x86 sets standard tag with NO `-arm64`; x86 sets `uv run saq …` because that image is 3.14 + .venv).
  - `PHAZE_AGENT_KIND=compute`, `network_mode: host`, volumes, and `*_FILE` secret machinery all untouched.
- **Task 2 — Relaxed guard test (`f2c59d75`):**
  - `test_worker_image_is_arm64_ghcr_pinned`: `startswith("ghcr.io/simplicityguy/phaze:")` → substring check; `endswith("-arm64")` → `"-arm64}" in image` DEFAULT marker. `PHAZE_IMAGE_TAG in image` pin check kept as-is.
  - `test_worker_command_invokes_system_python_not_uv`: strips the `${PHAZE_CLOUD_AGENT_CMD:-…}` wrapper, then asserts the DEFAULT is `python3 -m saq phaze.tasks.agent_worker.settings` and `uv` is not the default launcher. The `command is None` early-return branch retained. Other assertions (scratch volume, env, secrets, networking) untouched.

## Verification

- Task 1 automated check (raw-YAML invariants): OK.
- `uv run pytest tests/agents/deployment/test_cloud_agent_compose.py -x -q` → 9 passed.
- `uv run ruff check tests/agents/deployment/test_cloud_agent_compose.py` → clean.
- `uv run pytest tests/agents/deployment/` → 33 passed (no collateral breakage).
- `docker compose config` validation (Assumption A2) skipped: the docker CLI on this host lacks the `compose` plugin and `docker-compose` v1 is absent. Relied on the raw-YAML guard test per the plan's fallback. The nested `${PHAZE_CLOUD_AGENT_IMAGE:-…${PHAZE_IMAGE_TAG:-latest}…}` default is a standard docker-compose nested-substitution form.

## Deviations from Plan

None — plan executed exactly as written. Both override var names (`PHAZE_CLOUD_AGENT_IMAGE`, `PHAZE_CLOUD_AGENT_CMD`) match the PLAN's explicit spec and must_haves (the 74-RESEARCH excerpt showed an alternative `PHAZE_CLOUD_AGENT_TAG` spelling, but the PLAN and its verify checks mandate `PHAZE_CLOUD_AGENT_IMAGE` — followed the PLAN).

## Threat Surface

Per the plan's threat register: T-74-03 (mitigate) is enforced — the guard test asserts the DEFAULT still renders the arm64 image + system-python command, so a mis-parametrization that silently drops the arm64 pin fails CI. T-74-04 (accept) — no secret value added, no new port/credential; existing `*_FILE` machinery unchanged. No new threat surface introduced.

## Known Stubs

None.

## Self-Check: PASSED

- Files verified present: `docker-compose.cloud-agent.yml`, `tests/agents/deployment/test_cloud_agent_compose.py`, `74-02-SUMMARY.md`.
- Commits verified in git log: `8af0502d` (Task 1), `f2c59d75` (Task 2).
