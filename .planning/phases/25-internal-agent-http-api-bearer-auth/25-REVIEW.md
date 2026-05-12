---
phase: 25-internal-agent-http-api-bearer-auth
reviewed: 2026-05-11T00:00:00Z
depth: standard
files_reviewed: 24
files_reviewed_list:
  - alembic/versions/014_add_last_status_to_agents.py
  - src/phaze/config.py
  - src/phaze/main.py
  - src/phaze/models/agent.py
  - src/phaze/routers/agent_auth.py
  - src/phaze/routers/agent_execution.py
  - src/phaze/routers/agent_files.py
  - src/phaze/routers/agent_fingerprint.py
  - src/phaze/routers/agent_heartbeat.py
  - src/phaze/routers/agent_metadata.py
  - src/phaze/schemas/agent_execution.py
  - src/phaze/schemas/agent_files.py
  - src/phaze/schemas/agent_fingerprint.py
  - src/phaze/schemas/agent_heartbeat.py
  - src/phaze/schemas/agent_metadata.py
  - tests/conftest.py
  - tests/test_migrations/test_012_upgrade.py
  - tests/test_routers/test_agent_auth.py
  - tests/test_routers/test_agent_execution.py
  - tests/test_routers/test_agent_files.py
  - tests/test_routers/test_agent_fingerprint.py
  - tests/test_routers/test_agent_heartbeat.py
  - tests/test_routers/test_agent_metadata.py
  - tests/test_services/test_agent_upsert.py
findings:
  critical: 2
  warning: 8
  info: 4
  total: 14
status: issues_found
---

# Phase 25: Code Review Report

**Reviewed:** 2026-05-11
**Depth:** standard
**Files Reviewed:** 24
**Status:** issues_found

## Summary

Phase 25 ships the internal-agent HTTP API and SHA256-bearer auth. Overall the implementation is careful, with strong documentation of design decisions (D-numbers referenced inline), exact `extra="forbid"` Pydantic guards, an indexed-equality auth path with a partial index, and clear post-commit enqueue semantics. The auth dependency is functionally correct (no SQL injection, no `==` on `Authorization` header, no in-memory cache that would defeat revocation).

Two BLOCKER findings stand out:

1. The `PUT /api/internal/agent/metadata/{file_id}` handler does a **full-row UPDATE** on every replay because `body.model_dump()` is called without `exclude_unset=True`, so unset Optional fields (default `None`) are written to the SET clause and clobber existing columns. The docstring claims this is intentional ("last-write-wins"), but the Pydantic schema makes every field `Optional[...] = None`, so any partial PUT (e.g., `{"artist": "X"}`) will null out title/album/year/etc. on the existing row. No test covers partial-PUT-after-full-PUT, so the regression is invisible. This contradicts the natural read of `D-14 last-write-wins` for a row where only some fields are sent.

2. Idempotent PATCH against a **terminal** ExecutionLog row returns 409 instead of 200, breaking the documented idempotent-retry contract. Specifically: `PATCH /execution-log/<row in COMPLETED>` with `{"status": "completed"}` hits the terminal-state guard (line 111) **before** the same-status allowance (line 115) ever runs, so a same-status retry against a terminal row errors with `"execution-log status is terminal"`. The agent's idempotent retry path described in D-13/D-15 is therefore not idempotent at the terminal boundary — the most likely retry case (network glitch right after COMPLETED was written).

The remaining warnings cover: cross-tenant write authority (explicitly accepted as deferred — T-25-04-T — but still worth visible audit), inconsistent NFC normalization in `agent_files.py`, lack of metadata-table schema validation on `file_type` extensions, missing test coverage for the partial-index path (tests use `create_all`, not migrations), and a handful of input-bound / extension-shape quality issues.

## Critical Issues

### CR-01: `agent_metadata.py` partial-PUT silently nulls existing columns

**File:** `src/phaze/routers/agent_metadata.py:41-50`
**Issue:**
```python
payload = {**body.model_dump(), "file_id": file_id, "id": uuid.uuid4()}
# ...
update_keys = set(body.model_dump().keys())
stmt = stmt.on_conflict_do_update(
    index_elements=["file_id"],
    set_={k: stmt.excluded[k] for k in update_keys},
)
```

`body.model_dump()` (no `exclude_unset=True`) emits **every** field on `MetadataWriteRequest`, including the unset Optional fields whose Pydantic default is `None`. `update_keys` is therefore *always* the full nine-key set `{artist, title, album, year, genre, track_number, duration, bitrate, raw_tags}`, and `stmt.excluded[k]` for those keys evaluates to `NULL` from the INSERT row.

Consequence: a client that PUTs `{"artist": "Aphex Twin"}` against a row that already had `{artist=Aphex, title=Xtal, year=1992}` will null out `title` AND `year` AND every other field — only `artist` survives. The schema's `field: str | None = None` makes partial payloads look valid; the handler turns them into destructive overwrites. The single replay test (`test_metadata_replay_overwrites`) sends the same two fields both times, so it never sees a partial replay clobber.

Two interpretations of D-14:
- (a) "full-row replace, agents must always send every field" — then the schema is misleading; the Pydantic fields should not have `None` defaults, or the docstring should explicitly say "any unset field is set to NULL".
- (b) "Pydantic exclude-unset partial update" — then the dump must use `exclude_unset=True` and `update_keys` must be derived from the *set* fields.

Either way, the current state is incorrect: the docstring on line 29-30 says `"every column in body.model_dump() lands in the UPDATE set clause"`, which is *technically* true given the dump call but operationally surprising.

**Fix:** Decide on partial-update vs. full-replace semantics, then either:

```python
# Option A: partial update (most natural fit for the Optional[...] = None schema)
dumped = body.model_dump(exclude_unset=True)
payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}
stmt = pg_insert(FileMetadata).values([payload])
if dumped:
    stmt = stmt.on_conflict_do_update(
        index_elements=["file_id"],
        set_={k: stmt.excluded[k] for k in dumped},
    )
else:
    stmt = stmt.on_conflict_do_nothing(index_elements=["file_id"])
```

```python
# Option B: full replace (matches current intent) -- make it explicit and tested
# Keep the current code, but add a regression test that confirms partial PUT
# nulls every unset field, and update the docstring to flag the destructive
# semantics.
```

### CR-02: PATCH against terminal ExecutionLog with same status returns 409, breaks idempotent retry contract

**File:** `src/phaze/routers/agent_execution.py:107-122`
**Issue:**
```python
cur = ExecutionStatus(existing.status)
new = body.status
# D-15: terminal-state guard runs FIRST (early exit before regress check).
if cur in _TERMINAL:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="execution-log status is terminal")
# D-15: monotonic guard -- `<` (not `<=`) so same-status retry is allowed.
if _STATUS_ORDER[new] < _STATUS_ORDER[cur]:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="execution-log status would regress")
```

The docstring at lines 99-100 promises: "200 otherwise (same-status PATCH allowed for idempotent retry; comparator is strict `<`, NOT `<=`)". But the terminal-state guard runs *before* the regress check, so a `COMPLETED -> COMPLETED` PATCH (the canonical idempotent retry after a network blip swallowed the 200 from the first call) returns 409 `"execution-log status is terminal"`. The only same-status test (`test_same_status_patch_allowed`) covers `IN_PROGRESS -> IN_PROGRESS`, which dodges the terminal branch entirely.

This is the most common retry case in practice: the agent finishes the move, sends `PATCH {"status": "completed"}`, the network drops the response, SAQ retries the job, the agent re-sends the PATCH — and now gets a 409 it has no clean way to recover from (the agent cannot tell whether "is terminal" means "this is your own previous write" or "another writer terminalised this").

**Fix:** Allow same-status PATCH against terminal rows for idempotent retry:

```python
# D-15: terminal-state guard runs only when the new status *would change* the
# row. Same-status retry against a terminal row is the canonical idempotent
# retry case and must return 200, not 409.
if cur in _TERMINAL and new != cur:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="execution-log status is terminal")

if _STATUS_ORDER[new] < _STATUS_ORDER[cur]:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="execution-log status would regress")
```

Add a regression test `test_same_status_patch_terminal_allowed` that PATCHes a `completed` row with `{"status": "completed"}` and asserts 200.

If the terminal-no-retry behavior is intentional (e.g., to detect agent-side state-machine bugs), then the docstring and `test_same_status_patch_allowed` should explicitly call out that the same-status allowance is **only** for non-terminal states — and the agent SDK in Phase 26+ needs to suppress retries for terminal PATCHes.

## Warnings

### WR-01: Cross-agent write authority on file_id / proposal_id never verified

**File:** `src/phaze/routers/agent_metadata.py:21-53`, `src/phaze/routers/agent_fingerprint.py:20-49`, `src/phaze/routers/agent_execution.py:60-80`
**Issue:** None of the three "write by file_id" handlers verify that the `file_id` (or `proposal_id` in execution-log's case) belongs to the authenticated agent. Agent A, authenticated with its own bearer, can:
- `PUT /api/internal/agent/metadata/<file_id_owned_by_agent_B>` and overwrite B's metadata.
- `PUT /api/internal/agent/fingerprints/<file_id_owned_by_agent_B>/audfprint` and overwrite B's fingerprint row.
- `POST /api/internal/agent/execution-log {proposal_id: <B's proposal>}` and forge an audit row that the response then attributes to agent A.

The phase plan (`25-04-SUMMARY.md:261`) explicitly accepts this as **T-25-04-T** deferred to Phase 29, on the rationale that "watcher + execution paths in Phases 27/28 may legitimately need to update files originally owned by another agent." That's a defensible position, but worth visible audit because (a) the audit trail in `execution_log` is the system's compliance record, and (b) the `agent_id` field in responses creates a false attribution: the response says "agent A did this", but the row's chain-of-custody (via `proposal_id -> file_id -> agent_id`) points at agent B.

**Fix:** No code change required for v1. Add a `SECURITY.md` note pointing at T-25-04-T and Phase 29, and add an integration test that *documents* current cross-agent acceptance so a future hardening pass cannot accidentally tighten without a deliberate sign-off:

```python
async def test_cross_agent_write_currently_permitted(...):
    """T-25-04-T documents this gap; Phase 29 closes it. Locking in the
    current behavior so a silent change later requires a deliberate plan."""
    # ...
```

### WR-02: `agent_files.py` NFC-normalizes `original_path` but not `current_path` or `original_filename`

**File:** `src/phaze/routers/agent_files.py:60-65`
**Issue:**
```python
data["original_path"] = unicodedata.normalize("NFC", data["original_path"])
data["agent_id"] = agent.id
data["state"] = FileState.DISCOVERED
data["id"] = uuid.uuid4()
```

Only `original_path` is normalized. `current_path` and `original_filename` are stored verbatim. Result: if an agent sends paths containing precomposed-vs-decomposed Unicode (common on macOS HFS+, NTFS, Linux ext4 with mixed clients), the conflict-target lookup uses normalized `original_path`, but `current_path` might still hold the decomposed form. The two `Text` columns are now *inconsistent in normalization* on a single row.

Downstream comparisons (e.g., a query "find rows where `current_path = original_path`" or filesystem-touch logic that reads `current_path` and re-normalizes) will silently mismatch.

**Fix:**

```python
data["original_path"] = unicodedata.normalize("NFC", data["original_path"])
data["current_path"] = unicodedata.normalize("NFC", data["current_path"])
data["original_filename"] = unicodedata.normalize("NFC", data["original_filename"])
```

### WR-03: `agent_files.py` extension lookup fails silently if agent supplies dotted `file_type`

**File:** `src/phaze/routers/agent_files.py:105-107`
**Issue:**
```python
ext = "." + row.file_type.lower()
if EXTENSION_MAP.get(ext, FileCategory.UNKNOWN) not in _EXTRACTABLE:
    continue
```

The Pydantic schema accepts `file_type: str = Field(min_length=1, max_length=10)` with **no character restriction**. If an agent sends `file_type=".mp3"`, the lookup becomes `"..mp3"`, which misses `EXTENSION_MAP`, and the file is silently NOT enqueued for metadata extraction. No error surfaces to the client; the response's `enqueued` count is lower than expected. The agent has no way to distinguish "extension category not extractable" from "I gave you a malformed file_type."

This is also inconsistent with `tasks/metadata_extraction.py:39` which uses the same `"." + file_record.file_type.lower()` pattern — so the silent miss propagates.

**Fix:** Either normalize/validate the `file_type` shape:

```python
# in schemas/agent_files.py
file_type: str = Field(min_length=1, max_length=10, pattern=r"^[a-zA-Z0-9]+$")
```

Or strip a leading dot defensively on the router side:

```python
ext = "." + row.file_type.lstrip(".").lower()
```

### WR-04: Test DB uses `Base.metadata.create_all`, never runs migration 014; partial-index path is untested

**File:** `tests/conftest.py:39-45`
**Issue:**
```python
engine = create_async_engine(TEST_DATABASE_URL)
async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)
```

`Base.metadata.create_all` creates tables from SQLAlchemy models but **does not** run alembic migrations. The partial index `ix_agents_token_hash_active (token_hash) WHERE revoked_at IS NULL` from migration 014 is therefore never created in the router test DB. The auth dep's query still works (it's a regular SELECT with a `WHERE revoked_at IS NULL` clause that Postgres satisfies with a seq scan), but tests don't validate that the production index actually gets hit, and a future drift between `Agent.revoked_at.is_(None)` and the migration's `sa.text("revoked_at IS NULL")` predicate (the byte-for-byte match required for partial-index selection) is invisible to CI.

**Fix:** Either (a) replace `create_all` with `alembic.command.upgrade(cfg, "head")` in the `async_engine` fixture (the migration-test fixtures already do this — see `tests/test_migrations/conftest.py`), or (b) add a single regression test that asserts the partial index exists and the production query plan uses it:

```python
async def test_auth_query_uses_partial_index(migrated_engine):
    async with migrated_engine.connect() as conn:
        plan = (await conn.execute(text(
            "EXPLAIN SELECT * FROM agents "
            "WHERE token_hash = 'x' AND revoked_at IS NULL"
        ))).all()
    assert any("ix_agents_token_hash_active" in row[0] for row in plan)
```

### WR-05: `tests/conftest.py` seeds legacy agent with `scan_roots=[]`; migration 012 seeds with SCAN_PATH-derived list

**File:** `tests/conftest.py:44`
**Issue:**
```python
setup_session.add(Agent(id=LEGACY_AGENT_ID, name=LEGACY_AGENT_ID, scan_roots=[]))
```

Migration 012 (verified by `test_legacy_agent_scan_roots_fallback`) seeds the legacy agent with `scan_roots == ["/data/music"]`. The test fixture seeds with `[]`. Any test that reads `agent.scan_roots` expecting the migration-shaped default will silently see different data depending on which DB it ran against. Today no Phase-25 test exercises that path, but Phase 26+ services that read `agent.scan_roots` will inherit the mismatch.

**Fix:** Match the migration-seeded value:

```python
setup_session.add(Agent(
    id=LEGACY_AGENT_ID,
    name=LEGACY_AGENT_ID,
    scan_roots=["/data/music"],
))
```

### WR-06: `_CHUNK_MAX` resolved at module import — env-var change requires process restart

**File:** `src/phaze/schemas/agent_files.py:18-22`
**Issue:**
```python
_CHUNK_MAX: int = settings.agent_file_chunk_max
```

Evaluated once, at import time. Subsequent updates to `AGENT_FILE_CHUNK_MAX` in the environment have no effect until the process restarts. The docstring acknowledges this, but the docstring is the only signal — tests don't catch this if a deployment-time bumps the env var and forgets to restart.

This is fine for a single-user admin tool, but worth flagging because the schema's `Field(min_length=1, max_length=_CHUNK_MAX)` is enforced at class-definition time, so even a `Settings()` reload won't take effect.

**Fix:** Either accept the documented behavior (current state — fine for v1), or use a model validator that consults `settings.agent_file_chunk_max` at validation time:

```python
class FileUpsertChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[FileUpsertRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def _enforce_chunk_max(self) -> Self:
        if len(self.files) > settings.agent_file_chunk_max:
            raise ValueError(f"chunk exceeds max {settings.agent_file_chunk_max}")
        return self
```

### WR-07: `agent_heartbeat.py` accepts unbounded integer values for `worker_pid`, `queue_depth`

**File:** `src/phaze/schemas/agent_heartbeat.py:14-17`
**Issue:**
```python
class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_version: str
    worker_pid: int
    queue_depth: int
```

No upper or lower bounds. A buggy or hostile agent could send `worker_pid: 1 << 63` or negative numbers. The value lands in `agents.last_status` JSONB unbounded. Low risk on a private LAN, but cheap to fix.

Same for `agent_version` — no length cap.

**Fix:**

```python
class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_version: str = Field(min_length=1, max_length=64)
    worker_pid: int = Field(ge=1, le=2**31 - 1)  # PID_MAX_LIMIT on Linux
    queue_depth: int = Field(ge=0, le=10_000_000)
```

### WR-08: `agent_files.py` enqueue silently swallows per-row failures

**File:** `src/phaze/routers/agent_files.py:108-114`
**Issue:**
```python
try:
    await queue.enqueue("extract_file_metadata", file_id=str(row.id))
    enqueued += 1
except Exception:
    logger.exception("Failed to enqueue extract_file_metadata for file_id=%s agent_id=%s", row.id, agent.id)
```

The bare `except Exception` is documented as "best-effort post-commit", which is defensible. But:

1. The response field `enqueued` reflects the success count — the client cannot distinguish "100 inserted, 90 enqueued" (10 enqueue failures) from "100 inserted, 90 are music/video, 10 are companions skipped." Both produce the same `enqueued=90`.
2. There's no metric / counter for enqueue failures; the only signal is `logger.exception`. Operators searching for a missed-extraction backlog have to grep logs.

The doc says "the operator can re-enqueue manually via Phase 27's UI on retryable failure" — assuming Phase 27 ships that UI, this is acceptable. Until then, the silent path is a debugging hazard.

**Fix:** Add a third response counter:

```python
# in schemas/agent_files.py
class FileUpsertResponse(BaseModel):
    agent_id: str
    upserted: int
    inserted: int
    enqueued: int
    enqueue_failed: int  # new
```

```python
# in router
enqueue_failed = 0
# in except branch:
enqueue_failed += 1
# in return:
return FileUpsertResponse(..., enqueue_failed=enqueue_failed)
```

## Info

### IN-01: `agent_auth.py` 403 response missing `WWW-Authenticate` header

**File:** `src/phaze/routers/agent_auth.py:82-84`
**Issue:** The 403 emitted for unknown/revoked tokens does not include a `WWW-Authenticate: Bearer` header. RFC 6750 only requires that header on 401, so this is technically compliant, but some HTTP clients (and pen-test tooling) use the header's presence to distinguish "the resource exists, your creds are wrong" from "this scheme isn't recognized at all." A consistent header on both 401 and 403 makes the auth surface easier to introspect.

**Fix:** Optional, low priority:

```python
raise HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Forbidden",
    headers={"WWW-Authenticate": "Bearer"},
)
```

### IN-02: `agent_fingerprint.py` accepts arbitrary `engine` path-segment with no validation

**File:** `src/phaze/routers/agent_fingerprint.py:20-49`
**Issue:** `engine: str` is taken from the URL path with no Pydantic validation. An agent can `PUT /api/internal/agent/fingerprints/<file_id>/anything-up-to-30-chars`. Only the DB-side `String(30)` column truncation prevents overflow. Trusted-agent context makes this low risk, but a typed `Literal["audfprint", "panako"]` would catch agent bugs at the API edge instead of letting them silently create a row with a misspelled engine name.

**Fix:**

```python
from typing import Literal

@router.put("/{file_id}/{engine}", ...)
async def put_fingerprint(
    file_id: uuid.UUID,
    engine: Literal["audfprint", "panako"],
    ...
):
```

FastAPI will reject any other value with 422.

### IN-03: `tests/test_routers/test_agent_files.py:171` magic number `1001` lacks context

**File:** `tests/test_routers/test_agent_files.py:170-174`
**Issue:**
```python
chunk = {"files": [_make_record(path=f"/test/music/{i:04d}.mp3") for i in range(1001)]}
```

`1001` is one past `settings.agent_file_chunk_max` (1000). A future bump of the chunk cap to 2000 will silently make this test pass without exercising the boundary, because 1001 is well below the new cap. Tie the constant to the source of truth:

**Fix:**

```python
from phaze.config import settings

chunk = {"files": [_make_record(path=f"/test/music/{i:04d}.mp3") for i in range(settings.agent_file_chunk_max + 1)]}
```

### IN-04: `agent_files.py` per-request Queue construction creates Redis-connection churn

**File:** `src/phaze/routers/agent_files.py:99-116`
**Issue:** `Queue.from_url(...)` + `queue.disconnect()` per request creates a new Redis connection per chunk. At 1000 chunks/sec this will saturate Redis's accept queue. The plan acknowledges this (`per-call construction per RESEARCH Pattern 3`) and the deployment is single-user / private LAN, so it's not a v1 concern. But the existing `app.state.queue` (created in `main.py:41`) is the natural reuse target — a future refactor should funnel agent-files enqueue through that shared queue, with a `name` override per call rather than a fresh `Queue.from_url`.

**Fix (deferred — for follow-up plan):**

```python
# in main.py lifespan:
app.state.agent_queues = {}  # cache per-agent Queue instances

# in router:
queue_name = f"phaze-agent-{agent.id}"
queue = request.app.state.agent_queues.get(queue_name)
if queue is None:
    queue = Queue.from_url(settings.redis_url, name=queue_name)
    request.app.state.agent_queues[queue_name] = queue
# no disconnect -- managed by lifespan shutdown
```

---

_Reviewed: 2026-05-11_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
