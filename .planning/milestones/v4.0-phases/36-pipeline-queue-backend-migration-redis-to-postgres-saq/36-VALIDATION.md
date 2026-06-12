---
phase: 36
slug: pipeline-queue-backend-migration-redis-to-postgres-saq
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-12
---

# Phase 36 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`uv run pytest`) |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_deterministic_key.py tests/test_task_split.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60 seconds (unit); integration adds real-PG round-trips via `just integration-test` |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_deterministic_key.py tests/test_task_split.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite green, **including** the new Postgres integration tests against the dedicated integration DB (`just integration-test` / `just test-db`)
- **Max feedback latency:** ~60 seconds (unit); integration on wave merge

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 36-01-01 | 01 | 1 | REQ-36-1 | — | Queue factory returns PostgresQueue with both before-enqueue hooks attached | unit | `uv run pytest tests/test_queue_factory.py -x` | ❌ W0 | ⬜ pending |
| 36-01-02 | 01 | 1 | REQ-36-5 | T-36-01 / cache-Redis isolated from broker | counters increment via dedicated cache-redis, not `queue.redis` | unit | `uv run pytest tests/test_deterministic_key.py -x` | ⚠️ exists, update fakes | ⬜ pending |
| 36-01-03 | 01 | 1 | REQ-36-5 | T-36-01 | `generate_proposals` rate-limits on cache-redis, not `queue.redis` (no AttributeError) | unit | `uv run pytest tests/test_proposal_task.py -x` | ⚠️ verify/extend | ⬜ pending |
| 36-02-01 | 02 | 2 | REQ-36-2 | — | priority + scheduled columns honored on enqueue/dequeue ordering | integration (real PG) | `uv run pytest tests/integration/test_pg_queue_priority.py -x` | ❌ W0 | ⬜ pending |
| 36-02-02 | 02 | 2 | REQ-36-3 | — | duplicate deterministic key returns None (reenqueue skip) via ON CONFLICT | integration (real PG) | `uv run pytest tests/integration/test_pg_dedup.py -x` | ❌ W0 | ⬜ pending |
| 36-02-03 | 02 | 2 | REQ-36-4 | — | `saq_web` / `info()` renders against PostgresQueue | unit/integration | `uv run pytest tests/test_saq_mount.py -x` | ⚠️ exists, extend for PG | ⬜ pending |
| 36-02-04 | 02 | 2 | REQ-36 | — | agent_worker import boundary clean; `PHAZE_QUEUE_URL` handled; psycopg3 does not pull asyncpg | subprocess | `uv run pytest tests/test_task_split.py -x` | ⚠️ update env | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_queue_factory.py` — covers REQ-36-1 (factory returns PostgresQueue with both hooks)
- [ ] `tests/integration/test_pg_queue_priority.py` — covers REQ-36-2 (priority/scheduled ordering)
- [ ] `tests/integration/test_pg_dedup.py` — covers REQ-36-3 (ON CONFLICT dedup returns None)
- [ ] Update `tests/_queue_fakes.py` — `FakeQueue.redis` → reflect the new dedicated cache-redis mechanism
- [ ] Update `tests/test_task_split.py` env — provide `PHAZE_QUEUE_URL`, assert psycopg3 import does NOT pull `sqlalchemy.ext.asyncio`
- [ ] Extend `tests/test_saq_mount.py` — assert mount works over PostgresQueue `.info()`

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| End-to-end enqueue→dequeue on homelab Postgres broker | REQ-36-1, REQ-36-2 | Requires live homelab Postgres + agent network reachability (Postgres:5432); not reproducible in CI | After homelab redeploy: enqueue a scan job via control plane, confirm `saq_jobs` row appears in Postgres and an agent dequeues it; check `/saq` shows the job |
| `saq_jobs` table first-boot autocreation + DB permissions | REQ-36-1 | Depends on real DB role grants on the homelab cluster | On first boot against fresh DB, confirm SAQ creates `saq_jobs` without permission errors (role needs CREATE on schema) |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
