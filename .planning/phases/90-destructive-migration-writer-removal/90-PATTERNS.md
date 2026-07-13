# Phase 90: Destructive Migration & Writer Removal - Pattern Map

**Mapped:** 2026-07-12
**Files analyzed:** 3 create + ~20 modify (migration, guard test, migration test; ~11 reader files/sites, ~17 writer sites, model+exports)
**Analogs found:** 23 / 23 (every new/modified file has a concrete in-tree analog)

> Sequencing (D-09, readers-first): **PR-A** reader cutovers → **PR-B** writer removals → **PR-C** destructive `039` + `FileState` deletion + guard test. Every excerpt below cites verified live line numbers (2026-07-12 tree).

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `alembic/versions/039_*.py` (**new**) | migration | batch / transform | `alembic/versions/038_retire_legacy_sentinel.py` | exact (sibling revision) |
| `tests/integration/test_migrations/test_migration_039_*.py` (**new**) | test | batch | `tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py` | exact |
| `tests/shared/.../test_no_filestate_guard.py` (**new**) | test | transform (source-grep) | `tests/shared/test_no_raw_state_render.py` | exact (source-assertion + mutation self-test) |
| `services/pipeline.py` `get_analysis_failed_count` (PR-A) | service | CRUD (count) | `get_awaiting_cloud_count` (`pipeline.py:1357`) | exact (already-cutover count) |
| `services/pipeline.py` `get_pushing_count` / `get_pushed_count` (PR-A) | service | CRUD (count) | `get_awaiting_cloud_count` (`pipeline.py:1357`) + `get_inadmissible_count` (`:1381`) | exact |
| `services/pipeline.py` `get_analyze_stage_files` (PR-A) | service | CRUD (multi-row read) | `get_cloud_staging_candidates` (`:1503`) + itself already sidecar-joined | role-match |
| `services/pipeline.py` `get_proposal_pending_batches` (PR-A) | service | batch (pending-set) | `get_discovered_files_with_duration` (`:1322`, `eligible_clause`) | exact |
| `services/pipeline.py` `_backfill_candidates_stmt` (PR-A) | service | transform (Select) | `failed_clause(Stage.ANALYZE)` composition (`get_awaiting_cloud_count`) | role-match |
| `services/pipeline.py` `get_files_by_state` / `get_analysis_failed_files` (PR-A) | service | CRUD | `failed_clause(Stage.ANALYZE)` (`stage_status.py:222`) | role-match (delete-or-repoint) |
| `routers/pipeline.py:1040` `held_files` (PR-A) | route | request-response | `get_cloud_staging_candidates` candidate set (`:1503`) | role-match (drop redundant in-mem filter) |
| `routers/pipeline.py:1247` `retry_analysis_failed` reader (PR-A) | route | request-response | `failed_clause(Stage.ANALYZE)` | role-match |
| `services/search_queries.py:66,88` facet (PR-A, D-11) | service | CRUD (search) | (deletion — no derived analog; see No Analog) | delete |
| `services/dedup.py:270→346` `previous_state` (PR-A decouples `undo_resolve` gate; `:270`/`:274`/`:346` removed together in PR-B — see dedup section, do NOT drop `:270` in PR-A) | service | event-driven (undo) | `dedup_resolved_clause()` (`stage_status.py:94`) | role-match |
| ~17 writer sites (PR-B) | service/route | (deletion) | n/a — pure deletion of dual-writes | exact (delete) |
| `models/file.py` (PR-C) — drop `state` col, `ix_files_state`, `FileState` class | model | schema | `models/file.py:86,97` + `alembic/versions/012*` index-drop precedent | exact |
| `models/__init__.py` (PR-C) — drop `FileState` re-export | model | config | `models/__init__.py:9,36` | exact |

## Shared derived-source builders (PR-A readers compose these VERBATIM)

**Source:** `src/phaze/services/stage_status.py` — the SQL `ColumnElement` twin of `enums/stage.py`, drift-locked by `tests/integration/test_stage_status_equivalence.py` (re-spelling any predicate breaks that test — **never inline a hand-rolled `exists(...)`**).

| Builder (verified line) | Replaces legacy state |
|---|---|
| `done_clause(stage)` (`:170`) | `state == ANALYZED` / `METADATA_EXTRACTED` / `FINGERPRINTED` / `PROPOSAL_GENERATED` |
| `failed_clause(stage)` (`:222`) | `state == ANALYSIS_FAILED` |
| `inflight_clause(stage)` (`:252`) | (analyze in-flight) |
| `domain_completed_clause(stage)` (`:272`) | terminal analyze |
| `eligible_clause(stage)` (`:312`) | analyze/propose pending set |
| `awaiting_candidate_clause()` (`:358`) | `state == AWAITING_CLOUD` (drain-scoped) |
| `dedup_resolved_clause()` (`:94`) | `state == DUPLICATE_RESOLVED` |
| `applied_clause()` (`:118`) | `state == EXECUTED` |
| `stage_status_case(stage)` (`:392`) | 4-way status ladder |

Import site already present in `pipeline.py` (e.g. `eligible_clause`, `dedup_resolved_clause` used by `get_discovered_files_with_duration` at `:1348-1349`).

---

## Pattern Assignments

### `alembic/versions/039_*.py` (migration, batch/transform) — **NEW, PR-C**

**Analog:** `alembic/versions/038_retire_legacy_sentinel.py` (just-shipped sibling; the template for raw-`sa.text` + `bindparams` + raise-to-rollback + `-x` override).

**Revision header pattern** (`038:60-71`) — copy verbatim, bump ids:
```python
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import context, op

revision: str = "039"
down_revision: str | Sequence[str] | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```

**Module-level SQL constants + `bindparams` discipline** (`038:78-116, 152-167`) — every operand is a named param, **never f-stringed** (the mid-flight/violation `COUNT`s use static literals only; bandit `-s B608`, `S`-rules). Mirror `038`'s "each `raise` rolls the whole txn back" structure:
```python
row = bind.execute(sa.text(_VALIDATE_OVERRIDE).bindparams(id=override)).first()
if row is None:
    raise RuntimeError(...)   # aborts the single transaction
...
remaining = bind.execute(sa.text(_COUNT_REMAINING)).scalar_one()
if remaining != 0:
    raise RuntimeError(...)
```

**`-x` override read** (`038:125`) — reuse for the operator escape hatch (e.g. `-x force=1` to skip the guard, discretion at plan time):
```python
override = context.get_x_argument(as_dictionary=True).get("reattribute_to")
```

**NEW body (no analog — genuinely new code per RESEARCH §"Key insight"):**
- **DDL wrapper** (RESEARCH Pattern 1): `lock_timeout` + `begin_nested()` savepoint-retry around `op.drop_index("ix_files_state", table_name="files")` + `op.drop_column("files","state")`. Verify `alembic/env.py` transaction mode at plan time (Assumption A6).
- **Archive** (Pattern 2, D-10): `CREATE TABLE files_state_archive(file_id uuid PK, state varchar(30) NOT NULL, archived_at timestamptz DEFAULT now())` + `INSERT ... SELECT id, state FROM files` **before** the drop.
- **Self-guard** (Pattern 3, D-06/D-07): inline mid-flight `COUNT` + one anti-join `COUNT` per HARD shadow-compare invariant — transcribed from `services/shadow_compare.INVARIANTS` (do **NOT** import it). Gate only when the count is non-zero (empty DB → all zero → passes, avoids the `038`/CR-02 fresh-DB abort).
- **`downgrade()`** (Pattern 4, D-03/D-04/D-05): recreate col+index, single `UPDATE files SET state = CASE ...` (markers-override-then-furthest-along), D-10 restore-from-archive as primary + derived CASE fallback for post-`039` rows. Docstring enumerates lossy transients (`LOCAL_ANALYZING`/`PUSHING`/`PUSHED`/`AWAITING_CLOUD`/rollback-`FINGERPRINTED`/`MOVED`↔`executed`/`UNCHANGED`↔`failed`).

**Constraints carried from `038` docstring** (`038:42-53`): SYNC migration, NO model imports, NEVER reference `saq_jobs` (nor `scheduling_ledger`); guard SQL may read `files`, `cloud_job`, `analysis`, `metadata`, `dedup_resolution`, `proposals`, `execution_log` only. Table is `analysis` (not `analysis_results`).

---

### `tests/integration/test_migrations/test_migration_039_*.py` (test) — **NEW, PR-C**

**Analog:** `tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py`.

**Static DB-free assertions** (`038-test:96-117`) — copy the three: bare-number revision ids, `saq_jobs`-never-referenced grep guard, and the "never f-string interpolated" body-scan:
```python
def test_migration_never_references_saq_jobs() -> None:
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending
```

**Path-load helper for digit-prefixed module** (`038-test:81-88`) — reuse `importlib.util.spec_from_file_location`.

**Integration harness** (`038-test:154-178`) — the exact drive loop; **039's downgrade IS implemented** (unlike 038's `NotImplementedError`), so ADD a reversibility mirror (`downgrade_to(cfg,"038")` then assert col+index recreated + durable states restored). Still tear down via `_reset_schema` for isolation:
```python
cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
await asyncio.to_thread(upgrade_to, cfg, "038")
engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
try:
    # seed representative rows for every durable state + mid-flight/soft cases
    await asyncio.to_thread(upgrade_to, cfg, "039")
    # assert: state col + ix_files_state gone; files_state_archive populated
    # guard: seed a violation -> assert RuntimeError; empty -> assert clean pass; mid-flight -> RuntimeError
    await asyncio.to_thread(downgrade_to, cfg, "038")
    # assert: col+index recreated; durable states restored
finally:
    await engine.dispose()
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
```

**Seed helpers + `_SEED_FILE_SQL`** (`038-test:69-73, 131-146`) — reuse the raw-`text` INSERT shape (note it already writes `state, ... 'analyzed'`; adjust per-scenario state). Imports from `conftest`: `MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to` (verified `conftest.py:35,70,84,95,123`).

**FOOTGUN header** (`038-test:21-28`): export `MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test"` — `just test-bucket integration` does NOT export it; silent fallback to `:5432` (memory `reference_migrations_test_db_port`).

---

### `tests/shared/.../test_no_filestate_guard.py` (test, source-grep) — **NEW, PR-C, D-08**

**Analog:** `tests/shared/test_no_raw_state_render.py` — the in-tree source-assertion guard with a **built-in mutation self-test** (exactly the D-08 requirement: "mutation-test it — a GREEN guard proves nothing", memory `feedback_mutation_test_guard_tests`).

**Scan-root + vacuous-glob guard** (`no_raw_state:36-41, 81`) — mirror: resolve `src/phaze` root, assert the glob is non-empty so a silent empty scan can't pass vacuously:
```python
_SRC_ROOT = Path(__file__).resolve().parents[N] / "src" / "phaze"
...
assert files, f"guard scanned no source files under {_SRC_ROOT}"
```

**Specific regex + self-test pair** (`no_raw_state:44-46, 87-101`) — the load-bearing pattern: (1) a `test_no_...` that asserts zero violations across scanned files, AND (2) a `test_guard_flags_a_planted_...` that asserts the regex MATCHES a planted `.state =` / `FileState` / `files.state` string and does NOT match legitimate lookalikes. For 039 forbid: `FileState`, `files.state`, `\.state\s*=` in `src/phaze/**`. **Watch out** (memory `feedback_mutation_test_guard_tests`): a line-grep is blind to multi-line SQLAlchemy `.values(state=...)` and `.values(**splat)` — cover the `.values(` form too, and mutation-test EVERY syntactic form. Comment-stripping precedent (`no_raw_state:59-64`) so docstrings can name the retired symbols without tripping the guard.

---

### `get_analysis_failed_count` → derived (service, count) — **PR-A**

**Analog:** `get_awaiting_cloud_count` (`pipeline.py:1357`) — the already-cutover `_safe_count` + clause-builder pattern.

**Current (to replace)** (`pipeline.py:1304-1307`):
```python
return await _safe_count(
    session,
    select(func.count(FileRecord.id)).where(FileRecord.state == FileState.ANALYSIS_FAILED),
    node="analysis_failed",
)
```
**Cutover:** replace `.where(FileRecord.state == FileState.ANALYSIS_FAILED)` with `.where(failed_clause(Stage.ANALYZE))` (`stage_status.py:222` → `exists(analysis WHERE file_id AND failed_at IS NOT NULL)`). Keep `_safe_count`, keep `node="analysis_failed"`. Same shape applies to `get_analysis_failed_files` (`:1284`) and `_backfill_candidates_stmt` (`:1549`) and the `retry_analysis_failed` reader (`routers/pipeline.py:1247`).

---

### `get_pushing_count` / `get_pushed_count` → `cloud_job.status` (service, count) — **PR-A, D-12**

**Analog:** `get_awaiting_cloud_count` (`:1357`) + `get_inadmissible_count` (`:1381`, which already counts `CloudJob` by `status.in_([...])`).

**Current** (`pipeline.py:1472-1476, 1487-1491`):
```python
select(func.count(FileRecord.id)).where(FileRecord.state == FileState.PUSHING)   # pushing
select(func.count(FileRecord.id)).where(FileRecord.state == FileState.PUSHED)    # pushed
```
**Cutover (D-12 pinned mapping):** count `CloudJob` rows:
- `pushing = cloud_job.status IN ('uploading','submitted')`
- `pushed  = cloud_job.status IN ('uploaded','running')`

Mirror `get_inadmissible_count`'s `select(func.count(CloudJob.id)).where(CloudJob.status.in_([...]))`. Keep `_safe_count`, keep `node="pushing"` / `node="analyzing_cloud"`. `CloudJobStatus` is imported already (`stage_status.py:67`; `pipeline` uses it at `:1398`). Under `--profile drain` both are ~0 at migration time (Pitfall 3).

---

### `get_analyze_stage_files` → drop `state` from row dict (service, multi-row read) — **PR-A**

**Analog:** its own already-sidecar-joined body + `get_cloud_staging_candidates` (`:1503`).

**Current couplings to remove** (`pipeline.py:1031, 1042, 1051, 1066, 1070`):
```python
FileRecord.state,                                                    # :1031  SELECT column
.where(or_(FileRecord.state.in_(_ANALYZE_STAGE_STATES), AnalysisResult.id.is_not(None)))  # :1042
...
"state": state,                                                      # :1066  dict value
"completed": state == FileState.ANALYZED,                           # :1070
```
**Cutover:** replace the `_ANALYZE_STAGE_STATES` membership predicate with the derived analyze-stage set (compose `done_clause`/`failed_clause`/`inflight_clause(ANALYZE)` + `awaiting_candidate_clause`, or `AnalysisResult.id.is_not(None)` union already present). Derive `"completed"` from `done_clause(Stage.ANALYZE)` / the joined `analysis_completed_at`, not `state == ANALYZED`. Drop the `"state"` dict key; the template (`analyze_workspace.html:100,102`) compares `f.state == 'awaiting_cloud'`/`'analysis_failed'` — switch those to derived boolean flags in the dict (the row still degrades to `[]` inside the SAVEPOINT — keep that, `:1024-1048`).

---

### `get_proposal_pending_batches` → derived (service, batch/pending-set) — **PR-A, Pitfall 4**

**Analog:** `get_discovered_files_with_duration` (`:1322`, the `eligible_clause` pending-set cutover).

**Current** (`pipeline.py:1707`):
```python
.where(FileRecord.state.in_([FileState.ANALYZED, FileState.METADATA_EXTRACTED]))
```
**Cutover (Pitfall 4 — the state filter did DOUBLE duty):** the two existing `EXISTS` clauses (`:1708` metadata + `:1715-1722` analysis-completed) already cover done(metadata)∧done(analyze). Replace the state filter with **`~done_clause(Stage.PROPOSE)`** (`~exists(proposals WHERE file_id)`) to preserve the "exclude already-proposed" exclusion — a naive delete would re-propose files that already have proposals. Equivalent to `eligible_clause(Stage.PROPOSE)`.

---

### `held_files` in-memory sub-filter (route) — **PR-A**

**Analog:** `get_cloud_staging_candidates` (`:1503`) already scopes to `awaiting_candidate_clause()`.

**Current** (`routers/pipeline.py:1040`): `held_files = [... if file.state == AWAITING_CLOUD]`. **Cutover:** the candidate source already returns only awaiting rows → **drop the redundant in-memory `state == AWAITING_CLOUD` filter** (RESEARCH Code Examples). This is the subtlest coupling (ledger-seed for held files) — add explicit test coverage (Wave 0 gap; likely uncovered, Pitfall 1 warning).

---

### `services/search_queries.py` facet — **DELETE, PR-A, D-11**

**No derived analog** — a free-text `state` facet has no single derived scalar (per-stage model is multi-valued). **Delete** both sites:
```python
FileRecord.state.label("state"),                 # :66  result column
if file_state:
    file_q = file_q.where(FileRecord.state == file_state)   # :87-88  filter
```
Also remove the `file_state: str | None = None` param (`:47`), the tracklist-exclusion branch keyed on it (`:90`), and the search route + template surfaces that pass/render `file_state` / the `state` result column (grep the route + `templates/**/search*`). No replacement (appropriate for single-user admin tool).

---

### `services/dedup.py` `previous_state` capture/restore — **PR-A/PR-B**

**Analog:** `dedup_resolved_clause()` (`stage_status.py:94`) — the marker is already the authority (`dedup.py:76,187` comments confirm "marker-existence is authority, not FileRecord.state").

**Current** (`dedup.py:270, 274, 346`):
```python
file_states.append({"id": str(f.id), "previous_state": f.state})   # :270 capture (undo payload)
f.state = FileState.DUPLICATE_RESOLVED                             # :274 WRITER (dies PR-B)
... update(...).values(state=restore_by_id[...])                   # :346 WRITER (undo restore, dies PR-B)
```
**Cutover (CORRECTED 2026-07-12 — the naive "drop :270 in PR-A" premise below was the plan-checker's silent-break blocker; do NOT follow it):** the `:270` `previous_state` capture and the `:274`/`:346` state writers are a **matched set removed together in PR-B**. `:270` MUST stay alive through PR-A. **Why:** `undo_resolve` (`dedup.py:311-343`) builds `restore_by_id` from `previous_state`, then `if not restore_by_id: return 0` runs **before** the marker `DELETE` (`:337-342`), and the DELETE is scoped to `restore_by_id` — so dropping `:270` in PR-A empties the gate and the marker is **never deleted → undo silently no-ops** (existing tests hand-craft payloads containing `previous_state`, so they stay green). **PR-A's only dedup work (Plan 90-01 Task 4):** decouple `undo_resolve`'s DELETE id-set + early-return gate to key on `entry["id"]` **alone** (independent of `previous_state`/`FileState`), keeping `:270`/`:274` intact, and add a real `/resolve`→`/undo` round-trip test (single + bulk) that extracts the server-rendered payload and asserts the `DedupResolution` marker IS deleted. The `:270` capture + `:274`/`:346` writers then die together in PR-B.

---

### ~17 writer sites — **PURE DELETION, PR-B**

No analog needed — delete the dual-writes (each annotated "dies in Phase 90"). Full verified inventory (RESEARCH Pitfall 1):

| # | Site | Form |
|---|------|------|
| 1 | `routers/agent_files.py:111` | `data["state"]=DISCOVERED` (INSERT stamp) |
| 2 | `routers/agent_metadata.py:106` | `update(...).where(state==DISCOVERED).values(state=METADATA_EXTRACTED)` — **CAS embeds a state READ** |
| 3 | `routers/agent_analysis.py:247` | `.values(state=ANALYZED)` |
| 4 | `routers/agent_analysis.py:382` | `.values(state=ANALYSIS_FAILED)` (comment at :380) |
| 5 | `routers/pipeline.py:999` | `file.state=DISCOVERED` |
| 6 | `routers/pipeline.py:1129` | `f.state=FINGERPRINTED` |
| 7 | `routers/pipeline.py:1273` | `file.state=FINGERPRINTED` |
| 8 | `routers/agent_push.py:151` | `.values(state=PUSHED)` |
| 9 | `routers/agent_push.py:306` | `.values(state=AWAITING_CLOUD)` |
| 10 | `routers/agent_s3.py:128` | `update(...).where(state==PUSHING).values(state=PUSHED)` — **CAS embeds a state READ** |
| 11 | `routers/agent_s3.py:232` | `.values(state=AWAITING_CLOUD)` |
| 12 | `services/backends.py:124` | `AWAITING_CLOUD` |
| 13 | `services/backends.py:304` | `LOCAL_ANALYZING` |
| 14 | `services/backends.py:395` | `PUSHING` (compute) |
| 15 | `services/backends.py:508` | `PUSHING` (kueue) |
| 16 | `services/dedup.py:274` | `f.state=DUPLICATE_RESOLVED` |
| 17 | `services/dedup.py:346` | `.values(state=restore_by_id[...])` (undo restore) |

**Note #2 and #10:** the CAS-guard `.where(state==...)` embeds a READ — deleting the writer must also remove/repoint that guard (the CAS is redundant once the derived path is authority; confirm no behavior depends on the read-side of the CAS before deleting).

---

### `models/file.py` + `models/__init__.py` — **PR-C schema deletion**

**Analog:** the `Index` line + the `__table_args__` edit precedent (drop-column pairs 1:1 with removing the ORM `Index`).

**Delete** (`models/file.py`):
```python
state: Mapped[str] = mapped_column(String(30), nullable=False, default=FileState.DISCOVERED)  # :86
...
Index("ix_files_state", "state"),          # :97  (paired with op.drop_index in 039)
...
class FileState(enum.StrEnum): ...          # :20-71  entire class
```
**Delete re-export** (`models/__init__.py:9, 36`): `from phaze.models.file import FileRecord, FileState` → drop `FileState`; remove `"FileState"` from `__all__`. Also tidy the comment-only ref at `config.py:619` (D-context §code_context). RESEARCH: 26 `src/phaze/**` files reference `FileState` — planner enumerates the full import-removal set; mypy/ruff (D-08 primary guard) surfaces any missed reader immediately.

---

## Shared Patterns

### Migration scaffolding (raw `sa.text`, `bindparams`, raise-to-rollback, `-x` override)
**Source:** `alembic/versions/038_retire_legacy_sentinel.py` (lines 60-71 header, 78-116 SQL consts, 125 `-x` read, 141-167 body). **Apply to:** `039`. Frozen-in-time: NO `phaze.services.*` imports (D-07); NEVER `saq_jobs`/`scheduling_ledger`.

### `_safe_count` degrade discipline
**Source:** `services/pipeline.py:1371-1378` (`get_awaiting_cloud_count`). **Apply to:** every PR-A count reader (`get_analysis_failed_count`, `get_pushing_count`, `get_pushed_count`) — keep `_safe_count` + the same `node=` tag so the hot 5s `/pipeline/stats` poll never 500s.

### Derived clause builders (single-source predicates)
**Source:** `services/stage_status.py` (`done_clause`/`failed_clause`/`inflight_clause`/`domain_completed_clause`/`eligible_clause`/`awaiting_candidate_clause`/`dedup_resolved_clause`). **Apply to:** ALL PR-A readers. Drift-locked by `tests/integration/test_stage_status_equivalence.py` — compose verbatim, never re-spell the inner `exists(...)`.

### Mutation-tested source-grep guard
**Source:** `tests/shared/test_no_raw_state_render.py` (vacuous-glob assert `:81`; planted-match self-test `:87-101`; comment-strip `:59-64`). **Apply to:** the D-08 `FileState`/`files.state`/`.state =`/`.values(state=` guard.

### Migration integration harness + `:5433` footgun
**Source:** `test_migration_038_*.py:154-178` (drive loop) + `conftest` helpers (`_reset_schema`/`upgrade_to`/`downgrade_to`/`_build_alembic_config`) + `:21-28` footgun header. **Apply to:** `test_migration_039_*.py`. Export `MIGRATIONS_TEST_DATABASE_URL` at `:5433`.

## No Analog Found

| File / site | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `039` DDL wrapper (`lock_timeout`+savepoint-retry) | migration | transform | No prior migration drops a hot-path column; genuinely new (RESEARCH Pattern 1). `038` is a data-only migration with no DDL. |
| `039` `files_state_archive` create+snapshot | migration | transform | No prior archive-table pattern in `alembic/versions/`; new (Pattern 2, D-10). |
| `039` inline shadow-compare guard SQL | migration | transform | Logic exists in `services/shadow_compare.INVARIANTS` but MUST be transcribed (D-07 forbids importing it); no migration-side precedent. |
| `039` `downgrade()` reconstruction CASE | migration | transform | `038`/`035`/`036` downgrades are `NotImplementedError`/no-op; the derived-reconstruction `UPDATE ... CASE` is new (Pattern 4). |
| `search_queries.py` `file_state` facet | service | CRUD | Pure deletion (D-11) — no derived replacement for a free-text state facet. |

## Metadata

**Analog search scope:** `alembic/versions/`, `tests/integration/test_migrations/`, `tests/shared/`, `src/phaze/services/{pipeline,stage_status,search_queries,dedup,shadow_compare}.py`, `src/phaze/models/{file,__init__}.py`, `src/phaze/routers/{pipeline,agent_*}.py`.
**Files scanned:** ~14 read in full/targeted; inventory cross-checked against RESEARCH Pitfall 1 (grep-verified line numbers).
**Pattern extraction date:** 2026-07-12
</content>
</invoke>
