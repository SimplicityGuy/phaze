---
phase: 78
slug: derivation-layer-eligibility-anti-drift-test-harness
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-08
updated: 2026-07-08
---

# Phase 78 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x + pytest-asyncio (`uv run pytest`) — already installed, no Wave 0 framework install |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) + `tests/buckets.json` per-bucket isolation |
| **Quick run command** | `uv run pytest tests/services/test_stage_status_equivalence.py -x` (DERIV-04 drift-lock) |
| **Full suite command** | `just test-bucket services` + `just test-bucket <deriv-bucket>` in isolation (ephemeral PG `:5433`) |
| **Estimated runtime** | ~10–60 seconds (parametrized equivalence + eligibility unit/integration) |

**DB env note:** SQL-side equivalence + anti-join tests require `TEST_DATABASE_URL` pointed at the `:5433` ephemeral DB (`just test-db`). The DB-free `enums/stage.py` Python resolver tests run without Postgres.

---

## Sampling Rate

- **After every task commit:** Run the quick command for the touched module (the equivalence test after any predicate change; the eligibility unit tests after `enums/stage.py` topology changes).
- **After every plan wave:** Run the affected buckets **in isolation** — per-bucket hermeticity is enforced by `tests/shared/test_partition_guard.py`.
- **Before `/gsd:verify-work`:** the equivalence test, the ELIG-03 terminal-failed-analyze regression, and the INFLIGHT-02 SAVEPOINT-degrade test all green + `uv run ruff check .` + `uv run mypy .` + `pre-commit run --all-files`.
- **Max feedback latency:** ~60 seconds.

---

## Per-Task Verification Map

> Populated by the planner's plans; keyed by requirement + the Wave-0 test file that proves it.

| Task group | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|------------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| Stage/Status enums + DAG topology + Python per-row resolver (DB-free) | 1 | DERIV-01/02/03/05 | — | Agent-safe: no SQLAlchemy/DB import in `enums/stage.py` | unit | `uv run pytest tests/shared/test_stage_resolver.py -x` | ✅ | ✅ green (27 passed) |
| `eligible()` pure predicate — enrich per-stage rule + downstream conjuncts (ELIG-01/02) incl. ELIG-03 terminal-analyze + ELIG-04 failed-fp-eligible + apply-approved | 1 | ELIG-01, ELIG-02, ELIG-03, ELIG-04 | T-78 | Failed analyze NOT eligible (44.5K-guard); failed fingerprint stays eligible; apply gated on an approved proposal | unit | `uv run pytest tests/shared/test_stage_eligibility_dag.py -x` | ✅ | ✅ green (17 passed; `-k terminal_failed_analyze` selects the ELIG-03 regression) |
| SQLAlchemy `ColumnElement[bool]` `.where()` builders (SQL twin) | 2 | DERIV-01/03 | — | Predicates spelled `= ANY (ARRAY[...])` / `IS NOT NULL` / `~exists(...)` anti-join | integration | (equivalence test below) | ✅ | ✅ green (proven by equivalence matrix) |
| Parametrized SQL-vs-Python equivalence drift-lock + `in_flight` ledger/SAVEPOINT-degrade | 2 | DERIV-04, DERIV-05, INFLIGHT-01, INFLIGHT-02, INFLIGHT-03 | T-78 | SQL-derived == Python-derived over full fixture matrix incl. 1-success/1-failed fingerprint; saq_jobs read in `begin_nested()`, degrades to ledger-only; never falsely `not_started` | integration | `uv run pytest tests/integration/test_stage_status_equivalence.py -x` | ✅ | ✅ green (25 cases vs ephemeral PG `:5433`; skip-clean when PG absent). INFLIGHT-03 D-01 decision record verified present in `stage_status.py` docstring (static-artifact check) |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/shared/test_stage_resolver.py` (`shared` bucket, DB-free) — the pure-Python per-row resolver over plain scalars (DERIV-02/03/05, agent-safe path). **27 passed.**
- [x] `tests/shared/test_stage_eligibility_dag.py` (`shared` bucket, DB-free) — ELIG-01..04: the enrich per-stage rule, the **ELIG-03 terminal-failed-analyze regression** (a failed analyze is NOT eligible), ELIG-04 failed-fingerprint-stays-eligible (non-vacuous `engine_statuses=["failed"]` → `Status.FAILED` → still eligible), and the apply-approved-proposal gate (ELIG-02). **17 passed.**
- [x] `tests/integration/test_stage_status_equivalence.py` (`integration` bucket, real PG) — the DERIV-04 parametrized SQL-vs-Python drift-lock over the full fixture matrix (all stages × statuses; DERIV-05 one-success/one-failed fingerprint → `done`) **plus** the INFLIGHT-02 SAVEPOINT-degrade cases (poisoned `saq_jobs` read degrades to ledger-only without raising; crashed-mid-run — ledger row present, saq_jobs gone — reads `in_flight`, never `not_started`). **25 cases green vs ephemeral PG.**
- [x] Framework install: **none** — pytest/pytest-asyncio already present.

*Buckets verified against `tests/buckets.json`: DB-free resolver/eligibility → `shared`; real-PG equivalence + degrade → `integration`. One bucket per file (partition-guard enforced).*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `in_flight(propose)` derivation | INFLIGHT-01 | `generate_proposals` is keyed by a set-hash of `file_ids`, not per-file — a per-file ledger key can't derive it (research finding). Out of scope for Phase 78 (ELIG-02 defines propose eligibility as upstream conjuncts only). | Documented deferral; no test this phase. Re-evaluate when propose gains a per-file trigger. |

*Note: this phase is purely additive — no reader/writer cuts over, so there is no live-pipeline behavior to UAT; correctness is proven entirely by the automated equivalence + regression harness.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (equivalence, eligibility/ELIG-03, in_flight degrade, resolver)
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** validated 2026-07-08 — all 12 requirements (DERIV-01..05, ELIG-01..04, INFLIGHT-01..03) have automated coverage; the single deferral (`in_flight(propose)`, INFLIGHT-01 per-file sub-case) is documented Manual-Only. INFLIGHT-03 (written D-01 decision record) verified by static-artifact check.

---

## Validation Audit 2026-07-08

| Metric | Count |
|--------|-------|
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

All Wave-0 test targets delivered during execution and confirmed green (resolver 27 · eligibility 17 · equivalence 25 real-PG). No gaps required the gsd-nyquist-auditor. Phase is Nyquist-compliant.
