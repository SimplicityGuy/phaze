---
phase: 45-scheduling-ledger-for-orphan-recovery
plan: 03
subsystem: recovery
tags: [saq, recovery, scheduling-ledger, control-only, idempotency, dedup, postgres]

# Dependency graph
requires:
  - phase: 45-01
    provides: "get_ledger_rows + get_live_job_keys + SchedulingLedger(.key/.function/.routing/.payload)"
  - phase: 35-deterministic-keys
    provides: "_KEY_BUILDERS + apply_deterministic_key before_enqueue chokepoint (re-stamps row.key on replay)"
  - phase: 42-stage-recovery
    provides: "recover_orphaned_work gate (count_inflight_jobs) + per-agent routing + NoActiveAgentError skip"
provides:
  - "recover_orphaned_work rewritten to drive off the durable scheduling ledger (ledger MINUS live MINUS domain-completed)"
  - "explicit, total per-stage domain-completed classifier (_DOMAIN_COMPLETED_STAGES) + is_domain_completed predicate"
  - "the complement-of-done SWEEP queries removed from recovery (kept only for the manual DAG triggers in pipeline.py)"
affects: [45-04-startup-backfill]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "replay each orphaned ledger row via queue.enqueue(row.function, key=row.key, **row.payload) -- key re-stamped from the ledger key so dedup works in prod AND the fake; never a raw random-key enqueue"
    - "per-stage domain-completed predicate is TOTAL: predicate-covered XOR live-keys-only, sourced from _KEY_BUILDERS and asserted by a parametrized test"
    - "pending-set queries reused for MEMBERSHIP (absent-from-pending == done), NOT for their complement-of-done sweep"

key-files:
  created: []
  modified:
    - src/phaze/tasks/reenqueue.py
    - tests/test_tasks/test_recovery.py

key-decisions:
  - "Replay passes key=row.key explicitly. The ledger key IS the deterministic <function>:<natural_id> the before_enqueue hook stamps, so passing it is a no-op in production (the hook re-stamps the identical value) AND lets the DedupFakeQueue model SAQ dedup without running the hook. Avoids re-deriving a FileRecord, so extra='forbid' agent schemas validate the stored payload verbatim."
  - "stages dict is keyed per keyed FUNCTION (process_file/extract_file_metadata/...), all eight initialized to zero, so the return shape is total. The only consumer (controller.py startup log) reads detected_loss + stages generically; the manual button is fire-and-forget (no shape dependency)."
  - "get_metadata_pending_files / get_fingerprint_pending_files stay imported in recovery, but ONLY to compute the done-set predicate (absent-from-pending == done) -- this is NOT the complement-of-done sweep that caused the incident. Documented in _build_done_sets."

patterns-established:
  - "_DOMAIN_COMPLETED_STAGES = {process_file, extract_file_metadata, fingerprint_file}; every other keyed function is live-keys-only (its ledger clear is reliable on every terminal outcome -- scan via Plan 02 ack, controllers via Plan 01 after_process)."

requirements-completed: [L-03, L-05]

# Metrics
duration: ~50min
completed: 2026-06-19
---

# Phase 45 Plan 03: Ledger-Driven Orphan Recovery Summary

**`recover_orphaned_work` now re-enqueues exactly `ledger MINUS live-saq_jobs-keys MINUS domain-completed`, replaying each orphaned row's STORED payload through the SAME keyed producer it was originally enqueued by — so a never-scheduled DISCOVERED file (no ledger row) can never be swept again.**

## Performance

- **Duration:** ~50 min
- **Completed:** 2026-06-19
- **Tasks:** 1/1 (TDD: RED -> GREEN -> REFACTOR -> coverage)
- **Files modified:** 2 (0 created, 2 modified)

## Accomplishments

### Task 1 — Rewrite recover_orphaned_work to replay ledger MINUS live MINUS the per-stage completed predicate

- **RED (406e61f):** Rewrote `tests/test_tasks/test_recovery.py` to seed `scheduling_ledger` rows (via Plan-01 `upsert_ledger_entry`) instead of pending sets. Added the incident regression (11 DISCOVERED + 0 ledger rows -> 0 reenqueued), live-key exclusion, the three domain-completed exclusions (analyze / metadata / fingerprint) + their pending-replays, the scan-row-is-live-keys-only case, the predicate-totality parametrized assertion, the dedup-skip backstop, force-bypasses-gate, and the no-active-agent split (agent rows skip, controller rows replay).
- **GREEN (b53586e):** Rewrote `recover_orphaned_work`:
  - Keeps the `count_inflight_jobs == 0` no-op DETECT gate verbatim (`force=True` bypasses ONLY it).
  - `rows = get_ledger_rows(session)`, `live = get_live_job_keys(session)`, done sets built ONCE (`_build_done_sets`).
  - `orphaned = [r for r in rows if r.key not in live and not is_domain_completed(r, done_sets)]`.
  - Partitions by `r.routing`: controller rows replay on `ctx["queue"]`; agent rows on the active agent's per-agent queue (`select_active_agent` + `task_router.queue_for`); `NoActiveAgentError` skips agent rows with a WARNING while controller rows still replay.
  - `_replay_row` enqueues `row.function` with `key=row.key, **row.payload`; a `None` return (dedup) counts skipped, else reenqueued.
  - Removed the complement-of-done SWEEP queries (`get_files_by_state`/`get_untracked_files`/`get_proposal_pending_batches`/`get_scrape_pending_tracklists`/`get_match_pending_tracklists`) and the `_reconcile_*` helpers from recovery. Control-only banner preserved + extended with the Phase-45 ledger reframe.
- **REFACTOR (050e0b9):** Replaced the `_ABSENT` sentinel + placeholder dict entries with three explicit named key constants (`_ANALYZE_DONE` / `_METADATA_PENDING` / `_FINGERPRINT_PENDING`) so `_build_done_sets` and `is_domain_completed` cannot drift on stringly-typed keys.
- **Coverage (d1a5bee):** Added a unit test for the defensive `is_domain_completed` no-`file_id` branch -> `reenqueue.py` at 100% line coverage.

### Per-stage domain-completed classification (total, asserted)

| Stage (function) | Routing | Domain predicate | Why |
|---|---|---|---|
| process_file | agent | state in {ANALYZED, ANALYSIS_FAILED} | belt-and-suspenders (analyze has a /failed callback too) |
| extract_file_metadata | agent | NOT in get_metadata_pending_files | PRIMARY net — no /failed callback (Plan 02 residual gap) |
| fingerprint_file | agent | NOT in get_fingerprint_pending_files | PRIMARY net — no /failed callback |
| scan_live_set | agent | none (live-keys-only) | Plan 02 ack clears the row on every outcome |
| generate_proposals / search_tracklist / scrape_and_store_tracklist / match_tracklist_to_discogs | controller | none (live-keys-only) | Plan 01 after_process clears on every terminal status |

A parametrized test asserts every `_KEY_BUILDERS` function is predicate-covered XOR live-keys-only (no stage silently undefined — T-45-17).

## Deviations from Plan

None — the plan was executed exactly as written. The only judgment calls (key=row.key on replay; per-function stages dict; pending-query-for-membership) were explicitly sanctioned by the plan's `<interfaces>`/`<action>` and are recorded under key-decisions above, not as deviations.

## Threat Mitigations Applied

- **T-45-08 (queue detonation):** recovery reads ONLY ledger rows; the incident regression (11 DISCOVERED + 0 ledger rows -> 0 reenqueued) is a green verify gate.
- **T-45-09 (queue doubling):** primary exclusion `get_live_job_keys`; backstop the deterministic-key dedup (`None` -> skipped), tested by `test_dedup_skip_backstop_for_a_slipped_live_item`.
- **T-45-10 (tampering):** replay goes through the keyed producers; `extra='forbid'` agent schemas re-validate the stored payload on dequeue (a malformed row dead-letters rather than executing).
- **T-45-11 (boundary):** control-only banner preserved; `tests/test_task_split.py` green (reenqueue.py is never imported by the agent — its top-level `deterministic_key` import is one-directional and safe).
- **T-45-17 (recovery loop):** per-stage predicate is TOTAL (covered XOR live-keys-only), asserted.
- **T-45-SC:** no new packages this plan.

## Verification

- `uv run pytest tests/test_tasks/test_recovery.py tests/test_task_split.py -q` -> 31 passed (incident regression + import-boundary green).
- `uv run pytest tests/ -q` (full suite, ephemeral :5433 DB) -> **1935 passed**, 0 failures.
- `uv run mypy src/phaze/tasks/reenqueue.py` -> clean. `uv run ruff check .` -> clean (enforced by pre-commit on every commit; no `--no-verify`).
- Coverage: `reenqueue.py` 100.00%; suite TOTAL 97.57% (>= 85% gate).
- Acceptance greps: `get_ledger_rows` + `get_live_job_keys` + `_DOMAIN_COMPLETED_STAGES`/`is_domain_completed` all present in recovery; the five complement-of-done SWEEP query names appear ONLY in docstrings documenting their removal (no import, no call).
- The live-keys integration case (real `saq_jobs`) runs under `just integration-test` (broker-gated `@pytest.mark.integration` test retained, skips when Postgres is unavailable).

## Known Stubs

None — recovery is fully wired off the ledger. The agent-stage ledger CLEAR it depends on lands in Plan 02; on a worker without that clear, an orphaned row simply replays (the safe default) and dedups to a no-op if still live.

## Notes for Downstream Plans

- **Plan 04 (startup backfill):** seeds the ledger from live `saq_jobs` via `insert_ledger_if_absent` so the current in-flight cohort is recoverable on first boot. Recovery reads whatever the ledger holds — backfill widens the recoverable set without any recovery change.

## Self-Check: PASSED

- `src/phaze/tasks/reenqueue.py` exists; `tests/test_tasks/test_recovery.py` exists.
- All four task commits present in the worktree branch history: 406e61f (test/RED), b53586e (feat/GREEN), 050e0b9 (refactor), d1a5bee (test/coverage).
