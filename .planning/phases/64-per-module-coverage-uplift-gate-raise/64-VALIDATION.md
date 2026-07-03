---
phase: 64
slug: per-module-coverage-uplift-gate-raise
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-02
---

# Phase 64 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio, pytest-cov) via `uv run` |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`, `[tool.coverage.*]`) |
| **Quick run command** | `just test-file tests/<bucket>/<new_test>.py` (single new test file) |
| **Full suite command** | `just coverage-combine` (after per-bucket shards) — enforces global gate + new per-module floor |
| **Estimated runtime** | ~single test <30s; full combined suite ~minutes (2566 tests + DB) |

---

## Sampling Rate

- **After every task commit:** Run the new/affected test file(s) via `uv run pytest <file>`
- **After every plan wave:** Run the combined coverage gate (`just coverage-combine`) so both the global gate and the per-module floor are exercised
- **Before `/gsd:verify-work`:** Full combined suite must be green AND `scripts/coverage_floor.py` must pass
- **Max feedback latency:** ~30s per-file; full-suite once per wave

---

## Per-Task Verification Map

*Populated by the planner/Nyquist pass during planning — one row per task, each mapped to COV-01 or COV-02 with an automated command.*

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | — | — | COV-01 / COV-02 | — | N/A | unit | `TBD` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `scripts/coverage_floor.py` — the per-module floor check (parses `coverage json`) that the gate wires to (COV-01/COV-02 enforcement seam)
- [ ] New behavior-asserting tests for the sub-floor module(s) (`services/review.py`, plus any 85–90% margin modules planned) under the Phase-63 `tests/<bucket>/` layout
- [ ] Existing infrastructure (pytest + coverage config + `just coverage-combine`) covers the rest — no new framework install

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Raised gate surfaces as a red required check on regression | COV-02 | Requires a live CI run / branch-protection wiring (deferred Phase 63 chore) | Confirm the `combine` job's floor+gate failure fails the required status check on a PR; verify branch protection points at `aggregate-results` |

*Automatable portions (floor script exits non-zero below threshold; `coverage report --fail-under` fails below gate) are covered by unit tests.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s (per-file)
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
