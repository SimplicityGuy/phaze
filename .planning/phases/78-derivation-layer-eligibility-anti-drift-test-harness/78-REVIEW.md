---
phase: 78-derivation-layer-eligibility-anti-drift-test-harness
reviewed: 2026-07-08T00:00:00Z
depth: standard
files_reviewed: 5
files_reviewed_list:
  - src/phaze/enums/stage.py
  - src/phaze/services/stage_status.py
  - tests/integration/test_stage_status_equivalence.py
  - tests/shared/test_stage_eligibility_dag.py
  - tests/shared/test_stage_resolver.py
findings:
  critical: 0
  warning: 3
  info: 2
  total: 5
status: issues_found
---

# Phase 78: Code Review Report

**Reviewed:** 2026-07-08T00:00:00Z
**Depth:** standard
**Files Reviewed:** 5
**Status:** issues_found

## Summary

Reviewed the two new predicate modules (`enums/stage.py` DB-free resolver + `services/stage_status.py` SQLAlchemy twin) and their three test files. The phase is purely additive — no reader/writer is wired to these builders, so nothing ships broken. The three hard contracts hold:

- **DB-free boundary:** `enums/stage.py` imports only `enum` + `typing`; the subprocess guard in `test_stage_resolver.py` enforces it. Verified — no `phaze.models` / `phaze.database` / `sqlalchemy` in the import graph.
- **`saq_detail` SAVEPOINT + static SQL:** `_SAQ_DETAIL_SQL` is a static `text()` with a literal status allowlist, no interpolation, run inside `session.begin_nested()`, degrade-safe on any `Exception`. Verified — no injection surface.
- **`in_flight` from `scheduling_ledger`:** `inflight_clause` probes `SchedulingLedger.key` on the deterministic `"<function>:<file_id>"` key via `STAGE_TO_FUNCTION`; `saq_jobs` is read-only/detail-only and never flips the boolean. Verified.

The DERIV-04 equivalence harness genuinely covers the matrix cells it enumerates, and the SQL/Python twins agree on every covered cell. **No BLOCKER-class defect** (no security, data-loss, or ships-broken issue). Findings are latent-logic gaps and drift/maintainability hazards, several of which matter because this layer exists specifically to guard the 44.5K over-enqueue class.

## Warnings

### WR-01: `done(review)` is coextensive with `done(propose)` — review eligibility is unreachable, and the unit test masks it with an impossible status map

**File:** `src/phaze/services/stage_status.py:107-109`, `src/phaze/enums/stage.py:127-128,192-193`, `tests/shared/test_stage_eligibility_dag.py:123-126`

**Issue:** For both `PROPOSE` and `REVIEW`, `done_clause` returns the identical predicate `exists(RenameProposal WHERE file_id == FileRecord.id)`, and the Python twin resolves both from the same `row_present = (any proposal exists)` scalar. So whenever `PROPOSE` derives `DONE`, `REVIEW` derives `DONE` too — they can never differ. But `eligible(REVIEW)` (the generic downstream branch) requires `status_map[PROPOSE] == DONE AND status_map[REVIEW] != DONE`. Under real derived status those two conditions are mutually exclusive, so `eligible(status_map, REVIEW)` is **always `False`** once wired to derived rows. The passing unit test `test_review_requires_proposal_exists` hides this by hand-constructing `{PROPOSE: DONE}` with `REVIEW` absent (defaulting to `NOT_STARTED`) — a status map that can never arise from `resolve_status`, giving false confidence that the review gate fires. This is the open assumption RESEARCH A3 explicitly flagged ("If review 'done' should mean 'a decision was made' … the predicate shifts … Confirm with planner") carried as-is.

**Fix:** Confirm the intended review semantics with the planner before cutover. If review-eligible means "a proposal awaits a decision," `done(review)` should mean "a decision was made" (`status IN ('approved','rejected')`), distinct from `done(propose)` = "a proposal exists"; then add an equivalence-matrix cell where `PROPOSE=DONE` but `REVIEW≠DONE` so the divergence is actually exercised. If the current coextensive semantics are truly intended, remove `REVIEW` from the auto-eligibility DAG path (it is a human step) rather than shipping a gate that can never fire, and drop the misleading impossible-map test.

### WR-02: `eligible()` downstream branch gates only on `!= DONE` (not on `IN_FLIGHT`), and propose in-flight is invisible — a latent re-enqueue vector for the exact class this layer guards

**File:** `src/phaze/enums/stage.py:186-193`, `src/phaze/services/stage_status.py:158-167`

**Issue:** The enrich branch correctly excludes both `DONE` and `IN_FLIGHT` (`not in (DONE, IN_FLIGHT)`), but the generic downstream branch (`TRACKLIST`/`PROPOSE`/`REVIEW`) only checks `status_map.get(stage) != DONE`. Combined with `inflight_clause` returning a constant `false()` for propose (its ledger key is a batch set-hash, scoped out per OQ1/Pitfall 5), a propose batch that is genuinely mid-flight derives `NOT_STARTED` (no proposal row yet, `inflight=false`), so `eligible(status_map, PROPOSE)` returns `True` while the stage is already running. That is precisely the "re-queue never-should-be-requeued work" shape behind the 2026-06-18 44.5K over-enqueue incident. It is documented as deferred and the deterministic-key/ledger dedup at enqueue time is the real backstop, but the eligibility layer itself offers no guard and the asymmetry with the enrich branch is silent.

**Fix:** Make the guard uniform and defensive: exclude `IN_FLIGHT` in the downstream branch too — `return upstream_done and status_map.get(stage, Status.NOT_STARTED) not in (Status.DONE, Status.IN_FLIGHT)`. Separately, add an explicit note (or a follow-up ticket reference in the docstring) that propose in-flight is not representable until OQ1 cutover, so a future reader does not assume `eligible(PROPOSE)` is in-flight-safe.

### WR-03: The fingerprint "done" allowlist `_DONE_FP` is hardcoded twice — two sources of truth the anti-drift phase is meant to eliminate

**File:** `src/phaze/enums/stage.py:56` (frozenset) and `src/phaze/services/stage_status.py:86` (tuple)

**Issue:** `_DONE_FP = {"success", "completed"}` is defined independently in both modules. `services/stage_status.py` already imports from `phaze.enums.stage` (`Stage`, `Status`), so it could import the single source but instead re-spells the literal. The entire premise of this phase is "the two halves can NEVER drift." A hand-copied allowlist is a silent drift seam: editing one (e.g. adding a third success alias) without the other breaks equivalence, and because the DERIV-04 test only asserts on the currently-enumerated statuses, a drift on a new alias would not necessarily be caught.

**Fix:** Export the canonical allowlist from the DB-free module and import it in the SQL twin, e.g. in `enums/stage.py` expose `DONE_FINGERPRINT_STATUSES: tuple[str, ...] = ("success", "completed")` and in `stage_status.py` `from phaze.enums.stage import DONE_FINGERPRINT_STATUSES as _DONE_FP`. `.in_(tuple)` renders the same `= ANY (ARRAY[...])` and the frozenset-vs-tuple distinction is irrelevant for membership.

## Info

### IN-01: Four identical presence-status wrappers add no value

**File:** `src/phaze/enums/stage.py:119-132`

**Issue:** `_tracklist_status`, `_propose_status`, `_review_status`, and `_apply_status` all have identical signatures and bodies (`return _presence_status(present=row_present, failed=failed, inflight=inflight)`). `resolve_status` already dispatches all four to the same code path via the shared `row_present`/`failed` locals, so the four wrappers are dead duplication.

**Fix:** Delete the four wrappers and call `_presence_status(present=row_present, failed=failed, inflight=inflight)` directly in `resolve_status` for the downstream branch (the stage identity is already known from the dispatch). If they are kept as intentional seams for future per-stage divergence, add a one-line comment saying so.

### IN-02: Python `_presence_status` accepts a `failed` input for tracklist that the SQL twin hardcodes to `false()` — an uncovered twin-divergence seam

**File:** `src/phaze/enums/stage.py:119-120`, `src/phaze/services/stage_status.py:137-138`

**Issue:** For `TRACKLIST`, `failed_clause` returns a constant `false()` (no failure marker), but the Python `resolve_status(TRACKLIST, ...)` still honors a `failed` scalar. A caller that passes `{"failed": True}` for tracklist gets `Status.FAILED` from Python while the SQL twin can only ever yield `not_started`/`done`. The equivalence harness never exercises this (its tracklist reader hardcodes `failed=False`), so the divergence is undetected. Low risk today (no tracklist failure source exists), but it is a latent way for the two halves to disagree.

**Fix:** Either drop the `failed` parameter from the tracklist path (ignore it, matching the SQL `false()`), or add an equivalence cell that would fail if a tracklist `failed` input were ever honored. A short comment on `_tracklist_status` noting "tracklist has no failure marker; `failed` is always False by contract" would also suffice.

---

## Structural Findings (fallow)

No structural pre-pass (`<structural_findings>`) was provided for this review.

---

_Reviewed: 2026-07-08T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
