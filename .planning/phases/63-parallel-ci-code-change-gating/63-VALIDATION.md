---
phase: 63
slug: parallel-ci-code-change-gating
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-02
---

# Phase 63 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> This phase's "product" is the CI pipeline + test partition itself, so validation is
> partly meta: tests that assert the partition is complete, plus the CI workflow behaving
> correctly on code-only vs docs-only changes.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (asyncio_mode=auto), pytest-cov, **pytest-xdist (Wave 0 install)** |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`, `[tool.coverage.*]`) |
| **Quick run command** | `just test-bucket <name>` (per-bucket, Wave 0 recipe) |
| **Full suite command** | `just test-ci` (whole suite + coverage.xml) |
| **Estimated runtime** | full suite ~single-runner serial today; target: matrix wall-clock materially lower |

---

## Sampling Rate

- **After every task commit:** Run the affected `just test-bucket <name>` (or `uv run pytest tests/<bucket>/`)
- **After every plan wave:** Run `just test-ci` (full suite, confirms no test lost in reorg)
- **Before `/gsd:verify-work`:** Full suite green AND combined-coverage combine step reproduced locally
- **Max feedback latency:** ~per-bucket seconds; full suite minutes

---

## Validation Architecture (source: 63-RESEARCH.md)

Key seams to validate — these are what make CI-01/CI-03 *trustworthy*:

1. **Partition completeness (CI-01).** A guard test asserts every collected `test_*.py` lives
   under exactly one known `tests/<bucket>/` dir — no test in zero buckets (silently dropped
   from coverage) and none double-counted. With directory buckets this is a path assertion.
2. **Reorg preserves collection (no behavior change).** Full-suite pass count BEFORE reorg ==
   pass count AFTER reorg. Watch the two research-flagged hazards: (a) basename collisions when
   flattening `test_fingerprint.py`/`test_pipeline.py` from multiple dirs; (b) the 13
   `from tests.test_migrations.conftest import …` sites.
3. **Coverage combine correctness (CI-03).** `coverage combine` of per-bucket `.coverage.<bucket>`
   artifacts → one `coverage.xml`; combined total == whole-suite total (no per-shard loss, no
   double count); `coverage report --fail-under=85` enforced on the combined number.
   Keep `concurrency=["greenlet","thread"]` (do NOT add `multiprocessing`); set `relative_files=true`.
4. **skip-with-success (CI-04).** Docs-only PR → heavy jobs skipped, stable aggregate check still
   reports SUCCESS (mergeable); code PR → full pipeline runs. Regression tests over the
   `detect-changes` classifier (md, `.planning/**`, LICENSE → skip; any source file → run).
5. **xdist safety (D-01 revised).** DB-free buckets run `-n auto`; DB buckets run serial (shared
   `phaze_test` race). Validate a DB bucket does NOT get `-n auto`.

---

## Wave 0 Requirements

- [ ] `uv add --dev "pytest-xdist>=3.8.0"` — verify it clears the 7-day `exclude-newer` cooldown at lock time
- [ ] `just test-bucket <name>` recipe + `just coverage-combine` recipe (D-10)
- [ ] Partition-guard test file (asserts collection ⊆ known bucket dirs)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Branch-protection required-check contract | CI-04 | GitHub repo setting, not code | After merge, set the required status check to the stable aggregate/combine job (not per-bucket matrix jobs); confirm a docs-only PR stays mergeable |
| "Materially faster" wall-clock (CI-02) | CI-02 | Depends on live GH runner timing | Compare Actions run duration before vs after on a representative code PR; record the delta |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (pytest-xdist, bucket recipes, partition guard)
- [ ] No watch-mode flags
- [ ] Feedback latency acceptable
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
