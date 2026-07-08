---
phase: 78
slug: derivation-layer-eligibility-anti-drift-test-harness
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-08
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
| Stage/Status enums + DAG topology + Python per-row resolver (DB-free) | 1 | DERIV-01/02/03/05 | — | Agent-safe: no SQLAlchemy/DB import in `enums/stage.py` | unit | `uv run pytest tests/<bucket>/test_stage_resolver.py -x` | ❌ W0 | ⬜ pending |
| SQLAlchemy `ColumnElement[bool]` `.where()` builders (SQL twin) | 1 | DERIV-01/03 | — | Predicates spelled `= ANY (ARRAY[...])` / `IS NOT NULL` / `~exists(...)` anti-join | unit/integration | (equivalence test below) | ❌ W0 | ⬜ pending |
| Parametrized SQL-vs-Python equivalence (drift-lock) | 1 | DERIV-04, DERIV-05 | — | SQL-derived == Python-derived over full fixture matrix incl. 1-success/1-failed fingerprint | integration | `uv run pytest tests/services/test_stage_status_equivalence.py -x` | ❌ W0 | ⬜ pending |
| `eligible(f, stage)` pure predicate (enrich no-upstream; downstream conjuncts) | 2 | ELIG-01, ELIG-02 | — | Every `discovered` file eligible for all 3 enrich in any order | unit/integration | `uv run pytest tests/<bucket>/test_eligibility.py -x` | ❌ W0 | ⬜ pending |
| Failed-analyze terminal / failed-fingerprint eligible | 2 | ELIG-03, ELIG-04 | T-78 | Failed analyze absent from analyze pending/eligible set (44.5K-guard); failed fingerprint stays eligible | integration | `uv run pytest tests/<bucket>/test_eligibility.py::test_failed_analyze_terminal -x` | ❌ W0 | ⬜ pending |
| `in_flight` from ledger (authoritative) + saq_jobs SAVEPOINT degrade | 2 | INFLIGHT-01/02/03 | T-78 | saq_jobs read in `begin_nested()`, degrades to ledger-only; never falsely `not_started` | integration | `uv run pytest tests/<bucket>/test_in_flight_degrade.py -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/services/test_stage_status_equivalence.py` — the DERIV-04 parametrized SQL-vs-Python drift-lock over the full fixture matrix (all stages × statuses; DERIV-05 one-success/one-failed fingerprint → `done`).
- [ ] `tests/<bucket>/test_eligibility.py` — ELIG-01..04 including the **ELIG-03 terminal-failed-analyze regression** (a failed analyze is absent from the analyze pending/eligible set) and ELIG-04 failed-fingerprint-stays-eligible.
- [ ] `tests/<bucket>/test_in_flight_degrade.py` — INFLIGHT-02 SAVEPOINT-degrade: a poisoned/failed `saq_jobs` read degrades to ledger-only without raising; a crashed-mid-run (ledger row present, saq_jobs gone) reads `in_flight`, never `not_started`.
- [ ] `tests/<bucket>/test_stage_resolver.py` — the DB-free Python per-row resolver over plain scalars (agent-safe path).
- [ ] Framework install: **none** — pytest/pytest-asyncio already present.

*Bucket names to be finalized by the planner against `tests/buckets.json` (one bucket per file, partition-guard enforced).*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `in_flight(propose)` derivation | INFLIGHT-01 | `generate_proposals` is keyed by a set-hash of `file_ids`, not per-file — a per-file ledger key can't derive it (research finding). Out of scope for Phase 78 (ELIG-02 defines propose eligibility as upstream conjuncts only). | Documented deferral; no test this phase. Re-evaluate when propose gains a per-file trigger. |

*Note: this phase is purely additive — no reader/writer cuts over, so there is no live-pipeline behavior to UAT; correctness is proven entirely by the automated equivalence + regression harness.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (equivalence, eligibility/ELIG-03, in_flight degrade, resolver)
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
