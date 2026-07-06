---
status: complete
phase: 73-per-agent-dispatch-liveness-scratch-failure-isolation
source: [73-01-SUMMARY.md, 73-02-SUMMARY.md, 73-03-SUMMARY.md, 73-04-SUMMARY.md]
started: 2026-07-05T21:30:00Z
updated: 2026-07-05T21:45:00Z
mode: agent-driven
---

## Current Test

[testing complete]

## Tests

> Phase 73 has NO UI surface (MCOMP-07's N-lane compute UI is Phase 74), so this UAT is
> agent-driven: pure/config operator behaviors (T1–T4) were driven live via a standalone
> driver against the real production code paths (`get_settings` → `resolve_backends` →
> `_build_rsync_argv` → `ComputeBackend` validation); DB-backed behaviors (T5–T8) were driven
> live via their named behavior tests against real Postgres (5433/6380). No mocks of the
> code-under-test.

### 1. Cold-start: N-compute config boots
expected: A `backends.toml` declaring `local` + TWO distinct `compute` backends (each with its own `agent_ref`/`scratch_dir`/`push_host`) loads into `ControlSettings` and `resolve_backends` yields all three — the multi-compute registry boots clean.
result: pass
observed: `resolve_backends ids=['gcp-x86', 'local', 'oci-a1']`

### 2. MCOMP-03 — per-agent push destination
expected: A file dispatched to compute-A is pushed to A's host/scratch; a file dispatched to compute-B to B's — the rsync `remote_dest` is resolved per-agent from the recorded backend, not a single global. Backend B, which omits `ssh_user`, falls back to the fileserver's user.
result: pass
observed: A=`phaze@oci-a1.push.example:/srv/scratch-a/<uuid>.flac`  B=`fileserver-user@gcp-x86.push.example:/srv/scratch-b/<uuid>.flac` — distinct host, scratch, AND user (A uses its own `ssh_user=phaze`; B falls back to `fileserver-user`).

### 3. MCOMP-03/D-01 — missing push_host fail-fast
expected: A `compute` backend declared without a `push_host` fails construction with an id-tagged error (never silently builds a `None:...` remote spec).
result: pass
observed: `ValueError: 1 validation error for ComputeBackend` — message names `push_host` and the offending backend id `'broken'`.

### 4. T-73-04 — argv-injection safety
expected: A `dest_host` containing shell metacharacters (`host; rm -rf /`) is rejected at the schema layer, and the built rsync argv carries a `--` terminator before the positional operands so no destination can smuggle an rsync flag.
result: pass
observed: `metachar_rejected=True, argv_terminator_present=True`.

### 5. MCOMP-02 — per-agent liveness
expected: With two compute backends bound to distinct agents, only the ONLINE bound agent's backend reports `is_available` True; the offline-bound backend is unavailable (the file holds or spills, never dispatches to a dead agent).
result: pass
observed: live behavior test `test_mcomp02_two_compute_backends_only_the_online_bound_agent_is_available` passed against real Postgres.

### 6. MCOMP-04 — rank/cap load-spread
expected: The tiered drain spreads long files across N compute agents by rank (free arm64 preferred over paid/trial x86) and per-agent `cap`, spilling to the next-eligible backend when one is at cap.
result: pass
observed: live behavior test `test_mcomp04_compute_rank_cap_spread_prefers_free_arm64_then_spills_to_paid_x86` passed.

### 7. MCOMP-05 — one-flaky-agent isolation
expected: One flaky/offline compute agent degrades to 0 slots without failing the drain tick or blocking dispatch to healthy compute agents.
result: pass
observed: live behavior test `test_mcomp05_flaky_compute_backend_degrades_to_zero_slots_healthy_compute_lane_still_dispatches` passed.

### 8. MCOMP-06 — no cross-attribution + CR-01 hardening
expected: `/pushed` + `/mismatch` route off the RECORDED `cloud_job.backend_id` so a result is attributed to the agent that analyzed it; a wrong reporter is rejected 403 with no terminalization; and (CR-01 fix) an over-cap `/mismatch` on an already-advanced file is an idempotent no-op, not a clobber.
result: pass
observed: live behavior tests passed — `test_mismatch_wrong_reporter_rejected_403` (403 reporter gate), `test_pushed_holds_when_backend_id_unresolvable` (backend_id-scoped hold), `test_push_mismatch_over_cap_does_not_clobber_advanced_file` (CR-01 PUSHING-CAS no-clobber), `test_push_mismatch_over_cap_spills_to_awaiting_cloud_and_clears_ledger`.

## Summary

total: 8
passed: 8
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps

[none — all 8 operator-facing behaviors verified live]

> Deployment-gated (not a gap, tracked in 73-VALIDATION.md Manual-Only): live multi-agent rsync
> transfer over Tailscale to real hosts needs N deployed compute hosts + SSH key auth. Proven
> in-process here by the payload/argv correctness checks (T2/T4); the byte-over-the-wire leg
> unblocks at rollout.
