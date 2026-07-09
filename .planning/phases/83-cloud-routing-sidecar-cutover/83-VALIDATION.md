---
phase: 83
slug: cloud-routing-sidecar-cutover
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-09
---

# Phase 83 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `83-RESEARCH.md` §Validation Architecture. Requirement: **SIDECAR-01**.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio, via `uv run pytest` (never bare `pytest` — CLAUDE.md) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` + `tests/buckets.json` (CI shard map) |
| **Quick run command** | `just test-bucket <bucket>` (`agents` / `integration` / `analyze` / `shared`) |
| **Full suite command** | `just test-db && uv run pytest` |
| **Estimated runtime** | ~30–90s per bucket; full suite several minutes |

> **Isolation is mandatory, not optional.** This project has a documented non-hermetic-test class
> (`get_settings` lru_cache leak, `saq_jobs` stub poison). A new test MUST pass via
> `just test-bucket <bucket>` **in isolation** — passing only inside the full suite is a failure.
>
> **Port footgun:** the test DB is on **5433**, but `MIGRATIONS_TEST_DATABASE_URL` defaults to 5432
> and `just test-bucket` does **not** export it. The `034` migration test must export both DB URLs
> explicitly or it fails in isolation in a way that mimics an unrelated infra flake.

---

## Sampling Rate

- **After every task commit:** the touched bucket's quick run (e.g. `just test-bucket agents` for a callback-guard task), passing **in isolation**.
- **After every plan wave:** `just test-bucket integration` + `agents` + `analyze` + `shared`.
- **Before `/gsd:verify-work`:** full suite green; per-module coverage floor **90**.
- **Max feedback latency:** ~90 seconds (single bucket).

---

## Per-Task Verification Map

Task IDs are assigned by the planner; this map binds each **success criterion and delegated decision**
to its proving test. The planner must attach each row to a concrete task.

| Req / SC | Wave | Threat Ref | Secure Behavior | Test Type | Automated Command | Bucket | File Exists | Status |
|---|---|---|---|---|---|---|---|---|
| SC#2 (SIDECAR-01, D-09/D-10) | 1 | T-83-01 (Tampering) | A late/duplicate `/upload-failed` on an already-advanced file (`cloud_job` RUNNING/SUCCEEDED) matches 0 rows → **FULL no-op**: no `cloud_job` write, no `FileRecord` write, no multipart abort, no `delete_staged_object`, no ledger clear | regression | `just test-bucket agents` | agents | ❌ W0 — extend `tests/agents/routers/test_agent_s3.py` | ⬜ pending |
| SC#2 (D-11) | 1 | T-83-02 (Tampering) | Two concurrent `/upload-failed` for one file cannot each read the same `s3_upload_attempt` and lose an increment — `pg_advisory_xact_lock` serializes the RMW; the cap still trips at the boundary | concurrency regression | `just test-bucket agents` | agents | ❌ W0 — donor: `/mismatch` T-73-13 in `tests/agents/routers/test_agent_push.py` | ⬜ pending |
| **SC#3 (HARD GATE)** | 2 | — | Two sequential `stage_cloud_window` ticks across (a) local dispatch, (b) rolled-back tick with a committed ledger row, (c) terminally-failed local analyze → each file dispatched **exactly once**, and **never** to a cloud backend after a local dispatch | integration | `just test-bucket integration` | integration | ❌ W0 — new file in `tests/integration/`; drive via `tests/analyze/tasks/test_release_awaiting_cloud.py` + `test_staging_cron.py` fixtures | ⬜ pending |
| SC#3 (shadow green) | 2 | — | Shadow-compare stays green; the new go-forward `awaiting` writer + `034` repair make the **hard** `awaiting_cloud` invariant pass (it is violated at HEAD) | integration | `just test-bucket integration` | integration | ⚠ extend `tests/integration/test_shadow_compare.py` | ⬜ pending |
| SC#1 (D-05/D-06/D-12) | 1–2 | — | Drain query, the three dispatch route flips, and the four callback guards read/write `cloud_job` (or derived `in_flight`) — **no `FileRecord.state` routing read** | static + behavioral | `just test-bucket analyze` + grep audit | analyze | ⚠ extend `tests/analyze/services/test_backends.py`, `test_dispatch_snapshot.py` | ⬜ pending |
| D-04 (corpus repair) | 1 | T-83-03 (Tampering) | Migration `034` backfill is **idempotent** (`ON CONFLICT DO NOTHING`) and repairs the un-sidecar'd `AWAITING_CLOUD` corpus; static parameter-free SQL, no interpolation | migration | `MIGRATIONS_TEST_DATABASE_URL=…:5433… uv run pytest tests/integration/test_migrations/test_migration_034_*.py` | (migration) | ❌ W0 | ⬜ pending |
| D-04 (autogenerate parity) | 1 | — | ORM `__table_args__` mirrors any `034` constraint so `alembic revision --autogenerate` yields an **empty diff** (77 D-01 precedent) | unit | `just test-bucket integration` | integration | ⚠ existing autogenerate-parity pattern | ⬜ pending |
| D-03 (spill row) | 1 | — | `'awaiting' ∉ backends.IN_FLIGHT` — a re-stamped spill row does not corrupt per-backend `in_flight_count`; `select_backend` reads spent `attempts` and routes to local | unit | `just test-bucket analyze` | analyze | ⚠ extend `tests/analyze/services/test_backends.py` | ⬜ pending |
| D-14 (reaper) | 2 | — | `put_analysis` / `report_analysis_failed` DELETE the file's `cloud_job` row `WHERE status='awaiting'`; a cloud-analyzed file's `SUCCEEDED`/`RUNNING` row is left untouched | unit | `just test-bucket agents` | agents | ❌ W0 — extend `tests/agents/routers/test_agent_analysis.py` | ⬜ pending |
| D-15 (count card) | 2 | — | `get_awaiting_cloud_count` derives from the **same** clause builder the drain uses — card and drain cannot disagree (a locally-analyzing long file is excluded from both) | unit | `just test-bucket analyze` / `shared` | analyze/shared | ❌ W0 — extend `tests/shared/routers/test_pipeline.py` | ⬜ pending |
| D-13 (LOCAL_ANALYZING flip) | 2 | — | The flip is retained (dual-write, 81 D-05); a `LOCAL_ANALYZING` file carrying an `awaiting` row violates no shadow invariant (implication-not-equality, 79 D-04) | unit | `just test-bucket analyze` | analyze | ⚠ extend `test_backends.py` | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/integration/test_<sc3_drain>.py` — the SC#3 two-tick double-dispatch gate (**hard gate**, per ROADMAP: "not a recommendation")
- [ ] `tests/agents/routers/test_agent_s3.py` additions — SC#2 CAS full-no-op + D-11 concurrency (donor: T-73-13)
- [ ] `tests/integration/test_migrations/test_migration_034_*.py` — idempotent backfill; **export `MIGRATIONS_TEST_DATABASE_URL` on port 5433**
- [ ] `tests/integration/test_shadow_compare.py` additions — `awaiting_cloud` hard invariant green after the writer + `034`
- [ ] `tests/shared/routers/test_pipeline.py` additions — `get_awaiting_cloud_count` derives from the drain clause
- [ ] `tests/agents/routers/test_agent_analysis.py` additions — D-14 reaper
- [ ] Fixture reuse (do **not** reinvent): `tests/analyze/core/test_staging_cron.py`, `tests/analyze/core/test_dispatch_snapshot.py`, `tests/analyze/tasks/test_release_awaiting_cloud.py` for driving `stage_cloud_window` (fake agents, `task_router` stubs)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|---|---|---|---|
| Live-corpus shadow-compare run proving the `034` repair against the real 200K set | SC#3 / D-04 | Requires a restore of the production corpus; the 79 D-02 precedent explicitly defers the live run to the next homelab rollout | Run `just shadow-compare` against a live restore after the rollout; record the `awaiting_cloud` invariant's divergence count (must be 0) in `83-VERIFICATION.md` |
| Drain query plan uses `ix_cloud_job_awaiting` rather than a seq scan at 200K rows | D-05 / D-14 | Plan shape varies by row count and table statistics; a hard `EXPLAIN` assertion is brittle across PG versions | Optional: `EXPLAIN (ANALYZE, BUFFERS)` the drain SELECT on the restored corpus post-rollout. The durable defense is the D-14 reaper bounding the `awaiting` set, not the assertion |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or a Wave 0 dependency
- [ ] Sampling continuity: no 3 consecutive tasks without an automated verify
- [ ] Wave 0 covers all ❌ MISSING references above
- [ ] No watch-mode flags
- [ ] Every new test passes via `just test-bucket <bucket>` **in isolation**
- [ ] `034` migration test exports both DB URLs (port 5433)
- [ ] Feedback latency < 90s per bucket
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
</content>
