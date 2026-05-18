# Phase 25: Internal Agent HTTP API & Bearer Auth - Context

**Gathered:** 2026-05-11
**Status:** Ready for planning

<domain>
## Phase Boundary

The application server exposes `/api/internal/agent/*` — an authenticated, idempotent HTTP surface that file-server agents call to record every state change. A FastAPI dependency reads `Authorization: Bearer <token>`, hashes it with SHA-256, looks the row up in `agents`, rejects if missing (401), unknown (403), or revoked (403), and yields the resolved `Agent` to the handler. Endpoints cover (at minimum) file upsert (chunked), tag-metadata write, fingerprint write, execution-log create + PATCH, and heartbeat. Every endpoint is idempotent on its natural keys so an agent can blindly retry across network errors and SAQ retries without producing duplicate rows or corrupting state. `agent_id` is always derived from the token — never trusted from the request body — and request schemas use `extra="forbid"` so accidental spoofing attempts (or version-skew bugs) surface as 422 at the boundary. The auto-enqueue pipeline behaviour from v3.0 `run_scan` is preserved: the file-upsert endpoint enqueues `extract_file_metadata` per newly-inserted music/video row onto that agent's `phaze-agent-<id>` SAQ queue.

Phase 25 does **not** wire agent-side callers (Phase 26's job), does **not** add the watcher (Phase 27's job), does **not** ship HTTPS / Redis hardening (Phase 29's job), and does **not** implement an agent-registration endpoint (operator pre-seeds rows per OPS-06).

</domain>

<decisions>
## Implementation Decisions

### Token Format & Verification

- **D-01:** Bearer tokens look like `phaze_agent_<32 urlsafe-base64 random bytes>` on the wire (~42 chars total). The literal `phaze_agent_` prefix is part of the token and makes leaked tokens grep-able / secret-scanner-friendly (compare `ghp_`, `slack_`, etc.). Token generation tooling (`just generate-agent-token` or similar) ships in Phase 29; Phase 25 only consumes them.
- **D-02:** Storage is `sha256(token).hex()` in `agents.token_hash` (64 chars; `String(128)` column already provisioned in Phase 24 has plenty of headroom). The **entire** wire string (prefix + 32 random bytes) is hashed — server never strips the prefix.
- **D-03:** Per-request verification is a single indexed SELECT: `SELECT * FROM agents WHERE token_hash = $1 AND revoked_at IS NULL`. No in-process cache. This makes AUTH-04 (immediate revocation without restart) trivially correct — set `revoked_at = NOW()` on the row and the next request misses the predicate. The token_hash column gets a btree index in this phase's migration.
- **D-04:** Token entropy is high (32 random bytes ≥ 256 bits) so a KDF (argon2/bcrypt) buys nothing and would break the indexed-lookup model (random per-row salt makes equality lookup impossible). SHA-256 is correct here precisely because the input is already uniform-random.

### Auth Wiring

- **D-05:** Auth lives in a **FastAPI dependency** at module path `phaze.routers.agent_auth.get_authenticated_agent` (helper module, no router). Every `/api/internal/agent/*` route adds `agent: Agent = Depends(get_authenticated_agent)`. Explicit per-route — appears in OpenAPI security spec, trivial to mock in tests, impossible to silently mis-prefix (because routes that forget the dep simply don't receive an Agent).
- **D-06:** Status code split:
  - **401 Unauthorized** — `Authorization` header missing or not `Bearer <something>` form.
  - **403 Forbidden** — well-formed bearer whose hash is unknown OR whose row has `revoked_at IS NOT NULL`. Both surface as 403 (intentionally indistinguishable to clients) to avoid an oracle for "does this token id exist?".
  - The dep includes `WWW-Authenticate: Bearer` on 401 per RFC 6750.
- **D-07:** The legacy agent (`legacy-application-server`, born revoked per Phase 24 D-06) is unreachable by design — `revoked_at IS NOT NULL`, so it can never authenticate. Phase 24's contract relies on this; Phase 25 enforces it.

### Endpoint Layout

- **D-08:** New router files land **flat** in `src/phaze/routers/` (matches existing one-file-per-resource convention):
  - `agent_files.py` — chunked file upsert
  - `agent_metadata.py` — tag-metadata write
  - `agent_fingerprint.py` — fingerprint result write
  - `agent_execution.py` — execution-log create + PATCH
  - `agent_heartbeat.py` — heartbeat
- **D-09:** Auth helper (`get_authenticated_agent`, `hash_token`, etc.) lives in `src/phaze/routers/agent_auth.py` (same package, but not a router itself — no `router = APIRouter(...)`). Keeps the per-resource files thin and the helper close to its consumers. Pydantic request/response schemas live in `src/phaze/schemas/agent_*.py` mirroring the router filenames.
- **D-10:** Every router declares `APIRouter(prefix="/api/internal/agent/<resource>", tags=["agent-internal"])`. All five routers are registered in `phaze.main.create_app`. Tags grouped under `agent-internal` so OpenAPI docs naturally segregate the agent surface from the operator surface.
- **D-11:** HTTP verbs follow intent semantics:

  | Endpoint | Verb | Path | Notes |
  |---|---|---|---|
  | File upsert chunk | `POST` | `/api/internal/agent/files` | Batch operation; row-level idempotency via composite UQ |
  | Tag-metadata write | `PUT` | `/api/internal/agent/metadata/{file_id}` | Idempotent replace; UQ on `metadata.file_id` |
  | Fingerprint write | `PUT` | `/api/internal/agent/fingerprints/{file_id}/{engine}` | Idempotent replace; UQ on `(file_id, engine)` |
  | ExecutionLog create | `POST` | `/api/internal/agent/execution-log` | Agent-generated id in body (D-13) |
  | ExecutionLog update | `PATCH` | `/api/internal/agent/execution-log/{id}` | Monotonic status (D-15) |
  | Heartbeat | `POST` | `/api/internal/agent/heartbeat` | Body carries status (D-17) |

### Idempotency & Replay

- **D-12:** Natural keys per endpoint:
  - **Files:** `(agent_id, original_path)` — already swapped by Phase 24's migration 013; `bulk_upsert_files` (`services/ingestion.py:91-119`) is the canonical pattern to mirror.
  - **Tag metadata:** `file_id` — `metadata.file_id` is already `unique=True` in `models/metadata.py:18`.
  - **Fingerprint:** `(file_id, engine)` — already unique via `ix_fprint_file_engine` in `models/fingerprint.py:25`.
  - **ExecutionLog:** `id` (agent-generated UUID, see D-13).
- **D-13:** **ExecutionLog.id is agent-generated** (uuid.uuid4() on the agent). Agent supplies the id in the POST body. Server does `INSERT ... ON CONFLICT (id) DO NOTHING` — first-create wins, subsequent identical POSTs are no-ops. Agent persists the id in SAQ job state between POST and any subsequent PATCH so retries point at the same row.
- **D-14:** **Last-write-wins** for files / metadata / fingerprint / heartbeat. `ON CONFLICT (<natural key>) DO UPDATE SET <mutable fields> = EXCLUDED.<...>` — matches the established `bulk_upsert_files` pattern verbatim. Mismatched-payload retries (e.g., a file edited in place between scans, producing a new sha256_hash for the same path) simply overwrite the row.
- **D-15:** **Monotonic status** on ExecutionLog PATCH. Order: `PENDING < IN_PROGRESS < COMPLETED < FAILED`. `COMPLETED` and `FAILED` are terminal. A PATCH that would move status backward returns **409 Conflict** with body `{"detail": "execution-log status is terminal" | "execution-log status would regress"}`. This protects the audit-trail integrity of file moves on an irreplaceable collection from out-of-order retry storms.
- **D-16:** Request bodies are strict: every internal-agent request schema sets `model_config = ConfigDict(extra="forbid")`. Any extra field — including accidental `agent_id`, `agent`, etc. — returns 422 with Pydantic's standard error response. Forces lockstep between agent + server schemas at the cost of one-line forward compat (acceptable for a single-user app where agent and server ship together).

### Heartbeat & Status

- **D-17:** Heartbeat body shape:
  ```json
  { "agent_version": "4.0.0", "worker_pid": 1234, "queue_depth": 5 }
  ```
  All three fields required. Server stamps `agents.last_seen_at = NOW()` AND persists the payload as JSONB in a new `agents.last_status` column. Phase 29's Agents admin page reads `last_status` directly (no Redis caching layer in v4.0 — keep state in one place).
- **D-18:** Phase 25 ships its own migration (`014_add_last_status_to_agents.py`) adding `agents.last_status` (nullable JSONB, no backfill — legacy agent never heartbeats). Token-hash index also lands here:
  ```sql
  CREATE INDEX ix_agents_token_hash ON agents (token_hash) WHERE revoked_at IS NULL;
  ```
  Partial index — covers the only predicate the auth dep ever runs (`token_hash = ? AND revoked_at IS NULL`).
- **D-19:** Heartbeat response is **204 No Content** (no body needed; agent doesn't need a server echo). All other write endpoints return **200 OK** with a minimal `{"agent_id": "...", "<resource_id_field>": "..."}` confirmation — enough for the agent to log the round-trip without re-fetching.

### Auto-Enqueue Pipeline

- **D-20:** The file-upsert endpoint detects newly-inserted rows (those where Postgres `ON CONFLICT` resulted in an INSERT, not an UPDATE) and enqueues `extract_file_metadata` per music/video file onto `phaze-agent-<agent.id>` SAQ queue. Mirrors `services/ingestion.py:158-165` (`run_scan` auto-enqueue) almost verbatim — the difference is the queue is per-agent rather than the default queue.
- **D-21:** The endpoint uses Postgres `RETURNING (xmax = 0) AS inserted` (or equivalent SQLAlchemy idiom) to distinguish INSERTed vs UPDATEd rows in a single round-trip. This is preferred over a separate SELECT or over enqueuing for every row regardless of insert/update (which would be wrong — re-walking a directory must not re-trigger metadata extraction).
- **D-22:** Queue routing uses `Queue.from_url(settings.redis_url).enqueue(..., queue_name=f"phaze-agent-{agent.id}")` (or the SAQ-equivalent constructor). The `phaze-agent-<id>` naming pattern is the v4.0 contract Phase 26 will formalise; Phase 25 hard-codes it inline and Phase 26 may refactor to a shared helper.

### Claude's Discretion

- Exact byte-length / encoding of the 32-byte secret portion of the token (`secrets.token_urlsafe(32)` is the obvious choice; the resulting string is 43 chars before prefix, so the final token is `phaze_agent_<43 chars>` = 55 chars). Use `secrets`-module APIs only — never `random`.
- Constant-time comparison: not required for SHA-256-indexed lookup (the SELECT is the comparison and Postgres-side equality is sufficient because the hash space is uniform and not key-correlated). If the planner prefers `secrets.compare_digest` on the hex string for defence-in-depth, that's fine and free.
- The exact 409 vs silent-ignore choice for monotonic ExecutionLog regression — 409 is recommended (loud failure surfaces buggy callers); silent ignore is acceptable if the planner argues retry storms produce too much log noise.
- Pydantic schema file split: `schemas/agent_files.py`, `schemas/agent_metadata.py`, etc. vs one `schemas/agent.py` with everything. Recommend mirroring router files for grep-ability.
- Error-response shape for 401/403/422/409: stick with FastAPI defaults (`{"detail": "..."}`) unless the planner has a reason to use a richer envelope. Don't invent a new error format for this phase.
- Logging shape and verbosity for auth events (success / 401 / 403 / 422 / 409). Keep it operator-readable; include `agent_id` on success, omit token material on every line.
- Tests: pytest fixtures for "authenticated agent client" (probably an `httpx.AsyncClient` with the bearer pre-set and a known agent row pre-seeded), per-endpoint contract tests covering 200 / 401 / 403 / 409 / 422 / replay-idempotency happy paths, and an end-to-end test that walks an upsert chunk through to auto-enqueue (assert the SAQ queue received the expected jobs).
- Whether to add an OpenAPI `securitySchemes` `bearerAuth` definition on the FastAPI app so the auto-generated docs render the lock icon — recommend yes, costs nothing.
- File upsert chunk-size cap: Phase 25 should pick a hard server-side limit (recommend 1000 records per chunk; SCAN-02 mentions "e.g., 500" as the watcher's choice). Reject chunks above the cap with 422.
- Whether `agent_metadata.py` upsert payload includes the raw mutagen tags blob (`raw_tags` JSONB) or just the canonical fields. Recommend mirroring the existing FileMetadata model 1:1.
- Auto-enqueue concurrency: enqueue inside the same transaction as the upsert vs after commit. Recommend after commit (Postgres pattern: `await session.commit()` then enqueue; on enqueue failure, log and continue — the metadata extractor can be re-triggered by a future poll).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project & Milestone Context
- `.planning/PROJECT.md` — v4.0 milestone scope, key decisions table (especially the HTTP-only boundary and per-agent bearer-token rows), constraints
- `.planning/REQUIREMENTS.md` §"Topology & Boundary" — DIST-04, DIST-05 (the two requirements covering the API surface and its idempotency)
- `.planning/REQUIREMENTS.md` §"Authentication & Security" — AUTH-01, AUTH-04 (the two requirements this phase satisfies; AUTH-02 / AUTH-03 are Phase 29)
- `.planning/ROADMAP.md` §"Phase 25: Internal Agent HTTP API & Bearer Auth" — goal, dependencies (Phase 24), and the five-item success criteria checklist
- `.planning/STATE.md` §"Accumulated Context → Decisions → v4.0" — locked pre-roadmap decisions (HTTP-only boundary, per-agent queue naming `phaze-agent-<id>`, etc.)

### Direct Predecessor (MUST read in full)
- `.planning/phases/24-schema-foundation-agent-registry/24-CONTEXT.md` — Phase 24 decisions, especially D-06 (legacy agent born revoked), D-07 (token_hash nullable), D-09/D-10 (LIVE sentinel), D-13 (two-step migration shape)
- `.planning/phases/24-schema-foundation-agent-registry/24-RESEARCH.md` — Alembic mechanics; especially the note that `ScanStatus` is stored as `VARCHAR(20)` not a Postgres enum (relevant if Phase 25 ever needs to touch that column)
- `.planning/phases/24-schema-foundation-agent-registry/VERIFICATION.md` — what was actually shipped vs planned (sanity-check before building on it)

### Schema (Phase 25 reads these models; mostly does NOT modify them)
- `src/phaze/models/agent.py` — `Agent` class, `LEGACY_AGENT_ID` constant; Phase 25 will add a `last_status: Mapped[dict | None]` JSONB column here (D-17, D-18)
- `src/phaze/models/file.py` — `FileRecord` with `agent_id` non-null FK, composite UQ `uq_files_agent_id_original_path`; the natural key for file-upsert idempotency
- `src/phaze/models/scan_batch.py` — `ScanBatch` with `agent_id` non-null FK, `ScanStatus.LIVE`, partial UQ `uq_scan_batches_agent_id_live`; FileRecord.batch_id is nullable so Phase 25 endpoints don't require a batch
- `src/phaze/models/metadata.py` — `FileMetadata.file_id` is `unique=True` (line 18); natural key for the metadata PUT endpoint
- `src/phaze/models/fingerprint.py` — `FingerprintResult` with `ix_fprint_file_engine` unique index on `(file_id, engine)` (line 25); natural key for the fingerprint PUT endpoint
- `src/phaze/models/execution.py` — `ExecutionLog`, `ExecutionStatus` enum (`PENDING`, `IN_PROGRESS`, `COMPLETED`, `FAILED`); status is the column Phase 25's monotonic-PATCH check operates on (D-15)
- `src/phaze/models/base.py` — `Base`, `TimestampMixin`, naming-convention dict; every new constraint/index name in Phase 25's migration must follow `ix_`, `uq_`, `fk_`, `ck_`, `pk_` prefixes

### Routing & App Wiring Patterns
- `src/phaze/main.py` — `create_app()` factory and `include_router(...)` registration; Phase 25 adds 5 new `include_router` calls
- `src/phaze/routers/health.py` — minimal router with `Depends(get_session)` — pattern for the auth dep
- `src/phaze/routers/scan.py` — router that triggers SAQ tasks via `request.app.state.queue` — pattern for the file-upsert auto-enqueue (D-20, D-22)
- `src/phaze/routers/proposals.py` / `src/phaze/routers/execution.py` — examples of routers with multiple verbs (POST + PATCH) and Pydantic request schemas

### Established Service-Layer Patterns (DO NOT REINVENT)
- `src/phaze/services/ingestion.py:91-119` — `bulk_upsert_files` with `pg_insert(...).on_conflict_do_update(index_elements=["agent_id", "original_path"], set_={...})`. **This is the canonical idempotent-upsert pattern; mirror it for metadata + fingerprint endpoints.**
- `src/phaze/services/ingestion.py:158-165` — auto-enqueue pattern (`queue.enqueue("extract_file_metadata", file_id=...)`) — mirror for D-20; difference is the queue name is per-agent
- `src/phaze/services/hashing.py` — utility for SHA-256 of file content; **not** the same as token hashing (D-02 is on a string, not a file)

### Configuration & Conventions
- `src/phaze/config.py` — `Settings` class; Phase 25 may add a couple of fields (e.g., `agent_token_prefix: str = "phaze_agent_"`, `agent_file_chunk_max: int = 1000` if D-discretion adopts the cap)
- `src/phaze/database.py` — `get_session` dependency, async engine; reuse verbatim
- `CLAUDE.md` — Python 3.13, uv, mypy strict, ruff config (line length 150), pre-commit hook expectations, security-libraries note (no custom crypto — use `hashlib.sha256` and `secrets` stdlib for D-01..D-04)
- `alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py` — pattern for the new `014_add_last_status_to_agents.py` migration (D-18)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`pg_insert(...).on_conflict_do_update(...)`** in `services/ingestion.py`: exact idiom Phase 25 uses for files / metadata / fingerprint / ExecutionLog POST. Drop into each new endpoint with the appropriate `index_elements` / `set_` clauses.
- **`Depends(get_session)`** from `phaze.database`: the standard FastAPI session dep. The new `get_authenticated_agent` dep wraps it: `async def get_authenticated_agent(authorization: str = Header(...), session: AsyncSession = Depends(get_session)) -> Agent: ...`.
- **`request.app.state.queue`** wiring in `main.py:23`: the SAQ Queue is already attached during lifespan startup. Routers that auto-enqueue (D-20) read it via `request: Request` or a `queue` dependency.
- **`uuid.uuid4()` and `UUID(as_uuid=True)`** columns: established in every model; ExecutionLog already uses this for its PK (D-13's "agent-generated UUID" is just `str(uuid.uuid4())` on the agent side, parsed back to UUID by Pydantic).
- **`SettingsConfigDict`** in `config.py`: pattern for adding env-driven knobs (token_prefix, chunk_max if needed).

### Established Patterns
- **One router file per resource** (`companion.py`, `cue.py`, `duplicates.py`, etc.): D-08's flat `agent_*.py` naming matches exactly.
- **`APIRouter(prefix=..., tags=[...])`**: every router declares its own prefix; central `include_router` in `main.py` doesn't add additional prefixes. So `prefix="/api/internal/agent/files"` is correct.
- **Pydantic `BaseModel` in `schemas/`**: see `schemas/scan.py` for shape. Phase 25 adds `agent_files.py`, `agent_metadata.py`, etc. Each schema gets `model_config = ConfigDict(extra="forbid")` (D-16).
- **`TimestampMixin` for `created_at` / `updated_at`**: already on the `Agent` model; the new `last_status` column does NOT need its own timestamp because `updated_at` already covers it.
- **Naming convention from `base.py`**: any new index / FK / CHECK must use `ix_`, `fk_`, `ck_`, `uq_` prefixes. Migration 014's partial index name is `ix_agents_token_hash_active` (or similar — Claude's discretion on the suffix).

### Integration Points
- **5 new routers** registered in `main.py:create_app()`:
  ```python
  app.include_router(agent_files.router)
  app.include_router(agent_metadata.router)
  app.include_router(agent_fingerprint.router)
  app.include_router(agent_execution.router)
  app.include_router(agent_heartbeat.router)
  ```
- **1 new module** `src/phaze/routers/agent_auth.py` — exports `get_authenticated_agent`, `hash_token`, and the `Agent` dependency (used by every internal-agent route but is NOT itself a router).
- **1 new Alembic migration** `alembic/versions/014_add_last_status_to_agents.py` — adds `agents.last_status` JSONB nullable + partial index on `(token_hash) WHERE revoked_at IS NULL`. No backfill; the legacy agent never heartbeats.
- **1 small `Agent` model patch** in `src/phaze/models/agent.py` — add `last_status: Mapped[dict | None] = mapped_column(JSONB, nullable=True)`. Matches the JSONB pattern from `FileMetadata.raw_tags`, `TagWriteLog.before_tags`, etc.
- **Pydantic schemas** added to `src/phaze/schemas/` — one file per router (`agent_files.py`, `agent_metadata.py`, `agent_fingerprint.py`, `agent_execution.py`, `agent_heartbeat.py`).
- **Config knobs (optional)** in `phaze/config.py` — `agent_token_prefix` if Claude wants it env-configurable; `agent_file_chunk_max` if the chunk cap is adopted.
- **Tests** — new `tests/test_routers/test_agent_files.py`, `test_agent_metadata.py`, `test_agent_fingerprint.py`, `test_agent_execution.py`, `test_agent_heartbeat.py`, plus `test_agent_auth.py` covering token hashing + dep behaviour. A shared fixture (`authenticated_client`, `seed_test_agent`) lives in `conftest.py` or `tests/conftest_agent.py`.

</code_context>

<specifics>
## Specific Ideas

- Token prefix is exactly `phaze_agent_` (matches the `ghp_` / `slack_` / etc. naming family). Secret-scanner rules can be added to GitHub later; the prefix being part of the wire string is what makes that future trivial.
- The full token string (prefix + secret) is what gets hashed and stored. Server never strips the prefix before hashing — this means a future change to the prefix would invalidate all existing tokens, which is the right behavior (a prefix change is a versioning event).
- The SHA-256 hex column value is exactly 64 chars; `agents.token_hash` is `String(128)` from Phase 24, leaving room if a future token format needs more.
- The 403 response is intentionally indistinguishable for "unknown token" vs "revoked token" — no oracle for enumerating revoked agent ids.
- `extract_file_metadata` is the existing SAQ task name (see `src/phaze/tasks/metadata_extraction.py`); the auto-enqueue in D-20 calls it with `file_id=str(record["id"])` matching the v3.0 contract.
- `phaze-agent-<id>` queue name uses the `agent.id` slug (kebab-case from Phase 24 D-01) — so the queue name is, e.g., `phaze-agent-fileserver-01`. The slug regex constraint (`^[a-z0-9]+(-[a-z0-9]+)*$`) guarantees no Redis key special chars.
- The OpenAPI `bearerAuth` securityScheme name should literally be `bearerAuth` (FastAPI convention) and apply only to the internal-agent tag-group.
- "Replay" in this phase means: same HTTP request body, same Authorization header, sent twice (or N times). The contract is: identical effect to sending it once, no error, no duplicate rows.
- For ExecutionLog: agent generates a UUID at the start of each file-operation attempt and persists it in its SAQ job state. On retry, the SAQ job resumes with the SAME UUID, so the POST is a guaranteed no-op (D-13) and any subsequent PATCH targets the existing row.

</specifics>

<deferred>
## Deferred Ideas

- **Agent self-registration endpoint** — `POST /api/internal/agent/register` (OPS-06 Future). v4.0 keeps operator pre-seeding via SQL/admin tooling in Phase 29; no HTTP registration surface in Phase 25.
- **mTLS in addition to bearer tokens** — OPS-05; deferred. Phase 29 handles self-signed HTTPS + CA pinning; mTLS is a future hardening pass.
- **Token-rotation overlap window** — agents holding two simultaneously-valid tokens during rotation. AUTH-04 is satisfied by "revoke + issue new"; an overlap window (KID + secret split, multiple active tokens per agent) is deferred to a future milestone.
- **Idempotency-Key header pattern** — Stripe-style per-request idempotency keys with a (key → response) cache. Not needed; natural-key idempotency (D-12, D-13) covers every endpoint without extra plumbing.
- **Rate limiting / abuse controls** — private LAN, single trusted operator, very small set of known agents. No `slowapi` / `fastapi-limiter` in Phase 25. Revisit only if multi-tenant arrives (OPS-06 follow-on).
- **ScanBatch endpoints** — Phase 27's job. Watcher attaches to the LIVE sentinel via `agent_id`; user-initiated scan POSTs are the Phase 27 admin endpoint, not an internal-agent endpoint.
- **LIVE sentinel auto-creation for new agents** — when a new agent row is inserted (by Phase 29's `just create-agent` task), its LIVE sentinel ScanBatch must also be inserted in the same transaction (Phase 24 D-11 schema-supports this, but the implementation is Phase 29's tooling concern). Phase 25 assumes the sentinel exists when called.
- **Cross-file-server fingerprint matching** — XAGENT-01; v4.0 documents this as a known limitation.
- **Heartbeat-driven queue depth** — even though D-17 includes `queue_depth` in the heartbeat payload, the Phase 29 admin page may eventually also poll Redis directly for fresher data. That's a Phase 29 implementation choice; Phase 25 just provides the field.
- **Per-endpoint metrics (Prometheus)** — OPS-07; deferred.
- **SecretStr for the bearer token in agent-side config** — pydantic-settings supports it; Phase 26's `agent_worker` startup will use it. Phase 25 is server-side and only sees the hash, so SecretStr is irrelevant here.

</deferred>

---

*Phase: 25-internal-agent-http-api-bearer-auth*
*Context gathered: 2026-05-11*
