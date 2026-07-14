# Phase 81: Per-Stage Failure Persistence & Retry Paths - Pattern Map

**Mapped:** 2026-07-08
**Files analyzed:** 15 (7 new, 8 modified)
**Analogs found:** 15 / 15 (every donor named in CONTEXT/RESEARCH verified at HEAD)

All donor anchors below were **read at HEAD on branch `SimplicityGuy/phase-81`** and reflect
RESEARCH.md's corrected line numbers where they differ from CONTEXT.md.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match | Test Bucket |
|-------------------|------|-----------|----------------|-------|-------------|
| `src/phaze/schemas/agent_metadata.py` (+`MetadataFailurePayload`) | schema | request-response | `schemas/agent_analysis.py::AnalysisFailurePayload` | exact | metadata |
| `src/phaze/routers/agent_metadata.py` (`report_metadata_failed` upsert + optional body; `put_metadata` clear-on-success) | controller/route | event-driven (terminal-ack) | `routers/agent_analysis.py::report_analysis_failed` / `put_analysis` | exact | metadata |
| `src/phaze/routers/agent_analysis.py` (`report_analysis_failed` dual-write; `put_analysis` clear-on-success) | controller/route | event-driven (terminal-ack) | itself (in-place, `032` backfill for upsert shape) | self/exact | analyze |
| `src/phaze/routers/pipeline.py` (+`POST /pipeline/metadata-failed/retry`) | controller/route | request-response (bulk retry) | `pipeline.py::retry_analysis_failed` | exact | integration |
| `src/phaze/services/pipeline.py` (+`get_metadata_failed_files`) | service | CRUD (query) | `services/pipeline.py::get_analysis_failed_files` / `get_metadata_pending_files` + `failed_clause(METADATA)` | role-match | integration |
| `src/phaze/services/agent_client.py` (`report_metadata_failed` widened signature) | service (http client) | request-response | `agent_client.py::report_metadata_failed` (current) / `report_upload_failed` (optional-body precedent) | self/exact | agents |
| `src/phaze/tasks/metadata_extraction.py` (bind `as exc`, compose payload) | task | event-driven | itself (`except Exception as exc` + `functions.py:179-189`) | self | metadata |
| `alembic/versions/033_*.py` (NEW migration: cleanup UPDATE **then** CHECK) | migration | batch/DDL | `alembic/versions/032_add_derived_status_schema.py` | exact | integration |
| `src/phaze/models/analysis.py` (+CHECK in `__table_args__`) | model | n/a | `models/pipeline_stage_control.py` (named CHECK precedent) | role-match | integration |
| `src/phaze/enums/stage.py` (+`FAILURE_IS_TERMINAL`, `ELIGIBLE_AFTER_FAILURE`, `domain_completed()`; refactor `eligible()`) | utility (DB-free) | transform (pure) | itself (`ELIGIBILITY_DAG` table + `eligible()` dispatch) | self/exact | shared |
| `src/phaze/services/stage_status.py` (+`domain_completed_clause()`) | service | transform (SQL) | itself (`done_clause` / `failed_clause`) | self/exact | integration |
| `tests/integration/test_migrations/test_migration_033_*.py` (NEW) | test | n/a | `test_migration_032_additive_schema.py` | exact | integration |
| `tests/integration/test_stage_status_equivalence.py` (extend `CASES` / add domain_completed cells) | test | n/a | itself (`CASES` parametrization + `test_sql_equals_python`) | self/exact | integration |
| `tests/metadata/routers/test_agent_metadata.py` (bodyless + with-body report; clear-on-success) | test | n/a | itself (`:298-333` ledger-clear tests) | self/exact | metadata |
| `tests/fingerprint/routers/test_agent_fingerprint*.py` (no-row regression + docstrings) | test | n/a | existing fingerprint router tests | role-match | fingerprint |

---

## Pattern Assignments

### `src/phaze/schemas/agent_metadata.py` — new `MetadataFailurePayload` (schema, D-10)

**Analog:** `src/phaze/schemas/agent_analysis.py::AnalysisFailurePayload` (lines 114-127).

Copy the shape verbatim — `Literal` reason + bounded `error` + `extra='forbid'`:
```python
class AnalysisFailurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["timeout", "crashed", "error"]
    error: str | None = Field(default=None, max_length=2000)
```
The existing `schemas/agent_metadata.py` already imports `Literal`, `ConfigDict`, `Field` (lines 3-6)
and already defines `MetadataFailureResponse` (lines 36-49). Add `MetadataFailurePayload` alongside it;
the response model already exists and needs no change. Keep `extra='forbid'` (AUTH-01) so a present body
with an unknown field is 422 — only *new* opt-in agents ever send a body.

---

### `src/phaze/routers/agent_metadata.py` — `report_metadata_failed` (FAIL-02, D-10) + `put_metadata` clear-on-success (D-13)

**Analog for the failure upsert side-effect ordering:** `routers/agent_analysis.py::report_analysis_failed`
(lines 310-337) — the txn ordering to mirror is: write marker → `clear_ledger_entry` → `_delete_staged_object_if_cloud` → single `commit`.

Current `report_metadata_failed` (lines 98-125) clears the ledger only. D-10 adds `body: MetadataFailurePayload | None = None`
(NO `Body(...)` wrapper — a bodyless POST from an old agent must bind `None` and return 200, RESEARCH FastAPI section).
The new behavior inserts a `metadata` row with `failed_at` set + payload cols NULL, using the **shared upsert idiom**
(see below), then keeps the existing `clear_ledger_entry` + `commit`. `error_message` falls back to a fixed placeholder
when body is `None`; else `f"{body.reason}: {body.error}"` truncated.

**Signature to adopt (RESEARCH-verified 200-on-bodyless construct):**
```python
async def report_metadata_failed(
    file_id: uuid.UUID,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: MetadataFailurePayload | None = None,   # NO Body(...) wrapper, NO embed
) -> MetadataFailureResponse: ...
```

**Shared `pg_insert(...).on_conflict_do_update` idiom** (identical in `put_metadata` lines 69-81, `put_analysis`
lines 198-210, `put_fingerprint` lines 40-47) — stamp PK explicitly because `id` is Python-only default:
```python
payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}
stmt = pg_insert(FileMetadata).values([payload])
stmt = stmt.on_conflict_do_update(index_elements=["file_id"], set_={...})
await session.execute(stmt)
```
For the failure insert, `set_` must set `failed_at` + `error_message` (a re-failure updates in place).

**`put_metadata` clear-on-success (D-13) — THE SHARP EDGE.** Current `put_metadata` (lines 64-95) has TWO
branches. The empty-body branch (lines 78-81) is `on_conflict_do_nothing` and **never clears the marker** —
an empty-body success PUT after a failure would leave `failed_at` set forever:
```python
dumped = body.model_dump(exclude_unset=True)
payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}
stmt = pg_insert(FileMetadata).values([payload])
if dumped:
    stmt = stmt.on_conflict_do_update(index_elements=["file_id"], set_={k: stmt.excluded[k] for k in dumped})
else:
    stmt = stmt.on_conflict_do_nothing(index_elements=["file_id"])   # <-- never clears failed_at
```
D-13 fix (RESEARCH Pitfall 1): add `failed_at=None, error_message=None` **unconditionally** to the
`on_conflict_do_update` SET clause, AND make the empty-body branch still clear on an existing row (either
fold the clear into an unconditional `set_={"failed_at": None, "error_message": None, **fields}` so the
empty-body case also does `DO UPDATE`, or issue a separate `update(FileMetadata).where(file_id==...).values(failed_at=None, error_message=None)`).
Must NOT be `exclude_unset`-driven — `failed_at` is never in the agent body.

---

### `src/phaze/routers/agent_analysis.py` — `report_analysis_failed` dual-write (D-05/D-06) + `put_analysis` clear-on-success (D-13)

**Analog:** itself. `report_analysis_failed` (lines 310-337) currently writes ONLY `FileRecord.state = ANALYSIS_FAILED`
then `clear_ledger_entry(process_file:...)` (line 333) + `_delete_staged_object_if_cloud` (line 336) + `commit` (line 337).

D-05 dual-write: **keep** the `state = ANALYSIS_FAILED` write and **add** an `analysis` upsert into the SAME txn.
Recommended order (RESEARCH Discretion #1): (a) `analysis` upsert stamping `failed_at` + `error_message = f"{body.reason}: {body.error}"`
AND clearing `analysis_completed_at` (D-06 mutual-exclusion), (b) `FileRecord.state = ANALYSIS_FAILED`, (c) `clear_ledger_entry`,
(d) `_delete_staged_object_if_cloud`, (e) single `commit`. A pure analyze failure never wrote an `analysis` row, so use
`pg_insert(...).on_conflict_do_update(index_elements=["file_id"])` exactly like the `032` backfill (RESEARCH OQ2), NOT a bare UPDATE.

**`put_analysis` clear-on-success (D-13):** the completion branch (lines 234-241) already flips `ANALYZED` + stamps
`analysis_completed_at=func.now()`. Add unconditional `failed_at=None, error_message=None` — this is also what makes the
completion branch satisfy the D-06 CHECK (both columns can't be non-NULL). The upsert SET clause is built from `dumped`
(lines 199-210) exactly like metadata; add the clear outside `exclude_unset`.

---

### `src/phaze/routers/pipeline.py` — new `POST /pipeline/metadata-failed/retry` (FAIL-03, D-12)

**Analog:** `pipeline.py::retry_analysis_failed` (lines 884-951). Mirror the guard ordering **exactly**:

```python
files = await get_analysis_failed_files(session)          # -> get_metadata_failed_files
if not files:
    return templates.TemplateResponse(name="pipeline/partials/retry_failed_response.html",
        context={"request": request, "count": 0, "no_active_agent": False})
try:
    routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session)
except enqueue_router.NoActiveAgentError:
    # Do NOT flip state, do NOT enqueue, do NOT fall through to default queue (Phase-30).
    return templates.TemplateResponse(name="pipeline/partials/retry_failed_response.html",
        context={"request": request, "count": 0, "no_active_agent": True})
agent_id = cast("str", routed.agent_id)
for f in files:
    f.state = FileState.FINGERPRINTED     # <-- FAIL-03 OMITS this bucket flip (no metadata terminal state)
await session.commit()                    # commit BEFORE enqueue
for f in files:
    await enqueue_process_file(routed.queue, f, agent_id, settings.models_path)
```

**FAIL-03 differences (D-12, simpler than donor):**
- Task is `extract_file_metadata`, not `process_file` — resolve `resolve_queue_for_task("extract_file_metadata", ...)`
  and enqueue via the metadata producer with the deterministic `extract_file_metadata:<file_id>` dedup key.
- **NO `f.state = FINGERPRINTED` flip** — metadata has no terminal `FileState`; D-11 leaves the failure row in place
  and simply re-enqueues (clearing `failed_at` in place is UNSAFE — a zero-metadata row would read DONE forever).
- Still: resolve-queue-once → `NoActiveAgentError` returns without enqueue/mutation → commit-before-enqueue → deterministic-key dedup.

**HTMX template (RESEARCH Discretion #3):** `pipeline/partials/retry_failed_response.html` renders three branches
from `{request, count, no_active_agent}` and is stage-agnostic EXCEPT line 18 hard-codes `"Re-queued {{ count }} failed
file(s) for analysis."`. **Reuse requires a label var** (add a `stage`/`label` context var) or a sibling
`metadata_retry_response.html`. Do not reuse verbatim — the "for analysis" copy is analyze-specific.

---

### `src/phaze/services/pipeline.py` — new `get_metadata_failed_files` (D-12)

**Analog:** `get_analysis_failed_files` (lines 1057-1065) is a one-line `get_files_by_state(ANALYSIS_FAILED)` — but
metadata has **no `FileState`**, so it can't be copied directly. Lives in `services/pipeline.py` alongside its donor
and `get_metadata_pending_files` (lines 1330-1341), NOT the router (RESEARCH Discretion #2).

Query joins `FileRecord → FileMetadata WHERE metadata.failed_at IS NOT NULL`. Reuse the shape of
`failed_clause(METADATA)` from `services/stage_status.py:129-130`:
```python
return exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id, FileMetadata.failed_at.isnot(None)))
```
Pure ORM / bound params — NO f-string SQL (T-42-03, matches `get_metadata_pending_files`'s `select(FileRecord).where(...)`).

---

### `src/phaze/services/agent_client.py` — widen `report_metadata_failed` signature (D-10)

**Analog:** current `report_metadata_failed` (lines 401-415, no body) + the optional-body precedent
`report_upload_failed` (lines 385-399, sends `json=UploadFailedRequest(detail=...).model_dump(mode="json")`).
Widen to accept the optional payload and send it via `json=...` only when present. httpx-only — NO database import
(keeps agent Postgres-free).

---

### `src/phaze/tasks/metadata_extraction.py` — compose the failure payload (D-10)

**Analog:** itself (lines 65-81). The terminal-ack call site at lines 72-80 currently catches `except Exception:`
(no `as exc`) and calls `api.report_metadata_failed(payload.file_id)` bodyless. D-10's `error` detail requires binding
`except Exception as exc:` and composing a `MetadataFailurePayload` to pass. Keep the best-effort try/except around the
ack (swallow E2, always `raise` E1 — WR-01) and the `job.retryable` guard unchanged.

---

### `alembic/versions/033_*.py` — new migration (D-06/D-08/D-09)

**Analog:** `alembic/versions/032_add_derived_status_schema.py` (full file) — copy the structure: static-SQL module
constants, `op.execute(sa.text(...))`, bare-number `revision`/`down_revision` header, minimal DDL-only `downgrade()`.
`033` chains off `032` (`revision="033"`, `down_revision="032"`); nothing else claims it.

**`upgrade()` order is mandatory (D-09, RESEARCH Pitfall 2) — cleanup UPDATE BEFORE the CHECK:**
```python
# (1) D-09 mixed-row cleanup FIRST — clears failed_at, keeps analysis_completed_at (done ≻ failed).
op.execute(sa.text("""
    UPDATE analysis SET failed_at = NULL
     WHERE analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL
"""))
# (2) THEN the CHECK — a pre-existing mixed row would abort create_check_constraint otherwise.
op.create_check_constraint(
    "analysis_completed_xor_failed",   # BARE name — the ck_%(table_name)s_%(constraint_name)s convention re-prefixes
    "analysis",
    "NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)",
)
```
The mixed rows are real (from `032`'s `_BACKFILL_ANALYZE_FAILED` `ON CONFLICT DO UPDATE` with no `completed_at`
guard — see `032` lines 73-82). **Bare constraint name** — the `032` `status_enum` comment (lines 66-67) is the
precedent (passing an already-prefixed name double-prefixes it).

**`downgrade()`:** `op.drop_constraint("analysis_completed_xor_failed", "analysis", type_="check")`. The D-09 UPDATE
is NOT reversed (016/032 best-effort-DDL precedent — see `032` downgrade lines 167-186).

**D-08 doc renumber (in scope):** grep `033` across `.planning/ROADMAP.md`, `.planning/REQUIREMENTS.md`,
`.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` at plan time — CONTEXT's line list is incomplete (RESEARCH found
ROADMAP lines **492, 494** missed and REQUIREMENTS MIG-02 line 96 over-listed). Renumber only the *destructive* (Phase 90)
references `033→034`; do NOT touch this phase's own new `033` file.

---

### `src/phaze/models/analysis.py` — mirror the CHECK into `__table_args__` (D-06/D-08)

**Analog for a named CHECK in `__table_args__`:** `models/pipeline_stage_control.py:35`:
```python
__table_args__ = (CheckConstraint("priority BETWEEN 0 AND 100", name="priority_range"),)
```
The `analysis` model already has `__table_args__` with two partial indexes (lines 49-52). Add a third element:
```python
from sqlalchemy import CheckConstraint
CheckConstraint("NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)",
                name="analysis_completed_xor_failed"),
```
This keeps `alembic revision --autogenerate` empty (77-precedent, the `032` empty-diff contract). Confirm the rendered
name matches the autogenerate probe (RESEARCH Open Question 1).

---

### `src/phaze/enums/stage.py` — new tables + `domain_completed()` + `eligible()` refactor (D-14/D-15/D-16/D-17)

**Analog:** itself — the module-level `ELIGIBILITY_DAG` table (lines 61-69) and the `eligible()` dispatch (lines 167-193).
**HARD CONSTRAINT: this module is DB-free** (docstring lines 9-11; `tests/shared/test_stage_resolver.py` enforces no
`sqlalchemy`/`phaze.models` import). The new tables + helper must stay stdlib-only.

Add module-level tables next to `ELIGIBILITY_DAG` (RESEARCH D-15 spelling):
```python
FAILURE_IS_TERMINAL:    dict[Stage, bool] = {Stage.ANALYZE: True,  Stage.METADATA: True,  Stage.FINGERPRINT: False}
ELIGIBLE_AFTER_FAILURE: dict[Stage, bool] = {Stage.ANALYZE: False, Stage.METADATA: True,  Stage.FINGERPRINT: True}

def domain_completed(status_map, stage) -> bool:
    st = status_map.get(stage, Status.NOT_STARTED)
    return st is Status.DONE or (st is Status.FAILED and FAILURE_IS_TERMINAL[stage])
```

**`eligible()` refactor (D-16, semantics-preserving).** Current branches (lines 186-189) — verified line numbers:
```python
if stage in (Stage.METADATA, Stage.FINGERPRINT):
    return status_map.get(stage, Status.NOT_STARTED) not in (Status.DONE, Status.IN_FLIGHT)
if stage is Stage.ANALYZE:                                              # the inlined carve-out to collapse
    return status_map.get(Stage.ANALYZE, Status.NOT_STARTED) == Status.NOT_STARTED
```
Collapse the three enrich branches using `ELIGIBLE_AFTER_FAILURE` (RESEARCH terminality section):
`eligible = status not in (DONE, IN_FLIGHT) and (status != FAILED or ELIGIBLE_AFTER_FAILURE[stage])`.
This yields IDENTICAL truth: ANALYZE (`False`) → eligible iff NOT_STARTED; METADATA/FINGERPRINT (`True`) → eligible iff
NOT_STARTED or FAILED. **ELIG-01..04 must pass unchanged** (`tests/shared/test_stage_eligibility_dag.py`). Note `:190` is
the APPLY branch — do not touch it.

---

### `src/phaze/services/stage_status.py` — new `domain_completed_clause()` (D-17)

**Analog:** `done_clause` (lines 89-117) / `failed_clause` (lines 120-147) in the same module. This is the SQL twin of
`enums.stage.domain_completed`:
```python
def domain_completed_clause(stage: Stage) -> ColumnElement[bool]:
    if FAILURE_IS_TERMINAL[stage]:
        return or_(done_clause(stage), failed_clause(stage))
    return done_clause(stage)   # second disjunct collapses to false() when terminality is False
```
(Import `FAILURE_IS_TERMINAL` from `enums.stage` — already the module's dependency direction, line 65.) Ship it **this
phase** and lock it against the Python twin by extending the equivalence test — do NOT land the Python helper and SQL twin
one phase apart (the drift window Phase 78 D-04 closed).

---

### `tests/integration/test_migrations/test_migration_033_*.py` — new (D-06/D-08/D-09)

**Analog:** `tests/integration/test_migrations/test_migration_032_additive_schema.py` (full file). Copy the pattern:
- `test_revision_identifiers_are_bare_numbers` (lines 91-96) — assert `revision=="033"`, `down_revision=="032"`.
- `test_migration_never_references_saq_jobs` (lines 99-103) — grep-style banner assertion.
- The autogenerate-emptiness assertion (lines 139-162, 245-248) via `compare_metadata(ctx, Base.metadata)` filtered to
  this migration's objects — **the pattern that matters most** (PERF-01 empty-diff contract). Scope `_O33_*` sets to the
  new CHECK constraint.
- Seed corpus + upgrade/downgrade round-trip harness: `_build_alembic_config`, `upgrade_to`, `downgrade_to` from
  `tests/integration/test_migrations/conftest.py`; migrations DB is `phaze_migrations_test` (`MIGRATIONS_TEST_DATABASE_URL`,
  justfile `:191-215`).

**Must assert (RESEARCH Migration 033 Mechanics):** (a) upgrade adds the CHECK + runs the cleanup; (b) autogenerate diff
empty for the CHECK; (c) a **pre-seeded mixed row** (both `analysis_completed_at` and `failed_at` set) is cleaned BEFORE
the CHECK (the D-09 ordering — seed it, upgrade, assert `failed_at IS NULL` and `analysis_completed_at` retained);
(d) the rendered constraint name matches (`ck_analysis_analysis_completed_xor_failed`); (e) down/up round-trips.
Runs in the **integration** bucket.

---

### `tests/integration/test_stage_status_equivalence.py` — extend for `domain_completed` (D-17)

**Analog:** itself. The `CASES` list (lines 306-337) is `list[tuple[Stage, seed_fn, expected_status]]`; the driver
`test_sql_equals_python` (lines 400-412) seeds a row, runs `eval_sql_status` (SQL `stage_status_case`) and
`resolve_status(stage, load_scalars(...))` (Python), asserts all three equal. **Exact tuple shape to add cells to:**
```python
CASES: list[tuple[Stage, Callable[[AsyncSession], Awaitable[uuid.UUID]], str]] = [
    (Stage.ANALYZE, seed_analysis_failed, "failed"),          # line 311
    (Stage.METADATA, seed_metadata_failed_only, "failed"),    # line 316
    (Stage.FINGERPRINT, seed_fp_failed_only, "failed"),       # line 322
    (Stage.FINGERPRINT, seed_fp_success_and_failed, "done"),  # line 321
    ...
]
```
D-17 extension: add a **parallel parametrized cell set** asserting
`domain_completed(load_scalars(...), stage) == bool(<domain_completed_clause SQL result>)` reusing the SAME seed fns —
so the Python table and SQL twin can never drift. `load_scalars` (line 351) already reads each stage's rows into the
DB-free scalar dict. Runs in the **integration** bucket.

---

### `tests/metadata/routers/test_agent_metadata.py` — report + clear-on-success tests (D-10/D-13)

**Analog:** itself, the existing ledger-clear tests (lines 298-333). The authed-agent router pattern:
```python
async def test_metadata_put_success_clears_ledger(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/metadata/{file_id}", json={"artist": "A"})
    assert r.status_code == 200, r.text
```
Add: bodyless POST → 200 + ledger cleared + `metadata` row with `failed_at` NOT NULL / payload NULL; with-body POST →
200 + `error_message` populated; `extra='forbid'` unknown field → 422; empty-body success PUT after a failure clears
`failed_at` (D-13 sharp edge). Runs in the **metadata** bucket. See RESEARCH: agent endpoints use the **authed** client
(Bearer token); operator `pipeline.py` endpoints use the **plain** `client`.

---

### `tests/fingerprint/routers/test_agent_fingerprint*.py` — FAIL-04 regression (D-18)

**Analog:** existing fingerprint router tests + `report_fingerprint_failed` (lines 59-89, persists no row by design).
**No new writer** — assert: `report_fingerprint_failed` persists NO `fingerprint_results` row and only clears the ledger
(row count unchanged); a per-engine `status='failed'` row from `put_fingerprint` persists and keeps the file
`eligible(fingerprint)` and `_trackid_engine_badge` unpoisoned; document the asymmetry in docstrings. Runs in the
**fingerprint** bucket. (Anchor correction: `_trackid_engine_badge` + the two aliased per-engine joins are in
**`services/pipeline.py:864` / `:939-940`**, NOT `routers/pipeline.py`.)

---

## Shared Patterns

### Ledger-clear in the same transaction as the write (Phase 45 L-02)
**Source:** every terminal-ack path — `agent_metadata.py:93`, `agent_analysis.py:248,333`, `agent_fingerprint.py:54,85`.
**Apply to:** `report_metadata_failed`, `report_analysis_failed` (new writers must preserve exactly-once clear).
```python
await clear_ledger_entry(session, f"extract_file_metadata:{file_id}")   # key from PATH file_id ONLY (AUTH-01/T-45-05)
await session.commit()
```

### `pg_insert(...).on_conflict_do_update` upsert idiom (stamp Python-only PK)
**Source:** `put_metadata` (`agent_metadata.py:68-81`), `put_analysis` (`agent_analysis.py:197-210`),
`put_fingerprint` (`agent_fingerprint.py:39-47`), and `032`'s `_BACKFILL_ANALYZE_FAILED` (INSERT..ON CONFLICT).
**Apply to:** the metadata failure insert, the analyze failure upsert (D-05), and both D-13 clear-on-success SET clauses.
`{**dumped, "file_id": file_id, "id": uuid.uuid4()}` — `id` stamped explicitly because `pg_insert` bypasses the
Python-only `default=uuid.uuid4`.

### Auth: `agent` from the dep, keys from PATH `file_id` only (AUTH-01 / T-45-05)
**Source:** all agent routers — `Annotated[Agent, Depends(get_authenticated_agent)]`; `extra='forbid'` on every payload.
**Apply to:** the new optional `MetadataFailurePayload` body must keep `extra='forbid'`; never read agent/file from the body.

### `done ≻ failed` precedence
**Source:** `_analyze_status` (`enums/stage.py:75-84`) / `stage_status_case` ladder (`services/stage_status.py:184-189`).
**Apply to:** confirms the D-09 cleanup keeps `done` on mixed rows without changing derived status (D-04 shadow gate stays green).

### NoActiveAgentError → no default-queue fallthrough (Phase 30)
**Source:** `retry_analysis_failed` (`pipeline.py:920-928`).
**Apply to:** the FAIL-03 bulk retry endpoint — resolve queue once, catch, return without enqueue/mutation.

---

## No Analog Found

None. Every new/modified file has a concrete in-repo donor verified at HEAD.

---

## Metadata

**Analog search scope:** `src/phaze/{schemas,routers,services,models,enums,tasks}`, `alembic/versions/`,
`tests/{integration,metadata,fingerprint,analyze,shared}/`.
**Files scanned / read:** 16 (schemas/agent_analysis, schemas/agent_metadata, models/analysis, models/metadata,
models/pipeline_stage_control, enums/stage, services/stage_status, services/pipeline, services/agent_client,
routers/agent_metadata, routers/agent_analysis, routers/agent_fingerprint, routers/pipeline, tasks/metadata_extraction,
alembic/versions/032, test_migration_032, test_stage_status_equivalence, test_agent_metadata, retry_failed_response.html,
buckets.json).
**Pattern extraction date:** 2026-07-08
</content>
</invoke>
