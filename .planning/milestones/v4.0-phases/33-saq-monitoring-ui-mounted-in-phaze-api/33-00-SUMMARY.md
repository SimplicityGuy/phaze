---
phase: 33-saq-monitoring-ui-mounted-in-phaze-api
plan: "00"
subsystem: test-harness
tags: [saq, test-double, queue, tdd]
requires:
  - "saq.types.QueueInfo (shape mirrored by the new test double method)"
provides:
  - "tests/_queue_fakes.py::FakeQueue.info — Redis-free QueueInfo-shaped test double"
  - "tests/test_queue_fakes.py — regression test pinning the QueueInfo six-key shape"
affects:
  - "Wave 1 build_saq_app tests and Wave 2 lifespan tests (consume FakeQueue.info under saq_web)"
tech-stack:
  added: []
  patterns:
    - "Test double mirrors the real saq.queue.redis.Queue.info six-key QueueInfo shape, in-memory, no Redis"
key-files:
  created:
    - tests/test_queue_fakes.py
  modified:
    - tests/_queue_fakes.py
decisions:
  - "FakeQueue.info counts come from keyword-only __init__ kwargs (queued/active/scheduled, default 0) so existing positional callers are unaffected and tests can render non-zero depths"
  - "jobs/offset/limit accepted for saq_web signature parity but unused (ARG002 suppressed) — no real jobs to page over in-memory"
metrics:
  duration: "~6 min"
  completed: "2026-06-11"
  tasks: 2
  files: 2
---

# Phase 33 Plan 00: FakeQueue.info() Test Double Summary

Added a Redis-free `async def info()` to the existing `FakeQueue` test double returning the exact six-key `QueueInfo` shape `saq_web` renders, plus a regression test pinning that contract so the Wave 1/2 SAQ-monitoring-UI plans can build `saq_web` over in-memory fakes.

## What Was Built

- **`FakeQueue.info()`** (`tests/_queue_fakes.py`): mirrors `saq.queue.redis.Queue.info` (saq/queue/redis.py:170-176). Returns `{"workers": {}, "name": self.name, "queued", "active", "scheduled", "jobs": []}`. `workers`/`jobs` are always empty (no live workers, no Redis). The three counts are sourced from new keyword-only `__init__` kwargs (`queued`/`active`/`scheduled`, default 0) so a test can render non-zero dashboard depths. The `jobs`/`offset`/`limit` params are accepted for saq_web signature parity and unused (`# noqa: ARG002`).
- **`tests/test_queue_fakes.py`**: three async regression tests — full six-key shape + name echo, constructor count flow-through, and `info(jobs=True)` returning the same shape without raising. Module docstring documents WHY the shape matters (`saq_web._get_all_info` and the Wave 1 `build_saq_app` test depend on it).

## TDD Cycle

- RED (`734f029`): `test(33-00)` added `test_info_returns_full_queueinfo_shape_echoing_name` — failed with `AttributeError: 'FakeQueue' object has no attribute 'info'`.
- GREEN (`064ad4d`): `feat(33-00)` implemented `FakeQueue.info()` + the new keyword-only count kwargs — test passed.
- Task 2 (`363e84f`): `test(33-00)` pinned count flow-through and `info(jobs=True)` shape.

## Verification

- `uv run pytest tests/test_queue_fakes.py -q` — 3 passed.
- `uv run ruff check tests/_queue_fakes.py tests/test_queue_fakes.py` — clean.
- Regression sweep across all 9 `_queue_fakes` consumers (routers + services) — **145 passed**, confirming the keyword-only `__init__` addition broke no existing positional callers.
- No production code touched; no new dependency; mypy excludes `tests/` so ruff is the gate (clean).

## Deviations from Plan

None - plan executed exactly as written. (Task 2's `tests/test_queue_fakes.py` was seeded during Task 1's RED step since FakeQueue had no existing dedicated test module; Task 2 then completed it with the count-flow and `jobs=True` assertions — same final file, same commit semantics.)

## Self-Check: PASSED

- `tests/_queue_fakes.py` — FOUND (modified, `FakeQueue.info` present)
- `tests/test_queue_fakes.py` — FOUND (created)
- `734f029`, `064ad4d`, `363e84f` — all FOUND in git log
