---
phase: 34-pipeline-queue-depth-status-double-enqueue-guard
plan: "00"
subsystem: test-harness
tags: [test-doubles, saq, queue-depth, wave-0]
requires: []
provides:
  - "FakeQueue.count + per-kind depth seeding for queue-activity tests"
  - "FakeTaskRouter.set_counts for per-agent depth seeding"
affects:
  - "Plans 01-04 (get_queue_activity service + router/partial tests)"
tech-stack:
  added: []
  patterns:
    - "Async test-double method mirroring saq.Queue.count(kind) contract"
    - "Opt-in failure switch (fail_count) to exercise degrade-to-0 path"
key-files:
  created:
    - tests/_queue_fakes_test.py
  modified:
    - tests/_queue_fakes.py
decisions:
  - "count returns self._counts.get(kind, 0) so any unknown kind (incl. 'incomplete') reads 0 unless seeded"
  - "FakeTaskRouter.set_counts seeds via queue_for so the seeded queue is the same cached instance the service reads"
metrics:
  duration: "~6 min"
  completed: "2026-06-11"
  tasks: 2
  files: 2
---

# Phase 34 Plan 00: Queue-Fake Harness Extension Summary

Extended the shared `tests/_queue_fakes.py` doubles with an awaitable, seedable `FakeQueue.count(kind)` plus a `fail_count()` raise switch and a `FakeTaskRouter.set_counts(agent_id, ...)` helper, unblocking every downstream `get_queue_activity` / router / partial test (Plans 01-04) that needs to assert summed live queue depth or the Redis-error / missing-attr degrade path.

## What Was Built

- **`FakeQueue`** gained:
  - `self._counts: dict[str, int]` (defaults `{"queued": 0, "active": 0, "incomplete": 0}`) and `self._count_raises = False` in `__init__`.
  - `async def count(self, kind: str) -> int` — mirrors `saq.Queue.count`; raises `RuntimeError("fake redis down")` when `fail_count()` was called, else returns `self._counts.get(kind, 0)`.
  - `set_counts(*, queued, active, incomplete)` — seeds the per-kind depths.
  - `fail_count()` — flips the raise switch for the degrade-path tests.
- **`FakeTaskRouter`** gained `set_counts(agent_id, *, queued, active)` which forces lazy creation/caching via `queue_for(agent_id)` then seeds that cached queue — so a test pre-seeds an agent's depth before the service enumerates it, and the later `queue_for` read returns the same instance.
- **`tests/_queue_fakes_test.py`** (new) — four `@pytest.mark.asyncio` tests proving: seeded depths read back; un-seeded kind is 0; `fail_count()` makes `count` raise; per-agent seeding routes through the cached `queue_for` fake.

`enqueue`, `captured`, `captured_policy`, and the capture wiring were left untouched.

## Deviations from Plan

None - plan executed exactly as written.

## Verification

- `uv run ruff check tests/_queue_fakes.py` — clean.
- `uv run python -c "ast.parse(...)"` — parses.
- `uv run pytest tests/_queue_fakes_test.py -q` — 4 passed.
- `uv run pytest tests/test_routers/test_pipeline.py -q` — 31 passed (no regression in `_queue_fakes` importers).
- Pre-commit hooks (ruff, ruff-format, bandit, mypy) passed on both commits.

## Commits

- `fe419f0` test(34-00): add seedable async count + raise switch to queue fakes
- `e4e544a` test(34-00): cover queue-fake count/seed/raise harness methods

## Self-Check: PASSED

- FOUND: tests/_queue_fakes.py (`async def count` at line 85)
- FOUND: tests/_queue_fakes_test.py
- FOUND: commit fe419f0
- FOUND: commit e4e544a
