# Phase 26: Task Code Reorg & HTTP-Backed Agent Worker - Context

**Gathered:** 2026-05-12
**Status:** Ready for planning

<domain>
## Phase Boundary

`phaze.tasks.worker` is replaced by two SAQ settings modules driven by `PHAZE_ROLE`. `phaze.tasks.controller` boots the application-server role: fileless tasks only (`generate_proposals`, `match_tracklist_to_discogs`, `scrape_and_store_tracklist`, `search_tracklist`, `refresh_tracklists` cron), Postgres engine pool, no fingerprint/discogs/proposal services beyond what fileless work needs. `phaze.tasks.agent_worker` boots the file-server role: file-bound tasks only (`process_file`, `extract_file_metadata`, `fingerprint_file`, `scan_live_set`, `execute_approved_batch`), CPU-bound process pool for essentia analysis, local audfprint + panako sidecars, and a single `PhazeAgentClient` httpx instance that POSTs every state change to `/api/internal/agent/*` on the application server. The agent worker imports no Postgres driver and contains no `async_session` reachable code path.

The agent worker boots by calling a new `GET /api/internal/agent/whoami` endpoint with its bearer token; the server resolves the token through the existing auth dep and returns `{agent_id, name, scan_roots, created_at}`. The agent uses this `agent_id` to construct its SAQ queue name `phaze-agent-<agent_id>` at startup and refuses to serve jobs if /whoami fails after a bounded retry budget. The application-server enqueuer routes file-bound jobs through a new `AgentTaskRouter` helper that looks up `FileRecord.agent_id` and posts to `phaze-agent-<agent.id>`; agent_files.py's auto-enqueue (Phase 25 D-20) refactors to use it. Every agent job payload is a Pydantic model declared in `phaze.schemas.agent_tasks` carrying the minimum self-contained data (`file_id`, `original_path`, `file_type`, `agent_id`, plus `models_path` for analysis) so jobs execute without reading state back from the application server.

Phase 26 also closes a contract gap left by Phase 25: three new internal-agent endpoints land here so the full file-bound task surface can run on agents — `PUT /api/internal/agent/analysis/{file_id}` (AnalysisResult upsert), `POST /api/internal/agent/tracklists` (atomic Tracklist + new TracklistVersion + tracks insert, idempotency-keyed via a request_id Redis set), and `PATCH /api/internal/agent/proposals/{id}/state` (joint Proposal + FileRecord state transition with server-side state-machine validation).

Phase 26 does **not** introduce the watcher (Phase 27), does **not** implement distributed group-by-agent execution dispatch (Phase 28), and does **not** ship the deployment hardening / agents admin page (Phase 29). It also does **not** alter the schema beyond adding any indexes required by the three new endpoints (no new tables, no new columns).

</domain>

<decisions>
## Implementation Decisions

### Role Selection & Settings Modules

- **D-01:** `PHAZE_ROLE` env var values are exactly `control` and `agent` (per OPS-01). Compose entry point reads `PHAZE_ROLE` and selects between `phaze.tasks.controller.settings` and `phaze.tasks.agent_worker.settings`. The Dockerfile CMD is `uv run saq phaze.tasks.${PHAZE_ROLE}_worker.settings` for `agent`, and `uv run saq phaze.tasks.controller.settings` for `control` — the asymmetric name is acceptable; see D-02.
- **D-02:** Module names: `phaze.tasks.controller` (fileless, control role) and `phaze.tasks.agent_worker` (file-bound, agent role). The roadmap's `phaze.tasks.lux_worker` name leaks the application server's hostname and is replaced everywhere; a tiny doc-only sweep updates ROADMAP.md Phase 26/27/28/29 entries, Phase 25 CONTEXT, REQUIREMENTS.md, STATE.md, and any past summaries that reference `lux_worker`. The chosen name `controller` reads naturally and pairs cleanly with `PHAZE_ROLE=control`.
- **D-03:** Both settings modules import only the task functions in their half. `phaze.tasks.controller.settings.functions` lists fileless task functions; `phaze.tasks.agent_worker.settings.functions` lists file-bound task functions. Cross-imports between halves are forbidden — agent_worker MUST NOT transitively import `phaze.database`, `phaze.tasks.session`, or any module that imports them. A test asserts this (importable in process-isolation, see D-25).
- **D-04:** The legacy `phaze.tasks.worker` module is **deleted** in this phase and `docker-compose.yml` is updated in this phase to reference `phaze.tasks.controller.settings`. No back-compat shim. Phase 29 will add `docker-compose.agent.yml` for the agent role.

### Code Layout

- **D-05:** Task files stay **flat** under `src/phaze/tasks/` — no `tasks/control/` or `tasks/agent/` subpackages. File-bound task files (`functions.py`, `scan.py`, `fingerprint.py`, `metadata_extraction.py`, `execution.py`) get rewritten **in place** so they use `ctx["api_client"]` (a `PhazeAgentClient` instance) and `ctx["payload_model"]` validation instead of `ctx["async_session"]`. Fileless task files (`proposal.py`, `discogs.py`, `tracklist.py`) keep `ctx["async_session"]` unchanged.
- **D-06:** `phaze.tasks.session` (the legacy session-helper module noted in 24/25 maps) is **deleted** at the end of Phase 26 — only the two settings modules' startup hooks construct shared resources now.
- **D-07:** New files added in Phase 26:
  - `src/phaze/tasks/controller.py` — SAQ settings module for control role (fileless)
  - `src/phaze/tasks/agent_worker.py` — SAQ settings module for agent role (file-bound)
  - `src/phaze/services/agent_client.py` — `PhazeAgentClient` wrapper (see D-09..D-13)
  - `src/phaze/services/agent_task_router.py` — `AgentTaskRouter` helper (see D-19..D-21)
  - `src/phaze/schemas/agent_tasks.py` — typed payloads (see D-22..D-24)
  - `src/phaze/routers/agent_identity.py` — new `/whoami` router (see D-15..D-17)
  - `src/phaze/routers/agent_analysis.py` — new `/analysis/{file_id}` PUT router (D-26)
  - `src/phaze/routers/agent_tracklists.py` — new `/tracklists` POST router (D-27)
  - `src/phaze/routers/agent_proposals.py` — new `/proposals/{id}/state` PATCH router (D-28)
  - Pydantic schemas in `src/phaze/schemas/agent_identity.py`, `agent_analysis.py`, `agent_tracklists.py`, `agent_proposals.py`
- **D-08:** Files deleted in Phase 26: `src/phaze/tasks/worker.py`, `src/phaze/tasks/session.py`. Compose updates and any stale imports are removed in the same commit set.

### PhazeAgentClient (HTTP Wrapper)

- **D-09:** `phaze.services.agent_client.PhazeAgentClient` mirrors the `DiscogsographyClient` pattern (`src/phaze/services/discogs_matcher.py:21-46`): `__init__(base_url, token, *, timeout: float = 30.0)`, holds a single `httpx.AsyncClient`, `async def close(self)`. Constructed once in `agent_worker.startup` and stored at `ctx["api_client"]`. The agent's bearer token is set as a default header on the underlying client (`headers={"Authorization": f"Bearer {token}"}`) so every method inherits it.
- **D-10:** One method per endpoint, named after the resource + verb:
  - `whoami() -> AgentIdentity`
  - `upsert_files(chunk: list[FileUpsertRow]) -> FileUpsertResponse`
  - `put_metadata(file_id: UUID, payload: MetadataWritePayload) -> None`
  - `put_fingerprint(file_id: UUID, engine: str, payload: FingerprintWritePayload) -> None`
  - `put_analysis(file_id: UUID, payload: AnalysisWritePayload) -> None`
  - `create_tracklist(payload: TracklistCreatePayload) -> TracklistCreateResponse`
  - `post_execution_log(payload: ExecutionLogCreate) -> ExecutionLogResponse`
  - `patch_execution_log(id: UUID, payload: ExecutionLogPatch) -> ExecutionLogResponse`
  - `patch_proposal_state(id: UUID, payload: ProposalStatePatch) -> None`
  - `heartbeat(payload: HeartbeatPayload) -> None`
  Each method accepts and returns Pydantic models from `phaze.schemas.agent_*`.
- **D-11:** **Retry policy is `tenacity` with narrow scope:** add `tenacity>=8.4` to `[project].dependencies`. Decorator-style on each network method:
  ```python
  @retry(
      stop=stop_after_attempt(3),
      wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
      retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPStatusError)),
      reraise=True,
  )
  ```
  Plus a `before_sleep` hook that logs the retry attempt at INFO. **4xx responses do NOT retry** — wrap in a small predicate that re-raises non-5xx HTTPStatusError immediately. Total wall-time per call before bubbling to SAQ: ~4s (0.5 + 1 + 2 + final attempt). SAQ then performs job-level retries with its own backoff. Idempotency guarantees from Phase 25 D-12..D-14 + Phase 26 D-26..D-28 make the double-retry chain safe.
- **D-12:** Client surfaces three exception types for callers:
  - `AgentApiAuthError` (401/403) — fatal, do NOT retry, surface up to SAQ as job failure with no retry.
  - `AgentApiClientError` (4xx other than 401/403) — fatal, do NOT retry, job failure with no retry.
  - `AgentApiServerError` (5xx, network) — already retried by tenacity; bubble as-is so SAQ retries.
  All three derive from `AgentApiError`. SAQ's `retries=` config catches `AgentApiServerError` only (configure via SAQ's per-job error filter).
- **D-13:** Logging shape: every successful call logs at DEBUG (`agent_api method=put_analysis file_id=<uuid> status=200 elapsed_ms=42`); every failure logs at WARNING with the same fields plus `error=<class>:<message>`. The bearer token is **never** logged (it never appears in any log line — token reaches `httpx.AsyncClient.headers` and stays there). Constant: token preview in startup log is the first 12 chars + `...` (e.g., `phaze_agent_a1b2...`) and never the secret portion.

### Settings: Role-Specific Subclasses

- **D-14:** `phaze.config.settings` is split into a base + two role-specific subclasses:
  - `phaze.config.BaseSettings` keeps shared fields (database_url is included here but only the controller reads it; redis_url, debug, scan_path, models_path stay shared since both roles may read different mounts).
  - `phaze.config.ControlSettings(BaseSettings)` adds the controller-specific bits (no new fields in Phase 26; existing LLM/Discogsography settings stay on this class).
  - `phaze.config.AgentSettings(BaseSettings)` adds `agent_api_url: str`, `agent_token: SecretStr`. **Required when role=agent** — `model_validator(mode="after")` raises if either is missing. The agent worker imports `AgentSettings` directly; the controller imports `ControlSettings`. A `phaze.config.get_settings()` factory returns the right subclass based on `PHAZE_ROLE`, used by `main.py` and other multi-role entry points. Existing module-level `settings = Settings()` is replaced by `settings = get_settings()` — most call sites are unaffected because they only read shared fields.

### /whoami Endpoint & Agent Identity

- **D-15:** New router: `src/phaze/routers/agent_identity.py`. Single endpoint:
  ```
  GET /api/internal/agent/whoami
  ```
  Uses `Depends(get_authenticated_agent)`. Returns Pydantic `AgentIdentity` body:
  ```json
  { "agent_id": "fileserver-01", "name": "File Server 01", "scan_roots": ["/data/music"], "created_at": "2026-05-12T..." }
  ```
  Registered in `phaze.main.create_app` alongside the Phase 25 routers. Status: 200 on success; 401/403 surface from the auth dep as usual.
- **D-16:** Agent worker startup sequence (`phaze.tasks.agent_worker.startup`):
  1. Read `AgentSettings()` — fails fast if `agent_api_url` or `agent_token` missing.
  2. Construct `PhazeAgentClient(base_url=settings.agent_api_url, token=settings.agent_token.get_secret_value(), timeout=30.0)`.
  3. Call `client.whoami()` with bounded retry: exponential backoff 1s → 2s → 4s → 8s → 16s → 32s (total ≤ 60s wall-clock). If still failing, log fatal and `raise RuntimeError(...)` — SAQ exits non-zero, Docker `restart: unless-stopped` retries the container on a saner cadence.
  4. Stash `identity = AgentIdentity(...)` at `ctx["agent_identity"]`, derive `ctx["agent_queue_name"] = f"phaze-agent-{identity.agent_id}"`.
  5. Construct the SAQ `queue = Queue.from_url(redis_url, name=ctx["agent_queue_name"])` at MODULE-LEVEL (SAQ requires queue at import time) — but the name must come from /whoami. **Resolution:** queue name is read from env var `PHAZE_AGENT_QUEUE` at import time (operator-supplied or set by the entrypoint script to match the token) AND the startup hook asserts `agent_queue_name == settings.queue_name` before serving jobs. This catches token/env mismatches.
- **D-17:** /whoami is also used by Phase 29's agents admin page (probe agent reachability). Phase 26 only adds the endpoint; Phase 29's UI use is deferred.

### Queue Naming & Mismatch Guard

- **D-18:** The agent's queue name is `phaze-agent-<agent_id>` where `agent_id` is the kebab-case slug from Phase 24 D-01. The slug regex `^[a-z0-9]+(-[a-z0-9]+)*$` guarantees Redis-safe key chars. The literal string lives in **two** places: (a) the agent worker's env (`PHAZE_AGENT_QUEUE`), needed at SAQ import time; (b) the AgentTaskRouter on the controller, derived from `FileRecord.agent_id` at every enqueue. The startup-time assertion in D-16 step 5 is the single guard that catches operator misconfiguration.

### AgentTaskRouter (Controller-Side Enqueuer)

- **D-19:** `src/phaze/services/agent_task_router.py` exports `AgentTaskRouter`:
  ```python
  class AgentTaskRouter:
      def __init__(self, redis_url: str) -> None: ...
      async def enqueue_for_file(self, *, file_record: FileRecord, task_name: str, payload: BaseModel) -> Job: ...
      async def enqueue_for_agent(self, *, agent_id: str, task_name: str, payload: BaseModel) -> Job: ...
      async def close(self) -> None: ...
  ```
  - `enqueue_for_file`: derives queue name from `file_record.agent_id`, calls `enqueue_for_agent` under the hood.
  - `enqueue_for_agent`: constructs `Queue.from_url(self.redis_url, name=f"phaze-agent-{agent_id}")` and enqueues the task with the payload `.model_dump()`. Lazily caches Queue instances per agent_id to avoid re-creating Redis connection pools (`functools.cached` or an internal dict).
- **D-20:** Wired into the FastAPI app in `phaze.main.create_app` as `app.state.task_router = AgentTaskRouter(redis_url=settings.redis_url)`. Routers that need to enqueue file-bound jobs read `request.app.state.task_router`. Lifespan shutdown calls `task_router.close()`.
- **D-21:** Two call sites refactor to use the router in Phase 26:
  - `src/phaze/routers/agent_files.py` lines 100–115 (auto-enqueue from Phase 25 D-20) — replaces the inline `Queue.from_url(...).enqueue(...)` with `task_router.enqueue_for_file(file_record=..., task_name="extract_file_metadata", payload=ExtractMetadataPayload(...))`.
  - `src/phaze/routers/scan.py` (or wherever a user-initiated scan POSTs — to be confirmed in Phase 27, but if a Phase 26-touching call site exists, route through here). Phase 27 will add more call sites; Phase 26 establishes the pattern.

### Payload Schemas (phaze.schemas.agent_tasks)

- **D-22:** Single file `src/phaze/schemas/agent_tasks.py` exports one Pydantic model per file-bound task:
  - `ProcessFilePayload(file_id: UUID, original_path: str, file_type: str, agent_id: str, models_path: str)` — process_file's CPU-bound essentia analysis.
  - `ExtractMetadataPayload(file_id: UUID, original_path: str, file_type: str, agent_id: str)` — extract_file_metadata's mutagen read.
  - `FingerprintFilePayload(file_id: UUID, original_path: str, agent_id: str)` — fingerprint_file's audfprint + panako submit.
  - `ScanLiveSetPayload(file_id: UUID, original_path: str, agent_id: str)` — scan_live_set's fingerprint query + tracklist resolve.
  - `ExecuteApprovedBatchPayload(batch_id: UUID, agent_id: str, proposal_ids: list[UUID])` — execute_approved_batch's per-agent sub-batch.
  All declare `model_config = ConfigDict(extra="forbid")` per Phase 25 D-16.
- **D-23:** **Payload contents are MINIMAL** — every payload contains only the data the agent needs to execute the job without reading state back from the controller. Models_path appears only in `ProcessFilePayload` (essentia analysis needs the .pb files); fingerprint/metadata/scan tasks don't need it because their adapters point at local sidecars / read files directly.
- **D-24:** Payload **does not include** `current_path` — agents work off `original_path` (set at scan time). `current_path` is the post-execution path (only meaningful after execute_approved_batch flips state); send it back via `patch_proposal_state` instead. Phase 28 may revisit if execute_approved_batch needs more state in its payload.

### Test/Import Boundary Verification

- **D-25:** A pytest test (likely `tests/test_task_split.py`) asserts that importing `phaze.tasks.agent_worker` does **not** transitively import `phaze.database`, `phaze.tasks.session`, or `sqlalchemy.ext.asyncio`. Implementation: subprocess `uv run python -c "import sys; sys.modules.clear(); import phaze.tasks.agent_worker; banned = {'phaze.database', 'sqlalchemy.ext.asyncio'}; assert banned.isdisjoint(sys.modules)"`. Runs in CI. This is the structural enforcement of TASK-01's "no `async_session` reachable in agent code paths."

### New Endpoints Closing the Phase 25 Gap

- **D-26:** `PUT /api/internal/agent/analysis/{file_id}` in `src/phaze/routers/agent_analysis.py`. Idempotent upsert on `AnalysisResult.file_id` (unique constraint exists in `models/analysis.py`). Body schema in `schemas/agent_analysis.py`: `AnalysisWritePayload(bpm: float, musical_key: str, mood: dict[str, float], style: dict[str, float], danceability: float | None = None, energy: float | None = None)`. `extra="forbid"`. Uses `pg_insert(...).on_conflict_do_update(index_elements=["file_id"], set_={...})`. Returns 200 with `{file_id}` echo.
- **D-27:** `POST /api/internal/agent/tracklists` in `src/phaze/routers/agent_tracklists.py`. Body: `TracklistCreatePayload(file_id: UUID, source: Literal["fingerprint"], external_id: str, tracks: list[TracklistTrackPayload], request_id: UUID)` where `request_id` is an agent-generated UUID for idempotency. Server-side: (1) check Redis set `tracklist_req:{request_id}` (1-hour TTL) — if present, return the prior `TracklistCreateResponse` body cached as JSON in `tracklist_resp:{request_id}` (1-hour TTL); (2) upsert Tracklist by `external_id` (composite UQ already exists in models/tracklist.py); (3) compute next version number under that tracklist; (4) INSERT TracklistVersion + N TracklistTrack rows in one transaction; (5) cache the response under `tracklist_resp:{request_id}`. Returns `{tracklist_id: UUID, version: int, track_count: int}`. Idempotency via request_id matches Stripe-style semantics — appropriate for the multi-row write that has no good natural-key idempotency.
- **D-28:** `PATCH /api/internal/agent/proposals/{id}/state` in `src/phaze/routers/agent_proposals.py`. Body: `ProposalStatePatch(proposal_state: Literal["executed", "failed"], file_state: Literal["moved", "unchanged"] | None = None, current_path: str | None = None, error_message: str | None = None)`. Server validates allowed transitions (state machine: `APPROVED → EXECUTED | FAILED` for Proposal; `APPROVED → MOVED | UNCHANGED` for FileRecord — confirm exact enum values from existing models during planning). Updates Proposal and (if `file_state` provided) FileRecord in one transaction. Returns 200 with `{proposal_id, proposal_state, file_state, current_path}`. Returns 409 if the requested transition is not allowed (e.g., already EXECUTED → re-PATCH to EXECUTED is no-op 200; → FAILED is 409). Phase 28's batch execution will call this once per file-operation.

### Test Infrastructure

- **D-29:** Per-task contract tests live alongside the Phase 25 pattern: `tests/test_routers/test_agent_identity.py`, `test_agent_analysis.py`, `test_agent_tracklists.py`, `test_agent_proposals.py`. Shared `authenticated_client` fixture from Phase 25's tests is reused.
- **D-30:** AgentTaskRouter has a unit test (`tests/test_services/test_agent_task_router.py`) using `pytest-asyncio` and a real (Docker-Compose-provided) Redis — assert that enqueuing for two different agent IDs puts jobs on two different SAQ queues, no cross-talk. The lazy Queue cache is exercised by a "enqueue twice for same agent → second one reuses Queue instance" assertion.
- **D-31:** PhazeAgentClient gets a contract test using `respx` (httpx mock library; add to dev dependencies). Each `client.put_*` / `client.post_*` / `client.patch_*` method has a happy-path test, a 4xx test (assert `AgentApiClientError`, no retry), a 5xx test (assert tenacity retries 3 times then raises `AgentApiServerError`), and a network-error test. Total ~10 tests; covers the auth-header-injection invariant too.
- **D-32:** The import-boundary test (D-25) lands as a single pytest case in `tests/test_task_split.py`. Runs in CI under the standard `pytest` invocation (no special mark). Failure prints both the imported banned module AND a trace of which import chain pulled it in (`importlib._bootstrap` chain) so the regression is debuggable.

### Roadmap & Doc Sweep

- **D-33:** Tiny doc-only commit at the end of Phase 26 (after all code lands): sweep `lux_worker` → `controller` in:
  - `.planning/ROADMAP.md` — Phase 26/27/28/29 entries
  - `.planning/REQUIREMENTS.md` — TASK-01, TASK-02 reference `lux_worker` / `agent_worker`; rename to `controller` / `agent_worker`
  - `.planning/STATE.md` — accumulated decisions referencing lux_worker
  - `.planning/PROJECT.md` — milestone description
  - `.planning/phases/25-internal-agent-http-api-bearer-auth/25-CONTEXT.md` — references to phaze-agent-<id> queue and lux_worker
  Single commit message: `docs(v4.0): replace hostname-leaked lux_worker with role-neutral controller`.

### Claude's Discretion

- Exact tenacity `wait_exponential` parameters within the spirit of D-11 (the 0.5 / 1.0 / 2.0 / 2.0 sequence is recommended; if planner prefers a slightly different curve fine, total wall-time ≤ 5s before bubbling).
- The internal cache impl for `AgentTaskRouter` (dict-with-lock vs functools.cached_property vs an LRU). Just don't leak Redis connections across reconfigs.
- Whether `AgentApiError` and its subtypes live in `phaze.services.agent_client` or in their own `phaze.services.agent_client_errors` module — pick whichever keeps the public surface clean.
- The exact name and layout of test fixtures for agent-side tests that mock the API (`agent_client_mock` is a reasonable conftest name).
- Whether to introduce a `phaze.tasks._shared` module for the small startup-helper functions (model file check, etc.) that the controller's startup hook still needs — only do this if duplication would otherwise exist.
- Whether `phaze.config.get_settings()` returns a freshly-constructed instance every call or a cached singleton — cached singleton is fine for a single-process app; just don't make it module-level eager if a test wants to override `PHAZE_ROLE`.
- Whether the import-boundary test (D-25) runs in a subprocess (cleanest) or via `importlib.util` in-process (faster but harder to read). Subprocess recommended.
- The exact log format for tenacity retry attempts (one-liner with attempt/elapsed/exception type is recommended; pick a format consistent with existing service logging).
- Pydantic schema field names — match the existing model field names exactly where possible to minimize cognitive load (e.g., `AnalysisWritePayload.musical_key` not `key`).
- The Redis key TTL on tracklist request_id idempotency (1 hour recommended; agent retries should resolve well within that window).
- Whether `ProposalStatePatch.current_path` is `str | None` or always required when `file_state == "moved"` (recommend a `model_validator(mode="after")` that enforces the conditional).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project & Milestone Context
- `.planning/PROJECT.md` — v4.0 milestone scope, HTTP-only boundary, key decisions table
- `.planning/REQUIREMENTS.md` §"Task Execution" — TASK-01, TASK-02, TASK-03 (the three task-execution requirements this phase satisfies)
- `.planning/REQUIREMENTS.md` §"Topology & Boundary" — DIST-03 (per-agent SAQ queue), DIST-04 (no direct Postgres on agents), DIST-05 (idempotency)
- `.planning/REQUIREMENTS.md` §"Deployment & Operations" — OPS-01 (PHAZE_ROLE env-driven role selection)
- `.planning/ROADMAP.md` §"Phase 26: Task Code Reorg & HTTP-Backed Agent Worker" — goal, dependencies (Phase 25), success criteria
- `.planning/STATE.md` §"Accumulated Context → Decisions → v4.0" — locked pre-roadmap decisions

### Direct Predecessors (MUST read in full)
- `.planning/phases/24-schema-foundation-agent-registry/24-CONTEXT.md` — Phase 24 decisions; especially D-01 (agent.id slug format), D-09 (LIVE sentinel), D-11..D-12 (sentinel uniqueness)
- `.planning/phases/25-internal-agent-http-api-bearer-auth/25-CONTEXT.md` — Phase 25 decisions; especially D-05 (auth dep), D-08 (router file layout), D-11 (verb table), D-12..D-16 (idempotency contract), D-20..D-22 (auto-enqueue pattern this phase refactors)
- `.planning/phases/25-internal-agent-http-api-bearer-auth/25-VERIFICATION.md` — what was actually shipped; sanity-check before building on it
- `.planning/phases/25-internal-agent-http-api-bearer-auth/25-PATTERNS.md` — established Phase 25 patterns for new routers + tests

### Existing Task Code (will be REWRITTEN in place)
- `src/phaze/tasks/worker.py` — DELETED in Phase 26; replaced by controller.py + agent_worker.py
- `src/phaze/tasks/session.py` — DELETED in Phase 26 (legacy session-helper shim)
- `src/phaze/tasks/functions.py` (process_file) — rewritten to call `ctx["api_client"].put_analysis(...)`
- `src/phaze/tasks/metadata_extraction.py` (extract_file_metadata) — rewritten to call `ctx["api_client"].put_metadata(...)`
- `src/phaze/tasks/fingerprint.py` (fingerprint_file) — rewritten to call `ctx["api_client"].put_fingerprint(...)`
- `src/phaze/tasks/scan.py` (scan_live_set) — rewritten to call `ctx["api_client"].create_tracklist(...)`
- `src/phaze/tasks/execution.py` (execute_approved_batch) — rewritten to call `ctx["api_client"].post_execution_log(...)`, `patch_execution_log(...)`, `patch_proposal_state(...)`

### Existing Task Code (stays on controller, unchanged)
- `src/phaze/tasks/proposal.py` (generate_proposals) — fileless, controller-only
- `src/phaze/tasks/discogs.py` (match_tracklist_to_discogs) — fileless, controller-only
- `src/phaze/tasks/tracklist.py` (search_tracklist, scrape_and_store_tracklist, refresh_tracklists cron) — fileless, controller-only
- `src/phaze/tasks/pool.py` (create_process_pool, run_in_process_pool) — used by agent_worker startup (CPU-bound essentia)

### Pattern References
- `src/phaze/services/discogs_matcher.py:21-46` (DiscogsographyClient) — pattern for PhazeAgentClient
- `src/phaze/services/fingerprint.py` (AudfprintAdapter, PanakoAdapter, FingerprintOrchestrator) — multi-engine HTTP adapter pattern
- `src/phaze/routers/agent_files.py` — Phase 25's auto-enqueue inline pattern (D-20); Phase 26 refactors this to use AgentTaskRouter
- `src/phaze/routers/agent_metadata.py` — Phase 25's `PUT /metadata/{file_id}` pattern; mirror for `PUT /analysis/{file_id}`
- `src/phaze/routers/agent_fingerprint.py` — Phase 25's `PUT /fingerprints/{file_id}/{engine}` pattern
- `src/phaze/routers/agent_execution.py` — Phase 25's `POST + PATCH /execution-log` pattern; mirror for `PATCH /proposals/{id}/state`
- `src/phaze/services/ingestion.py:91-119` (`bulk_upsert_files`) — `pg_insert(...).on_conflict_do_update(...)` pattern for the new analysis-result endpoint

### Models the New Endpoints Touch (READ — not modified)
- `src/phaze/models/analysis.py` — `AnalysisResult` schema; verify `file_id` is unique (it is) for D-26 upsert
- `src/phaze/models/tracklist.py` — `Tracklist`, `TracklistVersion`, `TracklistTrack`; understand version increment + composite UQs for D-27
- `src/phaze/models/proposal.py` — `Proposal` + state enum for D-28 transitions
- `src/phaze/models/file.py` — `FileRecord` + `FileState` enum for D-28's joint update

### Configuration & Wiring
- `src/phaze/config.py` — `Settings` class; Phase 26 splits this into Base + ControlSettings + AgentSettings (D-14)
- `src/phaze/main.py` — `create_app()`; Phase 26 adds 4 new `include_router` calls (identity, analysis, tracklists, proposals) and wires `app.state.task_router`
- `src/phaze/database.py` — `get_session`; unchanged but verify agent_worker never imports it (D-25)
- `docker-compose.yml` — `worker` service command changes to `uv run saq phaze.tasks.controller.settings`; new env var consumption documented for the future docker-compose.agent.yml
- `Dockerfile` — confirm no role-specific COPY/RUN; same image runs both roles
- `pyproject.toml` — add `tenacity>=8.4` to deps; add `respx>=0.21` to dev deps
- `CLAUDE.md` — Python 3.13, uv, mypy strict, ruff line-length 150, pre-commit hook expectations, security guidance (no custom crypto)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **DiscogsographyClient** (`services/discogs_matcher.py:21-46`) — exact pattern PhazeAgentClient follows: `__init__(base_url, timeout)`, single `httpx.AsyncClient`, `async close()`. PhazeAgentClient adds a `token` arg and pre-sets the Authorization header on the underlying client.
- **AudfprintAdapter + PanakoAdapter + FingerprintOrchestrator** (`services/fingerprint.py`) — pattern for multi-engine HTTP wrappers with shared base class; not literally reused but informs PhazeAgentClient's per-method error-handling style.
- **`bulk_upsert_files`** (`services/ingestion.py:91-119`) — `pg_insert(...).on_conflict_do_update(...)` idiom for D-26's PUT /analysis upsert.
- **`request.app.state.queue`** wiring in `main.py` — pattern for D-20's `app.state.task_router` wiring during lifespan startup.
- **`Depends(get_authenticated_agent)`** (`routers/agent_auth.py`) — reused verbatim by the four new routers (identity, analysis, tracklists, proposals).
- **`secrets`, `hashlib.sha256`** — already used by Phase 25 for token hashing; not touched by Phase 26.

### Established Patterns
- **One router file per resource** — D-07's new routers (agent_identity, agent_analysis, agent_tracklists, agent_proposals) match.
- **`APIRouter(prefix="/api/internal/agent/<resource>", tags=["agent-internal"])`** — all four new routers use this.
- **Pydantic `BaseModel` with `model_config = ConfigDict(extra="forbid")`** — every new schema enforces strict input parsing (D-22, D-26, D-27, D-28).
- **SAQ `Queue.from_url(redis_url, name=...)`** — Phase 25 D-22's queue-name kwarg pattern; AgentTaskRouter packages it.
- **`Settings` from pydantic-settings** with env-driven fields — Phase 26 extends this with the Base / ControlSettings / AgentSettings split (D-14).
- **`ctx[...]` shared resources from SAQ startup hooks** — pattern repurposed: agent_worker's startup adds `ctx["api_client"]`, `ctx["agent_identity"]`, `ctx["agent_queue_name"]`; controller's startup keeps `ctx["async_session"]` etc.
- **`pyproject.toml [tool.mypy].exclude`** — currently excludes `tests/`, `prototype/`, `services/`. Phase 26 adds new modules to `services/` (PhazeAgentClient, AgentTaskRouter) — the existing exclusion lets us iterate quickly, but planner should confirm whether to lift the exclusion for these specific files (e.g., per-file `--strict` opt-in) to keep new code type-safe.

### Integration Points
- **5 new internal-agent endpoints** registered in `phaze.main.create_app()`:
  ```python
  app.include_router(agent_identity.router)
  app.include_router(agent_analysis.router)
  app.include_router(agent_tracklists.router)
  app.include_router(agent_proposals.router)
  ```
  Plus the existing 5 from Phase 25 (already registered).
- **1 controller-side service** `phaze.services.agent_task_router.AgentTaskRouter` instantiated in `create_app` lifespan as `app.state.task_router`, used by `routers/agent_files.py` (refactor) and any future controller-side enqueuer.
- **1 agent-side service** `phaze.services.agent_client.PhazeAgentClient` instantiated in `phaze.tasks.agent_worker.startup` and stored at `ctx["api_client"]`.
- **2 SAQ settings modules** replacing `phaze.tasks.worker.settings` — `phaze.tasks.controller.settings` and `phaze.tasks.agent_worker.settings`. Each defines its own `queue`, `functions`, `concurrency`, `startup`, `shutdown`, `cron_jobs`.
- **1 settings class split** — `phaze.config` exports `BaseSettings`, `ControlSettings`, `AgentSettings`, `get_settings()`. Module-level `settings = get_settings()` keeps existing call sites working.
- **1 docker-compose.yml change** — `worker.command` becomes `uv run saq phaze.tasks.controller.settings`; this is the v4.0 control-plane container.
- **Tests** — `tests/test_routers/test_agent_identity.py`, `test_agent_analysis.py`, `test_agent_tracklists.py`, `test_agent_proposals.py`, `test_services/test_agent_client.py`, `test_services/test_agent_task_router.py`, `tests/test_task_split.py` (import-boundary).
- **No model changes** — all four new endpoints write to existing tables (AnalysisResult, Tracklist/Version/Tracks, Proposal, FileRecord).

### Constraints to Plan Around
- **SAQ queue name at import time** — SAQ's settings module must declare its `queue` at import. The agent worker resolves its actual `agent_id` at /whoami time, which is post-import. Resolution per D-16 step 5: queue name comes from env (`PHAZE_AGENT_QUEUE`), startup hook asserts the env matches the token-derived agent_id. Document this constraint loudly so future devs don't try to "fix" the apparent duplication.
- **Process pool needs essentia models** — `create_process_pool()` ultimately loads essentia-tensorflow. This pulls TensorFlow into the agent's memory footprint. Phase 26 keeps the existing `models_path` startup check (lines 30-39 of current worker.py) — file servers MUST mount their own /models volume.
- **`extra="forbid"` strict body parsing** (Phase 25 D-16) extends to all four new schemas. Mismatched-payload requests return 422, never silently drop fields.
- **`pyproject.toml` dependency rename** — `tenacity` must land in `[project].dependencies` (used at runtime by PhazeAgentClient), not dev-only. `respx` is dev-only.

</code_context>

<specifics>
## Specific Ideas

- The compose entry point uses `phaze.tasks.${PHAZE_ROLE}_worker.settings` for `agent` but the controller's module is literally `phaze.tasks.controller` (no `_worker` suffix). The asymmetry is intentional: `controller` reads more naturally than `controller_worker`. Operator passes the full module path in compose anyway, so the asymmetry is invisible at runtime.
- `PHAZE_AGENT_QUEUE` env var format is `phaze-agent-<agent_id>` (literal string) — no template interpolation. The startup assertion catches any operator typo.
- Bearer token preview in startup logs: first 12 chars (covers `phaze_agent_` prefix + 0 secret bytes) + `...` — leaks zero entropy.
- The 4xx-no-retry / 5xx-with-retry split is enforced by tenacity's `retry_if_exception_type` plus a `before` callback that re-raises 4xx HTTPStatusError as the appropriate `AgentApiClientError` subtype before tenacity sees it. Alternative: write a custom `retry_if` predicate that inspects the HTTPStatusError's response status. Either is fine; pick the more readable one during planning.
- For tracklist idempotency, request_id should be generated by the agent at SAQ-job-start time and persisted in the job's SAQ state so retries reuse the same UUID. Same pattern as Phase 25 D-13's ExecutionLog.id contract — keeps the idempotency model consistent across endpoints.
- Proposal state machine transitions Phase 26 must explicitly enumerate: `APPROVED → EXECUTED`, `APPROVED → FAILED`. Re-applying `APPROVED → EXECUTED` to an already-EXECUTED row is a 200 no-op (idempotent retry). `EXECUTED → FAILED` or `FAILED → EXECUTED` is 409. The same shape applies to FileRecord state (`APPROVED → MOVED`, `APPROVED → UNCHANGED`).
- `PhazeAgentClient` constructor signature should accept `httpx.AsyncClient` injection for testability: `__init__(base_url, token, *, timeout=30.0, _client: httpx.AsyncClient | None = None)`. respx tests inject a mock client; production never passes `_client`.
- The legacy agent (`legacy-application-server`, born revoked per Phase 24 D-06) never authenticates, so it never reaches /whoami, /analysis, /tracklists, or /proposals/.../state. No test fixtures need to seed it.
- `AgentTaskRouter` cache: keyed by `agent_id` (the slug, not the queue name). Reasonable max size: unbounded for v4.0 (single-user app, small number of agents). Planner may add a max if the architecture changes.

</specifics>

<deferred>
## Deferred Ideas

- **Agent-side process-pool tuning** — process_pool size on agents may differ from current controller-default (4) depending on the file server's CPU count. Phase 26 keeps the existing `worker_process_pool_size` setting; per-agent override deferred to Phase 29's operator tooling.
- **Watcher service & user-initiated scan** — Phase 27 adds `phaze-agent-watcher` (watchdog lib + settle period) and the admin-UI scan trigger form. Phase 26 establishes the AgentTaskRouter pattern that the scan trigger will use.
- **Group-by-agent execution dispatch** — Phase 28 adds the controller-side dispatch that groups Approved proposals by `FileRecord.agent_id` and fans out `execute_approved_batch` sub-jobs. Phase 26 establishes the per-agent queue routing + the `patch_proposal_state` endpoint that the dispatch will call.
- **Deployment hardening** — HTTPS + internal CA, Redis `requirepass`, `docker-compose.agent.yml`, per-file-server `just download-models`, heartbeat-driven Agents admin page — all Phase 29.
- **mypy strictness on the new services** — `[tool.mypy].exclude` currently lifts `services/`. Phase 26 may or may not opt the two new files into strict checking inline; deferred decision unless planner prefers tighter typing from day 1.
- **Bearer-token rotation overlap window** — Phase 26 inherits Phase 25 AUTH-04 ("revoke + issue new"); two simultaneously-valid tokens per agent during rotation is a future enhancement.
- **`SecretStr` for the bearer token in agent-side config** — D-14 uses `SecretStr`; the wrapper already supports this. No deferred work here.
- **Cross-file-server fingerprint matching** — XAGENT-01 (v4.0 doc'd limitation).
- **Tracklist endpoint chunked POST** — current contract assumes the full tracklist fits in one POST. If a tracklist exceeds ~1000 tracks, chunking would be needed; v4.0 personal-collection scale doesn't hit this.
- **PhazeAgentClient request/response logging at structured-JSON level** — Phase 26 uses key=value logging per D-13; structured-JSON logging (e.g., structlog) is deferred to a milestone-wide observability pass.
- **Removing `phaze.config.settings` module-level singleton** — keeping it for back-compat with existing call sites; a per-request `Depends(get_settings)` pattern is deferred.

</deferred>

---

*Phase: 26-task-code-reorg-http-backed-agent-worker*
*Context gathered: 2026-05-12*
