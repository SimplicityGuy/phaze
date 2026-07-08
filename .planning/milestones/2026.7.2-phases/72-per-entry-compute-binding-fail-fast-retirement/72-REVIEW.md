---
phase: 72-per-entry-compute-binding-fail-fast-retirement
reviewed: 2026-07-05T00:00:00Z
depth: standard
files_reviewed: 7
files_reviewed_list:
  - src/phaze/config.py
  - src/phaze/services/backends.py
  - src/phaze/services/enqueue_router.py
  - tests/analyze/services/test_backends.py
  - tests/analyze/services/test_compute_binding_golden.py
  - tests/shared/config/test_backend_registry.py
  - tests/shared/config/test_bucket_registry.py
findings:
  critical: 0
  warning: 2
  info: 2
  total: 4
status: issues_found
---

# Phase 72: Code Review Report

**Reviewed:** 2026-07-05
**Depth:** standard
**Files Reviewed:** 7
**Status:** issues_found

## Summary

Phase 72 retires the two `≤1`/`>1`-compute fail-fasts, adds the id-keyed liveness
selector `select_agent_by_id`, rewires `ComputeAgentBackend.is_available` onto a per-entry
`agent_ref` binding, and adds a boot-time duplicate-`agent_ref` guard in `_validate_registry`.

The three focus areas are mostly sound:

- **`select_agent_by_id` (enqueue_router.py:131-160)** is correct and safe: parameterized,
  matches on `Agent.id` ONLY (no name fallback), reuses the exact liveness filter, honors the
  `kind` scope, and raises `NoActiveAgentError` on any miss. The test matrix (id-only, revoked,
  never-seen, absent, wrong-kind) covers the edge cases.
- **`ComputeAgentBackend.is_available` (backends.py:265-278)** correctly resolves the bound
  `agent_ref` per-call (record-don't-rederive), degrades absent/offline to `False`, and fails
  loud on an unbound config. Both consumers (the drain snapshot loop and the UI `_probe_one`)
  wrap it in `try/except`, so the fail-loud `ValueError` never escapes to a cron.
- **The duplicate-`agent_ref` guard (config.py:445-451)** is correct: a `Counter` over
  non-None compute `agent_ref`s, id-tagged message, static (no DB), correctly skips `None`.

The material concern is that **the fail-fast retirement weakens a session-safety invariant that
`_probe_availability` still explicitly depends on** (WR-01), and that the retirement outpaces the
per-agent dispatch/scratch wiring deferred to Phase 73 with only a partial replacement guard
(WR-02).

## Warnings

### WR-01: `_probe_availability` concurrently uses one `AsyncSession` under N compute backends (retired invariant)

**File:** `src/phaze/services/backends.py:593-603` (and `_probe_one` at `574-590`)
**Issue:** `_probe_availability` fans probes out with `asyncio.gather(*(_probe_one(session, b) for b in backends))`. `LocalBackend` short-circuits with no I/O and `KueueBackend.is_available` does kr8s I/O (no session), but **`ComputeAgentBackend.is_available` calls `select_agent_by_id(session, ...)` on the shared session**. The function's own docstring asserts this is safe *because* "the D-05 invariant caps compute at ≤1, so at most ONE probe ever uses the session concurrently." **Phase 72 retired exactly that ≤1-compute invariant** (`resolve_backends` now returns N compute impls; the boot guard only rejects duplicate `agent_ref`, not distinct N-compute). With ≥2 compute backends, `gather` drives ≥2 concurrent `session.execute()` calls on one `AsyncSession`, which SQLAlchemy forbids (`InvalidRequestError` / `IllegalStateChangeError`). `_probe_one`'s `except Exception` contains the raise, but the observable result is that compute lanes are reported **offline/unavailable** on the live 5s `/pipeline/stats` poll (`get_backend_lane_snapshot` -> `pipeline.py:563/661`) whenever ≥2 compute backends are configured — the precise scenario this milestone exists to enable. The drain path (`release_awaiting_cloud.py:142-157`) is unaffected because it probes **sequentially**; only this UI snapshot uses `gather`.

Severity note: WARNING because the effect is contained (read-only UI, degrade-safe to `[]`), and Phase 72's target deploy is still 1-compute. It becomes a hard correctness bug the moment a 2nd compute backend is declared.

**Fix:** Do not run compute probes concurrently on a shared session. Run session-free probes (local/kueue) via `gather` and serialize the compute probes, or give each compute probe its own session/connection. Also correct the now-false session-safety docstring.
```python
async def _probe_availability(session: AsyncSession, backends: list[Backend]) -> dict[str, bool]:
    # Local/kueue probes are session-free and may run concurrently; a ComputeAgentBackend
    # probe touches the shared AsyncSession, and there may now be N of them (the ≤1-compute
    # invariant was retired in Phase 72) -- they MUST NOT run concurrently on one session.
    session_free = [b for b in backends if not isinstance(b, ComputeAgentBackend)]
    compute = [b for b in backends if isinstance(b, ComputeAgentBackend)]
    results = list(await asyncio.gather(*(_probe_one(session, b) for b in session_free)))
    for b in compute:  # serialize the session-touching probes
        results.append(await _probe_one(session, b))
    return dict(results)
```

### WR-02: Fail-fast retirement outpaces per-agent dispatch; replacement guard is narrower than what it replaced

**File:** `src/phaze/config.py:483-501` (`active_compute_scratch_dir`), `src/phaze/services/backends.py:492-516` (`resolved_non_local_kind`), `src/phaze/services/backends.py:280-316` (`ComputeAgentBackend.dispatch`)
**Issue:** The retired `>1`-compute fail-fast previously prevented *any* N-compute registry from booting. Its replacement (the duplicate-`agent_ref` guard) only rejects two entries binding the *identical* `agent_ref`; a registry with N *distinct*-`agent_ref` compute backends now boots. But the dispatch/scratch surface is not yet per-agent: `active_compute_scratch_dir` silently returns the **first** compute entry's `scratch_dir` (read globally by `agent_push.py:133` to build the `/pushed` scratch path), `resolved_non_local_kind` reduces to `non_local[0].kind`, and `ComputeAgentBackend.dispatch` still pushes to the single static fileserver push target rather than the selected backend's bound agent. So an operator who configures ≥2 distinct compute backends in Phase 72 gets **silent scratch-path misrouting** (files pushed under one agent's dir, verified against another's) instead of a boot error. This is documented as a Phase-73 (MCOMP-03) deferral, but the boot guardrail that made the misconfiguration *impossible* was removed one phase before the capability that makes it *correct* lands.

**Fix:** Until per-agent dispatch/scratch resolution lands (Phase 73), keep a narrow transitional boot guard so N-compute cannot silently misroute — e.g. in `_validate_registry`, reject `>1` compute backend whose `scratch_dir` values differ (or `>1` compute entry outright), with an id-tagged message pointing at the Phase-73 dependency. If the phased sequencing is intentional and accepted, add an explicit runbook note that N-compute config is not yet dispatch-safe.

## Info

### IN-01: Module `is_available`-never-raises discipline is contradicted by the fail-loud `ValueError` path

**File:** `src/phaze/services/backends.py:33-34` (docstring) vs `251-263`/`265-278` (`_agent_ref` / `is_available`)
**Issue:** The module docstring states `is_available`/`dispatch`/`reconcile` "never raise out to a cron," but `ComputeAgentBackend.is_available` intentionally raises `ValueError` (via `_agent_ref()`) for an unbound config. This is benign in practice — the config validator guarantees `agent_ref` at construction, and both consumers (`release_awaiting_cloud.py:151-157`, `_probe_one:585-589`) catch `Exception` — but the blanket docstring claim is inaccurate for the compute impl.
**Fix:** Narrow the docstring to note the compute `is_available` fail-loud exception for the unreachable unbound-config case (defense-in-depth), so the "never raises" contract is not read as absolute.

### IN-02: `resolved_non_local_kind` compute-only branch returns `non_local[0].kind` where a literal `"compute"` is clearer

**File:** `src/phaze/services/backends.py:516`
**Issue:** The final `return non_local[0].kind` is only reached when `cloud_enabled` is true and no kueue backend exists, so `non_local` is all-compute and the value is always `"compute"`. Indexing `[0]` reads as if it could vary and is a latent surprise if a future non-local kind is added without updating this branch.
**Fix:** Return the literal `"compute"` (the branch's actual invariant) or assert all-compute, so the intent is explicit and a future kind addition fails loudly rather than silently returning an arbitrary first entry.

---

_Reviewed: 2026-07-05_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
