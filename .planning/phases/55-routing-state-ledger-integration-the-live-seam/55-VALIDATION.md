---
phase: 55
slug: routing-state-ledger-integration-the-live-seam
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-28
audited: 2026-06-28
---

# Phase 55 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Detailed critical-transition mapping lives in `55-RESEARCH.md` "## Validation Architecture";
> the planner reconciles this to the actual plan/test filenames it creates.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio, `asyncio_mode = "auto"`) |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` (`testpaths = ["tests"]`) |
| **Quick run command** | `uv run pytest tests/test_staging_cron.py tests/test_routers/test_agent_s3.py tests/test_routing_seam.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60 seconds (quick) / ~5 min (full) |
| **DB-backed tests** | ephemeral Postgres on host port 5433 + Redis 6380 via `just test-db`; export `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL` to `localhost:5433` and `PHAZE_REDIS_URL` to `localhost:6380` (conftest defaults to the CI port 5432). Kube fakes via `tests/kube_fakes.py` + the `kube_respx` fixture. |

---

## Sampling Rate

- **After every task commit:** Run the quick run command (window-branch + S3 callback + routing-seam guard).
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`.
- **Before `/gsd:verify-work`:** Full suite green + ≥85% coverage + `pre-commit run --all-files` (ruff/mypy/bandit).
- **Max feedback latency:** 60 seconds.

---

## Per-Task Verification Map

> Populated by the planner from `55-RESEARCH.md` "## Validation Architecture" against the actual plan tasks. Every KROUTE requirement + CONTEXT D-01..D-04 maps to a critical-transition test. The three load-bearing landmines from research each get a dedicated test:
> - **L1 (advisory-lock atomicity):** `stage_cloud_window` k8s branch holds the `pg_advisory_xact_lock` across the whole tick — assert the no-commit S3-staging core does NOT commit mid-loop.
> - **L2 (GATE-1 skip):** k8s files stage with NO online compute agent — assert they reach PUSHING (not wedged in AWAITING_CLOUD).
> - **L3 (no ledger seed):** k8s stage/backfill writes NO `process_file:<id>` scheduling-ledger row; backfill SELECT is ledger-scoped (EXISTS prior row).

> Reconciled to actual test filenames after execution. All requirements COVERED by green automated tests (full suite: 2474 passed via `just integration-test`). The three load-bearing landmines each have a dedicated test (L1/L2/L3 below).

| Requirement | Plan | Wave | Secure Behavior | Test File · Representative Test(s) | Status |
|-------------|------|------|-----------------|-----------------------------------|--------|
| KROUTE-01 (D-02) | 55-01 | 1 | `cloud_target` Literal selector + 3 per-target fail-fast validators; `cloud_burst_enabled` removed | `tests/test_config/test_cloud_target.py` · `test_cloud_target_default_local`, `test_cloud_target_invalid_member_rejected`; `tests/test_config/test_kube_settings.py`, `test_s3_settings.py` | ✅ green |
| KROUTE-02 (D-01a/b) | 55-03 | 2 | k8s branch reuses ≤N window in `stage_cloud_window` (S3 path); `report_uploaded` PUSHING→PUSHED + routed `submit_cloud_job` | `tests/test_staging_cron.py` · `test_k8s_branch_skips_compute_gate_and_stages_to_s3`, `test_k8s_branch_holds_with_no_fileserver`; `tests/test_routers/test_agent_s3.py` · PUSHING→PUSHED flip | ✅ green |
| KROUTE-03 (D-04) | 55-02 | 1 | PUSHING/PUSHED reused (no new FileRecord state); `cloud_phase` column on `cloud_job` only; submit seed + reconcile co-writes | `tests/test_migrations/test_migration_027_cloud_phase.py`; `tests/test_tasks/test_submit_cloud_job.py` · `test_submit_seeds_cloud_phase_queued_behind_quota`; `tests/test_tasks/test_reconcile_cloud_jobs.py` · `test_admission_to_success_sequence`; `tests/test_models/test_cloud_job.py` · `test_cloud_phase_nullable_string` | ✅ green |
| KROUTE-04 | 55-04 | 2 | static AST guard: every k8s enqueue routes through `enqueue_router`; no consumer-less default-queue, no whole-backlog sweep | `tests/test_no_default_queue_producers.py` · `test_submit_cloud_job_routes_to_controller_queue`, `test_k8s_backfill_query_is_ledger_scoped_not_whole_backlog`, `test_static_guard_would_catch_a_reintroduced_producer` | ✅ green |
| KROUTE-05 (D-03) | 55-04 | 2 | backfill ledger-scoped (EXISTS prior `process_file` row); k8s branch seeds NO ledger row | `tests/test_routers/test_pipeline.py` · `test_backfill_candidate_query_requires_prior_ledger_row`, `test_backfill_with_compute_online_still_holds_and_writes_single_ledger_row`; `tests/test_services/test_pipeline.py` · `test_backfill_candidates_filters_by_state_and_duration` | ✅ green |
| KROUTE-06 (D-04) | 55-05 | 3 | degrade-safe admission-state cards over `cloud_phase` | `tests/test_services/test_pipeline_counts.py` · `test_get_cloud_phase_counts_per_phase`, `test_get_cloud_phase_counts_degrades_to_zero_on_db_error`; `tests/test_routers/test_pipeline.py` (card render/OOB) | ✅ green |

**Landmine coverage (research's 3 load-bearing transitions):**
- **L1 (advisory-lock atomicity):** `tests/test_staging_cron.py::test_k8s_overlapping_ticks_never_exceed_window` — concurrent ticks never exceed the window (no mid-loop commit). ✅
- **L2 (GATE-1 skip):** `tests/test_staging_cron.py::test_k8s_branch_skips_compute_gate_and_stages_to_s3` — k8s files reach PUSHING with no compute agent online. ✅
- **L3 (no ledger seed / EXISTS-scoped):** `tests/test_routers/test_pipeline.py::test_backfill_candidate_query_requires_prior_ledger_row` + `tests/test_no_default_queue_producers.py::test_k8s_backfill_query_is_ledger_scoped_not_whole_backlog`. ✅

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

> Test scaffolds each tdd/test-bearing plan creates first (RED before GREEN). Real paths per research's 7 Wave-0 gaps — planner reconciles exact names:

- [x] `tests/test_config/test_cloud_target.py` — `cloud_target` selector + per-target fail-fast validators (cloud_burst_enabled removed) — D-02
- [x] `tests/test_staging_cron.py` — k8s branch in `stage_cloud_window` (S3 path, GATE-1 skip, advisory-lock held, no ledger seed) — D-01a, L1, L2, L3
- [x] `tests/test_routers/test_agent_s3.py` — `report_uploaded` flips PUSHING→PUSHED + routed `submit_cloud_job` enqueue — D-01b
- [x] `tests/test_migrations/test_migration_027_cloud_phase.py` — migration 027 additive/reversible, cloud_job-only — D-04
- [x] `tests/test_tasks/test_reconcile_cloud_jobs.py` (extend) + `tests/test_tasks/test_submit_cloud_job.py` (extend) — cloud_phase writes/seed — D-04
- [x] `tests/test_services/test_pipeline_counts.py` (extend) + `tests/test_routers/test_pipeline.py` — backfill candidate query (ledger-scoped) + KROUTE-06 admission cards — D-03, D-04
- [x] `tests/test_no_default_queue_producers.py` (extend) / `tests/test_routing_seam.py` — KROUTE-04 AST guard: k8s enqueue sites route through enqueue_router + no whole-backlog

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live K8s admission / quota behavior end-to-end | KROUTE-01..03 | Needs a real Kueue cluster (Phase 56 deploy); all in-phase logic is testable against the fake kube seam + ephemeral DB | Phase 56: select cloud_target=k8s against a live cluster, confirm a long file routes → suspended Job → admitted → analyzed via callback |

*All in-scope Phase 55 logic has automated verification against the fake kube API + ephemeral DBs.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (the 7 research gaps)
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** validated 2026-06-28

---

## Validation Audit 2026-06-28

| Metric | Count |
|--------|-------|
| Requirements audited | 6 (KROUTE-01..06) |
| COVERED (green automated) | 6 |
| PARTIAL | 0 |
| MISSING | 0 |
| Manual-only | 1 (live K8s admission E2E — Phase 56) |

State A audit: the plan-time draft's TBD per-task map was reconciled to the actual merged test files. Every KROUTE requirement maps to named, behavior-targeting tests that pass in the full suite (2474 passed via `just integration-test`). The three load-bearing landmines (L1 advisory-lock atomicity, L2 GATE-1 skip, L3 ledger-scoped backfill) each have a dedicated green test. No gaps to fill — no auditor spawn required. `nyquist_compliant: true`.
