---
phase: 71
slug: deployment-config-docs-n-lane-ui
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-04
---

# Phase 71 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio, httpx AsyncClient / TestClient) |
| **Config file** | `pyproject.toml` ([tool.pytest.ini_options]) |
| **Quick run command** | `uv run pytest tests/<touched>/ -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~full-suite (bucketed in CI); quick subset ~seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/<touched>/ -q`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 60 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| _(populated by planner)_ | | | BEUI-01/02/03 | | | | | | ⬜ pending |

---

## Wave 0 Requirements

*Existing infrastructure covers all phase requirements (pytest + fresh-DB fixtures + httpx AsyncClient + template-render assertions already present). Planner confirms per-task.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| _(populated by planner — e.g. live 2+-lane render + force-local toggle visual on local uvicorn + fresh phaze_uat DB)_ | BEUI-01/02 | Visual/perception | Boot app, load backends.toml with N backends, view Analyze lanes + header toggle |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
