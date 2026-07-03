---
phase: 64
slug: per-module-coverage-uplift-gate-raise
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-02
---

# Phase 64 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

This phase adds a per-module coverage floor gate + raised global gate. It is CI/tooling-only:
no runtime code, no network endpoints, no auth surface, and (verified) zero `src/phaze/**` and
zero dependency changes. The threat surface is entirely "can the gate silently fail open".

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| CI runner → floor script | `scripts/coverage_floor.py` reads `coverage.json`, a locally CI-generated artifact | Trusted coverage report (no external/untrusted input) |
| test → service under test | Uplift tests inject raising stubs into review/agent_liveness services | Test-only; no runtime behavior change |
| PR merge → required status check | Repo ruleset `aggregate-results` is the enforcement point; a red combine must block merge | Merge policy decision |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-64-01 | Tampering/Repudiation | `scripts/coverage_floor.py` on missing/empty coverage.json | mitigate | Fail-closed: missing → `FileNotFoundError`; empty-string/unparseable → `JSONDecodeError`; empty `{"files":{}}` → explicit `if not files: return 1` guard (coverage_floor.py:43-46, WR-01 fix commit c5f9bff). No try/except returns 0. | closed |
| T-64-02 | Repudiation/Tampering | gate misconfig / drift → false-green | mitigate | `test_coverage_gate.py` asserts pyproject `fail_under` == justfile `--fail-under`, both > 90.38, and `>= 95` pinned (WR-02), plus recipe keeps `coverage json` + `scripts/coverage_floor.py`. | closed |
| T-64-03 | Info Disclosure / Input Validation | `coverage.json` parse | accept | Trusted CI artifact; script imports only stdlib `json`/`sys`/`pathlib` — no `eval`/`exec`/`subprocess`/network on parsed data. | closed |
| T-64-04 | Tampering (test quality) | uplift tests could coverage-pad (D-07) | mitigate | Every uplift test asserts an observable outcome: `[]` + named `*_degraded` caplog key, exact formatter returns, `("IDLE", 0)` tuple. No "assert no exception". | closed |
| T-64-05 | Tampering (behavior change) | `src/phaze` under no-behavior-change milestone | accept | `git diff --name-only 7e0c95f..HEAD -- 'src/phaze/**'` is EMPTY (verified). Test-only phase. | closed |
| T-64-06 | DoS (brittle gate) | gate set too high blocks unrelated PRs | accept | Integer 95 pin vs measured combined 97.12% → ~2pt headroom. Deliberate margin. | closed |
| T-64-07 | Repudiation/Elevation | branch protection not gating combine → fail-open at policy layer | mitigate | Live ruleset `aggregate-results` (id 18454947) active, targets `~DEFAULT_BRANCH`, requires the `aggregate-results` check; CI chain floor→combine→test→aggregate-results (deny-list) verified. | closed |
| T-64-SC | Tampering (supply chain) | new dependencies | mitigate | Stdlib-only script; pyproject diff is `fail_under 85→95` ONLY — zero dependency lines changed (verified). Nothing to vet under the 7-day cooldown. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-64-03 | T-64-03 | `coverage.json` is a trusted, locally CI-generated artifact — not external input. Floor script performs no `eval`/`exec`/`subprocess`/network on parsed data (stdlib `json` parse + numeric comparison only). No injection surface. | Phase 64 plan (PLAN-time disposition) | 2026-07-02 |
| AR-64-05 | T-64-05 | Milestone "no backend behavior change" preserved by construction — verified empty `src/phaze/**` diff for the phase. Test-only degrade stubs; no D-08 seam needed. | Phase 64 plan (PLAN-time disposition) | 2026-07-02 |
| AR-64-06 | T-64-06 | Global gate pinned to integer 95 against a measured combined 97.12% overall — ~2pt deliberate headroom so unrelated future PRs are not brittle-blocked; integer avoids float `precision` edges. | Phase 64 plan (PLAN-time disposition) | 2026-07-02 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-02 | 8 | 8 | 0 | gsd-security-auditor |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-02
