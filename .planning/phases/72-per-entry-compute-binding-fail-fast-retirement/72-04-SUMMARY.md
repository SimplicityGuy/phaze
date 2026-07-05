---
phase: 72-per-entry-compute-binding-fail-fast-retirement
plan: 04
subsystem: shared/config
tags: [fail-fast, compute-backend, multi-compute, boot-guard, static-validation, MCOMP-01]
requires:
  - phaze.config.ControlSettings._validate_registry
  - phaze.config_backends.ComputeBackend.agent_ref
  - D-03 ≤1-compute fail-fast retirement (Plan 02)
provides:
  - _validate_registry fails fast (id-tagged) when two compute backends bind the same agent_ref (D-04)
  - the guard is STATIC (Counter over config values, no DB session) so an agent_ref to a not-yet-checked-in agent boots cleanly (D-05)
  - N compute backends with distinct agent_refs boot cleanly (retired ≤1-compute guard not reintroduced)
affects:
  - Plan 03 (per-entry compute binding rewire) — an unregistered agent_ref degrades to a runtime hold, never a boot error
  - Phase 73 (MCOMP-03) — per-agent scratch/dispatch resolution builds on the now-distinct-agent_ref invariant
tech-stack:
  added: []
  patterns:
    - id-tagged fail-fast boot guard mirroring the REG-05 duplicate-bucket-id Counter idiom
    - static config validation (Counter over config values) — no DB existence check at boot (D-05)
key-files:
  created: []
  modified:
    - src/phaze/config.py
    - tests/shared/config/test_backend_registry.py
decisions:
  - The duplicate-agent_ref guard lives in the container-level _validate_registry (a cross-entry invariant the per-variant ComputeBackend validator cannot see), placed immediately after the existing duplicate-bucket-id Counter check for read-locality.
  - The raised message names BOTH the duplicate agent_ref value(s) AND the colliding backend ids (a dict {ref: [ids]}), combining the bucket-id Counter idiom with the cluster-specific "name the colliding ids" style.
  - The guard skips agent_ref is None so the per-variant _require_dispatch_fields "requires an agent_ref" message is never masked (T-72-04-03).
  - Task 2 tests construct ControlSettings from a tmp backends.toml via the shared backends_toml_env conftest fixture; the unregistered-agent test uses NO DB fixture, proving the D-05 static/no-DB property structurally.
metrics:
  duration: ~7m
  completed: 2026-07-05
  tasks: 2
  files: 2
---

# Phase 72 Plan 04: Duplicate compute agent_ref boot fail-fast (D-04/D-05) Summary

The retired ≤1-compute blanket fail-fast (Plan 02) is replaced with a precise cross-entry invariant:
`ControlSettings._validate_registry` now fails fast at boot — id-tagged — when two compute backends
bind the SAME `agent_ref`, naming both the offending value and the colliding backend ids. The check is
STATIC (a `Counter` over config values, mirroring the REG-05 duplicate-bucket-id idiom), so an
`agent_ref` naming a not-yet-checked-in agent boots cleanly and degrades to a runtime hold (Plan 03)
rather than wedging startup (D-05). N compute backends with distinct `agent_ref`s boot cleanly, proving
the retired ≤1-compute guard is not reintroduced as an over-broad raise.

## What Was Built

**Task 1 — duplicate-agent_ref guard (`src/phaze/config.py::_validate_registry`):**
- Added a ~7-line block immediately after the existing duplicate-bucket-id `Counter` check: collect the
  `agent_ref` values of `ComputeBackend` entries (skipping `None`), build a `Counter`, compute
  `agent_dupes = sorted(...)`, and on any duplicate `raise ValueError` naming the duplicate value(s) and
  a `{ref: [colliding ids]}` map, citing D-04.
- STATIC / no DB (D-05): the block opens NO session and does no `Agent` existence check — an `agent_ref`
  to a dynamically-registering agent is legal at boot.
- Skips `agent_ref is None` so the per-variant `_require_dispatch_fields` "requires an agent_ref" message
  still surfaces for a missing field (T-72-04-03); this container guard only fires on genuine duplicates.

**Task 2 — registry tests (`tests/shared/config/test_backend_registry.py`):**
- Added `from phaze.config import ControlSettings` and three `backends_toml_env`-driven cells:
  1. `test_duplicate_compute_agent_ref_fails_fast_with_id` — two compute backends (`compute-a`/`compute-b`)
     both `agent_ref = "shared-node"` raise at `ControlSettings()`; asserts the message contains
     `shared-node` and both ids.
  2. `test_distinct_compute_agent_refs_boot_cleanly` — two compute backends with distinct agent_refs +
     scratch_dirs construct without raising and resolve to two `ComputeBackend` entries.
  3. `test_agent_ref_to_unregistered_agent_is_not_a_boot_error` — a single compute backend whose agent_ref
     names an agent absent from any DB constructs cleanly (`cloud_enabled is True`), using NO DB fixture —
     structural proof of the D-05 static/no-DB property.

## Verification Results

- `uv run pytest tests/shared/config/test_backend_registry.py -k "agent_ref" -q` → **4 passed** (3 new + the pre-existing per-variant test).
- `uv run pytest tests/shared/config/test_backend_registry.py -q` → **21 passed**.
- `uv run pytest tests/shared/config/ -q` → **82 passed** (no existing registry test regressed by the new guard).
- `grep -n "agent_ref" src/phaze/config.py` → shows a `Counter`-based check in `_validate_registry`; no `select(`/`session`/`Agent` in the new block (static, D-05).
- `uv run mypy src/phaze/config.py` → **Success: no issues found**.
- `uv run ruff check src/phaze/config.py` and `... tests/shared/config/test_backend_registry.py` → **All checks passed**.

## must_haves Coverage

- **D-04 (boot fail-fast, id-tagged, static, in `_validate_registry`):** met (Task 1; test 1 asserts the raise names value + ids).
- **N distinct-agent_ref compute backends boot cleanly (no over-broad guard):** met (Task 1 skips single/distinct; test 2 green).
- **D-05 (NO DB existence check; unregistered agent_ref is not a boot error):** met (Task 1 opens no session; test 3 uses no DB fixture and succeeds).
- **artifact `_validate_registry` provides Counter-based, id-tagged, static duplicate-agent_ref guard:** met.
- **key_link `_validate_registry` → Counter over compute agent_ref → id-tagged ValueError:** met (`Counter(compute_agent_refs)`).

## Deviations from Plan

None - plan executed exactly as written. (Note: the ruff-format pre-commit hook reformatted a
dict-comprehension line on the first Task-1 commit attempt; the commit was retried after the hook's
reformat with no semantic change.)

## Known Stubs

None. The guard is a complete, static cross-entry invariant. Per-agent scratch/dispatch resolution that
consumes the now-distinct-agent_ref guarantee is scheduled for Phase 73 (MCOMP-03), not an unwired stub.

## Threat Flags

None. No new network endpoint, auth path, file access, or schema change was introduced — the change is a
static in-process config validation that opens no DB session and no I/O.

## Self-Check: PASSED
