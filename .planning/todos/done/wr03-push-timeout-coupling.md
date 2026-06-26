---
status: done
created: 2026-06-26
resolved: 2026-06-26
source: 50-REVIEW.md (WR-03) + Phase 50 verification
resolves_phase: 51
resolution: "Fail-fast guard (preferred option) implemented in src/phaze/tasks/push.py _require_push_config — asserts push_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC < PUSH_FILE_SAQ_TIMEOUT_SEC and raises a clear RuntimeError on an inverted layering. Tests: test_require_push_config_rejects_inverted_timeout_layering + _rejects_exact_boundary + _passes_at_default_timeout."
tags: [config, deploy, footgun]
---

# WR-03: harden the push_file timeout-layering coupling in Phase 51

## Context

Phase 50's `push_file` fix (`fix(50): WR-03 ...`, commit `d17204b`) establishes a deterministic
timeout layering so a SAQ cancellation never orphans the rsync child before its SSH key is shredded:

```
rsync --timeout (inner) = AgentSettings.push_timeout_sec      = 600   src/phaze/tasks/push.py:103
asyncio.wait_for (outer)= push_timeout_sec + 30               = 630   src/phaze/tasks/push.py:167
PUSH_FILE_SAQ_TIMEOUT_SEC (SAQ job net)                       = 660   src/phaze/tasks/push.py:62
```

Required ordering: **inner < outer < SAQ-net**. Verified holding at defaults during Phase 50
verification (`inner=600 < outer=630 < net=660`).

## The footgun

`PUSH_FILE_SAQ_TIMEOUT_SEC` is a **module constant pinned to the default `push_timeout_sec` (600)**.
The producers that stamp it (`release_awaiting_cloud._enqueue_push_file`, `agent_push` mismatch
re-drive) live on the **control plane**, which builds `ControlSettings` and cannot see the agent's
`AgentSettings.push_timeout_sec`. So if an operator raises `PHAZE_PUSH_TIMEOUT_SEC` on the agent in
Phase 51 (e.g. for very large transfers) without bumping the control-side constant, the layering
**inverts** — SAQ cancels healthy long transfers ~minutes before rsync's own timeout.

## Acceptance (do in Phase 51 — deploy/config/docs phase)

Pick at least one; the fail-fast guard is preferred:

- [x] **Fail-fast guard (preferred):** `_require_push_config` (called at the agent's first push)
      asserts `push_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC < PUSH_FILE_SAQ_TIMEOUT_SEC` and raises a
      clear RuntimeError otherwise. Turns the silent footgun into a loud, immediate failure. ✅ DONE.
- [ ] **Make it a knob:** expose the control-side SAQ timeout as its own env var
      (e.g. `PHAZE_PUSH_FILE_SAQ_TIMEOUT_SEC`) so both timeouts can be raised together.
- [ ] **Document the coupling** in the Phase 51 deploy/config docs: "If you raise
      `PHAZE_PUSH_TIMEOUT_SEC`, you MUST keep the control-side SAQ margin above it."

## References
- `src/phaze/tasks/push.py:47-62` (the constants + rationale comment)
- `src/phaze/tasks/push.py:166-175` (the TimeoutError/CancelledError reaping the layering protects)
- `.planning/phases/50-push-pipeline/50-REVIEW.md` (WR-03)
