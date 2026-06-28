---
phase: 54
slug: kube-submit-watch-reconcile-cron
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-27
---

# Phase 54 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio) |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/test_kube_submit.py tests/test_kube_reconcile.py -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_kube_submit.py tests/test_kube_reconcile.py -q`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green (85% coverage min)
- **Max feedback latency:** 60 seconds

---

## Per-Task Verification Map

> Populated by the planner from RESEARCH.md "## Validation Architecture" (17 critical-transition tests mapped to KSUBMIT-01..06). The planner finalizes task IDs.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | — | — | KSUBMIT-01..06 | — | see RESEARCH §Validation Architecture | unit | `uv run pytest tests/test_kube_*.py -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_kube_submit.py` — submit-task + idempotency stubs (KSUBMIT-01, KSUBMIT-02)
- [ ] `tests/test_kube_reconcile.py` — reconcile state-machine stubs (KSUBMIT-03, KSUBMIT-04, KSUBMIT-05)
- [ ] `tests/conftest.py` — fake-kube fixtures (canned status objects + respx kube-REST stub per RESEARCH two-layer strategy)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live Kueue admission against a real cluster | KSUBMIT-04 | Phase is testable against a fake kube API; live admission/RBAC is Phase 56 | Deferred to Phase 56 deploy/runbook |

*All in-scope phase behaviors have automated verification against the fake kube API.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
