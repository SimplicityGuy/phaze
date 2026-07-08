---
status: complete
phase: 72-per-entry-compute-binding-fail-fast-retirement
source: [72-01-SUMMARY.md, 72-02-SUMMARY.md, 72-03-SUMMARY.md, 72-04-SUMMARY.md]
started: 2026-07-05T00:00:00Z
updated: 2026-07-05T00:00:00Z
mode: agent-driven
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test
expected: With no PHAZE_BACKENDS_CONFIG_FILE set, ControlSettings loads a zero-config implicit-local registry (cloud disabled) — boots clean, no crash.
result: pass
evidence: Live `ControlSettings()` with no config pointer → `cloud_enabled=False`, `backends=['local']`. Existing single-config deploy path unchanged.

### 2. N distinct compute backends all accepted at boot (MCOMP-01 headline)
expected: A backends.toml declaring 3 compute backends each bound to a distinct agent_ref boots cleanly; resolve_backends yields all N compute backends alongside local.
result: pass
evidence: Live 3-compute backends.toml (agent-a/b/c) → `ControlSettings()` booted, `agent_refs=['agent-a','agent-b','agent-c']`, `resolve_backends` → 3× ComputeAgentBackend. The retired ≤1-compute fail-fast is gone.

### 3. Duplicate agent_ref fails fast at boot (D-04)
expected: A backends.toml with two compute backends binding the SAME agent_ref raises a boot-time ValueError naming the duplicate value AND both colliding backend ids.
result: pass
evidence: Live two-backend shared `agent_ref="shared-node"` → boot `ValueError: duplicate compute agent_ref(s) ['shared-node'] bound by backends {'shared-node': ['compute-a', 'compute-b']}`. Value + both ids named.

### 4. Unregistered agent_ref boots cleanly, degrades to hold (D-05)
expected: A compute backend whose agent_ref names an agent not yet in the DB boots without error (no DB session at validation); at runtime is_available returns False (holds) rather than raising.
result: pass
evidence: Live `agent_ref="never-checked-in-agent"` → `ControlSettings()` booted (`cloud_enabled=True`, no DB access); runtime `is_available=False` against the live DB (holds, no raise).

### 5. Existing single-/zero-compute deploy unchanged (D-06/D-07)
expected: The golden characterization suite is byte-identical green — a ≤1-compute registry resolves exactly as before (kind, scratch-dir, /pushed path, is_available T/F).
result: pass
evidence: `tests/analyze/services/test_compute_binding_golden.py` → 4 passed. The golden module is byte-untouched since Wave 1; ≤1-compute behavior is provably preserved.

### 6. Per-entry availability binds to THIS backend's agent (D-02)
expected: With two compute backends bound to distinct agents, each reports is_available True iff ITS OWN bound agent is online; the other backend being online does not make it available.
result: pass
evidence: Live DB seeded with only `agent-online` live → `compute-a`(bound agent-online) `is_available=True`; `compute-b`(bound agent-offline, absent) `is_available=False`. Each backend resolves its OWN agent_ref, not "the freshest compute agent."

## Summary

total: 6
passed: 6
issues: 0
pending: 0
skipped: 0

## Gaps

[none]
