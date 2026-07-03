---
phase: 64
slug: per-module-coverage-uplift-gate-raise
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-07-02
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
| 64-01-T1 | 64-01 | 1 | COV-01 / COV-02 | T-64-01 | Floor script fails closed on missing/empty coverage.json | unit (script) | `uv run ruff check scripts/coverage_floor.py && uv run mypy scripts/coverage_floor.py` | ❌ W0 new | ⬜ pending |
| 64-01-T2 | 64-01 | 1 | COV-02 | T-64-01 | Fail-closed + sub-floor + exempt + zero-stmt exit contract | unit | `uv run pytest tests/shared/test_coverage_floor.py -q` | ❌ W0 new | ⬜ pending |
| 64-02-T1 | 64-02 | 1 | COV-01 | T-64-04 | Degrade branches return [] AND emit named warning (D-07) | unit (service) | `uv run pytest tests/review/services/test_review_degrade.py -q` | ❌ W0 new | ⬜ pending |
| 64-02-T2 | 64-02 | 1 | COV-01 | T-64-04 | classify_compute_lanes returns (IDLE,0) on SQLAlchemyError | unit (service) | `uv run pytest tests/agents/services/test_agent_liveness.py -q` | ✅ extend | ⬜ pending |
| 64-03-T1 | 64-03 | 2 | COV-02 | T-64-02 | Two gate sites equal & > 90.38; floor wired into recipe | guard | `uv run python -c "import tomllib,pathlib,re; p=tomllib.loads(pathlib.Path('pyproject.toml').read_text())['tool']['coverage']['report']['fail_under']; j=int(re.search(r'--fail-under=(\\d+)', pathlib.Path('justfile').read_text()).group(1)); assert p==j and p>90.38"` | ✏️ modified | ⬜ pending |
| 64-03-T2 | 64-03 | 2 | COV-02 | T-64-02 | Drift tripwire + floor-wiring presence assertion | guard | `uv run pytest tests/shared/test_coverage_gate.py -q` | ❌ W0 new | ⬜ pending |
| 64-04-T1 | 64-04 | 3 | COV-02 | T-64-07 | Read main-branch required status checks | auto | `gh api repos/{owner}/{repo}/branches/main/protection/required_status_checks` | — read-only | ⬜ pending |
| 64-04-T2 | 64-04 | 3 | COV-02 | T-64-07 | Red coverage-combine blocks merge (fail-closed policy) | manual (checkpoint) | N/A — human-verify branch protection | — | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**Nyquist coverage:** Every code-producing task has an `<automated>` verify. The only non-automated task
is 64-04-T2, a `checkpoint:human-verify` for GitHub branch-protection settings (genuinely not automatable
from here — repo-admin action); its automatable half (reading the required checks) is 64-04-T1. No run of
3 consecutive tasks lacks an automated verify.

---

## Wave 0 Requirements

- [ ] `scripts/coverage_floor.py` (64-01-T1) — the per-module floor check that the gate wires to (COV-01/COV-02 enforcement seam)
- [ ] `tests/shared/test_coverage_floor.py` (64-01-T2) — unit-tests the floor script incl. fail-closed
- [ ] `tests/review/services/test_review_degrade.py` (64-02-T1) — behavior-asserting review.py uplift (the only sub-floor module)
- [ ] `tests/shared/test_coverage_gate.py` (64-03-T2) — gate-consistency guard
- [ ] Existing infra (pytest + coverage config + `just coverage-combine`) covers the rest — no new framework install

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
