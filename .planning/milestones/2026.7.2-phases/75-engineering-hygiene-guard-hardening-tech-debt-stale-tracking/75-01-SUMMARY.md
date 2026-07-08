---
phase: 75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking
plan: 01
subsystem: planning-tracking + deployment-config
tags: [hygiene, reconciliation, docs-drift, docker-compose, tech-debt]
requires:
  - "PR #207 (ec80a53a) — the guard-hardening change HYG-01 records as already-satisfied"
  - "Phase 72 D-03 — the N-compute enablement that supersedes HYG-03"
provides:
  - "HYG-01 disposition = already-satisfied (PR #207 ec80a53a) in REQUIREMENTS.md + ROADMAP.md"
  - "HYG-03 disposition = SUPERSEDED (Phase 72 D-03) in REQUIREMENTS.md + ROADMAP.md + STATE.md"
  - "docker-compose.yml cloud_target/Phase-67 breadcrumb comments removed; backends.toml explainer kept"
  - "STATE.md 2026.7.1 deferred table: HYG-02 resolved / HYG-03 superseded / HYG-04 resolved-via-75-02; 70-UAT untouched"
  - "STATE.md 2026.7.0 deferred table: 63-UAT + 260628-wzq + 260629-eev reconciled complete"
affects:
  - ".planning/REQUIREMENTS.md"
  - ".planning/ROADMAP.md"
  - ".planning/STATE.md"
  - "docker-compose.yml"
tech-stack:
  added: []
  patterns:
    - "Requirement/tracking reconciliation at milestone close — mark satisfied/superseded with a cited PR/phase reason"
    - "Keep Traceability checkboxes/Status Pending for not-yet-passed phases; carry disposition in prose to keep docs-drift green"
key-files:
  created:
    - ".planning/phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-01-SUMMARY.md"
  modified:
    - ".planning/REQUIREMENTS.md"
    - ".planning/ROADMAP.md"
    - ".planning/STATE.md"
    - "docker-compose.yml"
decisions:
  - "HYG-01 recorded already-satisfied by PR #207 (ec80a53a) — no new code/test (D-01/D-02)"
  - "HYG-03 recorded SUPERSEDED by Phase 72 D-03 — no code change; re-adding the >1-compute reject would break Phases 72-74 (D-05/D-06/D-07)"
  - "HYG-02 comment-only deletion; there was never a live PHAZE_CLOUD_TARGET env key (D-03/D-04)"
  - "Traceability rows + HYG checkboxes left Pending; docs-drift guard forbids flipping active-phase reqs before Phase 75 is a passed phase — dispositions carried in prose (plan Task 1 guidance)"
  - "WR-01 probe-concurrency gap NOT fixed (user decision D-08); kept as a tracked deferred item in STATE.md"
metrics:
  duration: ~12 min
  tasks: 3
  files_changed: 4
  completed: 2026-07-06
---

# Phase 75 Plan 01: HYG Reconciliation + Docker-Compose Comment Cleanup Summary

Reconciled the four no-code hygiene items closing milestone 2026.7.2: recorded HYG-01 as already-satisfied by PR #207, HYG-03 as superseded by Phase 72 D-03, deleted two stale `cloud_target`/Phase-67 breadcrumb comments from docker-compose.yml (HYG-02), reconciled the stale 2026.7.0 tracking rows (HYG-05), and cleared all three still-`open` 2026.7.1 STATE deferred rows — with zero `src/` diff and the docs-drift guard green throughout.

## What Was Built

- **Task 1 (`60300b7d`)** — REQUIREMENTS.md / ROADMAP.md / STATE.md:
  - HYG-01 description + ROADMAP success-criterion #1 + backlog entry annotated **already-satisfied by PR #207 (`ec80a53a`)**; the `_NO_ACTIVE_MILESTONE` skipif + `test_archived_milestones_internally_consistent` already keep the `git rm REQUIREMENTS.md` close path green. No new code/test.
  - HYG-03 description + ROADMAP success-criterion #3 + Notes annotated **SUPERSEDED by Phase 72 (D-03)** — the `>1`-compute fail-fast was deleted to ship N-compute (MCOMP-01); the correct boot guard (`_validate_registry` rejects a duplicate `agent_ref`) already exists.
  - Phase-75 ROADMAP bullet (L25) reworded from the stale "promote to boot-time / harden the guard" premises to the reconciliation dispositions.
  - STATE.md 2026.7.1 deferred table: **HYG-02** `open`→resolved (Phase 75), **HYG-03** `open`→superseded (Phase 72 D-03), **HYG-04** `open`→resolved (via plan 75-02's regression test). The **70-UAT** deployment-gated row was left untouched. The genuinely-open **WR-01** probe-concurrency gap (user decision D-08, not fixed here) was added as a tracked deferred note.
- **Task 2 (`825dce7b`)** — docker-compose.yml: deleted the two "Replaces the removed cloud_target selector … (Phase 67 …)" breadcrumb sentences from the `api` and `worker` services; kept the `backends.toml` mount explainer and the "Mount a backends.toml to enable cloud backends." operator guidance. Comment-only diff; YAML still valid.
- **Task 3 (`4697bb89`)** — STATE.md 2026.7.0 deferred table: `63-UAT` `partial`→complete, `260628-wzq` (`5f43aa7`) and `260629-eev` (`267109b`) `missing`→complete. Quick-task SUMMARY files left untouched (status reconciliation only).

## Verification

- `just docs-drift` (`uv run pytest tests/shared/core/test_requirements_traceability.py -q`) → **10 passed** after every task.
- `git diff --stat 707fd0b7..HEAD -- src/` → **empty** (zero source change, as required).
- `git grep -nE "cloud_target|Phase 67" -- docker-compose.yml` → **CLEAN**.
- `git grep "PHAZE_BACKENDS_CONFIG_FILE" -- docker-compose.yml` → explainer preserved (api + worker).
- `git diff -- docker-compose.yml` → comment-only (no `image:`/`environment:`/`volumes:`/`command:` change).
- Quick-task SUMMARY files unchanged (`git diff --stat -- .planning/quick/` empty).

## Deviations from Plan

None — plan executed exactly as written. Per the plan's Task 1 HARD CONSTRAINT, HYG Traceability checkboxes and Status columns were deliberately left `Pending` (flipping an active-phase requirement to `[x]`/Complete before Phase 75 is a passed phase would trip the docs-drift guard's "marked Complete but mapped to no passed phase" check); the satisfied/superseded dispositions are carried in the requirement description prose + a Coverage/reconciliation note, exactly as the plan instructed.

## Known Stubs

None. This is a docs/tracking + inert-comment plan with no runtime surface.

## Threat Flags

None. No new endpoint, input, parser, auth, session, or crypto path; no `src/` behavior change (threat register T-75-01-NA / T-75-SC both `accept`).

## Self-Check: PASSED

- `.planning/phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-01-SUMMARY.md` — FOUND
- Commit `60300b7d` — FOUND
- Commit `825dce7b` — FOUND
- Commit `4697bb89` — FOUND
