# Phase 27: Watcher Service & User-Initiated Scan - Context

**Gathered:** 2026-05-13
**Status:** Ready for planning

<domain>
## Phase Boundary

Each file server runs an always-on `phaze-agent-watcher` service that observes the agent's configured `scan_roots` using the `watchdog` library and streams newly-arrived files to the application server via the existing Phase 25 `POST /api/internal/agent/files` endpoint. Watcher-originated files bind to the agent's sentinel `LIVE` ScanBatch (one per agent, seeded at agent registration per Phase 24 D-09/D-11/D-12) — the controller resolves the sentinel server-side from the bearer-token-derived `agent_id`. A new `phaze.agent_watcher` standalone Python entry point (NOT a SAQ worker) hosts the watchdog observer + an in-memory mtime debouncer that delays posting until a file's mtime has been stable for a configurable settle period (default 10s).

Phase 27 also delivers the admin-triggered bulk scan path. A new "Trigger Scan" card on the existing `/pipeline/` page lets the operator choose `(agent, scan_path)` from a constrained selector (scan_path picker is HTMX-swapped to the agent's `scan_roots` entries with an optional sub-path text input). The controller validates the chosen path by prefix-matching against `agent.scan_roots` (no filesystem stat — the application server has no agent-side mounts), creates a new `RUNNING` ScanBatch, and enqueues a new `scan_directory(scan_path, batch_id)` SAQ task onto the chosen agent's queue via the Phase 26 `AgentTaskRouter`. The agent's `scan_directory` task walks the path, NFC-normalizes paths, SHA-256s each known-extension file, and POSTs chunks of 500 records to `POST /api/internal/agent/files` with an explicit `batch_id` (the new optional schema field). After each chunk and at task end, it updates the batch via a new `PATCH /api/internal/agent/scan-batches/{batch_id}` endpoint so the Pipeline UI's HTMX poll partial can render live progress.

The Phase 25 `POST /api/internal/agent/files` endpoint gains one new field — `batch_id: UUID | None = None` — and one new behavior: when `batch_id` is absent, the controller resolves the calling agent's `LIVE` sentinel batch (`WHERE agent_id=? AND status='live'`) and stamps every file in the chunk into it. When `batch_id` is present, the chunk is bound to that batch (controller validates the batch belongs to the calling agent's `agent_id`, returns 403 otherwise per Phase 26 cross-tenant-guard pattern). This is the "same upsert endpoint serves both bulk scans and per-file watcher events" invariant from roadmap success criterion #5.

Phase 27 does **not** ship the `docker-compose.agent.yml` two-host split (Phase 29), does **not** add heartbeat / Agents-admin (Phase 29), does **not** implement watcher catch-up-on-startup (out of scope per PROJECT.md — manual user-initiated scan covers this), and does **not** handle `deleted`/`moved`/`modified` event types beyond using `modified` as a debounce timer reset. The watcher is `created`-only in terms of "newly-tracked files."

</domain>

<decisions>
## Implementation Decisions

### Watcher Event Model & Settle Behavior

- **D-01:** The watcher subscribes to both `FileCreatedEvent` and `FileModifiedEvent` on each watched root. There is exactly one in-memory pending-set entry per `original_path` keyed by NFC-normalized absolute path. Each event resets that entry's `last_change_at` timestamp. A background asyncio sweep task runs every `sweep_interval_seconds` (default 2s); for each pending entry where `now - last_change_at >= settle_period_seconds` (default 10s), the sweep computes SHA-256, builds the `FileUpsertRecord`, POSTs `/api/internal/agent/files` with chunk size 1 (batch_id omitted → controller resolves LIVE sentinel), and removes the entry. The sweep also handles the cap (see D-02).
- **D-02:** **Stuck-file cap.** If `now - first_seen_at > max_pending_seconds` (default 3600s = 1 hour), the sweep logs a WARNING (`watcher: dropping path=%s pending_for=%ds; mtime still changing`), removes the entry, and does NOT post. The operator picks the file up later via a manual `/pipeline/` scan trigger. Rationale: bounded in-memory cost is the lower priority concern; mis-attribution of an unfinished file's SHA-256 is the higher concern.
- **D-03:** **Env-driven watcher knobs on `AgentSettings`** (Phase 26 D-14). New fields on `AgentSettings`:
  - `watcher_settle_seconds: int = 10` ← `PHAZE_WATCHER_SETTLE_SECONDS`
  - `watcher_max_pending_seconds: int = 3600` ← `PHAZE_WATCHER_MAX_PENDING_SECONDS`
  - `watcher_sweep_interval_seconds: int = 2` ← `PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS`
  Use `AliasChoices` like the Phase 26-01 pattern so `PHAZE_WATCHER_*` env vars map onto the bare field names. Type-validated at watcher startup via the existing pydantic-settings machinery.
- **D-04:** **Strict startup.** The watcher does NOT walk existing files on first start. It only reacts to events emitted after watchdog's Observer is running. Matches PROJECT.md's "Watcher catch-up on startup is out of scope for v4.0; manual user-initiated scan covers this." Loses files that landed during downtime; operator's job to re-scan after a restart if they care.

### Admin Scan UX & Path Validation

- **D-05:** **Form location: extend `/pipeline/`** with a new "Trigger Scan" card. The Pipeline page already has the operator-focused dashboard pattern with HTMX polling (templates/pipeline/dashboard.html). No new top-level nav entry. The card lives above the existing stats panel and contains: (a) the agent dropdown + scan-path picker form, (b) a "Recent Scans" mini-table that auto-refreshes with the existing 5s dashboard poll loop. Phase 29 will add the dedicated `/admin/agents` page; that's where Agents admin lives.
- **D-06:** **Selector design — agent-constrained scan_path picker.**
  1. Agent dropdown lists every non-revoked agent (`SELECT id, name FROM agents WHERE revoked_at IS NULL ORDER BY name`). The legacy agent (`legacy-application-server`) is shown if it isn't revoked (it WAS born revoked per Phase 24 D-06; therefore in practice it won't appear).
  2. Selecting an agent HTMX-swaps the second selector to a `<select>` of that agent's `scan_roots` jsonb entries.
  3. Below the scan_root dropdown sits an optional "subpath" text input. Submit posts to a new `POST /pipeline/scans` controller endpoint with `{agent_id, scan_root, subpath}`.
  4. Controller joins root + subpath, NFC-normalizes, and validates the result starts with one of the agent's `scan_roots` and contains no `..` (mirrors current `routers/scan.py` rejection at line 41).
- **D-07:** **Prefix-only validation; no preflight.** The controller does NOT call back to the agent to stat the path. If the path doesn't exist on the agent's filesystem, the agent's `scan_directory` task fails on its first `os.walk` and PATCHes the ScanBatch to `FAILED` with `error_message="Scan path does not exist on agent: <path>"`. The Pipeline UI surfaces the failure via the recent-scans table. Avoids a synchronous controller→agent call (the v4.0 HTTP boundary flows agent→controller).
- **D-08:** **Progress display — HTMX poll partial.** After form submit, the controller returns a `scan_progress_card.html` partial showing `processed_files / total_files` and a status pill. The partial polls `GET /pipeline/scans/{batch_id}` every 2s (mirrors `templates/tracklists/partials/scan_progress.html`); when `status in {completed, failed}`, the partial replaces itself with a final-state card and stops polling. SSE is deferred to Phase 28's execution-dispatch aggregation; the polling pattern is the v4.0 standard for non-aggregated single-resource progress.

### `scan_directory` Task Contract

- **D-09:** **Extend `FileUpsertChunk` with `batch_id: UUID | None = None`.** Phase 25 D-16's `extra="forbid"` constraint means this is a wire-format change — but `None` is the default, so all existing Phase 25 callers (the Phase 26 refactor at `routers/agent_files.py:99-117` had none; no production callers yet) continue to pass without it. Server-side resolution:
  - `batch_id` present → SELECT the batch by id; 404 if missing; 403 if `batch.agent_id != calling_agent.id` (cross-tenant guard per Phase 26 D-08). Stamp all files with this batch_id.
  - `batch_id` absent → SELECT `id FROM scan_batches WHERE agent_id=? AND status='live'` (the partial unique index `uq_scan_batches_agent_id_live` guarantees ≤1 result; the Phase 24 sentinel guarantees ≥1 result for any registered agent). Stamp all files with the sentinel's id.
- **D-10:** **New endpoint `PATCH /api/internal/agent/scan-batches/{batch_id}`** in `src/phaze/routers/agent_scan_batches.py`. Body: `ScanBatchPatch(total_files: int | None = None, processed_files: int | None = None, status: Literal["running","completed","failed"] | None = None, error_message: str | None = None)`. `extra="forbid"`. Auth via `Depends(get_authenticated_agent)`. Cross-tenant guard: 403 if `batch.agent_id != caller.id` before any state-machine evaluation (Phase 26 D-08 pattern — prevents timing side-channel). Idempotent write-through: same PATCH twice = same final state, no error. Status enum CAN transition `RUNNING → COMPLETED` or `RUNNING → FAILED`; `LIVE` is a terminal sentinel state (the watcher never PATCHes its batch). Re-PATCH to the SAME status is a 200 no-op without bumping `updated_at` (Phase 26 D-08 invariant).
- **D-11:** **Chunk size: 500 records.** scan_directory walks the path, accumulates `FileUpsertRecord` dicts in a local list, flushes when len == `scan_chunk_size` (new `AgentSettings.scan_chunk_size: int = 500` ← `PHAZE_SCAN_CHUNK_SIZE`). The server cap stays at the existing `agent_file_chunk_max = 1000` (config.py:68). Watcher posts singletons (chunk size 1, one event at a time). After each successful chunk POST, scan_directory PATCHes `processed_files = total_so_far` on the batch.
- **D-12:** **Mid-walk error handling — per-file skip + warning log.** Mirrors `services/ingestion.py:65` (`except OSError: logger.warning("Skipping unreadable file..."); continue`). The walk only ABORTS if (a) the scan_path itself is not a directory (initial `os.walk` raises / returns immediately), or (b) a 5xx-after-retry from the controller bubbles up as `AgentApiServerError` (Phase 26 D-12). On abort: PATCH `status=failed, error_message=<reason>`. On clean walk: PATCH `status=completed, total_files=N, processed_files=N`. Successfully posted chunks BEFORE an abort are NOT rolled back — the composite UQ on `(agent_id, original_path)` makes a subsequent re-scan fully idempotent.
- **D-13:** **scan_directory task signature + module location:**
  ```python
  # src/phaze/tasks/scan.py (existing file; add new function alongside scan_live_set)
  async def scan_directory(
      ctx: dict[str, Any],
      *,
      scan_path: str,
      batch_id: str,
      agent_id: str,
  ) -> dict[str, Any]:
      ...
  ```
  Registered in `phaze.tasks.agent_worker.settings.functions` (the agent role). Reads `ctx["api_client"]` (PhazeAgentClient) for HTTP calls and `ctx["agent_identity"]` for agent_id confirmation. NEVER imports `phaze.database` (D-25 from Phase 26 enforces this).
- **D-14:** **scan_directory payload schema in `phaze.schemas.agent_tasks`** (Phase 26 D-22 pattern):
  ```python
  class ScanDirectoryPayload(BaseModel):
      model_config = ConfigDict(extra="forbid")
      scan_path: str
      batch_id: UUID
      agent_id: str
  ```
  Controller's POST `/pipeline/scans` handler builds this and calls `app.state.task_router.enqueue_for_agent(agent_id=agent_id, task_name="scan_directory", payload=ScanDirectoryPayload(...))`.

### Watcher Service Shape & Module Layout

- **D-15:** **Standalone Python entry point.** New package `src/phaze/agent_watcher/`:
  ```
  src/phaze/agent_watcher/
    __init__.py
    __main__.py          # asyncio.run(main()); reads AgentSettings; bootstrap; spins observer + sweep
    observer.py          # watchdog Observer + EventHandler subclass that pushes to debouncer
    debouncer.py         # in-memory pending-set + sweep task (D-01, D-02)
    poster.py            # adapts a single FileUpsertRecord to a chunk-of-1 POST via PhazeAgentClient
  ```
  Compose command: `uv run python -m phaze.agent_watcher`. Module is **never imported by anything that imports `phaze.database`** — same import-boundary invariant as `phaze.tasks.agent_worker` (Phase 26 D-03/D-25). The Phase 26 D-25 import-boundary test gets a sibling case for `phaze.agent_watcher`.
- **D-16:** **Watcher startup sequence** (mirrors `phaze.tasks.agent_worker.startup` D-16):
  1. Read `AgentSettings()` — fails fast if `agent_api_url`, `agent_token`, or any watcher knob is invalid.
  2. Construct `PhazeAgentClient(base_url=settings.agent_api_url, token=settings.agent_token.get_secret_value(), timeout=30.0)`.
  3. Call `client.whoami()` with bounded exponential backoff (reuse `_whoami_with_retry` helper from D-17). Failure → `raise RuntimeError`, container exits non-zero, `restart: unless-stopped` retries.
  4. Stash `identity = AgentIdentity(...)`. Use `identity.scan_roots` to decide which roots to observe.
  5. Construct watchdog `Observer`, attach an `EventHandler` per root that pushes events into the debouncer.
  6. Start the asyncio sweep task. Block on `asyncio.Event` until SIGINT/SIGTERM, then graceful shutdown (stop Observer, await sweep task completion / cancel pending entries, await `client.close()`).
- **D-17:** **Shared startup helpers — extract to `phaze.tasks._shared.agent_bootstrap`.** Phase 26 D-discretion left this as "only if duplication would otherwise exist." Phase 27 IS the trigger. The new module exports:
  - `async def whoami_with_retry(client: PhazeAgentClient, backoff: tuple[float, ...] = _WHOAMI_BACKOFF_S) -> AgentIdentity`
  - `def construct_agent_client(cfg: AgentSettings) -> PhazeAgentClient`
  - `_WHOAMI_BACKOFF_S` constant
  `phaze.tasks.agent_worker.startup` refactors to import from there (small in-place edit, no behavior change). Verified by the existing D-25 import-boundary test that this module stays Postgres-free (it imports only `phaze.config.AgentSettings`, `phaze.services.agent_client`, `phaze.schemas.agent_identity`).
- **D-18:** **No /whoami pre-cache of LIVE sentinel batch_id.** The watcher calls /whoami ONLY for identity bootstrap; for every file event POST, `batch_id` is omitted and the controller resolves the LIVE sentinel server-side from the bearer token (per D-09). Pros: zero additional agent→controller endpoints; the sentinel-resolution query is one indexed lookup. Cons: per-chunk overhead of one indexed query — negligible for a personal-collection app.
- **D-19:** **Compose wiring — add `watcher` service to root `docker-compose.yml` now.** New service block in `docker-compose.yml` alongside `worker`, `audfprint`, `panako`:
  ```yaml
  watcher:
    build:
      context: .
      dockerfile: Dockerfile
    command: uv run python -m phaze.agent_watcher
    env_file: .env
    environment:
      - PHAZE_ROLE=agent
      # PHAZE_AGENT_API_URL, PHAZE_AGENT_TOKEN, PHAZE_AGENT_QUEUE come from .env in Phase 27
    volumes:
      - "${SCAN_PATH:-/data/music}:/data/music:ro"
    depends_on:
      api:
        condition: service_started
    restart: unless-stopped
  ```
  Add a YAML comment block above the service noting that Phase 29 will move both `watcher` and `worker` (renamed) to `docker-compose.agent.yml` and strip them from the root file (which becomes application-server-only).

### Idempotency & Cross-Tenant Guards

- **D-20:** **Watcher events are idempotent** by virtue of the composite `(agent_id, original_path)` UQ on `FileRecord`. The same file landing twice (e.g., watcher restart followed by a manual scan covering the same root) produces no duplicate rows; the second post is a no-op UPDATE on `sha256_hash`, `file_size`, `state` (matches `services/ingestion.py:106-114`).
- **D-21:** **Cross-tenant guard on the new PATCH endpoint AND the new `batch_id` field on the upsert endpoint.** Both endpoints fetch the batch with `session.get(ScanBatch, batch_id)`, return 404 if missing, then check `batch.agent_id != calling_agent.id` and return 403 BEFORE state-machine evaluation (Phase 26 D-08 pattern; prevents timing side-channel via 409 vs 403). The cross-tenant test fixture from Phase 26's `tests/test_routers/test_agent_*.py` is reused.

### Test Infrastructure

- **D-22:** **New tests added in Phase 27:**
  - `tests/test_routers/test_agent_scan_batches.py` — contract tests for the new PATCH endpoint (auth, cross-tenant guard, state-machine transitions, idempotent same-state PATCH).
  - `tests/test_routers/test_agent_files_batch_id.py` — coverage for the new `batch_id` field on the upsert endpoint (absent → LIVE resolution, present → bound to that batch, cross-tenant rejection).
  - `tests/test_routers/test_pipeline_scans.py` — controller-side `POST /pipeline/scans` form handler; agent dropdown HTMX swap; prefix-validation rejection.
  - `tests/test_tasks/test_scan_directory.py` — agent-side scan_directory: chunking, mid-walk skip, PATCH-final-status, error propagation.
  - `tests/test_agent_watcher/test_debouncer.py` — debouncer state machine: created+modified resets timer, settle period elapsed → ready, stuck-file cap drops.
  - `tests/test_agent_watcher/test_observer.py` — observer wires watchdog events into debouncer (no real filesystem; use watchdog's `PatternMatchingEventHandler` test harness).
  - `tests/test_agent_watcher/test_main.py` — end-to-end with a respx-mocked PhazeAgentClient: drop a fixture file, watch it post.
  - `tests/test_task_split.py` (existing, Phase 26 D-25) — add a parallel case asserting `phaze.agent_watcher` imports stay Postgres-free (`phaze.database`, `phaze.tasks.session`, `sqlalchemy.ext.asyncio` are all absent from `sys.modules` after `import phaze.agent_watcher`).
- **D-23:** **`watchdog` dependency.** Add `watchdog>=4.0` to `[project].dependencies` in `pyproject.toml`. Used at runtime by `phaze.agent_watcher`. The recipe `just sync` / `uv sync` regenerates the lock file in the same commit. No new dev dep needed for tests — watchdog ships `events` types we can construct directly.

### Roadmap & Doc Sweep

- **D-24:** **Minimal doc touch at the end of Phase 27** (single commit):
  - `.planning/STATE.md` — accumulate Phase 27 decisions
  - `README.md` if any — note the new `watcher` service in the docker-compose snippet (operator-facing doc)
  - `CLAUDE.md` — no change (watcher is a deployment artifact, not a development workflow change)
  - Per-service READMEs — add a brief README at `src/phaze/agent_watcher/README.md` per the memory rule "README per service"

### Claude's Discretion

- Exact debouncer data structure (`dict[str, _PendingEntry]` with an asyncio.Lock vs `asyncio.Queue` consumer pattern). The dict-with-lock is recommended for clarity.
- Whether the sweep task uses `asyncio.create_task` with a wakeup `asyncio.sleep(sweep_interval)` loop or `loop.call_later` rescheduling. Sleep-loop is cleaner.
- The exact field name on `FileUpsertChunk` — `batch_id` vs `scan_batch_id`. `batch_id` is shorter and matches existing `ScanBatch.id`/`batch_id` naming in `routers/scan.py` + `services/ingestion.py`. Use `batch_id`.
- Whether `PATCH /api/internal/agent/scan-batches/{batch_id}` returns the updated batch row or just `200 {}`. Echoing the row is cheaper for the agent (no follow-up GET); pick echo.
- Whether the Pipeline form's scan-path picker pre-populates "scan_root + /" as the subpath placeholder or leaves the input empty. Empty is conservative; pick that.
- Whether the agent dropdown shows just `name` or `name (id)`. Showing both helps debugging. Pick `name (id)`.
- Whether `_WHOAMI_BACKOFF_S` lives in `phaze.tasks._shared.agent_bootstrap` or stays in `phaze.tasks.agent_worker`. Moving it consolidates the constant; pick `_shared`.
- Whether the watcher's chunk-of-1 POSTs use a different timeout than 30s (singleton POSTs are fast). Keep 30s for consistency.
- Whether the watcher's SHA-256 computation runs in a thread (`asyncio.to_thread`) to keep the event loop responsive. Yes — mirrors `services/hashing.py` usage in `services/ingestion.py:148`.
- The TTL/eviction policy for the in-memory pending-set. Bounded by D-02's cap. No additional LRU needed.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project & Milestone Context
- `.planning/PROJECT.md` — v4.0 milestone scope; "User-initiated scan + always-on watcher with watchdog, settle/debounce, sentinel scan batch" goal statement; "Watcher catch-up on startup is out of scope" deferred-item lock
- `.planning/REQUIREMENTS.md` §"Topology & Boundary" — DIST-02 (each file server runs agents holding local files and executing file-bearing work locally)
- `.planning/REQUIREMENTS.md` §"Scan & Watcher" — SCAN-01 (admin scan trigger), SCAN-02 (chunked streaming + auto-enqueue), SCAN-03 (always-on watcher via watchdog), SCAN-04 (mtime settle period)
- `.planning/ROADMAP.md` §"Phase 27: Watcher Service & User-Initiated Scan" — goal, depends-on Phase 26, 5 success criteria
- `.planning/STATE.md` §"Accumulated Context → Decisions" — v4.0 locked decisions; especially the v3.0 UI regression noted on Phase 26-11 ("scan_live_set drops in-process FileMetadata artist/title resolution; deferred to a future Phase 27/28 controller-side enrichment task" — NOT in scope for Phase 27 unless explicitly added later)

### Direct Predecessors (MUST read in full)
- `.planning/phases/24-schema-foundation-agent-registry/24-CONTEXT.md` — D-01 (agent.id kebab-case slug `^[a-z0-9]+(-[a-z0-9]+)*$`), D-09 (ScanStatus.LIVE enum), D-10 (sentinel scan_path literal `"<watcher>"`), D-11 (sentinel created at agent-registration time), D-12 (partial unique index `uq_scan_batches_agent_id_live`)
- `.planning/phases/25-internal-agent-http-api-bearer-auth/25-CONTEXT.md` — D-05 (auth dep `get_authenticated_agent`), D-12..D-16 (idempotency contract + `extra="forbid"`), D-20..D-22 (chunked upsert endpoint + auto-enqueue pattern this phase extends)
- `.planning/phases/26-task-code-reorg-http-backed-agent-worker/26-CONTEXT.md` — D-03/D-25 (import-boundary invariant + test), D-09..D-13 (PhazeAgentClient pattern + retry policy), D-14 (AgentSettings split), D-19..D-21 (AgentTaskRouter + per-agent SAQ queue routing), D-22..D-24 (agent_tasks payload schemas + minimal-payload principle)
- `.planning/phases/26-task-code-reorg-http-backed-agent-worker/26-VERIFICATION.md` (if present) — what was actually shipped

### Existing Code to Read Before Modifying

#### Endpoint refactor + new endpoints
- `src/phaze/routers/agent_files.py` — Phase 25's chunked upsert; Phase 27 adds optional `batch_id` field + LIVE-sentinel resolution
- `src/phaze/schemas/agent_files.py` — `FileUpsertChunk` schema; Phase 27 adds `batch_id: UUID | None = None`
- `src/phaze/routers/agent_execution.py` — Phase 25's POST + PATCH pattern; mirror layout for the new PATCH `/scan-batches/{batch_id}` router
- `src/phaze/routers/agent_proposals.py` — Phase 26 D-28's cross-tenant 403-before-state-machine pattern; mirror for the new PATCH endpoint
- `src/phaze/main.py` — `create_app()`; Phase 27 adds 1 new `include_router` (agent_scan_batches) and 1 new pipeline scan router

#### Models (READ — only ScanBatch enum touched indirectly)
- `src/phaze/models/scan_batch.py` — `ScanBatch` model + `ScanStatus.LIVE`; verify partial unique index name
- `src/phaze/models/agent.py` — `Agent` + `scan_roots: list[str]` jsonb (used for dropdown + prefix validation)
- `src/phaze/models/file.py` — `FileRecord` + composite UQ on `(agent_id, original_path)`

#### scan_directory task body + agent-side machinery
- `src/phaze/tasks/scan.py` — existing `scan_live_set`; new `scan_directory` lands alongside (D-13)
- `src/phaze/tasks/agent_worker.py` — register new task in `settings.functions`; refactor `_whoami_with_retry` import per D-17
- `src/phaze/services/agent_client.py` — `PhazeAgentClient`; Phase 27 uses existing `upsert_files` method; adds a new `patch_scan_batch(batch_id, payload)` method
- `src/phaze/schemas/agent_tasks.py` — Phase 26's payload schemas; Phase 27 adds `ScanDirectoryPayload` (D-14)
- `src/phaze/services/agent_task_router.py` — Phase 26 D-19; `enqueue_for_agent(agent_id, "scan_directory", payload)` is the controller-side call site

#### Watcher new package
- `src/phaze/agent_watcher/__init__.py` — NEW
- `src/phaze/agent_watcher/__main__.py` — NEW
- `src/phaze/agent_watcher/observer.py` — NEW
- `src/phaze/agent_watcher/debouncer.py` — NEW
- `src/phaze/agent_watcher/poster.py` — NEW
- `src/phaze/tasks/_shared/agent_bootstrap.py` — NEW (D-17; shared with agent_worker)

#### Admin UI
- `src/phaze/templates/pipeline/dashboard.html` — Phase 27 adds the "Trigger Scan" card above the existing stats panel
- `src/phaze/templates/tracklists/partials/scan_progress.html` — existing HTMX poll-partial pattern to mirror for the new `scan_progress_card.html`
- `src/phaze/routers/pipeline.py` (if it exists; otherwise the page-render router) — Phase 27 adds `POST /pipeline/scans` form handler + `GET /pipeline/scans/{batch_id}` poll endpoint + the agent-dropdown HTMX swap endpoint

#### Reference patterns (READ, do not modify)
- `src/phaze/services/ingestion.py:45-88` — `discover_and_hash_files` pattern (NFC normalize, classify by extension, SHA-256, per-file skip on OSError) — scan_directory's walk body adapts this WITHOUT the `LEGACY_AGENT_ID` stamping (D-12)
- `src/phaze/services/ingestion.py:91-119` — `bulk_upsert_files` pattern (now lives controller-side; the agent calls `client.upsert_files(chunk)` instead)
- `src/phaze/routers/scan.py` — legacy `/api/v1/scan`; remains for backwards-compat reading but is NOT used by Phase 27's flow (could be deprecated as a follow-up — see Deferred)
- `src/phaze/services/discogs_matcher.py:21-46` — `DiscogsographyClient` pattern; mirrored by PhazeAgentClient (Phase 26 D-09)
- `src/phaze/routers/agent_auth.py` — `get_authenticated_agent` dep used verbatim by the new PATCH router

### Configuration & Wiring
- `src/phaze/config.py` — `AgentSettings`; Phase 27 adds `watcher_settle_seconds`, `watcher_max_pending_seconds`, `watcher_sweep_interval_seconds`, `scan_chunk_size` per D-03/D-11
- `docker-compose.yml` — adds new `watcher` service block per D-19
- `pyproject.toml` — adds `watchdog>=4.0` to `[project].dependencies` per D-23
- `justfile` — verify `just sync` / dev recipes exist; per memory rule, add any watcher-specific recipe if it improves DX
- `CLAUDE.md` — Python 3.13, uv, mypy strict, ruff 150 char, pre-commit frozen SHAs

### Tests
- `tests/test_task_split.py` — Phase 26 D-25 import-boundary test; Phase 27 adds a parallel case for `phaze.agent_watcher`
- Phase 26 contract-test pattern under `tests/test_routers/test_agent_*.py` — mirrored for the new PATCH endpoint

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`PhazeAgentClient`** (`src/phaze/services/agent_client.py`) — the watcher uses it verbatim for HTTP posts (Phase 26 D-09..D-13). Add one new method `patch_scan_batch(batch_id, payload)`.
- **`AgentTaskRouter`** (`src/phaze/services/agent_task_router.py`) — Phase 26 D-19; the controller's `POST /pipeline/scans` handler uses `enqueue_for_agent` to route `scan_directory` to the chosen agent's queue.
- **`get_authenticated_agent` + `agent_id` derivation** (`src/phaze/routers/agent_auth.py`) — Phase 25 AUTH-01; the new PATCH endpoint uses it verbatim.
- **`discover_and_hash_files`** (`src/phaze/services/ingestion.py:45-88`) — the walk body is the reference template for the agent-side scan_directory walk (NFC normalize, classify, SHA-256, per-file skip). Notable difference: agent-side walk does NOT stamp `LEGACY_AGENT_ID`; the controller stamps `agent_id` from the bearer token on the upsert (Phase 25 AUTH-01).
- **`templates/tracklists/partials/scan_progress.html`** — exact HTMX poll-partial pattern to mirror (`hx-get`, `hx-trigger="every 2s"`, conditional final-state swap).
- **`hashing.compute_sha256`** (`src/phaze/services/hashing.py`) — used by both the watcher (chunk-of-1) and scan_directory.
- **`EXTENSION_MAP` + `FileCategory.MUSIC/VIDEO`** (`src/phaze/constants.py`) — the watcher filters events to known extensions; scan_directory filters via the same map.

### Established Patterns
- **One router file per resource** — Phase 27 adds `agent_scan_batches.py` (new endpoint) and extends `agent_files.py` (new field).
- **Cross-tenant guard placement** — Phase 26 D-08: 403 before state-machine evaluation. Mirror in PATCH `/scan-batches/{batch_id}`.
- **Idempotent same-state PATCH** — Phase 26 D-08: re-PATCH to same state echoes row, no `updated_at` bump.
- **`ctx[...]` shared resources** — agent_worker startup writes `ctx["api_client"]`, `ctx["agent_identity"]`; scan_directory reads them. Watcher uses the same `PhazeAgentClient` but constructs it directly in `agent_watcher.__main__` (no SAQ ctx).
- **Per-agent SAQ queue routing** — Phase 26 D-18/D-19: `phaze-agent-<agent_id>` queue name; controller routes `scan_directory` via `AgentTaskRouter`.
- **HTMX poll partial swap-on-finish** — existing `tracklists/scan_progress.html` halts polling by replacing itself with a non-`hx-trigger` final-state card.
- **Pydantic `extra="forbid"`** — every new schema (`ScanDirectoryPayload`, `ScanBatchPatch`, updated `FileUpsertChunk`) sets it.
- **`AliasChoices` per-field env mapping** — Phase 26-01: new `PHAZE_WATCHER_*` env vars map onto bare field names via `AliasChoices`.

### Integration Points
- **1 new agent-internal router** — `phaze.routers.agent_scan_batches` (PATCH `/api/internal/agent/scan-batches/{batch_id}`)
- **1 new admin-UI router** — `phaze.routers.pipeline_scans` (or extend the existing pipeline router if one exists): `POST /pipeline/scans`, `GET /pipeline/scans/{batch_id}` (poll partial), `GET /pipeline/scans/agent/{agent_id}/scan-roots` (HTMX swap for the second selector)
- **1 schema extension** — `FileUpsertChunk.batch_id: UUID | None = None` in `phaze.schemas.agent_files`
- **1 new agent SAQ task** — `phaze.tasks.scan.scan_directory` registered in `agent_worker.settings.functions`
- **1 new agent payload schema** — `phaze.schemas.agent_tasks.ScanDirectoryPayload`
- **1 new agent-side standalone process** — `phaze.agent_watcher` package; compose service `watcher`
- **1 new shared bootstrap module** — `phaze.tasks._shared.agent_bootstrap` (refactored from agent_worker)
- **5 new tests + 1 existing-test extension** (D-22)
- **1 docker-compose.yml change** — new `watcher` service block
- **1 pyproject.toml change** — `watchdog>=4.0` runtime dep
- **2 new AgentSettings fields** — `watcher_*` knobs + `scan_chunk_size`
- **1 new PhazeAgentClient method** — `patch_scan_batch`

### Constraints to Plan Around
- **Watcher module must be Postgres-free** — same invariant as `phaze.tasks.agent_worker` (Phase 26 D-03/D-25). Verified in CI by extending `tests/test_task_split.py`. The shared bootstrap module also must stay Postgres-free (imports only `phaze.config.AgentSettings`, `phaze.services.agent_client`, `phaze.schemas.agent_identity`).
- **`ScanStatus` enum is already deployed** — Phase 24's migration 012 added `LIVE` and the partial unique index `uq_scan_batches_agent_id_live`. Phase 27 does NOT touch the enum or the index. The new PATCH endpoint only accepts `running`, `completed`, `failed` (NOT `live` — operators can't manually flip a sentinel batch).
- **Phase 26-11 v3.0 UI regression is OUT OF SCOPE** — STATE.md notes scan_live_set's artist/title resolution drop was deferred to "future Phase 27/28 controller-side enrichment task." Phase 27 is NOT picking that up; it's still a separate future task.
- **`extra="forbid"` on `FileUpsertChunk`** — adding `batch_id: UUID | None = None` is a non-breaking change for Phase 25 callers (default `None`); no existing callers pass an unknown field.
- **No schema migration** — Phase 27 reuses existing tables (ScanBatch, FileRecord). The new PATCH endpoint writes existing columns. No Alembic revision.
- **AgentSettings `model_validator(mode="after")`** — Phase 26 D-14 validates `agent_api_url` + `agent_token`. Phase 27's new watcher fields have safe defaults so the validator doesn't need extension.
- **Watcher process model is NOT SAQ** — it's a plain `asyncio.run(main())`. Errors in the sweep task must be caught and logged but must NOT crash the process; only fatal init errors (whoami exhaustion, AgentSettings validation) trigger non-zero exit + container restart.

</code_context>

<specifics>
## Specific Ideas

- The watcher's `EventHandler` should subclass watchdog's `FileSystemEventHandler` (not `PatternMatchingEventHandler`) so we can filter inline using the project's `EXTENSION_MAP` + `FileCategory.MUSIC/VIDEO` rule rather than maintaining a separate glob pattern. Keeps the filter in one place.
- The debouncer's pending-set should be a `dict[str, _PendingEntry]` keyed on NFC-normalized absolute path; `_PendingEntry` is a `dataclass(slots=True)` with `first_seen_at: float`, `last_change_at: float`. Use `time.monotonic()` for both — wall-clock skew doesn't matter.
- The sweep loop uses `await asyncio.sleep(settings.watcher_sweep_interval_seconds)` between passes; on shutdown, the asyncio.Event lets the loop exit gracefully and the in-flight POST (if any) completes before the process exits.
- The new `PATCH /api/internal/agent/scan-batches/{batch_id}` endpoint should return the updated `ScanBatch` row (echoed as `ScanBatchPatchResponse`) so the agent can verify state without a follow-up GET — matches the Phase 26 D-28 "echo current row state" idempotency invariant.
- The agent dropdown's HTMX partial swap should target a `<div id="scan-path-picker">` slot; the second selector's `<select>` plus the subpath text input render together so the operator sees the constrained options immediately on agent selection.
- `POST /pipeline/scans` returns a partial that replaces the form's submit-result region — NOT a full page render. The form itself stays open for the operator to trigger another scan immediately.
- "Recent Scans" mini-table on the Pipeline page shows the last 10 ScanBatches across all agents (sorted by `created_at desc`), with columns: agent name, scan_path, status pill, processed/total, elapsed time. The table auto-refreshes via the existing 5s dashboard poll.
- The new `patch_scan_batch` method on `PhazeAgentClient` follows the Phase 26 D-10 verb table: `patch_scan_batch(batch_id: UUID, payload: ScanBatchPatch) -> ScanBatchPatchResponse`. Wrapped by the same tenacity retry policy (D-11) — 5xx retries, 4xx immediate-fail.
- The shared bootstrap module `phaze.tasks._shared.agent_bootstrap` is named with a leading-underscore submodule because the public name is `phaze.tasks._shared` (treated as private from the outside; agent_worker and agent_watcher import directly).
- The Phase 26-11 v3.0 UI regression (scan_live_set artist/title resolution drop) stays deferred — Phase 27 does NOT touch `scan_live_set`. If the operator notices missing artist/title in tracklists post-Phase 27, they file a follow-up.

</specifics>

<deferred>
## Deferred Ideas

- **Watcher delete/move/rename event handling** — PROJECT.md locks v4.0 as `created`-only. A future phase can add `FileDeletedEvent` / `FileMovedEvent` handling once the application server has a "file disappeared" state semantic worked out.
- **Watcher catch-up on startup** — out of scope per PROJECT.md; manual user-initiated scan covers this. A future deployment-hardening phase could add an `--initial-scan` flag if operators want it.
- **Synchronous scan-path preflight via a new agent endpoint** — rejected for Phase 27 (violates the agent→controller HTTP boundary). If operator UX demands immediate-feedback in a later milestone, Phase 29's heartbeat machinery could carry a periodic `roots_snapshot` payload that the controller validates against.
- **SSE for live scan progress** — deferred to Phase 28, which standardizes SSE for execution-dispatch aggregation. Phase 27 uses HTMX polling for consistency with existing patterns.
- **`COMPLETED_WITH_ERRORS` ScanStatus enum value** — rejected for Phase 27 (per-file skips already log warnings; batch-level partial-success is over-engineering for v4.0).
- **Per-agent watcher tuning** — `PHAZE_WATCHER_*` env vars are set per-container (i.e., per-agent), so this is already supported. Database-level "watcher config per agent" UI is deferred.
- **Scheduled re-scans (cron)** — operator triggers manual scans for now. A future phase could add a SAQ cron job that triggers `scan_directory` per agent at configurable intervals.
- **Legacy `/api/v1/scan` deprecation** — Phase 27 leaves the legacy endpoint in place for backwards-compat with any external smoke scripts. Removal is a follow-up doc/code cleanup.
- **scan_live_set artist/title resolution rewrite** — Phase 26-11 STATE.md note. Stays deferred; Phase 27 does not pick it up. A future controller-side enrichment phase will rebuild `FileMetadata`-backed resolution via HTTP boundary.
- **Watcher health/liveness endpoint** — Phase 29 adds heartbeat-based liveness; Phase 27's watcher only logs internal state. The compose `restart: unless-stopped` is the only liveness mechanism in Phase 27.
- **Atomic "scan in progress" lock to prevent overlapping scans on the same scan_path** — for v4.0 personal-collection scale, two concurrent scans of the same path produce the same end-state via idempotent upsert. Optional lock can be added when operator-driven duplicate scans become a real problem.

</deferred>

---

*Phase: 27-watcher-service-user-initiated-scan*
*Context gathered: 2026-05-13*
