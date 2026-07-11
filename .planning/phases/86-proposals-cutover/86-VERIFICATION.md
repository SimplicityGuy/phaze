---
phase: 86-proposals-cutover
verified: 2026-07-11T03:10:00Z
status: passed
score: 9/9 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 7/9
  gaps_closed:
    - "Full test suite (review bucket) is green after this phase's deletions — no test in the repository asserts the removed FileRecord.state cascade"
    - "The AST source-scan guard is mutation-verified for every syntactic form of the deleted cascade"
  gaps_remaining: []
  regressions: []
deferred: []
human_verification: []
---

# Phase 86: Proposals Cutover Verification Report

**Phase Goal:** Make `proposals.status` the sole authority for review decisions and apply outcomes, deleting the redundant, drift-prone `FileRecord.state` cascade (where the `store_proposals` MOVED-regression bug lives).
**Verified:** 2026-07-11T03:10:00Z
**Status:** passed
**Re-verification:** Yes — after gap closure (plans 86-04, 86-05)

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Review decisions/apply outcomes read from `proposals.status`; `_TERMINAL_FILE_STATES` cascade deleted from all 3 files (Roadmap SC1) | VERIFIED | Regression check: `grep -c "FileState"` = 0 in `proposal.py`, `proposal_queries.py`, `agent_proposals.py`. `git diff --name-only -- src/` empty across the whole gap-closure wave — production deletion untouched and still correct. |
| 2 | `store_proposals` MOVED-regression evaporates, proven by a test (Roadmap SC2) | VERIFIED | `tests/shared/core/test_proposals_upsert.py::test_stale_batch_does_not_disturb_executed_file` unchanged, still passes (part of the 1058-passed `tests/shared` bucket run). |
| 3 | `proposal_queries.py` still writes `proposals.status` for APPROVED/REJECTED (86-01 must-have) | VERIFIED | Unchanged since initial verification; `tests/review/services/test_proposal_queries.py` still part of the green 428-passed `tests/review` bucket. |
| 4 | Apply-PATCH echoes `body.file_state`, persists `current_path`, wire byte-identical on the mutation branch (86-02 must-have) | VERIFIED | Unchanged; `tests/review/routers/test_agent_proposals.py` still part of the green `tests/review` bucket. |
| 5 | Same-state idempotent replay returns 200 without reading `file.state`, echoing `None` (86-02 must-have) | VERIFIED | Unchanged; covered by the same green bucket run. |
| 6 | Cross-tenant 403 guard + request schema unchanged (86-02 must-have) | VERIFIED | Unchanged; `agent_proposals.py` byte-identical guard block, confirmed via `git diff --name-only -- src/` empty. |
| 7 | AST source-scan guard proves clean absence across all 3 files, walks `Call.args`+`Call.keywords`, existence-asserted root (86-03 must-have) | VERIFIED | `tests/shared/test_proposals_cutover_source_scan.py` — 18/18 pass (16 prior + 2 new from 86-05); independently re-run `_violations()` on all three real source files returns `[]` for each (zero false positives after broadening). |
| 8 | AST guard is mutation-verified for **every syntactic form** of the deleted cascade (86-03 must-have text) — **GAP 2, now closed** | VERIFIED | Both previously-evasive shapes independently reproduced as now caught: `_violations('proposal.file.state = "approved"\n')` returns a 1-element list with a Store-context `.state` Attribute whose base is the chained `proposal.file` Attribute; `_violations('file_record = result.scalar_one_or_none()\nfile_record.state = "moved"\n')` returns a 1-element list with a Store-context `.state` off the ORM-row-bound `file_record` Name. Independently mutation-verified: reverting the two broadening edits in a throwaway copy of the guard file reproduces RED (`2 failed` — both `!=[]` assertions fail with `assert [] != []`), and the committed file runs GREEN (`18 passed`). Real-source guards on all 3 files stay clean (`[]`) — no false positives introduced. |
| 9 | Full `tests/review` bucket is green — no test in the repo asserts the removed cascade — **GAP 1, now closed** | VERIFIED | Independently re-ran `uv run pytest tests/review -q` → **428 passed, 0 failed** (was 1 failed, 427 passed). `grep -n 'file_record.state == "proposal_generated"'` and `grep -niE "file[_ ]state advances"` both return nothing in `tests/review/services/test_proposal.py`. Broad repo-wide grep for `file_record.state`/`FileRecord.state`/`proposal_generated` outside the guard file turns up only docstring/comment prose or reads of unrelated, still-legitimate `FileRecord.state` writers in other subsystems (dedup, cloud push, reconcile, staging) — none reference the deleted proposals cascade. The one remaining `file_record.state = "analyzed"` at `test_proposal.py:647` is harmless `MagicMock` scaffolding (never asserted on), explicitly left in place per the gap-closure plan's scope. |

**Score:** 9/9 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/proposal.py` | `store_proposals` upsert with cascade block removed | VERIFIED | Zero `FileState`/`.state` occurrences (regression check); untouched by gap plans. |
| `src/phaze/services/proposal_queries.py` | `update_proposal_status`/`bulk_update_status` write only `proposals.status` | VERIFIED | Zero `FileState`/`.file.state`; untouched by gap plans. |
| `src/phaze/routers/agent_proposals.py` | `patch_proposal_state` echoes request, zero `file_record.state` reads/writes | VERIFIED | Untouched by gap plans; `git diff --name-only -- src/` empty for the whole wave. |
| `tests/shared/core/test_proposals_upsert.py` | D-03 regression test | VERIFIED | Unchanged, still passes. |
| `tests/review/services/test_proposal_queries.py` | Status-only assertions | VERIFIED | Unchanged, part of green 428-passed bucket. |
| `tests/review/routers/test_agent_proposals.py` | Echo/seed-unchanged/replay-None assertions | VERIFIED | Unchanged, part of green 428-passed bucket. |
| `tests/shared/test_proposals_cutover_source_scan.py` | Mutation-verified AST guard | VERIFIED | 18/18 pass; base-kind-agnostic `.state` matching + `_orm_row_bound_names` helper added; independently mutation-verified RED→GREEN. |
| `tests/review/services/test_proposal.py` | No stale assertion of the removed cascade | VERIFIED | `test_creates_rename_proposal_records` no longer asserts `file_record.state == "proposal_generated"`; comment reworded to state `store_proposals` does NOT touch `FileRecord.state`. File passes in isolation (42 passed) and as part of the full bucket. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `test_proposals_upsert.py` | `services/stage_status.py` | `is_applied(session, file_id)` | WIRED | Unchanged, still passes. |
| `proposal.py` | `proposals` table | `pg_insert(...).on_conflict_do_update(index_where=status=='pending')` | WIRED | Unchanged (production code untouched by gap plans). |
| `agent_proposals.py` | `ProposalStateResponse.file_state` | `response_file_state = body.file_state` | WIRED | Unchanged. |
| `test_proposals_cutover_source_scan.py::_state_writes` | `proposal_queries.py` (deleted `proposal.file.state = ...`) | base-kind-agnostic `.state` attribute scan | WIRED | Independently confirmed: chained-attribute base (`ast.Attribute`) is flagged; real source file scans clean (no false positive). |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full `tests/review` bucket green | `uv run pytest tests/review -q` (independently re-run, TEST_DATABASE_URL/MIGRATIONS_TEST_DATABASE_URL/PHAZE_REDIS_URL exported at 5433/6380) | 428 passed, 0 failed | PASS |
| Stale assertion/comment removed | `grep -n 'file_record.state == "proposal_generated"'` / `grep -niE "file[_ ]state advances"` on `test_proposal.py` | both empty | PASS |
| AST guard full suite green | `uv run pytest tests/shared/test_proposals_cutover_source_scan.py -v` | 18 passed | PASS |
| AST guard catches chained-attribute write | `_violations('proposal.file.state = "approved"\n')` (independent repro, not from SUMMARY) | 1 violation, Store-context, `.state` off `ast.Attribute` base | PASS |
| AST guard catches two-step ORM idiom write | `_violations('file_record = result.scalar_one_or_none()\nfile_record.state = "moved"\n')` (independent repro) | 1 violation, Store-context, `.state` off ORM-row-bound Name | PASS |
| Real source files still scan clean | `_violations()` on `proposal.py`, `proposal_queries.py`, `agent_proposals.py` (independent repro) | `[]`, `[]`, `[]` | PASS |
| Mutation RED reproduced independently | Reverted the two broadening edits in a throwaway copy of the guard file, ran the two new RED cases | `2 failed` — `assert [] != []` on both | PASS |
| Mutation GREEN reproduced independently | Same two cases against the committed (broadened) guard | `2 passed` (part of 18 passed) | PASS |
| `src/` untouched by gap-closure wave | `git diff --name-only -- src/` | empty | PASS |
| Full `tests/shared` bucket | `uv run pytest tests/shared -q` | 1058 passed, 1 failed (`test_migration_019_dedupe.py` — `ModuleNotFoundError: No module named 'psycopg2'`) | INFO — see note below |
| Project mypy convention | `uv run mypy .` (the actual pre-commit/CI invocation, which excludes `tests/`) | `Success: no issues found in 209 source files` | PASS |

**Note on the one `tests/shared` failure:** `test_migration_019_dedupe.py::test_upgrade_019_dedupes_pending_and_creates_partial_unique_index` fails with `ModuleNotFoundError: No module named 'psycopg2'` — a missing sync PostgreSQL driver dependency, confirmed absent from `pyproject.toml` and the venv (`uv run python -c "import psycopg2"` also fails). This is a pre-existing environment gap unrelated to SIDECAR-03: neither gap-closure plan touched this file or any migration/psycopg2-related code (`files_modified` for 86-04/86-05 are `tests/review/services/test_proposal.py` and `tests/shared/test_proposals_cutover_source_scan.py` only), and `git diff --name-only -- src/` is empty for the whole wave. Not a regression introduced by this phase; out of scope for SIDECAR-03.

### Probe Execution

SKIPPED — no `scripts/*/tests/probe-*.sh` declared or found for this phase.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|--------------|--------|----------|
| SIDECAR-03 | 86-01, 86-02, 86-03, 86-04, 86-05 | Review decisions/apply outcomes read from `proposals.status` + `execution_log`; `FileRecord.state` no longer a redundant mirror; `store_proposals` MOVED-regression fixed | SATISFIED | Core deletion verified (truths 1-7, unchanged since initial verification). Both prior gaps independently confirmed closed: the full `tests/review` bucket is green (428 passed, 0 failed) and no test asserts the removed cascade; the AST anti-drift guard is now base-kind-agnostic and independently mutation-verified against both previously-evasive shapes (chained-attribute write, two-step ORM idiom). No orphaned requirements — REQUIREMENTS.md line 151 maps SIDECAR-03 to Phase 86 only, matching all five plans' `requirements:` frontmatter. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `tests/review/services/test_proposal.py` | 647 | `file_record.state = "analyzed"` on a `MagicMock` (scaffolding, never asserted on) | ℹ️ Info | Harmless leftover mock setup; explicitly scoped out of the gap-closure plan ("Leave the file_record MagicMock scaffolding... in place; it is harmless"). Does not assert the deleted cascade — confirmed no assertion downstream references it. |
| `tests/shared/core/test_migration_019_dedupe.py` | n/a | `ModuleNotFoundError: No module named 'psycopg2'` | ℹ️ Info | Pre-existing environment gap, unrelated to SIDECAR-03 or either gap-closure plan; not introduced by this phase. |

No `TBD`/`FIXME`/`XXX` debt markers found in either gap-closure plan's touched files.

### Human Verification Required

None — this phase (and its gap-closure wave) is backend-only (service + router + test-only changes), no UI/visual/real-time surface introduced.

### Gaps Summary

None. Both gaps from the prior verification are independently confirmed closed:

1. **Gap 1 (stale test assertion) — CLOSED.** The `file_record.state == "proposal_generated"` assertion and its misleading "the file state advances" comment are gone from `tests/review/services/test_proposal.py`. The full `tests/review` bucket now runs 428 passed, 0 failed (independently re-run, not taken from SUMMARY claims).

2. **Gap 2 (AST guard blind spot) — CLOSED.** The guard's `_state_reads`/`_state_writes` now flag a `.state` attribute regardless of base kind (bare `ast.Name` bound via `FileRecord`-textual RHS or the new `_orm_row_bound_names` ORM-row-fetch idiom, OR any `ast.Attribute` chain). Both previously-evasive shapes — `proposal.file.state = "approved"` and the two-step `file_record = result.scalar_one_or_none(); file_record.state = "moved"` idiom — are independently reproduced as now caught, and independently mutation-verified RED (reverted scanner) → GREEN (committed scanner). No false positives introduced: all three real source files still scan clean.

The phase goal is achieved: `proposals.status` is the sole authority for review decisions and apply outcomes, the `FileRecord.state` cascade is deleted from all three production files, the `store_proposals` MOVED-regression is proven fixed by a test, the full `tests/review` bucket is green, and the anti-drift guard has real, independently-verified teeth against the actual deleted-cascade shapes.

---

*Verified: 2026-07-11T03:10:00Z*
*Verifier: Claude (gsd-verifier)*
