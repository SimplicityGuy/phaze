---
phase: 66
slug: docs-drift-gate-dead-code-sweep
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-03
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
| 66-01-01 | 01 | 1 | DOCS-01 | — | Drift gate fails on passed-but-unmarked / marked-but-unpassed / checkbox≠table | unit | `uv run pytest tests/shared/core/test_requirements_traceability.py` | ❌ W0 | ⬜ pending |
| 66-02-01 | 02 | 1 | CLEAN-01 | — | `/saq` link renders only when `enable_saq_ui` true; `target=_blank rel=noopener` | unit | `uv run pytest -k saq_link` | ❌ W0 | ⬜ pending |
| 66-03-01 | 03 | 1 | CLEAN-02 | — | Dead entry-root literal fails guard instead of masking an orphan | unit | `uv run pytest tests/shared/core/test_dead_template_guard.py` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/shared/core/test_requirements_traceability.py` — new drift-gate guard (DOCS-01), analog: `test_docs_ia_current.py`
- [ ] `tests/shared/core/test_dead_template_guard.py` — extend with entry-literal-resolves assertion (CLEAN-02, D-14)
- [ ] `/saq` link render test (CLEAN-01) — assert conditional visibility + attributes

*Existing pytest infrastructure covers all phase requirements — no framework install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `/saq` link visually discreet on the live Agents page | CLEAN-01 | Visual placement/weight is subjective | Boot app with `enable_saq_ui=true`, open `/admin/agents`, confirm a muted footer link opens `/saq` in a new tab |
| `vulture` sweep removes only confirmed-dead code | CLEAN-02 | Dynamic-reachability judgment per candidate | For each vulture candidate: grep dynamic refs, run full suite green before deleting |

*The drift gate, guard blind-spot fix, and /saq conditional render all have automated verification; only visual discreetness and per-candidate dead-code judgment are manual.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
