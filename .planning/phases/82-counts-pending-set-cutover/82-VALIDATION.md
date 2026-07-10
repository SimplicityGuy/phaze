---
phase: 82
slug: counts-pending-set-cutover
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-10
---

# Phase 82 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) — `uv run pytest` |
| **Config file** | pyproject.toml (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/pipeline/services/test_stage_status_equivalence.py tests/pipeline/services -q` |
| **Full suite command** | `uv run pytest` (or per-bucket `just test-bucket pipeline`) |
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
| 82-01-* | 01 | 1 | READ-01 | — | `eligible_clause(stage)` SQL == Python `eligible()` for every stage across the fixture matrix (incl. the load-bearing `(ANALYZE, analysis_failed, ineligible)` ELIG-03 cell) | integration (DERIV-04 harness) | `uv run pytest tests/pipeline/services/test_stage_status_equivalence.py -q` | ❌ W0 (extend) | ⬜ pending |
| 82-02-* | 02 | 2 | READ-01 | T-82 double-dispatch | A single file completes all 3 enrich stages in ANY order; a `PUSHING`/`PUSHED` file is ABSENT from the analyze pending set (A1); dedup-resolved files absent from all 3 pending sets | integration (all-orderings + divergence guard) | `uv run pytest tests/pipeline/services/test_pending_sets.py -q` | ❌ W0 | ⬜ pending |
| 82-02-* | 02 | 2 | READ-01 | — | Anti-drift source scan: no `FileState.*` in a READ position in the 3 pending helpers (dual-write `.state=` writes allowed); mutation-tested both directions | unit (AST/source scan) | `uv run pytest tests/pipeline/services/test_pending_no_state_read.py -q` | ❌ W0 | ⬜ pending |
| 82-03-* | 03 | 3 | READ-02 | — | `get_stage_progress` enrich nodes return `{not_started,in_flight,done,failed,total}` summing to total; `get_pipeline_stats` has no `GROUP BY FileRecord.state`; all prior callers re-expressed; `_safe_count` degrade preserved | integration | `uv run pytest tests/pipeline/services/test_stage_progress.py tests/pipeline/routers/test_pipeline_stats.py -q` | ❌ W0 | ⬜ pending |
| 82-04-* | 04 | 4 | PERF-02 | — | `/pipeline/stats` endpoint `< ~1s` at 200K synthetic corpus at HEAD; EXPLAIN ANALYZE confirms 032 partial-index scans (not seq-scans) on the 3 pending queries + four-bucket query; number recorded in VERIFICATION | manual/bench (see below) | `just perf-stats-bench` (new recipe) | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] Extend `tests/integration/test_stage_status_equivalence.py` — add the `ELIGIBLE_CASES`/`eligible_clause` matrix (DERIV-04 additive extension)
- [ ] `tests/pipeline/services/test_pending_sets.py` — the SC#1 all-orderings test + A1 cloud-exclusion regression + dedup-exclusion assertions
- [ ] `tests/pipeline/services/test_pending_no_state_read.py` — the mutation-tested anti-drift source/divergence guard (mirror Phase 84 D-14)
- [ ] `tests/pipeline/services/test_stage_progress.py` / `tests/pipeline/routers/test_pipeline_stats.py` — four-bucket-sums-to-total + consumer re-expression coverage
- [ ] `scripts/seed_perf_corpus.py` + `just` bench recipe — the 200K synthetic-seed harness (no reusable harness exists)

*(Exact file paths are the planner's discretion; align with the existing `tests/pipeline/` layout.)*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| 200K poll-latency measurement | PERF-02 | Requires a seeded ~200K local corpus at migration HEAD + EXPLAIN ANALYZE; not part of the hermetic unit suite | `just test-db` → migrate to HEAD → `uv run python scripts/seed_perf_corpus.py --n 200000` → run the bench recipe → paste the endpoint timing + EXPLAIN ANALYZE index-scan evidence into VERIFICATION |
| Deploy ≥036 + zero `analyzed`-NULL guard (D-02) | READ-01 | Live/shadow assertion against the deploy target; CI can't see prod rows | Read-only lux probe: `COUNT(files WHERE state='analyzed' AND analysis_completed_at IS NULL AND failed_at IS NULL) = 0`; confirm Alembic head ≥ 036 |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 90 s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
