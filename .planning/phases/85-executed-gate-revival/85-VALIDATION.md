---
phase: 85
slug: executed-gate-revival
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-10
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

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 85-01-xx | 01 | 1 | READ-05 | — | `applied()` predicate returns true iff `proposals.status=='executed'`; never reads `file.state` | unit | `just test-bucket review` | ❌ W0 | ⬜ pending |
| 85-01-xx | 01 | 1 | READ-05 | — | **Behavior change (SC#2):** an actually-applied file now PASSES the tag/CUE guards that previously always failed | unit | `just test-bucket review` | ❌ W0 | ⬜ pending |
| 85-0x-xx | 0x | 2 | READ-05 | — | Unbounded operator list builders (`review.py:get_tagwrite_review_rows` + sibling) return a bounded page | unit | `just test-bucket review` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*
*Exact task IDs assigned by the planner; this map is the coverage contract, not the task breakdown.*

---

## Wave 0 Requirements

- [ ] Test(s) for the `applied()` / `applied_clause()` predicate + `is_applied()` per-record helper — bucket `review` (or `shared` if predicate lives in a shared module).
- [ ] The SC#2 behavior-change test: seed an applied file (`proposals.status='executed'`), assert it now passes a previously-always-failing guard. **Mutation-test it** — break the predicate, confirm RED, restore.
- [ ] Pagination test(s) for the newly-bounded list builders.
- [ ] Assert from an INDEPENDENT session where a mutating router path is exercised (conftest get_session override reads uncommitted rows).

*Existing pytest infrastructure covers the framework; no new framework install.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live-corpus tag/CUE write end-to-end | READ-05 | Requires the real applied-file corpus + filesystem mutation; not reproducible in unit tests | Deploy to homelab; confirm Tags/Cue operator lists populate with real applied files; trigger one manual tag-write; verify tags written to the file on disk and a `TagWriteLog` COMPLETED row persists |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
