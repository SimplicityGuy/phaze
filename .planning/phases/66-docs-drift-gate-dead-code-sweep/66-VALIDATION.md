---
phase: 66
slug: docs-drift-gate-dead-code-sweep
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-03
updated: 2026-07-03
---

# Phase 66 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/shared/core/test_requirements_traceability.py tests/shared/core/test_dead_template_guard.py` |
| **Full suite command** | `just test-bucket shared` |
| **Estimated runtime** | ~30 seconds (shared bucket) |

---

## Sampling Rate

- **After every task commit:** Run the quick run command (the two guard tests)
- **After every plan wave:** Run `just test-bucket shared`
- **Before `/gsd:verify-work`:** Full suite green + `uv run ruff check .` + `uv run mypy .` + `pre-commit run --all-files`
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 66-01-01 | 01 | 1 | DOCS-01 | T-66-01..04 | Drift gate fails on passed-but-unmarked / marked-but-unpassed / checkbox≠table; in-flight tolerated; archived internal-consistency-only | unit | `uv run pytest tests/shared/core/test_requirements_traceability.py` (5 tests) | ✅ | ✅ green |
| 66-02-01 | 02 | 1 | CLEAN-01 | T-66-05,07 | `/saq` link renders only when `enable_saq_ui` true; `target=_blank rel=noopener`; absent from poll partial | unit | `uv run pytest tests/agents/routers/test_admin_agents.py -k saq_link` (3 tests, needs test-db) | ✅ | ✅ green |
| 66-03-01 | 03 | 1 | CLEAN-02 | T-66-08 | Dead entry-root literal fails guard instead of masking an orphan (D-14) | unit | `uv run pytest tests/shared/core/test_dead_template_guard.py::test_entry_literals_resolve_to_templates` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**Audit note (2026-07-03):** All three requirement tests exist and run green — DOCS-01 + D-14 are hermetic (7 passed, no DB, ~0.13s); the 3 CLEAN-01 tests are DB-backed and passed live (17-passed admin_agents run, verified by both the post-merge gate and the phase verifier). The plan-time `-k saq_link` selector was confirmed to select exactly the 3 CLEAN-01 tests. Zero gaps — no test generation required.

---

## Wave 0 Requirements

- [x] `tests/shared/core/test_requirements_traceability.py` — new drift-gate guard (DOCS-01), 5 drift-class tests; green
- [x] `tests/shared/core/test_dead_template_guard.py` — extended with `test_entry_literals_resolve_to_templates` (CLEAN-02, D-14); green
- [x] `/saq` link render tests (CLEAN-01) in `tests/agents/routers/test_admin_agents.py` — conditional visibility + `target=_blank rel=noopener` + absent-from-partial; green

*Existing pytest infrastructure covered all phase requirements — no framework install needed. All Wave 0 tests delivered and green.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `/saq` link visually discreet on the live Agents page | CLEAN-01 | Visual placement/weight is subjective | Boot app with `enable_saq_ui=true`, open `/admin/agents`, confirm a muted footer link opens `/saq` in a new tab |
| `vulture` sweep removes only confirmed-dead code | CLEAN-02 | Dynamic-reachability judgment per candidate | For each vulture candidate: grep dynamic refs, run full suite green before deleting |

*The drift gate, guard blind-spot fix, and /saq conditional render all have automated verification; only visual discreetness and per-candidate dead-code judgment are manual.*

---

## Validation Audit 2026-07-03

| Metric | Count |
|--------|-------|
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

All 3 requirement→test mappings classified COVERED (test exists, targets behavior, runs green). No MISSING or PARTIAL gaps — no test generation or auditor spawn required. Two manual-only items (visual discreetness of the `/saq` link; per-candidate dead-code reachability judgment) remain manual by design and were both exercised this phase (live UI-render verification + the human-approved deletion-review gate, which found no dead code).

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (all Wave 0 tests delivered green)
- [x] No watch-mode flags
- [x] Feedback latency < 30s (hermetic guards ~0.13s)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** verified 2026-07-03
