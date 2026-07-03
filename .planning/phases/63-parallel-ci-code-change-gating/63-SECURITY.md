---
phase: 63
slug: parallel-ci-code-change-gating
status: secured
threats_total: 17
threats_closed: 17
threats_open: 0
asvs_level: 1
register_authored_at_plan_time: true
created: 2026-07-02
---

# Phase 63 — Parallel CI & Code-Change Gating: Security Audit

**Audited:** 2026-07-02
**ASVS Level:** L1 (default)
**Disposition source:** plan-time threat register (17 threats, 4 plans) — verified, not re-derived
**Result:** SECURED — 17/17 threats closed

Every declared mitigation was confirmed present in the implemented code by grep/read
(documentation and intent were not accepted as evidence). Two plan-time mitigations were
strengthened during the code-review/verification gates (CR-01, WR-01); the stronger,
current forms are what was verified below.

---

## Threat Verification

### Plan 63-01 — pyproject.toml, uv.lock, justfile, tests/buckets.json

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| T-63-01-SC | Tampering | mitigate | CLOSED | `pytest-xdist>=3.8.0` pinned (`pyproject.toml:225`); supply-chain cooldown `exclude-newer = "7 days"` active with no override (`pyproject.toml:198`); blocking package-legitimacy checkpoint (pytest-dev provenance, 3.8.0 ~12mo old) recorded in 63-01-SUMMARY Task 1. |
| T-63-01-02 | Tampering | mitigate | CLOSED | `concurrency = ["greenlet", "thread"]` (`pyproject.toml:73`); grep confirms NO `multiprocessing` anywhere in pyproject — shard data cannot be mis-merged. |
| T-63-01-03 | Repudiation | accept | CLOSED | `fail_under = 85` unchanged (`pyproject.toml:68`); the combined coverage number remains the single auditable gate (enforced once in `coverage-combine`, `justfile:110`). Accepted risk holds. |

### Plan 63-02 — tests/** reorg, tests/BUCKETS.md, tests/shared/test_partition_guard.py

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| T-63-02-01 | Tampering | mitigate | CLOSED | Partition guard fails CI on any test escaping a bucket, globbing BOTH `test_*.py` and `*_test.py` (`tests/shared/test_partition_guard.py:46,79-87`) with a non-vacuous meta-test (`:90-97`); pre/post pass-count equality baseline **2566 passed** recorded (`tests/BUCKETS.md:12`); collision-free `<bucket>/<layer>/<basename>` layout. |
| T-63-02-02 | DoS (flaky) | accept | CLOSED | DB buckets stay serial — `test-bucket` defaults `XDIST=""` (`justfile:102-103`); `tests.yml:97-104` rationale confirms every bucket has >0 integration-marked tests so none get `-n auto`; fixtures untouched. Accepted race-avoidance holds. |
| T-63-02-03 | Repudiation | mitigate | CLOSED | Combined pre-reorg coverage baseline recorded (**96.89%**, `tests/BUCKETS.md:12`) as the drift-detection reference for the combined number. |
| T-63-02-SC | Tampering | accept | CLOSED | No packages added by this plan (63-02 `tech-stack.added: []`); reorg is `git mv` only. |

### Plan 63-03 — .github/workflows/tests.yml

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| T-63-03-01 | Info Disclosure | mitigate | CLOSED | `CODECOV_TOKEN` appears exactly once, in the `combine` job env (`tests.yml:154`); grep confirms ZERO occurrences in the `setup` or `test` matrix legs (`test:` job spans lines 28–112, token is at 154 inside `combine:`). |
| T-63-03-02 | Tampering | mitigate | CLOSED | Every third-party `uses:` is pinned to a 40-char SHA (grep for non-SHA `uses:` returns only in-repo `./.github/workflows/*` reusable workflows + one local `./.github/actions/*` composite — not third-party). actionlint + check-jsonschema enforce. |
| T-63-03-03 | EoP | mitigate | CLOSED | `ci.yml` keeps `pull_request` (`ci.yml:14`); grep confirms `pull_request_target` appears NOWHERE in `.github/workflows/` — fork PRs run without secret exposure. |
| T-63-03-04 | Info Disclosure | accept | CLOSED | `.coverage.<bucket>` artifacts contain source paths + line hit-counts only, no secrets/PII; internal to the run (uploaded via `upload-artifact`, consumed by `combine`). Accepted risk holds. |
| T-63-03-SC | Tampering | accept | CLOSED | No packages added by this plan (63-03 `tech-stack.added: []`). |

### Plan 63-04 — .github/workflows/ci.yml, scripts/classify-changed-files.sh, justfile, tests/shared/test_change_gate.py

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| T-63-04-01 | Tampering / EoP | mitigate | CLOSED | Conservative keep-only-non-doc classifier — `grep -vE '(\.md$\|^\.planning/\|^LICENSE$\|^docs/\|\.txt$)'` (`classify-changed-files.sh:51`); any non-doc path ⇒ `code-changed=true` (`:56-57`). Dedicated positive test on a mixed doc+code list (`test_change_gate.py:76-83`). Further strengthened by CR-01 deny-list (below) — a bypass can no longer ride a broken gate to green. |
| T-63-04-02 | DoS (unmergeable) | mitigate | CLOSED (strengthened) | `aggregate-results` has `if: always()` (`ci.yml:128`); docs-only skip-with-success `exit 0` path preserved (`ci.yml:169-179`); grep confirms NO `paths-ignore`. **CR-01 deny-list (commit b24853a) verified present:** `DETECT_RESULT`/`QUALITY_RESULT` must equal `"success"` (`ci.yml:156-165`), and on a code change every gated job (Tests/Security/Docker) must equal `"success"` (`ci.yml:181-189`), rejecting `failure`/`cancelled`/`skipped`. Closes the prior fail-open (allow-list of only `"failure"`). |
| T-63-04-03 | Tampering | mitigate | CLOSED | Classifier is a versioned, shellcheck-clean (`set -euo pipefail`) script (`scripts/classify-changed-files.sh`) under regression tests (`tests/shared/test_change_gate.py`, 11 cases) — not opaque inline YAML. |
| T-63-04-04 | EoP | mitigate | CLOSED | `ci.yml` triggers/permissions unchanged — `pull_request` (not `pull_request_target`), verified by grep. |
| T-63-04-SC | Tampering | accept | CLOSED | No packages added by this plan (63-04 `tech-stack.added: []`). |

---

## Post-Plan Hardening — Verified Present (not flagged as drift)

Two mitigations were strengthened after execution during the review/verification gates
(operator-approved; 63-REVIEW.md `status: resolved`, 63-VERIFICATION.md `status: passed`).
The current, stronger forms are confirmed in code:

1. **CR-01 (commit b24853a) — aggregate-results deny-list.** Original allow-listed only the
   value `"failure"`, so a failed/cancelled `detect-changes` cascaded to skipped gated legs and
   the required check went green with nothing run. Now a deny-list: `detect-changes` + `quality`
   must be `success`; on a code change every gated job must be `success`; the docs-only
   skip-with-success path accepts `skipped`/`success` and rejects `failure`/`cancelled`.
   Verified at `ci.yml:148-191`. Directly strengthens T-63-04-01 and T-63-04-02.

2. **WR-01 (commit b24853a) — empty-input fail-safe.** `classify-changed-files.sh` now treats
   empty/whitespace-only stdin as `code-changed=true` (`:42-47`), not `false`, so a
   spurious-empty diff cannot silently skip CI. Regression tests lock it in
   (`test_change_gate.py:64-67`). Strengthens T-63-04-01 and T-63-04-03.

---

## Unregistered Flags

None. No SUMMARY declares a `## Threat Flags` section (63-01..63-04 use narrative
"Threat Coverage"/"Threat surface" sections mapping only to the registered IDs). No new
attack surface appeared during implementation without a threat mapping.

## Accepted Risks Log

| Threat ID | Category | Rationale (still valid) |
|-----------|----------|-------------------------|
| T-63-01-03 | Repudiation | `fail_under=85` unchanged; combined coverage number is the auditable source of truth, enforced once at combine time. |
| T-63-02-02 | DoS (flaky) | Shared `phaze_test` DB race avoided by keeping DB buckets serial (no `-n auto`); fixtures untouched. |
| T-63-02-SC | Tampering | Plan installs no packages. |
| T-63-03-04 | Info Disclosure | Coverage shard data = source paths + line hit-counts only; no secrets/PII; run-internal. |
| T-63-03-SC | Tampering | Plan installs no packages. |
| T-63-04-SC | Tampering | Plan installs no packages. |

---

_Audited by gsd-security-auditor. Implementation files unmodified (read-only)._
