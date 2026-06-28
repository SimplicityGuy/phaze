---
phase: 55
slug: routing-state-ledger-integration-the-live-seam
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-28
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
| **Quick run command** | `uv run pytest tests/test_tasks/test_release_awaiting_cloud.py tests/test_routers/test_agent_s3.py tests/test_routing_seam.py -x` |
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

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | TBD | TBD | KROUTE-01..06 / D-01..04 | T-55-* (planner) | populated by planner | unit | `uv run pytest ...` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

> Test scaffolds each tdd/test-bearing plan creates first (RED before GREEN). Real paths per research's 7 Wave-0 gaps — planner reconciles exact names:

- [ ] `tests/test_config/test_cloud_target.py` — `cloud_target` selector + per-target fail-fast validators (cloud_burst_enabled removed) — D-02
- [ ] `tests/test_tasks/test_release_awaiting_cloud.py` — k8s branch in `stage_cloud_window` (S3 path, GATE-1 skip, advisory-lock held, no ledger seed) — D-01a, L1, L2, L3
- [ ] `tests/test_routers/test_agent_s3.py` — `report_uploaded` flips PUSHING→PUSHED + routed `submit_cloud_job` enqueue — D-01b
- [ ] `tests/test_migrations/test_migration_027_cloud_phase.py` — migration 027 additive/reversible, cloud_job-only — D-04
- [ ] `tests/test_tasks/test_reconcile_cloud_jobs.py` (extend) + `tests/test_tasks/test_submit_cloud_job.py` (extend) — cloud_phase writes/seed — D-04
- [ ] `tests/test_services/test_pipeline_counts.py` (extend) + `tests/test_routers/test_pipeline_*.py` — backfill candidate query (ledger-scoped) + KROUTE-06 admission cards — D-03, D-04
- [ ] `tests/test_no_default_queue_producers.py` (extend) / `tests/test_routing_seam.py` — KROUTE-04 AST guard: k8s enqueue sites route through enqueue_router + no whole-backlog

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live K8s admission / quota behavior end-to-end | KROUTE-01..03 | Needs a real Kueue cluster (Phase 56 deploy); all in-phase logic is testable against the fake kube seam + ephemeral DB | Phase 56: select cloud_target=k8s against a live cluster, confirm a long file routes → suspended Job → admitted → analyzed via callback |

*All in-scope Phase 55 logic has automated verification against the fake kube API + ephemeral DBs.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (the 7 research gaps)
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
