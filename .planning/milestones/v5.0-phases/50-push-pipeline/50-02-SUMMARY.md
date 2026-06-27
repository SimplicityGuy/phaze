---
phase: 50-push-pipeline
plan: 02
subsystem: infra
tags: [saq, recovery, deterministic-key, enqueue-router, scheduling-ledger, cloud-burst]

# Dependency graph
requires:
  - phase: 50-01
    provides: FileState.PUSHING / FileState.PUSHED enum members
  - phase: 49
    provides: kind-filtered select_active_agent(kind="compute"|"fileserver") + AWAITING_CLOUD held-row partition
  - phase: 45
    provides: scheduling ledger + ledger-driven recover_orphaned_work + _DOMAIN_COMPLETED_STAGES predicate
provides:
  - push_file registered across all three totality guards (deterministic key, pipeline counters, agent-task router)
  - push_file:<file_id> deterministic-key dedup (CLOUDPIPE-05 idempotency)
  - push_file recovery classification (PUSHED/ANALYZED/ANALYSIS_FAILED = done; PUSHING/AWAITING_CLOUD/DISCOVERED = re-drivable)
  - fileserver-routed push_file re-drive partition in recover_orphaned_work
affects: [50-03, 50-04, push_file task implementation, staging cron, agent_worker push registration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "A new pipeline stage trips four completeness guards at once (key builder + counters + router + recovery predicate); land them in one plan so the suite never goes red between them"
    - "Per-kind recovery re-drive partition: file-touching stages route to a kind-scoped agent (fileserver for push, compute for held analyze), skip-not-raise when that kind is offline"

key-files:
  created:
    - tests/test_reenqueue.py
  modified:
    - src/phaze/tasks/_shared/deterministic_key.py
    - src/phaze/services/pipeline_counters.py
    - src/phaze/services/enqueue_router.py
    - src/phaze/tasks/reenqueue.py
    - tests/test_pipeline_counters.py
    - tests/test_tasks/test_recovery.py

key-decisions:
  - "push_file is keyed (not unkeyed): push_file:<file_id> collapses a double-tick of the staging cron to a no-op"
  - "push-done set is {PUSHED, ANALYZED, ANALYSIS_FAILED}; the analyze done-set stays {ANALYZED, ANALYSIS_FAILED} so a PUSHED file still drives analysis (D-10)"
  - "A re-driven push_file routes to a fileserver-kind agent (the rsync initiator); no fileserver online => WARNING skip, never a raise or a compute enqueue"

patterns-established:
  - "Pattern 1: register a new keyed stage in deterministic_key._KEY_BUILDERS + pipeline_counters.PIPELINE_FUNCTIONS + enqueue_router.AGENT_TASKS together, bumping all three count comments in the same commit"
  - "Pattern 2: a file-touching recovery re-drive partitions its rows out of the kind-agnostic agent loop and routes them to select_active_agent(kind=...), mirroring the AWAITING_CLOUD compute partition"

requirements-completed: [CLOUDPIPE-01, CLOUDPIPE-05]

# Metrics
duration: ~35min
completed: 2026-06-26
---

# Phase 50 Plan 02: Push Pipeline Totality Guards + Recovery Classification Summary

**push_file registered across the deterministic-key / pipeline-counter / agent-router guards (keyed, counted, routable) and classified in ledger-driven recovery so an orphaned PUSHING file re-drives to a fileserver while a PUSHED file stays analysis-eligible**

## Performance

- **Duration:** ~35 min
- **Completed:** 2026-06-26
- **Tasks:** 2 (Task 2 was TDD: RED + GREEN)
- **Files modified:** 6 (1 created, 5 modified)

## Accomplishments
- `push_file` trips all three totality guards together in one commit, so the keyed-or-exempt drift guard, the counter-sync guard, and router-routability are satisfied at once (no red window).
- `push_file:<file_id>` deterministic key collapses a repeated staging-cron enqueue to a SAQ no-op (CLOUDPIPE-05 / T-50-double-enqueue).
- Recovery classifies the new states: PUSHED/ANALYZED/ANALYSIS_FAILED = domain-completed; PUSHING/AWAITING_CLOUD/DISCOVERED = orphaned and re-driven.
- A re-driven `push_file` routes to a fileserver-kind agent (the media-mount owner that runs the rsync), and skips with a WARNING — never raises, never enqueues onto a compute agent (T-50-misroute).
- The analyze done-set is unchanged ({ANALYZED, ANALYSIS_FAILED}), so a PUSHED file is still re-drivable for `process_file` (D-10).

## Task Commits

1. **Task 1: Register push_file in all three totality guards** - `f43dc93` (feat)
2. **Task 2 (RED): Recovery tests for push_file classification** - `f5263b9` (test)
3. **Task 2 (GREEN): Classify push_file in recovery + fileserver re-drive** - `3a667c7` (feat)

_Note: Task 2 is a TDD task — RED test commit precedes the GREEN implementation commit._

## Files Created/Modified
- `tests/test_reenqueue.py` - New recovery tests for push_file (PUSHING→re-drive, PUSHED/ANALYZED→done, fileserver routing, no-fileserver→skip, analyze done-set unchanged)
- `src/phaze/tasks/_shared/deterministic_key.py` - Added `push_file: lambda k: str(k["file_id"])` builder; "8 entries" → 9
- `src/phaze/services/pipeline_counters.py` - Appended `push_file` to `PIPELINE_FUNCTIONS`; "8 functions" → 9
- `src/phaze/services/enqueue_router.py` - Added `push_file` to the `AGENT_TASKS` frozenset
- `src/phaze/tasks/reenqueue.py` - Added `_PUSH_DONE` set + `_select_done_push_ids`; `push_file` in `_DOMAIN_COMPLETED_STAGES` + `is_domain_completed` branch; fileserver re-drive partition in `recover_orphaned_work`
- `tests/test_pipeline_counters.py` - Updated count-dependent test 8 → 9
- `tests/test_tasks/test_recovery.py` - Updated predicate-totality test 3 → 4 stages (push_file joins the predicate-covered set)

## Decisions Made
- **push_file keyed, not exempt:** placed in `_KEY_BUILDERS` (not `_UNKEYED_TASKS`) so a repeat staging-cron tick dedups — idempotency is the CLOUDPIPE-05 mitigation.
- **push-done predicate spans pushing AND analysis:** `{PUSHED, ANALYZED, ANALYSIS_FAILED}` — a file that advanced to ANALYZED can only have done so after a successful push, so it is push-done by implication.
- **Analyze done-set untouched:** PUSHED is deliberately NOT added to the analyze done-set, so a pushed-but-not-analyzed file keeps driving analysis (D-10).
- **Fileserver-kind re-drive:** push_file rows partition out of the kind-agnostic agent loop and route to `select_active_agent(kind="fileserver")`, mirroring the Phase-49 AWAITING_CLOUD compute partition; no fileserver online → WARNING skip.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated count-dependent guard test (8 → 9)**
- **Found during:** Task 1 (registering push_file in pipeline_counters)
- **Issue:** `tests/test_pipeline_counters.py::test_read_counters_covers_eight_functions` asserted `len(counters) == 8`; adding `push_file` to `PIPELINE_FUNCTIONS` made the real count 9, so the assertion would fail.
- **Fix:** Renamed to `test_read_counters_covers_all_functions` and asserted `len(counters) == len(PIPELINE_FUNCTIONS) == 9` (robust to future additions).
- **Files modified:** tests/test_pipeline_counters.py
- **Verification:** `uv run pytest tests/test_pipeline_counters.py -q` green.
- **Committed in:** f43dc93 (Task 1 commit)

**2. [Rule 1 - Bug] Updated recovery predicate-totality test (3 → 4 stages)**
- **Found during:** Task 2 GREEN (adding push_file to `_DOMAIN_COMPLETED_STAGES`)
- **Issue:** `tests/test_tasks/test_recovery.py::test_domain_completed_stages_are_exactly_the_three_agent_stages` asserted the predicate-covered set equalled exactly the three prior stages; adding `push_file` broke that exact-equality assertion.
- **Fix:** Renamed to `..._four_agent_stages` and added `push_file` to the expected set; updated the parametrized XOR test docstring and the module docstring enumeration accordingly.
- **Files modified:** tests/test_tasks/test_recovery.py
- **Verification:** `uv run pytest tests/test_tasks/test_recovery.py -q` green (50 passed across recovery suites).
- **Committed in:** 3a667c7 (Task 2 GREEN commit)

---

**Total deviations:** 2 auto-fixed (both Rule 1 — directly-caused existing-test updates)
**Impact on plan:** Both were mechanical updates to count/exact-set assertions that the plan's own guard additions necessarily invalidated. No scope creep.

## Issues Encountered
- **Pre-commit stash vs. linter auto-fix aborted two commit attempts.** The ruff isort/format hooks reordered an import (`_PUSH_DONE` before `_build_done_sets`) and collapsed a multi-line comprehension; with competing staged/unstaged states the pre-commit stash rolled back and aborted the commit. Resolved by committing the RED test file alone (no competing unstaged changes to the same file), then re-staging the format-clean source before the GREEN commit. No `--no-verify` used; all hooks passed on the landed commits.

## TDD Gate Compliance
- RED commit `f5263b9` (`test(50-02): ...`) precedes GREEN commit `3a667c7` (`feat(50-02): ...`). RED was verified failing (ImportError on `_PUSH_DONE` + unclassified push_file) before implementation. No REFACTOR commit was needed.

## Known Stubs
None — all changes are real logic (key builder, counter registration, router membership, recovery predicate + partition). No placeholder data or empty values introduced.

## Threat Flags
None — changes implement the plan's existing threat-model mitigations (T-50-double-enqueue via deterministic key, T-50-orphan-leak via predicate classification, T-50-misroute via fileserver-kind routing). No new security surface introduced.

## Next Phase Readiness
- The guards now accept `push_file`, so the actual `push_file` task implementation + `agent_worker` registration (a later 50-plan) can land without re-tripping the drift/totality guards.
- Recovery already re-drives orphaned PUSHING files to a fileserver and holds them safely when none is online.
- Note: the `push_file` task function (`phaze/tasks/push.py`) and its `agent_worker.settings["functions"]` registration are intentionally NOT part of this plan and remain to be implemented.

## Self-Check: PASSED

All claimed files exist on disk; all three task commits (`f43dc93`, `f5263b9`, `3a667c7`) are present in git history.

---
*Phase: 50-push-pipeline*
*Completed: 2026-06-26*
