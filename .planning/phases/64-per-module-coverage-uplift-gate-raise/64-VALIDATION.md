---
phase: 64
slug: per-module-coverage-uplift-gate-raise
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-02
updated: 2026-07-02
---

# Phase 64 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.1.1 (pytest-asyncio 1.4.0 `asyncio_mode=auto`, pytest-cov 7.1.0) via `uv run` |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`, `[tool.coverage.*]`) |
| **Quick run command** | `uv run pytest tests/<bucket>/<new_test>.py -q` (single new test file) |
| **Full suite command** | `just coverage-combine` (after per-bucket shards) — enforces global gate + new per-module floor |
| **Estimated runtime** | ~single test <30s; full combined suite ~minutes (2566 tests + DB, ≈9m under colima) |

---

## Sampling Rate

- **After every task commit:** Run the new/affected test file(s) via `uv run pytest <file>` (isolation-safe: `just test-bucket <bucket>`)
- **After every plan wave:** Run the combined coverage gate (`just coverage-combine`) so both the global gate and the per-module floor are exercised
- **Before `/gsd:verify-work`:** Full combined suite green AND `scripts/coverage_floor.py` passes AND `coverage report --fail-under=<NEW_GLOBAL>` passes
- **Max feedback latency:** ~30s per-file; full-suite once per wave

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 64-01-T1 | 64-01 | 1 | COV-01 / COV-02 | T-64-01 | Floor script fails closed on missing/empty coverage.json | unit (script) | `uv run ruff check scripts/coverage_floor.py && uv run mypy scripts/coverage_floor.py` | ✅ new | ✅ green |
| 64-01-T2 | 64-01 | 1 | COV-02 | T-64-01 | Fail-closed (missing / empty-string / empty-`{"files":{}}` — WR-01) + sub-floor + exempt + zero-stmt exit contract | unit | `uv run pytest tests/shared/test_coverage_floor.py -q` | ✅ new | ✅ green (7 pass) |
| 64-02-T1 | 64-02 | 1 | COV-01 | T-64-04 | Degrade branches return [] AND emit named warning (D-07) | unit (service, DB) | `uv run pytest tests/review/services/test_review_degrade.py -q` | ✅ new | ✅ green (DB up) |
| 64-02-T2 | 64-02 | 1 | COV-01 | T-64-04 | classify_compute_lanes returns (IDLE,0) on SQLAlchemyError | unit (service, DB) | `uv run pytest tests/agents/services/test_agent_liveness.py -q` | ✅ extended | ✅ green (DB up) |
| 64-03-T1 | 64-03 | 2 | COV-02 | T-64-02 | Two gate sites equal & > 90.38 & >= 95 pin; floor wired into recipe | guard | `uv run pytest tests/shared/test_coverage_gate.py -q` | ✅ new | ✅ green |
| 64-03-T2 | 64-03 | 2 | COV-02 | T-64-02 | Drift tripwire + floor-wiring presence assertion | guard | `uv run pytest tests/shared/test_coverage_gate.py -q` | ✅ new | ✅ green |
| 64-04-T1 | 64-04 | 3 | COV-02 | T-64-07 | Read main required checks — via RULESETS API (legacy branch-protection API 404s; see feedback memory) | auto (live read) | `gh api repos/SimplicityGuy/phaze/rulesets` → ruleset detail → `required_status_checks` | ✅ live read | ✅ PASS (`aggregate-results` required) |
| 64-04-T2 | 64-04 | 3 | COV-02 | T-64-07 | Red coverage-combine blocks merge (fail-closed policy) | manual (checkpoint) | N/A — human-verify ruleset required check | — | ✅ approved (PASS via ruleset) |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**Nyquist coverage:** Every code-producing task has an `<automated>` verify. The only non-automated task
is 64-04-T2, a `checkpoint:human-verify` for GitHub branch-protection settings (genuinely not automatable
from here — repo-admin action); its automatable half (reading the required checks) is 64-04-T1. No run of
3 consecutive tasks lacks an automated verify.

---

## Wave 0 Requirements

- [x] `scripts/coverage_floor.py` (64-01-T1) — the per-module floor check that the gate wires to (COV-01/COV-02 enforcement seam)
- [x] `tests/shared/test_coverage_floor.py` (64-01-T2) — unit-tests the floor script incl. fail-closed (7 cases incl. WR-01 empty-dict)
- [x] `tests/review/services/test_review_degrade.py` (64-02-T1) — behavior-asserting review.py uplift (the only sub-floor module)
- [x] `tests/shared/test_coverage_gate.py` (64-03-T2) — gate-consistency guard (equal + >90.38 + >=95 pin + floor wiring)
- [x] Existing infra (pytest + coverage config + `just coverage-combine`) covers the rest — no new framework install

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Raised gate surfaces as a red required check on regression | COV-02 | Requires live CI / branch-protection wiring (deferred Phase 63 chore, RESEARCH A3/Open-Q1) | 64-04: confirm the `combine`/`aggregate-results` job is a required status check on main so a red `just coverage-combine` blocks merge; set it if missing |

*Automatable portions (floor script exits non-zero below threshold; `coverage report --fail-under` fails below gate; gate-site consistency) are covered by unit/guard tests in 64-01 and 64-03.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies (64-04-T2 is the sole sanctioned human-verify)
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (floor script + its test + review test + gate guard)
- [x] No watch-mode flags
- [x] Feedback latency < 30s (per-file)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** ready

---

## Validation Audit 2026-07-02

| Metric | Count |
|--------|-------|
| Requirements audited | 2 (COV-01, COV-02) |
| Tasks mapped | 8 |
| COVERED (automated) | 7 |
| Manual-only (sanctioned) | 1 (64-04-T2 human-verify, its automatable half 64-04-T1 PASSED) |
| Gaps found | 0 |
| Resolved / generated | 0 (all tests authored during execution; none missing) |

**Result:** NYQUIST-COMPLIANT. Every code-producing task has a green automated verify; the sole
manual task (64-04-T2) is a sanctioned GitHub-settings human-verify whose automatable half
(reading the ruleset's required checks, 64-04-T1) PASSED. Post-execution correction: 64-04-T1's
command was moved off the legacy branch-protection API (404s here) to the **rulesets API** —
`aggregate-results` is confirmed as the required, merge-blocking check. DB-free guards re-run green
this session; DB-backed uplift tests (33) confirmed green by both the executor (full CI-faithful
suite) and the phase verifier.
