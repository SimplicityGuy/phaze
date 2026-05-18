# Phase 25: Internal Agent HTTP API & Bearer Auth - Research

**Researched:** 2026-05-11
**Domain:** Authenticated, idempotent HTTP API surface on a FastAPI + SQLAlchemy 2.x async + SAQ stack
**Confidence:** HIGH

## Summary

Phase 25 wires an authenticated `/api/internal/agent/*` surface onto the existing FastAPI app. The phase is largely *applied* engineering on a stack the codebase already uses heavily: FastAPI 0.136.1, SQLAlchemy 2.0.49 + asyncpg, SAQ 0.26.3, Pydantic 2.12.5, Postgres 16. Every required primitive — `pg_insert(...).on_conflict_do_update(...)`, `request.app.state.queue.enqueue(...)`, `Depends(get_session)`, `ConfigDict(extra="forbid")` — already has a verified working example in the repo. There is essentially no greenfield framework risk; the work is composing existing idioms into 5 new routers, 1 auth helper, 1 migration, and ~50 tests.

Three small idioms need confirmation before planning:
1. **FastAPI's `HTTPBearer` security class** is the right tool — it emits OpenAPI `bearerAuth` automatically, sets `WWW-Authenticate: Bearer` on 401 per RFC 6750, and is trivially subclassable to customize the 401-vs-403 split D-06 requires.
2. **Postgres `RETURNING (xmax = 0) AS inserted`** is the canonical single-roundtrip idiom for distinguishing INSERTed vs UPDATEd rows in an UPSERT. It works in Postgres 16 (and every version since 9.5), but relies on an implementation detail — caveat documented below and mitigated by a regression test.
3. **SAQ's `Queue` constructor accepts a `name=` parameter** that maps to the Redis key namespace; `Queue.from_url(redis_url, name=f"phaze-agent-{agent.id}")` is the supported public API for per-agent routing. The existing app-wide queue at `request.app.state.queue` is the *default* queue and cannot be used for per-agent enqueue.

**Primary recommendation:** Build a thin `phaze.routers.agent_auth` module that exports a `HTTPBearer` subclass plus a `get_authenticated_agent` async dependency. All 5 new routers consume that dep. The file-upsert endpoint adds `.returning(FileRecord.id, FileRecord.file_type, literal_column("(xmax = 0)").label("inserted"))` to its `pg_insert` statement and walks the result set after commit to enqueue onto `Queue.from_url(settings.redis_url, name=f"phaze-agent-{agent.id}")`. Per-agent queue instances are constructed *on demand* inside the handler and `disconnect()`'d in a `finally` block — they are cheap because they share the Redis connection pool with `app.state.queue` at the lower level. Schemas all set `ConfigDict(extra="forbid")`. Tests use a single `seed_test_agent` fixture, a single `authenticated_client` fixture, and `AsyncMock` patched on `Queue` to assert enqueue calls without standing up Redis.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Bearer token verification | API / Backend | — | Auth must run server-side before any handler logic — agent (the consumer) is untrusted by definition |
| Token hash storage | Database | — | `agents.token_hash` (BTREE indexed) is the single source of truth; revocation toggles a column, no cache |
| Idempotent file upsert | API / Backend → Database | — | `ON CONFLICT DO UPDATE` is a DB feature; the API handler just wraps it |
| Auto-enqueue extract_file_metadata | API / Backend → Redis (SAQ) | — | The handler that did the upsert detects new rows (`xmax=0`) and enqueues — same transaction boundary owns this |
| Heartbeat persistence | Database (JSONB) | — | No Redis caching layer in v4.0; one source of truth per CONTEXT.md D-17 |
| OpenAPI `bearerAuth` declaration | API / Backend | — | FastAPI auto-generates the schema from `HTTPBearer` instance |
| Monotonic ExecutionLog status check | API / Backend | — | Application-level invariant; expressed as ordered enum + 409 response, not a DB CHECK |

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| FastAPI | 0.136.1 | HTTP framework, security primitives, OpenAPI generation | Already in use across all 13 existing routers. `HTTPBearer` is the idiomatic bearer-token auth. [VERIFIED: `uv pip show fastapi`] |
| `fastapi.security.HTTPBearer` | bundled | Authorization header parsing, OpenAPI security scheme emission | Standard. Subclassable for custom error status (CONTEXT.md D-06). [CITED: fastapi.tiangolo.com/reference/security] |
| SQLAlchemy | 2.0.49 | Async ORM, `pg_insert(...).on_conflict_do_update(...)` | Used in `services/ingestion.py:91-119` — exact pattern to mirror. [VERIFIED: `uv pip show sqlalchemy`] |
| asyncpg | ≥0.31.0 | PostgreSQL async driver | Already wired through `database.py`. No changes needed. [VERIFIED: pyproject.toml] |
| Pydantic | 2.12.5 | Request/response validation, `ConfigDict(extra="forbid")` | Industry standard, already in use. v2 syntax confirmed. [VERIFIED: `uv pip show pydantic`] |
| SAQ | 0.26.3 | Redis-backed async task queue, multi-queue support via `name=` parameter | Already in use; per-agent queue routing is the v4.0 contract. [VERIFIED: `.venv/.../saq/queue/redis.py:64-77`] |
| `hashlib.sha256` | stdlib | Token hashing — input is uniform-random, no KDF needed (D-04) | stdlib, zero dependencies. [VERIFIED: built-in] |
| `secrets` | stdlib | Optional `compare_digest` for defence-in-depth (D-04 discretion) | stdlib, constant-time string comparison. [VERIFIED: built-in] |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `sqlalchemy.literal_column` | bundled | Express raw `(xmax = 0)` expression in a `.returning()` clause | File-upsert endpoint only; lets us read the inserted-vs-updated flag in a single round-trip. [CITED: docs.sqlalchemy.org/en/20/core/sqlelement.html] |
| Alembic | ≥1.18.4 | Migration 014 for `agents.last_status` JSONB + partial token-hash index | Standard for the project. [VERIFIED: pyproject.toml] |
| `httpx.AsyncClient` + `ASGITransport` | bundled (httpx) | Test client for bearer-pre-set requests | Existing pattern in `tests/conftest.py:62-64`. [VERIFIED: existing tests pass] |
| `unittest.mock.AsyncMock` | stdlib | Mock `Queue.enqueue` to avoid Redis in tests | Established pattern in `tests/test_routers/test_scan.py:25-26`. [VERIFIED: existing tests] |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `HTTPBearer` subclass | Hand-rolled `Header(...)` + manual `Authorization` parsing | Hand-rolling loses automatic OpenAPI `bearerAuth` emission, loses `WWW-Authenticate: Bearer` on 401, requires re-implementing the "missing-vs-malformed" header distinction. No good reason to do it. |
| `RETURNING (xmax = 0)` | Separate `SELECT` after UPSERT to look up which rows existed | Extra round-trip + race condition (concurrent agent retries could insert+overwrite between the SELECT and the UPSERT). Worse on all axes. |
| `RETURNING (xmax = 0)` | Enqueue `extract_file_metadata` for every row regardless of insert/update | Wrong — re-walking a directory would re-trigger metadata extraction for unchanged files (violates D-20). |
| `Queue.from_url(name=...)` per handler call | One shared `app.state.queues[agent_id]` dict | Adds startup-time complexity, requires invalidation when agents are added/revoked. Per-call construction is simple, and the underlying Redis pool is shared by aioredis. |
| Per-route `Depends` | FastAPI router-level `dependencies=[...]` | `dependencies=[...]` runs the dep but doesn't expose the return value to the handler. We need the `Agent` in the handler body (`agent_id` is stamped onto every upserted row). |
| `Idempotency-Key` header pattern | Stripe-style per-request idempotency key + cache | CONTEXT.md "Deferred Ideas" — natural-key idempotency covers every endpoint; no extra plumbing. |

**Installation:** No new dependencies. All required libraries are already in `pyproject.toml`.

**Version verification:** All packages verified via `uv pip show` on the active `.venv` on 2026-05-11. [VERIFIED]

## Architecture Patterns

### System Architecture Diagram

```
                ┌────────────────────────────────────┐
                │   Agent (file server, untrusted)   │
                │   Authorization: Bearer phaze_…    │
                └─────────────┬──────────────────────┘
                              │ HTTPS (Phase 29)
                              ▼
              ╔═══════════════════════════════════════╗
              ║   FastAPI app — /api/internal/agent/* ║
              ║                                       ║
              ║  ┌──────────────────────────────┐     ║
              ║  │  HTTPBearer (security scheme)│     ║
              ║  │  → emits OpenAPI bearerAuth  │     ║
              ║  │  → 401 if header missing     │     ║
              ║  └────────────┬─────────────────┘     ║
              ║               ▼                       ║
              ║  ┌──────────────────────────────┐     ║
              ║  │  get_authenticated_agent dep │     ║
              ║  │  1. extract credentials      │     ║
              ║  │  2. sha256(token).hex()      │     ║
              ║  │  3. SELECT agents WHERE      │     ║
              ║  │     token_hash = ? AND       │     ║
              ║  │     revoked_at IS NULL       │     ║
              ║  │  4. 403 if no row            │     ║
              ║  │  5. yield Agent              │     ║
              ║  └────────────┬─────────────────┘     ║
              ║               ▼                       ║
              ║  ┌────────────────────────────────┐   ║
              ║  │ Router handler                 │   ║
              ║  │ ────────────                   │   ║
              ║  │  • Pydantic schema             │   ║
              ║  │    (extra="forbid")            │   ║
              ║  │  • Use agent.id (not body)     │   ║
              ║  │  • Idempotent UPSERT via       │   ║
              ║  │    pg_insert(...).on_conflict_ │   ║
              ║  │    do_update(...).returning()  │   ║
              ║  │  • Commit                      │   ║
              ║  │  • (files only) enqueue        │   ║
              ║  │    extract_file_metadata for   │   ║
              ║  │    rows where xmax = 0         │   ║
              ║  └─────┬──────────────────┬───────┘   ║
              ║        │                  │           ║
              ║  ┌─────▼─────┐   ┌────────▼────────┐  ║
              ║  │ Postgres  │   │ Queue.from_url( │  ║
              ║  │  16       │   │  redis_url,     │  ║
              ║  │           │   │  name=          │  ║
              ║  │ agents,   │   │  f"phaze-agent- │  ║
              ║  │ files,    │   │  {agent.id}")   │  ║
              ║  │ metadata, │   │  .enqueue(...)  │  ║
              ║  │ exec_log, │   └─────────────────┘  ║
              ║  │ etc.      │                        ║
              ║  └───────────┘                        ║
              ╚═══════════════════════════════════════╝
```

**Data flow (file upsert, the most complex endpoint):**
1. Agent POSTs `{files: [...]}` to `/api/internal/agent/files` with `Authorization: Bearer phaze_agent_…`
2. `HTTPBearer` extracts the token; missing/malformed → 401 with `WWW-Authenticate: Bearer`
3. `get_authenticated_agent` hashes, looks up, returns `Agent` (or raises 403)
4. Handler validates Pydantic body (`extra="forbid"`); unknown fields → 422 auto-generated
5. Handler stamps `agent.id` onto each record (ignores any `agent_id` in the body)
6. `pg_insert(FileRecord).values([...]).on_conflict_do_update(index_elements=["agent_id", "original_path"], set_={...}).returning(FileRecord.id, FileRecord.file_type, literal_column("(xmax = 0)").label("inserted"))`
7. `await session.commit()`
8. Walk the `RETURNING` rows: for each row where `inserted is True` AND `file_type ∈ {music, video}`, enqueue `extract_file_metadata` on the per-agent queue
9. Return `200 OK` with `{"agent_id": "...", "upserted": N, "inserted": M}` (or similar minimal echo per D-19)

### Recommended Project Structure

```
src/phaze/
├── routers/
│   ├── agent_auth.py          # NEW — get_authenticated_agent dep + hash_token helper (NOT a router)
│   ├── agent_files.py         # NEW — POST /api/internal/agent/files (chunked upsert + auto-enqueue)
│   ├── agent_metadata.py      # NEW — PUT /api/internal/agent/metadata/{file_id}
│   ├── agent_fingerprint.py   # NEW — PUT /api/internal/agent/fingerprints/{file_id}/{engine}
│   ├── agent_execution.py     # NEW — POST + PATCH /api/internal/agent/execution-log
│   └── agent_heartbeat.py     # NEW — POST /api/internal/agent/heartbeat
├── schemas/
│   ├── agent_files.py         # NEW — FileUpsertChunk, FileUpsertRecord
│   ├── agent_metadata.py      # NEW — MetadataWriteRequest
│   ├── agent_fingerprint.py   # NEW — FingerprintWriteRequest
│   ├── agent_execution.py     # NEW — ExecutionLogCreate, ExecutionLogPatch
│   └── agent_heartbeat.py     # NEW — HeartbeatRequest
├── models/
│   └── agent.py               # MODIFY — add last_status JSONB column
└── main.py                    # MODIFY — register 5 new routers, declare bearerAuth scheme

alembic/versions/
└── 014_add_last_status_to_agents.py  # NEW — JSONB column + partial token_hash index

tests/test_routers/
├── test_agent_auth.py         # NEW — dep behaviour (200/401/403, hash, revoke)
├── test_agent_files.py        # NEW — upsert + idempotency + auto-enqueue assertion
├── test_agent_metadata.py     # NEW
├── test_agent_fingerprint.py  # NEW
├── test_agent_execution.py    # NEW — create + monotonic-PATCH 409
└── test_agent_heartbeat.py    # NEW — 204 + last_status JSONB persistence

tests/
└── conftest.py                # MODIFY — add seed_test_agent + authenticated_client fixtures
```

### Pattern 1: HTTPBearer subclass for 401-vs-403 split (D-06)

**What:** Subclass `HTTPBearer` so the *missing/malformed header* case returns 401 with `WWW-Authenticate: Bearer`, but the *unknown/revoked token* case (which only the lookup code knows about) returns 403.
**When to use:** Phase 25's auth dep. Every `/api/internal/agent/*` route uses this.
**Example:**
```python
# Source: pattern verified via [CITED: fastapi.tiangolo.com/how-to/authentication-error-status-code]
# combined with hash-lookup logic from CONTEXT.md D-03

import hashlib
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent


# auto_error=True (default) gives us 401 + WWW-Authenticate: Bearer on missing header per RFC 6750.
# We do NOT subclass to change error behaviour — we want the default 401 for missing.
bearer_scheme = HTTPBearer(scheme_name="bearerAuth", description="Per-agent bearer token (phaze_agent_<32 urlsafe-base64 bytes>)")


def hash_token(token: str) -> str:
    """SHA-256 hex of the entire wire token, prefix included (CONTEXT.md D-02)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def get_authenticated_agent(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Agent:
    """Resolve the calling agent from the bearer token.

    - Missing/malformed Authorization header → 401 (raised by HTTPBearer before this code runs)
    - Token hash unknown OR row has revoked_at IS NOT NULL → 403
    """
    token_hash = hash_token(credentials.credentials)
    stmt = select(Agent).where(Agent.token_hash == token_hash, Agent.revoked_at.is_(None))
    agent = (await session.execute(stmt)).scalar_one_or_none()
    if agent is None:
        # 403 — intentionally indistinguishable for "unknown" vs "revoked" (CONTEXT.md D-06, specifics)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return agent
```

Note: this dep does **not** add `WWW-Authenticate` to the 403 response — that header is for 401 per RFC 6750.

### Pattern 2: Idempotent UPSERT with insert-detection (D-20, D-21)

**What:** Use `pg_insert(...).on_conflict_do_update(...).returning(..., literal_column("(xmax = 0)").label("inserted"))`. The boolean column is `True` for INSERTed rows, `False` for UPDATEd rows, in a single round-trip.
**When to use:** File-upsert endpoint (the only one that auto-enqueues). Other endpoints can omit `inserted` because they don't gate side-effects on it.
**Example:**
```python
# Source: pattern composed from services/ingestion.py:91-119 (UPSERT shape, VERIFIED in repo)
# + [CITED: sigpwned.com/2023/08/10/postgres-upsert-created-or-updated/] (xmax=0 idiom)
# + [CITED: docs.sqlalchemy.org/en/20/core/sqlelement.html] (literal_column)

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.models.file import FileRecord


async def upsert_files_with_insert_detection(session, records, agent_id):
    """UPSERT and return (id, file_type, inserted_flag) per row in one round-trip."""
    # Stamp agent_id from auth dep — never trust request body (CONTEXT.md AUTH-01)
    for r in records:
        r["agent_id"] = agent_id

    stmt = pg_insert(FileRecord).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=["agent_id", "original_path"],
        set_={
            "sha256_hash": stmt.excluded.sha256_hash,
            "file_size": stmt.excluded.file_size,
            "state": stmt.excluded.state,
            "batch_id": stmt.excluded.batch_id,
            "file_type": stmt.excluded.file_type,
        },
    ).returning(
        FileRecord.id,
        FileRecord.file_type,
        literal_column("(xmax = 0)").label("inserted"),
    )
    result = await session.execute(stmt)
    rows = result.all()
    await session.commit()
    return rows  # list[Row] with .id, .file_type, .inserted
```

**Renders to SQL (Postgres 16):**
```sql
INSERT INTO files (agent_id, original_path, sha256_hash, ...)
VALUES (...), (...), ...
ON CONFLICT (agent_id, original_path) DO UPDATE
  SET sha256_hash = EXCLUDED.sha256_hash, ...
RETURNING files.id, files.file_type, (xmax = 0) AS inserted;
```

### Pattern 3: Per-agent SAQ queue enqueue (D-22)

**What:** Construct a `Queue` instance keyed by the agent id; SAQ's `name=` parameter maps to the Redis namespace, so `phaze-agent-<id>` becomes the Redis key prefix.
**When to use:** Inside the file-upsert handler, after commit, for each INSERTed music/video row.
**Example:**
```python
# Source: pattern verified against [VERIFIED: .venv/.../saq/queue/redis.py:64-77]
# - RedisQueue.from_url(url, **kwargs) passes kwargs through to __init__
# - __init__(redis, name="default", ...) — name defaults to "default"
# - queue.namespace(key) returns "saq:{name}:{key}" — so name="phaze-agent-fs01" gives Redis keys "saq:phaze-agent-fs01:queued"

from saq import Queue

from phaze.config import settings
from phaze.constants import EXTENSION_MAP, FileCategory


_EXTRACTABLE = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})


async def enqueue_metadata_extraction_for_new_files(agent_id: str, returning_rows) -> int:
    """For each row where inserted=True AND file is music/video, enqueue extract_file_metadata.

    Per CONTEXT.md D-22, queue name is exactly f"phaze-agent-{agent.id}".
    Per Claude's discretion in CONTEXT.md, enqueue is AFTER commit; failures here log + continue.
    """
    queue_name = f"phaze-agent-{agent_id}"
    queue = Queue.from_url(settings.redis_url, name=queue_name)
    enqueued = 0
    try:
        for row in returning_rows:
            if not row.inserted:
                continue
            ext = "." + row.file_type.lower()
            if EXTENSION_MAP.get(ext, FileCategory.UNKNOWN) not in _EXTRACTABLE:
                continue
            await queue.enqueue("extract_file_metadata", file_id=str(row.id))
            enqueued += 1
    finally:
        await queue.disconnect()
    return enqueued
```

**Why construct per-call instead of caching:**
- `Queue.from_url` is cheap — wraps `aioredis.from_url()` which itself uses a connection pool
- Agents come and go (registration is operator-driven); pre-creating queues at startup would require live cache invalidation
- The default queue at `app.state.queue` only knows the "default" name; we cannot reuse it for per-agent routing
- `disconnect()` releases the per-agent client back to the pool — clean

**Optimisation (deferred to Phase 26):** A `phaze.services.agent_queues` helper that caches `Queue` instances keyed by agent_id with a TTL. Not needed in Phase 25 — premature optimisation. The phase 25 plan should explicitly note this is a known follow-on.

### Pattern 4: Pydantic v2 strict request schemas (D-16)

**What:** Every request body schema uses `model_config = ConfigDict(extra="forbid")`. FastAPI auto-translates the resulting `ValidationError` into a 422 response with details.
**When to use:** Every Phase 25 request body. Response schemas don't need `extra="forbid"`.
**Example:**
```python
# Source: [CITED: pydantic.dev/concepts/models — Extra fields handling]

from pydantic import BaseModel, ConfigDict, Field


class FileUpsertRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256_hash: str = Field(min_length=64, max_length=64)
    original_path: str = Field(min_length=1)
    original_filename: str
    current_path: str
    file_type: str = Field(min_length=1, max_length=10)
    file_size: int = Field(ge=0)
    # NB: NO agent_id field — comes from auth dep, never the body (AUTH-01)


class FileUpsertChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[FileUpsertRecord] = Field(min_length=1, max_length=1000)  # 1000 = chunk cap per CONTEXT.md discretion
```

**Resulting 422 on extra field:**
```json
{
  "detail": [{
    "type": "extra_forbidden",
    "loc": ["body", "files", 0, "agent_id"],
    "msg": "Extra inputs are not permitted",
    "input": "sneaky-agent-id"
  }]
}
```

### Pattern 5: Monotonic status check on ExecutionLog PATCH (D-15)

**What:** Express the order `PENDING < IN_PROGRESS < COMPLETED < FAILED` as a Python tuple lookup; reject backward transitions and terminal-state mutations with 409.
**When to use:** `PATCH /api/internal/agent/execution-log/{id}` handler only.
**Example:**
```python
# Source: standard application-level invariant check; no library needed
# [VERIFIED: phaze.models.execution.ExecutionStatus enum has the 4 values]

from phaze.models.execution import ExecutionStatus


_STATUS_ORDER = {
    ExecutionStatus.PENDING: 0,
    ExecutionStatus.IN_PROGRESS: 1,
    ExecutionStatus.COMPLETED: 2,
    ExecutionStatus.FAILED: 3,
}
_TERMINAL = {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED}


def check_monotonic(current: str, proposed: str) -> str | None:
    """Return None if the transition is allowed, else an error detail for the 409 response."""
    cur = ExecutionStatus(current)
    new = ExecutionStatus(proposed)
    if cur in _TERMINAL:
        return "execution-log status is terminal"
    # Same status is allowed (idempotent re-PATCH from a retry)
    if _STATUS_ORDER[new] < _STATUS_ORDER[cur]:
        return "execution-log status would regress"
    return None


# In handler:
# err = check_monotonic(existing.status, body.status)
# if err: raise HTTPException(status_code=409, detail=err)
```

Note: `ExecutionStatus` is stored as `String(20)` (verified in `models/execution.py:34`) not a Postgres enum, so adding new values would never need a migration. Doesn't affect this phase, but worth confirming.

### Pattern 6: OpenAPI bearerAuth declaration

**What:** Pass `scheme_name="bearerAuth"` to the `HTTPBearer` instance. FastAPI emits the security scheme automatically; routes that depend on it get a lock icon in `/docs`.
**When to use:** Once, on the shared `HTTPBearer` instance in `agent_auth.py`.
**Example:**
```python
# Source: [CITED: fastapi.tiangolo.com/reference/security — HTTPBearer constructor]
# scheme_name appears in the OpenAPI schema under components.securitySchemes.<scheme_name>

bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    description="Per-agent bearer token. Format: phaze_agent_<32 urlsafe-base64 bytes>",
    auto_error=True,  # default — emits 401 with WWW-Authenticate: Bearer on missing header
)
```

No extra `app.openapi_extra` configuration needed — the scheme registers automatically via the Depends chain.

### Anti-Patterns to Avoid

- **Reading `agent_id` from the request body:** every endpoint resolves `agent_id` from the auth dep (`agent.id`). Schemas explicitly omit any `agent_id` field. `extra="forbid"` makes "I tried to sneak it in" a 422 at the boundary. (CONTEXT.md AUTH-01.)
- **Caching the auth lookup result in-process:** would defeat AUTH-04 (immediate revocation). The DB SELECT is the cache — it's a single indexed equality lookup, microsecond-scale.
- **Subclassing `HTTPBearer` just to change 401 → 403 globally:** breaks RFC 6750 + loses the `WWW-Authenticate: Bearer` header. Keep 401 for missing/malformed; use a separate `HTTPException(403, ...)` inside the resolver for unknown/revoked.
- **Hand-rolled `Authorization` header parsing with `Header(...)`:** loses automatic OpenAPI `bearerAuth` emission. Use `HTTPBearer`.
- **Enqueuing inside the transaction:** if the commit then fails, jobs are already in Redis pointing at non-existent rows. Always enqueue AFTER commit (CONTEXT.md discretion confirms this). On enqueue failure, log + continue — `extract_file_metadata` can be re-triggered manually via Phase 27's UI.
- **Eager `Queue` instances cached at app startup:** agents are not known at startup. Per-call construction is correct.
- **`secrets.compare_digest` on the token before the SELECT:** the SELECT IS the comparison; there's nothing to `compare_digest` against until after the SELECT, by which point Postgres has already done a constant-time-on-uniform-random-hashes equality. Defence-in-depth on the hex string after the lookup is a no-op. Skip it; document the reasoning.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Authorization header parsing | Custom `Header()` reader with split-on-space logic | `fastapi.security.HTTPBearer` | Free OpenAPI integration, RFC 6750 compliance, free `WWW-Authenticate` |
| UPSERT insert-vs-update detection | Separate SELECT before UPSERT, or per-row INSERT-then-fallback-UPDATE | `RETURNING (xmax = 0) AS inserted` | Single round-trip, no race, no extra query |
| Bulk INSERT/UPDATE | Loop with individual statements | `pg_insert(Model).values([dict, dict, ...]).on_conflict_do_update(...)` | One round-trip per chunk, idempotent, established in `services/ingestion.py:91-119` |
| Token hashing comparison | Custom timing-safe hex compare | Postgres-side equality lookup with btree index | The Index does the comparison; uniform-random input means no timing oracle |
| Pydantic strict-mode forbidding extras | Custom `__init_subclass__` checks | `ConfigDict(extra="forbid")` | Native, free 422 with structured error |
| Per-agent queue routing | Custom SAQ wrapper class | `Queue.from_url(url, name="phaze-agent-<id>")` | The `name=` parameter is the supported public API |
| OpenAPI security scheme | Manual `app.openapi = lambda: ...` patching | `HTTPBearer(scheme_name="bearerAuth")` + `Depends(...)` chain | FastAPI's introspection auto-emits the spec |
| Monotonic state machine | Custom decorator framework | Ordered tuple lookup + `HTTPException(409)` | Three lines of code; framework would be overkill |

**Key insight:** Phase 25's "novelty" is composition, not invention. Every primitive is already-load-tested. The risk is in *not following* the existing patterns, not in adopting them.

## Common Pitfalls

### Pitfall 1: `WWW-Authenticate` header missing on 401

**What goes wrong:** Hand-rolled auth returns 401 without `WWW-Authenticate: Bearer`. Strict HTTP clients (some `httpx` versions, browser fetch with credentials, OpenAPI test runners) treat this as a protocol violation and don't surface the auth challenge.
**Why it happens:** Easy to forget when raising `HTTPException(401, ...)` manually.
**How to avoid:** Use `HTTPBearer` with `auto_error=True` (the default) — FastAPI emits the header automatically. Don't override this behaviour.
**Warning signs:** Test that does `response = await client.post("/api/internal/agent/heartbeat")` (no auth) asserts `response.headers["WWW-Authenticate"] == "Bearer"`. Existing tests must include this.

### Pitfall 2: `xmax = 0` returning False for actually-new rows

**What goes wrong:** `xmax` is non-zero on a fresh INSERT under rare conditions: when the row is inserted *and* immediately deleted by a concurrent transaction, when MVCC HOT updates re-use the slot, or when triggers acquire row locks on the new row.
**Why it happens:** `xmax` is an MVCC implementation detail. The semantic is "transaction id of the deleting or locking transaction; 0 if no such transaction." A fresh insert that hasn't been touched by anything else has `xmax = 0`.
**How to avoid:**
- (1) Phase 25 has no triggers on `files` (verified — grep `alembic/versions/` for `CREATE TRIGGER`).
- (2) The endpoint is single-statement; no concurrent transaction can hold a row lock during the same statement that did the INSERT.
- (3) Add an explicit regression test: insert a brand-new (agent_id, original_path), assert `inserted is True`; then upsert the same key, assert `inserted is False`.
- (4) Document the assumption in the handler's docstring so a future contributor adding a trigger sees the warning.
**Warning signs:** Auto-enqueue stops working on a Postgres major-version bump or after someone adds a trigger to `files`. The regression test will catch it.

### Pitfall 3: Caching the auth dep result silently

**What goes wrong:** FastAPI caches dependency results per-request by default (`use_cache=True`). That's fine within one request. But if someone adds `Depends(get_authenticated_agent)` to a sub-dependency that gets re-evaluated per row (e.g., a streaming response), it could memoize a stale agent.
**Why it happens:** Streaming/SSE responses with per-event sub-deps are a v3.0 pattern in this codebase (see `routers/execution.py:execution_progress`).
**How to avoid:** Don't use `get_authenticated_agent` in SSE/streaming handlers. Internal-agent endpoints are all simple request/response. If a future phase adds an agent-facing SSE endpoint, re-evaluate.
**Warning signs:** N/A in Phase 25 — no internal-agent SSE.

### Pitfall 4: Same-statement UPSERT with identical natural keys in the chunk

**What goes wrong:** If a single chunk has two records with the same `(agent_id, original_path)`, Postgres raises:
```
ERROR: ON CONFLICT DO UPDATE command cannot affect row a second time
HINT: Ensure that no rows proposed for insertion within the same command have duplicate constrained values.
```
**Why it happens:** Postgres prohibits affecting the same row twice in one statement. The agent could accidentally include the same path twice in a chunk.
**How to avoid:** Server-side dedup before the UPSERT: `records = list({(r.original_path,): r for r in records}.values())`. Or document that the agent must not send duplicates in a chunk — but server-side dedup is more robust.
**Warning signs:** 500 errors during scans of directories with case-insensitive duplicate paths on a case-sensitive filesystem (rare but possible). Test should cover this case.

### Pitfall 5: Pydantic `extra="forbid"` doesn't validate nested lists

**What goes wrong:** The outer schema has `extra="forbid"`, but nested item schemas don't. Extras inside list items slip through silently.
**Why it happens:** `ConfigDict(extra="forbid")` is per-class.
**How to avoid:** Every schema class in `schemas/agent_*.py` declares `ConfigDict(extra="forbid")`, including nested item schemas like `FileUpsertRecord`.
**Warning signs:** Test that sends `{"files": [{"agent_id": "evil", "sha256_hash": "...", ...}]}` should get 422 with `loc: ["body", "files", 0, "agent_id"]`. If it returns 200, the inner schema is missing `extra="forbid"`.

### Pitfall 6: Per-agent `Queue.from_url` leaking aioredis clients

**What goes wrong:** Each call to `Queue.from_url(...)` creates a new aioredis client via `aioredis.from_url(url)`. If `disconnect()` is not called, the client stays alive until GC.
**Why it happens:** Forgetting the `finally: await queue.disconnect()` block.
**How to avoid:** Wrap in `try/finally`, or use a small `async with` helper:
```python
@asynccontextmanager
async def per_agent_queue(agent_id: str):
    q = Queue.from_url(settings.redis_url, name=f"phaze-agent-{agent_id}")
    try:
        yield q
    finally:
        await q.disconnect()
```
**Warning signs:** Connection-pool exhaustion under load. With single-user app + small N agents, unlikely to surface in v4.0 — but the helper is so cheap to write it should land in Phase 25 to prevent the foot-gun.

### Pitfall 7: NFC normalization mismatch on original_path

**What goes wrong:** Agent sends path normalized differently (NFD vs NFC) than the server expects. UPSERT misses the conflict target, INSERTs a duplicate row.
**Why it happens:** `services/ingestion.py:32-33` NFC-normalizes paths during scan. Agent code in Phase 26 must do the same. Phase 25's endpoint should defensively NFC-normalize on receive.
**How to avoid:** Apply `unicodedata.normalize("NFC", record["original_path"])` server-side before the UPSERT. Single line, eliminates the entire class of bug.
**Warning signs:** Same file appearing as two rows after a scan + watcher event.

## Runtime State Inventory

> N/A — Phase 25 is greenfield code (adds endpoints + 1 migration with no data backfill). Not a rename/refactor.

## Code Examples

### Complete auth router module (`src/phaze/routers/agent_auth.py`)
```python
# Source: composed from CONTEXT.md D-01..D-07 + [CITED: fastapi.tiangolo.com/reference/security]
"""Bearer-token authentication helper for /api/internal/agent/* routes.

Not a router itself — exports get_authenticated_agent for use as Depends() in
every agent-internal route handler. Every route adds:

    agent: Agent = Depends(get_authenticated_agent)

Status codes:
  - 401 (missing/malformed Authorization header): emitted by HTTPBearer with
    WWW-Authenticate: Bearer per RFC 6750.
  - 403 (token hash unknown OR row revoked_at IS NOT NULL): intentionally
    indistinguishable — no oracle for "does this token exist?".
"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent


bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    description="Per-agent bearer token. Format: phaze_agent_<32 urlsafe-base64 bytes>.",
)


def hash_token(token: str) -> str:
    """SHA-256 hex of the entire wire token (prefix included). Per CONTEXT.md D-02."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def get_authenticated_agent(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Agent:
    """Resolve the calling agent from the bearer token; raise 403 if unknown/revoked."""
    token_hash = hash_token(credentials.credentials)
    stmt = select(Agent).where(Agent.token_hash == token_hash, Agent.revoked_at.is_(None))
    agent = (await session.execute(stmt)).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return agent
```

### Heartbeat router (simplest endpoint as anchor example)
```python
# Source: composed from CONTEXT.md D-17, D-19 + verified router patterns
"""POST /api/internal/agent/heartbeat — agent liveness signal."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_heartbeat import HeartbeatRequest


router = APIRouter(prefix="/api/internal/agent/heartbeat", tags=["agent-internal"])


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def post_heartbeat(
    body: HeartbeatRequest,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Update agents.last_seen_at and last_status. Returns 204."""
    await session.execute(
        update(Agent)
        .where(Agent.id == agent.id)
        .values(
            last_seen_at=func.now(),
            last_status=body.model_dump(),
        )
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

### Migration 014 sketch
```python
# Source: composed from alembic/versions/013_*.py pattern (VERIFIED in repo)
"""Add agents.last_status JSONB column and partial token-hash index.

Revision ID: 014
Revises: 013
Create Date: 2026-05-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "014"
down_revision: str | Sequence[str] | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add last_status JSONB + partial index on token_hash WHERE revoked_at IS NULL."""
    op.add_column("agents", sa.Column("last_status", postgresql.JSONB, nullable=True))
    op.create_index(
        "ix_agents_token_hash_active",
        "agents",
        ["token_hash"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    """Drop partial index and last_status column."""
    op.drop_index("ix_agents_token_hash_active", table_name="agents")
    op.drop_column("agents", "last_status")
```

### Authenticated test client fixture (shared)
```python
# Source: extends tests/conftest.py:62 pattern + adds bearer pre-set
import hashlib
import secrets

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.agent import Agent


@pytest_asyncio.fixture
async def seed_test_agent(session):
    """Create a known agent with a known token. Returns (agent, raw_token)."""
    raw_token = "phaze_agent_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    agent = Agent(
        id="test-agent-01",
        name="test-agent-01",
        token_hash=token_hash,
        scan_roots=["/test/music"],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, raw_token


@pytest_asyncio.fixture
async def authenticated_client(session, seed_test_agent):
    """AsyncClient with Authorization: Bearer <known token> pre-set."""
    _agent, raw_token = seed_test_agent
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        yield ac
```

### Asserting SAQ enqueue in tests (no Redis)
```python
# Source: pattern verified in tests/test_routers/test_scan.py:25-26
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_upsert_enqueues_extract_for_new_music_files(authenticated_client, seed_test_agent, session):
    agent, _raw = seed_test_agent
    chunk = {"files": [
        {"sha256_hash": "0" * 64, "original_path": "/test/music/a.mp3", "original_filename": "a.mp3",
         "current_path": "/test/music/a.mp3", "file_type": "mp3", "file_size": 100},
    ]}

    with patch("phaze.routers.agent_files.Queue") as MockQueue:
        mock_queue = AsyncMock()
        MockQueue.from_url.return_value = mock_queue

        response = await authenticated_client.post("/api/internal/agent/files", json=chunk)

    assert response.status_code == 200
    # Assert per-agent queue name was used
    MockQueue.from_url.assert_called_once_with(
        "redis://redis:6379/0",  # or settings.redis_url
        name=f"phaze-agent-{agent.id}",
    )
    # Assert one enqueue call for the music file
    mock_queue.enqueue.assert_awaited_once()
    args, kwargs = mock_queue.enqueue.call_args
    assert args[0] == "extract_file_metadata"
    assert "file_id" in kwargs
    # Cleanup
    mock_queue.disconnect.assert_awaited_once()
```

### Replay-idempotency test
```python
@pytest.mark.asyncio
async def test_upsert_replay_no_duplicates(authenticated_client, seed_test_agent, session):
    chunk = {"files": [{
        "sha256_hash": "0" * 64, "original_path": "/test/music/a.mp3", "original_filename": "a.mp3",
        "current_path": "/test/music/a.mp3", "file_type": "mp3", "file_size": 100,
    }]}
    with patch("phaze.routers.agent_files.Queue"):
        r1 = await authenticated_client.post("/api/internal/agent/files", json=chunk)
        r2 = await authenticated_client.post("/api/internal/agent/files", json=chunk)
    assert r1.status_code == 200
    assert r2.status_code == 200

    # Only one row in DB
    from phaze.models.file import FileRecord
    from sqlalchemy import select, func as sa_func
    result = await session.execute(select(sa_func.count()).select_from(FileRecord))
    assert result.scalar_one() == 1
```

### Revocation mid-test (AUTH-04)
```python
@pytest.mark.asyncio
async def test_revoke_blocks_next_call_without_restart(authenticated_client, seed_test_agent, session):
    agent, _raw = seed_test_agent

    # Sanity: first heartbeat succeeds
    r1 = await authenticated_client.post(
        "/api/internal/agent/heartbeat",
        json={"agent_version": "4.0.0", "worker_pid": 1234, "queue_depth": 0},
    )
    assert r1.status_code == 204

    # Revoke
    from sqlalchemy import update
    from sqlalchemy.sql import func as sa_func
    await session.execute(update(Agent).where(Agent.id == agent.id).values(revoked_at=sa_func.now()))
    await session.commit()

    # Next call rejected — NO restart, NO cache invalidation
    r2 = await authenticated_client.post(
        "/api/internal/agent/heartbeat",
        json={"agent_version": "4.0.0", "worker_pid": 1234, "queue_depth": 0},
    )
    assert r2.status_code == 403
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `OAuth2PasswordBearer` for bearer tokens | `HTTPBearer` | FastAPI 0.61+ | `HTTPBearer` is the right choice when you're not doing OAuth2 — cleaner OpenAPI |
| Pydantic v1 `class Config: extra = "forbid"` | Pydantic v2 `model_config = ConfigDict(extra="forbid")` | Pydantic 2.0 | Already on v2 in this repo |
| Postgres 17+ `MERGE ... RETURNING` (with `merge_action()`) | Postgres 16 — still `ON CONFLICT ... RETURNING (xmax = 0)` | PG 17 (2024) | Phase 25 is on Postgres 16; xmax idiom remains the standard |
| Postgres 18+ `RETURNING OLD.*, NEW.*` for clean insert/update detection | Postgres 16 — still `(xmax = 0)` | PG 18 (Sep 2025) | Future migration consideration; not applicable to Phase 25 |
| arq queue | SAQ queue | This repo migrated 2026-03 | All new task code uses SAQ. `Queue.from_url(url, name=...)` is the supported API |

**Deprecated/outdated:**
- `OAuth2PasswordBearer` for non-OAuth tokens — works but emits a misleading OpenAPI scheme. Use `HTTPBearer`.
- `psycopg2` — sync driver. asyncpg only.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | No triggers exist on `files`, `metadata`, `fingerprint_results`, `execution_log` that would set `xmax` non-zero on a fresh INSERT | Pitfall 2, Pattern 2 | Auto-enqueue silently no-ops. Mitigation: regression test in Phase 25 plan asserts `inserted=True` for a brand-new key |
| A2 | The 1000-record chunk cap is correct for the watcher; SCAN-02 says "e.g., 500" — assuming server-side cap of 1000 (CONTEXT.md discretion) | Pattern 4 | If the actual cap should be different, the schema's `Field(max_length=...)` value changes. No structural impact |
| A3 | Per-call `Queue.from_url` is acceptable performance for a single-user app with <10 agents | Pattern 3 | Connection-pool pressure under high concurrent enqueue. Mitigation: defer optimisation to Phase 26 if measured |
| A4 | The default error response shape `{"detail": "..."}` for 401/403/422/409 is acceptable to the planner | Anti-patterns | No structural risk — easy to change in one place if needed |

**None of these assumptions block planning.** A1 has a concrete mitigation (regression test). A2 is a numeric tunable. A3 is performance, not correctness. A4 is UX, not architecture.

## Open Questions

1. **Where does the chunk-cap value live?** (CONTEXT.md discretion suggests config-driven via `settings.agent_file_chunk_max`.)
   - What we know: 1000 records per CONTEXT.md discretion; `Field(max_length=1000)` on the Pydantic schema is the minimum-fuss approach.
   - What's unclear: Should it be a `Settings` field to allow env override?
   - Recommendation: Add `agent_file_chunk_max: int = 1000` to `Settings`; reference it in the schema as `Field(max_length=settings.agent_file_chunk_max)`. Cheap.

2. **Does the file-upsert response include the upsert counts?** D-19 says "minimal `{"agent_id": "...", "<resource_id_field>": "..."}` confirmation" — for the file-upsert endpoint, what's the resource_id field?
   - What we know: The endpoint is batch, not single-row. A list of IDs would be large.
   - What's unclear: `{"agent_id": ..., "upserted": N}` vs `{"agent_id": ..., "upserted": N, "inserted": M}` vs include the list.
   - Recommendation: `{"agent_id": ..., "upserted": N, "inserted": M, "enqueued": K}` — three integers, useful for agent-side logging, no list payload.

3. **PR scope:** Is Phase 25 expected to land as one PR or multiple?
   - What we know: Phase 24 shipped as one PR with 5 plans.
   - What's unclear: Same shape here?
   - Recommendation: Match Phase 24 — one phase branch (`gsd/phase-25-internal-agent-http-api-bearer-auth`), one PR with as many plans/waves as the planner decides.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | All | ✓ | 3.13 | — |
| PostgreSQL | All endpoints | ✓ (via Docker) | 16+ | — |
| Redis | SAQ enqueue | ✓ (via Docker) | 7+ | — |
| FastAPI | API surface | ✓ | 0.136.1 | — |
| SQLAlchemy | DB | ✓ | 2.0.49 | — |
| SAQ | Task queue | ✓ | 0.26.3 | — |
| Pydantic | Schemas | ✓ | 2.12.5 | — |
| asyncpg | DB driver | ✓ | ≥0.31.0 | — |
| Alembic | Migration 014 | ✓ | ≥1.18.4 | — |
| httpx | Test client | ✓ | ≥0.28.1 | — |
| pytest, pytest-asyncio | Tests | ✓ | (from dev deps) | — |

**Nothing missing, nothing requires fallback.** [VERIFIED: `uv pip show` for each package on 2026-05-11]

## Validation Architecture

> nyquist_validation enabled (.planning/config.json workflow.nyquist_validation: true) — section included.

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 8.x + pytest-asyncio (asyncio_mode = "auto") |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_routers/test_agent_*.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| AUTH-01 (1/4) | Missing Authorization header → 401 with `WWW-Authenticate: Bearer` | unit/integration | `uv run pytest tests/test_routers/test_agent_auth.py::test_missing_header_returns_401 -x` | ❌ Wave 0 |
| AUTH-01 (2/4) | Malformed header ("Token foo") → 401 | unit/integration | `uv run pytest tests/test_routers/test_agent_auth.py::test_malformed_header_returns_401 -x` | ❌ Wave 0 |
| AUTH-01 (3/4) | Valid bearer with unknown hash → 403 | unit/integration | `uv run pytest tests/test_routers/test_agent_auth.py::test_unknown_token_returns_403 -x` | ❌ Wave 0 |
| AUTH-01 (4/4) | agent_id in request body is rejected (422) by `extra="forbid"` | unit/integration | `uv run pytest tests/test_routers/test_agent_files.py::test_agent_id_in_body_rejected -x` | ❌ Wave 0 |
| AUTH-04 (1/2) | Setting `revoked_at = NOW()` mid-test causes next call to return 403 without restart | integration | `uv run pytest tests/test_routers/test_agent_auth.py::test_revoke_blocks_next_call -x` | ❌ Wave 0 |
| AUTH-04 (2/2) | Token rotation: insert new agent row with new token_hash → that agent authenticates | integration | `uv run pytest tests/test_routers/test_agent_auth.py::test_new_token_authenticates -x` | ❌ Wave 0 |
| DIST-04 (1/5) | POST /files round-trips end-to-end with auth + idempotent upsert | integration | `uv run pytest tests/test_routers/test_agent_files.py::test_upsert_happy_path -x` | ❌ Wave 0 |
| DIST-04 (2/5) | PUT /metadata/{file_id} round-trips | integration | `uv run pytest tests/test_routers/test_agent_metadata.py::test_metadata_put_happy_path -x` | ❌ Wave 0 |
| DIST-04 (3/5) | PUT /fingerprints/{file_id}/{engine} round-trips | integration | `uv run pytest tests/test_routers/test_agent_fingerprint.py::test_fingerprint_put_happy_path -x` | ❌ Wave 0 |
| DIST-04 (4/5) | POST + PATCH /execution-log round-trips | integration | `uv run pytest tests/test_routers/test_agent_execution.py::test_execution_log_create_and_patch -x` | ❌ Wave 0 |
| DIST-04 (5/5) | POST /heartbeat returns 204 and persists last_status JSONB | integration | `uv run pytest tests/test_routers/test_agent_heartbeat.py::test_heartbeat_persists_status -x` | ❌ Wave 0 |
| DIST-05 (1/5) | File upsert replay: same chunk twice → one row | integration | `uv run pytest tests/test_routers/test_agent_files.py::test_replay_no_duplicates -x` | ❌ Wave 0 |
| DIST-05 (2/5) | Metadata replay: same payload twice → one row, latest values | integration | `uv run pytest tests/test_routers/test_agent_metadata.py::test_replay_overwrites -x` | ❌ Wave 0 |
| DIST-05 (3/5) | Fingerprint replay: same (file_id, engine) twice → one row | integration | `uv run pytest tests/test_routers/test_agent_fingerprint.py::test_replay_overwrites -x` | ❌ Wave 0 |
| DIST-05 (4/5) | ExecutionLog POST replay: same agent-supplied id twice → one row, no error | integration | `uv run pytest tests/test_routers/test_agent_execution.py::test_create_replay_no_op -x` | ❌ Wave 0 |
| DIST-05 (5/5) | ExecutionLog monotonic PATCH: IN_PROGRESS → PENDING returns 409 | integration | `uv run pytest tests/test_routers/test_agent_execution.py::test_monotonic_regress_returns_409 -x` | ❌ Wave 0 |
| D-20 (1/2) | After POST /files with 2 INSERTed music files, 2 enqueue calls on `phaze-agent-<id>` queue | integration (mocked Queue) | `uv run pytest tests/test_routers/test_agent_files.py::test_auto_enqueue_only_for_inserts -x` | ❌ Wave 0 |
| D-20 (2/2) | After POST /files where all rows are UPDATEs, 0 enqueue calls | integration (mocked Queue) | `uv run pytest tests/test_routers/test_agent_files.py::test_no_enqueue_for_updates -x` | ❌ Wave 0 |
| D-22 | Queue.from_url called with `name=f"phaze-agent-{agent.id}"` exactly | integration (mocked) | covered by `test_auto_enqueue_only_for_inserts` | ❌ Wave 0 |
| D-21 | `RETURNING (xmax = 0)` regression: new key → inserted=True; same key → inserted=False | integration (real Postgres) | `uv run pytest tests/test_services/test_agent_upsert.py::test_xmax_inserted_flag -x` | ❌ Wave 0 |
| D-15 | Terminal state COMPLETED rejects further PATCH with 409 | integration | `uv run pytest tests/test_routers/test_agent_execution.py::test_terminal_state_rejects_patch -x` | ❌ Wave 0 |
| D-16 | Extra body field returns 422 with `extra_forbidden` type | integration | `uv run pytest tests/test_routers/test_agent_files.py::test_extra_body_field_422 -x` | ❌ Wave 0 |
| D-19 (heartbeat) | Heartbeat returns 204 (no body) | integration | covered by `test_heartbeat_persists_status` | ❌ Wave 0 |
| OpenAPI | `/openapi.json` includes `components.securitySchemes.bearerAuth` with `type: http, scheme: bearer` | unit | `uv run pytest tests/test_routers/test_agent_auth.py::test_openapi_bearer_scheme -x` | ❌ Wave 0 |

### Sampling Rate

- **Per task commit:** `uv run pytest tests/test_routers/test_agent_*.py -x` (~12 test files, sub-second per test, should run in <10s)
- **Per wave merge:** `uv run pytest -x` (full suite — current count ~700 tests; phase adds ~50)
- **Phase gate:** Full suite green + `uv run pytest --cov --cov-report=term-missing` passes 85% threshold before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] `tests/test_routers/test_agent_auth.py` — covers AUTH-01, AUTH-04, OpenAPI
- [ ] `tests/test_routers/test_agent_files.py` — covers DIST-04 (1/5), DIST-05 (1/5), D-20, D-22, D-16
- [ ] `tests/test_routers/test_agent_metadata.py` — covers DIST-04 (2/5), DIST-05 (2/5)
- [ ] `tests/test_routers/test_agent_fingerprint.py` — covers DIST-04 (3/5), DIST-05 (3/5)
- [ ] `tests/test_routers/test_agent_execution.py` — covers DIST-04 (4/5), DIST-05 (4/5), DIST-05 (5/5), D-15
- [ ] `tests/test_routers/test_agent_heartbeat.py` — covers DIST-04 (5/5)
- [ ] `tests/test_services/test_agent_upsert.py` — covers D-21 (xmax regression test against real Postgres)
- [ ] `tests/conftest.py` — extend with `seed_test_agent` + `authenticated_client` fixtures (shared)
- [ ] Framework install: none needed — pytest + pytest-asyncio + httpx all present

**Note:** All tests in `tests/test_routers/` are auto-marked `integration` by `pytest_collection_modifyitems` in `tests/conftest.py:22-25` because they use the `client`/`session` fixtures. This is fine — they run against a real Postgres `phaze_test` DB.

## Security Domain

> security_enforcement is enabled (config has no override). Section included.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | `HTTPBearer` + `hashlib.sha256` for token verification. NO password storage — bearer tokens only (CONTEXT.md D-01..D-04). |
| V3 Session Management | no | Stateless bearer auth, no sessions, no cookies |
| V4 Access Control | yes | Per-request DB lookup of `agents.token_hash WHERE revoked_at IS NULL` is the only authorization mechanism; no roles/scopes in v4.0 |
| V5 Input Validation | yes | Pydantic `ConfigDict(extra="forbid")` on every request body; `Field(min_length=, max_length=, ge=, le=)` constraints; server-side NFC normalization of paths |
| V6 Cryptography | yes | `hashlib.sha256` for token hashing — uniform-random 256-bit input, no KDF needed (CONTEXT.md D-04 documents the rationale). NO custom crypto. |
| V7 Error Handling | yes | FastAPI default `{"detail": "..."}` envelope; no stack traces leak (debug=False in prod) |
| V8 Data Protection | yes | Tokens stored as hashes, never plaintext. Token never appears in logs (CONTEXT.md discretion confirms) |
| V9 Communication | partial | HTTPS deferred to Phase 29; Phase 25 ships HTTP-only on private LAN. **This is by design and documented.** |
| V10 Malicious Code | yes (process-isolation) | No code execution from agent payloads; only data writes to known schemas |
| V13 API & Web Service | yes | RFC 6750 compliance via `HTTPBearer` (WWW-Authenticate header on 401); idempotent endpoints with natural keys (CONTEXT.md D-12, D-13) |

### Known Threat Patterns for Phaze v4.0 Agent API

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection via path strings | Tampering | SQLAlchemy parameterized queries throughout — never string interpolation |
| Token enumeration via 401 vs 403 oracle | Information disclosure | 403 indistinguishable for "unknown token" vs "revoked token" (CONTEXT.md D-06) |
| Token replay (intercepted bearer) | Spoofing | Revocation is single SQL UPDATE; HTTPS in Phase 29 prevents network capture |
| Agent impersonation via `agent_id` in body | Spoofing | `extra="forbid"` rejects body `agent_id`; resolver always uses authenticated `agent.id` (AUTH-01) |
| Race condition in upsert | Tampering | Single statement `INSERT ... ON CONFLICT` is atomic; natural-key UNIQUE constraint prevents duplicates |
| Status regression in ExecutionLog | Tampering / Repudiation | Monotonic check in PATCH handler returns 409 (CONTEXT.md D-15) |
| DOS via huge file chunks | DoS | Server-side chunk cap (1000 records, CONTEXT.md discretion); Pydantic `Field(max_length=1000)` |
| Token timing oracle on equality compare | Information disclosure | Postgres-side indexed equality lookup; uniform-random hash space — no timing signal (CONTEXT.md D-04) |
| Log injection via token in logs | Information disclosure | Logging policy: never log token material, only `agent.id` on success (CONTEXT.md discretion) |
| Leaked tokens on disk/in git | Disclosure | Token prefix `phaze_agent_` makes them grep-able / secret-scanner-friendly (CONTEXT.md D-01) |
| Missing CSRF protection | (intentional gap) | Internal LAN-only, bearer auth not cookie auth — CSRF not applicable. Documented in Phase 29 hardening plan. |

## Project Constraints (from CLAUDE.md)

The planner must verify these constraints are honored by every plan and task:

- **Python 3.13 exclusively** — use modern syntax (`str | None`, `list[T]`, match statements where appropriate)
- **uv only** — every command in plans / verification steps MUST be prefixed `uv run`. Never bare `pytest`, `python`, `mypy`, `ruff`
- **Pre-commit must pass** — never `--no-verify` (project memory: feedback_no_verify.md)
- **Line length 150 chars** (ruff config)
- **Mypy strict, double quotes** — type hints on all functions including helpers
- **85% coverage minimum** — new code must keep the project above this floor (current uses `fail_under = 85` in `[tool.coverage.report]`)
- **Frequent commits during phase execution** (project memory: feedback_commit_frequently.md) — not one giant end-of-phase commit
- **Phase 2+ uses worktree branches with PRs** (project memory: feedback_pr_per_phase.md) — Phase 25 lands as `gsd/phase-25-internal-agent-http-api-bearer-auth` → PR
- **Update READMEs alongside code** (project memory: feedback_docs_up_to_date.md) — if any service-level README references the API surface, update it
- **Use SAQ conventions** (project memory: project_arq_to_saq.md) — `Queue.from_url`, `await queue.enqueue("task_name", **kwargs)`
- **Generic server names in design docs** (project memory: feedback_generic_server_names.md) — say "application server" not specific hostnames

### Test markers / config to honor

- `asyncio_mode = "auto"` (pyproject.toml) — `pytest_asyncio.fixture` for fixtures; `@pytest.mark.asyncio` is implicit for async tests but explicit form is also accepted
- Tests using `client`, `session`, `async_engine` fixtures are auto-marked `integration` (conftest.py:22-25) — they require a running Postgres at `postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test`
- Per-file ruff ignores allow `T201` (print) and `PLC`/`S105` in tests — don't suppress them in source

## Sources

### Primary (HIGH confidence)
- [VERIFIED: in-repo code]
  - `src/phaze/services/ingestion.py:91-119` — canonical `pg_insert.on_conflict_do_update` pattern
  - `src/phaze/services/ingestion.py:158-165` — canonical auto-enqueue pattern
  - `src/phaze/main.py:23` — `Queue.from_url(settings.redis_url)` lifespan wiring
  - `src/phaze/models/agent.py` — current Agent model (where `last_status` is added)
  - `src/phaze/models/file.py:49-62` — current FileRecord with composite UQ
  - `src/phaze/models/execution.py` — ExecutionStatus enum + ExecutionLog model
  - `src/phaze/routers/scan.py:46-54` — `request.app.state.queue` access pattern in a handler
  - `src/phaze/database.py:20-23` — `get_session` dep
  - `tests/conftest.py:50-64` — existing `client` + `session` fixtures (extend with `seed_test_agent`)
  - `tests/test_routers/test_scan.py:22-26` — `AsyncMock` patched on `app.state.queue` pattern
  - `alembic/versions/013_*.py` — migration shape for `014_add_last_status_to_agents.py`
  - `.venv/.../saq/queue/redis.py:64-77` — confirms `Queue.from_url(url, name=...)` is the public API
  - `.venv/.../saq/queue/base.py:64-71` — confirms Queue base class signature
- [CITED: Context7 / fastapi.tiangolo.com/reference/security] — HTTPBearer class, auto_error, make_authenticate_headers, custom 403 subclass pattern
- [CITED: Context7 / fastapi.tiangolo.com/tutorial/security/simple-oauth2] — WWW-Authenticate: Bearer per RFC 6750
- [CITED: Context7 / docs.sqlalchemy.org/en/20/dialects/postgresql.html] — `Insert.on_conflict_do_update` signature
- [CITED: Context7 / docs.sqlalchemy.org/en/20/core/sqlelement.html] — `literal_column()` for `.returning()`
- [CITED: pydantic.dev / Context7] — `ConfigDict(extra="forbid")` + `extra_forbidden` error type

### Secondary (MEDIUM confidence)
- [CITED: sigpwned.com/2023/08/10/postgres-upsert-created-or-updated/] — xmax=0 trick explained with caveats. Verified working in Postgres 16 via the existing v3.0 schema patterns (no triggers).
- [CITED: github.com/PostgREST/postgrest/issues/1683] — confirms xmax=0 is the de facto solution across the Postgres community
- [CITED: crunchydata.com/blog/postgres-18-old-and-new-in-the-returning-clause] — Postgres 18 adds cleaner `OLD IS NULL` / `NEW IS NULL` syntax; Phase 25 is on Postgres 16 so not applicable

### Tertiary (LOW confidence)
- None. Every claim in this research has either an in-repo reference or an official-docs citation.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — every library is already in `pyproject.toml` with verified versions; patterns are mirrored from working code
- Architecture: HIGH — diagram and responsibility map composed from CONTEXT.md decisions which are themselves derived from a full-stack review
- Pitfalls: HIGH — pitfalls 1, 3, 4, 5, 6, 7 are concrete and either verified or have explicit test coverage; pitfall 2 (xmax) is well-known across the ecosystem with a clear mitigation
- Validation Architecture: HIGH — every requirement maps to ≥1 named, runnable pytest command; framework already in place

**Research date:** 2026-05-11
**Valid until:** 2026-06-10 (30 days — stable stack, no fast-moving deps; pin the recheck on Pydantic v3 release if/when announced)
