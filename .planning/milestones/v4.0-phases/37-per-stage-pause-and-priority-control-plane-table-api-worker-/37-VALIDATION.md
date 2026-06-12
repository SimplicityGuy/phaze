---
phase: 37
slug: per-stage-pause-and-priority-control-plane-table-api-worker
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-12
---

# Phase 37 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`uv run pytest`) |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_stage_control.py tests/test_task_split.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Integration DB** | `just integration-test` / `just test-db` (dedicated local PG) — required for real-PG UPDATE/dequeue tests |
| **Estimated runtime** | ~60s unit; integration adds real-PG round-trips |

---

## Sampling Rate

- **After every task commit:** `uv run pytest tests/test_stage_control.py tests/test_task_split.py -x`
- **After every plan wave:** `uv run pytest --cov --cov-report=term-missing` plus real-PG integration tests via `just integration-test`
- **Before `/gsd:verify-work`:** full suite + integration green; ≥85% coverage on the new modules
- **Max feedback latency:** ~60 seconds (unit); integration on wave merge

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 37-mig | 01 | 1 | schema | — | migration 020 upgrade creates table + seeds 3 rows + CHECK 0–100; downgrade drops cleanly | migration | `uv run pytest tests/test_migrations/test_020.py -x` | ❌ W0 | ⬜ pending |
| 37-hook | 02 | 2 | REQ-37-1, REQ-37-2 | T-37-04 | enqueue hook stamps stage priority + parks (`scheduled=SENTINEL`) when paused; non-stage jobs untouched; best-effort on read failure; reads via `job.queue.pool` only (no SQLAlchemy in agent) | unit (fake queue/pool) | `uv run pytest tests/test_stage_control.py -x` | ❌ W0 | ⬜ pending |
| 37-boundary | 02 | 2 | REQ-37 | T-37-04 | new hook module does NOT pull `phaze.database`/`sqlalchemy.ext.asyncio` into `agent_worker` | subprocess | `uv run pytest tests/test_task_split.py -x` | ⚠️ extend | ⬜ pending |
| 37-pause | 03 | 3 | REQ-37-1 | T-37-02 | pause parks queued backlog; active job drains untouched; paused `count("queued")`→0, `count("incomplete")` unchanged | integration (real PG) | `uv run pytest tests/integration/test_stage_pause.py -x` | ❌ W0 | ⬜ pending |
| 37-priority | 03 | 3 | REQ-37-2 | T-37-01 | priority UPDATE reorders dequeue (lower picked first); clamp `[0,100]` | integration (real PG) | `uv run pytest tests/integration/test_stage_priority.py -x` | ❌ W0 | ⬜ pending |
| 37-resume | 03 | 3 | REQ-37-3 | — | resume un-parks only SENTINEL rows; a retry-backoff (`scheduled=now+delay`) job is untouched | integration (real PG) | `uv run pytest tests/integration/test_stage_resume.py -x` | ❌ W0 | ⬜ pending |
| 37-concurrency | 03 | 3 | REQ-37-4 | T-37-03 | concurrent admin UPDATE vs dequeue: no double-pickup, no deadlock (status guard + SKIP LOCKED) | integration (real PG) | `uv run pytest tests/integration/test_stage_concurrency.py -x` | ❌ W0 | ⬜ pending |
| 37-endpoints | 04 | 4 | REQ-37-1, REQ-37-2, REQ-37-3 | T-37-01 | endpoints validate `stage ∈ {metadata,analyze,fingerprint}` (422 on unknown); delta clamps; returns `{stage,priority,paused}` | unit (httpx AsyncClient) | `uv run pytest tests/test_routers/test_stage_endpoints.py -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `src/phaze/models/pipeline_stage_control.py` + register in `models/__init__.py`
- [ ] `alembic/versions/020_add_pipeline_stage_control.py` — table + 3 seed rows + `priority` CHECK 0–100
- [ ] `src/phaze/tasks/_shared/stage_control.py` — `apply_stage_control` hook + `STAGE_TO_FUNCTION` + TTL-cached `_read_stage_control` + `SENTINEL`
- [ ] `src/phaze/services/stage_control.py` (or router) — `set_stage_priority` / `pause_stage` / `resume_stage` raw-UPDATE helpers
- [ ] `src/phaze/routers/pipeline_stages.py` (or extend `routers/pipeline.py`) — the 3 endpoints
- [ ] `tests/test_stage_control.py` — hook unit tests (fake queue/pool)
- [ ] `tests/integration/test_stage_{pause,priority,resume,concurrency}.py` — real-PG semantics
- [ ] `tests/test_routers/test_stage_endpoints.py` — endpoint validation/clamp/return-shape
- [ ] `tests/test_migrations/test_020.py` — migration upgrade/downgrade
- [ ] Extend `tests/test_task_split.py` for the new hook module's import boundary
- [ ] Register `apply_stage_control` in `build_pipeline_queue` (touches Phase 36 factory) — confirm all 4 construction sites inherit it

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live backlog reprioritization observed end-to-end on homelab | REQ-37-2 | Requires a real live backlog on the homelab Postgres broker | After redeploy: enqueue a stage backlog, POST a priority delta, confirm lower-priority jobs dequeue sooner via `/saq` |
| Pause across reboot re-applies to Phase-32 re-enqueued jobs | REQ-37-1 | Depends on a real reboot cycle + Phase-32 re-enqueue path | Pause a stage, reboot the worker, confirm re-enqueued jobs are re-parked (per Open-Q3 decision: pause persists) |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
