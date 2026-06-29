---
phase: 56-deployment-runbook-config-docs
plan: 03
subsystem: admin-ui / agent-liveness
tags: [d-07, ephemeral-identity, agents-ui, dead-suppression, import-boundary, k8s-burst]
requires:
  - phaze.services.agent_liveness.classify (never-before-dead precedence, Phase 29 D-12)
  - phaze.job_runner one-shot Kueue entrypoint (Phase 52)
  - phaze.tasks.heartbeat._heartbeat_loop / agent_worker (Phase 46)
provides:
  - static ephemeral k8s-lane note on the Agents page (D-07)
  - never-not-dead executable invariant (classify != 'dead' when last_seen_at is None)
  - no-heartbeat-in-job_runner import-boundary invariant
affects:
  - src/phaze/templates/admin/agents.html
  - tests/test_services/test_agent_liveness.py
  - tests/test_task_split.py
tech-stack:
  added: []
  patterns:
    - neutral-panel idiom reuse (border … rounded-lg p-4, text-sm text-gray-*)
    - subprocess import-boundary introspection (D-25 style)
    - parametrized time-deterministic classify invariant (no freezegun)
key-files:
  created: []
  modified:
    - src/phaze/templates/admin/agents.html
    - tests/test_services/test_agent_liveness.py
    - tests/test_task_split.py
decisions:
  - "56-03: the DEAD-suppression is a verification target, not a build — the invariant holds by construction (classify 'never' branch precedes the dead-threshold math; the one-shot pod has no heartbeat loop), so the work is two passing regression guards + a static operator note"
  - "56-03: no kind=compute filter to hide the k8s row (RESEARCH Pitfall 4) — that would also hide the v5.0 A1 compute agent which DOES heartbeat and SHOULD show; accept the 'never' pill + the static note"
metrics:
  duration: ~10 min
  completed: 2026-06-29
requirements: [KDEPLOY-04]
---

# Phase 56 Plan 03: Ephemeral k8s-lane Identity (D-07) Summary

Static Agents-page note explaining the k8s burst lane is ephemeral/Job-based plus two passing regression guards proving the non-heartbeating bearer-token Agent row classifies 'never' and can never render a perpetually-DEAD pill.

## What Was Built

**Task 1 — static ephemeral k8s-lane note (`admin/agents.html`).** Added a neutral (gray, deliberately not amber/red) static `<section>` info panel after the intro `<p>` and before the `agents_table` include, carrying the locked 56-UI-SPEC §Copywriting body string `The Kubernetes burst lane runs as ephemeral, per-file Jobs — it does not register as a heartbeating agent here. Its live activity is visible as in-flight Kueue workloads on the pipeline dashboard.` It uses the neutral-panel idiom (`border border-gray-200 dark:border-phaze-border rounded-lg p-4`, body `text-sm text-gray-600 dark:text-gray-400`), an optional inline `K8s burst lane` heading, and a decorative `ℹ` glyph marked `aria-hidden="true"`. Fully static — no `hx-trigger`, no Alpine, no poll; it does not participate in the table's 5s self-refresh. All strings are static through Jinja autoescape (T-56-XSS).

**Task 2 — DEAD-suppression invariants (two tests).**
- `tests/test_services/test_agent_liveness.py::test_classify_never_not_dead_when_last_seen_at_none` — parametrized over six elapsed deltas (0s up to a century in the future) asserting `classify(agent, now) != "dead"` and `== "never"` whenever `last_seen_at is None`. The 'never' branch precedes the threshold math (`agent_liveness.py:79-80`), so no elapsed time can promote a no-signal agent to 'dead'.
- `tests/test_task_split.py::test_job_runner_does_not_run_heartbeat_loop` — subprocess import-boundary guard (D-25 style) asserting `phaze.job_runner` never imports `phaze.tasks.heartbeat` / `phaze.tasks.agent_worker`, carries no `_heartbeat_loop` name in its namespace, and has no `_heartbeat_loop`/`heartbeat` reference in its source. This is the structural proof the k8s burst lane stays at `last_seen_at IS NULL`.

Both invariants were already true; the work locks them as regression guards for D-07. The full synthetic "k8s burst" card with workload-derived liveness is explicitly deferred to v7.0 RECORD-03.

## Tasks Completed

| Task | Name | Commit | Files |
| ---- | ---- | ------ | ----- |
| 1 | static ephemeral k8s-lane note on the Agents page | 858daae | src/phaze/templates/admin/agents.html |
| 2 | prove the DEAD-suppression invariant (never != dead; job_runner has no heartbeat) | 7708557 | tests/test_services/test_agent_liveness.py, tests/test_task_split.py |

## Verification

- `uv run pytest tests/test_services/test_agent_liveness.py tests/test_task_split.py -k "never or heartbeat" -x` → 10 passed.
- `uv run pytest tests/test_services/test_agent_liveness.py tests/test_task_split.py` → 39 passed.
- `grep -q "runs as ephemeral, per-file Jobs" src/phaze/templates/admin/agents.html` → match.
- pre-commit hooks (ruff, ruff-format, bandit, mypy) ran on the test commit → all Passed.

## Deviations from Plan

None — plan executed exactly as written. No architectural changes, no auto-fixes, no authentication gates.

## Threat Surface

- T-56-DEAD (mitigate) — covered by the never-not-dead + no-heartbeat invariants (Task 2).
- T-56-XSS (mitigate) — note copy is static through Jinja autoescape; no operator string interpolated (Task 1).
- T-56-FILTER (accept) — no `kind=compute` filter added (would hide the v5.0 A1 agent); accept the 'never' pill + note.
- T-56-SC (accept) — zero new external packages.

No new security surface introduced beyond the threat model.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/phaze/templates/admin/agents.html (contains locked copy)
- FOUND: tests/test_services/test_agent_liveness.py (never-not-dead invariant)
- FOUND: tests/test_task_split.py (no-heartbeat invariant)
- FOUND commit: 858daae
- FOUND commit: 7708557
