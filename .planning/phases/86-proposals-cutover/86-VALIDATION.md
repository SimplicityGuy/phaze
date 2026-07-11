---
phase: 86
slug: proposals-cutover
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-07-10
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

| Seam | Behavior | Test Type | Automated Command | File Exists | Status |
|------|----------|-----------|-------------------|-------------|--------|
| MOVED-not-re-proposed (the bug) | `store_proposals` on a stale batch where the file has an `executed` proposal → executed proposal row untouched, `is_applied()` True, file row not written | integration | `just test-bucket shared` | ❌ W0 — new test in `test_proposals_upsert.py` | ⬜ pending |
| PATCH still writes current_path | `moved` PATCH persists `file_record.current_path = body.current_path` | integration | `just test-bucket review` | ⚠️ adapt `test_agent_proposals.py` | ⬜ pending |
| PATCH echoes request file_state | success `moved`/`unchanged` responses return `body["file_state"]` without reading `file.state` | integration | `just test-bucket review` | ✅ echo asserted; add "state unchanged" guard | ⬜ pending |
| Idempotent replay path | same-state PATCH returns 200 and does NOT read `file.state` | integration | `just test-bucket review` | ⚠️ adapt replay test | ⬜ pending |
| Anti-drift (no state write survives) | AST scan over `proposal.py`, `proposal_queries.py`, `agent_proposals.py` finds zero `FileRecord.state` Store/Load and zero `FileState` write-target occurrences | source-scan (DB-free) | `just test-bucket shared` | ❌ W0 — new `test_proposals_cutover_source_scan.py` | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/shared/core/test_proposals_upsert.py` — new D-03 stale-batch/executed test (replaces the terminal-state test); delete the `PROPOSAL_GENERATED` assertion in `test_fresh_insert_stamps_pk`
- [ ] `tests/shared/test_proposals_cutover_source_scan.py` (or `review/`) — new AST guard, mutation-verified (modeled on `test_reenqueue_reconcile_source_scan.py`)
- [ ] `tests/review/services/test_proposal_queries.py` — drop 3 `.file.state` assertions
- [ ] `tests/review/routers/test_agent_proposals.py` — drop `f.state` assertions; add current_path + echo + "state unchanged" guards; adapt replay test

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Mutation-verify each new guard | SIDECAR-03 (D-03) | A GREEN guard proves nothing until it has failed once; automation can't self-certify the RED step | For each new assertion/AST guard: break the code (re-add the `file.state` write / regress `_TERMINAL_FILE_STATES`) → run the bucket → confirm RED → restore → confirm GREEN. Record in verification doc. |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [ ] Every new guard mutation-verified (RED→restore recorded) — *asserted at execution time*
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-10 (plan-checker VERIFICATION PASSED; 5/5 tasks carry automated verify, no watch-mode, latency <60s)
