# Phase 8: Safe File Execution & Audit - Research

**Researched:** 2026-03-29
**Domain:** File operations (copy-verify-delete), append-only audit logging, SSE real-time progress, HTMX integration
**Confidence:** HIGH

## Summary

Phase 8 implements the final critical pipeline step: executing approved file renames safely using a copy-verify-delete protocol with full audit logging. The codebase is well-prepared -- the ExecutionLog model, ExecutionStatus enum, and FileState transitions (EXECUTED/FAILED) already exist. The arq task infrastructure from Phase 4 provides the job execution pattern. The Phase 7 approval UI provides the HTMX/Jinja2 template patterns to extend.

The primary new dependency is `sse-starlette` for Server-Sent Events streaming to provide real-time execution progress. The HTMX SSE extension (`htmx-ext-sse`) provides the client-side integration with zero custom JavaScript. The copy-verify-delete logic itself is straightforward Python stdlib (`shutil.copy2` + hashlib SHA256 verification), but the audit logging discipline (log-before-execute, append-only) and error handling (delete bad copy on hash mismatch, continue on failure) require careful implementation.

**Primary recommendation:** Use `shutil.copy2` for copy (preserves metadata), `hashlib.sha256` for verification, `pathlib.Path.unlink()` for delete. Log each operation step to ExecutionLog BEFORE executing it (write-ahead pattern). Use `sse-starlette` EventSourceResponse for live progress. One arq job per batch execution, processing files sequentially within the job to avoid overwhelming disk I/O.

<user_constraints>

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Admin button in the existing approval UI -- "Execute approved" button triggers a batch job for all approved proposals
- **D-02:** Executes ALL approved proposals at once. No selection UI -- one click processes everything. arq workers handle the batch in parallel
- **D-03:** Rename in-place -- file stays in its current directory, gets the proposed_filename. Destination = current_path directory + proposed_filename
- **D-04:** Leave empty directories alone after renames
- **D-05:** If SHA256 verification fails after copy, delete the bad copy at destination. Original file remains untouched. No tag writing -- SHA256 must match exactly on byte-for-byte copy
- **D-06:** No automatic retry on failure. Mark as failed, move to next file
- **D-07:** Continue on failure -- each file is independent. One failure does not affect others
- **D-08:** Live progress counter via SSE. FastAPI streams updates using HTMX native SSE support (hx-ext='sse')
- **D-09:** Separate audit log page -- paginated table of ExecutionLog rows with filtering by status
- **D-10:** After execution completes, approval UI shows "Executed" badge on executed proposals

### Claude's Discretion
- SSE endpoint implementation details (EventSourceResponse pattern)
- HTMX SSE integration specifics (hx-ext, event names, swap strategy)
- Batch size for arq jobs (how many files per job vs one job per file)
- ExecutionLog write timing (before vs after each operation step)
- Audit log page URL and navigation placement
- "Execute approved" button placement and styling in approval UI
- How to update FileRecord.current_path after successful rename
- Alembic migration strategy if ExecutionLog table needs changes

### Deferred Ideas (OUT OF SCOPE)
- EXE-03 (Full undo/rollback via audit trail)
- EXE-04 (Acoustic duplicate detection)
- EXE-05 (Full progress tracking / job status visibility)
- AIP-03 (Directory path proposals)
- Empty directory cleanup

</user_constraints>

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| EXE-01 | System executes approved renames using copy-verify-delete protocol (never direct move) | Core execution service using shutil.copy2 + hashlib.sha256 verification + pathlib.Path.unlink(). Existing FileRecord.sha256_hash provides the expected hash for comparison. |
| EXE-02 | System logs every file operation to an append-only audit table in PostgreSQL | ExecutionLog model already defined. Write-ahead logging pattern: insert log row with PENDING status before each operation, update to COMPLETED/FAILED after. |

</phase_requirements>

## Project Constraints (from CLAUDE.md)

- Python 3.13 exclusively, `uv` package manager only
- All commands prefixed with `uv run` (never bare `pip`, `pytest`, `mypy`)
- Pre-commit hooks must pass before commits (frozen SHAs)
- 85% minimum code coverage
- Type hints on all functions, strict mypy (excluding tests)
- Ruff: 150-char line length, double quotes, specific rule sets
- Frequent git commits during execution, not batched at end
- Each service needs a README kept up to date
- GitHub Actions must delegate to just commands
- PR per phase with worktree branches

## Standard Stack

### Core (already in project)
| Library | Version | Purpose | Status |
|---------|---------|---------|--------|
| shutil (stdlib) | 3.13 | File copy with metadata preservation | Built-in, no install needed |
| hashlib (stdlib) | 3.13 | SHA256 hash computation | Built-in, no install needed |
| pathlib (stdlib) | 3.13 | Path manipulation and file operations | Built-in, no install needed |
| SQLAlchemy | >=2.0.48 | Async ORM for ExecutionLog writes | Already installed |
| arq | >=0.27.0 | Task queue for batch execution jobs | Already installed |
| FastAPI | >=0.135.2 | API endpoints including SSE | Already installed |

### New Dependencies
| Library | Version | Purpose | Why Needed |
|---------|---------|---------|------------|
| sse-starlette | >=3.3.4 | EventSourceResponse for SSE streaming | Required for D-08 live progress counter. Standard SSE library for Starlette/FastAPI. W3C SSE spec compliant. |
| htmx-ext-sse | 2.2.4 (CDN) | HTMX SSE client extension | Required for D-08 browser-side SSE consumption. Loaded via CDN script tag (no pip install). |

**Installation:**
```bash
uv add "sse-starlette>=3.3.4"
```

**CDN addition to base.html:**
```html
<script src="https://cdn.jsdelivr.net/npm/htmx-ext-sse@2.2.4"></script>
```

## Architecture Patterns

### Recommended Project Structure (new files)
```
src/phaze/
├── services/
│   └── execution.py           # Copy-verify-delete logic + audit logging
├── routers/
│   └── execution.py           # Execute button endpoint, SSE endpoint, audit log page
├── tasks/
│   └── execution.py           # arq job function for batch execution
└── templates/
    └── execution/
        ├── audit_log.html     # Full page: audit log with filters
        └── partials/
            ├── audit_table.html    # HTMX fragment: paginated audit rows
            ├── progress.html       # SSE progress counter fragment
            └── execute_button.html # Execute approved button fragment
```

### Pattern 1: Write-Ahead Audit Logging
**What:** Log each operation to ExecutionLog BEFORE executing it, then update status after.
**When to use:** Every file operation (copy, verify, delete).
**Why:** If the process crashes mid-operation, the log shows what was attempted. This is the append-only audit trail required by EXE-02.

```python
# Pattern: log-before-execute
async def log_operation(
    session: AsyncSession,
    proposal_id: uuid.UUID,
    operation: str,
    source_path: str,
    destination_path: str,
) -> ExecutionLog:
    """Create a PENDING audit log entry before executing an operation."""
    log_entry = ExecutionLog(
        proposal_id=proposal_id,
        operation=operation,  # "copy", "verify", "delete"
        source_path=source_path,
        destination_path=destination_path,
        sha256_verified=False,
        status=ExecutionStatus.IN_PROGRESS,
    )
    session.add(log_entry)
    await session.commit()
    return log_entry


async def complete_operation(
    session: AsyncSession,
    log_entry: ExecutionLog,
    *,
    sha256_verified: bool = False,
    error_message: str | None = None,
) -> None:
    """Update the audit log entry after operation completes or fails."""
    log_entry.status = ExecutionStatus.FAILED if error_message else ExecutionStatus.COMPLETED
    log_entry.sha256_verified = sha256_verified
    log_entry.error_message = error_message
    await session.commit()
```

### Pattern 2: Copy-Verify-Delete Protocol
**What:** Three-step safe rename: copy file, verify SHA256 matches, delete original.
**When to use:** Every approved file rename execution.

```python
import hashlib
import shutil
from pathlib import Path


def compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hash of a file in chunks (memory efficient for large files)."""
    sha256 = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def copy_file(source: Path, destination: Path) -> None:
    """Copy file preserving metadata. Raises on failure."""
    shutil.copy2(source, destination)


def verify_copy(source_hash: str, destination: Path) -> bool:
    """Verify SHA256 of copied file matches expected hash."""
    return compute_sha256(destination) == source_hash


def delete_original(source: Path) -> None:
    """Delete original file after successful copy+verify."""
    source.unlink()
```

### Pattern 3: SSE Progress Streaming
**What:** FastAPI endpoint streams execution progress via Server-Sent Events.
**When to use:** D-08 live progress counter during batch execution.

```python
import asyncio
from sse_starlette import EventSourceResponse


async def execution_progress_stream(batch_id: str) -> AsyncGenerator[dict, None]:
    """Yield SSE events as files are processed."""
    while True:
        # Query current progress from DB or Redis
        stats = await get_execution_progress(batch_id)
        yield {
            "event": "progress",
            "data": f'<span id="progress-count">{stats["completed"]}/{stats["total"]}</span>',
        }
        if stats["completed"] + stats["failed"] >= stats["total"]:
            yield {"event": "complete", "data": "<span>Execution complete!</span>"}
            break
        await asyncio.sleep(1)


@router.get("/execution/progress/{batch_id}")
async def sse_progress(batch_id: str):
    return EventSourceResponse(execution_progress_stream(batch_id))
```

**HTMX client-side:**
```html
<div hx-ext="sse" sse-connect="/execution/progress/{{ batch_id }}">
    <span sse-swap="progress">Waiting...</span>
    <span sse-swap="complete" sse-close="complete"></span>
</div>
```

### Pattern 4: arq Batch Execution Job
**What:** Single arq job processes all approved proposals sequentially.
**Recommendation:** One arq job for the entire batch, processing files one at a time within the job. File I/O on the same disk benefits from sequential access, not parallel writes. Store progress in Redis for SSE polling.

```python
async def execute_approved_batch(ctx: dict, batch_id: str) -> dict:
    """Execute all approved proposals in sequence."""
    session = await _get_session()
    try:
        proposals = await get_approved_proposals(session)
        total = len(proposals)
        completed = 0
        failed = 0

        # Store progress in Redis for SSE endpoint to read
        redis = ctx["redis"]
        await redis.hset(f"exec:{batch_id}", mapping={"total": total, "completed": 0, "failed": 0})

        for proposal in proposals:
            try:
                await execute_single_file(session, proposal)
                completed += 1
            except Exception:
                failed += 1

            await redis.hset(f"exec:{batch_id}", mapping={
                "completed": completed, "failed": failed
            })

        return {"batch_id": batch_id, "completed": completed, "failed": failed}
    finally:
        await session.close()
```

### Anti-Patterns to Avoid
- **Direct rename/move:** Never use `os.rename()` or `shutil.move()` -- these can lose data on cross-filesystem operations or failures. Always copy-verify-delete.
- **Logging after execution:** Don't log operations after they complete. Log BEFORE (write-ahead) so crashes are traceable.
- **Parallel file writes:** Don't use process pool or parallel I/O for the copy operations. Sequential disk I/O on a single drive is faster and safer.
- **Modifying ExecutionLog rows:** The audit table is append-only. Status updates on existing rows are acceptable (PENDING -> COMPLETED), but never delete rows or change operation/path fields.
- **Large file reads into memory:** Always hash files in chunks (8KB buffer), never read entire file into memory. Some music files can be 100MB+.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| SSE streaming | Custom streaming response | sse-starlette EventSourceResponse | Handles SSE protocol, keep-alive pings, reconnection, Content-Type headers correctly |
| File copy with metadata | Manual read/write loop | shutil.copy2 | Preserves file metadata (timestamps, permissions), handles platform differences |
| SHA256 hashing | Custom hash implementation | hashlib.sha256 | C-optimized, memory-efficient with chunked reads |
| SSE client consumption | Custom JavaScript EventSource | htmx-ext-sse | Automatic reconnection, DOM swapping, event routing, connection lifecycle |
| Pagination for audit log | Custom SQL offset/limit | Reuse existing Pagination dataclass from proposal_queries.py | Already tested, consistent UI pattern |

## Common Pitfalls

### Pitfall 1: Cross-filesystem Copy Failure
**What goes wrong:** `os.rename()` fails when source and destination are on different filesystems (e.g., Docker volumes).
**Why it happens:** `os.rename()` is an atomic operation that only works within the same filesystem.
**How to avoid:** Always use `shutil.copy2()` + verify + `unlink()`. This works across any filesystem boundary.
**Warning signs:** `OSError: [Errno 18] Invalid cross-device link`

### Pitfall 2: Hash Mismatch on Destination -- Forgetting to Clean Up
**What goes wrong:** SHA256 verification fails but the bad copy is left at the destination path, creating a corrupted file.
**Why it happens:** Error handler doesn't delete the failed copy.
**How to avoid:** Per D-05, if verification fails, delete the destination file immediately before marking as failed. Use try/finally.
**Warning signs:** Destination file exists with different hash than source.

### Pitfall 3: Race Condition on "Execute Approved" Button
**What goes wrong:** User clicks "Execute approved" twice, spawning duplicate batch jobs that process the same files.
**Why it happens:** No idempotency guard on the execute endpoint.
**How to avoid:** Check for active execution jobs before spawning a new one. Use a Redis lock or check for in-progress ExecutionLog entries. Disable the button via HTMX after click.
**Warning signs:** Duplicate ExecutionLog entries for the same proposal.

### Pitfall 4: SSE Connection Not Closing
**What goes wrong:** SSE stream stays open after execution completes, consuming server resources.
**Why it happens:** Generator doesn't yield a close event, or HTMX client doesn't have `sse-close` configured.
**How to avoid:** Yield a "complete" event when batch finishes. Use `sse-close="complete"` on the HTMX element.
**Warning signs:** Browser DevTools shows open SSE connections after execution finishes.

### Pitfall 5: File Path Conflicts
**What goes wrong:** Two files in the same directory get proposed the same filename, causing the second copy to overwrite the first.
**Why it happens:** AI proposals don't guarantee unique filenames within a directory.
**How to avoid:** Before copying, check if destination path already exists. If so, fail the operation rather than overwriting.
**Warning signs:** ExecutionLog shows successful copy but file content doesn't match expected hash.

### Pitfall 6: Stale current_path After Rename
**What goes wrong:** FileRecord.current_path not updated after successful rename, causing future operations to reference the old path.
**Why it happens:** Execution service copies/deletes the file but forgets to update the DB record.
**How to avoid:** After successful copy-verify-delete, update FileRecord.current_path to the new path and FileRecord.state to EXECUTED in the same transaction.
**Warning signs:** Subsequent queries or UI show old file paths.

## Code Examples

### Complete Single File Execution Flow
```python
async def execute_single_file(
    session: AsyncSession,
    proposal: RenameProposal,
    file_record: FileRecord,
) -> bool:
    """Execute copy-verify-delete for a single file. Returns True on success."""
    source = Path(file_record.current_path)
    dest_dir = source.parent
    destination = dest_dir / proposal.proposed_filename

    # Guard: destination must not already exist
    if destination.exists():
        log = await log_operation(session, proposal.id, "copy", str(source), str(destination))
        await complete_operation(session, log, error_message="Destination already exists")
        file_record.state = FileState.FAILED
        await session.commit()
        return False

    # Step 1: Copy
    copy_log = await log_operation(session, proposal.id, "copy", str(source), str(destination))
    try:
        shutil.copy2(source, destination)
        await complete_operation(session, copy_log)
    except OSError as e:
        await complete_operation(session, copy_log, error_message=str(e))
        file_record.state = FileState.FAILED
        await session.commit()
        return False

    # Step 2: Verify
    verify_log = await log_operation(session, proposal.id, "verify", str(source), str(destination))
    dest_hash = compute_sha256(destination)
    if dest_hash != file_record.sha256_hash:
        await complete_operation(session, verify_log, error_message=f"Hash mismatch: expected {file_record.sha256_hash}, got {dest_hash}")
        # D-05: Delete the bad copy
        try:
            destination.unlink()
        except OSError:
            pass
        file_record.state = FileState.FAILED
        await session.commit()
        return False
    await complete_operation(session, verify_log, sha256_verified=True)

    # Step 3: Delete original
    delete_log = await log_operation(session, proposal.id, "delete", str(source), str(destination))
    try:
        source.unlink()
        await complete_operation(session, delete_log)
    except OSError as e:
        await complete_operation(session, delete_log, error_message=str(e))
        # File was successfully copied and verified -- update path even if delete fails
        # (the copy is good, we just couldn't clean up the original)

    # Update file record
    file_record.current_path = str(destination)
    file_record.state = FileState.EXECUTED
    await session.commit()
    return True
```

### SSE Progress with Redis State
```python
# In tasks/execution.py -- store progress in Redis
async def update_progress(redis, batch_id: str, completed: int, failed: int, total: int) -> None:
    """Update execution progress in Redis for SSE consumers."""
    await redis.hset(f"exec:{batch_id}", mapping={
        "total": str(total),
        "completed": str(completed),
        "failed": str(failed),
        "status": "complete" if completed + failed >= total else "running",
    })


# In routers/execution.py -- SSE endpoint reads from Redis
async def progress_generator(redis, batch_id: str) -> AsyncGenerator[dict, None]:
    """Generate SSE events by polling Redis progress state."""
    while True:
        data = await redis.hgetall(f"exec:{batch_id}")
        if not data:
            yield {"event": "progress", "data": "Waiting for execution to start..."}
        else:
            completed = int(data.get(b"completed", 0))
            failed = int(data.get(b"failed", 0))
            total = int(data.get(b"total", 0))
            yield {
                "event": "progress",
                "data": f"{completed + failed}/{total} processed ({failed} failed)",
            }
            if data.get(b"status") == b"complete":
                yield {"event": "complete", "data": "Execution complete. Refresh to see results."}
                break
        await asyncio.sleep(1)
```

### Audit Log Query Pattern (reuse Pagination)
```python
async def get_execution_logs_page(
    session: AsyncSession,
    *,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ExecutionLog], Pagination]:
    """Paginated, filtered audit log query. Mirrors proposal_queries pattern."""
    base = select(ExecutionLog).order_by(ExecutionLog.executed_at.desc())
    count_base = select(func.count()).select_from(ExecutionLog)

    if status and status != "all":
        base = base.where(ExecutionLog.status == status)
        count_base = count_base.where(ExecutionLog.status == status)

    count_result = await session.execute(count_base)
    total = count_result.scalar_one()

    base = base.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(base)
    logs = list(result.scalars().all())

    pagination = Pagination(page=page, page_size=page_size, total=total)
    return logs, pagination
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| os.rename() for file moves | shutil.copy2() + verify + unlink() | Long-standing best practice | Cross-filesystem safety, crash recovery |
| Custom SSE response | sse-starlette EventSourceResponse | sse-starlette 3.x (2025) | W3C compliant, keep-alive, reconnection |
| HTMX built-in hx-sse | htmx-ext-sse extension | HTMX 2.0 | Migrated from core to extension, richer API |
| Polling for progress | SSE for real-time push | Standard pattern | Lower latency, no wasted requests |

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_services/test_execution.py tests/test_routers/test_execution.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| EXE-01a | Copy file to destination with correct name | unit | `uv run pytest tests/test_services/test_execution.py::test_copy_verify_delete_success -x` | Wave 0 |
| EXE-01b | Verify SHA256 matches after copy | unit | `uv run pytest tests/test_services/test_execution.py::test_hash_verification -x` | Wave 0 |
| EXE-01c | Delete bad copy on hash mismatch, original untouched | unit | `uv run pytest tests/test_services/test_execution.py::test_hash_mismatch_cleanup -x` | Wave 0 |
| EXE-01d | Destination already exists -> fail without overwrite | unit | `uv run pytest tests/test_services/test_execution.py::test_destination_exists -x` | Wave 0 |
| EXE-01e | FileRecord.current_path updated after success | unit | `uv run pytest tests/test_services/test_execution.py::test_file_record_updated -x` | Wave 0 |
| EXE-02a | ExecutionLog entry created before each operation | unit | `uv run pytest tests/test_services/test_execution.py::test_audit_log_created -x` | Wave 0 |
| EXE-02b | ExecutionLog status updated after operation completes/fails | unit | `uv run pytest tests/test_services/test_execution.py::test_audit_log_status_update -x` | Wave 0 |
| EXE-02c | Audit log page renders with pagination/filtering | integration | `uv run pytest tests/test_routers/test_execution.py::test_audit_log_page -x` | Wave 0 |
| D-08 | SSE endpoint streams progress events | integration | `uv run pytest tests/test_routers/test_execution.py::test_sse_progress -x` | Wave 0 |
| D-02 | Execute button triggers batch job | integration | `uv run pytest tests/test_routers/test_execution.py::test_execute_approved -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_services/test_execution.py tests/test_routers/test_execution.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/test_execution.py` -- covers EXE-01, EXE-02 service logic
- [ ] `tests/test_routers/test_execution.py` -- covers execution endpoints, SSE, audit page
- [ ] `tests/test_tasks/test_execution.py` -- covers arq batch job function

## Open Questions

1. **Alembic migration for ExecutionLog table**
   - What we know: ExecutionLog model exists in `models/execution.py` and is imported in `models/__init__.py`. Three existing migrations exist.
   - What's unclear: Whether the execution_log table was included in an existing migration or needs a new one.
   - Recommendation: Check existing migrations. If not present, create migration `004_add_execution_log.py`. The model is already defined, so autogenerate should pick it up.

2. **Redis connection in arq job context**
   - What we know: arq provides `ctx["redis"]` in job functions (the arq Redis pool).
   - What's unclear: Whether arq's internal Redis connection object supports `hset` and `hgetall` directly or needs wrapping.
   - Recommendation: arq uses `arq.connections.ArqRedis` which extends `redis.asyncio.Redis`. Standard Redis commands are available directly.

3. **SSE testing approach**
   - What we know: httpx AsyncClient can make requests but SSE is a streaming response.
   - What's unclear: Best pattern for testing SSE endpoints with httpx.
   - Recommendation: Use `httpx.AsyncClient.stream()` to read SSE events in tests. Alternatively, test the generator function directly (unit test) and test the endpoint returns correct content-type (integration test).

## Sources

### Primary (HIGH confidence)
- Existing codebase: `src/phaze/models/execution.py` -- ExecutionLog model already defined
- Existing codebase: `src/phaze/models/file.py` -- FileRecord with sha256_hash, current_path, EXECUTED state
- Existing codebase: `src/phaze/tasks/functions.py` -- arq task pattern with _get_session
- Existing codebase: `src/phaze/routers/proposals.py` -- HTMX fragment pattern
- Python stdlib docs: shutil.copy2, hashlib, pathlib

### Secondary (MEDIUM confidence)
- [sse-starlette PyPI](https://pypi.org/project/sse-starlette/) -- version 3.3.4 verified
- [HTMX SSE extension docs](https://htmx.org/extensions/sse/) -- version 2.2.4, sse-connect/sse-swap/sse-close API
- [FastAPI SSE tutorial](https://fastapi.tiangolo.com/tutorial/server-sent-events/) -- EventSourceResponse integration pattern

### Tertiary (LOW confidence)
- None -- all findings verified against primary or secondary sources

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - All libraries are stdlib or already in project, except sse-starlette which is the established Starlette SSE library
- Architecture: HIGH - Patterns directly extend existing codebase patterns (arq tasks, HTMX templates, SQLAlchemy queries)
- Pitfalls: HIGH - Copy-verify-delete is well-understood; pitfalls are based on file system operation fundamentals
- SSE integration: MEDIUM - sse-starlette + htmx-ext-sse is standard but testing SSE streams requires some experimentation

**Research date:** 2026-03-29
**Valid until:** 2026-04-28 (stable domain, no fast-moving dependencies)
