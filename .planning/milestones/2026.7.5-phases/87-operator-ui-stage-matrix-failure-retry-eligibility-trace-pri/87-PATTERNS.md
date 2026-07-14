# Phase 87: Operator UI — Stage Matrix, Failure Retry, Eligibility Trace & Priority - Pattern Map

**Mapped:** 2026-07-11
**Files analyzed:** 24 (new + modified) across 4 PR seams (a–d)
**Analogs found:** 24 / 24 (every artifact maps to a concrete in-tree analog)

> Every artifact below has a closest existing analog with a file path + line range. The `skipped`-marker
> slice (seam a) is the correctness-critical 20%; seams b/c/d are UI re-wiring of already-live backends.
> RESEARCH.md pins most analogs; this map extracts the concrete code to copy from each.

---

## File Classification

### Seam (a) — `skipped` marker: schema + migration + writer + derivation + DERIV-04 (D-13)

| New/Modified File | Role | Data Flow | Closest Analog | Match |
|-------------------|------|-----------|----------------|-------|
| `src/phaze/models/stage_skip.py` (NEW) | model | CRUD/marker | `src/phaze/models/dedup_resolution.py` | exact (sidecar marker) |
| `src/phaze/models/__init__.py` (MOD) | config/registry | — | same file, `DedupResolution` lines 6,28 | exact |
| `alembic/versions/037_add_stage_skip.py` (NEW) | migration | DDL+create-table | `alembic/versions/032_add_derived_status_schema.py` §(B) | exact (additive sidecar) |
| `tests/integration/test_migrations/test_037_stage_skip.py` (NEW) | test | migration up/down/empty-diff | `tests/integration/test_migrations/test_migration_032_additive_schema.py` | exact |
| `src/phaze/enums/stage.py` (MOD) | enum/resolver | transform (pure) | same file — `Status`, `resolve_status`, `eligible`, `domain_completed` | exact (extend twin) |
| `src/phaze/services/stage_status.py` (MOD) | service | clause-builder | same file — `done_clause`/`eligible_clause`/`domain_completed_clause`/`stage_status_case` | exact (extend twin) |
| `tests/integration/test_stage_status_equivalence.py` (MOD) | test | parametrized SQL⇔Python | same file — `CASES`/`ELIGIBLE_CASES`/`DOMAIN_COMPLETED_CASES`/`load_scalars` | exact |
| `tests/shared/test_stage_resolver.py` (MOD) | test | pure-fn | same file (resolver unit tests) | exact |
| force-skip writer endpoint in `src/phaze/routers/pipeline.py` (MOD) | route | request-response (mutating) | `routers/pipeline_stages.py` (validate+commit) + `pipeline.py:934` retry | role-match |
| shadow-compare skipped-corpus test (NEW) | test | invariant anti-join | existing `shadow_compare` tests + `services/shadow_compare.py` INVARIANTS | role-match |

### Seam (b) — paginated files table + pill matrix + status filters (UI-01/UI-02)

| New/Modified File | Role | Data Flow | Closest Analog | Match |
|-------------------|------|-----------|----------------|-------|
| `templates/pipeline/partials/_stage_pill.html` (NEW) | template/component | render | `templates/pipeline/partials/scan_status_pill.html` | exact (pill token) |
| `templates/pipeline/partials/_stage_matrix.html` (NEW) | template/component | render | composes `_stage_pill`; row idiom from `_file_table.html` cells | role-match |
| files-table router + `get_files_page` service query (NEW) | route+service | request-response / paginated CRUD | `routers/tracklists.py:80` `list_tracklists` + `services/pipeline.py:1367` `get_cloud_staging_candidates` (correlated clause + LIMIT) | exact |
| status filter bar partial (NEW) | template | render | `templates/tracklists/partials/pagination.html` filter/query idiom | role-match |
| reuse `templates/tracklists/partials/pagination.html` | template | render | itself | exact |
| `metadata_workspace.html` / `analyze_workspace.html` (MOD — retire raw `f.state`) | template | render | `metadata_workspace.html:40-51` (the retire site) | exact |
| per-file + bulk retry endpoints/partials | route+template | request-response | `pipeline.py:934` `retry_analysis_failed`, `:1017` `retry_metadata_failed`; `metadata_retry_response.html` / `retry_failed_response.html` | exact |

### Seam (c) — right-pane expanded matrix + eligibility trace (UI-03) + force-skip control (UI-04)

| New/Modified File | Role | Data Flow | Closest Analog | Match |
|-------------------|------|-----------|----------------|-------|
| `templates/pipeline/partials/_eligibility_trace.html` (NEW) | template | render | `templates/record/record_body.html` section + `_file_table.html` cell idiom | role-match |
| expanded matrix + trace host in record slide-in (MOD `record_body.html`) | template | render | `templates/record/record_body.html` | exact (same right-pane) |
| trace endpoint (single-row `resolve_status`) in `routers/record.py` or `pipeline.py` (NEW) | route | request-response (read) | `routers/record.py:41` `file_record` (file_id-scoped single-row reads) | exact |
| force-skip control + confirm dialog partial (NEW) | template | render + form-post | Alpine `x-trap` dialog idiom (record slide-in / ⌘K, per UI-SPEC) | role-match |

### Seam (d) — orphan-count badge (UI-05) + priority stepper/pause-resume rewire (PRIO-01)

| New/Modified File | Role | Data Flow | Closest Analog | Match |
|-------------------|------|-----------|----------------|-------|
| orphan-count service helper (NEW, `services/pipeline.py`) | service | derived-count (degrade-safe) | `services/pipeline.py:566` `get_live_job_keys` + `stage_status.py:392` `saq_detail` (SAVEPOINT) + `awaiting_candidate_clause` | role-match |
| orphan-count badge on rail (MOD `shell/partials/rail.html`) | template | render (OOB seed) | `rail.html:79,95,107,119` live-count `x-text` spans | exact |
| priority stepper + pause/resume on rail (MOD `rail.html` + seed in `pipeline.py`) | template | request-response (HTMX post) | `routers/pipeline_stages.py:82-128` (LIVE endpoints) + `pipeline.py:219-227` DAG overlay seed | exact (backend live) |

---

## Pattern Assignments

### `src/phaze/models/stage_skip.py` (model, marker sidecar) — SEAM a

**Analog:** `src/phaze/models/dedup_resolution.py` (whole file, 55 lines). This is the `(file_id, …)`
sidecar marker shape RESEARCH names. Copy the imports, `TimestampMixin + Base` inheritance, UUID PK,
FK-to-`files.id`, and the `server_default=func.now()` timestamp. The one structural delta: the
uniqueness is on the **composite** `(file_id, stage)`, not `file_id` alone (dedup is 1:1 per file;
skip is ≤1 per `(file, stage)`).

**Imports + class shell** (`dedup_resolution.py:31-54`):
```python
from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class DedupResolution(TimestampMixin, Base):
    __tablename__ = "dedup_resolution"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), unique=True, nullable=False)
    ...
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
```

**Deltas for `StageSkip`** (per RESEARCH §1 + UI-SPEC D-09/D-10):
- add `stage: Mapped[str]` (String, the enrich value `'metadata'|'analyze'|'fingerprint'`), `reason: Mapped[str]` (Text, D-09 required), `skipped_at` (TIMESTAMPTZ default now).
- `file_id` is NOT `unique=True` on its own; instead add `__table_args__` with a `UniqueConstraint("file_id", "stage", name="uq_stage_skip_file_stage")` (mirror the empty-diff `__table_args__` discipline in `models/metadata.py:39` / `models/analysis.py:53-56`).
- optional `CheckConstraint("stage IN ('metadata','analyze','fingerprint')")` (OQ-3, belt-and-suspenders on D-10). Spell the predicate exactly as Postgres renders it (Pitfall 5 — a plain `UNIQUE` b-tree avoids the `= ANY(ARRAY[...])` reserialization trap).

**`__table_args__` mirror pattern** (`models/analysis.py:53-56`):
```python
__table_args__ = (
    Index("ix_analysis_completed", "file_id", postgresql_where=text("analysis_completed_at IS NOT NULL")),
    CheckConstraint("NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)", name="analysis_completed_xor_failed"),
)
```

---

### `src/phaze/models/__init__.py` (registry) — SEAM a

**Analog:** same file. Add `from phaze.models.stage_skip import StageSkip` (mirror line 6
`DedupResolution`) and append `"StageSkip"` to `__all__` (mirror line 28). RESEARCH Runtime-State
Inventory: this import is load-bearing for the Alembic autogenerate empty-diff contract — the model
must be on `Base.metadata` (the migration-032 test imports `phaze.models` at line 42 for exactly this).

---

### `alembic/versions/037_add_stage_skip.py` (migration) — SEAM a

**Analog:** `alembic/versions/032_add_derived_status_schema.py`, specifically the create-table block
`upgrade()` §(B) lines 130-142 and the mirrored `downgrade()` lines 167-186.

**Revision header** (`032:59-63`) — bare-number strings:
```python
revision: str = "037"
down_revision: str | Sequence[str] | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```

**Create-table** (copy `032:130-142`, adapt columns):
```python
op.create_table(
    "stage_skip",
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("stage", sa.String(), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("skipped_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint("id", name=op.f("pk_stage_skip")),
    sa.UniqueConstraint("file_id", "stage", name=op.f("uq_stage_skip_file_stage")),
    sa.ForeignKeyConstraint(["file_id"], ["files.id"], name=op.f("fk_stage_skip_file_id_files")),
)
```

**Mirrored downgrade** (copy `032:179`): `op.drop_table("stage_skip")`.

**Critical carry-overs (from `032`'s module docstring + `033`):**
- **NO `saq_jobs` reference anywhere** (032:39-41 banner). The migration test grep-asserts this.
- **NO backfill statement** — RESEARCH Runtime-State: "greenfield marker, no backfill" (unlike 032's
  `_BACKFILL_*` blocks; there is no historical "skipped" source).
- **Bare constraint names** via `op.f(...)` — the naming convention re-applies the `pk_`/`uq_`/`fk_`
  prefix; passing an already-prefixed name double-prefixes it (032:66-67 warning).
- If adding a partial index, spell the predicate as Postgres renders it (`= ANY (ARRAY[...])`, see
  `032:156` `ix_fprint_success`). A plain `UNIQUE(file_id, stage)` b-tree sidesteps this entirely.

---

### `tests/integration/test_migrations/test_037_stage_skip.py` (test) — SEAM a

**Analog:** `tests/integration/test_migrations/test_migration_032_additive_schema.py` (whole file, 284 lines).
Copy verbatim and retarget from 031→032 to 036→037. Reuse:
- `_load_migration_0XX()` path-loader (032:81-88) — the module name starts with a digit.
- `test_revision_identifiers_are_bare_numbers` (032:91-96) → assert `"037"` / `down_revision "036"`.
- `test_migration_never_references_saq_jobs` (032:99-103) — the grep guard, verbatim.
- The upgrade/seed/assert/empty-diff/downgrade body (032:165-283) — but simpler: seed a few `files`,
  insert `stage_skip` rows, assert `to_regclass('public.stage_skip')` exists, assert the
  `UNIQUE(file_id, stage)` rejects a dup, run `_diffs_touching_037` (copy `_diffs_touching_032`
  032:139-162 with `_O37_TABLES = {"stage_skip"}`), then downgrade and assert the table is dropped.
- Header/fixtures: `_build_alembic_config`, `downgrade_to`, `upgrade_to`, `MIGRATIONS_TEST_DATABASE_URL`
  from `tests/integration/test_migrations/conftest.py` (032:44-49).

---

### `src/phaze/enums/stage.py` (Python twin) — SEAM a

**Analog:** same file. Extend the DB-free twin — **stdlib-only import boundary must survive** (T-78-01;
`skipped` arrives as a `bool` scalar owned by the caller, never a model import).

**Add 5th `Status` member** (after `stage.py:52`):
```python
class Status(enum.StrEnum):
    NOT_STARTED = "not_started"
    IN_FLIGHT = "in_flight"
    DONE = "done"
    SKIPPED = "skipped"   # NEW (D-08) — reported bucket; ordered done ≻ skipped ≻ failed
    FAILED = "failed"
```

**Precedence in the enrich resolvers** — thread a `skipped` scalar into `_analyze_status`
(`stage.py:94-102`), `_metadata_status` (`105-113`), `_fingerprint_status` (`116-124`), placing the
`skipped` check **after done, before failed** (Pitfall 2 — the load-bearing precedence). Pattern to mirror
(`_analyze_status`):
```python
def _analyze_status(*, completed_at, failed_at, inflight, skipped=False):
    if inflight:  return Status.IN_FLIGHT
    if completed_at is not None:  return Status.DONE
    if skipped:  return Status.SKIPPED          # NEW — after done, before failed
    if failed_at is not None:  return Status.FAILED
    return Status.NOT_STARTED
```
`resolve_status` (`154-183`) reads `skipped = bool(scalars.get("skipped", False))` and passes it to the
three enrich branches only.

**`eligible` enrich branch** (`stage.py:237-243`) — a skipped stage is NOT eligible:
```python
status = Status(status_map.get(stage, Status.NOT_STARTED))
return status not in (Status.DONE, Status.IN_FLIGHT, Status.SKIPPED) and (status != Status.FAILED or ELIGIBLE_AFTER_FAILURE[stage])
```

**`domain_completed`** (`stage.py:202-212`) — skipped ⇒ domain-complete:
```python
st = Status(status_map.get(stage, Status.NOT_STARTED))
return st in (Status.DONE, Status.SKIPPED) or (st == Status.FAILED and FAILURE_IS_TERMINAL[stage])
```
Note the WR-03 compare-by-VALUE discipline (`stage.py:207-211`) — coerce raw strings through `Status(...)`.

---

### `src/phaze/services/stage_status.py` (SQL twin) — SEAM a

**Analog:** same file — the `done_clause` (169-197), `eligible_clause` (285-327),
`domain_completed_clause` (250-282), `stage_status_case` (364-383) house style.

**New `skipped_clause` builder** — mirror `done_clause`'s correlated-`exists` shape + the enrich-only
`ValueError` guard from `eligible_clause:320-323`:
```python
def skipped_clause(stage: Stage) -> ColumnElement[bool]:
    if stage not in ELIGIBLE_AFTER_FAILURE:              # enrich-only guard (same shape as eligible_clause)
        got = getattr(stage, "value", stage)
        raise ValueError(f"skipped_clause is defined only for the enrich stages ...; got {got!r}")
    return exists(select(StageSkip.id).where(StageSkip.file_id == FileRecord.id, StageSkip.stage == stage.value))
```
Import `StageSkip` at module top alongside `DedupResolution` (`stage_status.py:68`).

**Thread into `stage_status_case`** (`364-383`) — 5th branch, `done ≻ skipped ≻ failed`:
```python
return case(
    (inflight_clause(stage), Status.IN_FLIGHT.value),
    (done_clause(stage),     Status.DONE.value),
    (skipped_clause(stage),  Status.SKIPPED.value),   # NEW — enrich stages only
    (failed_clause(stage),   Status.FAILED.value),
    else_=Status.NOT_STARTED.value,
)
```
⚠ `skipped_clause` raises on downstream stages — guard the branch so only the three enrich stages get
it (the downstream `stage_status_case` calls must stay 4-way). `skipped ≻ failed` is load-bearing: the
writer is additive (never clears `failed_at`), so `failed_clause` still returns True; CASE order makes
`skipped` win (Pitfall 2).

**Thread into `eligible_clause`** (`324-327`) — add `~skipped` conjunct:
```python
conjuncts = [not_(inflight_clause(stage)), not_(done_clause(stage)), not_(skipped_clause(stage))]  # NEW
if not ELIGIBLE_AFTER_FAILURE[stage]:
    conjuncts.append(not_(failed_clause(stage)))
return and_(*conjuncts)
```
This propagates to all 3 enrich pending sets with **zero per-caller edits** (`get_metadata_pending_files`
:1441, `get_fingerprint_pending_files` :1480, `get_discovered_files_with_duration` :1151 all read
`eligible_clause`).

**Thread into `domain_completed_clause`** (`280-282`) — skipped disjunct so recovery never re-enqueues:
```python
disjuncts = [done_clause(stage), skipped_clause(stage)]
if FAILURE_IS_TERMINAL[stage]:
    disjuncts.append(failed_clause(stage))
return or_(*disjuncts)
```
`tasks/reenqueue.py` reads only `domain_completed_clause` → skipped file is "don't re-run" for free.

---

### `tests/integration/test_stage_status_equivalence.py` (DERIV-04 harness) — SEAM a

**Analog:** same file. This is the drift-lock RESEARCH mandates extending (never bypassing). Extend
exactly the four extension points:

1. **Seed fns** — add `seed_<stage>_skipped` fns mirroring the existing seed idiom (`seed_apply_done`
   :284-300). The load-bearing one (RESEARCH Code Example):
   ```python
   async def seed_analysis_skipped_over_failed(session):
       fid = await _new_file(session)
       session.add(AnalysisResult(file_id=fid, failed_at=datetime.now(UTC)))   # terminally-failed
       await session.flush()
       session.add(StageSkip(file_id=fid, stage="analyze", reason="corrupt source"))
       await session.flush()
       return fid
   ```
2. **`CASES`** (`306-337`) — add `(Stage.ANALYZE, seed_analysis_skipped_over_failed, "skipped")` (the
   skipped≻failed precedence cell) + metadata/fingerprint skipped cells.
3. **`ELIGIBLE_CASES`** (`502-520`) — add `(Stage.<enrich>, seed_<stage>_skipped, False)` — skipped
   leaves the pending set.
4. **`DOMAIN_COMPLETED_CASES`** (`445-460`) — add `(Stage.<enrich>, seed_<stage>_skipped, True)` —
   recovery treats skipped as complete.
5. **`load_scalars`** (`351-389`) — read a `skipped` bool from `stage_skip` for each enrich stage and
   add it to the returned dict, so the Python-twin side sees it. E.g. for analyze:
   ```python
   skipped = (await session.execute(select(StageSkip.id).where(StageSkip.file_id == file_id, StageSkip.stage == "analyze"))).first() is not None
   return {"completed_at": ..., "failed_at": ..., "inflight": inflight, "skipped": skipped}
   ```

The three parametrized tests (`test_sql_equals_python` :400, `test_domain_completed_sql_equals_python`
:471, `test_eligible_sql_equals_python` :531) then cover the new cells with zero test-body edits.

**Mutation-test discipline (Pitfall 6, project memory):** for the skipped≻failed cell and the
`~skipped` eligible cell, drop the source conjunct, watch RED, restore — a green cell proves nothing.

---

### Force-skip writer endpoint (route) — SEAM a

**Analog:** `routers/pipeline_stages.py` (validate-then-commit discipline) + `pipeline.py:934`
`retry_analysis_failed` (HTMX template response). RESEARCH Code Example:
```python
@router.post("/pipeline/files/{file_id}/skip/{stage}", response_class=HTMLResponse)
async def force_skip_stage(file_id: uuid.UUID, stage: str, reason: Annotated[str, Form()], ...):
    if stage not in STAGE_TO_FUNCTION:            # D-10 enrich-only — mirror pipeline_stages._validate_stage:53-56
        raise HTTPException(422, "stage not force-skippable")
    if not reason.strip():                        # D-09 reason required
        return <inline "A reason is required." fragment>
    reason = sanitize_pg_text(reason)             # project memory services/pg_text.py — NUL aborts PG txn
    session.add(StageSkip(file_id=file_id, stage=stage, reason=reason))  # additive-only; never clears failed_at
    await session.commit()                        # get_session does NOT auto-commit (Pitfall 7)
```
Copy the `_validate_stage` allowlist guard (`pipeline_stages.py:53-56`) and the `await session.commit()`
before returning (`pipeline_stages.py:99`). `reason` is `Form()` (x-www-form-urlencoded, HTMX default —
matches `set_priority`'s `delta: Annotated[int, Form()]` at `pipeline_stages.py:85`).

---

### `templates/pipeline/partials/_stage_pill.html` (component) — SEAM b

**Analog:** `templates/pipeline/partials/scan_status_pill.html` (whole file, 11 lines). Copy the pill
geometry verbatim and extend to 5 buckets per the UI-SPEC token table (add `skipped` = violet + `⊘` +
dashed ring). The mandatory token string (UI-SPEC + `scan_status_pill.html:6`):
```
text-xs font-semibold px-2 py-0.5 rounded-full bg-{hue}-100 dark:bg-{hue}-950 text-{hue}-700 dark:text-{hue}-400
```
Every branch carries an `aria-label` (WCAG 1.4.1 — color is never the sole channel; glyph + word +
aria-label all distinguish). The `skipped` branch adds `ring-1 ring-dashed ring-violet-400/60` and glyph
`⊘` (deliberately unlike `done`'s solid `✓`, D-08 honesty).

---

### `templates/pipeline/partials/_stage_matrix.html` (component) — SEAM b

**Analog:** NEW composition — no direct analog, but the row idiom comes from `_file_table.html` cells and
the pill loop from `scan_status_pill.html`. Renders 6 `_stage_pill` in stage order with the
**7-stage→6-pill remap LANDMINE** (RESEARCH): `Meta=metadata · FP=fingerprint · Analyze=analyze ·
Prop=propose · Appr=Stage.REVIEW · Exec=Stage.APPLY`; `tracklist` is NOT shown. Container:
`flex flex-wrap gap-2` (wraps on narrow, D-01). One legend per surface.

---

### Files-table router + `get_files_page` service query — SEAM b

**Analog (router pagination):** `routers/tracklists.py:80-153` `list_tracklists` — copy the
`page: int = Query(...)`, `page_size: int = Query(20, ge=10, le=100)`, `offset = (page-1)*page_size`,
`stmt.offset(offset).limit(page_size)`, `Pagination(page, page_size, total)` context shape (:104-153).

**Analog (correlated per-page derivation, D-00c):** `services/pipeline.py:1367-1375`
`get_cloud_staging_candidates` — the `select(FileRecord, <correlated clause>) ... .limit(...)` shape.
Build:
```python
stmt = (
    select(FileRecord, stage_status_case(Stage.METADATA), stage_status_case(Stage.FINGERPRINT), ...)
    .order_by(FileRecord.id).limit(page_size).offset(offset)   # or keyset on id (D-00c preferred)
)
```
The `stage_status_case` correlated subqueries evaluate only for the N page rows (they correlate to
`FileRecord`), never the corpus. **Never** an unbounded `COUNT(*)` per poll — either keyset-paginate
(no count) or wrap the count in `_safe_count` (`services/pipeline.py:303`). Reuse `_file_table.html`
(`:columns/:rows/:row_file_ids` contract, :9-29) for the render.

**Degrade-safe discipline:** every derived read here inherits `begin_nested()` SAVEPOINT degrade —
copy the `saq_detail` idiom (`stage_status.py:392-412`): wrap in `async with session.begin_nested()`,
`except Exception: log + return safe default`. Never 500 the 5s poll.

---

### Retire raw-enum "State" (UI-01 cutover) — SEAM b

**Retire sites** (exact): `templates/pipeline/partials/metadata_workspace.html:40-51` (the
`{'text': f.state, 'color': state_color}` cell) and `analyze_workspace.html:81-86`. Replace the State
column/cell with a `_stage_matrix` (or per-stage `_stage_pill`) cell. RESEARCH + UI-SPEC Retirement Note:
add a grep-guard test for `f.state` render sites (mutation-tested — the anti-feature table forbids
"rendering raw internal status strings").

---

### Per-file + bulk retry (UI-02) — SEAM b

**Analog:** `routers/pipeline.py:934-1014` `retry_analysis_failed` + `:1017+` `retry_metadata_failed`
(the bulk endpoints, live, Phase-30 hardened). Per-file (D-04) is a **scoped variant** — same guarded
funnel (`enqueue_router.resolve_queue_for_task` → `NoActiveAgentError` guard → `enqueue_process_file`),
but filtered to one `file_id` instead of the whole failed set. **Keep the analyze terminal-guard
(D-00b):** the analyze retry flips `ANALYSIS_FAILED → FINGERPRINTED` and clears `analysis.failed_at` in
the SAME transaction, commits BEFORE enqueue (`pipeline.py:997-1002`) — do NOT auto-loop.

**Response partials:** reuse `metadata_retry_response.html` / `retry_failed_response.html` (both are
3-branch int/bool-only acks: `no_active_agent` / `count==0` / success — no operator free-text through
Jinja). Copy verbatim, adjust the stage label.

---

### Eligibility trace (UI-03) — SEAM c

**Analog (endpoint):** `routers/record.py:41-131` `file_record` — the file_id-scoped single-row read
pattern. The trace is a single-row `resolve_status` per stage (RESEARCH: "NOT a SQL scan"). Load one
file's scalars via the `load_scalars` shape (`test_stage_status_equivalence.py:351-389`), call
`resolve_status` + evaluate the `eligible()` conjuncts in Python, and render:
`done?` · `in-flight?` · `upstream met?` · `terminal fail?`, naming the blocker from
`ELIGIBILITY_DAG[stage]` (`enums/stage.py:61-69`). For enrich stages `upstream met?` is vacuously true
(empty upstream). Trace subtlety: a skipped upstream counts as met (`stage_satisfied = done OR skipped`).

**Analog (template):** `templates/record/record_body.html` section idiom + `_file_table.html` cell
styling. `_eligibility_trace.html` renders under the clicked pill (HTMX `hx-get` on pill click).

---

### Force-skip control + confirm dialog (UI-04) — SEAM c

**Analog:** Alpine `x-trap` focus-trap dialog idiom (the record slide-in / ⌘K pattern, per UI-SPEC
Component Inventory). Enrich stages only (D-10); `<textarea>` required reason (D-09); posts to the
force-skip endpoint above. Confirm button is accent (Phaze cyan, not red — gated, not deletion); Cancel
is default focus. Copy strings from UI-SPEC Copywriting Contract.

**Right-pane host:** extend `templates/record/record_body.html` — the expanded 6-pill matrix + trace +
force-skip controls compose into the existing `#record-body` slide-in (D-02). No new pane; same host.

---

### Orphan-count helper + rail badge (UI-05) — SEAM d

**Analog (service):** `services/pipeline.py:566` `get_live_job_keys` (ledger − live) + the
`awaiting_candidate_clause` composition idiom (`stage_status.py:330-361`) + the `_safe_count` /
`begin_nested()` degrade (`stage_status.py:392-412`). RESEARCH OQ-2 recommendation: define orphan as
the per-stage recovery-candidate count (`ledger − live saq_jobs keys − domain_completed`), degrade-safe.
**Pitfall 4 (project memory):** if a staleness threshold is used, `scheduling_ledger.enqueued_at` is a
**naive** TIMESTAMP (`models/scheduling_ledger.py:63`) — compare naive-to-naive or cast, or a
`TypeError` aborts the txn. Confirm the count matches what `recover_orphaned_work` would re-enqueue (no
drift vs recovery).

**Analog (rail badge):** `shell/partials/rail.html` live-count spans (`:79,95,107,119` —
`<span class="font-mono text-xs ..." x-text="$store.pipeline.<key>">0</span>`). Add an **amber** numeral
pill near the affected enrich node (`role="status"`, `empty:hidden`/`x-show` at 0). Amber = "needs
attention, not failure" (`_lane_card.html` precedent, UI-SPEC). Rides the existing `#pipeline-stats` OOB
seed fanout — **no self-poll**. Seed the store key server-side alongside the DAG overlay
(`pipeline.py:224-236` `dag[...] = int(...)` loop).

---

### Priority stepper + pause/resume rewire (PRIO-01) — SEAM d

**Analog (backend — LIVE, do NOT rebuild):** `routers/pipeline_stages.py:82-128` — `set_priority`
(`POST /pipeline/stages/{stage}/priority`, `delta: Form()`), `pause` (`/pause`), `resume` (`/resume`).
All three return `{stage, priority, paused}` from the durable control row (`_response`, :77-79). This is
a **pure UI re-wire** — the endpoints are fully implemented + threat-modeled.

**Analog (seed):** `pipeline.py:219-227` already seeds `dag["{stage}Paused"]` / `dag["{stage}Priority"]`
into the store every 5s poll from `get_stage_controls` (degrade-safe). The store keys exist; the rail
just needs the control markup.

**Analog (rail markup):** `shell/partials/rail.html` enrich nodes (`:86-120`). Add per-enrich-stage
`▲`/`▼` steppers + pause/resume toggle. RESEARCH Code Example:
```html
<button hx-post="/pipeline/stages/analyze/priority" hx-vals='{"delta": -10}'
        aria-label="Raise analyze priority">▲</button>   <!-- lower number = sooner (D-11 tooltip) -->
```
**Do NOT resurrect the Phase-38 templates** (removed in v7.0 redesign) — build fresh rail-node steppers
against the live endpoints (RESEARCH State of the Art). Each button carries an explicit `aria-label`
(not tooltip-only) + the D-11 clarifying tooltip (UI-SPEC Copywriting).

---

## Shared Patterns

### Correlated `exists(... == FileRecord.id)` marker probe
**Source:** `services/stage_status.py:93-114` (`dedup_resolved_clause`), `169-197` (`done_clause`).
**Apply to:** `skipped_clause` (seam a), the files-table per-page derivation (seam b).
```python
return exists(select(Model.id).where(Model.file_id == FileRecord.id, <cond>))
```
Never an outer-join-null / negated-membership anti-pattern; every operand an ORM column or bound param.

### SAVEPOINT degrade-safe read (never-500 the 5s poll, D-00c)
**Source:** `services/stage_status.py:392-412` (`saq_detail`).
**Apply to:** files-table query + count, orphan count, every new derived read (seams b, d).
```python
try:
    async with session.begin_nested():
        rows = (await session.execute(stmt)).all()
except Exception:
    logger.warning("<name>_degraded", exc_info=True)
    return <safe default>
```

### Mutating router commits itself (project memory)
**Source:** `routers/pipeline_stages.py:99,113,127` (`await session.commit()`).
**Apply to:** force-skip writer (seam a), any per-file retry state flip (seam b). `get_session` NEVER
auto-commits; tests assert from an INDEPENDENT session.

### `sanitize_pg_text` before persisting free text (project memory)
**Source:** `services/pg_text.py`.
**Apply to:** the force-skip `reason` (seam a) — a NUL passes pydantic then aborts the PG txn (unbounded
recovery loop). Sanitize before store.

### Enrich-only `ValueError` guard on a Stage-dispatch builder
**Source:** `services/stage_status.py:320-323` (`eligible_clause`), `276-279` (`domain_completed_clause`).
**Apply to:** `skipped_clause` (seam a) — `if stage not in ELIGIBLE_AFTER_FAILURE: raise ValueError(...)`.
Keeps the marker enrich-only (D-10) and consistent with the twin the DERIV-04 test locks.

### Additive-only writer keeps shadow-compare green WITHOUT allowlisting
**Source:** `services/shadow_compare.py:123-151` INVARIANTS — the `analysis_failed` invariant
(`:128`) asserts `state='analysis_failed' ⇒ failed_clause(ANALYZE)`. The skip writer must NEVER clear
`analysis.failed_at` (Pitfall 3): `failed_clause` still returns True → implication holds → no false
flag, no allowlist growth (the allowlist must never grow past `{fingerprinted, local_analyzing}`,
`:145-151`). **Test the shadow gate stays green post-skip** (Wave-0 gap).

---

## No Analog Found

None. Every artifact maps to a concrete in-tree analog. The two most-novel surfaces still have close
role-matches:

| File | Role | Data Flow | Nearest analog (partial) |
|------|------|-----------|--------------------------|
| `_stage_matrix.html` | component | render | composition of `_stage_pill` (from `scan_status_pill.html`) + `_file_table.html` cell loop — no single-file analog, but both parts exist |
| force-skip confirm dialog | template | render+form | Alpine `x-trap` idiom is used elsewhere (record slide-in / ⌘K) but not as a reusable partial — pattern exists, not a copy target |

---

## Metadata

**Analog search scope:** `src/phaze/models/`, `src/phaze/services/` (stage_status, pipeline,
shadow_compare, fingerprint), `src/phaze/routers/` (pipeline, pipeline_stages, record, tracklists),
`src/phaze/enums/stage.py`, `src/phaze/templates/pipeline/partials/`, `templates/shell/partials/`,
`templates/record/`, `templates/tracklists/partials/`, `alembic/versions/032-036`,
`tests/integration/test_migrations/`, `tests/integration/test_stage_status_equivalence.py`.
**Files scanned:** ~24 read in full/part; ~40 located via glob/grep.
**Pattern extraction date:** 2026-07-11
