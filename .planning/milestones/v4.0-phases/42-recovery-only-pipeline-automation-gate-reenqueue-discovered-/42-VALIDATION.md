# Phase 42 — Validation Plan

**Source:** materialized from 42-RESEARCH.md §Validation Architecture (single source of truth; this file is the standalone Nyquist artifact).

## Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config | `pyproject.toml` `[tool.pytest.ini_options]`; `integration` marker in use |
| Quick run | `uv run pytest tests/test_tasks/test_recovery.py -x` |
| Full suite | `uv run pytest --cov --cov-report=term-missing` (≥85%, CLAUDE.md) |

## Requirements → Test Map
| Req | Behavior to prove | Test type | Command | Status |
|-----|-------------------|-----------|---------|--------|
| REQ-42-1 (remove steady-state cron) | `reenqueue_discovered` / `*/5` entry absent from `settings["cron_jobs"]`; `refresh_tracklists` + `reap_stalled_scans` still present | unit (structural) | `uv run pytest tests/test_tasks/test_controller_reenqueue.py -x` | extend existing |
| REQ-42-2 (loss detector) | `saq_jobs` has queued/active rows ⇒ `recover_orphaned_work` returns `detected_loss=False`, enqueues nothing (durable-restart no-op) | unit + integration | `uv run pytest tests/test_tasks/test_recovery.py -x` | Wave 0 (new) |
| REQ-42-3 (all-stages reconcile) | empty `saq_jobs` + one pending item per stage ⇒ exactly one correctly-keyed enqueue per stage on the correct queue (agent vs controller) | unit | `uv run pytest tests/test_tasks/test_recovery.py -x` | Wave 0 (new) |
| REQ-42-4 (manual Recover surface) | `POST /pipeline/recover` exists, returns HTMX partial, and invokes the SAME `recover_orphaned_work` producer; DAG Recover button renders | unit (router + canvas) | `uv run pytest tests/test_routers/test_pipeline.py tests/test_dag_canvas_render.py -x` | Wave 2 |
| REQ-42-5 (idempotency / no doubling) | pre-marking half the keys live ⇒ those count `skipped`, only stragglers re-enqueue (Phase-32 doubling backstop); `force=True` bypasses ONLY the no-op gate, never per-item dedup | unit | `uv run pytest tests/test_tasks/test_recovery.py -x` | Wave 0 (new) |
| REQ-42 (agent-skip, cold boot) | `NoActiveAgentError` ⇒ agent stages skip while controller stages still reconcile | unit | `uv run pytest tests/test_tasks/test_recovery.py -x` | Wave 0 (new) |

## Sampling Rate
- **Per task commit:** `uv run pytest tests/test_tasks -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** full suite green + ≥85% coverage + `ruff`/`mypy`/`pre-commit` before verify.

## Wave 0 Gaps (new test scaffolding)
- [ ] `tests/test_tasks/test_recovery.py` — all-stages reconcile + loss detector + idempotency + force + agent-skip. One `@pytest.mark.integration` case reads the real `saq_jobs` via the `stage_env` fixture.
- [ ] Reuse `tests/_queue_fakes` (`DedupFakeTaskRouter`, `seed_active_agent`); add per-stage seed helpers (metadata/fingerprint/proposals/tracklist) — extend, don't fork.
- [ ] Extend `tests/test_tasks/test_controller_reenqueue.py` with the "cron removed" structural assert.

## Out of scope (not validated here)
- `trigger_scan` file_id-only dead-letter fix (separate follow-up PR).
