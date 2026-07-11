---
phase: 86
slug: proposals-cutover
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-10
validated: 2026-07-11
---

# Phase 86 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `86-RESEARCH.md` § Validation Architecture.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (async) |
| **Config file** | `pyproject.toml`; buckets in `tests/buckets.json` (directory-based) |
| **Buckets relevant** | `shared` (store_proposals), `review` (proposal_queries + agent_proposals router) |
| **Quick run command** | `uv run pytest tests/shared/core/test_proposals_upsert.py -x` |
| **Bucket run (isolation)** | `just test-bucket shared` / `just test-bucket review` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~seconds per bucket (full suite flakes under colima VM pressure — re-run failed subset in isolation) |

---

## Sampling Rate

- **After every task commit:** Run the touched bucket's quick run (`uv run pytest tests/<bucket>/... -x`)
- **After every plan wave:** Run `just test-bucket shared` + `just test-bucket review` in isolation
- **Before `/gsd:verify-work`:** Full suite green; 90% coverage floor
- **Max feedback latency:** < 60 seconds

---

## Per-Task Verification Map (behavioral seams)

| Seam | Behavior | Test Type | Automated Command | Test Function | Status |
|------|----------|-----------|-------------------|---------------|--------|
| MOVED-not-re-proposed (the bug) | `store_proposals` on a stale batch where the file has an `executed` proposal → executed proposal row untouched, `is_applied()` True, file row not written | integration | `just test-bucket shared` | `test_proposals_upsert.py::test_stale_batch_does_not_disturb_executed_file` (:183) | ✅ green |
| PATCH still writes current_path | `moved` PATCH persists `file_record.current_path = body.current_path` | integration | `just test-bucket review` | `test_agent_proposals.py::test_executed_joint_update` — asserts `f.current_path == "/new/proposed.mp3"` (:95) | ✅ green |
| PATCH echoes request file_state | success `moved`/`unchanged` responses return `body["file_state"]` without reading `file.state` | integration | `just test-bucket review` | `test_agent_proposals.py::test_executed_joint_update` — `body["file_state"]=="moved"` (:84) + `f.state == APPROVED.value` unchanged guard (:94) | ✅ green |
| Idempotent replay path | same-state PATCH returns 200 and does NOT read `file.state` | integration | `just test-bucket review` | `test_agent_proposals.py::test_same_state_idempotent_no_op` (:120) | ✅ green |
| Anti-drift (no state write survives) | AST scan over `proposal.py`, `proposal_queries.py`, `agent_proposals.py` finds zero `FileRecord.state` Store/Load and zero `FileState` write-target occurrences; base-kind-agnostic (chained-attr + two-step-ORM idiom) | source-scan (DB-free) | `uv run pytest tests/shared/test_proposals_cutover_source_scan.py` | `test_proposals_cutover_source_scan.py` — 18 tests incl. `test_guard_flags_chained_attr_string_write` (:403), `test_guard_flags_two_step_orm_idiom_write` (:422) | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/shared/core/test_proposals_upsert.py` — new D-03 stale-batch/executed test (`test_stale_batch_does_not_disturb_executed_file`); stale `PROPOSAL_GENERATED` assertion removed (86-01; stale `test_proposal.py` assertion also removed in gap plan 86-04)
- [x] `tests/shared/test_proposals_cutover_source_scan.py` — AST guard, mutation-verified; broadened to base-kind-agnostic `.state` + two-step-ORM idiom in gap plan 86-05
- [x] `tests/review/services/test_proposal_queries.py` — `.file.state` assertions dropped (86-01; 29/29 green)
- [x] `tests/review/routers/test_agent_proposals.py` — `f.state` cascade assertions dropped; current_path + echo + "state unchanged" guards added; replay test adapted (86-02; 11/11 green)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions | Result |
|----------|-------------|------------|-------------------|--------|
| Mutation-verify each new guard | SIDECAR-03 (D-03) | A GREEN guard proves nothing until it has failed once; automation can't self-certify the RED step | For each new assertion/AST guard: break the code (re-add the `file.state` write / regress `_TERMINAL_FILE_STATES`) → run the bucket → confirm RED → restore → confirm GREEN. Record in verification doc. | ✅ DONE — 86-03 (initial guard) and 86-05 (chained-attr + two-step-ORM RED cases) both recorded RED→restore→GREEN in their SUMMARYs; the gsd-verifier independently reproduced RED→GREEN (reverting the 86-05 broadening → 2 new tests fail; restored → 18 pass). Recorded in 86-VERIFICATION.md. |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] Every new guard mutation-verified (RED→restore recorded) — 86-03 + 86-05 SUMMARYs; verifier-reproduced
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-10 (plan-checker VERIFICATION PASSED; 5/5 tasks carry automated verify, no watch-mode, latency <60s)

---

## Validation Audit 2026-07-11

| Metric | Count |
|--------|-------|
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

All 5 behavioral seams are COVERED by shipped, passing tests (State A audit — no gaps, no auditor spawn). Every seam's automated command ran green during and after execution: `test_proposals_upsert.py` 5/5, `test_proposal_queries.py` 29/29, `test_agent_proposals.py` 11/11, `test_proposals_cutover_source_scan.py` 18/18, full `tests/review` bucket 428/0. The single Manual-Only item (mutation-verify each guard) is complete with recorded RED→restore→GREEN evidence, independently reproduced by the verifier. Phase 86 is Nyquist-compliant.
