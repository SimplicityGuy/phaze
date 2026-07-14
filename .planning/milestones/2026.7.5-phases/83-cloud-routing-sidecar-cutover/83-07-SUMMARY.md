---
phase: 83-cloud-routing-sidecar-cutover
plan: 07
subsystem: cloud-routing
tags: [backends, agent_s3, agent_push, cloud_job, CAS, single-writer, anti-drift, gap-closure]
requires:
  - "hold_awaiting_cloud() shared awaiting writer (83-01) — the helper this plan extends"
  - "report_upload_failed / report_push_mismatch over-cap CAS spill paths (83-04) — the two inline writers this plan consolidates"
provides:
  - "hold_awaiting_cloud dual-mode CAS-preserving writer: expect_status / clear_cloud_phase kwargs, returns bool"
  - "report_upload_failed over-cap spill routed through hold_awaiting_cloud (expect_status=(uploading,uploaded), clear_cloud_phase=True)"
  - "report_push_mismatch over-cap spill routed through hold_awaiting_cloud (expect_status=(submitted,), no cloud_phase touch)"
  - "hermetic AST anti-drift test asserting services/backends.py is the sole awaiting .values(...) writer"
affects:
  - "Any future cloud-routing spill path — the D-02 single-writer invariant is now self-enforcing via the AST guard"
tech-stack:
  added: []
  patterns:
    - "dual-mode helper: unconditional upsert (hold) vs rowcount-guarded CAS (spill) selected by expect_status"
    - "NULL-GUARD short-circuit (file is not None and await helper(...)) keeps an absent FileRecord a FULL no-op, not an AttributeError"
    - "hermetic ast.parse source scan keyed on .values(status=<awaiting>) — WRITE detection that ignores WHERE/DELETE reads"
key-files:
  created:
    - "tests/analyze/services/test_single_awaiting_writer.py"
  modified:
    - "src/phaze/services/backends.py"
    - "src/phaze/routers/agent_s3.py"
    - "src/phaze/routers/agent_push.py"
    - "tests/analyze/services/test_backends.py"
    - "tests/agents/routers/test_agent_s3.py"
    - "tests/agents/routers/test_agent_push.py"
decisions:
  - "D-02: hold_awaiting_cloud is the ONLY writer of cloud_job.status='awaiting'; both over-cap spill paths route through it — no inline update(CloudJob).values(status=AWAITING...) survives outside services/backends.py (AST-enforced)"
  - "The naive upsert-swap was FORBIDDEN: the spill branch keeps the rowcount-guarded CAS (returns False on a 0-row advanced-file match) so SC#2 / T-83-PUSH-CLOBBER stays closed; the spill-CAS-MISS unit test goes RED if the guard is dropped"
  - "D-03: spill re-stamp retains attempts=cloud_submit_max_attempts; 'awaiting' stays out of backends.IN_FLIGHT"
  - "D-09/D-11: report_upload_failed CAS anchor (status IN {uploading,uploaded}) + pg_advisory_xact_lock RMW serialization untouched"
  - "D-10: FULL no-op on CAS-miss preserved byte-for-byte on both spill paths"
  - "D-12: each spill CAS anchors on its own kind's status set — s3 clears cloud_phase (clear_cloud_phase=True), push must NOT touch cloud_phase (flag omitted)"
  - "Landmine L1: hold_awaiting_cloud never commits in either mode; the hold-path file.state=AWAITING_CLOUD dual-write (D-00c) stays ONLY on the expect_status=None branch; callers own their commit + gated FileRecord write"
  - "NULL-GUARD: an absent FileRecord (unreachable — cloud_job.file_id FKs files.id) takes the FULL no-op (cleared=False), never a 404 (agent-callback contract) and never an AttributeError"
metrics:
  duration: "~40m"
  completed: "2026-07-09"
  tasks: 3
  files: 7
---

# Phase 83 Plan 07: Single Awaiting-Writer Consolidation (Gap Closure) Summary

Realized the LOCKED **D-02** single-writer invariant in code: `hold_awaiting_cloud` is now the ONLY
writer of `cloud_job.status='awaiting'`, reused by the hold path (`trigger_analysis`) and both over-cap
spill paths (`report_upload_failed`, `report_push_mismatch`) — closing the one failed Phase-83 must-have
(WR-03 / gap `gaps_found`) without weakening any of the D-09/D-10/D-11/D-12 CAS guards Phase 83 shipped.

## What Was Built

- **`hold_awaiting_cloud` extended to a CAS-preserving dual-mode writer** (`services/backends.py`, Task 1):
  - New signature `(session, file, *, attempts=0, expect_status: Sequence[str] | None = None,
    clear_cloud_phase: bool = False) -> bool`.
  - **Hold mode** (`expect_status is None`): today's body verbatim — `file.state = AWAITING_CLOUD`
    (D-00c dual-write) + the `pg_insert(CloudJob).on_conflict_do_update` unconditional upsert — then
    `return True`.
  - **Spill mode** (`expect_status` set): a rowcount-guarded CAS ONLY —
    `update(CloudJob).where(file_id==…, status.in_(expect_status)).values(...)` re-stamping to
    `status='awaiting'` with `attempts` from the arg, and `cloud_phase=None` iff `clear_cloud_phase`
    (built so the key is ABSENT when the flag is False). Reads rowcount via `cast("CursorResult[Any]", …)`
    and `return res.rowcount > 0`. Writes NO `file.state`, touches NO FileRecord, NEVER commits.

- **Both over-cap spill paths routed through the helper** (`routers/agent_s3.py`, `routers/agent_push.py`,
  Task 2): the inline `update(CloudJob).values(status=AWAITING…)` CAS blocks are replaced by
  `cleared = file is not None and await hold_awaiting_cloud(...)`. The FULL no-op (`if not cleared:`) and
  the gated success branch (FileRecord dual-write + S3 cleanup / ledger clear) are kept byte-identical, so
  the emitted SQL and response bodies are unchanged. `grep -c "CloudJobStatus.AWAITING.value"` now returns
  **0** for both routers.

- **Anti-drift AST guard** (`tests/analyze/services/test_single_awaiting_writer.py`, Task 3): a hermetic
  `ast.parse` scan of `src/phaze/**.py` that flags any `.values(status=<AWAITING | "awaiting">)` WRITE and
  asserts the writer set equals exactly `{services/backends.py}`. Keying on `.values(...)` means the
  drain / count-card / shadow-invariant `.where(status == AWAITING)` READs and the D-14 reaper
  `delete(...).where(...)` are correctly NOT flagged (verified programmatically).

## Verification

- `tests/analyze/services/test_backends.py` — **50 passed** (incl. 4 new: hold-branch return, spill CAS
  hit/miss no-op, cloud_phase preserve-vs-clear).
- `tests/agents/routers/test_agent_s3.py` + `test_agent_push.py` — **41 passed** (39 pre-existing +
  2 new NULL-GUARD tests); the SC#2 CAS-noop tests stayed green.
- Full **agents bucket — 452 passed** (SC#2 / T-83-PUSH-CLOBBER regression surface).
- `tests/integration/test_drain_double_dispatch.py` + `test_shadow_compare.py` — **39 passed** (SC#3 hard
  gate + shadow invariant, both unaffected).
- `tests/analyze/services/test_single_awaiting_writer.py` — **1 passed**; detector confirmed discriminating
  (fires on backends.py + a synthetic inline-drift snippet, NOT on agent_s3/agent_push as shipped, NOT on
  the agent_analysis reaper DELETE).
- Hold-path callers (`trigger_analysis`, staging cron) — **21 passed**.
- `uv run ruff check .` clean; `uv run mypy .` clean (205 source files).

## Deviations from Plan

None — plan executed exactly as written. Rules 1–4 were not triggered; the refactor was behavior-preserving
and every pre-existing test stayed green without edits.

## Success Criteria

- [x] All 3 tasks executed, each committed individually.
- [x] `hold_awaiting_cloud` is the SOLE writer of `cloud_job.status='awaiting'`;
      `grep -c "CloudJobStatus.AWAITING.value"` returns 0 for both routers.
- [x] Spill CAS-miss FULL no-op preserved; a unit test goes RED if the CAS is replaced by an unconditional
      upsert (`test_hold_awaiting_cloud_spill_cas_miss_is_full_noop`).
- [x] AST anti-drift test passes and does NOT false-positive on WHERE-clause readers or the D-14 reaper DELETE.
- [x] NULL-GUARD implemented and tested (FileRecord absent → FULL no-op, `cleared=False`, no AttributeError)
      in both routers.
- [x] All pre-existing regression tests stay green.
- [x] No modifications to STATE.md / ROADMAP.md / plans 83-01..83-06.

## Self-Check: PASSED

- FOUND: src/phaze/services/backends.py, src/phaze/routers/agent_s3.py, src/phaze/routers/agent_push.py
- FOUND: tests/analyze/services/test_single_awaiting_writer.py (created)
- FOUND: commits 7ae8e141 (Task 1), 192e0ab8 (Task 2), 9e5a520e (Task 3)
