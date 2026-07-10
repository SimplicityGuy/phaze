---
phase: 85
slug: executed-gate-revival
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-10
updated: 2026-07-10
---

# Phase 85 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (uv-managed; `uv run pytest`) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `just test-bucket review` (the bucket owning tag/cue/tracklist/review tests) |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~30–60s per bucket; full suite several minutes |

---

## Sampling Rate

- **After every task commit:** Run `just test-bucket <bucket>` for the touched bucket (in isolation — per-bucket isolation is mandatory).
- **After every plan wave:** Run the affected buckets (`review`, `shared`, `agents` as touched).
- **Before `/gsd:verify-work`:** Full suite must be green (`uv run pytest`) plus `uv run mypy .` and `uv run ruff check .`.
- **Max feedback latency:** ~60 seconds.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Test File | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-----------|--------|
| 85-01 | 01 | 1 | READ-05 | T-85-02 | `applied()` predicate returns true iff `proposals.status=='executed'`; never reads `file.state` | unit (DB) | `tests/shared/test_applied_clause.py` (7 cases incl. `test_applied_never_reads_file_state`, `test_failed_and_executed_proposals_is_applied`) | ✅ green |
| 85-02 | 02 | 2 | READ-05 | T-85-02 | **Behavior change (SC#2):** an actually-applied file (`state='moved'` + executed proposal) now PASSES the tag guard that previously always failed; a non-applied file RAISES | unit (DB) | `tests/review/services/test_tag_writer.py::TestExecuteTagWrite::{test_applied_file_passes_guard,test_non_applied_file_raises}` (mutation-verified RED→GREEN by 85-VERIFICATION) | ✅ green |
| 85-03 | 03 | 2 | READ-05 | T-85-02 | **SC#2 (CUE):** CUE generation admits an applied file seeded `state='moved'`, rejects a non-applied file | unit (route) | `tests/review/routers/test_cue.py::{test_generate_cue_admits_applied_file_not_executed_state,test_generate_cue_file_not_applied}` | ✅ green |
| 85-04 | 04 | 3 | READ-05 | T-85-04, T-85-06 | Unbounded operator list builders (`get_tagwrite_review_rows` / `get_cue_review_cards`) return a bounded page (D-03 `_MAX_REVIEW_ROWS`) | unit (DB) | `tests/review/services/test_review_degrade.py::test_get_tagwrite_review_rows_bounded_by_cap` (+ degrade-wrapper cells) | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*
*Cross-referenced against the executed test suite 2026-07-10; all 12 contract tests pass (`-p no:randomly`, ephemeral DB on 5433).*

---

## Wave 0 Requirements

- [x] Test(s) for the `applied()` / `applied_clause()` predicate + `is_applied()` per-record helper — landed in `tests/shared/test_applied_clause.py` (shared bucket; the predicate lives in the shared `services/stage_status.py`).
- [x] The SC#2 behavior-change test: seed an applied file (`state='moved'` + `proposals.status='executed'`), assert it now passes a previously-always-failing guard. **Mutation-tested** — 85-VERIFICATION independently reverted `tag_writer.py:185` to the dead `state != EXECUTED` guard, watched `test_applied_file_passes_guard` go RED, restored, re-ran GREEN.
- [x] Pagination/bound test for the newly-bounded list builders — `test_get_tagwrite_review_rows_bounded_by_cap` monkeypatches `_MAX_REVIEW_ROWS=3` and asserts the builder returns exactly 3.
- [x] Assert from an INDEPENDENT session where a mutating router path is exercised — the route tests (`test_cue.py`, `test_tags.py`) exercise the write/generate paths and read back via the DB-backed fixtures (get_session override reads uncommitted rows; assertions use committed reads).

*Existing pytest infrastructure covers the framework; no new framework install.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live-corpus tag/CUE write end-to-end | READ-05 | Requires the real applied-file corpus + filesystem mutation; not reproducible in unit tests | Deploy to homelab; confirm Tags/Cue operator lists populate with real applied files; trigger one manual tag-write; verify tags written to the file on disk and a `TagWriteLog` COMPLETED row persists |

---

## Validation Audit 2026-07-10

| Metric | Count |
|--------|-------|
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

State A audit (post-execution). Cross-referenced the plan-time coverage contract against the executed
test suite: all 3 automated requirement-verification items are COVERED by real, green tests
(predicate contract, SC#2 behavior change incl. mutation-verification, D-03 bound). No MISSING or
PARTIAL gaps — the gsd-nyquist-auditor was not needed. The one Manual-Only item (live-corpus tag/CUE
write) remains tracked in `85-HUMAN-UAT.md` for the next homelab rollout. Phase is Nyquist-compliant.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-10 (plan-checker confirmed all 9 tasks carry automated verify)
