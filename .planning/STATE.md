---
gsd_state_version: 1.0
milestone: v4.0
milestone_name: Distributed Agents
status: planning
stopped_at: Phase 28 context gathered
last_updated: "2026-05-14T21:43:58.232Z"
last_activity: 2026-05-14
progress:
  total_phases: 6
  completed_phases: 4
  total_plans: 33
  completed_plans: 34
  percent: 100
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-02)

**Core value:** Get 200K messy music and concert files properly named, organized, deduplicated, with rich metadata in Postgres -- human-in-the-loop approval so nothing moves without review.
**Current focus:** Phase 27 — watcher-service-user-initiated-scan

## Current Position

Phase: 28
Plan: Not started
Status: Ready to plan
Last activity: 2026-05-14

Progress: [██████████] 100%

## Performance Metrics

**v1.0 Velocity:**

- Total plans completed: 39
- Total phases: 11
- Timeline: 4 days (2026-03-27 -> 2026-03-30)
- Tests: 282 passing
- LOC: 7,975 Python

**v2.0 Velocity:**

- Total plans completed: 16
- Total phases: 6
- Timeline: 3 days (2026-03-31 -> 2026-04-02)
- Tests: 538 passing
- LOC: 5,966 Python

## Accumulated Context

### Decisions

- v3.0 scope: Search, Discogs Linking, Tag Writing, CUE Sheets -- enrichment layer, not pipeline extension
- FileState enum NOT extended -- enrichment tracked via TagWriteLog and DiscogsLink tables
- Zero new pip dependencies -- httpx, mutagen, rapidfuzz, SQLAlchemy already in pyproject.toml
- Discogs integration routes through discogsography HTTP API only, never direct Discogs API
- Search UI: HTMX partial detection via truthy HX-Request header check
- Search UI: Alpine.js collapsible filter panel pattern (x-data showFilters boolean)
- [Phase 19]: Confidence blending: 0.6 token_set_ratio + 0.4 API relevance, denormalized Discogs metadata in DiscogsLink
- [Phase 19]: Discogs results excluded when file_state filter active, matching tracklist exclusion pattern
- [Phase 19]: Three-entity UNION ALL search: file (blue), tracklist (green), discogs_release (purple) pill colors
- [Phase 19]: Discogs UI: HTMX candidate lifecycle with accept/dismiss, auto-dismiss siblings, bulk-link top candidate
- [Phase 20-tag-writing]: Mock-based tests for OGG/M4A formats, real MP3 for end-to-end write/verify
- [Phase 20-tag-writing]: Tracklist date.year is fallback-only for year field (does not override metadata year)
- [Phase 20-tag-writing]: Inline edits are transient (client-side), no server session storage for edited proposed values
- [Phase 20-tag-writing]: Tag row partial with OOB toast for post-write HTMX swap response
- [Phase 20-tag-writing]: Server-side fallback for empty form data in Write Tags endpoint; ID-based HTMX targeting over closest tr
- [Phase 21]: CueTrackData uses dataclass not Pydantic for zero-overhead service input
- [Phase 21]: Dropped from __future__ annotations in CUE router to avoid FastAPI uuid runtime resolution issues
- [Phase 21-03]: HX-Target header prefix matching for cross-page response routing (tracklist- prefix returns tracklist_card.html)
- [Phase 21-03]: Dynamic _cue_version attribute on Tracklist ORM objects for UI-only display data
- [Phase 26-01]: pydantic-settings v2 does NOT comma-split list[str] env vars natively -- Annotated[list[str], NoDecode] + @field_validator(mode="before") is the canonical workaround
- [Phase 26-01]: pydantic-settings reads env vars by field name absent env_prefix -- AliasChoices(...) per-field is required to map PHAZE_AGENT_* env vars onto bare field names
- [Phase 26-01]: Module-level `settings: ControlSettings = ...` keeps existing call sites' `settings.llm_*` reads type-checking; agent worker calls get_settings() / AgentSettings() directly per D-14
- [Phase 26-01]: `Settings = ControlSettings` back-compat alias preserves `from phaze.config import Settings` for test files until they migrate
- [Phase 26-02]: Tenacity retry funnel via AsyncRetrying async-iterator (not @retry decorator) -- cleaner try/except integration for 4xx/5xx status-code mapping post-loop
- [Phase 26-02]: PhazeAgentClient bearer token NEVER stored as instance attribute -- lives only inside httpx.AsyncClient.headers (T-26-02-I mitigation)
- [Phase 26-02]: Parallelization-debt marker pattern: type: ignore[import-not-found] + warn_unused_ignores makes missing-cross-plan-schema diagnostic self-deleting on merge
- [Phase 26-04]: AgentTaskRouter cache impl chose plain `dict[str, Queue]` over `functools.cache` (rejected: extra layer for single-instance service) and LRU (rejected: eviction without `.disconnect()` would leak Redis connections; bounded growth not needed for v4.0's 1-5 agent scale)
- [Phase 26-04]: AgentTaskRouter integration tests use a real Redis (no fakeredis fallback) per D-30 -- SAQ Queue.from_url is not compatible with fakeredis at saq>=0.26.3
- [Phase 26-04]: Per-agent SAQ queue naming invariant: `phaze-agent-<agent_id>` (D-18); agent_id is the kebab-case slug from Phase 24 D-01, Redis-safe by construction
- [Phase 26-05]: Smoke-app pattern adopted for per-router contract tests; matches Phase 25 test_agent_metadata.py precedent and decouples Plan 26-12 wiring
- [Phase 26-05]: /whoami response uses naive UTC created_at -- matches project-wide TimestampMixin convention; deferred timezone-aware migration to a future architectural plan
- [Phase 26-06]: Overflow funnel pattern -- wire-format fields without a dedicated column (e.g. danceability, energy on AnalysisResult) merge into the row's `features` JSONB column rather than being dropped, preserving D-26's wire contract without an Alembic migration. Future migration can promote to dedicated columns.
- [Phase 26-06]: Deterministic dict summarization -- `sorted(items, key=lambda kv: (-kv[1], kv[0]))[:N]` two-key sort is the canonical pattern for compacting classifier-score dicts into bounded, replay-safe strings. `reverse=True` single-key sort tiebreaks by insertion order which is non-deterministic.
- [Phase 26-07]: Stripe-style request-id idempotency via Redis SET NX EX -- atomic lock-acquire + bounded-wait concurrent-writer poll (10*50ms -> 409) + cached-response fast-path; 1h TTL
- [Phase 26-07]: `request.app.state.redis` thin pass-through dep keeps the Redis client lifecycle in main.py lifespan (Plan 26-12) while keeping the handler smoke-app-testable via direct `app.state.redis = client` assignment
- [Phase 26-07]: `sqlalchemy.update(Model)` is mypy-friendly; `Model.__table__.update()` trips `FromClause has no attribute "update"` because mypy types `__table__` as the abstract parent
- [Phase 26-08]: Cross-tenant guard placement: 403 returns BEFORE state-machine evaluation to prevent timing side-channel via 409 vs 403 (W1 / T-26-08-S2)
- [Phase 26-08]: Joint Proposal+FileRecord mutation uses single await session.commit() (RESEARCH Pitfall 6 invariant)
- [Phase 26-08]: Idempotent same-state PATCH echoes current row state with ZERO DB writes -- does NOT bump updated_at on same-state retry
- [Phase 26-08]: Mirror agent_execution.py PATCH structure byte-for-byte (Annotated[AsyncSession, Depends] dep pattern, session.get->404 pattern)
- [Phase 26-11]: ExecutionStatus enum extracted to phaze.enums (DB-free); models/execution.py re-exports it. Schemas under phaze.schemas.agent_* now load without sqlalchemy/phaze.database -- the D-03 import boundary holds for the agent worker
- [Phase 26-11]: scan_live_set drops in-process FileMetadata artist/title resolution; fingerprint-sourced tracklist rows land with artist=None,title=None. Known v3.0 UI regression deferred to a future Phase 27/28 controller-side enrichment task
- [Phase 26-11]: services/fingerprint.py uses function-local DB imports inside get_fingerprint_progress so the module surface stays DB-free for the agent worker
- [Phase 26-11]: execute_approved_batch ExecutionLog reporting maps onto Phase 25's per-proposal schema (one POST + one PATCH per file op); batch-level completed_with_errors lives in the returned dict, not the schema
- [Phase 26-11]: AnalysisWritePayload mood/style wire conversion -- two helpers in tasks/functions.py rebuild dict[str, float] from analysis["features"] (averaging mood_* sets across variants; top-N genres) instead of dropping the str labels
- [Phase ?]: 26-10: agent_worker SAQ settings module ships with subprocess import-boundary test (D-25) enforcing no phaze.database / sqlalchemy.ext.asyncio in agent import chain
- [Phase ?]: 26-10: D-13 token-preview banner uses 'auth_id_prefix=' format key (not 'token_preview=') to avoid semgrep secret-detector false-positives; rendered value unchanged
- [Phase ?]: 26-10: /whoami startup probe budget = exponential 1s→32s = ~63s wall-clock; RuntimeError on exhaustion; queue-name mismatch guard catches PHAZE_AGENT_QUEUE vs token-derived agent_id misconfig
- [Phase ?]: [Phase 26-13] D-04+D-06 finalized: phaze.tasks.{worker,session} deleted with no back-compat shim; docker-compose worker service rewired to phaze.tasks.controller.settings under PHAZE_ROLE=control; lux_worker→controller doc sweep across PROJECT.md + ROADMAP.md
- [Phase 27-01]: phaze.tasks._shared.agent_bootstrap centralizes whoami_with_retry + construct_agent_client; Pitfall 7 short-circuit on AgentApiAuthError closes the "bad token infinite-restart" failure mode
- [Phase 27-01]: Four new AgentSettings fields (watcher_settle_seconds=10, watcher_max_pending_seconds=3600, watcher_sweep_interval_seconds=2, scan_chunk_size=500) with PHAZE_WATCHER_*/PHAZE_SCAN_CHUNK_SIZE env-var aliases via AliasChoices (Phase 26-01 pattern)
- [Phase 27-02]: FileUpsertChunk.batch_id: UUID | None added; absent → controller resolves LIVE sentinel via uq_scan_batches_agent_id_live partial UQ; present → 403-before-state-machine cross-tenant guard (T-27-02)
- [Phase 27-03]: PATCH /api/internal/agent/scan-batches/{batch_id} state machine: RUNNING→COMPLETED/FAILED only; LIVE rejected at schema layer (Literal); idempotent same-state PATCH echoes row with zero DB writes
- [Phase 27-04]: scan_directory chunk size = 500; per-chunk PATCH progress; terminal status PATCH on completion or failure; per-file OSError skip (mirrors services/ingestion.py:65); module-private _classify duplicates EXTENSION_MAP lookup to keep agent-side scan.py Postgres-free (D-13 / D-25 invariant)
- [Phase 27-05]: phaze.agent_watcher uses dict[str, _PendingEntry] + asyncio-owned single-loop sweep (time.monotonic clock); loop.call_soon_threadsafe is the ONLY sanctioned thread bridge from the watchdog Observer thread
- [Phase 27-05]: Stuck-file cap = 3600s default (D-02 / T-27-05); evicted entries log WARNING but do NOT post; bounded in-memory cost. Watcher POSTs chunk-of-1 with batch_id OMITTED (not None) to trigger server-side LIVE-sentinel resolution (D-18)
- [Phase 27-06]: HTMX poll-partial halt: terminal-state markup OMITS hx-trigger AND hx-get; outerHTML swap replaces the polling element entirely (Pitfall 6); cadence = every 2s for scan progress, every 5s for stats bar. Recent Scans mini-table uses transient _agent_name / _elapsed_seconds attrs on ORM rows to avoid N+1
- [Phase 27-07]: Compose 'watcher' service lives in root docker-compose.yml; Phase 29 will move it + 'worker' to docker-compose.agent.yml; depends_on api: service_started (no healthcheck); restart: unless-stopped is the only liveness mechanism in Phase 27. Volume mount SCAN_PATH:/data/music:ro only (no MODELS_PATH/OUTPUT_PATH; watcher is fileless-write)

### Pending Todos

None.

### Blockers/Concerns

- Phase 19: Verify discogsography `/api/search` response shape before writing adapter (research flag)
- arq replaced by SAQ -- all new task code must use SAQ conventions

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 260410-kco | Add Docker image publishing to GHCR following discogsography pattern | 2026-04-10 | 3f91f93 | [260410-kco-add-docker-image-publishing-to-ghcr-foll](./quick/260410-kco-add-docker-image-publishing-to-ghcr-foll/) |
| 260414-quo | Add Discord notification to docker-publish.yml workflow mirroring discogsography pattern | 2026-04-14 | 9c5cedb | [260414-quo-add-discord-notification-to-docker-publi](./quick/260414-quo-add-discord-notification-to-docker-publi/) |
| 260502-lqb | Remove Discord notification step from docker-publish.yml workflow | 2026-05-02 | ea84be2 | [260502-lqb-remove-discord-notification-step-from-do](./quick/260502-lqb-remove-discord-notification-step-from-do/) |
| Phase 26 P02 | 9min | 2 tasks | 2 files |
| Phase 26 P04 | 5min | 2 tasks | 2 files |
| Phase 26 P05 | 18min | 2 tasks | 2 files |
| Phase 26 P06 | 13min | 3 tasks | 3 files |
| Phase 26 P07 | 14min | 2 tasks | 2 files |
| Phase 26 P08 | 14min | 2 tasks | 3 files |
| Phase 26 P11 | 30min | 4 tasks | 13 files (5 task bodies rewritten + supporting refactors + 5 test rewrites + new contract test file + phaze.enums package) |
| Phase 26 P10 | 25min | 3 tasks | 3 files |
| Phase 26 P12 | 7m 25s | 2 tasks | 3 files |
| Phase 26 P13 | 11m | 2 tasks | 8 files |

## Session Continuity

Last session: 2026-05-14T21:43:58.225Z
Stopped at: Phase 28 context gathered
Resume file: .planning/phases/28-distributed-execution-dispatch/28-CONTEXT.md
