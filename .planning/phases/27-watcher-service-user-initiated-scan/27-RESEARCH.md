# Phase 27: Watcher Service & User-Initiated Scan - Research

**Researched:** 2026-05-13
**Domain:** Filesystem watching + chunked HTTP streaming + admin trigger UX
**Confidence:** HIGH

> CONTEXT.md (24 implementation decisions) and UI-SPEC.md (5 component contracts) are exhaustive. This research fills in
> technical/library specifics, surfaces existing-code patterns to mirror byte-for-byte, and answers the 10 focus-area questions
> that the planner needs resolved before producing PLAN.md files. It does NOT re-derive any decision already locked in
> CONTEXT.md.

---

## Summary

Phase 27 introduces an always-on watcher process and an admin-triggered bulk scan. The technical surface is small in concept
but rich in landmines: a Python thread (watchdog Observer) must hand events into an asyncio event loop without races, an
in-memory debouncer must survive realistic write patterns (rsync's `.tmp` files, slow downloads, editor save-then-rename),
and a controller-side endpoint must reuse the Phase 26 `403-before-state-machine` cross-tenant guard verbatim. The good
news is that every architectural pattern this phase needs already exists in the codebase — Phase 25's `agent_files.py`
upsert + auto-enqueue, Phase 26's `PhazeAgentClient` + `AgentTaskRouter`, Phase 26 D-08's cross-tenant guard, and the
existing `tracklists/partials/scan_progress.html` HTMX poll pattern — and they only need composition, not invention.

The library choice (`watchdog>=4.0`) is correct and verified: watchdog 4.0.2 (August 2024) was the first release with
Python 3.13 support, current stable is 6.0.0 (November 2024). On Linux (deployment target) it uses inotify natively with
no extra system packages required. The thread→asyncio bridge is `loop.call_soon_threadsafe(...)` from the EventHandler's
synchronous callbacks; the sweep loop runs purely in asyncio and never touches watchdog state directly.

**Primary recommendation:** Build the four-file `phaze.agent_watcher/` package as a thin orchestrator over (a) `watchdog.observers.Observer`
running in its own thread, (b) an asyncio-owned `dict[str, _PendingEntry]` debouncer accessed via `loop.call_soon_threadsafe`,
and (c) the existing `PhazeAgentClient.upsert_files` method for the singleton POST. The controller-side surface (one new PATCH
endpoint, one optional `batch_id` field on the existing upsert, one new admin UI router) is small, well-precedented, and
needs no new architecture.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions (24 total — verbatim from CONTEXT.md `<decisions>`)

**Watcher Event Model & Settle Behavior**
- **D-01:** Watcher subscribes to `FileCreatedEvent` + `FileModifiedEvent`; one in-memory pending-set entry per NFC-normalized
  absolute path; each event resets `last_change_at`; sweep task runs every `sweep_interval_seconds` (default 2s); when
  `now - last_change_at >= settle_period_seconds` (default 10s), compute SHA-256, build `FileUpsertRecord`, POST `/api/internal/agent/files`
  with chunk size 1 (no `batch_id` → controller resolves LIVE sentinel), remove the entry.
- **D-02:** Stuck-file cap: `now - first_seen_at > max_pending_seconds` (default 3600s) → log WARNING, remove entry, do NOT post.
- **D-03:** Three new `AgentSettings` fields (env vars via `AliasChoices` per Phase 26-01):
  `watcher_settle_seconds: int = 10` ← `PHAZE_WATCHER_SETTLE_SECONDS`
  `watcher_max_pending_seconds: int = 3600` ← `PHAZE_WATCHER_MAX_PENDING_SECONDS`
  `watcher_sweep_interval_seconds: int = 2` ← `PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS`
- **D-04:** Strict startup — no walk-existing-files on first start. Watcher only reacts to events emitted after Observer is running.

**Admin Scan UX & Path Validation**
- **D-05:** Extend `/pipeline/` with a Trigger Scan card; no new top-level nav entry.
- **D-06:** Agent dropdown (non-revoked agents) → HTMX-swap to `scan_roots` `<select>` + optional subpath text input → POST
  `/pipeline/scans` with `{agent_id, scan_root, subpath}`. Controller joins root + subpath, NFC-normalizes, validates result
  starts with one of the agent's `scan_roots`, no `..`.
- **D-07:** Prefix-only validation; no controller→agent preflight. Bad paths fail at the agent's `os.walk` and PATCH the
  ScanBatch to `FAILED`.
- **D-08:** Progress display — HTMX poll partial `scan_progress_card.html` polling `GET /pipeline/scans/{batch_id}` every 2s;
  terminal-state response omits `hx-trigger` to halt polling.

**`scan_directory` Task Contract**
- **D-09:** Extend `FileUpsertChunk` with `batch_id: UUID | None = None`. Server-side resolution: present → SELECT by id, 404 if
  missing, 403 if `batch.agent_id != calling_agent.id`; absent → SELECT LIVE sentinel by agent_id (guaranteed exactly one via
  Phase 24's `uq_scan_batches_agent_id_live` partial unique index).
- **D-10:** New endpoint `PATCH /api/internal/agent/scan-batches/{batch_id}` in `src/phaze/routers/agent_scan_batches.py`.
  Body: `ScanBatchPatch(total_files, processed_files, status: Literal["running","completed","failed"], error_message)`,
  `extra="forbid"`. Cross-tenant guard returns 403 BEFORE state-machine. Idempotent same-state PATCH is 200 no-op. LIVE is
  a terminal sentinel state — never PATCHed.
- **D-11:** Chunk size: 500 records. New `AgentSettings.scan_chunk_size: int = 500` ← `PHAZE_SCAN_CHUNK_SIZE`. Server-side
  `agent_file_chunk_max = 1000` cap stays.
- **D-12:** Mid-walk error handling — per-file skip + warning log (mirrors `services/ingestion.py:65`). Abort only on
  scan_path not-a-directory or 5xx-after-retry. On abort: PATCH `status=failed`. On clean walk: PATCH `status=completed,
  total_files=N, processed_files=N`. Posted chunks before abort are NOT rolled back (composite UQ makes re-scan idempotent).
- **D-13:** `scan_directory(ctx, *, scan_path, batch_id, agent_id) -> dict` in `src/phaze/tasks/scan.py`. Registered in
  `agent_worker.settings.functions`. Reads `ctx["api_client"]` and `ctx["agent_identity"]`. MUST NOT import `phaze.database`.
- **D-14:** `ScanDirectoryPayload` in `phaze.schemas.agent_tasks` with `model_config = ConfigDict(extra="forbid")`.

**Watcher Service Shape & Module Layout**
- **D-15:** New package `src/phaze/agent_watcher/` with 4 files: `__main__.py` (asyncio.run + bootstrap), `observer.py`
  (watchdog Observer + EventHandler), `debouncer.py` (in-memory pending-set + sweep), `poster.py` (single-record POST adapter).
  Compose command: `uv run python -m phaze.agent_watcher`. Module MUST NOT import `phaze.database`.
- **D-16:** Watcher startup sequence: AgentSettings → PhazeAgentClient → whoami_with_retry → identity → Observer per root +
  asyncio sweep task → block on asyncio.Event until SIGINT/SIGTERM → graceful shutdown.
- **D-17:** Shared startup helpers extracted to `phaze.tasks._shared.agent_bootstrap` (`whoami_with_retry`,
  `construct_agent_client`, `_WHOAMI_BACKOFF_S`). `phaze.tasks.agent_worker.startup` refactors to import from there.
- **D-18:** No /whoami pre-cache of LIVE sentinel batch_id. Every chunk-of-1 POST omits `batch_id`; controller resolves
  server-side from bearer token.
- **D-19:** Compose wiring — add `watcher` service to root `docker-compose.yml` alongside `worker`, `audfprint`, `panako`.
  Volume mount `:ro` matching `worker`. Phase 29 will move both to `docker-compose.agent.yml`.

**Idempotency & Cross-Tenant Guards**
- **D-20:** Watcher events idempotent via composite `(agent_id, original_path)` UQ on `FileRecord`.
- **D-21:** Cross-tenant guard on the new PATCH endpoint AND the new `batch_id` field — 403 before state-machine
  (Phase 26 D-08 pattern).

**Test Infrastructure**
- **D-22:** 7 new test files (see "Validation Architecture" §"Wave 0 Gaps" below for full list).
- **D-23:** `watchdog>=4.0` added to `[project].dependencies`.

**Roadmap & Doc Sweep**
- **D-24:** Single end-of-phase doc commit: STATE.md accumulation + README touchups + per-service README at
  `src/phaze/agent_watcher/README.md`. CLAUDE.md unchanged.

### Claude's Discretion (verbatim from CONTEXT.md)

- Debouncer data structure: `dict[str, _PendingEntry]` with an asyncio.Lock. **Recommended.**
- Sweep task uses sleep-loop (`await asyncio.sleep(sweep_interval)`). **Recommended.**
- Field name: `batch_id` (not `scan_batch_id`). **Recommended.**
- PATCH endpoint returns the updated batch row (echo). **Recommended.**
- Subpath placeholder: empty (conservative). **Recommended.**
- Agent dropdown shows `name (id)`. **Recommended.**
- `_WHOAMI_BACKOFF_S` moves to `phaze.tasks._shared.agent_bootstrap`. **Recommended.**
- Watcher chunk-of-1 POSTs use 30s timeout (matches `PhazeAgentClient` default). **Recommended.**
- Watcher SHA-256 runs in `asyncio.to_thread` (mirrors `services/hashing.py` usage in `services/ingestion.py:148`). **Recommended.**
- No additional LRU on pending-set; D-02's cap bounds memory.

### Deferred Ideas (OUT OF SCOPE — verbatim from CONTEXT.md)

- Watcher delete/move/rename event handling (PROJECT.md locks v4.0 as `created`-only).
- Watcher catch-up on startup.
- Synchronous scan-path preflight via a new agent endpoint.
- SSE for live scan progress (deferred to Phase 28).
- `COMPLETED_WITH_ERRORS` ScanStatus enum value.
- Per-agent watcher tuning via DB-level UI.
- Scheduled re-scans (cron).
- Legacy `/api/v1/scan` deprecation.
- `scan_live_set` artist/title resolution rewrite (Phase 26-11 regression — still deferred).
- Watcher health/liveness endpoint (Phase 29 adds heartbeat).
- Atomic "scan in progress" lock against overlapping scans.

</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| **DIST-02** | Each file server runs one or more agents (SAQ worker + watcher + audfprint + panako sidecars) that hold local files and execute all file-bearing work locally | D-19 adds the `watcher` compose service alongside existing `worker`/`audfprint`/`panako`; module layout in D-15 establishes the standalone Python entry point; Reusable assets §"PhazeAgentClient" lets the watcher reuse the established HTTP boundary verbatim. |
| **SCAN-01** | Administrator can trigger a scan of a specific path on a specific agent from the admin UI; the application server enqueues `scan_directory(scan_path, batch_id)` onto the chosen agent's queue | D-05..D-08 + D-14 cover the controller-side trigger surface; existing `AgentTaskRouter.enqueue_for_agent` (Phase 26 D-19) is the call site for enqueueing onto `phaze-agent-<agent_id>`; UI-SPEC §"Trigger Scan card" specifies the form. |
| **SCAN-02** | As an agent walks the scan path, it streams discovered file records to the application server in chunks (e.g., 500 records per request); the application server upserts each chunk and enqueues `extract_file_metadata` per new music/video file before the scan completes | D-11 chunk size 500; D-12 mid-walk error handling; existing `agent_files.py:99-130` auto-enqueue mechanism extends to `batch_id`-bound POSTs unchanged (the `xmax=0`-based "newly INSERTed" detection is what gates the enqueue; Phase 27 doesn't touch that). |
| **SCAN-03** | Each file server runs an always-on `phaze-agent-watcher` service that observes its configured roots with the `watchdog` library; new file events stream to the application server via the same scan-batch upsert endpoint, attributed to a per-agent sentinel `ScanBatch` | D-09 server-side LIVE-sentinel resolution from bearer token; D-15 watcher package layout; D-18 omit-batch_id-from-watcher-posts policy; existing partial UQ `uq_scan_batches_agent_id_live` (Phase 24) guarantees exactly-one-sentinel-per-agent. |
| **SCAN-04** | The watcher waits for a file's `mtime` to be stable for a configurable settle period (default 10s) before computing SHA-256 and posting it; partial / in-progress writes are not propagated | D-01 settle algorithm; D-02 stuck-file cap; D-03 env-driven knobs; mtime-stability landmines covered in Pitfalls §"mtime stability detection" below. |

</phase_requirements>

---

## Project Constraints (from CLAUDE.md)

| Constraint | Bearing on Phase 27 |
|------------|---------------------|
| Python 3.13 exclusively | `watchdog>=4.0.2` is the minimum for 3.13 support; recommend pinning `>=4.0` (CONTEXT D-23) but the lock file should resolve to ≥4.0.2. **`[VERIFIED: PyPI release history]`** |
| `uv` only — never bare `pip`, `python`, `pytest`, `mypy` | `just sync` / `uv sync` regenerates lock when adding watchdog. Compose command is `uv run python -m phaze.agent_watcher` per D-19. |
| `pre-commit` frozen SHAs; all hooks must pass | New files trigger ruff, mypy, bandit. The new watcher module is excluded from mypy via the existing `^(tests/\|prototype/\|services/)` regex; the agent_watcher package lives under `src/phaze/agent_watcher/` so it IS type-checked. Plan should opt-in or accept strict. |
| Ruff line-length 150; double quotes; isort known-first-party `phaze` | All new files honor. |
| Mypy strict (excluding tests/prototype/services) | `src/phaze/agent_watcher/*` IS type-checked. Watchdog ships its own type stubs (`watchdog>=2.0` includes `py.typed`). `[CITED: gorakhargosh/watchdog README]` |
| `pre-commit-hooks`: bandit `-x tests -s B608` | Bandit will flag `subprocess` calls; the watcher uses none. SHA-256 via `hashlib` is allowed; the existing `services/hashing.py:compute_sha256` is the canonical helper. |
| Minimum 85% coverage; Codecov upload | The 7 new test files in D-22 must hit 85% on the new code paths. Subprocess-isolated import-boundary test (parallel to Phase 26 D-25) is unit-counted via the existing `tests/test_task_split.py`. |
| Workflow: feature → worktree → PR; never push to main | Phase 27 work goes onto `gsd/phase-27-watcher-service-user-initiated-scan` per `.planning/config.json` `branching_strategy: "phase"`. |
| **Justfile is the command runner** | New service may want a `just watcher-up` / `just watcher-logs` recipe. Discretion — confirm with planner. |
| **README per service** | Per memory rule, add `src/phaze/agent_watcher/README.md` (CONTEXT D-24 already covers this). |

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Filesystem event detection | **Agent (file-server)** | — | DIST-02 — only agents touch files. |
| mtime stability debounce | **Agent (file-server)** | — | Per-file timer state lives in-process on the agent; survives across the HTTP boundary only by being NOT crossed. |
| SHA-256 computation | **Agent (file-server)** | — | DIST-02 + DIST-04 — file content never leaves the file server. |
| Singleton POST to `/api/internal/agent/files` | **Agent (HTTP client)** | Application server (HTTP endpoint) | Phase 25 boundary. |
| LIVE sentinel resolution | **Application server (controller)** | — | DIST-04 — agents have no DB access. Bearer-token lookup happens controller-side. |
| ScanBatch row mutation (PATCH) | **Application server (controller)** | — | DIST-04. |
| Admin trigger form rendering | **Application server (Jinja/HTMX)** | — | The application server owns all admin UI. |
| Scan-path validation (prefix + `..` check) | **Application server (controller)** | — | Static, deterministic check against `agent.scan_roots` JSONB; no filesystem stat needed. |
| `scan_directory` walk execution | **Agent (SAQ task body)** | — | TASK-01 — file-bound tasks run on agents. |
| `phaze-agent-<id>` queue routing | **Application server (`AgentTaskRouter`)** | Agent (SAQ consumer) | Phase 26 D-19. |
| `extract_file_metadata` auto-enqueue | **Application server (existing `agent_files.py` handler)** | Agent (SAQ consumer) | The Phase 25 / 26 auto-enqueue is reused unchanged for both batch-bound and sentinel-bound upserts. |
| HTMX poll progress | **Application server (handler) ↔ Browser** | — | Server-rendered partial; browser polls. |

**Why this matters:** The watcher is conceptually "an agent process," but it is NOT a SAQ worker. It is a third role-shaped
process (`phaze-agent-watcher`) sharing the agent role's bearer token + HTTP client + import-boundary invariant but with
its own asyncio runtime. The planner must NOT treat it as another SAQ settings module; D-15 is explicit that it's
`asyncio.run(main())`.

---

## Standard Stack

### Core (no new top-level libraries — Phase 27 adds only `watchdog`)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `watchdog` | `>=4.0` (lock will resolve `>=4.0.2`; latest 6.0.0) | Cross-platform filesystem event monitoring | Industry standard for Python file watching since 2010. Inotify backend on Linux (deployment target) is native; macOS uses FSEvents; Windows uses ReadDirectoryChangesW. Ships type stubs (`py.typed`). v4.0.2 (Aug 2024) added Python 3.13 support; v6.0.0 (Nov 2024) is current. **`[VERIFIED: PyPI release history + Context7 /gorakhargosh/watchdog]`** |
| `httpx` | `>=0.28.1` (already in deps) | HTTP client for `PhazeAgentClient` | Already in deps (Phase 26). |
| `tenacity` | `>=8.5.0` (already in deps) | Retry policy for HTTP calls | Already in deps (Phase 26). |
| `pydantic-settings` | `>=2.14.0` (already in deps) | `AgentSettings` config | Already in deps; new fields use `AliasChoices` per Phase 26-01 pattern. |
| `saq[redis]` | `>=0.26.3` (already in deps) | `scan_directory` SAQ task registration on agent worker | Already in deps. The watcher itself is NOT a SAQ worker; only the `scan_directory` task is SAQ. |

### Supporting (all existing — no new transitive adds)

| Component | Source | When Used |
|---------|---------|-------------|
| `phaze.services.hashing.compute_sha256` | Existing (`services/hashing.py`) | Watcher chunk-of-1 SHA-256; `scan_directory` per-file SHA-256. **MUST run in `asyncio.to_thread` to keep the event loop responsive** (CONTEXT discretion §9, mirrors `services/ingestion.py:148`). |
| `phaze.constants.EXTENSION_MAP` + `FileCategory.MUSIC/VIDEO` | Existing (`constants.py`) | Watcher's EventHandler filters events to known extensions inline (CONTEXT specifics §1 — subclass `FileSystemEventHandler`, not `PatternMatchingEventHandler`). |
| `phaze.services.agent_client.PhazeAgentClient` | Existing (Phase 26) | Watcher reuses verbatim for HTTP posts. Gets ONE new method: `patch_scan_batch(batch_id, payload) -> ScanBatchPatchResponse`. |
| `phaze.services.agent_task_router.AgentTaskRouter` | Existing (Phase 26) | Controller's `POST /pipeline/scans` calls `enqueue_for_agent(agent_id=..., task_name="scan_directory", payload=ScanDirectoryPayload(...))`. |
| `phaze.routers.agent_auth.get_authenticated_agent` | Existing (Phase 25) | New PATCH endpoint uses verbatim. |
| `unicodedata.normalize("NFC", ...)` | stdlib | Used inline (already established at `services/ingestion.py:33` and `routers/agent_files.py:62`). |

### Alternatives Considered (informational — all locked AGAINST by CONTEXT)

| Instead of | Could Use | Why we don't |
|------------|-----------|----------|
| `watchdog>=4.0` | `inotify_simple` (Linux-only) | Less portable, smaller community, no Windows/macOS dev support. Phaze runs `pytest` on developers' macs; needs FSEvents fallback. |
| `watchdog.events.FileSystemEventHandler` | `watchdog.events.PatternMatchingEventHandler` | CONTEXT specifics §1 picks `FileSystemEventHandler` so extension filtering uses `EXTENSION_MAP` inline rather than maintaining a separate glob pattern. Single source of truth for "what we treat as music/video." |
| Polling-based watcher | `watchdog.observers.polling.PollingObserver` | Inotify is reliable on Linux ext4/zfs; polling has latency and CPU cost. PollingObserver is a fallback if a deployment target's FS doesn't support inotify (NFS, FUSE). **NOT needed for v4.0** but document as a known fallback. |
| `asyncio.Queue` consumer pattern for events | `dict[str, _PendingEntry]` with `asyncio.Lock` | CONTEXT discretion §1 picks the dict — simpler, no consumer/producer plumbing, easier to introspect for the cap check. |
| `loop.call_later(...)` rescheduling | `await asyncio.sleep(...)` loop | CONTEXT discretion §2 picks the sleep loop. Cleaner code; the loop owns its own cancellation via `asyncio.Event`. |

### Installation

```toml
# pyproject.toml — Phase 27 adds ONE runtime dep
[project]
dependencies = [
  # ... (existing, alphabetized)
  "watchdog>=4.0",
]
```

```bash
# After editing pyproject.toml
just sync   # equivalent: uv sync
```

### Version verification

```bash
$ python -c "import importlib.metadata; print(importlib.metadata.version('watchdog'))"
# Expected: 4.0.2 or higher (likely 6.0.0 when uv resolves latest)
```

**`[VERIFIED: PyPI metadata]`** — `watchdog` 4.0.2 released 2024-08-11 was the first to add Python 3.13 classifier;
6.0.0 released 2024-11-01 is the current stable. No supply-chain incidents reported.

---

## Architecture Patterns

### System Architecture Diagram

```
                                                       FILE SERVER
                                                      (agent role)
       ┌───────────────────────────────────────────────────────────────────────────────┐
       │                                                                               │
       │   Filesystem  ──inotify──▶  watchdog.Observer  ──events──▶  EventHandler      │
       │     events                  (own OS thread)                 (own OS thread)   │
       │                                                                  │            │
       │                                                                  │            │
       │                                                   loop.call_soon_threadsafe  │
       │                                                                  ▼            │
       │   ┌──────────────────  agent_watcher (asyncio.run)  ──────────────────────┐  │
       │   │                                                                       │  │
       │   │   debouncer.dict[path → PendingEntry]   ◀──── on_event handler ────  │  │
       │   │              ▲                                                        │  │
       │   │              │ sweep every 2s                                         │  │
       │   │              ▼                                                        │  │
       │   │   sweep loop ──ready paths──▶ poster.post_one(record)                │  │
       │   │                                       │                               │  │
       │   │                                       ▼                               │  │
       │   │            PhazeAgentClient.upsert_files (chunk-of-1, batch_id=None)  │  │
       │   │                                       │                               │  │
       │   └───────────────────────────────────────┼───────────────────────────────┘  │
       │                                           │                                  │
       │   ┌────  agent_worker (SAQ, separate proc) ───────┐                          │
       │   │     scan_directory(scan_path, batch_id):      │                          │
       │   │       os.walk → SHA-256 → chunk 500           │                          │
       │   │       PhazeAgentClient.upsert_files(batch_id) │                          │
       │   │       PhazeAgentClient.patch_scan_batch (NEW) │                          │
       │   └───────────────────────────────────────────────┘                          │
       │                                           │                                  │
       └───────────────────────────────────────────┼──────────────────────────────────┘
                                                   │ HTTPS (Phase 25 bearer auth)
                                                   ▼
                                       APPLICATION SERVER (controller role)
       ┌──────────────────────────────────────────────────────────────────────────────┐
       │                                                                              │
       │  POST /api/internal/agent/files (existing — extended with batch_id)          │
       │      ├── batch_id present  → cross-tenant guard → bound to that batch        │
       │      └── batch_id absent   → resolve LIVE sentinel by agent_id              │
       │                                                                              │
       │  PATCH /api/internal/agent/scan-batches/{batch_id} (NEW)                     │
       │      ├── cross-tenant guard (403 BEFORE state-machine)                       │
       │      └── status transition: running → completed | failed                     │
       │                                                                              │
       │  POST /pipeline/scans (NEW admin UI)                                         │
       │      ├── validate(agent_id, scan_root, subpath)                              │
       │      ├── create ScanBatch(status=RUNNING)                                    │
       │      └── AgentTaskRouter.enqueue_for_agent("scan_directory", payload)        │
       │                                                                              │
       │  GET /pipeline/scans/{batch_id} (NEW HTMX poll partial)                      │
       │      └── returns scan_progress_card.html with terminal-state halt            │
       │                                                                              │
       └──────────────────────────────────────────────────────────────────────────────┘
```

**Component Responsibilities**

| File (new) | Responsibility |
|------------|----------------|
| `src/phaze/agent_watcher/__init__.py` | Marker only; exports nothing public. |
| `src/phaze/agent_watcher/__main__.py` | `asyncio.run(main())`. Reads `AgentSettings()`, builds `PhazeAgentClient`, calls `whoami_with_retry` (shared helper from D-17), constructs Observer + sweep task, blocks on `asyncio.Event` until SIGINT/SIGTERM, graceful shutdown. |
| `src/phaze/agent_watcher/observer.py` | `WatcherEventHandler(FileSystemEventHandler)` subclass. `on_created` and `on_modified` filter via `EXTENSION_MAP` then bridge into asyncio via `loop.call_soon_threadsafe(debouncer.touch, normalized_path)`. The Observer + EventHandler run in watchdog's own threads. |
| `src/phaze/agent_watcher/debouncer.py` | `Debouncer` class. `dict[str, _PendingEntry]` keyed on NFC-normalized absolute path. `_PendingEntry(first_seen_at: float, last_change_at: float)` is `@dataclass(slots=True)`. Methods: `touch(path)`, `sweep() -> list[str]` returning ready paths and removing them, `shutdown()`. Uses `time.monotonic()` (CONTEXT specifics §2). |
| `src/phaze/agent_watcher/poster.py` | `Poster` class. One method: `async def post_one(path: str) -> None`. Reads file size + computes SHA-256 in `asyncio.to_thread`, builds `FileUpsertRecord` + `FileUpsertChunk(files=[record])`, calls `client.upsert_files(chunk)`. Handles `AgentApiServerError` (log + retain entry for re-walk via manual scan), `AgentApiClientError` (log + drop). |
| `src/phaze/tasks/_shared/__init__.py` | Empty marker. |
| `src/phaze/tasks/_shared/agent_bootstrap.py` | Refactor target — exports `whoami_with_retry`, `construct_agent_client`, `_WHOAMI_BACKOFF_S` (D-17). Used by both `agent_worker.startup` and `agent_watcher.__main__`. |
| `src/phaze/routers/agent_scan_batches.py` | `PATCH /api/internal/agent/scan-batches/{batch_id}` (D-10). Mirrors `agent_proposals.py` byte-for-byte for cross-tenant guard placement. |
| `src/phaze/routers/pipeline_scans.py` | `POST /pipeline/scans`, `GET /pipeline/scans/{batch_id}`, `GET /pipeline/scans/agent-roots?agent_id=...`. (Discretion: could extend `routers/pipeline.py` with these routes instead of a new file. Recommend NEW FILE to keep routers/pipeline.py focused on the existing pipeline-trigger surface; the new file is the admin-scan surface.) |
| `src/phaze/schemas/agent_scan_batches.py` | `ScanBatchPatch`, `ScanBatchPatchResponse` (D-10). |
| `src/phaze/schemas/pipeline_scans.py` | `TriggerScanForm` (form body for `POST /pipeline/scans`). |
| `src/phaze/templates/pipeline/partials/trigger_scan_card.html` | Per UI-SPEC Component 1. |
| `src/phaze/templates/pipeline/partials/scan_path_picker.html` | Per UI-SPEC Component 2. |
| `src/phaze/templates/pipeline/partials/scan_progress_card.html` | Per UI-SPEC Component 3. |
| `src/phaze/templates/pipeline/partials/recent_scans_table.html` | Per UI-SPEC Component 4. |
| `src/phaze/templates/pipeline/partials/scan_status_pill.html` | Per UI-SPEC Component 5. |
| `src/phaze/templates/pipeline/partials/scan_submit_error.html` | Per UI-SPEC §"Failure surfacing". |

### Pattern 1: Thread → Asyncio Event Bridge (CRITICAL)

**What:** watchdog's `Observer` runs in its own OS thread; the EventHandler's `on_created`/`on_modified` callbacks execute on
that thread. Asyncio state (`dict`, `Lock`) is NOT thread-safe. The bridge is `loop.call_soon_threadsafe(callback, *args)`.

**When to use:** Every event-handler callback that needs to mutate asyncio state.

**Example:**
```python
# src/phaze/agent_watcher/observer.py
# Source: Context7 /gorakhargosh/watchdog quickstart + Python asyncio docs
# https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe
import asyncio
import logging
import unicodedata
from pathlib import Path
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent

from phaze.constants import EXTENSION_MAP, FileCategory

logger = logging.getLogger(__name__)

_EXTRACTABLE: frozenset[FileCategory] = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})


class WatcherEventHandler(FileSystemEventHandler):
    """Bridges watchdog's thread-side events into the asyncio debouncer.

    The Observer thread invokes `on_created`/`on_modified` synchronously. The handler
    filters by extension inline (single source of truth = EXTENSION_MAP) and posts a
    threadsafe callback onto the asyncio loop. NEVER touches debouncer state directly
    from a watchdog thread.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, debouncer_touch: callable) -> None:
        super().__init__()
        self._loop = loop
        self._debouncer_touch = debouncer_touch

    def _filter_and_dispatch(self, src_path: str) -> None:
        if not src_path:
            return
        ext = "." + Path(src_path).suffix.lower().lstrip(".")
        if EXTENSION_MAP.get(ext, FileCategory.UNKNOWN) not in _EXTRACTABLE:
            return
        normalized = unicodedata.normalize("NFC", src_path)
        # call_soon_threadsafe is the ONLY safe asyncio bridge from a non-loop thread.
        self._loop.call_soon_threadsafe(self._debouncer_touch, normalized)

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        self._filter_and_dispatch(event.src_path)

    def on_modified(self, event: FileModifiedEvent) -> None:
        if event.is_directory:
            return
        self._filter_and_dispatch(event.src_path)
```

### Pattern 2: Async Sweep Loop with Graceful Shutdown

**What:** The sweep task runs in asyncio every `sweep_interval_seconds`, asks the debouncer for ready paths, and dispatches
posts. Cancellation via `asyncio.Event` lets `SIGINT`/`SIGTERM` shut down cleanly.

**When to use:** The single sweep task in `__main__.py`.

**Example:**
```python
# src/phaze/agent_watcher/__main__.py — sweep loop sketch
# Source: Python asyncio + CONTEXT D-01/D-02
import asyncio
import logging
import signal
from typing import Any

logger = logging.getLogger(__name__)


async def _sweep_loop(
    debouncer: "Debouncer",
    poster: "Poster",
    sweep_interval: float,
    settle_period: float,
    max_pending: float,
    shutdown_event: asyncio.Event,
) -> None:
    """Run until shutdown_event is set. Each pass:
       1. Ask debouncer for paths whose `last_change_at` is >= settle_period old.
       2. For each: poster.post_one. Failures log and retain the entry (or drop per Pitfall §1).
       3. Sweep the stuck-file cap.
    """
    while not shutdown_event.is_set():
        try:
            ready, evicted = debouncer.sweep(settle_period=settle_period, max_pending=max_pending)
            for path in ready:
                try:
                    await poster.post_one(path)
                except Exception:
                    logger.exception("post failed; entry already removed from debouncer (will be re-walked on manual scan)", extra={"path": path})
            for path in evicted:
                logger.warning("watcher: dropping path=%s; mtime still changing past cap", path)
        except Exception:
            logger.exception("sweep iteration failed")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=sweep_interval)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    cfg = get_settings()  # AgentSettings
    client = construct_agent_client(cfg)  # shared helper (D-17)
    identity = await whoami_with_retry(client)
    debouncer = Debouncer()
    poster = Poster(client=client, agent_id=identity.agent_id)
    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)

    observer = Observer()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=debouncer.touch)
    for root in identity.scan_roots:
        observer.schedule(handler, path=root, recursive=True)
    observer.start()

    try:
        await _sweep_loop(
            debouncer=debouncer,
            poster=poster,
            sweep_interval=cfg.watcher_sweep_interval_seconds,
            settle_period=cfg.watcher_settle_seconds,
            max_pending=cfg.watcher_max_pending_seconds,
            shutdown_event=shutdown_event,
        )
    finally:
        observer.stop()
        observer.join()
        await client.close()
```

### Pattern 3: Cross-Tenant Guard — 403 BEFORE State Machine (Phase 26 D-08 EXACT)

**What:** The new `PATCH /api/internal/agent/scan-batches/{batch_id}` endpoint MUST mirror `routers/agent_proposals.py:62-77`
byte-for-byte. The guard returns 403 BEFORE any state-machine evaluation so an attacker cannot use 409-vs-403 timing as
an oracle.

**When to use:** The new PATCH endpoint AND the new `batch_id`-bound branch of the existing `POST /api/internal/agent/files`.

**Example (verbatim from `src/phaze/routers/agent_proposals.py:62-77` — the planner MUST replicate this exact shape):**
```python
# 404 if proposal_id does not exist
proposal = await session.get(RenameProposal, proposal_id)
if proposal is None:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")

# W1 / T-26-08-S2: cross-tenant guard. Load FileRecord.agent_id and reject if
# the proposal's file belongs to a different agent than the authenticated one.
# Single-operator deployment makes this low-impact today, but the structural
# check matters for future multi-tenant. Returns 403 BEFORE state-machine logic
# so a leaked proposal_id cannot be probed via 409 timing.
file_record = await session.get(FileRecord, proposal.file_id)
if file_record is not None and file_record.agent_id != agent.id:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="proposal does not belong to authenticated agent",
    )
```

For Phase 27's `agent_scan_batches.py`, the parallel sequence is:
```python
batch = await session.get(ScanBatch, batch_id)
if batch is None:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scan batch not found")
if batch.agent_id != agent.id:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="scan batch does not belong to authenticated agent",
    )
# … now state-machine: same-state PATCH = 200 idempotent no-op; running→completed/failed allowed; LIVE rejected …
```

### Pattern 4: HTMX Poll Partial With Final-State Halt (mirrors `tracklists/partials/scan_progress.html`)

**What:** The poll partial's in-progress markup carries `hx-trigger="every 2s"` and `hx-swap="outerHTML"`. When the backend
returns a terminal-state render (no `hx-trigger`, no `hx-get`), HTMX swaps the outer element and polling stops automatically
because the new element has no trigger.

**When to use:** `scan_progress_card.html` per UI-SPEC Component 3.

**Reference (verbatim from `src/phaze/templates/tracklists/partials/scan_progress.html`):**
```jinja
{% if done %}
<div class="bg-white dark:bg-phaze-bg border border-gray-200 dark:border-phaze-border rounded-lg p-4" aria-live="polite">
    {# terminal state — NO hx-trigger here, so polling halts when this replaces the in-progress markup #}
    <p class="text-sm text-gray-900 dark:text-gray-100">Scan complete. …</p>
</div>
{% else %}
<div class="bg-white dark:bg-phaze-bg border border-gray-200 dark:border-phaze-border rounded-lg p-4" aria-live="polite"
     hx-get="/tracklists/scan/status?job_ids={{ job_ids }}"
     hx-trigger="every 3s"
     hx-swap="innerHTML"
     hx-target="#scan-panel">
    <p class="text-sm text-gray-900 dark:text-gray-100">Scanning... ({{ completed }} of {{ total }} files)</p>
</div>
{% endif %}
```

**Phase 27's `scan_progress_card.html` (per UI-SPEC §"Component 3") changes the cadence to `every 2s` and uses
`hx-swap="outerHTML"` (UI-SPEC line 269) — the swap MUST be `outerHTML` because the terminal markup is meant to REPLACE
the polling element, not merely populate its interior.**

### Pattern 5: PhazeAgentClient — Adding `patch_scan_batch`

**What:** Phase 27 adds one new method to `PhazeAgentClient` following the existing verb table exactly. The retry policy,
exception hierarchy, and logging shape are inherited from the existing `_request` funnel.

**When to use:** Agent-side scan_directory PATCHes the batch after each chunk + at task end.

**Example (mirrors `patch_proposal_state` at `src/phaze/services/agent_client.py:280-293`):**
```python
async def patch_scan_batch(
    self,
    batch_id: uuid.UUID,
    payload: ScanBatchPatch,
) -> ScanBatchPatchResponse:
    """PATCH /api/internal/agent/scan-batches/{batch_id} -- update batch status/counts (Phase 27 D-10)."""
    from phaze.schemas.agent_scan_batches import ScanBatchPatchResponse  # noqa: PLC0415

    response = await self._request(
        "PATCH",
        f"/api/internal/agent/scan-batches/{batch_id}",
        json=payload.model_dump(mode="json", exclude_unset=True),
    )
    return ScanBatchPatchResponse.model_validate(response.json())
```

The `exclude_unset=True` matches the established pattern (used by `put_metadata`, `put_fingerprint`, `put_analysis`,
`patch_execution_log`, `patch_proposal_state`) so partial PATCHes work correctly — only fields the agent set get sent.

### Anti-Patterns to Avoid

- **`PatternMatchingEventHandler` for extension filtering** — CONTEXT specifics §1 picks `FileSystemEventHandler` so filtering
  goes through `EXTENSION_MAP`. Don't introduce a second source of truth for "what we treat as music."
- **Mutating debouncer state from the watchdog thread** — race conditions, silently dropped events. ALL state mutation goes
  through `loop.call_soon_threadsafe`.
- **Synchronous SHA-256 in the asyncio loop** — large files (multi-GB concert videos) will block the event loop and miss
  events. Use `asyncio.to_thread(compute_sha256, path)`.
- **Stat-based "is file complete?" heuristics** — file size check, "is mtime in the last 100ms" — all unreliable across
  rsync/cp/wget/atomic-rename patterns. The mtime-stable-for-N-seconds debounce is the working design.
- **Reading `FileRecord.agent_id` from a request body** — AUTH-01 invariant. Existing `agent_files.py:62-65` stamps it from
  the auth dep. The new `batch_id` field is the ONLY new request field; agent_id stays implicit.
- **Stuck pending entries growing unbounded** — without D-02's cap, a single corrupt always-being-touched file (e.g., a log
  file under a watched root that turns out to have a music extension) leaks memory forever.
- **Walking existing files on watcher startup** — D-04 explicitly bans this. Users with 200K files would re-SHA-256 every
  file on every container restart.
- **Importing `phaze.database` from `phaze.agent_watcher`** — same import-boundary as Phase 26 D-25. The new
  `tests/test_task_split.py` case (D-22) enforces this.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Cross-platform filesystem event monitoring | Polling loop, inotify-only wrapper | `watchdog>=4.0` | 15-year-old library, ships type stubs, used by Django dev server, pytest-watch, mkdocs serve, etc. Inotify on Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows — single API. **`[VERIFIED: Context7 /gorakhargosh/watchdog]`** |
| Thread → asyncio bridge | Custom Queue, Condition variables | `loop.call_soon_threadsafe(...)` | Standard library; documented as "the safe bridge that allows a regular, separate thread to schedule a callback function to be run on the event loop's main thread". **`[CITED: docs.python.org/3/library/asyncio-eventloop.html]`** |
| Per-file timer | `dict[path, asyncio.Task]` (one task per file) | Single sweep loop + `dict[path, _PendingEntry]` | Per-file tasks scale poorly (thousands of concurrent timers); single sweep is O(pending entries) every 2s. CONTEXT D-01 locks this. |
| HTTP retry policy | Custom try/except + sleep | `tenacity.AsyncRetrying` (existing `PhazeAgentClient._request`) | Already in deps; Phase 26 D-11 funnel handles 4xx-no-retry / 5xx-retry split. The watcher's POSTs inherit this for free. |
| Cross-tenant guard | Inline conditional in each handler | Mirror `routers/agent_proposals.py:71-76` byte-for-byte | Phase 26 D-08 is a documented invariant; deviating would create a security regression. |
| Cap-bounded in-memory state | LRU cache | Per-iteration sweep + `first_seen_at` check | LRU evicts by access; we need eviction by AGE (per D-02). A 1-line `now - first_seen_at > cap` check in the sweep is simpler and explicit. |
| LIVE sentinel batch_id pre-cache | `whoami` → cache `live_batch_id` on agent → send with every POST | Omit `batch_id`; controller resolves server-side | CONTEXT D-18 — extra agent→controller round-trip adds no benefit; partial UQ guarantees one-indexed-lookup cost on the controller. |
| File-completion detection heuristics | `os.fstat` + size watching + "open file" detection | mtime-stable-for-10s debounce | rsync, cp, wget, editors all behave differently; only "no new mtime change for N seconds" is universally correct. See Pitfalls §1. |

**Key insight:** Every "I bet I could write a smaller version of X" temptation in Phase 27 has been considered and rejected in
CONTEXT.md. The phase's complexity lives in the *composition* of existing patterns — not in inventing new ones.

---

## Runtime State Inventory

This is a feature-add phase, NOT a rename/refactor phase. No runtime-state migration concerns apply.

For completeness — confirmed by reading every D-decision and verifying no string-replacement is in scope:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | **None.** No model/column rename or value migration. Phase 27 reuses ScanBatch + FileRecord. The new `batch_id` field on `FileUpsertChunk` is wire-only; the DB column `files.batch_id` already exists and is unchanged. | none |
| Live service config | **None.** No external service config changes. The new `watcher` compose service is additive. | none |
| OS-registered state | **None.** Watcher runs inside a container; no systemd/launchd/Task Scheduler entries. | none |
| Secrets/env vars | New env vars are additive: `PHAZE_WATCHER_SETTLE_SECONDS`, `PHAZE_WATCHER_MAX_PENDING_SECONDS`, `PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS`, `PHAZE_SCAN_CHUNK_SIZE`. All have defaults; absence does not break. The existing `PHAZE_AGENT_TOKEN` is REUSED by the watcher (D-19). | Update `.env.example` to document the four new optional vars. |
| Build artifacts | **None.** New package `src/phaze/agent_watcher/` is created fresh; no stale package metadata to invalidate. `uv sync` after pyproject.toml change refreshes the lock file as a routine step. | `uv sync` after pyproject edit. |

**Nothing-to-migrate verdict:** verified — every Phase 27 introduction is additive.

---

## Common Pitfalls

### Pitfall 1: mtime Stability Across Realistic Write Patterns

**What goes wrong:** The "stable mtime" debounce assumes a writer modifies an existing file in place repeatedly. Real-world
writers behave very differently:

| Writer | mtime behavior | Watcher behavior |
|--------|---------------|------------------|
| `cp src dst` (local filesystem) | Single write; mtime set once at copy completion. | Single `on_created`; settles after 10s of no further events. **Works correctly.** |
| `rsync src dst` (no `--inplace`) | Writes to `dst.~tmp~XXXX` (or similar); on completion, atomically renames to `dst`. The temp file has the music extension but `dst` appears via `FileMovedEvent` (which Phase 27 doesn't subscribe to). | Watcher sees `on_created` for `.~tmp~`; if extension matches, debouncer holds it. On rename, the temp path is *gone* — but the debouncer still has it. After 10s with no further `on_modified` (because the file was renamed away), debouncer fires SHA-256 on a path that no longer exists → `OSError`. **Edge case: handle `OSError` in `Poster.post_one` by dropping the entry.** |
| `rsync --inplace` | Repeated writes to the destination path; mtime updates with each block. | `on_modified` resets the timer repeatedly until rsync finishes. **Works correctly.** |
| `wget` / `curl -O` | Writes to a final filename incrementally. Each block flush updates mtime. | `on_modified` resets timer. When download stalls beyond 10s (slow link), watcher may post a partial file. **Mitigation: D-02's cap is the safety net; if the download eventually finishes, the next mtime change starts the 10s timer again. The risk is "download completed at exactly 10s of idle" — vanishingly rare in practice.** |
| `wget -c` (resume) | Initial seek to end of partial file (mtime change), then incremental writes. | Same as wget. |
| Vim / many editors save-then-rename | Writes to `.foo.mp3.swp` or `.foo.mp3.4n83BC` (period prefix or random suffix). Then renames over the real path. | Vim hidden temp files start with `.` — `EXTENSION_MAP` filter on `.swp` keeps them out. The atomic rename surfaces as `FileMovedEvent` (NOT subscribed). The original `.foo.mp3` was already in the debouncer though — it'll re-fire when `on_modified` lands after the rename. **Likely works; verify in tests.** |
| `truncate -s 0 foo.mp3 && cat src > foo.mp3` | First op sets mtime + file_size=0; second op grows it. | `on_modified` resets timer through the grow. **Works correctly.** |
| Concurrent SAQ scan-walk vs. watcher | The scan_directory walk reads + SHAs the same path the watcher is already debouncing. | Composite UQ on `(agent_id, original_path)` makes the duplicate POST a no-op UPDATE (CONTEXT D-20). **Works correctly.** |

**Why it happens:** filesystems make no "this file is complete now" guarantee. mtime-stable-for-N is a heuristic, not a proof.

**How to avoid:** (a) trust the heuristic, (b) **handle `OSError` (vanished path) gracefully in `Poster.post_one`** — log
DEBUG, drop the entry, do not crash the sweep. (c) Test all six writer patterns above in `tests/test_agent_watcher/`.

**Warning signs:** OSError on stat/sha256 → debouncer pending grows without firing → "post failed with OSError" log spam.

### Pitfall 2: Watchdog Observer Thread Safety With Asyncio

**What goes wrong:** Directly calling `debouncer.touch(path)` from `on_created` (which runs in watchdog's thread) races with
the sweep loop reading the same dict from the asyncio loop. Symptoms: `RuntimeError: dictionary changed size during iteration`,
silently lost events, occasional `asyncio.InvalidStateError`.

**Why it happens:** Python's `dict` is NOT thread-safe for concurrent mutation+iteration. Even individual operations can
fail under heavy event load.

**How to avoid:** EVERY callback from a watchdog thread MUST use `loop.call_soon_threadsafe(callback, *args)`. The
debouncer's `touch`, `sweep`, `evict` methods are then called only from the asyncio thread.

**Warning signs:** Intermittent test failures under high event rate; `RuntimeError: dictionary changed size during iteration`;
events apparently lost.

### Pitfall 3: NFC Normalization Drift Between Watcher and scan_directory

**What goes wrong:** macOS NFD-encodes filenames at the filesystem level (e.g., `é` becomes `e` + combining acute U+0301);
Linux ext4 stores whatever the writer wrote. If the watcher and `scan_directory` use different normalizations, they produce
two different `original_path` values for the same logical file — and the composite UQ doesn't catch it (different strings,
different rows).

**Why it happens:** Filesystem-level encoding mismatches; `unicodedata.normalize("NFC", ...)` only applies if you call it.

**How to avoid:** Apply `unicodedata.normalize("NFC", str(full_path))` consistently — once in the watcher's EventHandler
(`observer.py`), once in `scan_directory`'s walk body. Established at `src/phaze/services/ingestion.py:33` and
`src/phaze/routers/agent_files.py:62`. Test: drop a file with combining-character name, verify both code paths produce the
same `original_path`.

**Warning signs:** Same file appears with two FileRecord rows in DB (one from watcher, one from scan); rows differ only in
the byte representation of `original_path`.

### Pitfall 4: `os.walk` Symlink / Hidden-Dir Semantics

**What goes wrong:** `scan_directory`'s walk uses `os.walk(scan_root)` by default. Default behavior:
- `followlinks=False` — symlinks to directories are NOT followed. `services/ingestion.py:55` sets this explicitly.
- Hidden directories (`.git`, `.cache`) ARE walked.
- Permission-denied directories raise `PermissionError` that propagates out of the walk unless caught.

**Why it happens:** Defaults aren't always what we want. A `.git` directory under `/data/music` would be walked and SHA-256'd.

**How to avoid:** Mirror `services/ingestion.py:55-87` verbatim — `os.walk(scan_root, followlinks=False)` + try/except `OSError`
per file (line 65). Optionally filter hidden directories by removing entries from `dirnames` in-place during walk:
```python
for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=False):
    dirnames[:] = [d for d in dirnames if not d.startswith(".")]   # skip hidden dirs
    for filename in filenames:
        ...
```
**Discretion:** the existing `discover_and_hash_files` does NOT filter hidden directories. Recommend matching that behavior
for Phase 27 (preserves backwards compatibility); if a follow-up phase wants to skip hidden dirs, do it everywhere.

**Warning signs:** `.git/objects/...` files showing up in `FileRecord`; permission errors halting a scan partway through;
extension-classified hidden temp files (e.g., `.~tmp.mp3`) showing up.

### Pitfall 5: SAQ Settings Module Import-Time Failure on Watcher Container

**What goes wrong:** The watcher process does NOT use SAQ but DOES live in the same Docker image as the agent worker. If
operator misconfigures and sets `PHAZE_ROLE=agent` for the watcher container without `PHAZE_AGENT_QUEUE`, importing
`phaze.tasks.agent_worker` would fail at module-load time (lines 191-196). **But** the watcher entry point is
`uv run python -m phaze.agent_watcher`, NOT `uv run saq phaze.tasks.agent_worker.settings`. So `phaze.tasks.agent_worker`
is never imported by the watcher process. **Verify this in the import-boundary test (D-22 extension).**

**Why it happens:** Same image, different entry points; easy to confuse.

**How to avoid:** The import-boundary test for `phaze.agent_watcher` MUST assert that `phaze.tasks.agent_worker` is NOT in
`sys.modules` after `import phaze.agent_watcher`. (In addition to the existing checks for `phaze.database`,
`phaze.tasks.session`, `sqlalchemy.ext.asyncio`.)

**Warning signs:** Watcher container exits with `RuntimeError: PHAZE_AGENT_QUEUE env var is required for agent_worker`.

### Pitfall 6: Compose `depends_on` Race With API Service

**What goes wrong:** The watcher's first call is `client.whoami()` which hits the application server. If the watcher
container starts before the `api` container is accepting connections, `whoami_with_retry`'s exponential backoff
(1, 2, 4, 8, 16, 32s = ~63s) handles it — but only if the API takes <63s to come up.

**Why it happens:** Docker Compose `depends_on: condition: service_started` waits for the container to start, NOT for the
HTTP server to be ready. The Phase 25 API container has no `healthcheck`; it just starts uvicorn.

**How to avoid:** D-19 already declares `depends_on: api: condition: service_started` + `restart: unless-stopped`. The retry
budget handles the common-case ~5s uvicorn boot. For longer Postgres-migration boot times (rare in dev, possible on first
deployment), the container restarts and tries again.

**Warning signs:** Watcher container restarts ~once on a fresh deploy, then settles.

### Pitfall 7: `restart: unless-stopped` Masks Real Failures

**What goes wrong:** A misconfigured token causes 401 → `AgentApiAuthError` → `_whoami_with_retry` raises RuntimeError →
container exits non-zero → compose restarts the container → infinite restart loop, no log alerts.

**Why it happens:** `restart: unless-stopped` is the right policy for transient failures (API not yet up) but the wrong
policy for permanent misconfiguration. Phase 27 has no liveness/heartbeat surface (deferred to Phase 29).

**How to avoid:** Treat 401/403 specially in `_whoami_with_retry`: if the FIRST attempt returns 401/403 (AgentApiAuthError),
DON'T retry — fail fast and let the container restart, but log at ERROR with a clear "auth invalid; check PHAZE_AGENT_TOKEN"
message so the operator sees it in `docker compose logs`. The existing `_whoami_with_retry` at
`src/phaze/tasks/agent_worker.py:73-89` catches `AgentApiError` broadly and retries; **recommend tightening this in the
shared bootstrap module (D-17) to skip retries on `AgentApiAuthError`** since that's a permanent misconfiguration.

**Warning signs:** Container restart count climbing in `docker compose ps`; `/whoami probe failed: AgentApiAuthError`
repeated in logs.

---

## Code Examples

### Debouncer state machine (verified pattern; mirrors CONTEXT D-01/D-02 + specifics §2)

```python
# src/phaze/agent_watcher/debouncer.py
# Source: CONTEXT D-01, D-02, specifics §2, §3 (dict-with-lock pattern; monotonic clock)
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingEntry:
    first_seen_at: float
    last_change_at: float


class Debouncer:
    """In-memory pending-set for mtime-debounced file events.

    All public methods are called from the asyncio loop ONLY. The
    watchdog Observer bridges into this via `loop.call_soon_threadsafe(touch, path)`.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingEntry] = {}

    def touch(self, path: str) -> None:
        """Record an event for `path`. Resets `last_change_at`."""
        now = time.monotonic()
        entry = self._pending.get(path)
        if entry is None:
            self._pending[path] = _PendingEntry(first_seen_at=now, last_change_at=now)
        else:
            entry.last_change_at = now

    def sweep(self, settle_period: float, max_pending: float) -> tuple[list[str], list[str]]:
        """One sweep pass. Returns (ready_paths, evicted_paths). Mutates pending set.

        - ready: `now - last_change_at >= settle_period`. Removed before return.
        - evicted: `now - first_seen_at > max_pending` (the D-02 cap). Removed before return.
        """
        now = time.monotonic()
        ready: list[str] = []
        evicted: list[str] = []
        for path, entry in list(self._pending.items()):
            if now - entry.first_seen_at > max_pending:
                evicted.append(path)
                del self._pending[path]
            elif now - entry.last_change_at >= settle_period:
                ready.append(path)
                del self._pending[path]
        return ready, evicted

    def pending_count(self) -> int:
        """For observability / tests."""
        return len(self._pending)
```

### Poster — chunk-of-1 POST with OSError handling (Pitfall 1 mitigation)

```python
# src/phaze/agent_watcher/poster.py
# Source: CONTEXT D-01 (chunk size 1) + Pitfall 1 (handle vanished paths)
from __future__ import annotations

import asyncio
import logging
import unicodedata
from pathlib import Path

from phaze.services.agent_client import (
    AgentApiClientError,
    AgentApiError,
    AgentApiServerError,
    PhazeAgentClient,
)
from phaze.services.hashing import compute_sha256
from phaze.schemas.agent_files import FileUpsertChunk, FileUpsertRecord

logger = logging.getLogger(__name__)


class Poster:
    """Adapts a single watcher event into a chunk-of-1 POST via PhazeAgentClient."""

    def __init__(self, client: PhazeAgentClient, agent_id: str) -> None:
        self._client = client
        self._agent_id = agent_id

    async def post_one(self, path: str) -> None:
        """POST one record to /api/internal/agent/files. batch_id omitted -> LIVE sentinel."""
        p = Path(path)
        try:
            file_size = await asyncio.to_thread(lambda: p.stat().st_size)
            sha256 = await asyncio.to_thread(compute_sha256, p)
        except OSError as exc:
            # Pitfall 1: file vanished (rsync atomic-rename, transient unmount).
            # Drop the entry; do NOT crash the sweep loop.
            logger.debug("watcher: path vanished before post; dropping path=%s err=%s", path, exc)
            return

        record = FileUpsertRecord(
            sha256_hash=sha256,
            original_path=unicodedata.normalize("NFC", path),
            original_filename=unicodedata.normalize("NFC", p.name),
            current_path=unicodedata.normalize("NFC", path),
            file_type=p.suffix.lower().lstrip("."),
            file_size=file_size,
        )
        # D-18: batch_id omitted; controller resolves LIVE sentinel from bearer token.
        chunk = FileUpsertChunk(files=[record])
        try:
            await self._client.upsert_files(chunk)
        except AgentApiClientError:
            # 4xx -- bad request / forbidden / etc. Log + drop; operator can re-walk via /pipeline scan.
            logger.exception("watcher: 4xx posting path=%s; dropping", path)
        except AgentApiServerError:
            # 5xx after retries -- transient; the next manual scan will re-walk and pick this up.
            logger.exception("watcher: 5xx posting path=%s; dropping (will recover via manual scan)", path)
        except AgentApiError:
            logger.exception("watcher: unknown error posting path=%s; dropping", path)
```

### Schemas — FileUpsertChunk extension + new ScanBatchPatch

```python
# src/phaze/schemas/agent_files.py — Phase 27 D-09 EXTENSION
# Add this line to FileUpsertChunk; existing fields unchanged.
import uuid

class FileUpsertChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[FileUpsertRecord] = Field(min_length=1, max_length=_CHUNK_MAX)
    batch_id: uuid.UUID | None = None   # Phase 27 D-09: absent -> LIVE sentinel resolution
```

```python
# src/phaze/schemas/agent_scan_batches.py — NEW (Phase 27 D-10)
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScanBatchPatch(BaseModel):
    """Request body for PATCH /api/internal/agent/scan-batches/{batch_id}."""

    model_config = ConfigDict(extra="forbid")

    total_files: int | None = Field(default=None, ge=0)
    processed_files: int | None = Field(default=None, ge=0)
    status: Literal["running", "completed", "failed"] | None = None
    error_message: str | None = None


class ScanBatchPatchResponse(BaseModel):
    """Echo the updated batch row (D-discretion §4: returns full row, not 200 {})."""

    batch_id: uuid.UUID
    agent_id: str
    scan_path: str
    status: str
    total_files: int
    processed_files: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime
```

### Compose service block (CONTEXT D-19)

```yaml
# docker-compose.yml — APPENDED to existing services
# Phase 29 will move this + the existing `worker` to docker-compose.agent.yml.
watcher:
  build:
    context: .
    dockerfile: Dockerfile
  command: uv run python -m phaze.agent_watcher
  env_file: .env
  environment:
    - PHAZE_ROLE=agent
    # PHAZE_AGENT_API_URL, PHAZE_AGENT_TOKEN, PHAZE_AGENT_SCAN_ROOTS read from .env
    # PHAZE_WATCHER_* optional knobs read from .env (defaults: 10s settle, 2s sweep, 3600s cap)
  volumes:
    - "${SCAN_PATH:-/data/music}:/data/music:ro"
  depends_on:
    api:
      condition: service_started
  restart: unless-stopped
```

**Note on volume `:ro`:** matches `worker` (line 33 of existing compose). The watcher reads files for SHA-256; it never
writes. This is CORRECT for v4.0.

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `inotify-tools` shell scripts (`inotifywait \| while read; do ...`) | Python `watchdog.Observer` | 2010-ish | Single API across Linux/macOS/Windows; type stubs; battle-tested. |
| `pyinotify` (Linux-only, last release 2015) | `watchdog` | watchdog 1.0 (2018) | Cross-platform; actively maintained; type stubs. |
| Polling-based watchers (`stat` in a loop) | `watchdog` (inotify-backed) | Long ago | Inotify scales to thousands of watched files with near-zero CPU overhead vs polling. |
| Watchdog with synchronous-only handlers | Watchdog + asyncio via `call_soon_threadsafe` | Standard pattern since Python 3.4 | Lets event handlers integrate with FastAPI/SAQ/asyncio applications without thread-pool gymnastics. |

**Deprecated / outdated alternatives:**
- `pyinotify` — last PyPI release 2015, Linux-only, unmaintained.
- `python-fsmonitor` — niche, abandoned.
- `aiofiles.os.watch` — does not exist; aiofiles is unrelated.
- `asyncinotify` — Linux-only, less ecosystem usage.

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| (none) | All claims in this research are tagged `[VERIFIED: ...]` or `[CITED: ...]` or are direct restatements of CONTEXT.md decisions. | — | — |

**This table is empty:** all claims were either verified against tools (PyPI, Context7) or restated from CONTEXT.md/codebase
inspection. No user confirmation needed.

---

## Open Questions

1. **Hidden-directory filtering in `scan_directory`'s walk.**
   - What we know: `services/ingestion.py:55-87` does NOT filter hidden directories; CONTEXT specifics §reference §4 says
     "mirrors `services/ingestion.py:65`."
   - What's unclear: should `scan_directory` skip `.git`, `.cache`, etc.?
   - Recommendation: **Match existing behavior — don't filter.** If a follow-up phase wants to add hidden-dir filtering,
     do it in `discover_and_hash_files` AND `scan_directory` simultaneously to preserve the invariant that
     "a watcher upsert and a manual scan produce the same FileRecord rows."

2. **Should `_whoami_with_retry` skip retries on AgentApiAuthError?**
   - What we know: Pitfall 7 identifies the "infinite restart on bad token" failure mode; the existing
     `src/phaze/tasks/agent_worker.py:73-89` retries on broad `AgentApiError`.
   - What's unclear: is the failure mode rare enough to ignore (the operator will notice via container restart count) or
     common enough to harden against?
   - Recommendation: **Add the auth-error short-circuit when refactoring to `_shared/agent_bootstrap.py` (D-17).** Cost is
     ~3 lines; benefit is loud failure on token misconfiguration.

3. **Should the `pipeline_scans` router be a new file, or extend `routers/pipeline.py`?**
   - What we know: `routers/pipeline.py` is 350 lines and handles trigger endpoints for analyze/proposals/metadata/fingerprint;
     the new scan-trigger surface is conceptually adjacent.
   - What's unclear: file-cohesion tradeoff.
   - Recommendation: **New file `routers/pipeline_scans.py`** — keeps `pipeline.py` focused on the existing
     pipeline-orchestration trigger surface; the new file is dedicated to admin-triggered agent scans + their progress
     poll. Easier to grep and maintain.

4. **Polling fallback (inotify unavailable on NFS/FUSE).**
   - What we know: deployment target is Linux + ext4 / zfs (private home server per PROJECT.md); inotify works natively.
   - What's unclear: do any operators run with `/data/music` over NFS?
   - Recommendation: **Document but don't implement.** Add a one-line note in `src/phaze/agent_watcher/README.md` that if
     inotify fails (NFS, FUSE), the operator can swap `Observer` for `PollingObserver` in `__main__.py` as a one-line
     change. Not a Phase 27 deliverable.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|-------------|-----------|---------|----------|
| `watchdog>=4.0` | `phaze.agent_watcher` | ✓ (via uv sync after pyproject edit) | 4.0+ (likely 6.0.0) | — |
| Python 3.13 | Everything | ✓ | 3.13 | — (project constraint) |
| Linux inotify | Watcher Observer on deployment hosts | ✓ on Linux servers (kernel native); not present on macOS (dev — uses FSEvents) or Windows (uses ReadDirectoryChangesW) | n/a | `watchdog.observers.polling.PollingObserver` (1-line swap, NFS/FUSE fallback) |
| Docker Compose 2.x | New `watcher` service | ✓ | existing project tooling | — |
| Redis | `AgentTaskRouter` enqueue (controller-side) + `scan_directory` SAQ consumer (agent-side) | ✓ | existing | — |
| Postgres | LIVE sentinel resolution + ScanBatch PATCH (controller-side ONLY; agent-watcher has NONE) | ✓ | existing | — |

**Missing dependencies:** none.

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | `pytest` + `pytest-asyncio` (existing, project-wide) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (`asyncio_mode = "auto"`, `testpaths = ["tests"]`) |
| Quick run command | `uv run pytest tests/test_agent_watcher/ tests/test_routers/test_agent_scan_batches.py tests/test_routers/test_agent_files_batch_id.py tests/test_routers/test_pipeline_scans.py tests/test_tasks/test_scan_directory.py tests/test_task_split.py -x -q` |
| Full suite command | `just test-cov` (project-wide pytest + coverage; Codecov threshold 85%) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| **DIST-02** | Watcher service starts alongside worker/audfprint/panako on file server | integration (compose) | `docker compose up -d watcher && docker compose ps watcher \| grep -i up` (manual e2e per CONTEXT D-22 §test_main) — also covered by `tests/test_agent_watcher/test_main.py` (respx-mocked client; verifies process boots through `whoami → Observer.start → sweep`) | ❌ Wave 0 — `tests/test_agent_watcher/test_main.py` |
| **SCAN-01** | Admin trigger enqueues `scan_directory` on `phaze-agent-<id>` | contract | `pytest tests/test_routers/test_pipeline_scans.py::test_trigger_enqueues_to_correct_agent_queue -x` | ❌ Wave 0 |
| **SCAN-01** | Subpath `..` is rejected with 400 | contract | `pytest tests/test_routers/test_pipeline_scans.py::test_subpath_traversal_rejected -x` | ❌ Wave 0 |
| **SCAN-01** | Scan-path picker HTMX swap returns agent's scan_roots | contract | `pytest tests/test_routers/test_pipeline_scans.py::test_agent_roots_htmx_swap -x` | ❌ Wave 0 |
| **SCAN-02** | scan_directory chunks at 500 records per POST | unit (mocked client) | `pytest tests/test_tasks/test_scan_directory.py::test_chunks_at_500_records -x` | ❌ Wave 0 |
| **SCAN-02** | scan_directory PATCHes batch with processed_files after each chunk | unit | `pytest tests/test_tasks/test_scan_directory.py::test_patches_progress_per_chunk -x` | ❌ Wave 0 |
| **SCAN-02** | scan_directory PATCHes status=completed on clean walk | unit | `pytest tests/test_tasks/test_scan_directory.py::test_patches_completed_at_end -x` | ❌ Wave 0 |
| **SCAN-02** | scan_directory PATCHes status=failed on missing path | unit | `pytest tests/test_tasks/test_scan_directory.py::test_failed_on_missing_path -x` | ❌ Wave 0 |
| **SCAN-02** | scan_directory mid-walk OSError skips + logs warning | unit | `pytest tests/test_tasks/test_scan_directory.py::test_oserror_skip_continues_walk -x` | ❌ Wave 0 |
| **SCAN-02** | extract_file_metadata auto-enqueued by `agent_files.py` on INSERT (re-test with `batch_id` present) | contract | `pytest tests/test_routers/test_agent_files_batch_id.py::test_auto_enqueue_with_explicit_batch_id -x` | ❌ Wave 0 |
| **SCAN-03** | Watcher subscribes to FileCreatedEvent + FileModifiedEvent only | unit | `pytest tests/test_agent_watcher/test_observer.py::test_event_handler_subscribes_created_and_modified -x` | ❌ Wave 0 |
| **SCAN-03** | Watcher filters events by EXTENSION_MAP (no .txt, no hidden) | unit | `pytest tests/test_agent_watcher/test_observer.py::test_extension_filter_excludes_unknown -x` | ❌ Wave 0 |
| **SCAN-03** | Watcher POSTs without batch_id → controller resolves LIVE sentinel | contract | `pytest tests/test_routers/test_agent_files_batch_id.py::test_absent_batch_id_resolves_live_sentinel -x` | ❌ Wave 0 |
| **SCAN-03** | Controller rejects batch_id belonging to another agent with 403 | contract | `pytest tests/test_routers/test_agent_files_batch_id.py::test_cross_tenant_batch_id_403 -x` | ❌ Wave 0 |
| **SCAN-03** | PATCH /scan-batches/{id} cross-tenant returns 403 BEFORE state-machine | contract | `pytest tests/test_routers/test_agent_scan_batches.py::test_cross_tenant_403_before_state_machine -x` | ❌ Wave 0 |
| **SCAN-03** | PATCH /scan-batches/{id} same-state is 200 idempotent no-op | contract | `pytest tests/test_routers/test_agent_scan_batches.py::test_idempotent_same_state -x` | ❌ Wave 0 |
| **SCAN-03** | PATCH /scan-batches/{id} attempting to set LIVE returns 422 | contract | `pytest tests/test_routers/test_agent_scan_batches.py::test_cannot_set_live_status -x` | ❌ Wave 0 |
| **SCAN-04** | Debouncer touch+touch within settle_period does NOT mark ready | unit | `pytest tests/test_agent_watcher/test_debouncer.py::test_touch_resets_settle_timer -x` | ❌ Wave 0 |
| **SCAN-04** | Debouncer touch + sleep > settle_period yields ready | unit | `pytest tests/test_agent_watcher/test_debouncer.py::test_settle_period_elapsed_yields_ready -x` | ❌ Wave 0 |
| **SCAN-04** | Debouncer first_seen_at > max_pending evicts WITHOUT post | unit | `pytest tests/test_agent_watcher/test_debouncer.py::test_stuck_file_cap_drops_entry -x` | ❌ Wave 0 |
| **SCAN-04** | End-to-end: drop file via watchdog event ctor → sweep → respx-mocked POST verified | integration (no real FS) | `pytest tests/test_agent_watcher/test_main.py::test_event_to_post_e2e -x` | ❌ Wave 0 |
| **SCAN-04** | Vanished-path between debounce and post → OSError handled, no crash | unit | `pytest tests/test_agent_watcher/test_main.py::test_oserror_on_vanished_path -x` | ❌ Wave 0 |
| (boundary) | `phaze.agent_watcher` import does NOT pull `phaze.database` | unit (subprocess) | `pytest tests/test_task_split.py::test_agent_watcher_does_not_import_phaze_database -x` | ❌ Wave 0 (sibling case to existing test_agent_worker case) |
| (boundary) | `phaze.tasks._shared.agent_bootstrap` import does NOT pull `phaze.database` | unit (subprocess) | `pytest tests/test_task_split.py::test_shared_bootstrap_stays_postgres_free -x` | ❌ Wave 0 |

### Sampling Rate

- **Per task commit:** `uv run pytest tests/test_agent_watcher/ tests/test_routers/test_agent_scan_batches.py tests/test_routers/test_agent_files_batch_id.py tests/test_routers/test_pipeline_scans.py tests/test_tasks/test_scan_directory.py tests/test_task_split.py -x -q` (covers all new behavior in <30s)
- **Per wave merge:** `just test-cov` (full suite + coverage; Codecov 85% gate)
- **Phase gate:** Full suite green before `/gsd-verify-work`; coverage on new files ≥85%

### Wave 0 Gaps (CONTEXT D-22)

- [ ] `tests/test_routers/test_agent_scan_batches.py` — contract tests for PATCH endpoint (auth, cross-tenant guard, state-machine transitions, idempotent same-state). Covers SCAN-03 PATCH semantics.
- [ ] `tests/test_routers/test_agent_files_batch_id.py` — coverage for new `batch_id` field (absent → LIVE resolution; present → bound; cross-tenant rejection). Covers SCAN-02 + SCAN-03 upsert side.
- [ ] `tests/test_routers/test_pipeline_scans.py` — controller `POST /pipeline/scans` (agent dropdown, HTMX swap endpoint, prefix-validation rejection, `..` rejection, scan_root not in agent.scan_roots rejection, successful enqueue). Covers SCAN-01.
- [ ] `tests/test_tasks/test_scan_directory.py` — agent-side scan_directory (chunking at 500, per-chunk PATCH progress, terminal PATCH, OSError mid-walk skip, missing-scan_path PATCH failed). Covers SCAN-02.
- [ ] `tests/test_agent_watcher/__init__.py` — package marker.
- [ ] `tests/test_agent_watcher/conftest.py` — shared fixtures: `mock_loop`, fake `PendingEntry` factory, respx mock fixture for `PhazeAgentClient`. (Discretion — only if duplication would otherwise exist.)
- [ ] `tests/test_agent_watcher/test_debouncer.py` — debouncer state machine (touch resets timer; settle elapsed yields ready; stuck-file cap drops without post; concurrent touches via `call_soon_threadsafe` simulation). Covers SCAN-04.
- [ ] `tests/test_agent_watcher/test_observer.py` — observer wires watchdog events into debouncer (no real filesystem; construct `FileCreatedEvent(src_path="...")` and `FileModifiedEvent(src_path="...")` directly per "test fixtures" §10 below; assert `debouncer.touch` is called with NFC-normalized path and only for music/video extensions). Covers SCAN-03.
- [ ] `tests/test_agent_watcher/test_main.py` — end-to-end with respx-mocked `PhazeAgentClient`: synthesize event → advance asyncio time → assert one POST with empty `batch_id` and the expected `original_path`. Covers SCAN-03 + SCAN-04.
- [ ] `tests/test_task_split.py` — extend with two parallel cases: (a) `test_agent_watcher_does_not_import_phaze_database` (asserts `phaze.database`, `phaze.tasks.session`, `sqlalchemy.ext.asyncio` are all absent from `sys.modules` after `import phaze.agent_watcher`), (b) `test_shared_bootstrap_stays_postgres_free` (asserts the same for `phaze.tasks._shared.agent_bootstrap`). Subprocess-isolated per the existing Phase 26 pattern at `tests/test_task_split.py:36-58`.
- [ ] Framework install: none — `pytest`, `pytest-asyncio`, `respx`, `httpx` all already in dev deps (Phase 26 P-01). `watchdog>=4.0` lands in `[project].dependencies` per D-23; ALSO available in test code.

---

## Security Domain

> CONTEXT.md and PROJECT.md classify v4.0 as "private LAN, single trusted operator" — `security_enforcement` is NOT explicitly
> set in `.planning/config.json`, treat as enabled. Phase 27 inherits Phase 25's bearer-auth boundary; the new endpoint adds
> ONE new authenticated surface.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | `Depends(get_authenticated_agent)` on the new PATCH endpoint. Inherits Phase 25 D-05 SHA-256 token-hash auth. |
| V3 Session Management | n/a | No sessions; stateless bearer tokens. |
| V4 Access Control | **yes** | Cross-tenant guard on (a) new PATCH `/scan-batches/{batch_id}` and (b) new `batch_id` field on existing `POST /files`. Mirrors Phase 26 D-08 `403-before-state-machine` pattern verbatim. |
| V5 Input Validation | **yes** | `extra="forbid"` on every new schema (`ScanBatchPatch`, `ScanDirectoryPayload`, extended `FileUpsertChunk`, `TriggerScanForm`). `..` rejection + scan_root prefix-check on subpath input. NFC-normalization on `original_path`. |
| V6 Cryptography | yes (inherited) | SHA-256 via `hashlib` (existing `services/hashing.py`). No new crypto. |
| V7 Error Handling & Logging | yes | Bearer token NEVER logged (Phase 26 D-13 invariant preserved). `auth_id_prefix=<first-12-chars>...` format key. |
| V9 Communications | partial | HTTP for now; HTTPS comes in Phase 29. Operator's LAN is the threat boundary. |
| V11 Configuration | yes | New env vars (`PHAZE_WATCHER_*`, `PHAZE_SCAN_CHUNK_SIZE`) have safe defaults; `pydantic-settings` validation; secrets (`PHAZE_AGENT_TOKEN`) reuse `SecretStr` from Phase 26 D-14. |
| V14 Data Protection | n/a | No PII; music collection metadata only. |

### Known Threat Patterns for `FastAPI + Pydantic v2 + SAQ + watchdog`

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| **Cross-tenant batch hijack** — attacker (or buggy agent) POSTs files with a `batch_id` belonging to a different agent | Spoofing + Elevation of Privilege | 403-before-state-machine guard (Phase 26 D-08); `extra="forbid"` schema rejects unknown fields. |
| **Cross-tenant scan-batch PATCH** — agent A PATCHes agent B's batch via raw URL | Tampering | Same 403-before-state-machine guard. |
| **Subpath traversal via `..` in admin form** | Tampering | Controller validates subpath contains no `..` AND resolves under one of `agent.scan_roots`. Both checks; either failing → 400. (Mirrors `routers/scan.py:41` pattern per CONTEXT D-06.) |
| **Symlink traversal in `scan_directory`** | Tampering | `os.walk(scan_root, followlinks=False)` per `services/ingestion.py:55`. |
| **Token leak in watcher logs** | Information Disclosure | Token preview = first 12 chars + `...` (Phase 26 D-13 invariant; `agent_worker.py:103` template). Watcher startup mirrors. |
| **Timing oracle: 409 vs 403** | Information Disclosure | Cross-tenant guard returns 403 BEFORE state-machine evaluation (Phase 26 D-08). |
| **DoS via watcher pending-set growth** | Denial of Service | D-02 stuck-file cap (3600s eviction); bounded memory. |
| **DoS via huge subpath chains** | DoS | Pydantic schema constraints on subpath length; controller's NFC + prefix check is O(1). |
| **Replay of /pipeline/scans** | (intended idempotent) | Composite UQ on `(agent_id, original_path)` makes duplicate scans a no-op UPDATE. No request_id needed for the trigger surface — overlapping scans of the same path produce the same end-state. CONTEXT deferred §"Atomic scan in progress lock" confirms this is acceptable for v4.0. |

---

## Sources

### Primary (HIGH confidence)

- **Context7 `/gorakhargosh/watchdog`** — Observer, FileSystemEventHandler, FileCreatedEvent/FileModifiedEvent ctors, `event_filter` parameter, context-manager usage, recursive scheduling. `[VERIFIED via ctx7 fallback]`
- **PyPI `watchdog`** — version 4.0.2 (Aug 2024) first with Python 3.13 support; 6.0.0 (Nov 2024) current stable. `[VERIFIED via WebFetch https://pypi.org/project/watchdog/]`
- **Python 3 docs — asyncio event-loop** — `loop.call_soon_threadsafe(callback, *args)` is the canonical bridge from non-loop threads. `[CITED: https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe]`
- **Codebase: `src/phaze/services/agent_client.py`** — verified shape of `_request`, retry funnel, existing endpoint methods, exception hierarchy. `[VERIFIED via Read]`
- **Codebase: `src/phaze/routers/agent_proposals.py`** — verified cross-tenant 403-before-state-machine pattern that the new PATCH endpoint must mirror. `[VERIFIED via Read]`
- **Codebase: `src/phaze/routers/agent_files.py`** — verified existing auto-enqueue + xmax-based INSERT detection that extends unchanged to the new `batch_id` field. `[VERIFIED via Read]`
- **Codebase: `src/phaze/services/ingestion.py:45-119`** — verified walk-and-hash + bulk-upsert patterns; the agent-side `scan_directory` adapts the walk body (sans LEGACY_AGENT_ID stamping). `[VERIFIED via Read]`
- **Codebase: `src/phaze/tasks/agent_worker.py:73-89`** — verified `_whoami_with_retry` helper that becomes the shared bootstrap module. `[VERIFIED via Read]`
- **Codebase: `src/phaze/templates/tracklists/partials/scan_progress.html`** — verified HTMX poll-partial pattern with final-state halt via `hx-trigger` omission. `[VERIFIED via Read]`
- **Codebase: `src/phaze/models/scan_batch.py:39-48`** — verified partial UQ `uq_scan_batches_agent_id_live` exists and is the index server-side LIVE-sentinel resolution will hit. `[VERIFIED via Read]`
- **Codebase: `src/phaze/schemas/agent_files.py`** — verified existing `FileUpsertChunk` shape that gets the new `batch_id` field. `[VERIFIED via Read]`
- **Codebase: `tests/test_task_split.py`** — verified subprocess-isolated import-boundary test pattern that extends to `phaze.agent_watcher`. `[VERIFIED via Read]`
- **Codebase: `docker-compose.yml`** — verified existing `worker` service block that the new `watcher` block mirrors. `[VERIFIED via Read]`
- **CONTEXT.md** — 24 implementation decisions, exhaustive. `[CITED verbatim]`
- **UI-SPEC.md** — 5 component contracts. `[CITED verbatim where applicable]`
- **Phase 24/25/26 CONTEXT.md files** — predecessor decisions. `[CITED verbatim where applicable]`

### Secondary (MEDIUM confidence)

- **WebSearch on watchdog asyncio integration** — confirms `call_soon_threadsafe` as the standard pattern; multiple corroborating community sources (Python docs, GitHub gist on watchdog+asyncio, runebook.dev). `[VERIFIED across sources]`
- **`.planning/STATE.md`** — accumulated decisions; confirmed scan_live_set artist/title regression stays deferred. `[CITED]`

### Tertiary (LOW confidence)

- None. All findings either verified or cited.

---

## Metadata

**Confidence breakdown:**
- Library choice (watchdog 4.x): **HIGH** — verified version + Python 3.13 support via PyPI; type stubs verified; deployment-target (Linux) inotify backend is native.
- Architecture: **HIGH** — every pattern (PhazeAgentClient, AgentTaskRouter, cross-tenant guard, HTMX poll halt) is reused verbatim from existing code in the repository.
- Pitfalls (mtime stability, NFC drift, thread bridge): **HIGH** — known landmines documented from upstream watchdog patterns and existing codebase patterns; mitigations are concrete.
- Validation Architecture: **HIGH** — covers all 4 Phase 27 requirements + boundary invariants; each test names an exact pytest case the planner can target.
- Open questions: **MEDIUM** — non-blocking decisions that recommend a default but leave room for the planner.

**Research date:** 2026-05-13
**Valid until:** 2026-06-13 (30 days — stable library, mature codebase patterns)
