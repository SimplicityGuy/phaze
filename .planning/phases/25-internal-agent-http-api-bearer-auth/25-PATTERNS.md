# Phase 25: Internal Agent HTTP API & Bearer Auth - Pattern Map

**Mapped:** 2026-05-11
**Files analyzed:** 23 (16 new files + 4 modified + 3 test files)
**Analogs found:** 23 / 23

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/routers/agent_auth.py` | helper-module (dep + util) | request-response | `src/phaze/routers/health.py` + `src/phaze/database.py:20-23` | role-match (no existing auth dep) |
| `src/phaze/routers/agent_files.py` | router | CRUD + event-driven (auto-enqueue) | `src/phaze/routers/scan.py:30-54` + `src/phaze/services/ingestion.py:91-119, 158-165` | exact |
| `src/phaze/routers/agent_metadata.py` | router | CRUD (idempotent PUT) | `src/phaze/routers/companion.py:21-30` + `src/phaze/services/ingestion.py:91-119` | role-match |
| `src/phaze/routers/agent_fingerprint.py` | router | CRUD (idempotent PUT) | `src/phaze/routers/companion.py:21-30` + `src/phaze/services/ingestion.py:91-119` | role-match |
| `src/phaze/routers/agent_execution.py` | router | CRUD (POST + PATCH) | `src/phaze/routers/proposals.py:75-122` (multi-verb pattern) + ingestion UPSERT | role-match |
| `src/phaze/routers/agent_heartbeat.py` | router | CRUD (single POST, 204) | `src/phaze/routers/companion.py:21-30` | role-match |
| `src/phaze/schemas/agent_files.py` | schema | validation | `src/phaze/schemas/scan.py` + RESEARCH §Pattern 4 | role-match (no existing `extra="forbid"`) |
| `src/phaze/schemas/agent_metadata.py` | schema | validation | `src/phaze/schemas/scan.py` | role-match |
| `src/phaze/schemas/agent_fingerprint.py` | schema | validation | `src/phaze/schemas/scan.py` | role-match |
| `src/phaze/schemas/agent_execution.py` | schema | validation | `src/phaze/schemas/scan.py` | role-match |
| `src/phaze/schemas/agent_heartbeat.py` | schema | validation | `src/phaze/schemas/scan.py` | role-match |
| `alembic/versions/014_add_last_status_to_agents.py` | migration | DDL | `alembic/versions/012_*.py` (JSONB + partial UQ) + `alembic/versions/013_*.py` (shape) | exact |
| `src/phaze/models/agent.py` (MOD) | model-patch | DDL/ORM | `src/phaze/models/metadata.py:27` (JSONB col) | exact |
| `src/phaze/main.py` (MOD) | app-wiring | startup | `src/phaze/main.py:30-46` (existing include_router block) | exact (self-pattern) |
| `src/phaze/config.py` (MOD) | config | env | `src/phaze/config.py:7-59` (existing Settings) | exact (self-pattern) |
| `tests/conftest.py` (MOD) | fixture | test | `tests/conftest.py:28-64` (existing fixtures) | exact (self-pattern) |
| `tests/test_routers/test_agent_auth.py` | test | unit/integration | `tests/test_routers/test_scan.py:21-34` | role-match |
| `tests/test_routers/test_agent_files.py` | test | integration | `tests/test_routers/test_scan.py:21-34` (AsyncMock on queue) | exact |
| `tests/test_routers/test_agent_metadata.py` | test | integration | `tests/test_routers/test_scan.py:83-107` | role-match |
| `tests/test_routers/test_agent_fingerprint.py` | test | integration | `tests/test_routers/test_scan.py:83-107` | role-match |
| `tests/test_routers/test_agent_execution.py` | test | integration | `tests/test_routers/test_execution.py:22-74` | role-match |
| `tests/test_routers/test_agent_heartbeat.py` | test | integration | `tests/test_routers/test_scan.py:21-34` | role-match |
| `tests/test_services/test_agent_upsert.py` | test | integration (DB) | `tests/test_routers/test_scan.py:83-107` (raw DB asserts) | partial — pure service test |

## Pattern Assignments

### `src/phaze/routers/agent_auth.py` (helper-module, request-response dep)

**Analogs:** `src/phaze/database.py:20-23` (async dep shape) + `src/phaze/routers/health.py:13-17` (`Depends(get_session)` usage). No pre-existing bearer auth in the codebase — this is the *first* security-bearing dep.

**Imports pattern** (compose from `phaze/routers/health.py:1-7` + `phaze/database.py:1-7`):
```python
from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
```

**Core pattern** (verbatim from RESEARCH.md §"Complete auth router module", lines 558-579):
```python
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

**What differs from analogs:**
- No `router = APIRouter(...)` line — this is a helper module, not a router.
- Uses `HTTPBearer` (new to this codebase) instead of plain `Depends(get_session)`.
- Returns an `Agent` ORM object, not a session, so callers get `agent.id` directly.

**Gotchas:** RESEARCH §Pitfall 1 (must keep `auto_error=True` default so `WWW-Authenticate: Bearer` lands on 401). RESEARCH §Pitfall 3 (do NOT use this dep in SSE/streaming handlers like `routers/execution.py:execution_progress`). RESEARCH Anti-pattern (do NOT subclass `HTTPBearer` to flip 401→403 globally; keep 401 for missing/malformed, 403 inside the resolver for unknown/revoked).

---

### `src/phaze/routers/agent_files.py` (router, CRUD + event-driven auto-enqueue)

**Analogs:** `src/phaze/routers/scan.py:24-54` (router shape + `request.app.state.queue` access) + `src/phaze/services/ingestion.py:91-119` (UPSERT idiom) + `src/phaze/services/ingestion.py:158-165` (auto-enqueue idiom).

**Imports pattern** (mirror `routers/scan.py:1-21`):
```python
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from saq import Queue

from phaze.config import settings
from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_files import FileUpsertChunk, FileUpsertResponse

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
```

**Router declaration pattern** (mirror `routers/scan.py:24` — uses `prefix="/api/v1"` style; here uses agent-specific prefix per CONTEXT.md D-10):
```python
router = APIRouter(prefix="/api/internal/agent/files", tags=["agent-internal"])
```

**UPSERT-with-insert-detection pattern** (compose `ingestion.py:91-119` + RESEARCH §Pattern 2 lines 230-267):
```python
# Source: services/ingestion.py:103-117 verbatim, EXTENDED with .returning(...)
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
```

**Auto-enqueue pattern** (verbatim from `ingestion.py:158-165`, with queue-name change per D-22):
```python
# Source: services/ingestion.py:158-165
# DIFFERENCE: queue is per-agent (Queue.from_url with name=) not the default app.state.queue
queue_name = f"phaze-agent-{agent.id}"
queue = Queue.from_url(settings.redis_url, name=queue_name)
try:
    extractable = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})
    for row in rows:
        if not row.inserted:
            continue
        ext = "." + row.file_type.lower()
        if EXTENSION_MAP.get(ext, FileCategory.UNKNOWN) not in extractable:
            continue
        await queue.enqueue("extract_file_metadata", file_id=str(row.id))
finally:
    await queue.disconnect()
```

**Handler signature pattern** (compose `routers/scan.py:30-54` + auth dep):
```python
@router.post("", status_code=status.HTTP_200_OK)
async def upsert_files(
    body: FileUpsertChunk,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FileUpsertResponse:
    """Idempotently upsert a chunk of FileRecord rows for the calling agent."""
    # Stamp agent_id from auth — never trust body (CONTEXT.md AUTH-01)
    records = [{**r.model_dump(), "agent_id": agent.id, "state": FileState.DISCOVERED} for r in body.files]
    # NFC-normalize paths defensively (RESEARCH Pitfall 7)
    for r in records:
        r["original_path"] = unicodedata.normalize("NFC", r["original_path"])
    # [...UPSERT block above...]
    # [...auto-enqueue block above, AFTER commit...]
    return FileUpsertResponse(agent_id=agent.id, upserted=len(rows), inserted=sum(1 for r in rows if r.inserted))
```

**What differs from analogs:**
- `routers/scan.py` uses `request.app.state.queue` (the default queue). This router uses per-agent `Queue.from_url(..., name=f"phaze-agent-{agent.id}")` instead.
- `routers/scan.py` starts a background task. This router does the work inline (chunks are bounded by `agent_file_chunk_max`).
- `routers/scan.py` doesn't require auth. This router gates everything behind `Depends(get_authenticated_agent)`.
- Adds `.returning(..., literal_column("(xmax = 0)"))` — not present in `services/ingestion.py:91-119` (the v3.0 scan didn't need insert-vs-update detection).

**Gotchas:**
- RESEARCH §Pitfall 2 (`xmax = 0`): no triggers on `files` table; assumption documented; D-21 regression test required.
- RESEARCH §Pitfall 4 (same-chunk dupes): server-side dedup `records = list({(r['original_path'],): r for r in records}.values())` before UPSERT.
- RESEARCH §Pitfall 6 (Queue leak): always `try/finally: await queue.disconnect()`.
- RESEARCH §Pitfall 7 (NFC): apply `unicodedata.normalize("NFC", path)` on receive.
- CONTEXT.md discretion: enqueue AFTER `session.commit()`, on enqueue failure log + continue (do NOT raise).

---

### `src/phaze/routers/agent_metadata.py` (router, CRUD idempotent PUT)

**Analogs:** `src/phaze/routers/companion.py:21-30` (simple POST/PUT shape returning Pydantic) + `src/phaze/services/ingestion.py:91-119` (UPSERT idiom on different `index_elements`).

**Imports + router declaration** (mirror `routers/companion.py:1-18`):
```python
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.metadata import FileMetadata
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_metadata import MetadataWriteRequest, MetadataWriteResponse

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/internal/agent/metadata", tags=["agent-internal"])
```

**UPSERT pattern** (compose `ingestion.py:103-117` adapted to `FileMetadata`):
```python
@router.put("/{file_id}", status_code=status.HTTP_200_OK)
async def put_metadata(
    file_id: uuid.UUID,
    body: MetadataWriteRequest,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MetadataWriteResponse:
    """Idempotently replace tag-metadata for a file. Natural key: metadata.file_id (UQ)."""
    payload = {**body.model_dump(), "file_id": file_id}
    stmt = pg_insert(FileMetadata).values([payload])
    stmt = stmt.on_conflict_do_update(
        index_elements=["file_id"],  # UQ on metadata.file_id per models/metadata.py:18
        set_={k: stmt.excluded[k] for k in body.model_dump().keys()},
    )
    await session.execute(stmt)
    await session.commit()
    return MetadataWriteResponse(agent_id=agent.id, file_id=file_id)
```

**What differs from analogs:**
- `routers/companion.py:21-30` is a trigger endpoint (returns count), not a PUT. The shape is the same (FastAPI POST/PUT → service call → response model), but the verb + natural-key UPSERT body is new.
- `services/ingestion.py:91-119` upserts FileRecord on composite key; here upserts FileMetadata on single-column `file_id`.

**Gotchas:**
- D-16: `extra="forbid"` on schema — accidental `agent_id` field returns 422 (RESEARCH §Pitfall 5).
- D-14 last-write-wins: every column in `body.model_dump()` lands in the `set_=` map.
- `file_id` in URL path is the natural key — do NOT also accept it in body.

---

### `src/phaze/routers/agent_fingerprint.py` (router, CRUD idempotent PUT)

**Analogs:** Same as `agent_metadata.py` — `routers/companion.py:21-30` (shape) + `services/ingestion.py:91-119` (UPSERT). Natural key is composite `(file_id, engine)` from `models/fingerprint.py:25` (`ix_fprint_file_engine` unique index).

**UPSERT pattern** (adapted for `(file_id, engine)`):
```python
@router.put("/{file_id}/{engine}", status_code=status.HTTP_200_OK)
async def put_fingerprint(
    file_id: uuid.UUID,
    engine: str,
    body: FingerprintWriteRequest,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FingerprintWriteResponse:
    """Idempotently replace fingerprint result. Natural key: (file_id, engine)."""
    payload = {**body.model_dump(), "file_id": file_id, "engine": engine}
    stmt = pg_insert(FingerprintResult).values([payload])
    stmt = stmt.on_conflict_do_update(
        index_elements=["file_id", "engine"],  # ix_fprint_file_engine unique index per models/fingerprint.py:25
        set_={"status": stmt.excluded.status, "error_message": stmt.excluded.error_message},
    )
    await session.execute(stmt)
    await session.commit()
    return FingerprintWriteResponse(agent_id=agent.id, file_id=file_id, engine=engine)
```

**What differs:** Composite natural key in URL path (`/{file_id}/{engine}`) instead of single key. Otherwise identical to `agent_metadata.py`.

---

### `src/phaze/routers/agent_execution.py` (router, multi-verb CRUD with monotonic check)

**Analogs:** `src/phaze/routers/proposals.py:75-122` (POST + PATCH on same resource, separate handlers) + `services/ingestion.py:91-119` (UPSERT for POST with `DO NOTHING`) + `models/execution.py:14-21` (ExecutionStatus enum).

**POST handler with `ON CONFLICT DO NOTHING`** (D-13: agent-supplied id, first-create wins):
```python
@router.post("", status_code=status.HTTP_200_OK)
async def create_execution_log(
    body: ExecutionLogCreate,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExecutionLogCreateResponse:
    """Idempotently create an ExecutionLog row. Agent supplies id; replay POST = no-op."""
    payload = body.model_dump()
    stmt = pg_insert(ExecutionLog).values([payload]).on_conflict_do_nothing(index_elements=["id"])
    await session.execute(stmt)
    await session.commit()
    return ExecutionLogCreateResponse(agent_id=agent.id, execution_log_id=body.id)
```

**PATCH handler with monotonic check** (verbatim from RESEARCH.md §Pattern 5 lines 376-405):
```python
# Source: RESEARCH.md §Pattern 5 — application-level invariant; no library needed
_STATUS_ORDER = {
    ExecutionStatus.PENDING: 0,
    ExecutionStatus.IN_PROGRESS: 1,
    ExecutionStatus.COMPLETED: 2,
    ExecutionStatus.FAILED: 3,
}
_TERMINAL = {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED}


@router.patch("/{execution_log_id}", status_code=status.HTTP_200_OK)
async def patch_execution_log(
    execution_log_id: uuid.UUID,
    body: ExecutionLogPatch,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ExecutionLogPatchResponse:
    """Update an ExecutionLog row. Status transitions are monotonic (CONTEXT.md D-15)."""
    existing = await session.get(ExecutionLog, execution_log_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="execution-log not found")
    cur = ExecutionStatus(existing.status)
    new = ExecutionStatus(body.status)
    if cur in _TERMINAL:
        raise HTTPException(status_code=409, detail="execution-log status is terminal")
    if _STATUS_ORDER[new] < _STATUS_ORDER[cur]:
        raise HTTPException(status_code=409, detail="execution-log status would regress")
    # Apply mutations
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(existing, field, value)
    await session.commit()
    return ExecutionLogPatchResponse(agent_id=agent.id, execution_log_id=execution_log_id)
```

**What differs from analogs:**
- `routers/proposals.py:75-122` uses HTML responses + template rendering. This router returns plain JSON via Pydantic response models.
- `routers/proposals.py:75-122` is fire-and-forget (PATCH always succeeds). This router has a monotonic-state guard returning 409.
- `services/ingestion.py:91-119` uses `on_conflict_do_update`. This router's POST uses `on_conflict_do_nothing` (first-create wins per D-13).

**Gotchas:**
- D-15 monotonic: same-status PATCH (e.g., IN_PROGRESS → IN_PROGRESS) is allowed (`_STATUS_ORDER[new] < _STATUS_ORDER[cur]` uses `<`, not `<=`).
- D-13: agent supplies the id in the POST body; do NOT generate server-side. Schema validates as `uuid.UUID`.
- Terminal-state mutation returns 409 BEFORE the regress check (early exit).

---

### `src/phaze/routers/agent_heartbeat.py` (router, single POST, 204)

**Analogs:** `src/phaze/routers/companion.py:21-30` (simple POST returning small Pydantic) + RESEARCH.md §"Heartbeat router" example lines 583-622.

**Full handler** (verbatim from RESEARCH.md lines 602-621):
```python
from sqlalchemy import update
from sqlalchemy.sql import func

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

**What differs:** Returns `Response(status_code=204)` (no body) — different from every other Phase 25 router which returns JSON Pydantic. Uses `update(Agent).values(...)` instead of `pg_insert.on_conflict_do_update(...)` because there's no conflict (single Agent row exists, just updating columns).

**Gotchas:** `body.model_dump()` serializes to JSONB cleanly (all 3 fields are JSON-native types per D-17). `last_status` column is added by migration 014 — do NOT write code referencing `Agent.last_status` until migration is in place.

---

### Schema files (`src/phaze/schemas/agent_*.py`)

**Analog:** `src/phaze/schemas/scan.py` (existing Pydantic v2 BaseModel pattern). **Difference:** every Phase 25 request schema sets `model_config = ConfigDict(extra="forbid")` per D-16 — `scan.py` does NOT.

**Excerpt of existing pattern** (`schemas/scan.py:1-15`):
```python
from datetime import datetime
import uuid

from pydantic import BaseModel


class ScanRequest(BaseModel):
    """Request body for triggering a file scan."""
    path: str | None = None


class ScanResponse(BaseModel):
    """Response returned after starting a scan."""
    batch_id: uuid.UUID
    message: str
```

**Target pattern** (verbatim from RESEARCH.md §Pattern 4 lines 336-356):
```python
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
    files: list[FileUpsertRecord] = Field(min_length=1, max_length=1000)


class FileUpsertResponse(BaseModel):
    agent_id: str
    upserted: int
    inserted: int
```

**What differs:** Every request schema (and every nested item schema) sets `ConfigDict(extra="forbid")`. Response schemas do NOT — they're not validated on serialization, so adding fields later is backward-compatible. **Source for response shape:** RESEARCH §"Open Questions 2" recommends `{"agent_id", "upserted": N, "inserted": M, "enqueued": K}` for `agent_files`.

**Gotchas:**
- RESEARCH §Pitfall 5: nested item schemas need `extra="forbid"` too (`ConfigDict` is per-class, not inherited).
- Schemas explicitly omit `agent_id` field (AUTH-01). Including one would let a bug let an attacker forge attribution.
- For `agent_execution.py` `ExecutionLogCreate` schema, `id: uuid.UUID` IS in the body (D-13: agent-generated).
- For `agent_heartbeat.py`, body shape is locked: `{"agent_version": str, "worker_pid": int, "queue_depth": int}` (D-17).

---

### `alembic/versions/014_add_last_status_to_agents.py` (migration, DDL)

**Analog:** `alembic/versions/012_add_agents_table_and_backfill.py:104-110` (partial UQ pattern) + `alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py:1-19` (migration header shape).

**Header pattern** (verbatim from `013_*.py:1-19`):
```python
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
```

**JSONB column pattern** (compose from `012_*.py:38` adding `scan_roots JSONB`):
```python
# Source: alembic/versions/012_*.py:38 — scan_roots JSONB column pattern
op.add_column("agents", sa.Column("last_status", postgresql.JSONB, nullable=True))
```

**Partial index pattern** (verbatim from `012_*.py:104-110`):
```python
# Source: alembic/versions/012_*.py:104-110 — partial UQ on scan_batches.status='live'
# DIFFERENCE: unique=False (this is a lookup index, not uniqueness) and WHERE clause differs
op.create_index(
    "ix_agents_token_hash_active",  # naming: ix_ prefix per base.py:9 convention dict
    "agents",
    ["token_hash"],
    unique=False,
    postgresql_where=sa.text("revoked_at IS NULL"),
)
```

**Downgrade pattern** (mirror `013_*.py:40-60` shape but no guards needed — no data risk):
```python
def downgrade() -> None:
    """Drop partial index and last_status column."""
    op.drop_index("ix_agents_token_hash_active", table_name="agents")
    op.drop_column("agents", "last_status")
```

**What differs from analogs:**
- `012_*.py` partial index is UNIQUE (`unique=True`). This index is non-unique — it's a lookup index for the auth dep's SELECT.
- `013_*.py` has a duplicate-detection guard in `downgrade()`. `014` downgrade has no guard — dropping `last_status` and the index loses metadata but no user data.
- No data backfill needed — legacy agent never heartbeats (per CONTEXT.md D-07).

**Gotchas:**
- Index name `ix_agents_token_hash_active` follows `base.py:9` convention dict `ix` prefix. Do NOT name it `idx_*` or use any custom suffix that conflicts with autogenerated names.
- `postgresql_where=sa.text("revoked_at IS NULL")` — the partial predicate must EXACTLY match the query in `get_authenticated_agent`'s `select(Agent).where(..., Agent.revoked_at.is_(None))` for Postgres to use the index. NULL vs IS NULL: SQLAlchemy renders `.is_(None)` as `IS NULL`, matching `sa.text("revoked_at IS NULL")`. Verified consistent.

---

### `src/phaze/models/agent.py` (MODIFIED — add `last_status` JSONB column)

**Analog:** `src/phaze/models/metadata.py:27` (`raw_tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)`) — same exact pattern.

**Excerpt to add** (insert into `models/agent.py` after line 30):
```python
# Source: models/metadata.py:27 — raw_tags JSONB pattern, verbatim
last_status: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

**Surrounding context** (existing `models/agent.py:27-30`):
```python
token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
scan_roots: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
# ADD: last_status: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

**What differs from analog:** `models/metadata.py:27` uses `raw_tags` name. Otherwise pattern is byte-identical. `JSONB` is already imported on line 8 of `models/agent.py`.

**Gotchas:** Add the model field at the same time as the migration. Mypy-strict catches mismatches between model and migration if you ship one without the other.

---

### `src/phaze/main.py` (MODIFIED — register routers + bearerAuth scheme)

**Analog:** `src/phaze/main.py:14, 30-46` (existing import + `include_router` block — *self-pattern*).

**Existing import block** (`main.py:14`):
```python
from phaze.routers import companion, cue, duplicates, execution, health, pipeline, preview, proposals, scan, search, tags, tracklists
```

**Extended import block** (add agent_* modules):
```python
from phaze.routers import (
    agent_execution,
    agent_files,
    agent_fingerprint,
    agent_heartbeat,
    agent_metadata,
    companion,
    cue,
    duplicates,
    execution,
    health,
    pipeline,
    preview,
    proposals,
    scan,
    search,
    tags,
    tracklists,
)
```

**Existing include_router block** (`main.py:33-44`):
```python
app.include_router(health.router)
app.include_router(scan.router)
app.include_router(companion.router)
# [...]
```

**Add 5 new lines** (per CONTEXT.md D-10):
```python
app.include_router(agent_files.router)
app.include_router(agent_metadata.router)
app.include_router(agent_fingerprint.router)
app.include_router(agent_execution.router)
app.include_router(agent_heartbeat.router)
```

**What differs:** None — pure extension of the existing self-pattern. Note: `agent_auth` is NOT imported here — it's a helper module, not a router (D-05, D-09).

**Gotchas:** Alphabetical import order matters for ruff (`I` rules enabled). Insert agent_* names in alphabetical position (they sort BEFORE `companion`). The OpenAPI `bearerAuth` scheme is registered automatically via the `HTTPBearer(scheme_name="bearerAuth")` instance in `agent_auth.py` — no extra `app.openapi_extra` or manual override needed (RESEARCH §Pattern 6 line 426).

---

### `src/phaze/config.py` (MODIFIED — optional new settings)

**Analog:** `src/phaze/config.py:7-59` (existing `Settings` class — *self-pattern*).

**Existing field pattern** (`config.py:24-25`):
```python
# File discovery
scan_path: str = "/data/music"
```

**New fields to add** (per CONTEXT.md "Claude's Discretion" + RESEARCH §Open Question 1):
```python
# Internal agent API (Phase 25)
agent_token_prefix: str = "phaze_agent_"
agent_file_chunk_max: int = 1000
```

**What differs:** Pure addition; no structural change. Defaults match RESEARCH recommendations.

**Gotchas:** Field defaults are part of the public API — changing them is a versioning event. The token prefix change in particular invalidates all existing tokens (because the prefix is part of the hashed input per D-02).

---

### `tests/conftest.py` (MODIFIED — add `seed_test_agent` + `authenticated_client` fixtures)

**Analog:** `tests/conftest.py:28-64` (existing `async_engine`, `session`, `client` fixtures — *self-pattern*).

**Existing `client` fixture** (`conftest.py:58-64`):
```python
@pytest_asyncio.fixture
async def client(session) -> AsyncGenerator[AsyncClient]:  # type: ignore[no-untyped-def]
    """Yield an async HTTP test client with database session override."""
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
```

**New fixtures to add** (verbatim from RESEARCH.md §"Authenticated test client fixture" lines 680-705):
```python
import hashlib
import secrets


@pytest_asyncio.fixture
async def seed_test_agent(session):  # type: ignore[no-untyped-def]
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
async def authenticated_client(session, seed_test_agent) -> AsyncGenerator[AsyncClient]:  # type: ignore[no-untyped-def]
    """AsyncClient with Authorization: Bearer <known token> pre-set."""
    _agent, raw_token = seed_test_agent
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        yield ac
```

**Also extend `DB_FIXTURES` set** (`conftest.py:18`):
```python
DB_FIXTURES = {"async_engine", "session", "client", "authenticated_client", "seed_test_agent"}
```

**What differs:** Adds `agent_id` (kebab-case slug compliant with `^[a-z0-9]+(-[a-z0-9]+)*$` per `models/agent.py:34`), pre-sets `Authorization` header via `AsyncClient(headers=...)`. Uses `secrets.token_urlsafe(32)` per CONTEXT.md "Claude's Discretion".

**Gotchas:**
- `test-agent-01` MUST match the agent slug regex (lowercase + hyphens + digits only). `test_agent_01` (underscore) would violate `ck_agents_id_charset`.
- Use `secrets`, NOT `random` (CONTEXT.md "Claude's Discretion").
- `seed_test_agent` returns BOTH the agent (for `agent.id` in assertions) AND the raw_token (so other tests can construct unauthenticated requests if needed).
- Extending `DB_FIXTURES` ensures new tests are auto-marked `integration` per `conftest.py:21-25`.

---

### `tests/test_routers/test_agent_auth.py` (test, auth coverage)

**Analog:** `tests/test_routers/test_scan.py:21-34` (basic AsyncClient test shape).

**Test cases mapped from RESEARCH.md §Phase Requirements → Test Map** (lines 868-892):
| Test name | Requirement | Source pattern |
|-----------|-------------|----------------|
| `test_missing_header_returns_401` | AUTH-01 (1/4) | RESEARCH §Pitfall 1 line 460 |
| `test_malformed_header_returns_401` | AUTH-01 (2/4) | RESEARCH §Phase Reqs |
| `test_unknown_token_returns_403` | AUTH-01 (3/4) | RESEARCH §Phase Reqs |
| `test_revoke_blocks_next_call` | AUTH-04 (1/2) | RESEARCH §"Revocation mid-test" lines 766-789 |
| `test_new_token_authenticates` | AUTH-04 (2/2) | RESEARCH §Phase Reqs |
| `test_openapi_bearer_scheme` | OpenAPI | RESEARCH §Pattern 6 |

**401 test pattern** (compose from RESEARCH §Pitfall 1):
```python
@pytest.mark.asyncio
async def test_missing_header_returns_401(client: AsyncClient) -> None:
    response = await client.post("/api/internal/agent/heartbeat", json={"agent_version": "4.0.0", "worker_pid": 1, "queue_depth": 0})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
```

**Revocation test pattern** (verbatim RESEARCH lines 766-789):
```python
# Source: RESEARCH.md §"Revocation mid-test (AUTH-04)" lines 766-789
@pytest.mark.asyncio
async def test_revoke_blocks_next_call_without_restart(authenticated_client, seed_test_agent, session) -> None:
    agent, _raw = seed_test_agent
    r1 = await authenticated_client.post("/api/internal/agent/heartbeat", json={...})
    assert r1.status_code == 204
    await session.execute(update(Agent).where(Agent.id == agent.id).values(revoked_at=sa_func.now()))
    await session.commit()
    r2 = await authenticated_client.post("/api/internal/agent/heartbeat", json={...})
    assert r2.status_code == 403
```

**Gotchas:** Use the unauthenticated `client` fixture for 401 tests (no Authorization header pre-set). Use `authenticated_client` for revocation tests so the first request succeeds. Per `conftest.py:18-25`, both fixtures auto-mark tests as `integration`.

---

### `tests/test_routers/test_agent_files.py` (test, upsert + auto-enqueue assertions)

**Analog:** `tests/test_routers/test_scan.py:21-34` (AsyncMock on Queue) + RESEARCH.md §"Asserting SAQ enqueue in tests" lines 709-741.

**AsyncMock pattern** (verbatim from RESEARCH lines 711-740):
```python
# Source: RESEARCH.md §"Asserting SAQ enqueue in tests" lines 709-741
# DIFFERENCE from test_scan.py:26: patches Queue class import in agent_files module
#   (NOT app.state.queue, because this router constructs its OWN Queue per call)

from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_upsert_enqueues_extract_for_new_music_files(authenticated_client, seed_test_agent, session) -> None:
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
    MockQueue.from_url.assert_called_once_with(
        "redis://redis:6379/0",
        name=f"phaze-agent-{agent.id}",
    )
    mock_queue.enqueue.assert_awaited_once()
    args, kwargs = mock_queue.enqueue.call_args
    assert args[0] == "extract_file_metadata"
    assert "file_id" in kwargs
    mock_queue.disconnect.assert_awaited_once()
```

**What differs from `test_scan.py:21-34`:**
- `test_scan.py` patches `client._transport.app.state.queue = AsyncMock()` (the existing app-state queue).
- This test patches `phaze.routers.agent_files.Queue` (the Queue *class* the router imports) and asserts `.from_url(...)` is called with the per-agent `name=` kwarg.

**Replay-idempotency test** (verbatim from RESEARCH lines 745-762).

**Gotchas:**
- The `with patch(...)` block must enclose BOTH requests in the replay test so the same mock catches both calls.
- D-22 assertion is captured by `MockQueue.from_url.assert_called_once_with(..., name=f"phaze-agent-{agent.id}")`.
- D-20 (no-enqueue-for-updates) test: send the same chunk twice; on the second request, `mock_queue.enqueue` should NOT have been called (assert call count == call count from first request).
- D-16 (extra body field 422) test: send `{"files": [{..., "agent_id": "evil"}]}`; assert 422 + `loc == ["body", "files", 0, "agent_id"]` per RESEARCH §Pitfall 5 line 496.

---

### `tests/test_routers/test_agent_metadata.py`, `test_agent_fingerprint.py`, `test_agent_heartbeat.py` (tests, idempotent PUT/POST)

**Analog:** `tests/test_routers/test_scan.py:83-107` (DB sanity check after API call).

**Replay-idempotency test pattern** (compose from `test_scan.py:83-107` + RESEARCH §"Replay-idempotency test"):
```python
@pytest.mark.asyncio
async def test_metadata_replay_overwrites(authenticated_client, seed_test_agent, session) -> None:
    # 1. Seed a file
    file_id = uuid.uuid4()
    session.add(FileRecord(id=file_id, agent_id="test-agent-01", ...))
    await session.commit()

    # 2. PUT metadata twice with different payloads (last-write-wins per D-14)
    r1 = await authenticated_client.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A"})
    r2 = await authenticated_client.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "B"})
    assert r1.status_code == 200
    assert r2.status_code == 200

    # 3. One row in DB, latest values
    result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].artist == "B"
```

**Heartbeat 204 + JSONB persistence test** (compose from RESEARCH §Heartbeat router + DB sanity assertion):
```python
@pytest.mark.asyncio
async def test_heartbeat_persists_status(authenticated_client, seed_test_agent, session) -> None:
    agent, _ = seed_test_agent
    payload = {"agent_version": "4.0.0", "worker_pid": 1234, "queue_depth": 5}
    response = await authenticated_client.post("/api/internal/agent/heartbeat", json=payload)
    assert response.status_code == 204
    assert response.content == b""  # no body
    await session.refresh(agent)
    assert agent.last_status == payload
    assert agent.last_seen_at is not None
```

**What differs:** Heartbeat test asserts `response.content == b""` (no body) — unique to this endpoint per D-19. Metadata/fingerprint tests assert single-row + last-write-wins.

---

### `tests/test_routers/test_agent_execution.py` (test, multi-verb + monotonic 409)

**Analog:** `tests/test_routers/test_execution.py:22-74` (ExecutionLog setup helper) + general PATCH pattern from `tests/test_routers/test_proposals.py`.

**ExecutionLog setup helper** (mirror `test_execution.py:22-74` but simplified — no proposal/file prerequisites for the agent endpoint since agent supplies its own `id`):

**Monotonic regression test** (D-15):
```python
@pytest.mark.asyncio
async def test_monotonic_regress_returns_409(authenticated_client, seed_test_agent, session) -> None:
    log_id = uuid.uuid4()
    # POST in_progress
    await authenticated_client.post("/api/internal/agent/execution-log", json={"id": str(log_id), "status": "in_progress", ...})
    # PATCH back to pending
    response = await authenticated_client.patch(f"/api/internal/agent/execution-log/{log_id}", json={"status": "pending"})
    assert response.status_code == 409
    assert "regress" in response.json()["detail"]


@pytest.mark.asyncio
async def test_terminal_state_rejects_patch(authenticated_client, seed_test_agent, session) -> None:
    log_id = uuid.uuid4()
    # POST + PATCH to completed
    await authenticated_client.post("/api/internal/agent/execution-log", json={"id": str(log_id), "status": "completed", ...})
    # PATCH back to anything
    response = await authenticated_client.patch(f"/api/internal/agent/execution-log/{log_id}", json={"status": "in_progress"})
    assert response.status_code == 409
    assert "terminal" in response.json()["detail"]
```

**Replay-no-op test** (D-13):
```python
@pytest.mark.asyncio
async def test_create_replay_no_op(authenticated_client, seed_test_agent, session) -> None:
    log_id = uuid.uuid4()
    payload = {"id": str(log_id), "status": "pending", ...}
    r1 = await authenticated_client.post("/api/internal/agent/execution-log", json=payload)
    r2 = await authenticated_client.post("/api/internal/agent/execution-log", json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200
    result = await session.execute(select(func.count()).select_from(ExecutionLog).where(ExecutionLog.id == log_id))
    assert result.scalar_one() == 1
```

**Gotchas:**
- ExecutionLog FK to proposals — the agent endpoint must accept a proposal_id; either pre-seed a proposal in the test fixture or allow `proposal_id` to be nullable (current model has it NOT NULL — Phase 25 should either accept the constraint and pre-seed, or document an MOR if the schema needs adjustment).
- D-13 test uses the SAME `log_id` for both POSTs; the second is a guaranteed no-op via `ON CONFLICT (id) DO NOTHING`.

---

### `tests/test_services/test_agent_upsert.py` (test, xmax regression)

**Analog:** `tests/test_routers/test_scan.py:83-107` (raw DB assertion pattern). This is NOT a router test — it's a pure DB-level test against a real Postgres test database to catch RESEARCH §Pitfall 2.

**Test pattern** (composed from RESEARCH §Pitfall 2 line 469 + xmax-regression test requirements):
```python
@pytest.mark.asyncio
async def test_xmax_inserted_flag(session: AsyncSession, seed_test_agent) -> None:
    """Regression test for RESEARCH Pitfall 2: xmax=0 must remain True for fresh INSERTs."""
    from sqlalchemy import literal_column
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from phaze.models.file import FileRecord

    agent, _ = seed_test_agent
    record = {
        "id": uuid.uuid4(),
        "agent_id": agent.id,
        "sha256_hash": "0" * 64,
        "original_path": "/test/music/x.mp3",
        "original_filename": "x.mp3",
        "current_path": "/test/music/x.mp3",
        "file_type": "mp3",
        "file_size": 100,
        "state": "discovered",
    }

    # First UPSERT: brand-new key → inserted=True
    stmt = pg_insert(FileRecord).values([record]).on_conflict_do_update(
        index_elements=["agent_id", "original_path"],
        set_={"file_size": pg_insert(FileRecord).excluded.file_size},
    ).returning(FileRecord.id, literal_column("(xmax = 0)").label("inserted"))
    result = await session.execute(stmt)
    rows = result.all()
    await session.commit()
    assert len(rows) == 1
    assert rows[0].inserted is True  # MUST be True for fresh INSERT

    # Second UPSERT: same key → inserted=False
    record["id"] = uuid.uuid4()  # new uuid, but conflicts on (agent_id, original_path)
    record["file_size"] = 200
    stmt = pg_insert(FileRecord).values([record]).on_conflict_do_update(
        index_elements=["agent_id", "original_path"],
        set_={"file_size": pg_insert(FileRecord).excluded.file_size},
    ).returning(FileRecord.id, literal_column("(xmax = 0)").label("inserted"))
    result = await session.execute(stmt)
    rows = result.all()
    await session.commit()
    assert len(rows) == 1
    assert rows[0].inserted is False  # MUST be False for UPDATE
```

**Gotchas:**
- Requires real Postgres — `session` fixture provides this. Auto-marked `integration`.
- Documents the assumption from RESEARCH §Assumption A1; will fire if a future migration adds a trigger to `files`.
- Lives in `tests/test_services/`, not `tests/test_routers/`, because it tests SQL behaviour at the service-layer abstraction.

---

## Shared Patterns

### Pattern A: Authentication dependency (D-05, D-09)
**Source:** `src/phaze/routers/agent_auth.py` (new in this phase)
**Apply to:** Every `/api/internal/agent/*` router handler.

```python
agent: Annotated[Agent, Depends(get_authenticated_agent)],
```

Every handler in `agent_files.py`, `agent_metadata.py`, `agent_fingerprint.py`, `agent_execution.py`, `agent_heartbeat.py` includes this exact line. The Agent yielded is the ONLY source of `agent_id` (CONTEXT.md AUTH-01 — never trust the body).

---

### Pattern B: `pg_insert(...).on_conflict_do_update(...)` UPSERT
**Source:** `src/phaze/services/ingestion.py:91-119`
**Apply to:** `agent_files.py`, `agent_metadata.py`, `agent_fingerprint.py` (all idempotent writes via composite/single natural keys).

```python
stmt = pg_insert(Model).values(records)
stmt = stmt.on_conflict_do_update(
    index_elements=[...natural-key columns...],
    set_={
        "<mutable_col>": stmt.excluded.<mutable_col>,
        # ...
    },
)
await session.execute(stmt)
await session.commit()
```

**Natural-key matrix per CONTEXT.md D-12:**
| Endpoint | `index_elements` | Source |
|----------|------------------|--------|
| `agent_files.py` | `["agent_id", "original_path"]` | `models/file.py:61` (composite UQ) |
| `agent_metadata.py` | `["file_id"]` | `models/metadata.py:18` (`unique=True`) |
| `agent_fingerprint.py` | `["file_id", "engine"]` | `models/fingerprint.py:25` (`ix_fprint_file_engine` unique index) |
| `agent_execution.py` (POST) | `["id"]` + `do_nothing` | `models/execution.py:28` (PK; D-13 first-create-wins) |

---

### Pattern C: SAQ enqueue (default queue vs per-agent queue)
**Source:** `src/phaze/services/ingestion.py:158-165` (default queue) + RESEARCH §Pattern 3 (per-agent queue)
**Apply to:** `agent_files.py` ONLY (no other Phase 25 endpoint auto-enqueues).

| Pattern | Code | Used for |
|---------|------|----------|
| Default queue (existing) | `queue = http_request.app.state.queue; await queue.enqueue("task_name", **kwargs)` | `routers/scan.py:49-50`, `routers/execution.py:46-48`, `routers/pipeline.py:64,68` |
| **Per-agent queue (NEW for Phase 25)** | `Queue.from_url(settings.redis_url, name=f"phaze-agent-{agent.id}")` with `try/finally: await queue.disconnect()` | `agent_files.py` ONLY |

---

### Pattern D: Strict Pydantic request schemas (D-16)
**Source:** RESEARCH §Pattern 4 (codebase has NO existing `extra="forbid"` schemas — Phase 25 is greenfield for this)
**Apply to:** Every request schema in `src/phaze/schemas/agent_*.py`, including nested item schemas.

```python
from pydantic import BaseModel, ConfigDict, Field

class WhateverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # MUST be on every request schema + every nested item
    # NO agent_id field — comes from auth dep (AUTH-01)
    ...
```

Response schemas do NOT need `extra="forbid"` (forward-compat for adding response fields).

---

### Pattern E: Naming-convention compliance for migration 014
**Source:** `src/phaze/models/base.py:9-15` (project-wide naming convention dict)
**Apply to:** Every index/constraint name in migration 014.

```python
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

Index name `ix_agents_token_hash_active` honors the `ix_` prefix. The `_active` suffix is operator-readable (matches `postgresql_where="revoked_at IS NULL"` — only "active" tokens are indexed).

---

### Pattern F: Test fixture extension for authenticated client
**Source:** `tests/conftest.py:50-64` (existing `session` + `client` fixtures)
**Apply to:** `tests/conftest.py` (modified) — adds `seed_test_agent` + `authenticated_client` fixtures used by every `tests/test_routers/test_agent_*.py` file.

Extends `DB_FIXTURES = {"async_engine", "session", "client"}` to include `"authenticated_client"` and `"seed_test_agent"` so the auto-marker at lines 21-25 catches new tests.

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| (none) | — | — | All 23 files have at least a role-match analog in the existing codebase. The only "no exact analog" cases are `agent_auth.py` (first bearer-auth dep in the codebase — but `get_session` provides the dep shape pattern) and `schemas/agent_*.py` (first `extra="forbid"` schemas — but `schemas/scan.py` provides the BaseModel shape, and RESEARCH §Pattern 4 provides the strict-mode pattern). |

---

## Metadata

**Analog search scope:**
- `src/phaze/routers/` (13 existing routers scanned: companion, cue, duplicates, execution, health, pipeline, preview, proposals, scan, search, tags, tracklists)
- `src/phaze/services/ingestion.py` (UPSERT idiom + auto-enqueue pattern at lines 91-119, 158-165)
- `src/phaze/models/` (agent.py, file.py, scan_batch.py, metadata.py, fingerprint.py, execution.py, base.py)
- `src/phaze/schemas/` (companion.py, scan.py — existing schema shapes)
- `alembic/versions/012_*.py`, `alembic/versions/013_*.py` (migration shape + JSONB column + partial index patterns)
- `tests/conftest.py` (fixture shapes)
- `tests/test_routers/test_scan.py`, `test_execution.py` (test patterns + AsyncMock-on-Queue)
- `src/phaze/main.py`, `src/phaze/database.py`, `src/phaze/config.py` (app wiring + dep + settings shapes)

**Files scanned:** ~30 source files + 2 migrations + 4 test files.

**Pattern extraction date:** 2026-05-11
