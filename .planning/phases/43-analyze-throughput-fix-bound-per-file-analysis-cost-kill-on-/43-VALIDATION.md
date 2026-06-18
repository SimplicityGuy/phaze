---
phase: 43
slug: analyze-throughput-fix
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-17
---

# Phase 43 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Detailed sampling/measurement design lives in `43-RESEARCH.md` (## Validation Architecture).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio |
| **Config file** | pyproject.toml (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60–120 seconds |

---

## Sampling Rate

- **After every task commit:** Run the targeted test module for the touched area (`uv run pytest tests/test_<area>.py -q`)
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing` (≥85% gate)
- **Before `/gsd:verify-work`:** Full suite green + `uv run mypy .` + `uv run ruff check .` clean
- **Max feedback latency:** ~120 seconds

---

## Per-Task Verification Map

> Populated by the planner — each task maps to an automated check. essentia is heavy, so analysis is
> validated at MOCKABLE boundaries (window-list math, pool kill behavior, payload/coverage shapes,
> state transitions, timeout-terminal classification) rather than running real essentia in CI.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| (planner fills) | | | | | | | `uv run pytest …` | | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] Coverage assertions for the cap+even-stride window downsampler (deterministic, no essentia) — pure unit
- [ ] Fake/killable-pool fixture proving a runaway child is SIGKILLed on inner timeout (no real essentia)
- [ ] Fixtures for the new control-API coverage + `analysis-failed` endpoints (httpx AsyncClient)

*If existing infrastructure (tests/conftest.py) covers these fixtures, the planner notes that instead.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real long-set bounded runtime on the homelab | Bound cost | Requires real essentia + a multi-hour file (not in CI) | After redeploy to nox, confirm a known >4h set now completes in minutes and the analyze counter advances |
| Kill-on-timeout reclaims a real pool slot | Kill-on-timeout | Requires real runaway essentia child | After redeploy, confirm worker CPU/slot frees when a job hits the inner timeout |

---

## Validation Sign-Off

- [ ] All tasks have automated verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 120s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
