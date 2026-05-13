---
phase: 27-watcher-service-user-initiated-scan
reviewed: 2026-05-13T00:00:00Z
depth: standard
files_reviewed: 51
files_reviewed_list:
  - .env.example
  - alembic/env.py
  - docker-compose.yml
  - pyproject.toml
  - src/phaze/agent_watcher/__init__.py
  - src/phaze/agent_watcher/__main__.py
  - src/phaze/agent_watcher/debouncer.py
  - src/phaze/agent_watcher/observer.py
  - src/phaze/agent_watcher/poster.py
  - src/phaze/agent_watcher/README.md
  - src/phaze/config.py
  - src/phaze/main.py
  - src/phaze/routers/agent_files.py
  - src/phaze/routers/agent_scan_batches.py
  - src/phaze/routers/pipeline_scans.py
  - src/phaze/routers/pipeline.py
  - src/phaze/schemas/agent_files.py
  - src/phaze/schemas/agent_scan_batches.py
  - src/phaze/schemas/agent_tasks.py
  - src/phaze/schemas/pipeline_scans.py
  - src/phaze/services/agent_client.py
  - src/phaze/tasks/_shared/__init__.py
  - src/phaze/tasks/_shared/agent_bootstrap.py
  - src/phaze/tasks/agent_worker.py
  - src/phaze/tasks/scan.py
  - src/phaze/templates/pipeline/dashboard.html
  - src/phaze/templates/pipeline/partials/recent_scans_table.html
  - src/phaze/templates/pipeline/partials/scan_path_picker.html
  - src/phaze/templates/pipeline/partials/scan_progress_card.html
  - src/phaze/templates/pipeline/partials/scan_status_pill.html
  - src/phaze/templates/pipeline/partials/scan_submit_error.html
  - src/phaze/templates/pipeline/partials/trigger_scan_card.html
  - tests/test_agent_watcher/__init__.py
  - tests/test_agent_watcher/conftest.py
  - tests/test_agent_watcher/test_debouncer.py
  - tests/test_agent_watcher/test_main.py
  - tests/test_agent_watcher/test_observer.py
  - tests/test_config_role_split.py
  - tests/test_routers/test_agent_files_batch_id.py
  - tests/test_routers/test_agent_files.py
  - tests/test_routers/test_agent_scan_batches.py
  - tests/test_routers/test_pipeline_scans.py
  - tests/test_schemas/test_agent_files.py
  - tests/test_schemas/test_agent_scan_batches.py
  - tests/test_schemas/test_agent_tasks.py
  - tests/test_schemas/test_pipeline_scans.py
  - tests/test_services/test_agent_client_endpoints.py
  - tests/test_task_split.py
  - tests/test_tasks/test_agent_startup_banner.py
  - tests/test_tasks/test_scan_directory.py
  - tests/test_tasks/test_shared_agent_bootstrap.py
findings:
  critical: 1
  warning: 7
  info: 5
  total: 13
status: issues_found
---

# Phase 27: Code Review Report

**Reviewed:** 2026-05-13T00:00:00Z
**Depth:** standard
**Files Reviewed:** 51
**Status:** issues_found

## Summary

Phase 27 introduces an always-on watcher service (`phaze.agent_watcher`), a chunked HTTP-only `scan_directory` SAQ task, controller-side PATCH `/scan-batches` + batch_id-aware POST `/files`, and an admin Trigger Scan UI. Architecturally the implementation is sound and tracks the deliverables (D-01..D-25): cross-tenant guards run BEFORE state-machine evaluation (T-27-01, T-27-02), the import-boundary invariants for `agent_watcher` and `tasks.scan` are enforced by subprocess tests, the LIVE-sentinel resolution path is wired through the partial unique index, the bearer token never appears as an instance attribute or in log output (T-27-04), `loop.call_soon_threadsafe` is the only watchdog→asyncio bridge, evicted stuck files emit WARNING without posting, and the HTMX poll partial omits both `hx-trigger` and `hx-get` in terminal-state markup.

Findings concentrate on one BLOCKER (a category-filter inconsistency between the watcher and `scan_directory` that produces divergent FileRecord populations from the two ingestion paths), several WARNING-class robustness gaps (path-traversal substring check is too coarse, byte-path decode hardcodes UTF-8, watcher resource leak on startup failure, weak `lru_cache` interaction with `_resolve_chunk_size`), and a handful of INFO-class smells (dead Jinja branch, ORM transient-attribute pattern, `_SCAN_TRANSITIONS` defensive-programming dead code reachable only via Pydantic widening).

## Critical Issues

### CR-01: Watcher and `scan_directory` apply different file-category filters, producing divergent FileRecord ingestion sets

**File:** `src/phaze/agent_watcher/observer.py:41,69` AND `src/phaze/tasks/scan.py:158`
**Issue:** The watcher (`observer.py`) filters to `FileCategory.MUSIC` and `FileCategory.VIDEO` via `_EXTRACTABLE = frozenset({MUSIC, VIDEO})` (line 41), dropping every `COMPANION` extension (`.cue`, `.nfo`, `.txt`, `.jpg`, `.jpeg`, `.png`, `.gif`, `.m3u`, `.m3u8`, `.pls`, `.sfv`, `.md5`). The `scan_directory` SAQ task uses a permissive filter `if category == FileCategory.UNKNOWN: continue` (line 158), so it POSTs every non-UNKNOWN file, including COMPANION extensions. The same `EXTENSION_MAP` table is the source of truth for both modules.

This means a manually-triggered scan inserts FileRecord rows for `.txt`/`.jpg`/`.cue`/etc. that the watcher will NEVER discover. After the user runs one manual scan, the LIVE sentinel batch's row population for an agent is permanently broader than what the watcher maintains. Companion files inserted by `scan_directory` get a `batch_id` pointing at the operator's RUNNING batch; if the same directory is later re-scanned by the watcher (e.g., a music file is touched, causing the COMPANION sibling to be ignored), the music file's `batch_id` flips to LIVE while the companion remains pointing at the (now COMPLETED) RUNNING batch. Downstream reporting (Recent Scans, future deduplication) sees inconsistent ownership.

There is also no test that pins down the intended filter for either path: `tests/test_agent_watcher/test_observer.py::test_event_handler_filters_by_extension` exercises only `.txt` (UNKNOWN) and `.mp3` (MUSIC); it never asserts the COMPANION case. `tests/test_tasks/test_scan_directory.py::test_scan_directory_walks_known_extensions` only seeds MUSIC + VIDEO + UNKNOWN, never a COMPANION extension. The discrepancy is silent.

**Fix:** Pick one filter and use it for both paths. Either:

(a) Restrict `scan_directory` to MUSIC + VIDEO (matches watcher and the auto-enqueue gate in `agent_files.py:140`):

```python
# src/phaze/tasks/scan.py
_EXTRACTABLE: frozenset[FileCategory] = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})

# inside scan_directory loop:
if _classify(filename) not in _EXTRACTABLE:
    continue
```

(b) Or expand the watcher's `_EXTRACTABLE` to include COMPANION (and document the broader scope in `27-CONTEXT.md`).

Add a regression test in `tests/test_agent_watcher/test_observer.py` and `tests/test_tasks/test_scan_directory.py` that asserts the chosen category set explicitly, e.g.:

```python
def test_observer_drops_companion_files() -> None:
    handler.on_created(FileCreatedEvent(src_path="/foo/cover.jpg"))
    assert loop.call_soon_threadsafe.call_count == 0
```

## Warnings

### WR-01: Path-traversal substring check rejects legitimate filenames containing `..` (false positive)

**File:** `src/phaze/routers/pipeline_scans.py:142`
**Issue:** `if ".." in joined: return 400` uses simple substring containment, not a path-component check. Any filename or directory that contains the literal substring `..` is rejected — e.g., `subpath="..notes"` (a legitimate "started with three dots" filename), `subpath="folder/.../file.mp3"`, or even `joined="/data/music/...thinking.mp3"`. `".." in "..."` is `True` in Python.

The intent is to block `../` path-traversal sequences, but the substring check fires on any literal `..` anywhere in the path. Real legitimate paths (think `…` rendered as three dots, or torrent-archive directory names like `Album...Live`) will 400.

**Fix:** Check for `..` as a path **component**, not a substring:

```python
from pathlib import PurePosixPath

parts = PurePosixPath(joined).parts
if ".." in parts:
    return templates.TemplateResponse(
        ...
        name="pipeline/partials/scan_submit_error.html",
        context={"request": request, "error_message": "Subpath must not contain '..' path traversal."},
        status_code=status.HTTP_400_BAD_REQUEST,
    )
```

Add a unit test covering both directions: `subpath="..notes/file.mp3"` must pass; `subpath="../../etc/passwd"` must 400.

### WR-02: Watcher leaks `httpx.AsyncClient` when `whoami_with_retry` exits non-zero

**File:** `src/phaze/agent_watcher/__main__.py:104-105`
**Issue:** `main()` constructs the agent client BEFORE entering the `try/finally` that closes it:

```python
client = construct_agent_client(cfg)
identity = await whoami_with_retry(client)   # may raise RuntimeError
...
try:
    await _sweep_loop(...)
finally:
    ...
    await client.close()
```

When `whoami_with_retry` raises (budget exhausted on persistent 5xx, or short-circuit on `AgentApiAuthError`), the exception propagates to `asyncio.run(main())` and the `try/finally` is never entered. The underlying `httpx.AsyncClient` is never closed. Python emits `ResourceWarning: unclosed transport`. In the production path the container exits immediately afterward and the OS reclaims the socket — but the warning surface and the deterministic-close contract documented in the module docstring are both violated.

**Fix:** Use `async with` or move the client construction inside the `try`:

```python
async def main() -> None:
    cfg = get_settings()
    ...
    client = construct_agent_client(cfg)
    try:
        identity = await whoami_with_retry(client)
        ...
        observer = Observer()
        ...
        try:
            await _sweep_loop(...)
        finally:
            observer.stop()
            observer.join()
    finally:
        await client.close()
```

Add a regression test in `tests/test_agent_watcher/test_main.py` that asserts `fake_client.close.assert_awaited_once()` even when `whoami_with_retry` raises (currently only the happy-path graceful shutdown is asserted).

### WR-03: Watcher byte-path decoding hardcodes UTF-8, dropping legitimate filenames on non-UTF-8 filesystems

**File:** `src/phaze/agent_watcher/observer.py:60-65`
**Issue:** The docstring at line 54 promises "Decode bytes via the filesystem encoding", but the implementation hardcodes `"utf-8"` with `errors="strict"`:

```python
if isinstance(src_path, bytes):
    try:
        path_str = src_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        logger.debug("watcher: dropping undecodable bytes path; len=%d", len(src_path))
        return
```

On a host whose `LANG` / filesystem encoding is not UTF-8 (some legacy Linux ext4 mounts, older NFS exports, or filesystems containing pre-UTF-8 Latin-1 filenames), legitimate music files will be **silently dropped at DEBUG level**. The user has no signal that files are vanishing.

**Fix:** Use `os.fsdecode` (which honors `sys.getfilesystemencoding()`), or escalate the log to WARNING so the drop is at least visible:

```python
import os
...
if isinstance(src_path, bytes):
    try:
        path_str = os.fsdecode(src_path)
    except (UnicodeDecodeError, ValueError):
        logger.warning("watcher: dropping path; cannot decode via fs encoding; len=%d", len(src_path))
        return
```

`os.fsdecode` uses `surrogateescape` by default, which preserves un-decodable bytes through to logs without dropping the file. The downstream POST may fail, but the path becomes diagnosable.

### WR-04: `_resolve_chunk_size` relies on `lru_cache` state that leaks across tests, masking real bugs

**File:** `src/phaze/tasks/scan.py:68-73`
**Issue:** `_resolve_chunk_size` reads `get_settings()` (which is `@lru_cache(maxsize=1)`). Under `PHAZE_ROLE=control` (the default in most test environments), it falls through to `_DEFAULT_SCAN_CHUNK_SIZE = 500`. Production runs under `PHAZE_ROLE=agent` and gets `AgentSettings.scan_chunk_size`. The tests in `tests/test_tasks/test_scan_directory.py` do NOT set `PHAZE_ROLE=agent`, so they exercise the **fallback path**, not the production code path.

`tests/test_config_role_split.py::_clear_settings_cache` clears the cache at session boundaries, but if a `test_scan_directory.py` test runs after `test_config_role_split.py::test_get_settings_returns_agent_settings_when_role_is_agent` (which sets `PHAZE_ROLE=agent` via monkeypatch), the cache is invalidated by pytest's teardown of monkeypatch but the lru_cache may still hold the prior return value depending on ordering.

Result: the production `AgentSettings.scan_chunk_size` env override is **untested** in the chunking tests — `test_scan_directory_chunks_at_500` asserts behavior only against the hardcoded `_DEFAULT_SCAN_CHUNK_SIZE` constant.

**Fix:** Add a fixture that sets `PHAZE_ROLE=agent` + clears the `lru_cache` for `scan_directory` tests, and add at least one test that overrides `PHAZE_SCAN_CHUNK_SIZE=100` via monkeypatch and asserts chunks of 100:

```python
@pytest.fixture(autouse=True)
def _agent_env(monkeypatch):
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
    from phaze.config import get_settings
    get_settings.cache_clear()

async def test_scan_directory_honors_agent_settings_chunk_size(tmp_path, monkeypatch):
    monkeypatch.setenv("PHAZE_SCAN_CHUNK_SIZE", "3")
    from phaze.config import get_settings
    get_settings.cache_clear()
    ... # write 7 files, assert chunks of 3, 3, 1
```

### WR-05: `scan_path` validator does NOT reject `scan_root` values that are not in `agent.scan_roots` — only the joined path is validated

**File:** `src/phaze/routers/pipeline_scans.py:165`
**Issue:** The form accepts an arbitrary `scan_root` (from HTTP POST body), and validation only checks whether `joined = scan_root + "/" + subpath` falls inside one of `agent.scan_roots`. If `agent.scan_roots = ["/data/music"]` and the operator POSTs `scan_root="/data"` + `subpath="music/foo"`, the prefix-check passes because `joined.startswith("/data/music/")` is `True`. The handler then creates a `ScanBatch` row with `scan_path="/data/music/foo"` — semantically correct, but `scan_root="/data"` was never actually authorized by the configuration.

This is benign as long as the joined-path check is correct, but it diverges from the documented invariant in the planning notes (`scan_root rejected when not in selected agent's scan_roots (422)`) and creates a surprising mode where the audit log shows `scan_root="/data"` was used to ingest `/data/music/foo`. There is no test in `test_pipeline_scans.py` that pins down the expected behavior.

**Fix:** Either tighten the validation to require `form.scan_root in agent.scan_roots`:

```python
if form.scan_root not in agent.scan_roots:
    return templates.TemplateResponse(
        ...
        context={"request": request, "error_message": "Selected scan root is not configured for this agent."},
        status_code=status.HTTP_400_BAD_REQUEST,
    )
```

Or update the planning artifact to reflect the looser "joined-path-must-be-under-one-of-the-roots" contract and add a test that pins it down both ways.

### WR-06: `pipeline_scans.trigger_scan` rollback on enqueue failure has no isolation if `session.delete` also raises

**File:** `src/phaze/routers/pipeline_scans.py:194-202`
**Issue:** The rollback path is:

```python
try:
    await request.app.state.task_router.enqueue_for_agent(...)
except Exception:
    await session.delete(batch)
    await session.commit()
    return templates.TemplateResponse(..., status_code=503)
```

If `session.delete()` or `session.commit()` raises (e.g., the same network issue that broke the enqueue also took out Postgres, or the session is now in a tainted state because the original commit succeeded but the connection has since dropped), the exception escapes the handler and FastAPI returns a generic 500 — losing the documented "503 + scan_submit_error.html" copy. The orphan `ScanBatch` row stays RUNNING forever (no agent will ever PATCH it, because nothing was enqueued).

**Fix:** Wrap the rollback in its own try/except, or use `session.rollback()` after the enqueue catch and leave the row in a FAILED state instead of deleting it (so the operator can see the failed batch in Recent Scans):

```python
except Exception:
    batch.status = ScanStatus.FAILED.value
    batch.error_message = "controller could not enqueue scan to agent worker"
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("scan trigger: rollback also failed for batch=%s", batch.id)
    return templates.TemplateResponse(
        ...
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
```

This also gives the operator a visible failure in Recent Scans instead of a silently-deleted batch.

### WR-07: `observer.join()` in watcher shutdown has no timeout — a wedged watchdog thread will hang the container exit

**File:** `src/phaze/agent_watcher/__main__.py:140-142`
**Issue:** The graceful-shutdown sequence is:

```python
finally:
    observer.stop()
    observer.join()         # no timeout -- can block indefinitely
    await client.close()
```

`watchdog.observers.Observer.join()` (a `threading.Thread.join`) blocks forever by default. If the watchdog thread is wedged on a slow/hung filesystem (NFS stall, FUSE deadlock), `docker compose down` cannot stop the container without a forceful SIGKILL, defeating the graceful-shutdown contract.

**Fix:** Pass a timeout and log if exceeded:

```python
finally:
    observer.stop()
    observer.join(timeout=10.0)
    if observer.is_alive():
        logger.warning("watcher: observer thread did not stop within 10s; abandoning")
    await client.close()
```

## Info

### IN-01: `scan_path_picker.html` empty-state branch for "agent defined but no scan_roots" is unreachable

**File:** `src/phaze/templates/pipeline/partials/scan_path_picker.html:20-24` AND `src/phaze/routers/pipeline_scans.py:74-79`
**Issue:** The template branches on three conditions: `agent is not defined` → placeholder; `agent is none` → placeholder; `agent.scan_roots empty` → yellow-surface "no scan roots configured" copy. But the controller (`pipeline_scans.py:75`) already substitutes `agent = None` whenever `not agent.scan_roots`, collapsing the third branch into the second. The "yellow surface" branch is dead code from this route.

**Fix:** Either pass the real agent to the template even when `scan_roots` is empty (so the yellow-surface copy renders), or delete the dead branch from the template:

```python
# pipeline_scans.py
if agent is None or agent.revoked_at is not None:
    return templates.TemplateResponse(..., context={"agent": None}, ...)
# leave scan_roots-empty path untouched so the template can branch on it
return templates.TemplateResponse(..., context={"agent": agent}, ...)
```

### IN-02: Dashboard handler mutates ORM model instances with `_agent_name` / `_elapsed_seconds` transient attributes

**File:** `src/phaze/routers/pipeline.py:155-158`
**Issue:** The handler sets non-Mapped attributes on `ScanBatch` instances inside a loop, with `# type: ignore[attr-defined]` annotations. This works because SQLAlchemy ignores leading-underscore attrs, but it's fragile (a future shift to `MappedAsDataclass(slots=True)` would break it) and leaks template logic into the router.

**Fix:** Build a parallel list of view-dicts in the handler:

```python
recent_scans = [
    {
        "batch": b,
        "agent_name": agent_name_by_id.get(b.agent_id, b.agent_id),
        "elapsed_seconds": int((now - b.created_at).total_seconds()) if b.created_at else None,
    }
    for b in recent_scans_rows
]
```

And update `recent_scans_table.html` to read `row.batch.scan_path`, `row.agent_name`, etc.

### IN-03: `_SCAN_TRANSITIONS` defensive LIVE-check is dead code (Pydantic Literal rejects "live" at 422 before the handler runs)

**File:** `src/phaze/routers/agent_scan_batches.py:97-102`
**Issue:** The handler has:

```python
if body.status is not None and ScanStatus(body.status) == ScanStatus.LIVE:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cannot transition to LIVE")
```

But `ScanBatchPatch.status: Literal["running", "completed", "failed"] | None` means Pydantic rejects `"live"` at validation time (422). This branch is unreachable. The code comment acknowledges this ("Defensive: LIVE is rejected at the Literal layer (422)") but leaves the branch in place "for any future schema widening".

This is fine as a documentation aid but should be marked clearly. Better: convert it to an assertion so a future schema change that allows "live" surfaces loudly:

**Fix:**
```python
if body.status is not None:
    new_status = ScanStatus(body.status)
    assert new_status != ScanStatus.LIVE, "Pydantic Literal must reject 'live' before handler"
```

### IN-04: `agent_files.py` partial-enqueue failure mode is silently absorbed, no operator surface

**File:** `src/phaze/routers/agent_files.py:142-161`
**Issue:** If 100 files are INSERTed and the first 50 enqueue succeeds but the 51st raises, the loop logs at exception level and continues. The response body returns `enqueued=50`. The remaining 50 INSERTed FileRecord rows are in DISCOVERED state with no SAQ job and no UI surface to re-enqueue them — the comment "operator can re-enqueue manually via Phase 27's UI on retryable failure" describes a workflow that doesn't exist for the metadata-extraction queue (Phase 27 UI only triggers scan, not metadata extraction).

**Fix:** Either (a) accept this as a documented operational concern and add a Recent Scans badge for "files awaiting metadata extraction", or (b) re-raise the first enqueue failure to surface a 503 to the agent and let it retry (idempotency on `(agent_id, original_path)` makes this safe). Track in a follow-up phase.

### IN-05: `compute_sha256` opens the file synchronously inside `asyncio.to_thread`, but `stat()` is a separate to_thread call → TOCTOU window

**File:** `src/phaze/agent_watcher/poster.py:74-75`
**Issue:** Stat and SHA-256 are two separate thread offloads:

```python
file_size = await asyncio.to_thread(lambda: p.stat().st_size)
sha256 = await asyncio.to_thread(compute_sha256, p)
```

Between the two, the file could be truncated, replaced, or unlinked. The settle window (`settle_period=10s`) is meant to mitigate this, but a rapid second write inside the settle window followed by a sweep does land both stat and hash with no consistency guarantee. Result: a record with `file_size=N` (old size) and `sha256=hash_of_new_content`. Cross-agent dedup on sha256 will then look up a stale size.

**Fix:** Stat once, hash once, inside a single `asyncio.to_thread` block to minimize the race window:

```python
def _stat_and_hash(p: Path) -> tuple[int, str]:
    size = p.stat().st_size
    return size, compute_sha256(p)

try:
    file_size, sha256 = await asyncio.to_thread(_stat_and_hash, p)
except OSError as exc:
    logger.debug("watcher: path vanished before post; dropping path=%s err=%s", path, exc)
    return
```

This is not a security issue; it's a data-consistency hardening.

---

_Reviewed: 2026-05-13T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
