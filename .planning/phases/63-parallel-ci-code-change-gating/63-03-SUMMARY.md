---
phase: 63-parallel-ci-code-change-gating
plan: 03
subsystem: ci
tags: [ci, coverage, matrix, codecov, github-actions]
requires:
  - "tests/buckets.json (63-02): canonical 9-bucket list in matrix order"
  - "just test-bucket / just coverage-combine (63-01): per-bucket runner + shard combine recipes"
  - "relative_files=true coverage config (63-01): cross-shard union"
provides:
  - "tests.yml bucket matrix: setup job (fromJSON buckets) -> parallel test legs -> combine job"
  - "single merged coverage.xml + single Codecov upload gated at --fail-under=85"
affects:
  - ".github/workflows/tests.yml"
tech-stack:
  added: []
  patterns:
    - "setup job emits tests/buckets.json via jq into GITHUB_OUTPUT; matrix consumes it with fromJSON (single source of truth, no hardcoded bucket list)"
    - "per-leg upload-artifact with include-hidden-files + if-no-files-found:error for the .coverage.<bucket> dotfile"
    - "download-artifact pattern:coverage-* + merge-multiple:true -> just coverage-combine -> single codecov upload"
    - "CODECOV_TOKEN scoped exclusively to the combine job (info-disclosure mitigation T-63-03-01)"
key-files:
  created: []
  modified:
    - ".github/workflows/tests.yml"
decisions:
  - "63-03: all 9 buckets run serial (no -n auto) — empirically every bucket has >0 integration-marked tests, so xdist would race on the shared phaze_test schema create/drop; matrix fan-out alone delivers CI-02"
  - "63-03: removed matrix.xdist expression entirely (actionlint flags undefined matrix props with no include); a future DB-free bucket would re-add it via matrix.include"
  - "63-03: matrix + combine live INSIDE tests.yml so ci.yml's test: reusable-workflow call and the aggregate-results required check stay untouched"
metrics:
  duration: ~15min
  completed: 2026-07-02
---

# Phase 63 Plan 03: Parallelize tests.yml Summary

Replaced the single serial `Tests` job in `.github/workflows/tests.yml` with a `setup` → bucket-matrix `test` → `combine` topology: buckets fan out in parallel (CI-02) and one combine job merges every per-shard `.coverage.<bucket>` into a single `coverage.xml` for one trustworthy Codecov upload gated at `--fail-under=85` (CI-03).

## What Was Built

**Task 1 — bucket matrix (commit 02860a9):**
- Added a lightweight `setup` job that reads `tests/buckets.json` with `jq -c` into a `buckets` output — the matrix stays driven by the single source of truth (no hardcoded bucket list).
- Replaced the serial `Tests` job with a `test` job (`needs: [setup]`, `strategy.fail-fast: false`, `matrix.bucket: ${{ fromJSON(needs.setup.outputs.buckets) }}`).
- Each leg keeps the postgres:18-alpine + redis:7-alpine service block, the env block, the migrations-DB creation step, and the frozen action SHAs verbatim (D-07).
- The test step delegates to `just test-bucket ${{ matrix.bucket }}` (D-10) and replaces the old `just test-ci`.
- Each leg uploads `.coverage.<bucket>` via `upload-artifact` v7.0.1 with `include-hidden-files: true` (the dotfile is dropped by default) and `if-no-files-found: error` (an empty bucket is a bug).
- The Codecov upload was removed from the leg (moved to the combine job).

**Task 2 — combine job (commit 8cd3e64):**
- New `combine` job (`needs: [test]`) checks out, installs uv/just, downloads every shard with `pattern: coverage-*` + `merge-multiple: true`, runs `just coverage-combine` (coverage combine + xml + report `--fail-under=85`), then does the single Codecov upload (`flags: unittests`, `disable_search: true`, `files: ./coverage.xml`).
- `CODECOV_TOKEN` appears ONLY in the combine job — zero occurrences in any matrix leg (info-disclosure mitigation T-63-03-01, asserted in verify).

## Per-bucket xdist decision (Q-B, empirical)

Ran `uv run pytest tests/<bucket> -m integration --collect-only -q` for all 9 buckets at execution time. Every bucket returned >0 integration-marked (DB-fixture) tests:

| bucket | integration-marked |
|--------|-------------------|
| discovery | 84 |
| metadata | 11 |
| fingerprint | 15 |
| analyze | 101 |
| identify | 116 |
| review | 163 |
| agents | 153 |
| integration | 59 |
| shared | 290 |

No bucket is DB-free, so **no bucket qualifies for `-n auto`** — xdist inside a DB-touching bucket would race on the shared `phaze_test` schema create/drop (D-01/D-05/D-06). All buckets run serial; the GitHub matrix fan-out alone satisfies CI-02 (per research, this is acceptable). A rationale comment on the run step documents how to re-enable `-n auto` (via `matrix.include`) if a future bucket becomes DB-free.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Removed the `${{ matrix.xdist }}` expression from the run step**
- **Found during:** Task 1 (actionlint hook)
- **Issue:** The plan's literal run step `just test-bucket ${{ matrix.bucket }} ${{ matrix.xdist }}` failed actionlint: `property "xdist" is not defined in object type {bucket: any}`. Because no bucket qualified for `-n auto`, there is no `matrix.include` defining `xdist`, so the reference is undefined and blocks the pre-commit gate.
- **Fix:** Dropped the `${{ matrix.xdist }}` arg (the `just test-bucket` recipe defaults `XDIST=""` → serial, which is exactly the intended behavior for all-DB buckets). Added a rationale comment explaining the empirical DB-free check and how to re-add `-n auto` later.
- **Files modified:** `.github/workflows/tests.yml`
- **Commit:** 02860a9

The plan explicitly anticipated this branch: "If NO bucket qualifies, that is acceptable — matrix fan-out alone satisfies CI-02; leave `xdist` empty everywhere." Leaving it empty via omission (rather than an undefined expression) is the actionlint-clean form.

## Verification

- Task 1 automated verify (yaml structure): `matrix ok` — setup+test jobs present, `fail-fast: false`, `fromJSON` matrix, `just test-bucket` present, no `test-ci`, no codecov in leg, `include-hidden-files` + `if-no-files-found` present.
- Task 2 automated verify: `combine ok` — `needs: [test]`, `coverage-*` + `merge-multiple`, `just coverage-combine`, codecov present, `CODECOV_TOKEN` absent from the test job.
- `actionlint` and `check-github-workflows` (check-jsonschema) pre-commit hooks: PASS on the final file.
- `ci.yml` untouched — the `test:` reusable-workflow call and `aggregate-results` required check are unchanged.

## Threat Coverage

- **T-63-03-01 (Info Disclosure — CODECOV_TOKEN):** mitigated — token lives only in the combine job env; zero occurrences in any matrix leg (asserted).
- **T-63-03-02 (Tampering — third-party actions):** mitigated — all `uses:` reuse the existing frozen 40-char SHA pins; actionlint + check-github-workflows enforce.
- **T-63-03-03 (EoP — PR trigger model):** unaffected — this plan did not touch triggers; ci.yml keeps `pull_request` (not `pull_request_target`).

## Self-Check: PASSED
- FOUND: `.github/workflows/tests.yml`
- FOUND: commit 02860a9 (Task 1)
- FOUND: commit 8cd3e64 (Task 2)
