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
| 60-XX-XX | XX | X | REVIEW-01 | — | Rename/Tag/Move before→after diff renders with per-file Approve/Edit/Skip | integration | `uv run pytest tests/routers/test_shell.py -q` | ❌ W0 | ⬜ pending |
| 60-XX-XX | XX | X | REVIEW-02 | — | Bulk-approve re-queries server-side (confidence≥0.9 / zero-discrepancies); never trusts a client id-list | integration | `uv run pytest tests/routers/test_proposals.py -q` | ❌ W0 | ⬜ pending |
| 60-XX-XX | XX | X | REVIEW-03 | — | Dedupe keeper-select resolves via `canonical_id`; auto-keep bulk + reversible undo | integration | `uv run pytest tests/routers/test_duplicates.py -q` | ❌ W0 | ⬜ pending |
| 60-XX-XX | XX | X | REVIEW-04 | — | Cue preview renders + approve gated on matched tracklist (`POST /cue/{id}/generate`) | integration | `uv run pytest tests/routers/test_cue.py -q` | ❌ W0 | ⬜ pending |
| 60-XX-XX | XX | X | REVIEW-05 | — | Exactly one audit row (ExecutionLog/TagWriteLog/dedupe-resolution) per applied change; reversible | integration | `uv run pytest tests/routers/test_execution.py -q` | ❌ W0 | ⬜ pending |

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
