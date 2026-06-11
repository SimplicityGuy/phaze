---
phase: 32-pipeline-reboot-resilience-re-enqueue
plan: 00
subsystem: test-harness
tags: [saq, dedup, test-doubles, wave-0]
requires:
  - "tests/_queue_fakes.py::FakeQueue (extended)"
  - "tests/_queue_fakes.py::FakeTaskRouter (extended)"
provides:
  - "tests/_queue_fakes.py::DedupFakeQueue — SAQ deterministic-key dedup no-op double"
  - "tests/_queue_fakes.py::DedupFakeTaskRouter — caches DedupFakeQueue per agent"
  - "tests/test_queue_fakes_dedup.py — self-tests pinning the dedup contract"
affects:
  - "Wave 2 re-enqueue task (asserts re-enqueuing an in-flight file is a no-op)"
tech-stack:
  added: []
  patterns:
    - "Additive subclass of an existing test double (zero blast radius on existing consumers)"
    - "Model SAQ per-queue incomplete-set dedup without live Redis"
key-files:
  created:
    - tests/test_queue_fakes_dedup.py
  modified:
    - tests/_queue_fakes.py
decisions:
  - "DedupFakeQueue subclasses FakeQueue rather than mutating it — keeps the 6 existing FakeQueue consumers byte-identical"
  - "Dedup discriminator is the `key` kwarg (already a saq.Job dataclass field, so the parent routes it to captured_policy); keyless enqueues never dedup"
metrics:
  duration: ~6m
  completed: 2026-06-11
  tasks: 2
  files: 2
---

# Phase 32 Plan 00: Wave-0 Dedup Test Harness Summary

Built `DedupFakeQueue` + `DedupFakeTaskRouter` — additive test doubles that model SAQ's deterministic-key dedup no-op (None on a repeat in-flight key) without a live Redis, plus a 4-test self-test module proving the contract.

## What Was Built

- **`DedupFakeQueue(FakeQueue)`** (`tests/_queue_fakes.py`): overrides `enqueue` so that a `key` kwarg already present in `self._live_keys` returns `None` immediately — no append to `captured` / `captured_policy` / the shared `_capture` list — mirroring `RedisQueue._enqueue`'s nil-on-duplicate-incomplete-key behavior (32-RESEARCH §Q1). Otherwise delegates to `super().enqueue(...)`, records the key, and returns the parent's job. A keyless enqueue never dedups. `finish(key)` discards a key to model job completion so the same key re-enqueues afterward.
- **`DedupFakeTaskRouter(FakeTaskRouter)`**: identical wiring to the parent (`queue_for_calls` recording, shared `captures`, per-agent caching) with the single override of constructing `DedupFakeQueue` instances.
- **`tests/test_queue_fakes_dedup.py`**: 4 async self-tests — repeat-live-key→None (captured len 1), keyless-never-dedups (captured len 2), finish()-allows-reenqueue, and taskrouter-returns-cached-DedupFakeQueue.

## Why It Matters

Wave 2's re-enqueue task asserts that re-enqueuing a still-incomplete file is a no-op. The base `FakeQueue` always appends and returns a fresh `MagicMock` job, so it cannot express that assertion (the §Q5 Wave-0 gap). These doubles make the load-bearing primitive of the whole phase provable without Redis.

## Deviations from Plan

None — plan executed exactly as written.

## Verification

- `uv run pytest tests/test_queue_fakes_dedup.py -q` → 4 passed.
- `uv run pytest tests/test_routers/test_pipeline.py tests/test_services/test_enqueue_router.py -q` → 47 passed (additive subclass did not perturb existing `FakeQueue` consumers).
- `uv run ruff check tests/_queue_fakes.py tests/test_queue_fakes_dedup.py` → clean.
- Pre-commit hooks (ruff, ruff-format, bandit, mypy, ...) passed on both commits.

## Threat Surface

T-32-00-01 (DedupFakeQueue diverging from real SAQ) remains mitigated downstream: Wave 2 adds a `@pytest.mark.integration` real-Redis test pinning SAQ's actual None-on-duplicate behavior end-to-end. No new runtime trust boundary crossed (test-only harness).

## Commits

- `0f91cc2` test(32-00): add DedupFakeQueue + DedupFakeTaskRouter dedup doubles
- `9dc96dc` test(32-00): self-test the dedup-aware queue doubles

## Self-Check: PASSED
- FOUND: tests/_queue_fakes.py (modified, DedupFakeQueue present)
- FOUND: tests/test_queue_fakes_dedup.py (created)
- FOUND: commit 0f91cc2
- FOUND: commit 9dc96dc
