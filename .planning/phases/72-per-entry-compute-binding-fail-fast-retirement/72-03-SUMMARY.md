---
phase: 72-per-entry-compute-binding-fail-fast-retirement
plan: 03
subsystem: analyze/backends
tags: [per-entry-binding, compute-backend, multi-compute, id-keyed-selector, byte-identical, MCOMP-01]
requires:
  - phaze.services.enqueue_router.select_active_agent (the sibling selector + NoActiveAgentError)
  - phaze.config_backends.ComputeBackend.agent_ref (the D-01 binding key, non-empty at construction)
  - phaze.services.backends.ComputeAgentBackend.is_available (the seam to rewire)
  - D-06 golden ≤1-compute (matching-ref) characterization (Plan 01)
provides:
  - phaze.services.enqueue_router.select_agent_by_id — id-keyed liveness selector (Agent.id, no name fallback), raises NoActiveAgentError when the bound agent is absent/offline/wrong-kind
  - phaze.services.backends.ComputeAgentBackend._agent_ref — fail-loud per-entry binding accessor (mirrors KueueBackend._kube())
  - ComputeAgentBackend.is_available rewired to the bound agent_ref → Agent.id (record-don't-rederive); the single-active-compute pick is retired
affects:
  - Phase 73 (MCOMP-03+) — builds per-agent dispatch / push / reconcile on this per-entry binding
tech-stack:
  added: []
  patterns:
    - record-don't-rederive (MKUE-01, Phase 70) — resolve a per-entry reference recorded at construction, read per-call; the compute-side twin of KueueBackend._kube()
    - degrade-safe absent-agent → hold (T-68-05) — NoActiveAgentError → is_available False, never raises out to the drain/cron
    - fail-loud accessor (defense-in-depth) — an unbound config raises via the accessor rather than silently holding
key-files:
  created: []
  modified:
    - src/phaze/services/enqueue_router.py
    - src/phaze/services/backends.py
    - tests/analyze/services/test_backends.py
decisions:
  - select_agent_by_id reuses select_active_agent's EXACT liveness filter (revoked_at IS NULL AND last_seen_at IS NOT NULL) + optional kind scope, keyed on Agent.id == agent_id instead of ORDER BY last_seen_at — matches on Agent.id ONLY, no id-or-name fallback (D-01).
  - _agent_ref() raises ValueError naming self.id when no agent_ref is bound (defense-in-depth; _require_dispatch_fields already guarantees it non-empty). is_available catches only NoActiveAgentError → False, so the unbound-fail-loud path propagates the ValueError rather than degrading to a silent hold.
  - Task 3 made no source edits — behavior-preserving confirmation only (golden + backend suites re-run green, golden module byte-untouched).
metrics:
  duration: ~15m
  completed: 2026-07-05
  tasks: 3
  files: 3
---

# Phase 72 Plan 03: Per-Entry Compute Binding Rewire (D-02) Summary

Each compute backend now resolves ITS bound agent from a per-entry reference recorded at construction
(`self.config.agent_ref`), read per-call and matched against `Agent.id` — replacing
`ComputeAgentBackend.is_available`'s retired `select_active_agent(kind="compute")` single-active-compute
pick. An absent / unregistered / offline bound agent degrades to a hold (`is_available` False, never
raises; D-05); an unbound config fails loud via the accessor. The Plan-01 golden stays byte-identical
green on the real single-compute deploy (agent id == agent_ref), so this closes success criterion 2
while preserving the ≤1-compute behavior.

## What Was Built

**Task 1 — `select_agent_by_id` id-keyed liveness selector (`enqueue_router.py`, D-01):**
- Added `async def select_agent_by_id(session, agent_id, *, kind=None) -> Agent` directly beneath
  `select_active_agent`. Reuses the SAME liveness filter (`revoked_at IS NULL` AND
  `last_seen_at IS NOT NULL`) and optional `kind` scope, but keys on `Agent.id == agent_id` instead of
  ordering by `last_seen_at`. Raises `NoActiveAgentError` (message naming the missing `agent_id`) when
  no row matches.
- Matches on `Agent.id` ONLY — the constrained slug PK / FK target — never on the free-form collidable
  `Agent.name` (D-01, no id-or-name fallback). The query is parameterized, so `agent_id` cannot inject
  SQL (T-72-03-01).
- RED→GREEN: six behavior cells (`# === select_agent_by_id (per-entry binding, D-01) ===`) —
  id-match returns the agent; name-only match, revoked, never-seen, absent, and wrong-kind all raise.
  Added `_seed_agent_row` helper (explicit id/name/liveness) because `seed_active_agent` always sets
  `name == agent_id` and always-online, so it cannot express the name-only / revoked / never-seen
  fixtures.

**Task 2 — per-entry binding + rewired `is_available` (`backends.py`, D-02/D-05):**
- Added a fail-loud `_agent_ref()` accessor to `ComputeAgentBackend`, mirroring `KueueBackend._kube()`:
  reads `getattr(self.config, "agent_ref", None)` and raises `ValueError` naming `self.id` when unbound
  (defense-in-depth; `_require_dispatch_fields` already guarantees it non-empty at construction).
- Rewired `is_available` to `await select_agent_by_id(session, self._agent_ref(), kind="compute")` inside
  the existing `try/except NoActiveAgentError → return False` structure. Imported `select_agent_by_id`
  alongside the existing `select_active_agent` import. The single-active pick is gone.
- Left the `select_active_agent(session, kind="fileserver")` push-initiator lookup in `dispatch`
  UNCHANGED (that is a fileserver lookup, not the compute binding); `agent_push.py` untouched (D-07).
- Updated the `_compute()` test factory to bind a real `ComputeBackend` config (agent_ref defaults to
  the backend id; `config=None` exercises the unbound fail-loud path). RED→GREEN: rewrote the
  compute `is_available` cells — bound-online True; bound-absent False; online-but-id-mismatches-ref
  False (the D-02 behavior change vs the retired pick); reads-bound-ref-not-`select_active_agent`;
  unbound fails loud.

**Task 3 — byte-identical confirmation (no source edits, D-06):**
- Re-ran the Plan-01 golden + the full backend suite + mypy. All green; the golden module was not
  touched. The matching-ref cell (`agent id == agent_ref`) returns the SAME True as the retired
  single-active pick, and the absent case returns the SAME False — the rewire is byte-identical on the
  real single-compute deploy.

## Verification Results

- `uv run pytest tests/analyze/services/test_compute_binding_golden.py tests/analyze/services/test_backends.py -q` → **40 passed**.
- `grep -n 'select_active_agent(session, kind="compute")' src/phaze/services/backends.py` → **NONE** (the single-active pick is retired); the two `kind="fileserver"` lookups remain.
- `git diff --stat src/phaze/routers/agent_push.py` → **empty** (D-07 boundary untouched).
- `git diff --stat tests/analyze/services/test_compute_binding_golden.py` → **empty** (golden byte-untouched).
- `grep -n "Agent.id ==" src/phaze/services/enqueue_router.py` → the id-keyed predicate is present; no `Agent.name` match exists in the new function (no fallback).
- `uv run mypy src/phaze/services/enqueue_router.py src/phaze/services/backends.py` → **Success: no issues found**.
- `uv run ruff check` on the two edited modules + the test file → **All checks passed**.
- `uv run pytest -k "enqueue_router or select_active_agent or select_agent_by_id"` → **24 passed** (no wider selector regression).

## must_haves Coverage

- **Per-entry binding read per-call (D-02):** met — `is_available` resolves `self.config.agent_ref` via `_agent_ref()` each call; the single-active pick is gone (Task 2, grep NONE).
- **agent_ref resolves against Agent.id, no fallback (D-01):** met — `select_agent_by_id` keys on `Agent.id ==`; the name-only-match cell raises (Task 1).
- **is_available True iff bound agent online, False when absent, never raises (D-01/D-05):** met — bound-online True / bound-absent False / mismatched False cells green; only `NoActiveAgentError` is caught.
- **Plan-01 golden stays byte-identical green (D-06):** met — golden module unchanged, re-run green in Task 3 (matching-ref True, absent False both byte-identical).

## Deviations from Plan

None - plan executed exactly as written.

## Known Stubs

None. The per-entry binding is fully wired end-to-end (`agent_ref` → `Agent.id` → live liveness query).
Per-agent dispatch / push / reconcile that consume this binding are Phase 73 scope by design, not an
unwired stub in this plan.

## Self-Check: PASSED
