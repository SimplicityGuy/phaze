---
phase: 60
slug: review-apply
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-01
---

# Phase 60 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio + httpx AsyncClient |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/routers/ -x -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/routers/ -x -q`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green (≥85% coverage)
- **Max feedback latency:** 60 seconds

---

## Per-Task Verification Map

> Populated during planning/execution. Each REVIEW requirement maps to automated pytest coverage per the RESEARCH.md § Validation Architecture.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 60-02/03 | 02, 03 | 2-3 | REVIEW-01 | — | Rename/Tag/Move before→after diff renders as a bare fragment with per-file Approve/Edit/Skip | unit (route+template) | `uv run pytest tests/test_review_apply_workspaces.py -q` | ❌ W0 | ⬜ pending |
| 60-01 T2/T3 | 01 | 1 | REVIEW-02 | T-60-01/02 | Bulk-approve re-queries server-side (confidence≥0.9 / no-discrepancies predicate); never trusts a client id-list | unit (behavioral) | `uv run pytest tests/test_review_apply_workspaces.py -q` | ❌ W0 | ⬜ pending |
| 60-04 T1 | 04 | 4 | REVIEW-03 | — | Dedupe keeper-select resolves via `canonical_id`; auto-keep bulk + reversible undo (`file_states` round-trip) | unit | `uv run pytest tests/test_review_apply_workspaces.py -q` | ❌ W0 | ⬜ pending |
| 60-04 T2 | 04 | 4 | REVIEW-04 | — | Cue preview renders + approve gated on matched tracklist (`POST /cue/{id}/generate`) | unit | `uv run pytest tests/test_review_apply_workspaces.py -q` | ❌ W0 | ⬜ pending |
| 60-01 T3 + 60-04 T1 | 01, 04 | 1, 4 | REVIEW-05 | — | Exactly one audit row (ExecutionLog/TagWriteLog/dedupe-resolution) per applied change; reversible | integration | `uv run pytest tests/integration/test_review_audit.py -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] Test files/fixtures for the two NEW thin endpoints (D-02 server-predicate bulk-approve, D-05 inline Edit PATCH) — assert server re-query semantics, not client id-lists
- [ ] Fragment/template render assertions for the six superseded workspace stages (propose/rename/tagwrite/move/dedupe/cue)
- [ ] Reuse existing router test fixtures/conftest (async session, seeded RenameProposal / duplicate-group / tracklist rows)

*Existing pytest infrastructure covers the framework; new test files target the new endpoints + workspace fragments.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Visual before→after diff styling (struck-through current vs highlighted proposed) | REVIEW-01 | Pixel/visual fidelity to UI-SPEC not automatable in pytest | Load `/s/rename`, confirm diff partial matches 60-UI-SPEC.md |
| Counts-only OOB poll never clobbers in-progress selection | REVIEW-02 | Live HTMX polling + operator selection race is a runtime-timing behavior | Select rows, wait ≥5s for `/pipeline/stats` poll, confirm selection subtree unchanged |

*Automatable behaviors (endpoint predicates, audit-row counts, fragment rendering) are covered above.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
