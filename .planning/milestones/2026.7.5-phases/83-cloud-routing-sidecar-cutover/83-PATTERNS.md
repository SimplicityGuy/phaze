# Phase 83: Cloud-Routing Sidecar Cutover - Pattern Map

**Mapped:** 2026-07-09
**Files analyzed:** 16 (10 source create/modify + 6 test create/modify)
**Analogs found:** 16 / 16 (every donor verified against HEAD of `SimplicityGuy/phaze-83`)

> **Headline:** This phase is **wiring and anchor-swapping, not new construction.** The CAS pattern, the
> advisory lock, the `on_conflict_do_update` upsert, the backfill SQL, both clause builders, the DELETE
> idiom, and every test scaffold already exist in-tree. Each row below names the **exact donor** and the
> **delta** so the planner and executor copy rather than invent. Re-spelling a locked predicate
> (`inflight_clause` / `domain_completed_clause`) breaks the DERIV-04 equivalence test — never re-write one.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `services/backends.py` — new `hold_awaiting_cloud()` helper (D-02) | service | transform / upsert | `ComputeAgentBackend.dispatch` upsert (`backends.py:326-339`) | exact (same file, same upsert idiom) |
| `routers/pipeline.py` — `trigger_analysis` hold path (D-01) | route | request-response | the new `hold_awaiting_cloud()` helper | exact (call-site swap) |
| `services/pipeline.py` — `get_cloud_staging_candidates` drain query (D-05/D-06/D-07) | service | CRUD / query | current query `pipeline.py:1248-1264` + `stage_status.py:150,170` builders | exact |
| `services/pipeline.py` — `get_awaiting_cloud_count` (D-15) | service | CRUD / count | itself `pipeline.py:1112-1123` + the drain clause | exact |
| `routers/agent_s3.py` — `report_upload_failed` CAS + no-op + lock (D-09/D-10/D-11/D-03) | route (callback) | event-driven / CAS | `report_push_mismatch` (`agent_push.py:240,258-293`) | exact (sibling handler) |
| `routers/agent_push.py` — `report_push_mismatch` spill re-stamp (D-03) | route (callback) | event-driven / CAS | itself `agent_push.py:279-283` | exact (1-line value change) |
| `routers/agent_analysis.py` — D-14 awaiting-row reaper | route (callback) | event-driven / delete | `scan_deletion.py:110` `delete(CloudJob).where(...)` | role+flow match |
| `services/backends.py` — `LocalBackend.dispatch` (D-13, keep flip) | service | transform | itself `backends.py:210-241` | exact (no change / verify) |
| `services/backend_selection.py` — staleness clock move (D-07) | service | transform | itself `backend_selection.py:80-119` | exact (signature tweak) |
| `alembic/versions/034_*.py` — repair backfill migration (D-04) | migration | batch / INSERT…SELECT | `032:_BACKFILL_CLOUD_AWAITING` (`032:96-102`) | exact |
| `tests/agents/routers/test_agent_s3.py` — SC#2 CAS + D-11 concurrency | test | regression | `test_agent_push.py` T-73-13 (`:865,983`) + `test_agent_s3.py:332` | exact |
| `tests/integration/test_<sc3_drain>.py` — SC#3 two-tick gate | test | integration | `tests/analyze/tasks/test_release_awaiting_cloud.py` | role match |
| `tests/integration/test_migrations/test_migration_034_*.py` | test | migration | `test_migration_032_additive_schema.py` | exact |
| `tests/integration/test_shadow_compare.py` — `awaiting_cloud` green | test | integration | itself | exact |
| `tests/shared/routers/test_pipeline.py` — `get_awaiting_cloud_count` | test | unit | itself | exact |
| `tests/agents/routers/test_agent_analysis.py` — D-14 reaper | test | unit | itself (`_seed_cloud_job` helpers) | exact |

---

## Pattern Assignments

### `services/backends.py` — new `hold_awaiting_cloud()` helper (service, upsert) — D-02

**Analog:** `ComputeAgentBackend.dispatch`'s upsert, `services/backends.py:326-339`. Same file already
imports `pg_insert`, `CloudJob`, `CloudJobStatus`, `FileState`, `uuid` — no new imports.

**Upsert donor** (`backends.py:326-339`) — copy the shape, change the `set_` and the status:
```python
stmt = pg_insert(CloudJob).values(
    id=uuid.uuid4(),                       # stamp PK explicitly (CR-01 defensive)
    file_id=file.id,
    backend_id=self.id,
    s3_key=None,
    status=CloudJobStatus.SUBMITTED.value, # <-- helper writes AWAITING.value instead
)
stmt = stmt.on_conflict_do_update(
    index_elements=["file_id"],            # uq_cloud_job_file_id -> plain INSERT unsafe on the spill case
    set_={"backend_id": stmt.excluded.backend_id, "status": stmt.excluded.status},
)
await session.execute(stmt)
```

**Delta (D-02 helper):** writes `status=AWAITING.value`, `set_={"status": ..., "attempts": ...}`
(the spill path needs `attempts` in `set_` to re-stamp a terminalized row's spent budget), stamps
`file.state = FileState.AWAITING_CLOUD` (D-00c dual-write), and **NEVER commits** (see discipline below).
RESEARCH's target shape (`83-RESEARCH.md:311-322`):
```python
async def hold_awaiting_cloud(session: AsyncSession, file: FileRecord, *, attempts: int = 0) -> None:
    """Stamp AWAITING_CLOUD + upsert the cloud_job awaiting row in the CALLER'S txn. NEVER commits."""
    file.state = FileState.AWAITING_CLOUD  # D-00c dual-write; dies in Phase 90
    stmt = pg_insert(CloudJob).values(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.AWAITING.value, attempts=attempts)
    stmt = stmt.on_conflict_do_update(index_elements=["file_id"], set_={"status": stmt.excluded.status, "attempts": stmt.excluded.attempts})
    await session.execute(stmt)
```

**Dispatch discipline (MUST honor — Shared Pattern below):** the helper mutates in the caller's session
and **never commits**. See `backends.py:146-152` (the `Backend.dispatch` protocol docstring) and
`release_awaiting_cloud.py:257-264` (the single post-loop commit). A commit inside the helper would drop
the tick advisory lock and re-open the over-stage class (Landmine L1).

**Reused by three call sites (one writer, not three copies):** the hold path (`routers/pipeline.py:346`),
the `report_upload_failed` over-cap spill (`agent_s3.py:190-195`), and the `report_push_mismatch` over-cap
spill (`agent_push.py:279-283`). The spill callers pass `attempts=settings.cloud_submit_max_attempts`
(D-03 budget-spent marker).

---

### `routers/pipeline.py` — `trigger_analysis` hold path (route, request-response) — D-01

**Analog:** the new `hold_awaiting_cloud()` helper.

**Current gap** (`routers/pipeline.py:339-356`) — the bare state write with **no `CloudJob` import**:
```python
for file, duration in files_with_duration:
    is_long = cloud_enabled and duration is not None and duration >= threshold_sec
    if is_long:
        file.state = FileState.AWAITING_CLOUD   # <-- D-01 GAP: writes state, NO sidecar row
        held += 1
    ...
if held:
    await session.commit()                       # this file DOES commit (it is the top of the hold, not a dispatch)
```

**Delta:** replace `file.state = FileState.AWAITING_CLOUD` with
`await hold_awaiting_cloud(session, file)` (attempts defaults to 0 — a fresh hold has spent no budget).
The existing post-loop `await session.commit()` at `:356` stays (this is the writer's own commit boundary,
NOT a dispatch loop). Add the import of the helper.

---

### `services/pipeline.py` — `get_cloud_staging_candidates` drain query (service, query) — D-05/D-06/D-07

**Analog:** the current query at `pipeline.py:1248-1264` + the LOCKED clause builders at
`stage_status.py:150` (`inflight_clause`) and `:170` (`domain_completed_clause`).

**Current query** (`pipeline.py:1257-1263`) — the `state`-read to be replaced:
```python
stmt = (
    select(FileRecord)
    .where(FileRecord.state == FileState.AWAITING_CLOUD)   # <-- SC#1 routing read to remove
    .order_by(FileRecord.created_at.asc())
    .limit(limit)
    .with_for_update(skip_locked=True)
)
```

**Clause-builder donors — REUSE VERBATIM, never re-spell** (`stage_status.py:150-167,170-...`):
```python
def inflight_clause(stage: Stage) -> ColumnElement[bool]:
    func_name = STAGE_TO_FUNCTION.get(stage.value)
    if func_name is None:
        return false()
    return exists(select(SchedulingLedger.key).where(SchedulingLedger.key == func.concat(func_name + ":", cast(FileRecord.id, String))))

def domain_completed_clause(stage: Stage) -> ColumnElement[bool]:  # DONE ∨ (FAILED ∧ FAILURE_IS_TERMINAL[stage])
    ...  # reuses done_clause / failed_clause verbatim; drift-locked by tests/integration/test_stage_status_equivalence.py
```
Both correlate to `FileRecord.id` **inside** `exists(select(...).where(X.file_id == FileRecord.id))`, so
adding a `.join(CloudJob)` to the outer query does NOT change which entity they resolve against
(RESEARCH Pitfall 3, MEDIUM-confidence — the SC#3 test's `EXPLAIN` is the safety net).

**Target query** (`83-RESEARCH.md:292-309`):
```python
from phaze.enums.stage import Stage
from phaze.services.stage_status import inflight_clause, domain_completed_clause

stmt = (
    select(FileRecord)
    .join(CloudJob, CloudJob.file_id == FileRecord.id)   # INNER: a candidate MUST have an awaiting row
    .where(
        CloudJob.status == CloudJobStatus.AWAITING.value,   # D-05 conjunct 1
        ~inflight_clause(Stage.ANALYZE),                    # D-05 conjunct 2 (survives the rolled-back tick)
        ~domain_completed_clause(Stage.ANALYZE),            # D-05 conjunct 3 (Phase 81 twin; excludes terminally-failed local)
    )
    .order_by(FileRecord.created_at.asc())               # D-07 FIFO on immutable discovery order (byte-identical to today)
    .limit(limit)
    .with_for_update(of=CloudJob, skip_locked=True)      # D-06 lock the candidacy table, not files
)
```

**Pitfall 2 (`83-RESEARCH.md:243-247`):** INNER (not outer) join is required — Postgres rejects
`FOR UPDATE OF cloud_job` against the nullable side of an outer join. The `of=CloudJob` moves the lock to
the table the deciding column now lives on (EvalPlanQual re-checks the locked-table qual).

**D-07 staleness clock:** the drain loop reads `file.updated_at` today (`release_awaiting_cloud.py:210-214`,
`backend_selection.py:108`). Surface `cloud_job.updated_at` from this candidate query (extra column) or via
a per-candidate read like `_cloud_attempts_for` (`release_awaiting_cloud.py:88-95,205`), and pass it into
`select_backend` — see `backend_selection.py` entry below.

---

### `services/pipeline.py` — `get_awaiting_cloud_count` (service, count) — D-15

**Analog:** itself, `pipeline.py:1112-1123`, keeping the `_safe_count` degrade-safe wrapper.

**Current** (`pipeline.py:1119-1123`) — the `state`-read count to re-anchor:
```python
return await _safe_count(
    session,
    select(func.count(FileRecord.id)).where(FileRecord.state == FileState.AWAITING_CLOUD),
    node="awaiting_cloud",
)
```

**Delta:** derive from the **same** clause the drain uses so card and drain cannot disagree — count the
genuinely-parked (non-locally-dispatched) rows:
```python
select(func.count(CloudJob.id)).where(
    CloudJob.status == CloudJobStatus.AWAITING.value,
    ~inflight_clause(Stage.ANALYZE),
    ~domain_completed_clause(Stage.ANALYZE),
)
```
Keep `_safe_count(..., node="awaiting_cloud")` (poll never 500s). **Do NOT touch** `get_pushing_count`
(`:1206`) / `get_pushed_count` (`:1224`) — they are an unowned Phase-90 blocker (Deferred).

---

### `routers/agent_s3.py` — `report_upload_failed` (route, callback CAS) — D-09/D-10/D-11/D-03

**Analog:** `report_push_mismatch`, `routers/agent_push.py:240` (advisory lock) and `:258-293` (guarded
spill + full no-op). Same-shaped sibling handler in the same package. The file already imports
`cast`, `CursorResult`, `CloudJob`, `CloudJobStatus`, `func`(? verify), `update`.

**D-11 advisory-lock donor** (`agent_push.py:240`, with its full rationale at `:230-239`) — copy verbatim,
place BEFORE the attempt RMW read at `agent_s3.py:176`:
```python
# A row lock self-deadlocks against the before_enqueue hook's own session (stage_file_to_s3 upserts the
# SAME s3_upload:<file_id> ledger row). The advisory lock lives in a different lock space, so the hook's
# upsert never blocks on it; a second concurrent /failed waits until we commit -> RMW serialized.
await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(ledger_key))))
```
Note: `agent_s3.py` must import `func` from sqlalchemy (currently imports `select, update`; add `func`).

**D-09/D-10 CAS + full-no-op donor** (`agent_push.py:258-272`) — the guarded spill, rowcount==0 → clean
idempotent 200 with **no** terminalization / **no** ledger clear:
```python
res = cast(
    "CursorResult[Any]",
    await session.execute(
        update(FileRecord).where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING).values(state=FileState.AWAITING_CLOUD)
    ),
)
if res.rowcount == 0:
    await session.commit()
    return PushMismatchResponse(file_id=file_id, cleared=False)   # <-- FULL no-op: no cloud_job write, no ledger clear
```

**Current bug** (`agent_s3.py:190-195`) — the **unguarded** write (SC#2 bug at `:195`):
```python
await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.FAILED.value, cloud_phase=None, attempts=settings.cloud_submit_max_attempts))
await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.AWAITING_CLOUD))  # <-- :195 NO CAS
```

**Delta (the anchor swap, D-09):** the CAS anchors on `cloud_job.status`, not `FileRecord.state`
(`83-RESEARCH.md:184-194`):
```python
res = cast("CursorResult[Any]", await session.execute(
    update(CloudJob)
    .where(CloudJob.file_id == file_id, CloudJob.status.in_([CloudJobStatus.UPLOADING.value, CloudJobStatus.UPLOADED.value]))
    .values(status=CloudJobStatus.AWAITING.value, cloud_phase=None, attempts=settings.cloud_submit_max_attempts)  # D-03 re-stamp FAILED->awaiting
))
if res.rowcount == 0:
    await session.commit()   # D-10 FULL no-op: NO FileRecord write, NO abort/delete_staged_object, NO clear_ledger_entry
    return UploadFailedResponse(file_id=file_id, cleared=False)
# rowcount != 0: NOW the FileRecord dual-write + S3 cleanup + clear_ledger_entry (all gated behind the rowcount)
```
D-03: the spill re-stamps to `status='awaiting'` (was `FAILED`), retaining `attempts` spent and
`cloud_phase=None` — reuse the D-02 helper here rather than hand-copy. D-10 safety: the S3 cleanup and
`clear_ledger_entry` (currently `agent_s3.py:196-204`) move **inside** the `rowcount != 0` branch —
`_delete_staged_object_if_cloud` owns the object on both analyze-terminal seams (KSTAGE-04 still holds).

**Preserve `report_uploaded`'s existing rowcount guards** (`agent_s3.py:105-134`) — they are the working
CAS donor; the redundant `FileRecord.state == PUSHING` guard at `:128` is a Deferred belt-and-braces
(do NOT retire in scope).

---

### `routers/agent_push.py` — `report_push_mismatch` spill re-stamp (route, callback CAS) — D-03

**Analog:** itself, `agent_push.py:279-283`.

**Current** (`agent_push.py:279-283`) — terminalizes to `FAILED`:
```python
await session.execute(
    update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.FAILED.value, attempts=settings.cloud_submit_max_attempts)
)
```

**Delta:** `FAILED → awaiting` so the hard invariant `AWAITING_CLOUD ⇒ status='awaiting'` holds after the
spill (`83-RESEARCH.md:93-97`). Keep `attempts=settings.cloud_submit_max_attempts` (budget-spent → routes
to local). Reuse the D-02 helper (this is "the D-02 helper applied on the spill path"). The CAS guard at
`:258-272` (gate on `state == PUSHING`) is UNCHANGED — it is the D-10 donor for the `agent_s3.py` change,
not itself modified.

---

### `routers/agent_analysis.py` — D-14 awaiting-row reaper (route, callback delete)

**Analog:** the DELETE idiom at `scan_deletion.py:110` (`delete(CloudJob).where(...)`, cast to
`CursorResult`), joined at the two seams that already own cloud cleanup.

**Seam donors** — the two `_delete_staged_object_if_cloud` call sites the reaper joins:
- `put_analysis` success path, `agent_analysis.py:262-266`:
  ```python
  await _delete_staged_object_if_cloud(session, file_id)   # existing seam
  await session.commit()
  ```
- `report_analysis_failed` terminal path, `agent_analysis.py:379-382` (same shape).

**DELETE idiom donor** (`scan_deletion.py:110,117-119`):
```python
(CloudJob.__tablename__, delete(CloudJob).where(CloudJob.file_id.in_(files_of_batch)))
...
result = cast("CursorResult[Any]", await session.execute(stmt.execution_options(synchronize_session=False)))
```

**Delta:** at each seam, in the transaction already open, add:
```python
await session.execute(delete(CloudJob).where(CloudJob.file_id == file_id, CloudJob.status == CloudJobStatus.AWAITING.value))
```
A cloud-analyzed file's row is `SUCCEEDED`/`RUNNING`, so the `status='awaiting'` filter leaves it
untouched (only the inert LocalBackend hold-over row is reaped — the D-14 index-growth defense). Verify
`agent_analysis.py` imports `delete`, `CloudJob`, `CloudJobStatus`.

---

### `services/backends.py` — `LocalBackend.dispatch` (service, transform) — D-13

**Analog / delta:** itself, `backends.py:210-241`. **No change** — the `file.state = FileState.LOCAL_ANALYZING`
flip at `:234` is RETAINED (D-00c dual-write). Its old consumer (the `state == AWAITING_CLOUD` drain
predicate) is replaced by the D-05 conjunct, but keeping the flip prevents `get_awaiting_cloud_count`
inflation. `LocalBackend.dispatch` stays a **no-`cloud_job`-row writer** (writes and deletes no row); the
inert `awaiting` row is reaped by D-14, not here. `ComputeAgentBackend.dispatch` (`:310-356`) and
`KueueBackend.dispatch` (`:405-...`) already promote the `awaiting` row via `on_conflict_do_update` — **no
change needed** for the retirement of the awaiting row on the cloud paths.

---

### `services/backend_selection.py` — staleness clock move (service, transform) — D-07

**Analog:** itself, `backend_selection.py:80-119` (`select_backend`).

**Current** (`backend_selection.py:108`):
```python
waited = (now - file.updated_at).total_seconds() >= cfg.cloud_spill_to_local_after_seconds
```

**Delta:** once Phase 90 drops the dual-written `file.state = AWAITING_CLOUD`, nothing stamps
`file.updated_at` at lane entry, silently breaking `cloud_spill_to_local_after_seconds`. Pass the awaiting
row's `updated_at` explicitly (like `cloud_attempts` is already passed explicitly per the signature note at
`:27-31`) rather than reading it off `file`. The D-02 helper's upsert stamps `cloud_job.updated_at` at hold
time (TimestampMixin `onupdate`), giving the clock a Phase-90-durable home. Keep FIFO on
`FileRecord.created_at` (immutable — do NOT move FIFO to `cloud_job.created_at`, it changes ordering).

---

### `alembic/versions/034_*.py` — repair backfill migration (migration, batch) — D-04

**Analog:** `032:_BACKFILL_CLOUD_AWAITING`, `alembic/versions/032_add_derived_status_schema.py:96-102`.
Migrations are **SYNC** (`def upgrade()`, plain `op.execute(...)`; only `env.py` is async).

**Backfill donor** (`032:96-102`) — re-run verbatim:
```python
INSERT INTO cloud_job (id, file_id, status)
SELECT gen_random_uuid(), f.id, 'awaiting'
FROM files f
WHERE f.state = 'awaiting_cloud'
ON CONFLICT (file_id) DO NOTHING
```

**Delta:** `034` chains `down_revision="033"` (head is `033`, verified). Static parameter-free SQL (no
interpolation). Touches **no ORM-mapped schema** — the `'awaiting'` CHECK value and `ix_cloud_job_awaiting`
already shipped in `032` (`models/cloud_job.py:114,122`; `032:144-156`), so no `__table_args__` mirroring
is needed and `alembic revision --autogenerate` stays empty (77 D-01 empty-diff contract holds trivially).
`downgrade()`: no-op or a documented-lossy `DELETE FROM cloud_job WHERE status='awaiting'`.

**Phase-90 renumber (doc-only, `034 → 035`):** grep `\b034\b` across `.planning/` — verified references at
`ROADMAP.md` (21,25,36,281,497,504,506), `REQUIREMENTS.md:98`, `PARALLEL-ENRICH-DAG-DESIGN.md` (352,356).
Deferred to Phase 90 (mechanical); this phase only ADDS `034`.

---

### Test files

#### `tests/agents/routers/test_agent_s3.py` — SC#2 CAS no-op + D-11 concurrency (test, regression)

**Analog (CAS no-op):** `test_agent_s3.py:332` `test_uploaded_lost_flip_race_is_idempotent_noop` — seeds a
`cloud_job` at an already-advanced status via `_seed_cloud_job(session, file_id, status=...)`
(`:205-227`), posts the callback, asserts the winner's row stands. Copy that scaffold: seed
`cloud_job` at `RUNNING`/`SUCCEEDED`, POST `/failed`, assert the row is unchanged (still RUNNING/SUCCEEDED),
`FileRecord.state` NOT clobbered to `AWAITING_CLOUD`, no S3 abort/delete, no ledger clear, `cleared=False`.

**Analog (D-11 concurrency):** `test_agent_push.py:865` `test_mismatch_concurrent_no_lost_update` and
`:983` `test_mismatch_real_enqueue_hook_does_not_deadlock` (T-73-13). These drive genuine contention via a
monkeypatched park-point that holds the advisory lock open while a second request is launched against the
real port-5433 engine, asserting `push_attempt == 2` (no lost increment) and no deadlock. Mirror for
`s3_upload_attempt` on the `s3_upload:<file_id>` ledger key. `_seed_ledger(..., attempt=...)`
(`test_agent_s3.py:229`) seeds the counter.

**Bucket:** `agents`. Must pass via `just test-bucket agents` **in isolation**.

#### `tests/integration/test_<sc3_drain>.py` — SC#3 two-tick gate (test, integration, HARD GATE)

**Analog:** `tests/analyze/tasks/test_release_awaiting_cloud.py`. Reuse its scaffold: `_make_ctx(async_engine,
router)` (`:107-118`) builds the ctx; `DedupFakeQueue`/`DedupFakeTaskRouter`/`seed_active_agent` from
`tests/_queue_fakes`; a fake `Backend` impl (`:76`) with a stub `dispatch`. Drive **two sequential**
`stage_cloud_window(ctx)` ticks across the three outcomes (local dispatch; rolled-back tick with a
committed ledger row; terminally-failed local analyze), asserting each file dispatched exactly once and
never to a cloud backend after a local dispatch. Optionally `EXPLAIN` the drain to assert
`ix_cloud_job_awaiting` usage (MEDIUM value — the D-14 reaper is the real defense).

**Bucket:** `integration`. Must pass via `just test-bucket integration` **in isolation**.

#### `tests/integration/test_migrations/test_migration_034_*.py` (test, migration)

**Analog:** `tests/integration/test_migrations/test_migration_032_additive_schema.py` — the per-migration
pattern: `_build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)`, `upgrade_to(cfg, "033")`,
`_seed_corpus(engine)` (seed `AWAITING_CLOUD` files with NO `cloud_job` row), `upgrade_to(cfg, "034")`,
assert the backfill inserted rows, re-run for idempotency (`ON CONFLICT DO NOTHING`), assert autogenerate
diff empty via `compare_metadata`/`_diffs_touching_034`, then downgrade.

**Footgun:** export `MIGRATIONS_TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test`
explicitly — `just test-bucket` does NOT export it (defaults to 5432, wrong).

#### `tests/integration/test_shadow_compare.py` — `awaiting_cloud` invariant green (test, integration)

**Analog:** itself. After the D-02 writer + `034` repair, the hard `awaiting_cloud` invariant
(`shadow_compare.py:131`, `_cloud_awaiting`) passes on backfilled fixtures (it is violated at HEAD). Extend
existing fixtures to seed an `AWAITING_CLOUD` file WITH its `awaiting` cloud_job row and assert zero
divergence. **No change to `shadow_compare.py` source** — no converse invariant is added (Deferred).

#### `tests/shared/routers/test_pipeline.py` — `get_awaiting_cloud_count` (test, unit) — D-15

**Analog:** itself (existing `/pipeline` tests, e.g. `:553` `test_analyze_ui_reports_split_counts`). Assert
the count derives from the drain clause: a `LOCAL_ANALYZING` long file carrying an `awaiting` row is
excluded from BOTH the count and the drain (card and drain cannot disagree).

#### `tests/agents/routers/test_agent_analysis.py` — D-14 reaper (test, unit)

**Analog:** itself (35 KB, existing `put_analysis` / `report_analysis_failed` tests). Seed an `awaiting`
cloud_job row, POST the analyze-terminal callback, assert the row is DELETED; seed a `SUCCEEDED`/`RUNNING`
row, assert it is LEFT untouched.

---

## Shared Patterns

### Dispatch discipline — never commit inside a dispatch/spill mutation
**Source:** `services/backends.py:146-152` (protocol docstring) + `tasks/release_awaiting_cloud.py:257-264`
(single post-loop commit / whole-tick rollback).
**Apply to:** the D-02 `hold_awaiting_cloud()` helper (never commits). The hold-path caller
(`trigger_analysis`) and the two callback spill paths own their own commit — the helper does not.
```python
# release_awaiting_cloud.py: the ONE commit; a mid-loop commit drops pg_advisory_xact_lock (Landmine L1)
        await session.commit()
    except Exception:
        await session.rollback()   # whole-tick rollback -> the committed ledger row alone re-excludes (D-05)
        return {"staged": 0, "skipped": len(candidates)}
```

### Rowcount-guarded idempotent CAS on `cloud_job.status`
**Source:** `routers/agent_s3.py:105-116` (`report_uploaded`), `routers/agent_push.py:123-137`
(`report_pushed`), `:258-272` (`report_push_mismatch`).
**Apply to:** the `report_upload_failed` rewrite (D-09/D-10). The `cast("CursorResult[Any]", ...)` idiom is
mandatory — async stubs type `execute()` as base `Result`; only `CursorResult` exposes `rowcount`.
```python
res = cast("CursorResult[Any]", await session.execute(update(CloudJob).where(...).values(...)))
if res.rowcount == 0:
    await session.commit()          # idempotent no-op
    return SomeResponse(..., cleared=False)
```

### Correlated `exists(...)` clause reuse — never re-spell a predicate
**Source:** `services/stage_status.py:89-117` (`done_clause`), `:120-147` (`failed_clause`), `:150-167`
(`inflight_clause`), `:170-...` (`domain_completed_clause`).
**Apply to:** the drain query (D-05) and `get_awaiting_cloud_count` (D-15). Compose `~inflight_clause(...)`
/ `~domain_completed_clause(...)` verbatim. Re-hand-writing the ledger/analysis EXISTS breaks the
DERIV-04 equivalence test (`tests/integration/test_stage_status_equivalence.py`).

### `_safe_count` degrade-safe display reads
**Source:** `services/pipeline.py:1119-1123` (and the whole count-card family `:1126-1236`).
**Apply to:** `get_awaiting_cloud_count` (keep the wrapper — the hot 5s `/pipeline/stats` poll never 500s).

### AUTH-01 — path-only `file_id`, token identity, `extra='forbid'` bodies
**Source:** `routers/agent_s3.py:24-26,60-67` and `routers/agent_push.py:173-179`.
**Apply to:** every touched callback handler — unchanged; the phase adds no auth surface.

---

## No Analog Found

None. Every new/modified file has a verified in-tree donor (the phase's defining characteristic per
RESEARCH: "almost every building block this phase needs already exists in-tree").

---

## Metadata

**Analog search scope:** `src/phaze/{routers,services,models,tasks,enums}/`, `alembic/versions/`,
`tests/{agents,integration,analyze,shared}/`.
**Files scanned:** 14 source + 5 test files, all read at HEAD of `SimplicityGuy/phaze-83`.
**Project conventions demonstrated by the donors:** `uv run` prefix for all tooling; type hints on every
function; 150-char lines; double quotes; SQLAlchemy 2.0 async (`AsyncSession`, `await session.execute`);
`cast("CursorResult[Any]", ...)` to read `rowcount`; Alembic migrations are SYNC; `structlog` logging;
`pg_insert(...).on_conflict_do_update(index_elements=["file_id"])` for the `uq_cloud_job_file_id` upsert;
`pg_advisory_xact_lock(hashtext(key))` for RMW serialization (never a self-deadlocking row lock).
**Pattern extraction date:** 2026-07-09
