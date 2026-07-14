---
phase: 92-milestone-close-tech-debt-cleanup
plan: 01
subsystem: source-hygiene
tags: [comment-only, doc-hygiene, cleanup, CLEAN-03]
requires: []
provides:
  - "De-duplicated MKUE-01/D-04 KubeConfig comment in backends.py (one copy)"
  - "Corrected ON-CONFLICT comment in agent_files.py reflecting post-Phase-90 schema"
affects:
  - src/phaze/services/backends.py
  - src/phaze/routers/agent_files.py
tech-stack:
  added: []
  patterns: []
key-files:
  created:
    - .planning/phases/92-milestone-close-tech-debt-cleanup/92-01-SUMMARY.md
  modified:
    - src/phaze/services/backends.py
    - src/phaze/routers/agent_files.py
decisions: []
metrics:
  duration: ~4m
  completed: 2026-07-13
  tasks: 1
  files_changed: 2
  commits: 1
---

# Phase 92 Plan 01: CLEAN-03 Comment Hygiene Summary

Two behavior-preserving, comment-only fixes surfaced by the 2026.7.5 milestone audit: removed the byte-identical duplicated KubeConfig comment in `backends.py` (D-09) and reworded the stale ON-CONFLICT comment in `agent_files.py` that still described the Phase-90-dropped `files.state`/DISCOVERED-stamp semantics (D-10). Zero runtime change.

## What Was Built

### Task 1: D-09 dedupe + D-10 stale-comment correction (commit b2062c94)

**D-09 — `src/phaze/services/backends.py`** (inside `KueueBackend.reconcile`, ~L563):
The `MKUE-01/D-04: thread THIS backend's KubeConfig ...` comment appeared twice, byte-identical. Dropped the second copy, keeping a single copy immediately above the `await _reconcile_one(...)` call. No code line changed.

**D-10 — `src/phaze/routers/agent_files.py`** (inside the `on_conflict_do_update(... set_={...})` block, ~L131):
The prior comment described the removed `files.state` column ("NEVER overwrite `state` on conflict", "regress its pipeline progress to DISCOVERED", "New-file INSERT still stamps state=DISCOVERED via ... data['state']") — semantics deleted in Phase 90. Read the current `set_` dict (updates `sha256_hash`/`file_size`/`batch_id`/`file_type`; no `state` key) and reworded the comment to accurately describe today's behavior: an agent rescan refreshes only content facts (hash/size/batch/file_type) while identity columns (the `(agent_id, original_path)` conflict target and the server-generated `id`) are never touched, so a rescan can never re-key or duplicate a known file. Kept the still-accurate AUTH-01 note (`agent_id` stamped from the auth dep, never body). The `set_` dict was not changed.

## Verification

- `grep -c "thread THIS backend's KubeConfig" src/phaze/services/backends.py` → `1` (was 2).
- `grep "regress its pipeline progress to DISCOVERED" src/phaze/routers/agent_files.py` → no match; no residual `files.state` / `data["state"]` / `state=DISCOVERED` reference in that block.
- `git diff` on both files → comment lines only; no changes to `set_=` keys or any logic line.
- `uv run ruff check` and `uv run ruff format --check` on both files → exit 0.
- `uv run pytest tests/shared/test_partition_guard.py` → 3 passed (anti-drift guards green after the edits).
- Full pre-commit hook suite (ruff, ruff-format, bandit, mypy, EOF/whitespace) → all passed at commit time.
- Post-commit deletion check → zero deleted files; only the two intended source files changed.

## Deviations from Plan

None - plan executed exactly as written.

## Known Stubs

None.

## Notes for Orchestrator

- `.planning/STATE.md` was already modified in the worktree working tree when this agent was spawned (the orchestrator's "execution started" update). Per parallel-executor instructions it was NOT staged or committed — it remains as an unstaged working-tree change for the orchestrator to own.
- This Orca-managed worktree is on branch `SimplicityGuy/phase-92` (the phase branch, `branching_strategy=none`), not the Claude Code `worktree-agent-*` namespace. Commit safety was enforced via the protected-ref deny-list (branch is non-protected, HEAD not detached).

## Self-Check: PASSED

- FOUND: src/phaze/services/backends.py (1 KubeConfig comment copy)
- FOUND: src/phaze/routers/agent_files.py (reworded ON-CONFLICT comment)
- FOUND: commit b2062c94 (docs(92-01) dedupe D-09 + correct D-10)
- FOUND: .planning/phases/92-milestone-close-tech-debt-cleanup/92-01-SUMMARY.md
