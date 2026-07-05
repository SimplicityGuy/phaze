---
phase: 72-per-entry-compute-binding-fail-fast-retirement
verified: 2026-07-05T18:30:00Z
status: passed
score: 7/7 must-haves verified
overrides_applied: 0
---

# Phase 72: Per-Entry Compute Binding + Fail-Fast Retirement Verification Report

**Phase Goal:** Declare N `compute` backends in `backends.toml`, each bound to a specific registered
compute Agent, all accepted at boot; retire + generalize the `≤1-compute` fail-fasts
(`active_compute_scratch_dir`, `resolved_non_local_kind`) for a `local + N-Kueue + N-compute` registry;
behavior-preserving groundwork, existing single-/zero-compute deploys unchanged (MCOMP-01).

**Verified:** 2026-07-05
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | The ≤1-compute + zero-compute paths are pinned by a committed golden characterization, green against pre-change behavior | ✓ VERIFIED | `tests/analyze/services/test_compute_binding_golden.py` (4 cells) exists, asserts `cloud_enabled`, `resolved_non_local_kind`, `active_compute_scratch_dir`, the exact `/pushed` scratch-path format, and `is_available` online/absent. Ran green: `uv run pytest tests/analyze/services/test_compute_binding_golden.py -q` → 4 passed. |
| 2 | `resolved_non_local_kind` no longer raises on 2+ compute-only backends — returns `"compute"` | ✓ VERIFIED | `grep -n "len(non_local) > 1" src/phaze/services/backends.py` → no match (raise deleted, `src/phaze/services/backends.py:492-516`). Live spot-check: 3-distinct-agent_ref compute registry → `resolved_non_local_kind == "compute"`. |
| 3 | `active_compute_scratch_dir` no longer raises on 2+ compute backends; ≤1 return byte-identical | ✓ VERIFIED | `grep -n "len(compute) > 1" src/phaze/config.py` → no match (raise deleted, `src/phaze/config.py:484-501`). Live spot-check with 3 compute backends returns first entry's `scratch_dir` (`/srv/scratch-a`), no raise; golden's 1-compute case still returns `/srv/scratch`. |
| 4 | A `backends.toml` declaring 2+ compute backends is accepted through both accessors where it previously 500'd | ✓ VERIFIED | Live behavioral spot-check: constructed `ControlSettings()` from a 3-compute-backend TOML (distinct `agent_ref`s) — booted cleanly, `resolve_backends` returned `[LocalBackend, ComputeAgentBackend×3]`. |
| 5 | Each compute backend resolves its bound agent from a per-entry `agent_ref` read per-call, not `select_active_agent(kind="compute")`'s single-active pick | ✓ VERIFIED | `src/phaze/services/backends.py:265-278` — `ComputeAgentBackend.is_available` calls `select_agent_by_id(session, self._agent_ref(), kind="compute")`. `grep -n 'select_active_agent(session, kind="compute")' src/phaze/services/backends.py` → no match inside `is_available` (only the unrelated `kind="fileserver"` dispatch lookup remains at line 292). |
| 6 | `agent_ref` resolves against `Agent.id` only, no name fallback; `is_available` True iff bound agent online, False (never raise) when absent | ✓ VERIFIED | `src/phaze/services/enqueue_router.py:131-160` — `select_agent_by_id` filters `Agent.id == agent_id`, no `Agent.name` predicate anywhere in the function. Test matrix (`test_backends.py`) covers id-match/name-only-mismatch/revoked/never-seen/wrong-kind, all green. |
| 7 | Boot fails fast (id-tagged) on duplicate compute `agent_ref`; N distinct-`agent_ref` compute backends boot cleanly; an unregistered `agent_ref` is NOT a boot error (static, no DB) | ✓ VERIFIED | `src/phaze/config.py:437-451` — `Counter`-based guard in `_validate_registry`, no `session`/`select(`/`Agent` DB access in the block. Live spot-check: two compute backends sharing `agent_ref="shared-node"` raised `ValueError` at `ControlSettings()` construction naming the value + both colliding ids (`compute-a`, `compute-b`). Registry test `test_agent_ref_to_unregistered_agent_is_not_a_boot_error` uses no DB fixture and passes. |

**Score:** 7/7 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `tests/analyze/services/test_compute_binding_golden.py` | D-06 golden byte-identical characterization | ✓ VERIFIED | 155 lines, 4 test functions, all pass against current code. |
| `src/phaze/services/backends.py::resolved_non_local_kind` | Generalized for N compute (no >1 raise) | ✓ VERIFIED | Raise block removed (L494 originally); falls through to `return non_local[0].kind`. |
| `src/phaze/config.py::active_compute_scratch_dir` | Generalized for N compute (no >1 raise), ≤1 byte-identical | ✓ VERIFIED | Raise block removed; falls through to `compute[0].scratch_dir`; ≤1 case unchanged. |
| `src/phaze/services/enqueue_router.py::select_agent_by_id` | Id-keyed liveness selector, raises `NoActiveAgentError` on miss | ✓ VERIFIED | Present at L131-160, mirrors `select_active_agent`'s liveness filter, keys on `Agent.id`. |
| `src/phaze/services/backends.py::ComputeAgentBackend` | Per-entry binding accessor + rewired `is_available` | ✓ VERIFIED | `_agent_ref()` (L251-263) + rewired `is_available` (L265-278), both present and wired. |
| `src/phaze/config.py::_validate_registry` | Counter-based, id-tagged, static duplicate-`agent_ref` guard | ✓ VERIFIED | L437-451, static (no DB session), skips `agent_ref is None`, names colliding ids. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `test_compute_binding_golden.py` | `resolve_backends` / `resolved_non_local_kind` | ControlSettings from 1-compute registry | ✓ WIRED | Golden calls both functions directly, asserts values. |
| `test_compute_binding_golden.py` | `active_compute_scratch_dir` / `cloud_enabled` | settings property reads | ✓ WIRED | Golden asserts both properties under 1-compute and all-local registries. |
| `backends.py::resolved_non_local_kind` | `non_local[0].kind` | compute-only branch, no >1 raise | ✓ WIRED | Confirmed via grep + live 3-compute spot-check returning `"compute"`. |
| `config.py::active_compute_scratch_dir` | `compute[0]` | >1 raise deleted, ≤1 unchanged | ✓ WIRED | Confirmed via grep + live 3-compute spot-check returning first entry's scratch_dir. |
| `backends.py::ComputeAgentBackend.is_available` | `enqueue_router.select_agent_by_id(session, self.config.agent_ref, kind="compute")` | per-entry bound agent_ref resolved against Agent.id | ✓ WIRED | Source read confirms exact call; test matrix (bound-online True / bound-absent False / mismatched-id False) green. |
| `enqueue_router.select_agent_by_id` | `Agent.id == agent_id AND revoked_at IS NULL AND last_seen_at IS NOT NULL` | id-keyed liveness query | ✓ WIRED | Source matches exactly; behavior cells (id-match/name-mismatch/revoked/never-seen/wrong-kind) all green. |
| `config.py::_validate_registry` | `Counter` over compute backends' `agent_ref` values | sorted duplicates -> id-tagged ValueError | ✓ WIRED | Source matches; live spot-check raised with both offending value and colliding ids named. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| N=3 distinct compute backends (distinct agent_refs) accepted at boot | ad-hoc `ControlSettings()` construction from a 3-compute TOML | `cloud_enabled=True`, `resolved_non_local_kind="compute"`, `resolve_backends` → `[local, compute-a, compute-b, compute-c]` | ✓ PASS |
| Duplicate compute `agent_ref` fails boot | ad-hoc `ControlSettings()` construction with two compute backends sharing `agent_ref="shared-node"` | `ValidationError` / `ValueError` naming `shared-node` and both colliding ids (`compute-a`, `compute-b`) | ✓ PASS |
| Golden ≤1-compute + zero-compute characterization | `uv run pytest tests/analyze/services/test_compute_binding_golden.py -q` | 4 passed | ✓ PASS |
| Full directly-affected test surface (golden + backends + registry + staging_cron + dispatch_snapshot) | `uv run pytest tests/analyze/services/test_compute_binding_golden.py tests/analyze/services/test_backends.py tests/shared/config/test_bucket_registry.py tests/shared/config/test_backend_registry.py tests/analyze/core/test_staging_cron.py tests/analyze/core/test_dispatch_snapshot.py -q` | 106 passed | ✓ PASS |
| mypy + ruff on all 3 edited production modules | `uv run ruff check src/phaze/config.py src/phaze/services/backends.py src/phaze/services/enqueue_router.py && uv run mypy` (same files) | All checks passed / Success: no issues found | ✓ PASS |
| Full repo suite (`uv run pytest -q`, ~2781 tests) | background run | Hung mid-run at fixed CPU time across 3 consecutive checks (~7 min, no progress) — matches the project's documented local colima full-suite flake; killed and re-verified the directly-affected surface in isolation (106 passed, see above) | ? INFRA-FLAKE (not attributable to Phase 72 code; targeted suites are conclusive) |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| MCOMP-01 | 72-01, 72-02, 72-03, 72-04 | Operator can declare N compute backends in backends.toml, each bound to a specific registered compute Agent, all accepted at boot; the two ≤1-compute fail-fasts retired and generalized | ✓ SATISFIED | All 7 observable truths verified above; both named fail-fasts (`active_compute_scratch_dir`, `resolved_non_local_kind`) retired; boot-time duplicate-agent_ref guard added as the fail-loud replacement. |

No orphaned requirements: REQUIREMENTS.md maps only MCOMP-01 to Phase 72; all 4 plans declare `requirements: [MCOMP-01]`. (Note: REQUIREMENTS.md's checkbox for MCOMP-01 is still `[ ]` unchecked — a documentation-sync lag, not a code gap; flagged below as informational.)

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/phaze/services/backends.py` | 593-603 (`_probe_availability`) | Docstring/invariant now false: "the D-05 invariant caps compute at ≤1, so at most ONE probe ever uses the session concurrently" — Phase 72 retired that invariant, so `resolve_backends` can now return N compute backends, and `asyncio.gather` in `_probe_availability` will drive ≥2 concurrent `session.execute()` calls on one shared `AsyncSession` the moment an operator configures ≥2 distinct compute backends | ⚠️ WARNING (already captured in `72-REVIEW.md` as WR-01, disposition: contained/deferred) | Read-only `/pipeline/stats` UI poll only; degrades compute lanes to "offline" in the dashboard rather than crashing or corrupting data (`_probe_one`'s `except Exception` contains the fault). Does not affect boot acceptance, the golden, or any ≤1-compute/zero-compute path. Recommend fixing before any real ≥2-compute deploy goes live (Phase 73 is the natural place, since it also owns the per-agent dispatch/scratch generalization referenced by the sibling WR-02). |
| `src/phaze/config.py` / `src/phaze/services/backends.py` | `active_compute_scratch_dir` (config.py:484-501), `resolved_non_local_kind` (backends.py:492-516), `ComputeAgentBackend.dispatch` (backends.py:280-316) | N-distinct-agent_ref compute registries now boot, but the scratch-path/dispatch surface still reduces to the FIRST compute entry globally (not per-selected-backend) — documented transitional reduction (D-07), explicitly deferred to Phase 73 (MCOMP-03) | ⚠️ WARNING (already captured in `72-REVIEW.md` as WR-02; explicitly pre-cleared as expected scope-boundary per phase task context) | An operator who configures ≥2 distinct compute backends today would get silent scratch-path misrouting rather than a boot error, until Phase 73 lands per-agent dispatch/scratch resolution. This is the intentional "groundwork, not full N-compute capability" scope of Phase 72 — confirmed as EXPECTED, not a gap, per the phase's own review disposition and the verification task's explicit scope note. |
| `.planning/STATE.md` | 23-30 | Stale tracking: still shows "Phase 72 — EXECUTING, Plan 1 of 4" despite all 4 plans + review being committed | ℹ️ INFO | Documentation-lag only; does not affect code correctness. Should be refreshed by the next `/gsd` tracking update. |
| `.planning/REQUIREMENTS.md` | 12 | MCOMP-01 checkbox still `[ ]` despite phase completion | ℹ️ INFO | Documentation-lag only; code evidence fully satisfies the requirement text. |

No `TBD`/`FIXME`/`XXX` unresolved debt markers found in any of the 3 edited production modules (`config.py`, `services/backends.py`, `services/enqueue_router.py`).

### Human Verification Required

None. All observable truths for MCOMP-01 are independently verifiable via source inspection, targeted test runs, and live behavioral spot-checks (ad-hoc `ControlSettings()` construction against synthetic multi-compute registries). No visual, real-time, or external-service behavior is in scope for this phase.

### Gaps Summary

No gaps against the phase's stated goal or MCOMP-01. All 7 derived observable truths are verified in the
codebase (not merely claimed in SUMMARY.md): the golden characterization is real and green, both named
`≤1-compute` fail-fasts are demonstrably retired (grep-confirmed absence + live 3-compute-backend
behavioral spot-check), the per-entry `agent_ref` → `Agent.id` binding replaces the single-active-compute
pick with a real, tested id-keyed selector, and the new duplicate-`agent_ref` boot guard fires correctly
on live construction while leaving distinct-ref and unregistered-ref registries to boot cleanly.

Two WARNING-level anti-patterns were found and are worth carrying forward into Phase 73 planning (both
already surfaced by `72-REVIEW.md`, not newly discovered here): (1) `_probe_availability`'s
UI-dashboard probe fan-out concurrently touches one shared `AsyncSession` across N compute backends —
harmless today (read-only, degrade-safe) but becomes a real correctness issue for the dashboard poll the
moment ≥2 compute backends are actually configured; (2) the scratch-path/dispatch surface still reduces
to the first compute entry, a documented and explicitly-scoped Phase-73 deferral. Neither blocks Phase
72's own goal — "declare N compute backends... accepted at boot... behavior-preserving groundwork" — both
are pre-existing-review-disposed and explicitly out of MCOMP-01's stated scope (MCOMP-02/03 belong to
Phase 73). Recommend Phase 73 prioritize the WR-01 session-concurrency fix alongside its planned
per-agent dispatch/scratch work, since it is a small, self-contained change (serialize the compute probes
instead of running them under `asyncio.gather`) that closes the gap between "N compute backends boot"
and "N compute backends are safe to actually run."

---

_Verified: 2026-07-05_
_Verifier: Claude (gsd-verifier)_
