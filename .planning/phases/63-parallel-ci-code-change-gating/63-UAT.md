---
status: partial
phase: 63-parallel-ci-code-change-gating
source:
  - 63-01-SUMMARY.md
  - 63-02-SUMMARY.md
  - 63-03-SUMMARY.md
  - 63-04-SUMMARY.md
started: 2026-07-02T22:00:00Z
updated: 2026-07-02T22:15:00Z
---

## Current Test

[testing complete]

## Tests

<!--
This is a CI-infrastructure phase (no interactive UI). "Operator-observable" deliverables
were driven by the orchestrator against the ephemeral test DB (`just test-db`, PG 5433 /
Redis 6380) and by static workflow inspection — not by manual clicking. Two items are
inherently GH-live (a real Actions run + a repo-admin setting) and are marked blocked.
-->

### 1. CI-01 — Test suite partitioned into 9 buckets, guarded
expected: `uv run pytest --collect-only` collects every test across `tests/<bucket>/`; the partition guard fails if any test escapes a bucket.
result: pass
evidence: "2586 tests collected; tests/shared/test_partition_guard.py → 3 passed (reads buckets.json, both-glob, non-vacuous meta)."

### 2. Cold-start smoke — deps resolve from lock, xdist importable
expected: A fresh `uv sync --frozen` succeeds and `import xdist` works (pytest-xdist landed in the dev group without breaking the lock).
result: pass
evidence: "`uv sync --frozen` OK; `python -c 'import xdist'` OK."

### 3. CI-02/CI-03 — `just test-bucket <name>` exits 0 and writes a coverage shard
expected: Running a single bucket exits 0 (does NOT fail on its own partial coverage) and writes `.coverage.<bucket>` for the combine step.
result: pass
evidence: "`just test-bucket metadata` exit 0 (67 passed); `just test-bucket fingerprint` exit 0 (78 passed); `.coverage.metadata` + `.coverage.fingerprint` written."

### 4. CI-03 — `coverage combine` unions shards; 85% gate enforced only on the combined number
expected: `just coverage-combine` unions per-bucket shards into one `coverage.xml` and runs `coverage report --fail-under=85` on the combined total (not per-bucket).
result: pass
evidence: "Combined 2 files → 34.97% (higher than either bucket alone, proving real relative_files union); gate fired at combine (`total of 34.97 is less than fail-under=85` — correct with only 2/9 buckets); coverage.xml written."

### 5. CI-03 — Single Codecov upload, token scoped to the combine job
expected: `CODECOV_TOKEN` appears only in the `combine` job of tests.yml; zero occurrences in any matrix leg (no per-leg uploads).
result: pass
evidence: "`grep -c CODECOV_TOKEN tests.yml` = 1 (combine job only); wiring guard `test_codecov_token_is_confined_to_the_combine_job` green."

### 6. CI-02 — Matrix wired to the canonical bucket list
expected: The `test` matrix derives buckets via `fromJSON(needs.setup.outputs.buckets)` (setup reads `tests/buckets.json`), with `fail-fast: false` — no hardcoded inline list that could drift.
result: pass
evidence: "tests.yml uses `fromJSON`; wiring guard `test_matrix_bucket_list_is_derived_via_fromjson_not_hardcoded` + `test_setup_job_reads_the_canonical_buckets_json` green."

### 7. CI-04 — Doc-only classifier is conservative and fail-safe
expected: docs-only paths → `code-changed=false` (skip); any code path → `true`; mixed doc+code → `true`; empty diff → `true` (fail-safe).
result: pass
evidence: "Live classifier: docs-only→false, code→true, mixed→true, empty→true; tests/shared/test_change_gate.py → 11 passed."

### 8. CR-01 hardening — `aggregate-results` is a fail-closed deny-list
expected: The required check fails unless `detect-changes` and `quality` are `success` and (on a code change) every gated job is `success` — a failed/cancelled `detect-changes` can no longer cascade to a green check.
result: pass
evidence: "ci.yml aggregate-results asserts DETECT_RESULT/QUALITY_RESULT `!= success` → exit 1, and per-leg `did not succeed` → exit 1 (deny-list); docs-only skip-with-success path preserved."

### 9. Regression guard — CI matrix/combine wiring is locked by a test
expected: The structural guard added during validation asserts the fragile invariants (esp. `--cov-fail-under=0` deferral) so the CI-02/CI-03 wiring can't silently regress.
result: pass
evidence: "tests/shared/test_ci_workflow_wiring.py → 6 passed."

### 10. CI-02 — Materially lower wall-clock on a real GitHub runner
expected: On a representative code PR, the parallel bucket matrix finishes materially faster (wall-clock) than the old single serial job.
result: blocked
blocked_by: other
reason: "Requires a live GitHub Actions run on an opened PR to measure runner wall-clock; cannot be measured locally. Unblocks when the Phase 63 PR runs CI. (Already listed as Manual-Only in 63-VALIDATION.md.)"

### 11. CI-04 — Branch-protection required check stays satisfiable on doc-only PRs
expected: With the required status check set to the stable `aggregate-results` job, a docs-only PR reports SUCCESS and stays mergeable (skip-with-success, not skip-absent).
result: blocked
blocked_by: other
reason: "Requires the GitHub repo-admin branch-protection setting (required check = aggregate-results) plus a live docs-only PR to confirm mergeability. Repo configuration, not code. (Already listed as Manual-Only in 63-VALIDATION.md.)"

## Summary

total: 11
passed: 9
issues: 0
pending: 0
skipped: 0
blocked: 2

## Gaps

[none — 0 issues; the 2 blocked items are deployment/GH-live gated, not code defects]
