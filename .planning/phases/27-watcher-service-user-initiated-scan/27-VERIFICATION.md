---
phase: 27-watcher-service-user-initiated-scan
verified: 2026-05-14T18:40:00Z
status: pass
score: 5/5 must-haves verified
overrides_applied: 0
human_verification_completed: 2026-05-14T18:35:00Z
human_verification_artifact: 27-HUMAN-UAT.md
human_verification:
  - test: "Start docker compose with the watcher service and drop a new music file into the watched root"
    expected: "After the settle period (10s), a new FileRecord appears in Postgres under the agent's LIVE ScanBatch with (agent_id, original_path) as the natural key; no duplicate rows on re-drop"
    why_human: "Requires a running Docker environment with a live postgres, api, and watcher container; the settle-period timer must elapse in real time; database state must be inspected post-event"
    result: pass
    closed_gaps: 9
  - test: "Trigger a scan from /pipeline/ admin UI by selecting an agent and path, then monitor progress"
    expected: "POST /pipeline/scans returns the scan_progress_card partial with RUNNING state and hx-trigger='every 2s'; the card auto-updates every 2s; when scan completes the card transitions to COMPLETED state and polling halts (no hx-trigger in completed markup)"
    why_human: "Requires a running browser + live stack; HTMX polling behavior and DOM swap are not verifiable programmatically"
    result: pass
    closed_gaps: ["gap-13"]
  - test: "Visual inspection: /pipeline/ dashboard renders Trigger Scan card above stats panel with agent dropdown, scan_root select, and subpath input"
    expected: "All UI-SPEC components (trigger_scan_card, scan_path_picker, recent_scans_table, scan_status_pill, scan_submit_error) render correctly per the UI-SPEC markup"
    why_human: "Visual/layout correctness requires a browser; HTMX agent-dropdown swap requires real HTTP round-trip"
    result: pass
    closed_gaps: ["gap-14"]
---

# Phase 27: Watcher Service & User-Initiated Scan Verification Report

**Phase Goal:** Each file server continuously streams new file arrivals to the application server, and the administrator can also trigger an explicit scan of any path on any agent from the admin UI.
**Verified:** 2026-05-14T18:40:00Z (initial: 2026-05-13)
**Status:** pass
**Re-verification:** Promoted from `human_needed` → `pass` after live UAT on rancher-desktop / linux-arm64 closed 14 gaps and all three human-verification checkpoints passed (see `27-HUMAN-UAT.md`).

## Goal Achievement

### Observable Truths

| #   | Truth | Status | Evidence |
| --- | ----- | ------ | -------- |
| 1   | A new `phaze-agent-watcher` service is defined in docker-compose and starts alongside `worker`, `audfprint`, `panako`; runs `watchdog` library | ✓ VERIFIED | `docker-compose.yml` has a `watcher:` service block at lines 47-64; `command: uv run python -m phaze.agent_watcher`; `PHAZE_ROLE=agent`; `depends_on api: condition: service_started`; `restart: unless-stopped`; no redis/postgres deps (DIST-04 invariant); `watchdog>=4.0` in pyproject.toml |
| 2   | Dropping a new file into a watched root results in a new `FileRecord` on the application server under that agent's sentinel LIVE ScanBatch, with `(agent_id, original_path)` as the natural key | ? UNCERTAIN (human) | Code path verified: `WatcherEventHandler.on_created` → `debouncer.touch` → `sweep` → `Poster.post_one` → `PhazeAgentClient.upsert_files(FileUpsertChunk(files=[record]))` → POST `/api/internal/agent/files` (batch_id omitted → LIVE sentinel resolved via `uq_scan_batches_agent_id_live`); upsert uses `index_elements=["agent_id","original_path"]`; end-to-end requires live stack |
| 3   | A file whose `mtime` is still changing is NOT posted; only after the configured settle period (default 10s) of stable `mtime` does the watcher compute SHA-256 and stream the record | ✓ VERIFIED | `Debouncer.sweep()` in `debouncer.py:72-95` returns `ready` paths only when `now - entry.last_change_at >= settle_period`; `touch()` resets `last_change_at` on every watchdog event; tested by `test_sweep_returns_ready_after_settle`, `test_sweep_does_not_return_unsettled_entry`, `test_touch_resets_last_change_at`; default `watcher_settle_seconds=10` in AgentSettings |
| 4   | From the admin UI, an administrator can choose `(agent, scan_path)` and trigger a scan; this enqueues `scan_directory(scan_path, batch_id)` onto the chosen agent's queue and the agent streams discovered files back in chunks (e.g., 500 records per request), with `extract_file_metadata` enqueued per new music/video file | ✓ VERIFIED | `pipeline_scans.trigger_scan()` POST handler creates RUNNING ScanBatch and calls `enqueue_for_agent(task_name="scan_directory", payload=ScanDirectoryPayload(...))`; `scan_directory` in `tasks/scan.py` chunks at `_resolve_chunk_size()` (default 500 per AgentSettings); calls `api.upsert_files(FileUpsertChunk(files=batch, batch_id=payload.batch_id))` per chunk; `agent_files.py` auto-enqueues `extract_file_metadata` for newly-inserted music/video rows via xmax-based detection; 10 contract tests in `test_pipeline_scans.py` pass |
| 5   | The same upsert endpoint serves both bulk scans and per-file watcher events, and a re-walked path produces no duplicate FileRecord rows | ✓ VERIFIED | `POST /api/internal/agent/files` is the single upsert endpoint; `agent_files.py` resolves `batch_id` present (scan_directory) vs absent (watcher LIVE sentinel); `on_conflict_do_update(index_elements=["agent_id","original_path"])` makes re-walk idempotent; tested in `test_agent_files_batch_id.py::test_batch_id_absent_resolves_live_sentinel` and `test_batch_id_present_binds_files_to_that_batch` |

**Score:** 5/5 truths verified (2 require human verification for live-stack end-to-end confirmation)

### Required Artifacts

| Artifact | Expected | Status | Details |
| -------- | -------- | ------ | ------- |
| `src/phaze/agent_watcher/__init__.py` | Package marker | ✓ VERIFIED | Exists; `"""Always-on file watcher for the file-server agent role (Phase 27 D-15)."""` |
| `src/phaze/agent_watcher/__main__.py` | Entry point with `asyncio.run(main())` | ✓ VERIFIED | Contains `asyncio.run(main())` at bottom; `from phaze.tasks._shared.agent_bootstrap import construct_agent_client, whoami_with_retry`; SIGINT/SIGTERM handled via `loop.add_signal_handler`; per-root Observer scheduling via `for root in identity.scan_roots` |
| `src/phaze/agent_watcher/observer.py` | WatcherEventHandler with thread→asyncio bridge | ✓ VERIFIED | `call_soon_threadsafe` is the only thread bridge; NFC normalization applied; filters via `_EXTRACTABLE = frozenset({MUSIC, VIDEO})`; `os.fsdecode` used for bytes paths (WR-03 fixed) |
| `src/phaze/agent_watcher/debouncer.py` | Debouncer with touch/sweep; `_PendingEntry` dataclass | ✓ VERIFIED | `@dataclass(slots=True)` on `_PendingEntry`; `time.monotonic()` for clock; `list(self._pending.items())` avoids RuntimeError; stuck-file eviction via `first_seen_at` check |
| `src/phaze/agent_watcher/poster.py` | `Poster.post_one` with OSError handling | ✓ VERIFIED | `FileUpsertChunk(files=[record])` with NO `batch_id` (D-18); `asyncio.to_thread` for stat and SHA-256; OSError dropped at DEBUG; 3x NFC normalization |
| `src/phaze/tasks/_shared/__init__.py` | Package marker | ✓ VERIFIED | Exists |
| `src/phaze/tasks/_shared/agent_bootstrap.py` | Exports `whoami_with_retry`, `construct_agent_client`, `_WHOAMI_BACKOFF_S` | ✓ VERIFIED | All three exported; `AgentApiAuthError` short-circuit (Pitfall 7 fixed); no Postgres imports |
| `src/phaze/config.py` | Four new AgentSettings fields | ✓ VERIFIED | `watcher_settle_seconds=10`, `watcher_max_pending_seconds=3600`, `watcher_sweep_interval_seconds=2`, `scan_chunk_size=500`; all with `AliasChoices` |
| `pyproject.toml` | `watchdog>=4.0` dependency | ✓ VERIFIED | Line 30: `"watchdog>=4.0",` in alphabetic order |
| `src/phaze/schemas/agent_files.py` | `FileUpsertChunk.batch_id: uuid.UUID | None = None` | ✓ VERIFIED | Field present; `extra="forbid"` preserved |
| `src/phaze/schemas/agent_scan_batches.py` | `ScanBatchPatch` + `ScanBatchPatchResponse` | ✓ VERIFIED | Exists; `Literal["running","completed","failed"]` (no "live"); `extra="forbid"` on Patch only |
| `src/phaze/schemas/agent_tasks.py` | `ScanDirectoryPayload` | ✓ VERIFIED | Class exists with `scan_path`, `batch_id`, `agent_id`; `extra="forbid"` |
| `src/phaze/schemas/pipeline_scans.py` | `TriggerScanForm` | ✓ VERIFIED | Exists; `agent_id`, `scan_root`, `subpath=""` default; `extra="forbid"` |
| `src/phaze/routers/agent_scan_batches.py` | PATCH endpoint with state machine + cross-tenant guard | ✓ VERIFIED | `if batch.agent_id != agent.id:` 403 BEFORE state machine; `_SCAN_TRANSITIONS = {RUNNING: {COMPLETED, FAILED}}`; idempotent same-state 200 echo; 404 for missing batch |
| `src/phaze/routers/agent_files.py` | Modified upsert with batch_id resolution | ✓ VERIFIED | Resolution block at line 58-85; cross-tenant guard; LIVE sentinel via `uq_scan_batches_agent_id_live` |
| `src/phaze/services/agent_client.py` | `patch_scan_batch` method | ✓ VERIFIED | `async def patch_scan_batch` at line 296; uses `model_dump(mode='json', exclude_unset=True)` |
| `src/phaze/main.py` | Both routers wired via `include_router` | ✓ VERIFIED | Lines 101, 105: `app.include_router(agent_scan_batches.router)` and `app.include_router(pipeline_scans.router)` |
| `src/phaze/tasks/scan.py` | `scan_directory` task body | ✓ VERIFIED | `async def scan_directory` at line 131; `_EXTRACTABLE = frozenset({MUSIC, VIDEO})` (CR-01 fixed to match watcher); `followlinks=False`; per-chunk PATCH; terminal PATCH; `asyncio.to_thread` for stat and SHA-256; ≥3 NFC normalization calls |
| `src/phaze/tasks/agent_worker.py` | `scan_directory` in `settings.functions` | ✓ VERIFIED | Line 179: `scan_directory,` in functions list; import from `phaze.tasks.scan` at line 62 |
| `src/phaze/routers/pipeline_scans.py` | Three handlers: POST, GET/{batch_id}, GET/agent-roots | ✓ VERIFIED | All three routes present; `".." in PurePosixPath(joined).parts` traversal check (WR-01 fixed); `form.scan_root not in agent.scan_roots` check (WR-05 fixed); enqueue calls `task_name="scan_directory"` |
| `src/phaze/templates/pipeline/partials/trigger_scan_card.html` | Form card with HTMX | ✓ VERIFIED | Exists |
| `src/phaze/templates/pipeline/partials/scan_path_picker.html` | HTMX swap target | ✓ VERIFIED | Exists |
| `src/phaze/templates/pipeline/partials/scan_progress_card.html` | Poll partial with terminal halt | ✓ VERIFIED | `hx-trigger="every 2s"` in running branch only; COMPLETED/FAILED branches have no HTMX attrs |
| `src/phaze/templates/pipeline/partials/scan_status_pill.html` | Pill with aria-label (3 states) | ✓ VERIFIED | 3 `aria-label="Status: ...` occurrences |
| `src/phaze/templates/pipeline/partials/recent_scans_table.html` | Mini-table with failed-row inline error | ✓ VERIFIED | `colspan="6"` and `No scans yet` both present |
| `src/phaze/templates/pipeline/partials/scan_submit_error.html` | `role="alert"` error card | ✓ VERIFIED | `role="alert"` present |
| `src/phaze/templates/pipeline/dashboard.html` | Two includes above stats panel | ✓ VERIFIED | Lines 11, 14: `{% include "pipeline/partials/trigger_scan_card.html" %}` and `{% include "pipeline/partials/recent_scans_table.html" %}` |
| `docker-compose.yml` | `watcher` service block | ✓ VERIFIED | `command: uv run python -m phaze.agent_watcher`; `PHAZE_ROLE=agent`; `depends_on api: service_started`; `restart: unless-stopped`; `:ro` volume only; no redis/postgres deps |
| `src/phaze/agent_watcher/README.md` | Per-service README ≥ 30 lines | ✓ VERIFIED | 41 lines; all 4 required env vars; all 4 tunable env vars; Phase 29 note; `phaze.database` import-boundary mention; entry-point command |
| `.env.example` | Four watcher env vars documented | ✓ VERIFIED | `PHAZE_WATCHER_SETTLE_SECONDS`, `PHAZE_WATCHER_MAX_PENDING_SECONDS`, `PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS`, `PHAZE_SCAN_CHUNK_SIZE` all present commented-out |

### Key Link Verification

| From | To | Via | Status | Details |
| ---- | -- | --- | ------ | ------- |
| `agent_watcher/__main__.py` | `tasks/_shared/agent_bootstrap` | `from phaze.tasks._shared.agent_bootstrap import construct_agent_client, whoami_with_retry` | ✓ WIRED | Line 47 of `__main__.py` |
| `agent_watcher/observer.py::WatcherEventHandler` | `debouncer.Debouncer.touch` | `loop.call_soon_threadsafe(self._debouncer_touch, normalized)` | ✓ WIRED | Line 81; no direct dict mutation from watchdog thread |
| `agent_watcher/poster.py::post_one` | `PhazeAgentClient.upsert_files` | `FileUpsertChunk(files=[record])` — batch_id omitted | ✓ WIRED | Line 91; D-18 compliance confirmed |
| `tasks/scan.py::scan_directory` | `ctx["api_client"].upsert_files` + `ctx["api_client"].patch_scan_batch` | Chunked HTTP, no Postgres | ✓ WIRED | Lines 197-209; both calls present |
| `agent_worker.py settings.functions` | `scan_directory` | Import + registration in functions list | ✓ WIRED | Lines 62, 179 |
| `routers/pipeline_scans.py POST handler` | `request.app.state.task_router.enqueue_for_agent` | `task_name="scan_directory"` | ✓ WIRED | Lines 228-231 |
| `routers/agent_scan_batches.py` | `main.py include_router` | `app.include_router(agent_scan_batches.router)` | ✓ WIRED | main.py line 101 |
| `routers/pipeline_scans.py` | `main.py include_router` | `app.include_router(pipeline_scans.router)` | ✓ WIRED | main.py line 105 |
| `templates/pipeline/dashboard.html` | `trigger_scan_card.html` + `recent_scans_table.html` | `{% include %}` | ✓ WIRED | Lines 11, 14 of dashboard.html |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| -------- | ------------- | ------ | ------------------ | ------ |
| `poster.py::post_one` | `sha256`, `file_size` | `asyncio.to_thread(compute_sha256, p)` + `asyncio.to_thread(lambda: p.stat().st_size)` | Yes — real filesystem reads | ✓ FLOWING |
| `scan_directory` | `batch` (chunk list) | `os.walk(scan_root, followlinks=False)` + `asyncio.to_thread(compute_sha256, full_path)` | Yes — real filesystem walk | ✓ FLOWING |
| `scan_progress_card.html` | `batch.processed_files`, `batch.total_files` | DB via `session.get(ScanBatch, batch_id)` in poll handler | Yes — live DB query per poll | ✓ FLOWING |
| `recent_scans_table.html` | `recent_scans` | `select(ScanBatch).where(status != LIVE).order_by(created_at.desc()).limit(10)` in pipeline.py dashboard handler | Yes — live DB query | ✓ FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| -------- | ------- | ------ | ------ |
| `phaze.agent_watcher` importable (activates boundary test) | `uv run python -c "import phaze.agent_watcher"` | `importable` | ✓ PASS |
| `test_agent_watcher_does_not_import_phaze_database` PASSES (not SKIPPED) | `uv run pytest tests/test_task_split.py -v` | 4 PASSED, 0 skipped | ✓ PASS |
| `scan_directory` registered in `agent_worker.settings.functions` | Python import + assertion | `scan_directory FOUND` in functions list | ✓ PASS |
| All Phase 27 specific tests pass | `uv run pytest tests/test_agent_watcher/ tests/test_task_split.py tests/test_tasks/test_scan_directory.py tests/test_tasks/test_shared_agent_bootstrap.py tests/test_routers/test_agent_scan_batches.py tests/test_routers/test_agent_files_batch_id.py tests/test_routers/test_pipeline_scans.py tests/test_schemas/ -q` | 128 passed | ✓ PASS |
| Full test suite | `uv run pytest -q` | 1070 passed, 0 failing | ✓ PASS |
| Pipeline routes present in app | Python import app + route inspection | `/pipeline/scans/agent-roots`, `/pipeline/scans/{batch_id}`, `/pipeline/scans`, `/api/internal/agent/scan-batches/{batch_id}` all present | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| ----------- | ----------- | ----------- | ------ | -------- |
| DIST-02 | Plans 01, 02, 03, 04, 05, 06, 07 | Each file server runs one or more agents (SAQ worker + watcher + audfprint + panako sidecars) | ✓ SATISFIED | `watcher:` service defined in docker-compose.yml; runs `python -m phaze.agent_watcher` (Plans 05, 07); SAQ worker already existed |
| SCAN-01 | Plans 02, 06 | Administrator can trigger a scan from admin UI; enqueues `scan_directory` | ✓ SATISFIED | `POST /pipeline/scans` creates RUNNING ScanBatch and enqueues `scan_directory` via `task_router.enqueue_for_agent`; 10 contract tests pass |
| SCAN-02 | Plans 02, 03, 04 | Agent streams 500-record chunks; server upserts and auto-enqueues `extract_file_metadata` | ✓ SATISFIED | `scan_directory` chunks at `scan_chunk_size` (default 500); `agent_files.py` auto-enqueues `extract_file_metadata` via xmax detection for new rows |
| SCAN-03 | Plans 01, 05, 07 | Always-on watcher service with `watchdog` library; streams to same upsert endpoint under LIVE ScanBatch | ✓ SATISFIED | `phaze.agent_watcher` package; `WatcherEventHandler` subscribes to `FileCreatedEvent` + `FileModifiedEvent`; POSTs to `/api/internal/agent/files` with batch_id omitted |
| SCAN-04 | Plan 05 | Watcher waits for settle period before posting | ✓ SATISFIED | `Debouncer.sweep()` returns paths only when `now - last_change_at >= settle_period`; configurable via `PHAZE_WATCHER_SETTLE_SECONDS` (default 10s) |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| ---- | ---- | ------- | -------- | ------ |
| `routers/pipeline.py` | ~155-158 | Transient `_agent_name` / `_elapsed_seconds` attributes set on ORM `ScanBatch` instances with `# type: ignore[attr-defined]` | Info | Works today; fragile if SQLAlchemy model becomes `slots=True`; documented in code review (IN-02); no behavioral impact for Phase 27 |
| `routers/agent_scan_batches.py` | ~97-102 | Defensive `if new == ScanStatus.LIVE: raise 409` is unreachable (Pydantic Literal rejects "live" at 422) | Info | Dead code; documented in review (IN-03); harmless as documentation of invariant |
| `routers/pipeline_scans.py` | ~74-79 | Empty-scan-roots branch in `agent_roots_swap` passes `agent=None` to template even when agent exists but has no roots, collapsing the yellow-surface branch | Info | Cosmetic: yellow-surface "no scan roots" Jinja branch is unreachable from this code path (IN-01); functional behavior correct (empty state shown) |

No blocker anti-patterns found. Review fixes CR-01 (filter consistency), WR-01 (path component traversal), WR-02 (client close in finally), WR-03 (fsdecode), WR-04 (lru_cache/PHAZE_ROLE fixture), WR-05 (scan_root validation), WR-06 (enqueue failure sets FAILED), WR-07 (observer.join timeout) are all applied.

### Human Verification Required

#### 1. End-to-end file drop → FileRecord creation (SC-2 live confirmation)

**Test:** In a running `docker compose up` environment, drop a new `.mp3` file into the directory mounted at `/data/music` on the file server. Wait 10+ seconds (settle period). Query Postgres for `SELECT * FROM file_records WHERE agent_id = '<agent>' ORDER BY created_at DESC LIMIT 5`.

**Expected:** A new row appears with `batch_id` pointing at the agent's LIVE ScanBatch (the one with `status='live'`); `original_path` is NFC-normalized; dropping the same file again produces no additional row (idempotent upsert).

**Why human:** Requires live Docker stack with running Postgres, API, and watcher container; settle-period timer must elapse in real time; database must be inspected directly.

#### 2. Admin UI scan trigger → progress polling → terminal halt (SC-4 live confirmation)

**Test:** Navigate to `/pipeline/` in a browser. Select an agent from the dropdown, select a scan root, optionally enter a subpath. Click Trigger Scan. Observe the progress card polling every 2s. Wait for scan to complete.

**Expected:** POST returns RUNNING card with `hx-trigger="every 2s"`; card updates with file counts during scan; on completion the card swaps to COMPLETED state with no HTMX polling attributes (browser network tab shows polling stops).

**Why human:** HTMX polling and DOM replacement require a browser; the 2s cadence and polling-halt behavior require visual observation of the network tab or DOM inspector.

#### 3. Visual layout verification

**Test:** Inspect `/pipeline/` in a browser with the full CSS loaded.

**Expected:** Trigger Scan card appears above the pipeline stats panel; Recent Scans table below it; agent dropdown populates on page load; selecting an agent HTMX-swaps the scan_path picker with the correct scan roots; status pills use correct surface-variant colors (blue/RUNNING, green/COMPLETED, red/FAILED).

**Why human:** Visual appearance, layout, and CSS-dependent color verification require browser rendering.

### Gaps Summary

No gaps found. All 5 success criteria from ROADMAP.md are verified in the codebase:

1. `phaze-agent-watcher` service defined in docker-compose — confirmed
2. File drop → FileRecord under LIVE ScanBatch — code path fully wired; live stack confirmation deferred to human verification
3. Settle period (mtime stability) enforced before posting — Debouncer implementation verified with unit tests
4. Admin UI trigger → `scan_directory` enqueue → 500-record chunks + `extract_file_metadata` auto-enqueue — all wired and tested
5. Same upsert endpoint for both paths; idempotent on `(agent_id, original_path)` composite key — verified

All 7 review findings (CR-01, WR-01 through WR-07) are resolved. Info-class findings (IN-01 through IN-05) are non-blocking and documented. 1070 tests pass, 0 fail.

---

_Verified: 2026-05-13T00:00:00Z_
_Verifier: Claude (gsd-verifier)_
