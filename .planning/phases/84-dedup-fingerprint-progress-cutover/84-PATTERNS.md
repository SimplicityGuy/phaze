# Phase 84: Dedup & Fingerprint-Progress Cutover - Pattern Map

**Mapped:** 2026-07-09
**Files analyzed:** 11 (5 modify, 6 create)
**Analogs found:** 11 / 11 (every touched file has an in-repo model or template)

All line numbers verified against the `SimplicityGuy/phase-84` branch (base `main` @ `6855cfe2`).
Excerpts are the real clauses so the planner can write concrete `<action>` text.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `alembic/versions/035_reconcile_dedup_resolution.py` | migration | batch / data-only | `alembic/versions/034_backfill_cloud_awaiting.py` | exact (same shape) |
| `tests/integration/test_migrations/test_migration_035_*.py` | test | batch / real-PG | `tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py` | exact |
| `src/phaze/services/stage_status.py` (MODIFY) | service (predicate builder) | transform (ColumnElement) | its own `done_clause`/`failed_clause` at `:89,:120,:131`; `shadow_compare._dedup_exists` at `:85-87` | exact (idiom already in file) |
| `src/phaze/services/dedup.py` (MODIFY) | service | CRUD + CAS write | writer: `routers/agent_analysis.py:204-222`; CAS-delete: `routers/agent_push.py:131-151` + `scan_deletion.py:119` | role+flow match |
| `src/phaze/services/fingerprint.py` (MODIFY) | service (controller-called) | request-response / count | its own `get_fingerprint_progress:256-295`; `stage_status.done_clause/failed_clause` | exact (in-place rewrite) |
| `src/phaze/models/dedup_resolution.py` (MODIFY) | model | — (docstring only) | its own docstring; `scan_deletion.py:102-109` (behavior to document) | exact |
| dedup **divergence test** (inconsistent corpus, real PG) | test | real-PG behavioral | `tests/integration/test_shadow_compare.py:84-113` (`db_session`), `:116-132` (`_new_file`), `:157` (`DedupResolution(...)`) | exact fixture reuse |
| **source-scan AST guard** (DB-free) | test | source assertion | `tests/analyze/services/test_single_awaiting_writer.py` (Phase 83 — the CORRECTED writer guard) | model, but READER-scan is new — see caution below |
| **resolve→undo→re-resolve** shadow-compare test | test | real-PG integration | `tests/integration/test_shadow_compare.py` fixtures + `run_shadow_compare` | exact |
| real-DB replacement for `test_fingerprint.py:291-309` | test | real-PG behavioral | `test_shadow_compare.py:84-113` fixture; `tests/integration/test_pg_dedup.py` (real-PG dedup precedent, but see SAQ caution) | role match |

---

## Pattern Assignments

### `alembic/versions/035_reconcile_dedup_resolution.py` (migration, data-only)

**Analog (copy verbatim shape):** `alembic/versions/034_backfill_cloud_awaiting.py` (read in full).

**Module + revision header** (`034:38-49`):
```python
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "035"
down_revision: str | Sequence[str] | None = "034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```

**Static SQL constants + sync upgrade/downgrade** (`034:56-81`) — note `op.execute(sa.text(...))`, no
model import, no interpolation, and the CRITICAL `saq_jobs` banner in the docstring:
```python
_BACKFILL_CLOUD_AWAITING = """
INSERT INTO cloud_job (id, file_id, status)
SELECT gen_random_uuid(), f.id, 'awaiting'
FROM files f
WHERE f.state = 'awaiting_cloud'
ON CONFLICT (file_id) DO NOTHING
"""

def upgrade() -> None:
    op.execute(sa.text(_BACKFILL_CLOUD_AWAITING))

def downgrade() -> None:
    op.execute(sa.text(_DOWNGRADE_DELETE_AWAITING))
```

**Statement 1 for `035` — re-run `032`'s `_BACKFILL_DEDUP` VERBATIM** (from RESEARCH `032:84-94`):
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

**Statement 2 for `035` — NEW orphaned-marker delete (D-04)**, static, parameter-free, no `saq_jobs`:
```sql
DELETE FROM dedup_resolution dr
USING files f
WHERE dr.file_id = f.id AND f.state <> 'duplicate_resolved'
```

**downgrade() — Claude's Discretion (D-04):** `034` chose the documented-lossy `DELETE` (`034:64-65,73-80`).
`035` may follow it or make downgrade a no-op — **document the choice in the docstring** either way.

---

### `tests/integration/test_migrations/test_migration_035_*.py` (test, real-PG)

**Analog (mirror 1:1):** `tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py`.

**DB-free assertions** (`034-test:74-106`) — copy all three:
- `test_revision_identifiers_are_bare_numbers` — loads the module by path via
  `importlib.util.spec_from_file_location` (name starts with a digit; `:74-81`), asserts
  `revision == "035"`, `down_revision == "034"`, `branch_labels is None`.
- `test_migration_never_references_saq_jobs` — line scan excluding the banner comment (`:92-96`).
- `test_backfill_sql_is_static_and_parameter_free` — asserts `"ON CONFLICT (file_id) DO NOTHING"`
  present, `".format("` absent, `f'''`/`f"""` absent (`:99-106`).

**Integration body** (`034-test:156-231`) — the exact scaffold to copy:
- `_build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)`; `downgrade_to(cfg,"base")`;
  `upgrade_to(cfg,"034")`; `create_async_engine(MIGRATIONS_TEST_DATABASE_URL)`.
- Seed helper (`:67-71,109-112`): `_SEED_FILE_SQL` INSERT with `agent_id='legacy-application-server'`
  (the `012`-seeded FK). Fixed seed UUIDs with readable last-nibble roles (`:61-65`).
- Snapshot `files.state` before, assert byte-unchanged after (`:167-169,197-199`) — `035` is READ-ONLY
  on `files.state`.
- Idempotency: re-execute `module._BACKFILL_DEDUP` in a fresh `engine.begin()`, assert no
  `GROUP BY file_id HAVING count(*) > 1` rows (`:201-213`).
- Empty diff: `_diffs_touching_035` via `conn.run_sync` with `_O35_TABLES/_INDEXES/_COLUMNS` = empty
  sets (`035` touches no ORM schema) → offenders `== []` (`:130-153,215-218`).
- Downgrade: run it, assert its documented effect, cleanup `DELETE FROM dedup_resolution`, then
  `finally: downgrade_to(cfg,"base")` (`:220-231`).

**Corpus (both migration directions — D-04):**
- `state='duplicate_resolved'`, no marker → gains one (canonical derived by the subquery);
- orphaned marker (`state<>'duplicate_resolved'`, marker present) → deleted;
- control non-resolved file, no marker → stays row-less;
- resolved file with a pre-existing marker → `DO NOTHING`, unchanged, no dup.

**FOOTGUN (copy the docstring warning `034-test:21-28`):** migration DB is on port **5433**, but
`conftest.MIGRATIONS_TEST_DATABASE_URL` defaults to **5432**, and `just test-bucket` does NOT export it:
```
MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test" \
  just test-bucket integration
```

---

### `src/phaze/services/stage_status.py` (MODIFY — add the file-level predicate, D-13)

**Analog:** the module's own correlated-`exists()` idiom, and `shadow_compare._dedup_exists`.

`done_clause`/`failed_clause` house style (`stage_status.py:102-104,131-136`):
```python
if stage is Stage.FINGERPRINT:
    return exists(select(FingerprintResult.id).where(FingerprintResult.file_id == FileRecord.id, FingerprintResult.status.in_(_DONE_FP)))
```

`shadow_compare.py:85-87` is the exact body to reproduce (identical clause already exists there):
```python
def _dedup_exists() -> ColumnElement[bool]:
    """A ``dedup_resolution`` marker row for the file (existence = resolved)."""
    return exists(select(DedupResolution.id).where(DedupResolution.file_id == FileRecord.id))
```

**Assignment:**
- Add `from phaze.models.dedup_resolution import DedupResolution` to the model imports (`stage_status.py:66-73`).
- Add a **file-level** clause (name it for what it is, e.g. `dedup_resolved_clause`) with the
  `_dedup_exists` body above. It takes NO `stage` argument.
- **Keep it OUT of the `Stage` dispatch ladders** (`done_clause`/`failed_clause`/`inflight_clause`/
  `domain_completed_clause`/`stage_status_case`, `:89,:120,:150,:170,:197`) — all raise `ValueError` on
  unknown stages and are drift-locked by `tests/integration/test_stage_status_equivalence.py`. A non-Stage
  clause does not touch that test.
- The nine dedup readers use `~dedup_resolved_clause()`; `fingerprint.py` imports it function-locally.
- `shadow_compare.py:85-87` MAY be refactored to reuse the new canonical clause but need not be (its
  private copy is harmless).

---

### `src/phaze/services/dedup.py` (MODIFY — writer + undo CAS + nine reader flips)

**Current state (read in full).** `DedupResolution` is NOT imported (`dedup.py:6-10`) — confirming D-01.
`update` (line 6) is imported only for `undo_resolve`'s loop.

**The nine `FileRecord.state != FileState.DUPLICATE_RESOLVED` read sites** (all become `~dedup_resolved_clause()`):

| Line | Function | Form |
|------|----------|------|
| 78 | `find_duplicate_groups` | `.where(state != ...)` chained |
| 90 | `find_duplicate_groups` | `.where(state != ...)` chained |
| 128 | `find_duplicate_groups_with_metadata` | `.where(state != ...)` chained |
| 141 | `find_duplicate_groups_with_metadata` | `.where(state != ...)` chained |
| 188 | `count_duplicate_groups` | `.where(state != ...)` chained |
| 209 | `get_duplicate_stats` | `.where(state != ...)` chained |
| **221** | `get_duplicate_stats` | `.where(<a>, state != ...)` **positional 2nd arg** |
| **235** | `get_duplicate_stats` | `.where(<a>, state != ...)` **positional 2nd arg** |
| **260** | `resolve_group` selection | `.where(<a>, <b>, state != ...)` **positional 3rd arg** |

Nothing else in those queries changes (the `sha256_hash` grouping, `LIMIT/OFFSET`, ordering stay).
**Do NOT touch the `LIMIT/OFFSET`-without-`ORDER BY` at `:81,:131,:207`** (Pitfall — deferred quick-task).

**Writer to ADD in `resolve_group`** (analog `routers/agent_analysis.py:202-223`) — the id-stamping is
load-bearing (Pitfall 2): `pg_insert` bypasses the Python-only `default=uuid.uuid4` PK.
```python
# routers/agent_analysis.py:202-205 — the precedent, verbatim:
# Stamp PK explicitly because AnalysisResult.id has a Python-only default, which pg_insert bypasses.
payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}
stmt = pg_insert(AnalysisResult).values([payload])
...
stmt = stmt.on_conflict_do_nothing(index_elements=["file_id"])   # :222 (empty-body branch)
await session.execute(stmt)
```
Applied to dedup (D-02/D-03/D-07 — `canonical_file_id` = operator's actual `canonical_id`, stays `DO NOTHING`):
```python
from sqlalchemy.dialects.postgresql import insert as pg_insert          # module-level OK in dedup.py
from phaze.models.dedup_resolution import DedupResolution
...
if files:
    rows = [{"id": uuid_mod.uuid4(), "file_id": f.id, "canonical_file_id": canonical_id} for f in files]
    await session.execute(
        pg_insert(DedupResolution).values(rows).on_conflict_do_nothing(index_elements=["file_id"])
    )
```
`resolve_group` already loads the ORM objects (`dedup.py:263`) and captures `previous_state` BEFORE the
dual-write (`:267` then `:268`) — D-02 "ids are free" and `previous_state` capture are both TRUE.
**Line 268's `f.state = FileState.DUPLICATE_RESOLVED` STAYS** (D-00a dual-write; dies Phase 90). This is the
one surviving `FileState.DUPLICATE_RESOLVED` occurrence the source scan must tolerate as a WRITE.

**Undo CAS to REPLACE the unconditional loop** (`dedup.py:280-284`). Analogs:
- `scan_deletion.py:117-120` — the `delete(...).execution_options(synchronize_session=False)` + `rowcount` cast idiom;
- `agent_push.py:131-151` — CAS on the sidecar domain, then gate the `FileRecord.state` dual-write on `rowcount != 0`.

`scan_deletion.py:119` (the async ORM-delete hygiene precedent):
```python
result = cast("CursorResult[Any]", await session.execute(stmt.execution_options(synchronize_session=False)))
counts[tablename] = result.rowcount
```
`agent_push.py:139-151` (the CAS-then-gated-dual-write shape, D-06's direct template):
```python
if res.rowcount == 0:
    # no-op: NO FileRecord write, NO ledger clear (a stale/late replay)
    ...
    return PushedResponse(file_id=file_id)
# rowcount != 0: gate the FileRecord dual-write behind the sidecar CAS
await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.PUSHED))
```
Applied to `undo_resolve` (D-05/D-06): one `DELETE ... RETURNING file_id` as the CAS anchor, then restore
`previous_state` ONLY to the returned ids (statement shape N-UPDATEs vs one `UPDATE...FROM(VALUES)` is
Claude's Discretion; only returned ids may be written). Caller-owned txn: `flush`, never `commit`.
`update` and `FileState` imports stay if the restore uses them; else fix ruff `F401`.

**Caller-owned transaction discipline** (enforced by the router's `get_session`): `resolve_group`/
`undo_resolve` `await session.flush()` and RETURN — they NEVER `commit`. Confirmed at `dedup.py:270,285`.

---

### `src/phaze/services/fingerprint.py` (MODIFY — `get_fingerprint_progress`, D-09..D-12,D-17)

**Analog:** its own current body (`fingerprint.py:256-295`) for the function-local-import boundary, and
`stage_status.done_clause/failed_clause` for the new counts.

**Function-local DB import boundary (D-00e — load-bearing, Pitfall 5)** — exactly how the existing locals
are written (`fingerprint.py:268-271`), inside the function, `# noqa: PLC0415`:
```python
from sqlalchemy import func, select  # noqa: PLC0415

from phaze.models.file import FileRecord, FileState  # noqa: PLC0415
from phaze.models.fingerprint import FingerprintResult  # noqa: PLC0415
```
Every NEW dependency MUST be imported the same way, INSIDE the function:
`from phaze.services.pipeline import MUSIC_VIDEO_TYPES`, `from phaze.services.stage_status import
done_clause, failed_clause, dedup_resolved_clause`, `from phaze.enums.stage import Stage`. A module-level
import of any of these drags `phaze.models` into the agent-worker import graph and crashes it at import.
After cutover the `FileState` local import is dropped (no writer here — Pitfall 1: the AST scan asserts
ZERO `FileState.FINGERPRINTED`).

**Target bodies** (all three share `total`'s denominator — D-17):
```python
denom  = FileRecord.file_type.in_(MUSIC_VIDEO_TYPES), ~dedup_resolved_clause()
total     = count(FileRecord.id) where (*denom)
completed = count(FileRecord.id) where (*denom, done_clause(Stage.FINGERPRINT))     # rides ix_fprint_success
failed    = count(FileRecord.id) where (*denom, failed_clause(Stage.FINGERPRINT))
```
`completed`/`failed` become FILE counts (D-11: `failed` was a `fingerprint_results` ROW count at `:292` —
it will visibly DROP; `completed` read `state == FINGERPRINTED` at `:288` which counts ~nothing — it will
visibly JUMP; say so in the SUMMARY). Keep the exact `_DONE_FP = ("success","completed")` spelling via
`done_clause` (Pitfall 6 — it must match the `ix_fprint_success` partial-index predicate). Rewrite the
docstring at `:257-266`. Sole caller: `routers/pipeline.py:1339`; 3-key contract preserved.

---

### `src/phaze/models/dedup_resolution.py` (MODIFY — docstring note only, D-08)

**Analog:** its own docstring (`:1-15`) and the behavior at `scan_deletion.py:102-109`.

Add a note that `scan_deletion.py:108` deletes markers matching **either** FK
(`file_id IN batch | canonical_file_id IN batch`), so deleting the scan batch holding a *keeper*
un-resolves its duplicates (they reappear for re-review). D-03 exposes every go-forward resolution to this.
Document so it is not rediscovered as a bug. No code change.

---

## Shared Patterns

### `pg_insert(...).on_conflict_do_nothing(...)` with explicit `id` stamping
**Source:** `routers/agent_analysis.py:202-222` (also `:234-237`, `:307-314`); RESEARCH names
`agent_metadata.py:161-163` as a sibling precedent.
**Apply to:** `dedup.py` `resolve_group` writer (D-02).
**Pitfall 2 (load-bearing):** `DedupResolution.id` uses `default=uuid.uuid4` — a **Python-side** default
that `pg_insert` does NOT fire (`models/dedup_resolution.py:32`). Omit it → INSERT fails on NULL PK. Stamp
`"id": uuid_mod.uuid4()` per row. `resolved_at` has `server_default=func.now()` (`:40`) — safe to omit.

### `delete(...).returning(...)` + `synchronize_session=False` + `rowcount` cast
**Source:** `scan_deletion.py:117-120` (the cast + `synchronize_session=False`), `agent_push.py:131-151`
(CAS-gate the dual-write on `rowcount`).
**Apply to:** `dedup.py` `undo_resolve` (D-06). The marker is the single CAS domain; a stale-tab replay
against a since-re-resolved file matches 0 rows and no-ops. Direct analogue of 83 D-09.

### Caller-owned transactions in `services/` — build + flush, NEVER commit
**Source:** `dedup.py:270,285` (existing), `stage_status`/all service builders. The router's `get_session`
dependency commits. **Apply to:** every write in `dedup.py`. (`agent_push.py` DOES `commit` because it is a
router, not a service — do not copy that part into the service.)

### Correlated `exists(...)` predicates only (never outer-join-null / negated-membership)
**Source:** `stage_status.py:89-104,131-136`, `shadow_compare.py:82,87,97`.
**Apply to:** the new `dedup_resolved_clause` (D-13) and the fingerprint counts.

### Real-PG `db_session` fixture (no SAQ)
**Source:** `tests/integration/test_shadow_compare.py:84-113`. Probes broker (`pytest.skip` if down),
`create_async_engine(SA_DSN)`, `Base.metadata.create_all`, seeds the `legacy-application-server` Agent
(for the `files.agent_id` RESTRICT FK), `async_sessionmaker(expire_on_commit=False)`, one rolled-back txn
per test. Helpers: `_new_file(session, state=...)` at `:116-132`, `DedupResolution(file_id=file_id)` at
`:157`. **Copy this fixture** for the divergence test, the resolve/undo/re-resolve test, and the fingerprint
replacement. **DSN derivation** at `test_shadow_compare.py` top; destructive-write guard refuses any DB
whose name does not end in `_test`.

### Live-corpus shadow-compare run (D-16.2)
**Source:** `justfile:482-485` (`just shadow-compare *ARGS` → `python -m phaze.cli.shadow_compare`);
`src/phaze/cli/shadow_compare.py` (argparse, `--database-url` password-safe via `make_url`, exit 1 iff any
HARD invariant diverged). The gate is `services/shadow_compare.py:135`
(`Invariant("duplicate_resolved", ..., _dedup_exists, soft=False)`). Run AFTER `035`, before merge.

---

## CAUTIONARY ANALOG — the source-scan AST guard (D-14)

The closest analog is **`tests/analyze/services/test_single_awaiting_writer.py`** (Phase 83). Project memory
(`feedback_mutation_test_guard_tests`) records that Phase 83 shipped **toothless** guards — but note the
distinction:

- **This test IS the CORRECTED version** and is a good structural MODEL for walking the AST. Its
  `_values_call_writes_awaiting` (`:94-115`) explicitly handles the `.values(**splat)` form
  (`keyword.arg is None`, `:100-108`) that the original scan was blind to — the comment at `:102-105` cites
  "found by 83-07 review WR-01; confirmed by mutation test." **Copy this `**splat`-aware walking discipline.**
- **BUT it guards a WRITE (`.values(...)`); Phase 84's scan must guard a READ.** That inverts the problem and
  introduces the exact trap the memory warns about:
  - The Phase-83 *grep* was toothless because SQLAlchemy splits a `.values(...)` call across lines so a
    line-oriented match missed it. **Do NOT ship a grep.** For Phase 84 a naive grep is worse: the target
    token `FileState.DUPLICATE_RESOLVED` is a single unbroken attribute chain, so a grep would **false-positive
    on the surviving dual-writer at `dedup.py:268`** (D-00a). Use an `ast.walk` scan.
  - **The nine former read sites pass the clause POSITIONALLY at `dedup.py:221,235,260`** (`.where(a, b, c)`),
    not as chained `.where(<Compare>)`. An AST rule keyed only on chained/keyword args is **blind** to them —
    the same class of blindness that made 83's `keyword.arg is None` scan toothless. **Walk `Call` positional
    AND keyword args** for `where`/`filter`/`filter_by`/`having`.

**Correct AST shape for `dedup.py`:** match `ast.Attribute` nodes with `.attr == "DUPLICATE_RESOLVED"` whose
value resolves to `FileState`. **Allow exactly one** — the RHS of an `ast.Assign` whose target ends in
`.state` (the surviving writer at `:268`). **Forbid every read** occurrence (inside a `Compare`, or as any
argument — positional included — to a `.where(...)`/`.filter(...)`/`.having(...)` `Call`).
For `fingerprint.py`: assert **zero** `FileState.FINGERPRINTED` (no writer there — clean absence).

**Mutation-test BOTH directions before the phase closes:** reintroduce a read → RED; the surviving writer
(`dedup.py:268`) stays GREEN (no false positive). Also mutation-test the divergence test per reader (revert
`~dedup_resolved_clause()` → `state != DUPLICATE_RESOLVED`, watch it invert).

---

## No Analog Found

None. Every touched file has an in-repo model or template. Two soft gaps to flag for the planner:

| Item | Note |
|------|------|
| dedup **divergence test** on an inconsistent corpus | No existing test seeds `marker` and `state` divergently — the whole point (D-14). Build it on the `test_shadow_compare.py:84-113` fixture. `tests/integration/test_pg_dedup.py` is a real-PG dedup precedent but pulls in `saq.PostgresQueue` — AVOID that dependency (saq_jobs stub-poison hazard; this phase reads no `saq_jobs`). |
| source-scan READER guard | See CAUTIONARY ANALOG above — model exists (writer guard) but the read-position scan is net-new. |

---

## Test Bucket Placement (`tests/buckets.json`)

Buckets: `["discovery","metadata","fingerprint","analyze","identify","review","agents","integration","shared"]`.

| Test file (today / new) | Current bucket | New tests land in |
|-------------------------|----------------|-------------------|
| `tests/discovery/services/test_dedup.py` (mock unit) | **discovery** | keep mock units here; the real-PG divergence test needs PG → **integration** |
| `tests/review/routers/test_duplicates.py` | review | unchanged |
| `tests/integration/test_pg_dedup.py` (real-PG, SAQ) | integration | divergence + resolve/undo tests → **integration** (reuse `test_shadow_compare` fixture, NOT the SAQ one) |
| `tests/fingerprint/services/test_fingerprint.py:291-309` (mock) | **fingerprint** | REPLACE with real-PG test → **integration** (needs PG; keep it off the mock `fingerprint` bucket) |
| `tests/integration/test_migrations/test_migration_035_*.py` | — | **integration** |
| source-scan AST guard (DB-free) | — | **shared** (scans source text; no DB) |

Per-bucket isolation (`reference_ci_bucket_isolation`): new tests must pass via `just test-bucket <bucket>`
in isolation, not only in the full suite. Avoid the `get_settings` lru_cache leak and any `saq_jobs` read.

---

## Metadata

**Analog search scope:** `alembic/versions/`, `src/phaze/services/`, `src/phaze/routers/`,
`src/phaze/models/`, `src/phaze/cli/`, `tests/integration/`, `tests/analyze/services/`,
`tests/discovery/services/`, `tests/fingerprint/services/`, `justfile`, `tests/buckets.json`.
**Files read in full or in targeted ranges:** 034 migration + its test, `dedup.py` (full), `stage_status.py`
(full), `fingerprint.py:240-296`, `agent_analysis.py:198-242`, `scan_deletion.py:78-124`,
`agent_push.py:126-155`, `shadow_compare.py:78-142`, `dedup_resolution.py` (full),
`test_shadow_compare.py:80-169`, `test_single_awaiting_writer.py` (full), `test_fingerprint.py:285-314`,
`cli/shadow_compare.py` head.
**Pattern extraction date:** 2026-07-09
</content>
</invoke>
