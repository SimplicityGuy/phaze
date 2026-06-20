---
phase: 45-scheduling-ledger-for-orphan-recovery
verified: 2026-06-19T23:30:00Z
status: passed
score: 6/6 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 5/6
  gaps_closed:
    - "CR-01: scan_live_set no-match path report_scan_terminal is now wrapped in try/except with not-job.retryable discipline — swallow+log on terminal attempt so no_matches COMPLETE returns; re-raise on retryable so the row survives the real retry (scan.py:106-113)"
    - "CR-02: extract_file_metadata and fingerprint_file now have control-side POST /{file_id}/failed endpoints that clear the deterministic ledger key, plus agent-worker terminal guards that ack only on the retries-exhausted attempt then re-raise — recovery regression tests prove a cleared row is never re-enqueued even though the domain predicate cannot fire for it"
  gaps_remaining: []
  regressions: []
---

# Phase 45: Scheduling Ledger for Orphan Recovery Verification Report

**Phase Goal:** Add a durable scheduling ledger that records "this `<task>:<natural_id>` was enqueued" at the single `before_enqueue` chokepoint and clears it on completion AND terminal failure, so recovery re-queues exactly `ledger − live saq_jobs keys − completed` through the existing keyed producers — never the complement-of-done domain backlog that detonated the queue (~11.4k never-scheduled files) in the 2026-06-18 incident.
**Verified:** 2026-06-19T23:30:00Z
**Status:** passed
**Re-verification:** Yes — after gap closure (plans 45-05 CR-01 + 45-06 CR-02)

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | L-01: A keyed enqueue upserts one scheduling_ledger row at the single before_enqueue chokepoint | VERIFIED | `apply_deterministic_key` (deterministic_key.py:117-140) calls `upsert_ledger_entry` via `getattr(job.queue, "ledger_sessionmaker", None)` + function-local lazy import; no-op on agent queue (absent handle); L-01 satisfied |
| 2 | L-02: Ledger cleared on completion AND terminal failure — controller stages via after_process, agent stages via control-side callbacks | VERIFIED | Controller stages (after_process TERMINAL_STATUSES gate): VERIFIED. analyze success + /failed (agent_analysis.py): VERIFIED. scan_live_set match (create_tracklist) + terminal-ack (agent_tracklists.py): VERIFIED. No-match path (scan.py:106-113): VERIFIED — try/except with not-job.retryable guard now swallows+logs on terminal attempt, re-raises on retryable (CR-01 CLOSED). extract_file_metadata: success (agent_metadata.py:73) + new /failed endpoint (agent_metadata.py:78-105) + worker guard (metadata_extraction.py:65-75): VERIFIED (CR-02 CLOSED). fingerprint_file: success (agent_fingerprint.py:54) + new /failed endpoint (agent_fingerprint.py:59-89) + worker guard (fingerprint.py:55-66): VERIFIED (CR-02 CLOSED). L-02 fully satisfied. |
| 3 | L-03: Recovery re-queues `ledger − live keys − completed` via existing keyed producers — never the complement-of-done backlog | VERIFIED | `recover_orphaned_work` (reenqueue.py:225-298) reads `get_ledger_rows` + `get_live_job_keys` + `_build_done_sets`; orphaned set is explicit complement; complement-of-done SWEEP queries removed; replays via keyed producers with stored payload |
| 4 | L-04: Idempotent startup backfill from live saq_jobs | VERIFIED | `backfill_ledger_from_saq_jobs` (reenqueue.py:341-395) uses `insert_ledger_if_absent` (ON CONFLICT DO NOTHING); controller.py:144 calls it before `recover_orphaned_work`; SAVEPOINT degrade-safe |
| 5 | L-05: Control-only boundary preserved (agent worker stays Postgres-free) | VERIFIED | DB access in deterministic_key.py strictly behind `getattr(job.queue, "ledger_sessionmaker", None)` + function-local import guard; scan.py, metadata_extraction.py, fingerprint.py, and agent_client.py import no phaze.database/phaze.models/sqlalchemy at runtime; the new report_metadata_failed/report_fingerprint_failed client methods are httpx-only with function-local response imports (noqa: PLC0415); test_task_split.py gate covers all agent modules |
| 6 | L-06: Reversible Alembic migration 022 + 85% coverage | VERIFIED | Migration 022 (revision="022", down_revision="021") creates/drops `scheduling_ledger`; no `saq_jobs` reference in executable DDL; 1988 tests pass, 97.49% total coverage; reenqueue.py at 100% |

**Score:** 6/6 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/models/scheduling_ledger.py` | SchedulingLedger ORM model | VERIFIED | `class SchedulingLedger(TimestampMixin, Base)` at line 46; PK=key, function, routing, payload JSONB, enqueued_at, TimestampMixin |
| `alembic/versions/022_add_scheduling_ledger.py` | Reversible migration 022 | VERIFIED | revision="022", down_revision="021"; creates/drops scheduling_ledger; no saq_jobs in DDL body |
| `src/phaze/services/scheduling_ledger.py` | upsert/insert-if-absent/clear/read/routing | VERIFIED | All five functions present; upsert uses ON CONFLICT DO UPDATE; insert-if-absent uses ON CONFLICT DO NOTHING |
| `src/phaze/services/pipeline.py` | get_live_job_keys SAVEPOINT-isolated | VERIFIED | Function present; SAVEPOINT pattern cloned from get_stage_busy_counts |
| `src/phaze/tasks/_shared/deterministic_key.py` | WRITE hook + CLEAR hook wired | VERIFIED | apply_deterministic_key upserts (lines 117-140); increment_completed clears on TERMINAL_STATUSES (lines 174-190); both use getattr + lazy import |
| `src/phaze/routers/agent_analysis.py` | clear_ledger_entry in put_analysis AND report_analysis_failed | VERIFIED | Lines 196 (success) and 225 (terminal failure) both call clear_ledger_entry before session.commit() |
| `src/phaze/routers/agent_metadata.py` | clear_ledger_entry in put_metadata AND new /failed endpoint | VERIFIED | Line 73: success path clears extract_file_metadata:{file_id}; lines 78-105: new report_metadata_failed endpoint clears same key control-side (CR-02 CLOSED) |
| `src/phaze/routers/agent_fingerprint.py` | clear_ledger_entry in put_fingerprint AND new /failed endpoint | VERIFIED | Line 54: success path clears fingerprint_file:{file_id}; lines 59-89: new report_fingerprint_failed endpoint clears same single-per-file key (CR-02 CLOSED) |
| `src/phaze/routers/agent_tracklists.py` | scan_live_set clear in create_tracklist + ack endpoint | VERIFIED | Match path (line 168) clears in owner-path transaction; terminal-ack endpoint `ack_scan_terminal` exists (POST /{file_id}/scanned) |
| `src/phaze/tasks/scan.py` | report_scan_terminal guarded on both no-match AND match-failure paths | VERIFIED | No-match path (lines 106-113): try/except with swallow+log on terminal attempt, re-raise on retryable (CR-01 CLOSED); match-failure handler (lines 149-152): gated on not job.retryable |
| `src/phaze/tasks/metadata_extraction.py` | terminal guard calling report_metadata_failed on not-retryable attempt | VERIFIED | Lines 65-75: try/except wrapping extract_tags + put_metadata call; acks via report_metadata_failed only on job is not None and not job.retryable, then re-raises; Postgres-free |
| `src/phaze/tasks/fingerprint.py` | terminal guard calling report_fingerprint_failed on not-retryable attempt | VERIFIED | Lines 55-66: try/except wrapping orchestrator.ingest_all + PUT loop; acks via report_fingerprint_failed only on terminal attempt then re-raises; Postgres-free |
| `src/phaze/services/agent_client.py` | report_metadata_failed + report_fingerprint_failed httpx-only client methods | VERIFIED | Lines 284-298: report_metadata_failed POSTs to /metadata/{file_id}/failed, function-local response import, no DB import. Lines 300-315: report_fingerprint_failed POSTs to /fingerprints/{file_id}/failed, same httpx-only pattern |
| `src/phaze/tasks/reenqueue.py` | recover_orphaned_work drives off ledger; backfill_ledger_from_saq_jobs present | VERIFIED | get_ledger_rows (line 266), get_live_job_keys (line 267), is_domain_completed (line 270), _DOMAIN_COMPLETED_STAGES (line 107), backfill_ledger_from_saq_jobs (line 341) — all present and wired |
| `src/phaze/tasks/controller.py` | backfill_ledger_from_saq_jobs before recover_orphaned_work in startup | VERIFIED | Line 144: backfill called in startup; line 40 import confirmed |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| deterministic_key.py | scheduling_ledger service | getattr(job.queue, "ledger_sessionmaker", None) then upsert_ledger_entry | WIRED | Lines 125-136: handle-gated lazy import and upsert |
| deterministic_key.py | saq.job.TERMINAL_STATUSES | increment_completed clears when status in TERMINAL_STATUSES | WIRED | Line 42 imports TERMINAL_STATUSES; line 174 gates the clear |
| controller.py | queue_factory.py | ledger_sessionmaker attached to controller queue and AgentTaskRouter | WIRED | controller.py line 112 sets queue.ledger_sessionmaker; line 121 passes ledger_sessionmaker to AgentTaskRouter |
| agent_analysis.py | scheduling_ledger service | clear_ledger_entry before commit in put_analysis AND report_analysis_failed | WIRED | Lines 196, 225 |
| agent_metadata.py | scheduling_ledger service | clear_ledger_entry in put_metadata (success) AND report_metadata_failed (/failed endpoint) | WIRED | Line 73 (success); lines 101-102 (terminal failure — CR-02 CLOSED) |
| agent_fingerprint.py | scheduling_ledger service | clear_ledger_entry in put_fingerprint (success) AND report_fingerprint_failed (/failed endpoint) | WIRED | Line 54 (success); lines 85-86 (terminal failure — CR-02 CLOSED) |
| agent_tracklists.py | scheduling_ledger service | clear_ledger_entry in create_tracklist owner-path + ack_scan_terminal endpoint | WIRED | Lines 168, 203 |
| scan.py (no-match path) | agent_tracklists.py | report_scan_terminal in try/except on no-match path; swallow+log on terminal attempt, re-raise on retryable | WIRED | Lines 106-113: try/except guard (CR-01 CLOSED) |
| scan.py (match-failure path) | agent_tracklists.py | report_scan_terminal gated on not job.retryable in match-failure handler | WIRED | Lines 149-152: gated on not job.retryable |
| metadata_extraction.py | agent_client.py | report_metadata_failed on terminal attempt only | WIRED | Line 74: report_metadata_failed called inside try/except gated on not job.retryable |
| fingerprint.py | agent_client.py | report_fingerprint_failed on terminal attempt only | WIRED | Line 65: report_fingerprint_failed called inside try/except gated on not job.retryable |
| agent_client.py | agent_metadata.py POST /{file_id}/failed | report_metadata_failed POSTs to /metadata/{file_id}/failed | WIRED | Lines 294-298: authenticated POST to the new endpoint |
| agent_client.py | agent_fingerprint.py POST /{file_id}/failed | report_fingerprint_failed POSTs to /fingerprints/{file_id}/failed | WIRED | Lines 311-314: authenticated POST to the new endpoint |
| reenqueue.py | scheduling_ledger service | get_ledger_rows drives recovery | WIRED | Line 79 import, line 266 call |
| reenqueue.py | pipeline.py | get_live_job_keys as live-exclusion set | WIRED | Line 76 import, line 267 call |
| controller.py | reenqueue.py | backfill_ledger_from_saq_jobs before recover_orphaned_work in startup | WIRED | Line 40 import, line 144 call |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|--------------|--------|--------------------|--------|
| reenqueue.py recover_orphaned_work | orphaned (ledger rows minus live minus domain-completed) | get_ledger_rows + get_live_job_keys + _build_done_sets | FLOWING — terminally-failed metadata/fingerprint rows are now cleared by the /failed endpoints before recovery runs; recovery regression tests confirm a cleared row is never replayed even when the file remains in the pending set | VERIFIED |

### Behavioral Spot-Checks

Step 7b: SKIPPED — tests are the primary verification surface; running the app requires Docker services (PostgreSQL, Redis). The 1988-test suite is confirmed passing (97.49% coverage) per plan 45-06 SUMMARY.

### Probe Execution

Step 7c: No probe scripts declared or found for this phase.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|---------|
| L-01 | 45-01 | Durable ledger written at the single before_enqueue chokepoint | SATISFIED | apply_deterministic_key upserts control-side; agent queue no-ops |
| L-02 (controller) | 45-01 | Ledger cleared on terminal status via after_process hook | SATISFIED | increment_completed clears on TERMINAL_STATUSES, not on QUEUED retry |
| L-02 (analyze) | 45-02 | process_file ledger cleared in put_analysis (success) AND report_analysis_failed (terminal failure) | SATISFIED | Both handlers clear in-transaction |
| L-02 (scan match) | 45-02 | scan_live_set cleared via create_tracklist owner-path | SATISFIED | agent_tracklists.py line 168 |
| L-02 (scan no-match — CR-01) | 45-05 | scan_live_set no-match ack guarded with not-job.retryable discipline | SATISFIED | scan.py:106-113 try/except: swallow+log on terminal attempt, re-raise on retryable (CR-01 CLOSED) |
| L-02 (scan failure — CR-01) | 45-02 | scan_live_set match-failure ack gated on not job.retryable | SATISFIED | scan.py:149-152 (pre-existing; CR-01 confirmed no regression) |
| L-02 (metadata terminal — CR-02) | 45-06 | extract_file_metadata cleared on terminal failure via /failed endpoint + worker guard | SATISFIED | agent_metadata.py:78-105 new endpoint; metadata_extraction.py:65-75 worker guard; clear key extract_file_metadata:{file_id} from PATH only (CR-02 CLOSED) |
| L-02 (fingerprint terminal — CR-02) | 45-06 | fingerprint_file cleared on terminal failure via /failed endpoint + worker guard | SATISFIED | agent_fingerprint.py:59-89 new endpoint; fingerprint.py:55-66 worker guard; clear key fingerprint_file:{file_id} from PATH only, single per-file not per-engine (CR-02 CLOSED) |
| L-03 | 45-03 | Recovery re-queues ledger minus live minus completed via keyed producers | SATISFIED | recover_orphaned_work rewrites; complement-of-done sweep queries removed |
| L-04 | 45-04 | Idempotent startup backfill from live saq_jobs | SATISFIED | backfill_ledger_from_saq_jobs with DO NOTHING, in startup before recovery |
| L-05 | 45-01/02/03/04/05/06 | Control-only boundary preserved; agent worker stays Postgres-free | SATISFIED | getattr gate + function-local lazy imports; new client methods are httpx-only with function-local response imports; test_task_split.py gate green |
| L-06 | 45-01 | Reversible Alembic migration 022 + 85% coverage | SATISFIED | Migration 022 (down_revision 021); 97.49% total coverage; 1988 tests pass |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| src/phaze/tasks/scan.py | 151 | `await api.report_scan_terminal(payload.file_id)` unguarded inside `except` block on match-failure path | WARNING (WR-01) | Pre-existing codebase pattern from functions.py:183-189 (process_file). Double-failure scenario: if the ack call also raises after the primary task failure, the ack exception propagates and the `raise` is never reached, leaving the ledger row uncleared. Requires controller to be unreachable for BOTH the primary failure AND the ack in the same SAQ attempt. Temporary and self-healing (next recovery pass re-enqueues; next successful ack clears). Not a novel defect introduced in this phase. Flagged as WR-01 in 45-REVIEW.md (critical: 0). |
| src/phaze/tasks/metadata_extraction.py | 74 | `await api.report_metadata_failed(payload.file_id)` unguarded inside `except` block | WARNING (WR-01) | Same double-failure pattern. New code replicates the pre-existing functions.py convention rather than introducing a novel defect. Same self-healing characteristic. |
| src/phaze/tasks/fingerprint.py | 65 | `await api.report_fingerprint_failed(payload.file_id)` unguarded inside `except` block | WARNING (WR-01) | Same double-failure pattern. Same self-healing characteristic. |
| src/phaze/schemas/agent_metadata.py | 48 | `cleared: bool` instead of `Literal[True]` | WARNING (WR-02) | Invariant enforced only by call-site literal, not by schema type. A future refactor omitting `cleared=True` would silently produce `cleared=False`. Flagged as WR-02 in 45-REVIEW.md. |
| src/phaze/schemas/agent_fingerprint.py | 38 | `cleared: bool` instead of `Literal[True]` | WARNING (WR-02) | Same schema type-safety gap. |

**Debt marker gate:** No TBD, FIXME, or XXX markers found in modified files.

### Human Verification Required

None required — all gaps are observable by code inspection, and behavioral contracts are proven by the test suite (1988 tests, 97.49% coverage).

### Gaps Summary

Both gaps from the initial verification are closed:

**Gap 1 — CR-01 (scan_live_set no-match path, scan.py) — CLOSED:**
The `report_scan_terminal` call on the no-match return is now wrapped in try/except at scan.py:106-113. On the terminal attempt (job is not None and not job.retryable), the exception is swallowed and logged, and the function still returns `{"status": "no_matches"}` — the `scan_live_set:<file_id>` row is not leaked. On a retryable attempt or with no job in ctx, the exception re-raises so SAQ retries and the row survives. Three test cases cover all paths; commit `1553c25`.

**Gap 2 — CR-02 (extract_file_metadata + fingerprint_file terminal failure) — CLOSED:**
Both stages now have control-side `POST /{file_id}/failed` endpoints (agent_metadata.py:78-105, agent_fingerprint.py:59-89) that clear the deterministic ledger key (`extract_file_metadata:{file_id}` / `fingerprint_file:{file_id}`) from the PATH `file_id` only. New `report_metadata_failed` / `report_fingerprint_failed` httpx-only client methods POST to these endpoints. The agent-worker tasks are wrapped in try/except that acks only on the retries-exhausted terminal attempt then re-raises, mirroring `process_file`. Recovery regression tests (`test_cleared_metadata_row_is_not_reenqueued`, `test_cleared_fingerprint_row_is_not_reenqueued`) prove that after the /failed ack clears the row, `recover_orphaned_work` does NOT re-enqueue the file even though it stays in the pending set (so `is_domain_completed` can never fire) — the clear closes the loop. Commits `79e9964` + `effc15e`.

**Outstanding code review warnings (not blocking L-02 goal):**
- WR-01: double-failure leaves ledger row uncleared (temporary, self-healing, pre-existing pattern)
- WR-02: `cleared: bool` should be `Literal[True]` in failure response schemas

These are quality improvements, not goal blockers. The 45-REVIEW.md classified them as `critical: 0, warning: 2`.

---

_Verified: 2026-06-19T23:30:00Z_
_Verifier: Claude (gsd-verifier)_
