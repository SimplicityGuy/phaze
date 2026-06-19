---
phase: 45-scheduling-ledger-for-orphan-recovery
verified: 2026-06-19T00:00:00Z
status: gaps_found
score: 5/6 must-haves verified
overrides_applied: 0
gaps:
  - truth: "Ledger cleared on completion AND terminal failure for every agent stage — extract_file_metadata and fingerprint_file have no terminal-failure callback, and their domain-completed predicate cannot fire for a terminally-FAILED file whose state was never advanced past its gate"
    status: failed
    reason: |
      L-02 requires the ledger to be cleared on completion AND terminal failure. For
      extract_file_metadata and fingerprint_file the ONLY ledger clear is in the success PUT
      handler. There is no /failed callback for either stage. The domain-completed predicate
      (the documented 'Plan 03 residual gap' secondary net) is supposed to catch these, but
      it is structurally unable to do so for the exact case recovery must handle:

      - extract_file_metadata: is_domain_completed returns True iff the file is NOT in
        get_metadata_pending_files, which returns ALL music/video files regardless of state
        (pipeline.py:670). A file that was scheduled, failed terminally (retries exhausted,
        no callback), and whose state was never advanced IS still in the pending set — so
        is_domain_completed returns False — so recovery re-enqueues it on every pass
        indefinitely. The row is never cleared.

      - fingerprint_file: get_fingerprint_pending_files returns METADATA_EXTRACTED files PLUS
        files with FingerprintResult(status="failed"). A file that failed fingerprinting
        terminally but is still METADATA_EXTRACTED remains in the pending set — re-enqueued
        forever. Even if a FingerprintResult(status="failed") was written, that row is also in
        the pending set (pipeline.py:692-697), still causing perpetual re-enqueue.

      The reenqueue.py docstring itself labels this "Plan 02 residual gap" and "PRIMARY net for
      the metadata/fingerprint residual gap." The net cannot fire for terminally-failed jobs
      because the domain predicate's logic is "absent from pending == done" and terminally-failed
      files remain in the pending set by construction. The result is an unbounded re-enqueue loop
      per recovery pass for any terminally-failed metadata/fingerprint job (bounded per-pass by
      deterministic-key dedup so it won't double the queue, but it will re-enqueue every recovery
      and never drain).

      This is the same class of defect the phase was created to fix, narrowed to the two
      no-failed-callback agent stages. L-02 reads "cleared on completion AND terminal failure";
      for these two stages, terminal failure does not clear the row and the secondary net cannot
      compensate.
    artifacts:
      - path: "src/phaze/routers/agent_metadata.py"
        issue: "No /failed callback; terminal failure of extract_file_metadata leaves the ledger row and the domain-completed predicate cannot exclude it from recovery"
      - path: "src/phaze/routers/agent_fingerprint.py"
        issue: "No /failed callback; terminal failure of fingerprint_file leaves the ledger row and the domain-completed predicate cannot exclude it from recovery"
      - path: "src/phaze/tasks/reenqueue.py"
        issue: "is_domain_completed for extract_file_metadata/fingerprint_file uses absent-from-pending as 'done', but terminally-failed files remain in the pending set — the predicate can never fire for these rows"
    missing:
      - "A /failed callback for extract_file_metadata and fingerprint_file (mirroring report_analysis_failed and ack_scan_terminal) that clears the ledger row, OR"
      - "Rework is_domain_completed for these two stages to consult a real terminal-failure signal (e.g. a failed-status result row) rather than complement-of-pending, which does not distinguish 'done' from 'terminally failed still in pending'"

  - truth: "scan_live_set no-match terminal-ack (scan.py) guards the ack call so a controller hiccup on the no-match path turns into a retrying failure that leaks the ledger row"
    status: failed
    reason: |
      CR-01 from the code review is confirmed by direct inspection of scan.py lines 94-100:

        matches = await orchestrator.combined_query(payload.original_path)
        if not matches:
            await api.report_scan_terminal(payload.file_id)   # <-- UNGUARDED
            return {"file_id": str(payload.file_id), "status": "no_matches"}

      report_scan_terminal routes through the tenancy funnel and raises AgentApiServerError if
      the controller is down/5xx after retries. When it raises on the no-match path:
      - The function never returns no_matches; the job records a FAILED attempt and SAQ retries it.
      - The scan_live_set:<file_id> ledger row is NOT cleared (the ack never completed).
      - On a retryable attempt the row survives for the retry, which is correct, but the job
        retries the WHOLE scan (including orchestrator.combined_query) — it does not resume from
        the ack. If the controller happens to be down when a legitimate no-match returns, the job
        retries unnecessarily.
      - On the terminal (retries-exhausted) attempt the controller is still unavailable, the ack
        still raises, and the ledger row is NEVER cleared. Recovery will re-enqueue this
        scan_live_set on every pass indefinitely.

      The match-path exception handler (scan.py:129-138) correctly gates its ack on
      `not job.retryable` before re-raising. The no-match path has no such discipline.
      scan_live_set is documented as live-keys-only (no domain predicate) because the ack
      was supposed to clear it on every outcome. That invariant is broken for the controller-down
      no-match case.
    artifacts:
      - path: "src/phaze/tasks/scan.py"
        issue: "Lines 99-100: await api.report_scan_terminal(payload.file_id) has no try/except wrapper; a controller hiccup on the no-match path raises, preventing the no_matches return and leaving the ledger row uncleared"
    missing:
      - "Wrap the no-match report_scan_terminal call in try/except; on a retryable attempt re-raise so the row survives for the real retry; on a terminal attempt (not job.retryable) swallow and log so the no-match COMPLETE still returns (mirroring the match-path discipline at scan.py:129-138)"
---

# Phase 45: Scheduling Ledger for Orphan Recovery Verification Report

**Phase Goal:** Add a durable scheduling ledger that records "this `<task>:<natural_id>` was enqueued" at the single `before_enqueue` chokepoint and clears it on completion AND terminal failure, so recovery re-queues exactly `ledger − live saq_jobs keys − completed` through the existing keyed producers — never the complement-of-done domain backlog.
**Verified:** 2026-06-19
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | L-01: A keyed enqueue upserts one scheduling_ledger row at the single before_enqueue chokepoint | VERIFIED | `apply_deterministic_key` (deterministic_key.py:117-140) calls `upsert_ledger_entry` via `getattr(job.queue, "ledger_sessionmaker", None)` + function-local lazy import; no-op on agent queue (absent handle); L-01 satisfied |
| 2 | L-02: Ledger cleared on completion AND terminal failure — controller stages via after_process, agent stages via control-side callbacks | FAILED | Controller stages (after_process TERMINAL_STATUSES gate, deterministic_key.py:174-190): VERIFIED. analyze success + /failed callback (agent_analysis.py): VERIFIED. scan_live_set match (create_tracklist) + terminal-ack endpoint (agent_tracklists.py): PARTIALLY VERIFIED — the ack is unguarded on the no-match path (CR-01). extract_file_metadata and fingerprint_file: SUCCESS callback clears; NO /failed callback exists and the domain-completed predicate is structurally unable to substitute for a terminally-failed job (CR-02). L-02 is NOT fully satisfied. |
| 3 | L-03: Recovery re-queues `ledger − live keys − completed` via existing keyed producers — never the complement-of-done backlog | VERIFIED | `recover_orphaned_work` (reenqueue.py:225-298) reads `get_ledger_rows` + `get_live_job_keys` + `_build_done_sets`; orphaned set is explicit complement; complement-of-done SWEEP queries removed (only in docstring comments, confirmed by grep); replays via keyed producers with stored payload |
| 4 | L-04: Idempotent startup backfill from live saq_jobs | VERIFIED | `backfill_ledger_from_saq_jobs` (reenqueue.py:341-395) uses `insert_ledger_if_absent` (ON CONFLICT DO NOTHING); controller.py:144 calls it before `recover_orphaned_work`; SAVEPOINT degrade-safe |
| 5 | L-05: Control-only boundary preserved (agent worker stays Postgres-free) | VERIFIED | DB access in deterministic_key.py is strictly behind `getattr(job.queue, "ledger_sessionmaker", None)` + function-local import guarded by presence of handle; `scan.py` and `agent_client.py` import no phaze.database; test_task_split.py is in the verify gates |
| 6 | L-06: Reversible Alembic migration 022 + 85% coverage | VERIFIED | Migration 022 (revision="022", down_revision="021") creates/drops `scheduling_ledger`; no `saq_jobs` reference in executable DDL; summaries report 97.57% total coverage, reenqueue.py at 100% |

**Score:** 5/6 truths verified (L-02 fails on two sub-claims: CR-01 and CR-02)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/models/scheduling_ledger.py` | SchedulingLedger ORM model | VERIFIED | `class SchedulingLedger(TimestampMixin, Base)` at line 46; PK=key, function, routing, payload JSONB, enqueued_at, TimestampMixin |
| `alembic/versions/022_add_scheduling_ledger.py` | Reversible migration 022 | VERIFIED | revision="022", down_revision="021"; creates/drops scheduling_ledger; no saq_jobs in DDL body |
| `src/phaze/services/scheduling_ledger.py` | upsert/insert-if-absent/clear/read/routing | VERIFIED | All five functions present; upsert uses ON CONFLICT DO UPDATE; insert-if-absent uses ON CONFLICT DO NOTHING |
| `src/phaze/services/pipeline.py` | get_live_job_keys SAVEPOINT-isolated | VERIFIED | Function present (line 661 area confirmed); SAVEPOINT pattern cloned from get_stage_busy_counts |
| `src/phaze/tasks/_shared/deterministic_key.py` | WRITE hook + CLEAR hook wired | VERIFIED | apply_deterministic_key upserts (lines 117-140); increment_completed clears on TERMINAL_STATUSES (lines 174-190); both use getattr + lazy import |
| `src/phaze/routers/agent_analysis.py` | clear_ledger_entry in put_analysis AND report_analysis_failed | VERIFIED | Lines 196 (success) and 225 (terminal failure) both call clear_ledger_entry before session.commit() |
| `src/phaze/routers/agent_metadata.py` | clear_ledger_entry in put_metadata | VERIFIED (partial) | Line 73: success path clears; NO /failed callback exists — terminal failure does not clear |
| `src/phaze/routers/agent_fingerprint.py` | clear_ledger_entry in put_fingerprint | VERIFIED (partial) | Line 54: success path clears; NO /failed callback exists — terminal failure does not clear |
| `src/phaze/routers/agent_tracklists.py` | scan_live_set clear in create_tracklist + ack endpoint | VERIFIED (partial) | Match path (line 168) clears in owner-path transaction; terminal-ack endpoint `ack_scan_terminal` exists (POST /{file_id}/scanned); no-match path in scan.py calls ack without guard (CR-01) |
| `src/phaze/tasks/scan.py` | report_scan_terminal called on no-match + terminal failure | FAILED | Line 99: called on no-match path WITHOUT error handling (CR-01 confirmed); line 137: match-path terminal failure correctly gated on `not job.retryable` |
| `src/phaze/tasks/reenqueue.py` | recover_orphaned_work drives off ledger; backfill_ledger_from_saq_jobs present | VERIFIED | get_ledger_rows (line 266), get_live_job_keys (line 267), is_domain_completed (line 270), _DOMAIN_COMPLETED_STAGES (line 107), backfill_ledger_from_saq_jobs (line 341) — all present and wired |
| `src/phaze/tasks/controller.py` | backfill_ledger_from_saq_jobs before recover_orphaned_work in startup | VERIFIED | Line 144: backfill called in startup; line 40 import confirmed |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| deterministic_key.py | scheduling_ledger service | getattr(job.queue, "ledger_sessionmaker", None) then upsert_ledger_entry | WIRED | Lines 125-136: handle-gated lazy import and upsert |
| deterministic_key.py | saq.job.TERMINAL_STATUSES | increment_completed clears when status in TERMINAL_STATUSES | WIRED | Line 42 imports TERMINAL_STATUSES; line 174 gates the clear |
| controller.py | queue_factory.py | ledger_sessionmaker attached to controller queue and AgentTaskRouter | WIRED | controller.py line 112 sets queue.ledger_sessionmaker; line 121 passes ledger_sessionmaker to AgentTaskRouter |
| agent_analysis.py | scheduling_ledger service | clear_ledger_entry before commit in put_analysis AND report_analysis_failed | WIRED | Lines 196, 225 |
| agent_metadata.py | scheduling_ledger service | clear_ledger_entry before commit in put_metadata (success only) | PARTIAL | Line 73 (success); no terminal-failure path |
| agent_fingerprint.py | scheduling_ledger service | clear_ledger_entry before commit in put_fingerprint (success only) | PARTIAL | Line 54 (success); no terminal-failure path |
| agent_tracklists.py | scheduling_ledger service | clear_ledger_entry in create_tracklist owner-path + ack_scan_terminal endpoint | WIRED | Lines 168, 203 |
| scan.py | agent_tracklists.py | report_scan_terminal on no-match + terminal failure | PARTIAL | Line 99: no-match call unguarded (CR-01); line 137: match-failure gated on not job.retryable |
| reenqueue.py | scheduling_ledger service | get_ledger_rows drives recovery | WIRED | Line 79 import, line 266 call |
| reenqueue.py | pipeline.py | get_live_job_keys as live-exclusion set | WIRED | Line 76 import, line 267 call |
| controller.py | reenqueue.py | backfill_ledger_from_saq_jobs before recover_orphaned_work in startup | WIRED | Line 40 import, line 144 call |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|--------------|--------|--------------------|--------|
| reenqueue.py recover_orphaned_work | orphaned (ledger rows minus live minus domain-completed) | get_ledger_rows + get_live_job_keys + _build_done_sets | FLOWING for most paths; DISCONNECTED for terminally-failed metadata/fingerprint (CR-02: domain predicate cannot fire) | HOLLOW for metadata/fingerprint terminal-failure case |

### Behavioral Spot-Checks

Step 7b: SKIPPED — tests are the primary verification surface; running the app requires Docker services (PostgreSQL, Redis). The 1968-test suite is confirmed passing per phase summaries.

### Probe Execution

Step 7c: No probe scripts declared or found for this phase.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|---------|
| L-01 | 45-01 | Durable ledger written at the single before_enqueue chokepoint | SATISFIED | apply_deterministic_key upserts control-side; agent queue no-ops |
| L-02 (controller half) | 45-01 | Ledger cleared on terminal status via after_process hook | SATISFIED | increment_completed clears on TERMINAL_STATUSES, not on QUEUED retry |
| L-02 (agent half — analyze) | 45-02 | process_file ledger cleared in put_analysis (success) AND report_analysis_failed (terminal failure) | SATISFIED | Both handlers clear in-transaction |
| L-02 (agent half — scan_live_set match) | 45-02 | scan_live_set cleared via create_tracklist owner-path | SATISFIED | agent_tracklists.py line 168 |
| L-02 (agent half — scan_live_set no-match/failure) | 45-02 | scan_live_set cleared via terminal-ack endpoint | PARTIAL | Endpoint exists and is wired; no-match call in scan.py is unguarded (CR-01 gap) |
| L-02 (agent half — metadata/fingerprint) | 45-02 | extract_file_metadata/fingerprint_file cleared on terminal failure | BLOCKED | Success-path clear exists; NO terminal-failure clear; domain-completed predicate cannot substitute (CR-02 gap) |
| L-03 | 45-03 | Recovery re-queues ledger minus live minus completed via keyed producers | SATISFIED | recover_orphaned_work rewrites; complement-of-done sweep queries removed |
| L-04 | 45-04 | Idempotent startup backfill from live saq_jobs | SATISFIED | backfill_ledger_from_saq_jobs with DO NOTHING, in startup before recovery |
| L-05 | 45-01/02/03/04 | Control-only boundary preserved; agent worker stays Postgres-free | SATISFIED | getattr gate + function-local lazy imports; test_task_split.py green |
| L-06 | 45-01 | Reversible Alembic migration 022 + 85% coverage | SATISFIED | Migration 022 (down_revision 021); 97.57% total coverage; reenqueue.py 100% |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| src/phaze/tasks/scan.py | 99 | `await api.report_scan_terminal(payload.file_id)` unguarded on no-match path | BLOCKER | CR-01: controller hiccup turns a legitimate no-match COMPLETE into a retrying failure; terminal attempt leaves ledger row uncleared; scan_live_set re-enqueues on every recovery |
| src/phaze/tasks/reenqueue.py | 48-49 | "Plan 02 residual gap" acknowledged in docstring for metadata/fingerprint terminal failure | BLOCKER | CR-02: the acknowledged gap is not compensated by the domain-completed predicate (which cannot fire for terminally-failed files still in the pending set); permanent re-enqueue loop for terminally-failed metadata/fingerprint jobs |

### Human Verification Required

None required — the gaps are observable by code inspection and confirmed by structural analysis of the predicate logic against the pending-set queries.

### Gaps Summary

Two gaps block the L-02 goal claim:

**Gap 1 — CR-01 (scan_live_set no-match path, scan.py:99):** The `report_scan_terminal` call on the no-match return has no error handling. The match-path failure handler (scan.py:129-138) correctly gates its ack on `not job.retryable` before re-raising. The no-match path lacks this discipline. When the controller is unavailable (5xx after retries), `report_scan_terminal` raises, preventing the `no_matches` return and leaving the `scan_live_set:<file_id>` ledger row uncleared. scan_live_set is classified live-keys-only (no domain predicate) precisely because the ack was supposed to clear it on every outcome. That invariant is broken for the controller-down no-match case, causing re-enqueue on every recovery pass.

**Gap 2 — CR-02 (extract_file_metadata + fingerprint_file terminal failure):** These two stages have no `/failed` callback. The Phase 45 design relies on the domain-completed predicate as the "primary net" for the residual gap. However, the predicates are:
- `extract_file_metadata`: "not in `get_metadata_pending_files`" — this query returns ALL music/video files regardless of state (pipeline.py:670). A terminally-failed metadata job's file is still a music file, still in the pending set, so `is_domain_completed` returns False and recovery re-enqueues it every pass.
- `fingerprint_file`: "not in `get_fingerprint_pending_files`" — this query returns `METADATA_EXTRACTED` files PLUS files with `FingerprintResult(status="failed")` (pipeline.py:692-697). A terminally-failed fingerprint job leaves the file in `METADATA_EXTRACTED` state (no state advance without a callback) AND possibly writes a `FingerprintResult(status="failed")` — both of which keep the file in the pending set. The predicate cannot fire.

L-02 states "cleared on completion AND terminal failure." For these two stages, terminal failure does not clear the ledger row and no secondary mechanism can clear it either. The result is an unbounded re-enqueue source that the ledger was specifically introduced to prevent.

Both gaps were identified by the 45-REVIEW.md code review (CR-01 and CR-02) and are confirmed by direct code inspection.

---

_Verified: 2026-06-19_
_Verifier: Claude (gsd-verifier)_
