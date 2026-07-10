# Phase 84: Dedup & Fingerprint-Progress Cutover — Research

**Researched:** 2026-07-09
**Domain:** SQLAlchemy 2.0 async data-model cutover (marker-existence predicate + derived progress counts) over a live Postgres corpus, plus a data-only repair migration
**Confidence:** HIGH — every claim below was read directly from source on the `SimplicityGuy/phase-84` branch (base `main` @ `6855cfe2`). No external packages; zero new dependencies.

---

## Summary

Phase 84 has three seams, all lockable against real code that I read in full:

1. **Migration `035`** — a data-only, bidirectional reconcile of `dedup_resolution` against `files.state`. The template is `alembic/versions/034_backfill_cloud_awaiting.py` (Phase 83) *exactly*: sync `upgrade()`, a single `op.execute(sa.text(STATIC_SQL))`, no DDL, no model import, empty autogenerate diff, idempotent via `ON CONFLICT ... DO NOTHING`. The insert half is `032`'s `_BACKFILL_DEDUP` re-run **verbatim**; the delete half is one new static statement. `035` is free (latest on disk is `034`).

2. **The dedup writer + undo + nine readers.** `services/dedup.py` today never imports `DedupResolution` — it stamps `f.state = FileState.DUPLICATE_RESOLVED` and reads `state != DUPLICATE_RESOLVED` at nine sites. This phase adds the go-forward `pg_insert(DedupResolution)...on_conflict_do_nothing` writer in `resolve_group`, converts `undo_resolve` to a `DELETE ... RETURNING file_id` CAS, and flips the nine reads to a `~dedup_resolved_clause()` marker predicate that lives in `services/stage_status.py` (D-13).

3. **`get_fingerprint_progress`** (`services/fingerprint.py:256-295`) — keep the 3-key `{total, completed, failed}` contract, redefine each body over `MUSIC_VIDEO_TYPES` + the dedup predicate + `done_clause`/`failed_clause(Stage.FINGERPRINT)`. All DB imports stay function-local (agent-worker boundary, D-00e).

**Primary recommendation:** Follow the Phase 83 template one-for-one. Split into three PRs in the CONTEXT's natural-seam order — (a) `035` + migration test; (b) writer + undo + nine readers + divergence/source guards; (c) `get_fingerprint_progress` + real-DB integration test. `035` MUST land before (b).

**Number-change callout for the SUMMARY of PR (c):** `completed` will visibly JUMP (it currently reads `state == FINGERPRINTED`, whose sole writer is `retry_analysis_failed` — counts ~nothing) and `failed` will visibly DROP (it currently counts `fingerprint_results` *rows*, double-counting a two-engine failure and misclassifying a one-success/one-failure file as failed). These are the fix, not a regression.

**One tension the planner MUST resolve (not a contradiction):** D-00a keeps the dual-write `f.state = FileState.DUPLICATE_RESOLVED` in `resolve_group` (dies Phase 90), yet D-14's source scan says `FileState.DUPLICATE_RESOLVED` "no longer appears" in `dedup.py`. Both are satisfiable only if the scan targets **read/comparison** positions, not the surviving writer assignment. See Pitfall 1 and Validation Architecture.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Dedup group discovery / stats reads | API / Backend (`services/dedup.py`) | Database (marker table) | UI-facing service, module-level model imports allowed |
| Dedup marker write / undo (CAS) | API / Backend (`services/dedup.py` in caller-owned txn) | Database | `resolve_group`/`undo_resolve` are the single funnels; router `get_session` commits |
| Fingerprint progress counts | API / Backend controller (`get_fingerprint_progress`) | Database (output tables + partial index) | Called only by `routers/pipeline.py`; module is agent-worker-imported so DB imports stay function-local |
| Dedup-resolved file-level predicate | Shared predicate module (`services/stage_status.py`) | — | Phase-78 single-source predicate home; consumed by both dedup (module-level) and fingerprint (function-local) |
| Corpus reconcile | Migration (`alembic/versions/035_*`) | — | Data-only, pre-cutover, `files.state` is still authority |
| Cutover proof | Test tiers (migration test, divergence test, real-DB fingerprint test, shadow-compare integration + live run) | — | Nyquist sampling |

---

## Current Source Shape

All line numbers verified against the branch. Quotes are the real clauses so the planner writes concrete `<action>` text.

### `services/dedup.py` — module imports (lines 3-10)

```python
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
```

`DedupResolution` is **NOT imported** — confirming D-01 (no go-forward writer exists). `update` is imported only for `undo_resolve`'s per-file loop.

### The nine `state != DUPLICATE_RESOLVED` read sites

Exact clause at each site (`FileRecord.state != FileState.DUPLICATE_RESOLVED`):

| Line | Function | Context |
|------|----------|---------|
| 78 | `find_duplicate_groups` | `dup_hashes` subquery `.where(...)` |
| 90 | `find_duplicate_groups` | main `select(FileRecord)` `.where(...)` |
| 128 | `find_duplicate_groups_with_metadata` | `dup_hashes` subquery `.where(...)` |
| 141 | `find_duplicate_groups_with_metadata` | main outer-join `select` `.where(...)` |
| 188 | `count_duplicate_groups` | `subq` `.where(...)` |
| 209 | `get_duplicate_stats` | `dup_hashes` subquery `.where(...)` |
| 221 | `get_duplicate_stats` | `stats_stmt` `.where(... , FileRecord.state != FileState.DUPLICATE_RESOLVED)` (2nd positional arg) |
| 235 | `get_duplicate_stats` | `max_per_group_subq` `.where(... , FileRecord.state != ...)` (2nd positional arg) |
| 260 | `resolve_group` | selection `.where(... , FileRecord.state != FileState.DUPLICATE_RESOLVED)` (3rd positional arg) |

Note sites 221/235/260 pass the clause as a **positional argument inside `.where(a, b, c)`**, not chained `.where().where()` — the AST source scan must walk `Call` keyword/positional args, not just `Compare` nodes at statement top level (see Pitfall 1).

Each becomes `~dedup_resolved_clause()` (a correlated `NOT EXISTS(marker)` — Pattern 1). The `.where(FileRecord.state != FileState.DUPLICATE_RESOLVED)` disappears; nothing else in these queries changes (the `sha256_hash` grouping, `LIMIT/OFFSET`, ordering stay).

### `resolve_group` (lines 251-271) — the writer site

```python
async def resolve_group(session, group_hash, canonical_id) -> tuple[int, list[dict[str, Any]]]:
    stmt = select(FileRecord).where(
        FileRecord.sha256_hash == group_hash,
        FileRecord.id != canonical_id,
        FileRecord.state != FileState.DUPLICATE_RESOLVED,   # line 260
    )
    result = await session.execute(stmt)
    files = result.scalars().all()

    file_states: list[dict[str, Any]] = []
    for f in files:
        file_states.append({"id": str(f.id), "previous_state": f.state})  # line 267
        f.state = FileState.DUPLICATE_RESOLVED                            # line 268 (DUAL-WRITE — stays)

    await session.flush()
    return len(file_states), file_states
```

- **D-02 "ids are free" is TRUE:** the ORM objects are already loaded into `files` (line 263) and iterated (266-268). `[f.id for f in files]` costs nothing.
- **`previous_state` capture is TRUE:** `f.state` is read into the payload at line 267 *before* the stamp at 268. The `[{id, previous_state}]` payload round-trips through the browser.
- Line 268's `f.state = FileState.DUPLICATE_RESOLVED` **stays** (D-00a dual-write). This is the surviving `FileState.DUPLICATE_RESOLVED` occurrence in `dedup.py` that the source scan must tolerate.
- Return signature `tuple[int, list[dict]]` is unchanged — `routers/duplicates.py:151,214` unpack it as `(count, file_states)`.

**New writer to add** (after the loop, before/with the flush), using the repo's established `pg_insert` idiom (see `agent_analysis.py:205,222`):

```python
from sqlalchemy.dialects.postgresql import insert as pg_insert  # module-level import OK in dedup.py
from phaze.models.dedup_resolution import DedupResolution
...
if files:
    rows = [{"id": uuid_mod.uuid4(), "file_id": f.id, "canonical_file_id": canonical_id} for f in files]
    await session.execute(
        pg_insert(DedupResolution).values(rows).on_conflict_do_nothing(index_elements=["file_id"])
    )
```

Pitfall 2 (id stamping) and Pitfall 3 (`canonical_file_id`) apply.

### `undo_resolve` (lines 274-286) — the CAS site

```python
async def undo_resolve(session, file_states: list[dict[str, Any]]) -> int:
    count = 0
    for entry in file_states:
        file_id = uuid_mod.UUID(entry["id"]) if isinstance(entry["id"], str) else entry["id"]
        stmt = update(FileRecord).where(FileRecord.id == file_id).values(state=entry["previous_state"])
        await session.execute(stmt)   # UNCONDITIONAL per-file update — the bug D-06 closes
        count += 1
    await session.flush()
    return count
```

Becomes (D-05/D-06): one `DELETE ... RETURNING file_id` as the CAS anchor, then restore `previous_state` **only** to the returned ids. See Pattern 2 and Pitfall 4. Callers (`routers/duplicates.py:177,242`) pass `parsed_states = json.loads(file_states)` and don't use the return count meaningfully for control flow, so a smaller "actually restored" count is safe.

### `get_fingerprint_progress` (`services/fingerprint.py:256-295`)

```python
async def get_fingerprint_progress(session: AsyncSession) -> dict[str, int]:
    from sqlalchemy import func, select                                   # function-local (D-00e)
    from phaze.models.file import FileRecord, FileState                   # function-local
    from phaze.models.fingerprint import FingerprintResult               # function-local

    eligible_states = { FileState.METADATA_EXTRACTED, FileState.FINGERPRINTED, FileState.ANALYZED,
        FileState.PROPOSAL_GENERATED, FileState.APPROVED, FileState.REJECTED,
        FileState.EXECUTED, FileState.DUPLICATE_RESOLVED }
    total = (await session.execute(select(func.count(FileRecord.id)).where(FileRecord.state.in_(eligible_states)))).scalar_one()
    completed = (await session.execute(select(func.count(FileRecord.id)).where(FileRecord.state == FileState.FINGERPRINTED))).scalar_one()
    failed = (await session.execute(select(func.count(FingerprintResult.id)).where(FingerprintResult.status == "failed"))).scalar_one()
    return {"total": total, "completed": completed, "failed": failed}
```

- **DB imports are all function-local** — the whole body's imports live inside the function. After cutover the `FileState` import can be dropped entirely (no writer here), and new function-local imports appear: `from phaze.services.pipeline import MUSIC_VIDEO_TYPES`, `from phaze.services.stage_status import done_clause, failed_clause, dedup_resolved_clause` (or whatever D-13 names it), `from phaze.enums.stage import Stage`. **All function-local** (Pitfall 5).
- The docstring at 259-261 currently describes the OLD contract — must be rewritten.
- Sole caller: `routers/pipeline.py:1339` (`GET /api/v1/fingerprint/progress`). No template consumes it. `justfile:500` curl recipe + `docs/api.md:35` reference it; the 3-key shape must be preserved.

**Target bodies (D-09/D-10/D-11):**
```python
total     = count(files) where file_type.in_(MUSIC_VIDEO_TYPES) AND ~dedup_resolved_clause()
completed = count(files) where done_clause(Stage.FINGERPRINT)          # rides ix_fprint_success
failed    = count(files) where failed_clause(Stage.FINGERPRINT)
```
See Pitfall 8 for the `completed ⊆ total` reachability question the planner must settle.

### `services/pipeline.py:46` — `MUSIC_VIDEO_TYPES` (D-10 denominator)

```python
MUSIC_VIDEO_TYPES = [ext.lstrip(".") for ext, cat in EXTENSION_MAP.items() if cat in (FileCategory.MUSIC, FileCategory.VIDEO)]
```
Already the enrich-stage denominator (used at `pipeline.py:336,947,1391,1460`). It lives in `services/pipeline.py`, which imports models at module level — so `fingerprint.py` must import it **function-locally** (Pitfall 5).

### `services/stage_status.py` — where D-13's predicate lands

`done_clause(Stage.FINGERPRINT)` (lines 102-104) and `failed_clause(Stage.FINGERPRINT)` (131-136) already implement DERIV-05 aggregation and are consumed unchanged. The module does **not** currently import `DedupResolution`. D-13's new predicate must:
- import `DedupResolution` (add to the model imports, lines 66-73),
- be a **file-level** clause (not a `Stage`), e.g. `def dedup_resolved_clause() -> ColumnElement[bool]: return exists(select(DedupResolution.id).where(DedupResolution.file_id == FileRecord.id))`,
- stay **out of** the `Stage` dispatch ladders (`done_clause`/`failed_clause`/`inflight_clause`/`domain_completed_clause`/`stage_status_case`), all of which `raise ValueError` on unknown stages and are drift-locked by the Phase-78 equivalence test (`tests/integration/test_stage_status_equivalence.py`). Adding a non-Stage clause does not touch that test.

Note: `services/shadow_compare.py:85-87` already has a private `_dedup_exists()` with the identical body. The planner MAY have shadow_compare reuse the new canonical clause, but it is not required (keeping shadow_compare's private copy is harmless).

### `services/shadow_compare.py:135` — the HARD invariant this phase must keep green

```python
Invariant("duplicate_resolved", FileState.DUPLICATE_RESOLVED.value, _dedup_exists, soft=False, doc_ref="§6.1 #15"),
```
`soft=False` ⇒ any `state='duplicate_resolved'` file with no marker is a **hard** divergence. Every post-`032` resolution (which stamped state but wrote no marker — D-01) violates this. `035` repairs it; SC#3 requires the gate green after cutover.

### `models/dedup_resolution.py`

- `id`: `mapped_column(UUID, primary_key=True, default=uuid.uuid4)` — **Python-only default** (Pitfall 2).
- `file_id`: unique FK to `files.id`, `nullable=False`. The `uq_dedup_resolution_file_id` implicit index serves both the `on_conflict (file_id)` target and the marker-EXISTS lookup.
- `canonical_file_id`: nullable FK to `files.id`.
- `resolved_at`: `server_default=func.now()` — safe to omit in `pg_insert.values()`.
- **D-08 docstring requirement:** the model docstring must note that `scan_deletion.py:108` un-resolves duplicates when their canonical file is deleted (dual-FK delete), so it isn't rediscovered as a bug.

### `services/scan_deletion.py:102-109` (D-08 — left as-is)

```python
(
    DedupResolution.__tablename__,
    delete(DedupResolution).where(DedupResolution.file_id.in_(files_of_batch) | DedupResolution.canonical_file_id.in_(files_of_batch)),
),
```
Deletes markers matching **either** FK. Unchanged by this phase (D-08). Only action: the docstring note in the model.

---

## Migration 035 Template

### Structural contract (from `034` + its test — copy exactly)

- **Filename:** `alembic/versions/035_<slug>.py` (e.g. `035_reconcile_dedup_resolution.py`). `035` is free — latest on disk is `034_backfill_cloud_awaiting.py` (orchestrator-confirmed).
- **Revision identifiers:** bare-number strings.
  ```python
  revision: str = "035"
  down_revision: str | Sequence[str] | None = "034"
  branch_labels: str | Sequence[str] | None = None
  depends_on: str | Sequence[str] | None = None
  ```
- **`upgrade()` is SYNC** — plain `def upgrade() -> None:`, only `env.py` is async. Body is `op.execute(sa.text(STATIC_SQL))` per statement. **No DDL**, no model import, no interpolation/f-string/`.format`.
- **Two statements** (D-04):
  1. `032`'s `_BACKFILL_DEDUP` **verbatim** (insert missing markers).
  2. A `DELETE` of orphaned markers (`marker exists AND files.state <> 'duplicate_resolved'`).
- **CRITICAL banner:** must never reference `saq_jobs` (020/031/032 rule; the migration-test asserts this).
- **`downgrade()`** — Claude's Discretion (D-04): documented-lossy `DELETE` (034 precedent) **or** a no-op. Whichever, **document the choice in the docstring**.
- **Empty autogenerate diff:** because `035` touches no ORM-mapped schema, `compare_metadata(ctx, Base.metadata)` yields no `035`-scoped diff. The test asserts this.

### `_BACKFILL_DEDUP` verbatim (from `032:84-94`) — statement 1

```python
_BACKFILL_DEDUP = """
INSERT INTO dedup_resolution (id, file_id, canonical_file_id, resolved_at)
SELECT gen_random_uuid(), f.id,
       (SELECT c.id FROM files c
        WHERE c.sha256_hash = f.sha256_hash AND c.state <> 'duplicate_resolved'
        ORDER BY c.id LIMIT 1),
       COALESCE(f.updated_at, now())
FROM files f
WHERE f.state = 'duplicate_resolved'
ON CONFLICT (file_id) DO NOTHING
"""
```

### Statement 2 (new — orphaned-marker delete, D-04)

Static, parameter-free, no `saq_jobs`. Shape:
```sql
DELETE FROM dedup_resolution dr
USING files f
WHERE dr.file_id = f.id AND f.state <> 'duplicate_resolved'
```
(Equivalent single-table form: `DELETE FROM dedup_resolution WHERE file_id IN (SELECT id FROM files WHERE state <> 'duplicate_resolved')`.) Both directions ⇒ `marker ≡ state` exactly at the cutover instant. Failure mode is safe: a wrongly-deleted marker makes its file reappear for re-review; a wrongly-kept one would hide it forever (which the delete half is precisely there to prevent).

### Migration-test contract (from `test_migration_034_backfill_cloud_awaiting.py` — mirror it)

- **DB-free assertions (no Postgres):**
  - `test_revision_identifiers_are_bare_numbers` — `revision == "035"`, `down_revision == "034"`, `branch_labels is None`. Loads the module by path via `importlib.util.spec_from_file_location` (name starts with a digit).
  - `test_migration_never_references_saq_jobs` — line scan excluding the banner comment.
  - `test_backfill_sql_is_static_and_parameter_free` — asserts `"ON CONFLICT (file_id) DO NOTHING"` present, `".format("` absent, `f'''`/`f"""` absent.
- **Integration body** (`@pytest.mark.asyncio`), using `tests/integration/test_migrations/conftest`'s `MIGRATIONS_TEST_DATABASE_URL`, `_build_alembic_config`, `downgrade_to`, `upgrade_to`:
  1. `downgrade_to(cfg, "base")` then `upgrade_to(cfg, "034")`; open `create_async_engine(MIGRATIONS_TEST_DATABASE_URL)`.
  2. Seed a corpus with the `_SEED_FILE_SQL` helper (FK to the `012`-seeded `legacy-application-server` agent). Design corpus for **both directions**:
     - a `state='duplicate_resolved'` file with **no** marker → gains one (`canonical_file_id` derived by the subquery);
     - an **orphaned** marker (`state<>'duplicate_resolved'`, marker present) → deleted;
     - a control non-resolved file with no marker → stays row-less;
     - a resolved file with a **pre-existing** marker → `DO NOTHING`, unchanged, no duplicate.
  3. Snapshot `files.state` before; assert byte-unchanged after (`035` is READ-ONLY on `files.state`).
  4. `upgrade_to(cfg, "035")`; assert per-file marker presence/absence and counts.
  5. **Idempotency:** re-execute `module._BACKFILL_DEDUP` in a fresh `engine.begin()`; assert no duplicate rows (`GROUP BY file_id HAVING count(*) > 1` == `[]`) and stable counts.
  6. **Empty diff:** `conn.run_sync(_diffs_touching_035)` with `_O35_TABLES/_INDEXES/_COLUMNS` = empty sets (035 touches no ORM schema) ⇒ offenders `== []`.
  7. **Downgrade:** run it, assert its documented effect; then `DELETE FROM dedup_resolution` cleanup before the `finally: downgrade_to(cfg, "base")`.
- **FOOTGUN (copy the docstring warning):** the migration DB is on port **5433** (`just test-db`), but `conftest.MIGRATIONS_TEST_DATABASE_URL` defaults to **5432**. `just test-bucket` does NOT export `MIGRATIONS_TEST_DATABASE_URL`. Run with it exported explicitly:
  ```
  MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test" \
    just test-bucket integration
  ```

---

## Validation Architecture

> nyquist_validation is enabled (no `workflow.nyquist_validation: false` found). This section is mandatory.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config | `pyproject.toml`; buckets in `tests/buckets.json` |
| Quick run (a bucket) | `just test-bucket <bucket> [XDIST]` (DB buckets serial) |
| Full integration | `just integration-test` (spins ephemeral PG:5433 + Redis:6380, auto-teardown) |
| Migration test invocation | `MIGRATIONS_TEST_DATABASE_URL=...5433...phaze_migrations_test just test-bucket integration` |

### Sampling: each Success Criterion → a provable surface

**SC#1 (dedup reads/writes the marker; undo = DELETE; backfilled rows honored; no state read).**
Sampled by the **mutation-proof divergence test** (D-14, load-bearing) + the **source scan** (D-14, insurance) + a **resolve→undo→re-resolve integration test**.

- **Why a consistent corpus can't prove it:** on `marker ≡ state`, "reads marker" and "reads state" return identical rows. The divergence test MUST seed a deliberately **inconsistent** corpus:
  - File A: **marker present, `state='analyzed'`** → must be **EXCLUDED** from every dedup reader (marker wins over state).
  - File B: **`state='duplicate_resolved'`, no marker** → must be **INCLUDED** (backfilled/absent-marker file reappears; marker is authority).
  - (Plus a normal duplicate pair sharing a hash so groups are non-empty.)
- **Readers it must cover** (all six selection surfaces): `find_duplicate_groups`, `find_duplicate_groups_with_metadata`, `count_duplicate_groups`, `get_duplicate_stats`, `resolve_group`'s selection, and `get_fingerprint_progress`'s `total` denominator.
- **The mutation that must turn it RED for each reader:** revert that reader's `~dedup_resolved_clause()` back to `FileRecord.state != FileState.DUPLICATE_RESOLVED`. Then File A (marker+`analyzed`) is wrongly INCLUDED and File B (`duplicate_resolved`+no marker) is wrongly EXCLUDED — both assertions invert. Mutation-test before close: break each reader, watch RED, restore.

- **Source scan (insurance) — the Phase-83 trap and the fix:** Phase 83 shipped a line-oriented `grep` that passed against buggy source because SQLAlchemy splits a call across lines (auto-memory `feedback_mutation_test_guard_tests`). Here the target token is the attribute access `FileState.DUPLICATE_RESOLVED`, which is a single unbroken token chain even when the enclosing `.where(...)` spans lines — so a naive grep would actually *fire* on the surviving dual-write at `dedup.py:268` and produce a **false positive**. **Use an AST scan** (`ast.walk` over `services/dedup.py` / `services/fingerprint.py`) matching `ast.Attribute` nodes with `.attr in {"DUPLICATE_RESOLVED", "FINGERPRINTED"}` whose value resolves to `FileState`. Then:
  - **`fingerprint.py`:** after cutover it has **no** writer and should not import `FileState` at all → assert **zero** `FileState.FINGERPRINTED` (and zero `FileState.*` read) occurrences. Clean.
  - **`dedup.py`:** the dual-write `f.state = FileState.DUPLICATE_RESOLVED` at line 268 **must survive** (D-00a, dies Phase 90). So the scan must **allow exactly the one occurrence that is the RHS of an `ast.Assign` to `<name>.state`**, and assert **no** occurrence appears inside a `Compare` node or as an argument to a `.where(...)`/`.filter(...)`/`.having(...)` `Call`. Equivalent simpler rule the planner may prefer: assert the only `FileState.DUPLICATE_RESOLVED` node in `dedup.py` is reachable from an `Assign` target ending in `.state` — count == 1, and it is a write. **Mutation-test both false-negative (reintroduce a read → RED) and false-positive (the surviving writer must stay GREEN).**
  - **False-positive risk on `.where()` readers:** the nine former read sites pass the clause positionally (`.where(a, b, c)` at 221/235/260), so an AST rule keyed only on chained `.where(<Compare>)` would miss them. Walk `Call` args (positional + keyword) for `where`/`filter`/`filter_by`, not just top-level compares.

- **resolve→undo→re-resolve integration test:** on a synthetic real-PG corpus, assert marker inserted on resolve, marker DELETEd on undo (and only returned ids get `state` restored — a stale replay of a file re-resolved since matches 0 rows and no-ops), marker re-inserted on re-resolve, and zero hard divergences from `run_shadow_compare` throughout (this is D-16.1, below).

**SC#2 (`get_fingerprint_progress` derives from output tables, not state).**
Sampled by the **real-DB replacement for `tests/fingerprint/services/test_fingerprint.py:295`** (D-15).

- I read `test_fingerprint.py:291-309` (`TestGetFingerprintProgress.test_get_progress_returns_counts`). It is exactly the stub-and-assert-your-own-dict shape CONTEXT describes: it builds `mock_session = AsyncMock()`, sets `mock_session.execute = AsyncMock(side_effect=[mock_result_total, mock_result_completed, mock_result_failed])` with `.scalar_one.return_value` = 100/50/5, then asserts `result == {"total": 100, "completed": 50, "failed": 5}`. It stays green through **any** rewrite of the predicates, including a wrong one. **Replace it** with a real-DB integration test.
- **Corpus that pins D-10/D-11/DERIV-05 in one test** (seed via the real-PG `db_session` idiom):
  - a **music** file (`file_type` in MUSIC_VIDEO_TYPES) with a `fingerprint_results` row `status='success'` → counts toward `total` and `completed`;
  - a **video** file, no fingerprint rows → `total` only;
  - a **non-audio** file (e.g. `file_type='txt'/'jpg'`) → excluded from `total` entirely (D-10);
  - a **dedup-resolved duplicate** (marker present) that is a music type → excluded from `total` (D-10 marker exclusion);
  - a file with **one engine success + one engine failure** → counts `completed` (DERIV-05: one success wins), NOT `failed` — this is the D-11 reclassification;
  - a file with **all engines failed** → counts `failed` (D-11), not `completed`.
- **Acceptance (must go RED on regression):** revert `completed` to `state == FileState.FINGERPRINTED` → the all-fingerprinted-via-`success`-row files vanish from `completed` and the assertion fails. Also assert the row-vs-file distinction: the two-engine-failure file must add **1** (not 2) to `failed`, and the one-success/one-failure file must add **0** to `failed`.

**SC#3 (shadow-compare stays green after cutover).**
Proven two ways (D-16).

- **D-16.1 — committed integration test:** assert `run_shadow_compare(session).hard_fail_total == 0` after `resolve → undo → re-resolve` on a synthetic corpus. Construct `DedupResolution` rows using the exact idiom at `tests/integration/test_shadow_compare.py:157`: `session.add(DedupResolution(file_id=file_id))` (id via ORM Python default; `canonical_file_id` optional in the marker). The real-PG `db_session` fixture (test_shadow_compare.py:84-113) creates all tables via `Base.metadata.create_all`, seeds the `legacy-application-server` agent, and rolls back per test. This gates every future PR.
- **D-16.2 — live-corpus `shadow_compare` run after `035`, before merge:** the committed CI test can't see the real post-`032` resolved-without-marker rows; only the live run proves `035` covered them. The runner exists today:
  - **justfile recipe** (`justfile:482-485`): `just shadow-compare [ARGS]` → `uv run python -m phaze.cli.shadow_compare {{ARGS}}`.
  - **CLI** (`src/phaze/cli/shadow_compare.py`): stdlib-argparse over the same `run_shadow_compare` core; `--database-url` for a live-corpus restore (password-safe via `make_url`), `--sample-cap` (non-negative int), `--verbose`. **Exit code 1 iff any HARD invariant diverged** (D-05).
  - **"Zero hard divergences" output:** the `Report.render()` TOTALS line reads `TOTALS: hard_fail_total=0, soft_divergence_total=<N>` and the process exits 0. The `duplicate_resolved` invariant line must show `0 divergent`. (Phase 79 built the gate re-runnable but deferred the live run — 79 D-02 — which is why D-01 went unnoticed. Run it here after `035`.)

### Wave 0 Gaps
- [ ] `tests/integration/test_migrations/test_migration_035_*.py` — the `035` contract (mirror `034`'s test).
- [ ] The dedup **divergence test** (inconsistent-corpus, six readers) — new file, `integration` bucket (needs real PG for the marker + state seed).
- [ ] The dedup **source-scan** AST guard — DB-free (can live in `shared` bucket; scans source text).
- [ ] The **resolve→undo→re-resolve** shadow-compare integration test (D-16.1).
- [ ] **Replace** `tests/fingerprint/services/test_fingerprint.py:291-309` with the real-DB integration test (D-15). Note: it currently lives in the `fingerprint` bucket as a **mock** unit test; the replacement needs real PG, so it likely moves to `integration` (or the `fingerprint` bucket gains a PG-backed test — see Test Infrastructure isolation note).
- No framework install needed (pytest/pytest-asyncio already present).

---

## Test Infrastructure

**Real-Postgres session for integration tests.** Two established idioms:
- `tests/integration/test_shadow_compare.py:84-113` — a self-contained `db_session` fixture: probe broker connectivity (`psycopg.AsyncConnection.connect(BROKER_DSN)`; `pytest.skip` if down), `create_async_engine(SA_DSN)`, `Base.metadata.create_all`, seed the `legacy-application-server` Agent (for the `files.agent_id` RESTRICT FK), `async_sessionmaker(..., expire_on_commit=False)`, yield one session, `rollback()` in `finally`. **Copy this fixture** for the new dedup/fingerprint integration tests.
- `tests/integration/conftest.py` — the Phase-37 `stage_env` fixture (queue + session on the same DB); heavier, only needed if a test touches `saq_jobs` (this phase does not).

**DSN derivation (identical in both):**
```python
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace("postgresql+asyncpg://", "postgresql://")
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")
```
**Destructive-write guard:** test_shadow_compare refuses any DB whose name doesn't end in `_test` (`make_url(SA_DSN).database`). Reuse this guard if a new test commits/truncates.

**Ports & DB names (auto-memory `reference_migrations_test_db_port`):**
- App/integration test DB: **5433** (`just test-db` provisions `phaze_test` + `phaze_migrations_test` via `postgres:18-alpine`); Redis **6380**.
- `MIGRATIONS_TEST_DATABASE_URL` defaults to **5432** in conftest — a footgun. Migration tests need it exported to **5433**; `just test-bucket` does NOT export it. `just integration-test` DOES export both `TEST_DATABASE_URL` and `MIGRATIONS_TEST_DATABASE_URL` (justfile:214-215).

**Buckets (`tests/buckets.json`):** `["discovery","metadata","fingerprint","analyze","identify","review","agents","integration","shared"]`. Each is a top-level directory under `tests/`. Dedup service tests currently live under `tests/review` (dedupe is the Review/Apply surface) — verify the exact path when planning; fingerprint mock tests are in `tests/fingerprint`; migration + PG-backed cross-cutting tests go in `tests/integration`.

**Per-bucket isolation demands (auto-memory `reference_ci_bucket_isolation`):** new tests must pass in isolation via `just test-bucket <bucket>`, not only in the full suite. Two documented non-hermetic hazards:
- **`get_settings` lru_cache leak** — a test that constructs `Settings` can poison a later test's cached settings. Clear/override the cache in fixtures if you touch settings.
- **`saq_jobs` stub poison** — this phase does not read `saq_jobs`; keep it that way (no SAQ dependency in dedup/fingerprint-progress tests).
- **Local full-suite colima flake** (`reference_local_fullsuite_colima_flake`): DB-heavy runs flake under VM pressure; re-run the failed subset in isolation to confirm infra-not-regression. **Do not** set `PHAZE_QUEUE_URL=redis`.

---

## Pitfalls

1. **The source scan must tolerate the surviving dual-writer (Phase-83 grep trap, inverted).** `resolve_group:268` keeps `f.state = FileState.DUPLICATE_RESOLVED` (D-00a). A grep or an AST rule that asserts the string is *absent* from `dedup.py` will FALSE-POSITIVE on this line. The guard must allow exactly the one write occurrence (RHS of an `Assign` to `*.state`) and forbid every *read* occurrence (inside a `Compare` or a `where/filter/having` `Call`, positional args included — sites 221/235/260 are positional). Mutation-test BOTH directions: reintroduce a read → RED; the surviving writer stays GREEN. `fingerprint.py` has no writer, so there the assertion is a clean "zero `FileState.FINGERPRINTED`."

2. **`pg_insert(DedupResolution).values(...)` bypasses the Python-only PK default — stamp `id` explicitly.** `DedupResolution.id` uses `default=uuid.uuid4` (a Python-side default), which `pg_insert` does not fire (identical to `agent_analysis.py:204` / `agent_metadata.py:161-163`, which stamp `"id": uuid.uuid4()` for exactly this reason). Omit it and the INSERT fails on a NULL PK. `resolved_at` has a `server_default=func.now()` so it may be omitted.

3. **`canonical_file_id` must carry the operator's actual pick (D-03), and stays `DO NOTHING` (D-07).** `resolve_group` receives `canonical_id` from the comparison table — set `canonical_file_id=canonical_id` on every inserted row (strictly better than `032`'s `ORDER BY c.id LIMIT 1` guess). Do NOT switch to `on_conflict_do_update`: post-cutover the selection filters `~dedup_resolved_clause()`, so a marker-bearing file is never in the insert set; the conflict fires only on a genuine concurrent HTMX double-submit, where first-writer-wins is correct.

4. **`DELETE ... RETURNING file_id` in async, and gate the state restore on it (D-06).**
   ```python
   from sqlalchemy import delete
   ids = [UUID(e["id"]) if isinstance(e["id"], str) else e["id"] for e in file_states]
   result = await session.execute(
       delete(DedupResolution).where(DedupResolution.file_id.in_(ids))
       .returning(DedupResolution.file_id)
       .execution_options(synchronize_session=False)   # ORM-DELETE-with-RETURNING async hygiene (scan_deletion.py:119 precedent)
   )
   returned = set(result.scalars().all())               # the file_ids that actually had a marker
   ```
   Then restore `previous_state` **only** for `entry["id"] in returned` (Claude's Discretion: N per-file `UPDATE`s or one `UPDATE ... FROM (VALUES ...)` — but only returned ids). A stale-tab replay against a since-re-resolved file finds no marker, returns 0 rows, no-ops — the CAS. Caller-owned transaction: `flush`, never `commit` (the router's `get_session` commits).

5. **The function-local-DB-import boundary in `fingerprint.py` is load-bearing (D-00e).** `services/fingerprint.py` is imported by the **agent worker**, which must not import `phaze.database` / `phaze.models` (Phase 26 Plan 10/11). Every new dependency `get_fingerprint_progress` consumes — `MUSIC_VIDEO_TYPES` (from `services.pipeline`, which imports models at module level), `done_clause`/`failed_clause`/the dedup predicate (from `services.stage_status`, ditto), `Stage` (from `enums.stage`, DB-free but keep it local for consistency) — MUST be imported **inside** the function, alongside the existing local imports. A module-level import of any of these would drag `phaze.models` into the agent-worker import graph and crash the worker at import time.

6. **`ix_fprint_success` matches `done_clause(FINGERPRINT)` exactly — the count rides the index.** The partial index is `fingerprint_results(file_id) WHERE status = ANY (ARRAY['success','completed'])` (`032:156`, mirrored `models/fingerprint.py:30`). `done_clause(Stage.FINGERPRINT)` is `exists(select(FingerprintResult.id).where(file_id==FileRecord.id, status.in_(("success","completed"))))`, and `.in_((...))` renders `= ANY (ARRAY[...])` — byte-identical predicate. So `completed = count(files) where done_clause(FINGERPRINT)`'s EXISTS probe can use the partial index. Do NOT re-spell the status set as a bare `IN` string or add a third status — it would diverge from the index predicate and the empty-autogenerate-diff contract.

7. **`find_duplicate_groups`' `LIMIT/OFFSET`-without-`ORDER BY` is pre-existing and OUT of scope — do not touch it.** The `dup_hashes` subquery applies `.limit().offset()` with no `ORDER BY` at `dedup.py:81` and `:131` (and the same shape informs `:207`), so group pagination is nondeterministic across pages. This is a deferred quick-task (CONTEXT Deferred Ideas), independent of the marker cutover. The cutover only swaps the `state != ...` `.where` clause; leave the `.limit/.offset` exactly as-is. Adding an `ORDER BY` here would be scope creep and risks changing which groups appear per page.

8. **`completed ⊆ total` reachability (D-10 vs D-11).** D-10 excludes dedup-resolved files and non-audio types from `total`; D-11 defines `completed = count where done_clause(FINGERPRINT)` with no such filter stated. A file that was fingerprinted and *then* resolved-as-duplicate (or a non-audio file that somehow has a fingerprint row) would count toward `completed` but not `total`, making `completed > total` for the progress bar. **Recommendation:** apply the same `file_type.in_(MUSIC_VIDEO_TYPES)` + `~dedup_resolved_clause()` guards to `completed` (and `failed`) so all three keys share one denominator and stay reachable. Flagged for the planner to lock explicitly (it is a small extension of D-11, consistent with D-10's stated intent). `[ASSUMED]` that this is the intended reading — confirm at plan time.

9. **`update` import becomes dead in `dedup.py` after undo is rewritten.** Line 6 imports `update` solely for `undo_resolve`'s per-file loop. If undo becomes a `delete(...).returning(...)` + a bulk restore, the `update` import may remain (if the restore uses `update`) or become unused (ruff `F401`). Adjust imports to satisfy ruff. Similarly `FileState` stays imported in `dedup.py` only for the surviving writer at line 268.

---

## Sequencing

**`035` MUST land before the dedup reader flip (D-04, load-bearing).** Before cutover, `files.state` is still the authority; `035` reconciles the derived representation *to* it so `marker ≡ state` at the cutover instant. If a reader flips to `~dedup_resolved_clause()` before `035` runs: every post-`032` resolved-without-marker file **reappears** in the dedup UI, and every orphaned-marker file **vanishes** unreachably.

**Migrations run automatically at app startup and in the integration harness.** `settings.auto_migrate` defaults to **`True`** (`config.py:385-388`, alias `PHAZE_AUTO_MIGRATE`); `main.py:87` runs `alembic upgrade head` in the API lifespan, gated by that flag. So in any live/UAT boot, `035` applies before code serves traffic. **Implication for plan/wave decomposition:** because `035` is a separate revision on the `034` chain, splitting it into its own PR/wave (seam (a)) is safe — as long as seam (a) merges and deploys before seam (b)'s reader flip. Within a single test run, the migration test drives `035` explicitly via `upgrade_to(cfg, "035")`; the divergence/integration tests use `Base.metadata.create_all` (not Alembic), so they get the `dedup_resolution` table regardless of migration order — but they must **seed markers themselves** to model the post-`035` world (they cannot rely on `035` having run).

**Recommended PR/wave order (CONTEXT Claude's Discretion, natural seams):**
1. Seam (a): migration `035` + its migration test. (No source reader touches the marker yet.)
2. Seam (b): the `pg_insert` writer in `resolve_group` + the `DELETE...RETURNING` undo + the nine reader flips + the D-13 predicate in `stage_status.py` + the divergence test + the source-scan guard + the D-16.1 resolve/undo integration test + the `scan_deletion` model-docstring note. Depends on (a).
3. Seam (c): `get_fingerprint_progress` rewrite + the D-15 real-DB test (replacing the mock). Independent of (b)'s dedup readers except for sharing the D-13 predicate; can follow (b).
4. After merge, before final sign-off: the **live-corpus `just shadow-compare`** run (D-16.2) on a restore, asserting `hard_fail_total=0`.

**Base branch:** `main` @ `6855cfe2` is sufficient. Branch `SimplicityGuy/phase-82` does not exist and the file sets are disjoint (82 owns `services/pipeline.py`; 84 owns `services/dedup.py` + `services/fingerprint.py`). Phase 84's real upstreams are 77 (`dedup_resolution` table + `ix_fprint_success`), 78 (`stage_status` predicate module), 79 (shadow gate) — all merged on `main`. Phase 82 is NOT a real prerequisite. Work branch `SimplicityGuy/phase-84` is already based on `main`; the ROADMAP `Depends on: Phase 82` is a stale declaration (roadmap-hygiene note in CONTEXT Deferred).

---

## Contradictions Found

**None that block the locked decisions.** One decision-internal tension worth surfacing (already handled above, not a contradiction):

- **D-14 source scan vs. D-00a dual-write.** D-14 says the source scan asserts `FileState.DUPLICATE_RESOLVED` "no longer appears" in `services/dedup.py`, while D-00a/D-05 keep the writer `f.state = FileState.DUPLICATE_RESOLVED` at `dedup.py:268` until Phase 90. These are jointly satisfiable **only** if the scan targets read/comparison positions and explicitly whitelists the single surviving write assignment (Pitfall 1). A literal "string absent" reading of D-14 would be impossible against the correct dual-write code — so the guard's assertion must be worded as "no `FileState.DUPLICATE_RESOLVED` in a read/where/compare position; exactly one, the writer assignment, allowed." The planner must encode this scoped form, not a bare absence check.

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `completed`/`failed` should carry the same `MUSIC_VIDEO_TYPES` + `~dedup marker` guards as `total`, to keep `completed ⊆ total` | Pitfalls #8 | Progress bar can show `completed > total`; a fingerprinted-then-resolved file miscounts. Low blast radius; confirm at plan time. |
| A2 | Dedup service tests currently live under `tests/review` (the Review/Apply/Dedupe surface) | Test Infrastructure | Wrong bucket path in a plan; trivially checked by `ls tests/` at plan time. |
| A3 | `just shadow-compare --database-url <restore>` is the exact live-run recipe (justfile:484) with no additional auth wiring for a homelab restore | Validation SC#3 | Live run may need a DSN/secret the operator supplies; the recipe itself is confirmed, only the restore-connection specifics are operator-side. |

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (test) | migration + integration tests | via `just test-db` | `postgres:18-alpine`, host port 5433 | none — required for the real-DB tiers |
| Redis (test) | `just integration-test` harness (not this phase's tests directly) | via `just test-db` | `redis:7-alpine`, port 6380 | dedup/fingerprint tests don't need it |
| uv / pytest / pytest-asyncio / alembic / sqlalchemy[asyncpg] | all | ✓ (in `pyproject.toml`) | project-pinned | none needed |

**No new packages.** Zero-dependency phase → Package Legitimacy Audit is N/A (nothing installed).

**Missing with no fallback:** none blocking — the test DB is provisioned on demand by `just test-db`. The live-corpus shadow-compare run (D-16.2) requires a homelab restore, which is operator-gated (as in Phase 79).

---

## Sources

### Primary (HIGH confidence — read directly this session)
- `src/phaze/services/dedup.py` (full) — nine read sites, `resolve_group`, `undo_resolve`.
- `src/phaze/services/fingerprint.py:240-296` — `get_fingerprint_progress`, function-local imports.
- `alembic/versions/034_backfill_cloud_awaiting.py` (full) + `tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py` (full) — the `035` template + test contract.
- `alembic/versions/032_add_derived_status_schema.py:70-165` — `_BACKFILL_DEDUP` verbatim, `dedup_resolution` DDL, `ix_fprint_success`.
- `src/phaze/services/stage_status.py` (full) — `done_clause`/`failed_clause`, the Stage dispatch ladders, house-style `exists()`.
- `src/phaze/services/shadow_compare.py` (full) — `_dedup_exists`, the hard `duplicate_resolved` invariant, `run_shadow_compare`/`Report`.
- `src/phaze/models/dedup_resolution.py`, `src/phaze/models/fingerprint.py` — Python-only PK default, unique FK, `ix_fprint_success` mirror.
- `src/phaze/routers/duplicates.py` (full) — resolve/undo/bulk endpoints, browser-held `file_states` payload.
- `src/phaze/services/scan_deletion.py:80-124` — the dual-FK marker delete (D-08).
- `src/phaze/services/pipeline.py:30-59` — `MUSIC_VIDEO_TYPES`.
- `src/phaze/routers/agent_analysis.py:200-223` — the `pg_insert ... on_conflict_do_nothing`/`do_update` idiom + explicit id stamping.
- `src/phaze/routers/agent_push.py:255-294` — CAS-via-single-domain + RETURNING/rowcount no-op precedent.
- `tests/fingerprint/services/test_fingerprint.py:291-309` — the mock-stub `get_fingerprint_progress` test to replace (D-15).
- `tests/integration/test_shadow_compare.py:1-189` — real-PG `db_session` fixture, `DedupResolution(file_id=...)` construction idiom (:157), destructive-DB guard.
- `src/phaze/cli/shadow_compare.py`, `justfile:482-485,110-217`, `tests/buckets.json`, `src/phaze/config.py:385-388`, `src/phaze/database.py:84-88`, `src/phaze/main.py:78-87` — runner, buckets, test-DB recipes, `auto_migrate` default.

### Secondary (project memory — MEDIUM)
- Auto-memory: `feedback_mutation_test_guard_tests` (Phase-83 toothless-guard failure mode), `reference_ci_bucket_isolation`, `reference_migrations_test_db_port`, `reference_local_fullsuite_colima_flake`, `project_phase81_failure_markers`.

---

## Metadata

**Confidence breakdown:**
- Current source shape: HIGH — every named site read directly, line numbers verified.
- Migration `035` template: HIGH — `034` + its test read in full; `_BACKFILL_DEDUP` quoted verbatim.
- Validation architecture: HIGH — divergence-test inversion mechanics and the AST source-scan false-positive on the surviving writer are grounded in the actual `resolve_group:268` dual-write and the positional-arg `.where(a,b,c)` sites.
- Pitfalls: HIGH — each backed by a concrete in-repo precedent (`agent_analysis.py`, `scan_deletion.py`, `ix_fprint_success` spelling).
- Sequencing / env: HIGH — `auto_migrate=True` default and the `034` revision head confirmed in source.

**Research date:** 2026-07-09
**Valid until:** ~2026-08-08 (stable; the only fast-moving risk is another migration taking `035` before this phase lands — re-check `alembic/versions/` head at plan time).

## RESEARCH COMPLETE

**Phase:** 84 — Dedup & Fingerprint-Progress Cutover
**Confidence:** HIGH

### Key Findings
- All nine `dedup.py` read sites, `resolve_group` (writer-to-add), `undo_resolve` (CAS-to-add), and `get_fingerprint_progress` were read in full; D-02 ("ids are free") and the `previous_state` capture both verified TRUE against real code.
- `035`'s template (`034` + its test) is exact and reusable; `_BACKFILL_DEDUP` is re-run verbatim + one new orphaned-marker `DELETE`; `035` is the free next revision.
- The single load-bearing subtlety: D-00a keeps `f.state = FileState.DUPLICATE_RESOLVED` at `dedup.py:268`, so D-14's source scan MUST allow that one write and forbid only reads — a naive grep false-positives (documented in Contradictions + Pitfall 1).
- Three number/behavior facts for the PR SUMMARY: `completed` jumps, `failed` drops, and both should share `total`'s denominator (Pitfall 8) — flagged `[ASSUMED A1]` for plan-time confirmation.
- Test infra: real-PG `db_session` fixture at `test_shadow_compare.py:84`; migration-test 5433/`MIGRATIONS_TEST_DATABASE_URL` footgun; `just shadow-compare` is the live-run recipe (exit 1 on hard divergence).

### File Created
`.planning/phases/84-dedup-fingerprint-progress-cutover/84-RESEARCH.md`

### Ready for Planning
Yes. Three natural PR seams, `035`-before-readers ordering confirmed, every guard's exact shape specified with its mutation test. No contradictions block the locked decisions.
