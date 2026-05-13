# Phase 27: Watcher Service & User-Initiated Scan - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-13
**Phase:** 27-watcher-service-user-initiated-scan
**Areas discussed:** Watcher event model & settle behavior, Admin scan UX & path validation, scan_directory task contract, Watcher service shape & module layout

---

## Watcher event model & settle behavior

### Q1: How should the watcher detect that a file has finished being written?

| Option | Description | Selected |
|--------|-------------|----------|
| Periodic mtime poll after first event | On `FileCreatedEvent`, record (path, mtime, first_seen_at). Background asyncio task scans pending-set every ~2s; when `now - mtime_last_changed >= settle_period`, post. | |
| Subscribe to created+modified, debounce on every event | Subscribe to both event types; each event resets a per-path timer. After settle_period_seconds with no new event, post. | ✓ |
| Hybrid: poll plus modified events | Created+Modified update last_mtime_change_at; background sweep posts paths whose last_mtime_change_at ≥ settle_period_seconds ago. | |

**User's choice:** Subscribe to created+modified, debounce on every event
**Notes:** Maps cleanly to dict-keyed pending-set with `last_change_at` reset on each event; sweep loop only checks the timestamps. Captured as D-01.

### Q2: Cap on how long the watcher will keep a file in the pending-debounce state?

| Option | Description | Selected |
|--------|-------------|----------|
| No cap — wait indefinitely | A 4-hour live recording stays pending for 4 hours, then posts when it finally settles. | |
| Cap at max_pending_seconds (e.g., 1 hour) | If pending > max_pending_seconds, log warning + DROP without posting. Operator picks up via manual scan. | ✓ |
| Cap with forced-post fallback | Force SHA-256 + POST after max_pending_seconds; controller re-upserts on next settle. | |

**User's choice:** Cap at max_pending_seconds (default 3600s); log warning + drop.
**Notes:** Avoids mis-attribution of an unfinished file's SHA-256; bounded in-memory cost. Captured as D-02.

### Q3: Configurable settle params via env? Defaults settle=10s, max_pending=3600s, sweep=2s.

| Option | Description | Selected |
|--------|-------------|----------|
| Env-driven via AgentSettings | All three knobs on AgentSettings with PHAZE_WATCHER_* env mapping. | ✓ |
| Hardcoded constants in the watcher module | Re-config requires code change + redeploy. | |
| Settle env-driven, the other two hardcoded | Smallest env surface. | |

**User's choice:** Env-driven via AgentSettings (all three).
**Notes:** Captured as D-03.

### Q4: On watcher startup / restart, what should happen to files already in the watched roots?

| Option | Description | Selected |
|--------|-------------|----------|
| Strict: do nothing; only react to new events post-startup | Matches PROJECT.md ("catch-up out of scope; manual scan covers this"). | ✓ |
| Eager: optional one-shot bootstrap walk on first start | `PHAZE_WATCHER_BOOTSTRAP_ON_START=true` triggers a one-time walk. | |

**User's choice:** Strict.
**Notes:** Captured as D-04. No bootstrap walk; operator runs a manual `/pipeline/` scan if they care about backfill.

---

## Admin scan UX & path validation

### Q1: Where should the admin scan-trigger form live in the UI?

| Option | Description | Selected |
|--------|-------------|----------|
| Extend /pipeline/ with a 'Trigger Scan' card | Reuses existing operator dashboard + HTMX poll loop; no new nav. | ✓ |
| New /admin/ section with a Scan page | Cleaner separation of admin-only actions; new nav entry. | |
| Keep existing /api/v1/scan + small inline form | Smallest patch but mixes legacy + new endpoints. | |

**User's choice:** Extend /pipeline/.
**Notes:** Captured as D-05.

### Q2: How should the (agent, scan_path) selectors work?

| Option | Description | Selected |
|--------|-------------|----------|
| Agent dropdown + scan_path picker constrained to that agent's scan_roots | HTMX-swap of second selector on agent change; optional subpath text input. | ✓ |
| Agent dropdown + free-form scan_path text | Maximum flexibility; relies on server prefix validation. | |
| Agent dropdown only — scans the whole agent (all roots) | Simplest UI; no subfolder targeting. | |

**User's choice:** Constrained scan_path picker.
**Notes:** Captured as D-06. Server still re-validates prefix on submit.

### Q3: How does the controller validate that the chosen scan_path actually exists on the agent?

| Option | Description | Selected |
|--------|-------------|----------|
| Prefix-only validation; agent reports 'not a directory' if invalid | Controller checks scan_roots prefix + no `..`. Agent's task fails fast on os.walk if path missing. | ✓ |
| Synchronous pre-flight: agent endpoint that stat()s before enqueue | Better UX but adds a controller→agent call, violating v4.0 HTTP boundary direction. | |

**User's choice:** Prefix-only.
**Notes:** Captured as D-07. Failure surfaces via Recent Scans table.

### Q4: How should the Pipeline page show scan progress for an in-flight scan?

| Option | Description | Selected |
|--------|-------------|----------|
| HTMX poll partial — reuse the existing pattern | Mirrors tracklists/scan_progress.html; poll every 2-3s; swap-on-finish. | ✓ |
| Single SSE stream for live scan progress | Lower latency; but Phase 28 standardizes SSE. | |
| Recent-scans table with auto-refresh, no per-scan stream | Simplest; coarsest UX. | |

**User's choice:** HTMX poll partial.
**Notes:** Captured as D-08. SSE deferred to Phase 28.

---

## scan_directory task contract

### Q1: How should chunked scans + watcher events bind files to the right ScanBatch?

| Option | Description | Selected |
|--------|-------------|----------|
| Add optional `batch_id` field to FileUpsertChunk; absent → LIVE sentinel | Same endpoint serves both; controller resolves LIVE sentinel when batch_id is None. | ✓ |
| Two separate endpoints — /files for bulk, /watcher-files for events | Violates success criterion #5 (one endpoint for both). | |
| Always require batch_id; watcher resolves LIVE sentinel locally via new endpoint | Adds an agent startup lookup; trades simpler upsert for more endpoints. | |

**User's choice:** Optional batch_id; absent → LIVE sentinel.
**Notes:** Captured as D-09.

### Q2: How should the agent's scan_directory task report total/processed file counts back?

| Option | Description | Selected |
|--------|-------------|----------|
| Add `PATCH /api/internal/agent/scan-batches/{batch_id}` endpoint | Agent PATCHes total/processed/status. Mirrors agent_execution.py PATCH pattern. | ✓ |
| Derive processed_files from chunk responses; agent only PATCHes status | Controller maintains accumulator; total_files unknown until completion. | |
| Single 'finalize scan' POST after all chunks | No real-time progress; UI shows 'In progress' until end. | |

**User's choice:** New PATCH endpoint.
**Notes:** Captured as D-10. Idempotent write-through; cross-tenant guard before state-machine evaluation (Phase 26 D-08 pattern).

### Q3: Chunk size for scan_directory and watcher posts?

| Option | Description | Selected |
|--------|-------------|----------|
| Use existing AGENT_FILE_CHUNK_MAX (1000); default 500 for scan_directory | New `PHAZE_SCAN_CHUNK_SIZE=500`; watcher posts singletons. | ✓ |
| Hardcode chunk size = 500; watcher singletons | No env knob; module constant. | |
| Adaptive chunk size | Premature optimization. | |

**User's choice:** Env-driven with default 500.
**Notes:** Captured as D-11. Server cap stays 1000.

### Q4: What happens if scan_directory errors partway through?

| Option | Description | Selected |
|--------|-------------|----------|
| Per-file skip + warning log; only fatal errors fail the batch | Mirrors discover_and_hash_files OSError-skip pattern; idempotent re-scan recovers. | ✓ |
| Fail-fast: any error marks the batch FAILED | Conservative; operator manually rescans. | |
| Partial-success status: 'completed_with_errors' enum value | Schema migration for a corner case. | |

**User's choice:** Per-file skip + warning log.
**Notes:** Captured as D-12. Fatal = path-not-exist or 5xx-after-retry.

---

## Watcher service shape & module layout

### Q1: Process model + entry point for the watcher?

| Option | Description | Selected |
|--------|-------------|----------|
| Standalone Python entry point: `phaze.agent_watcher.__main__` | asyncio.run + main loop; new package with observer.py, debouncer.py, poster.py. | ✓ |
| Run inside the agent_worker as a background asyncio task | Fragile inside SAQ event loop; rejected. | |
| SAQ cron-style task that polls the filesystem | Misses the watchdog contract; rejected. | |

**User's choice:** Standalone entry point.
**Notes:** Captured as D-15. Compose command: `uv run python -m phaze.agent_watcher`.

### Q2: How should the watcher resolve agent_id and LIVE sentinel batch_id?

| Option | Description | Selected |
|--------|-------------|----------|
| Call /whoami on startup, then omit batch_id on chunk POSTs | Controller resolves LIVE sentinel server-side from bearer token. | ✓ |
| Call /whoami AND a new /scan-batches/live endpoint to cache batch_id | Adds startup roundtrip + new endpoint; minor perf gain. | |

**User's choice:** /whoami only; omit batch_id.
**Notes:** Captured as D-16/D-18.

### Q3: Where do the small shared startup-helpers live?

| Option | Description | Selected |
|--------|-------------|----------|
| Extract to `phaze.tasks._shared` (Phase 26 D-discretion noted this option) | `_whoami_with_retry`, AgentSettings load, PhazeAgentClient construction. agent_worker refactors to import from there. | ✓ |
| Move shared bits into `phaze.services.agent_bootstrap` | Avoids tying watcher to `phaze.tasks`. | |
| Duplicate in agent_watcher.__main__ | Loses single source of truth. | |

**User's choice:** `phaze.tasks._shared.agent_bootstrap`.
**Notes:** Captured as D-17. Triggers Phase 26's deferred-decision.

### Q4: Compose wiring — where does the watcher run in Phase 27?

| Option | Description | Selected |
|--------|-------------|----------|
| Add `watcher` service to root docker-compose.yml now (note Phase 29 split) | Operator smoke-tests locally now; Phase 29 moves to docker-compose.agent.yml. | ✓ |
| Ship the watcher in a NEW docker-compose.agent.yml in Phase 27 | Pulls forward Phase 29 scope. | |
| Don't add compose entry; defer to Phase 29 | Violates success criterion #1. | |

**User's choice:** Add to root docker-compose.yml now.
**Notes:** Captured as D-19.

---

## Claude's Discretion

The CONTEXT.md `### Claude's Discretion` block lists the items the planner is free to decide:
- Debouncer data structure (recommend `dict[str, _PendingEntry]` + asyncio.Lock).
- Sweep task loop shape (recommend `asyncio.sleep(interval)` loop).
- Field name on FileUpsertChunk (recommend `batch_id`).
- Whether PATCH endpoint echoes the row or returns `{}` (recommend echo).
- Whether agent dropdown shows `name (id)` or just `name` (recommend both).
- Whether `_WHOAMI_BACKOFF_S` moves to the new shared module (recommend yes).
- Watcher chunk-of-1 POST timeout (keep 30s).
- Whether SHA-256 runs in `asyncio.to_thread` (yes — mirrors ingestion.py).

## Deferred Ideas

Captured in CONTEXT.md `<deferred>` block. Key items:
- Watcher delete/move/rename event handling (created-only per PROJECT.md).
- Watcher catch-up on startup (manual scan covers).
- Synchronous scan-path preflight (violates HTTP boundary direction).
- SSE for live scan progress (Phase 28).
- COMPLETED_WITH_ERRORS enum (over-engineering).
- Scheduled re-scans cron job (operator-triggered for now).
- Legacy `/api/v1/scan` deprecation (follow-up cleanup).
- scan_live_set artist/title resolution rewrite (Phase 26-11 deferred note; stays deferred).
- Watcher liveness/health endpoint (Phase 29 heartbeat).
- Atomic "scan in progress" lock (not needed at personal-collection scale).
