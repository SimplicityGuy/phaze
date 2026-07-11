---
phase: 82
slug: counts-pending-set-cutover
status: verified
nyquist_compliant: false
wave_0_complete: true
created: 2026-07-10
updated: 2026-07-10
---

# Phase 82 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) — `uv run pytest` |
| **Config file** | pyproject.toml (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/integration/test_stage_status_equivalence.py tests/integration/test_enrich_pending_independence.py -q` |
| **Full suite command** | `uv run pytest` (or per-bucket `just test-bucket <bucket>`) |
| **Estimated runtime** | ~30–90 s (quick) / several min (full) |

> Test-DB env: Postgres 5433 / Redis 6380 (`just test-db`); export both `TEST_DATABASE_URL` and `MIGRATIONS_TEST_DATABASE_URL` (port footgun). PERF-02 bench runs on a local synthetic-seed DB at migration HEAD, not the full suite. Full-suite runs can flake under colima VM pressure — re-run the failed subset in isolation to confirm infra-not-regression.

---

## Sampling Rate

- **After every task commit:** Run the quick command (the DERIV-04 equivalence test + pipeline-service tests)
- **After every plan wave:** Run `just test-bucket pipeline` (or full suite)
- **Before `/gsd:verify-work`:** Full suite green + the PERF-02 benchmark recorded in VERIFICATION
- **Max feedback latency:** ~90 s

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 82-01-* | 01 | 1 | READ-01 | — | `eligible_clause(stage)` SQL == Python `eligible()` for every stage across the fixture matrix (incl. the load-bearing `(ANALYZE, seed_analysis_failed, False)` ELIG-03 cell) | integration (DERIV-04 harness, extended) | `uv run pytest tests/integration/test_stage_status_equivalence.py -q` | ✅ | ✅ green (50 pass; ELIG-03 mutation-proven) |
| 82-02-* | 02 | 2 | READ-01 | T-82 double-dispatch | A single file completes all 3 enrich stages in ANY order (6 permutations) + RED against pre-cutover deadlock; a `PUSHING`/`PUSHED`/`AWAITING` file is ABSENT from the analyze pending set (A1); dedup-resolved files absent from all 3 pending sets | integration (all-orderings + divergence guard) | `uv run pytest tests/integration/test_enrich_pending_independence.py tests/integration/test_pending_set_divergence.py -q` | ✅ | ✅ green |
| 82-02-* | 02 | 2 | READ-01 | — | Anti-drift source scan: no `FileState.*` in a READ position in the 3 pending helpers (dual-write `.state=` writes allowed); AST-based (positional + splat `.where()` aware); mutation-tested both directions | unit (AST source scan) | `uv run pytest tests/shared/test_pending_set_source_scan.py -q` | ✅ | ✅ green (mutation-proven RED-on-break by security auditor) |
| 82-03-* | 03 | 3 | READ-02 | — | `get_stage_progress` enrich nodes return `{not_started,in_flight,done,failed,total}` summing to total; `get_pipeline_stats` deleted (no `GROUP BY FileRecord.state`); all callers re-expressed; `_safe_count` degrade preserved; both pre-existing `test_pipeline.py` files collect + pass | integration | `uv run pytest tests/integration/test_stage_progress_buckets.py tests/shared/routers/test_pipeline_stats.py tests/shared/services/test_pipeline.py -q` | ✅ | ✅ green (`def get_pipeline_stats` absent from `src/`) |
| 82-04-* | 04 | 4 | PERF-02 | — | `/pipeline/stats` at 200K synthetic corpus at HEAD; EXPLAIN ANALYZE index-scan evidence; number recorded in VERIFICATION; DENORM-01 go/no-go gated on it | bench (executed + recorded) | `just perf-db-up` → `just perf-seed` → `just perf-explain` (`scripts/seed_perf_corpus.py` + `scripts/perf_explain.py`) | ✅ | ✅ executed — real p50 ~1290ms direct / ~1405ms endpoint recorded in 82-VERIFICATION; DENORM-01 NO-GO/deferred. NOT a recurring CI gate (one-time measurement). |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

> **Post-execution audit (2026-07-10):** all planned Wave-0 automated tests were created and are green — no MISSING gaps required the nyquist-auditor. Confirmed on an isolated DB: READ-01 surface 57 passed, READ-02 surface green; both anti-drift guards mutation-proven (independently re-verified by the phase security audit). The PERF-02 200K bench was executed and its numbers recorded in VERIFICATION (it is a one-time measurement harness, not a hermetic CI gate). One requirement item (D-02 live-prod invariant) remains deployment-gated Manual-Only.

---

## Wave 0 Requirements

- [x] Extend `tests/integration/test_stage_status_equivalence.py` — added the `ELIGIBLE_CASES`/`eligible_clause` matrix (DERIV-04 additive extension)
- [x] `tests/integration/test_enrich_pending_independence.py` — the SC#1 all-orderings test + A1 PUSHING/PUSHED-absent regression + dedup-exclusion assertions
- [x] `tests/integration/test_pending_set_divergence.py` — behavioral divergence guard on an inconsistent corpus (mirror Phase 84 `test_dedup_divergence.py`)
- [x] `tests/shared/test_pending_set_source_scan.py` — the mutation-tested AST source scan (mirror Phase 84 `test_dedup_fingerprint_source_scan.py`)
- [x] `tests/integration/test_stage_progress_buckets.py` + `tests/shared/routers/test_pipeline_stats.py` — four-bucket-sums-to-total + consumer re-expression coverage
- [x] Modify `tests/shared/services/test_pipeline.py` + `tests/shared/routers/test_pipeline.py` — removed the `get_pipeline_stats` import + stale state-gated tests, repointed degrade canaries, audited dashboard tests
- [x] `scripts/seed_perf_corpus.py` + `scripts/perf_explain.py` + `just perf-*` recipes — the 200K synthetic-seed + EXPLAIN/bench harness

> Additional post-execution reconciliation (not a Wave-0 item, caught by the execute-phase regression gate): `tests/analyze/core/test_stage_progress.py` — two prior-phase tests updated to seed `analysis_completed_at` so they assert the canonical `done_clause` (analysis_completed_at IS NOT NULL) instead of the old drifted "any analysis row" semantics.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| 200K poll-latency measurement | PERF-02 | Requires a seeded ~200K local corpus at migration HEAD + EXPLAIN ANALYZE; not part of the hermetic unit suite | `just test-db` → migrate to HEAD → `uv run python scripts/seed_perf_corpus.py --n 200000` → run the bench recipe → paste the endpoint timing + EXPLAIN ANALYZE index-scan evidence into VERIFICATION |
| Deploy ≥036 + zero `analyzed`-NULL guard (D-02) | READ-01 | Live/shadow assertion against the deploy target; CI can't see prod rows | Read-only lux probe: `COUNT(files WHERE state='analyzed' AND analysis_completed_at IS NULL AND failed_at IS NULL) = 0`; confirm Alembic head ≥ 036 |

---

## Validation Audit 2026-07-10

| Metric | Count |
|--------|-------|
| Requirement rows audited | 5 |
| COVERED (automated green) | 4 (READ-01 ×3, READ-02 ×1) |
| Bench executed + recorded | 1 (PERF-02 200K measurement) |
| MISSING (auditor needed) | 0 |
| Manual-only (deployment-gated) | 1 (D-02 live-prod invariant) |

All planned automated tests exist and pass — no gaps required the nyquist-auditor. Verdict: **VALIDATED (PARTIAL)** — full automated coverage for all CI-testable behaviors; the PERF-02 200K measurement is a one-time bench (executed + recorded, not a recurring gate) and D-02 is a live-prod invariant that cannot be exercised in CI.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies (all Wave-0 tests created + green)
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (0 remained)
- [x] No watch-mode flags
- [x] Feedback latency < 90 s
- [ ] `nyquist_compliant: true` — held false: 1 deployment-gated manual-only (D-02) + PERF-02 one-time bench are inherently non-CI-automatable; all automatable behaviors are COVERED

**Approval:** validated (partial) 2026-07-10 — automated coverage complete; 1 manual-only (deployment-gated) + 1 executed-bench documented
