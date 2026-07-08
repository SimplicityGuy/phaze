# Phase 78: Derivation Layer, Eligibility & Anti-Drift Test Harness - Pattern Map

**Mapped:** 2026-07-08
**Files analyzed:** 5 new files (2 source modules + 3 test files)
**Analogs found:** 5 / 5 (all strong; one novel harness flagged)

All files this phase are **NEW** and **purely additive** — no existing reader/writer is
edited. Every predicate, key, index, and degrade idiom already exists in-tree; Phase 78
relocates and unifies them behind two modules and proves the relocation faithful with the
DERIV-04 equivalence test. The analogs below are the exact in-tree sources to copy from.

## File Classification

| New File | Role | Data Flow | Closest Analog | Match Quality |
|----------|------|-----------|----------------|---------------|
| `src/phaze/enums/stage.py` | enum / pure resolver (DB-free) | transform (scalars→status) | `src/phaze/enums/execution.py` | role-match (StrEnum, DB-free) |
| `src/phaze/services/stage_status.py` | service (SQL predicate builders) | request-response (`.where()` clauses) | `src/phaze/services/pipeline.py` (`get_untracked_files`, `get_stage_busy_counts`) | exact (same idioms) |
| `tests/shared/test_stage_resolver.py` | test (DB-free unit) | transform | `tests/shared/core/test_task_split.py` (shared, no PG) | role-match |
| `tests/shared/test_stage_eligibility_dag.py` | test (DB-free unit) | transform | `tests/shared/core/test_task_split.py` | role-match |
| `tests/integration/test_stage_status_equivalence.py` | test (real-PG parametrized) | request-response (seed→SELECT→assert) | `tests/integration/conftest.py` + `tests/integration/test_pg_dedup.py` | partial (harness reused; parametrized SQL⇔Python matrix is novel) |

**Bucket placement** (verified against `tests/buckets.json` — buckets are
`discovery, metadata, fingerprint, analyze, identify, review, agents, integration, shared`;
one bucket per file, enforced by `tests/shared/test_partition_guard.py` via the path segment
immediately under `tests/`):
- `tests/shared/…` → **`shared`** bucket (DB-free resolver + DAG + ELIG-03 terminal-failed).
- `tests/integration/…` → **`integration`** bucket (real-PG equivalence matrix + INFLIGHT-02 degrade).

---

## Pattern Assignments

### `src/phaze/enums/stage.py` (enum / pure resolver, DB-free)

**Analog:** `src/phaze/enums/execution.py` (the DB-free `StrEnum` precedent + agent-boundary docstring).

**Module docstring + StrEnum pattern** (`src/phaze/enums/execution.py:1-27`):
```python
"""Execution-status enum (DB-free).

Lives outside :mod:`phaze.models` so that :mod:`phaze.schemas.agent_execution`
(loaded inside the agent worker process) does not transitively pull in
SQLAlchemy / :mod:`phaze.database`. See Phase 26 D-03 / Plan 11.
"""

from __future__ import annotations

import enum


class ExecutionStatus(enum.StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
```
Copy this shape verbatim for `Stage` and `Status`. **Hard constraint (D-04, verified by
`tests/shared/core/test_task_split.py`):** NO `import phaze.models` / `phaze.database` /
`sqlalchemy` in this file. The resolver takes plain scalars only.

**Resolver precedence ladder** (new — hand-written twin; RESEARCH §Pattern 1/2, `in_flight ≻ done ≻ failed ≻ not_started`):
```python
def _analyze_status(*, completed_at, failed_at, inflight: bool) -> Status:
    if inflight:                  return Status.IN_FLIGHT   # DERIV-02 precedence
    if completed_at is not None:  return Status.DONE        # analysis_completed_at IS NOT NULL
    if failed_at is not None:     return Status.FAILED
    return Status.NOT_STARTED
```

**Eligibility DAG topology** (new — ELIG-01 enrich stages map to `()`):
```python
ELIGIBILITY_DAG: dict[Stage, tuple[Stage, ...]] = {
    Stage.METADATA: (), Stage.ANALYZE: (), Stage.FINGERPRINT: (),   # ELIG-01: no upstream
    Stage.TRACKLIST: (Stage.FINGERPRINT,),
    Stage.PROPOSE: (Stage.METADATA, Stage.ANALYZE),
    Stage.REVIEW: (Stage.PROPOSE,), Stage.APPLY: (Stage.REVIEW,),
}
```

**Fingerprint 1:N Python twin** (DERIV-05 — one success wins):
```python
_DONE_FP = frozenset({"success", "completed"})
def _fp_status(*, engine_statuses: list[str], inflight: bool) -> Status:
    if inflight:                                    return Status.IN_FLIGHT
    if any(s in _DONE_FP for s in engine_statuses): return Status.DONE   # DERIV-05
    if any(s == "failed" for s in engine_statuses): return Status.FAILED
    return Status.NOT_STARTED
```

---

### `src/phaze/services/stage_status.py` (service, SQL `ColumnElement[bool]` builders)

**Analog:** `src/phaze/services/pipeline.py` — the correlated `~exists(...)` anti-join
(`get_untracked_files`), the `exists(... isnot(None))` completion gate
(`get_proposal_pending_batches`), and the SAVEPOINT-wrapped `saq_jobs` read
(`get_stage_busy_counts`).

**Anti-join / EXISTS pattern** (`src/phaze/services/pipeline.py:1390`, `get_untracked_files`):
```python
stmt = select(FileRecord).where(
    FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
    ~exists(select(Tracklist.id).where(Tracklist.file_id == FileRecord.id)),
)
```
Use `~exists(...)` / `exists(...)` for EVERY predicate — never `LEFT JOIN..IS NULL`, never
`NOT IN(subquery)` (RESEARCH §Anti-Patterns / Pitfall 3: 170s cliff + 1:N under-count).

**Completion-discriminator gate — the `done(analyze)` shape** (`src/phaze/services/pipeline.py:1418-1426`):
```python
.where(
    exists(
        select(AnalysisResult.id).where(
            AnalysisResult.file_id == FileRecord.id,
            AnalysisResult.analysis_completed_at.isnot(None),   # DERIV-03: NOT bare row existence
        )
    )
)
```
This is the verbatim in-tree spelling to copy into `analyze_done_clause()`. Note the
`get_proposal_pending_batches` docstring already documents why bare `exists(AnalysisResult)`
is wrong (partial row upserted at analysis START has `completed_at NULL`).

**Verified column/table names for each `done`/`failed` builder** (read directly from the models):

| Stage | Table | `done` clause | `failed` clause | Source |
|-------|-------|---------------|-----------------|--------|
| metadata | `metadata` (1:1) | `exists(metadata WHERE file_id=F AND failed_at IS NULL)` (D-03) | `exists(metadata WHERE failed_at IS NOT NULL)` | `models/metadata.py:33` (`failed_at`), idx `ix_metadata_failed:39` |
| analyze | `analysis` (1:1) | `exists(analysis WHERE analysis_completed_at IS NOT NULL)` | `exists(analysis WHERE failed_at IS NOT NULL)` | `models/analysis.py:38,43`; idx `ix_analysis_completed:50`, `ix_analysis_failed:51` |
| fingerprint | `fingerprint_results` (1:N, unique `file_id`+`engine`) | `exists(fp WHERE status.in_(("success","completed")))` (any engine) | `~exists(success) AND exists(status='failed')` | `models/fingerprint.py:21-22`; idx `ix_fprint_success:30` (`= ANY(ARRAY['success','completed'])`), `ix_fprint_file_engine:26` |
| tracklist | `tracklists` (`file_id` nullable FK) | `exists(Tracklist WHERE file_id=F)` | (no failure marker — stays eligible) | `models/tracklist.py:32`; idx `ix_tracklists_file_id:46` |
| propose | `proposals` (`file_id` FK, `status`) | `exists(RenameProposal WHERE file_id=F)` | `exists(proposal WHERE status='failed')` | `models/proposal.py:43,47`; `ProposalStatus` (pending/approved/… `:30`) |
| review | `proposals` | `exists(proposal for F)` | — | `models/proposal.py` |
| apply | `execution_log` **JOIN** `proposals` | `exists(ExecutionLog.join(RenameProposal, ExecutionLog.proposal_id==RenameProposal.id).where(RenameProposal.file_id==F, ExecutionLog.status=='completed'))` | `... AND status='failed'` | **`execution_log` has NO `file_id`** — `models/execution.py:30` keys on `proposal_id`; `ExecutionStatus` = pending/in_progress/completed/failed |

**Fingerprint `status IN` spelling** — SQLAlchemy `.status.in_(("success","completed"))` renders
`= ANY(ARRAY[...])`, matching the Phase-77 partial index `ix_fprint_success`
(`models/fingerprint.py:30`) so both the positive EXISTS and its negation are index-only.
Reuse the Phase-59 WR-02 (`success`/`completed`) spelling — do not invent a variant.

**Precedence CASE ladder** (RESEARCH §Pattern 2) — SQL twin of the resolver:
```python
from sqlalchemy import case
def analyze_status_case() -> ColumnElement[str]:
    return case(
        (inflight_clause("process_file"), "in_flight"),   # in_flight ≻ done ≻ failed ≻ not_started
        (analyze_done_clause(),           "done"),
        (analyze_failed_clause(),         "failed"),
        else_="not_started",
    )
```

**`in_flight` from the ledger** (D-01 authoritative; RESEARCH §Pattern 5). The ledger PK is the
deterministic `"<function>:<natural_id>"` key (`models/scheduling_ledger.py:59` — `key` is
`String(255)` PK). Compose the key from `STAGE_TO_FUNCTION` (do NOT re-spell it):
```python
# services/stage_status.py — reuse STAGE_TO_FUNCTION, never a fresh f-string.
from phaze.models.scheduling_ledger import SchedulingLedger
def inflight_clause(function: str) -> ColumnElement[bool]:
    return exists(
        select(SchedulingLedger.key).where(
            SchedulingLedger.key == func.concat(function + ":", cast(FileRecord.id, String))
        )
    )
```
`STAGE_TO_FUNCTION` source of truth (`src/phaze/tasks/_shared/stage_control.py:51`):
```python
STAGE_TO_FUNCTION: dict[str, str] = {
    "metadata": "extract_file_metadata",
    "analyze": "process_file",
    "fingerprint": "fingerprint_file",
}
```
Key builders confirm the `"<function>:<file_id>"` format
(`src/phaze/tasks/_shared/deterministic_key.py:78-95` — `process_file`/`extract_file_metadata`/
`fingerprint_file`/`search_tracklist`/`push_file` are all `lambda k: str(k["file_id"])`;
`generate_proposals` is `_hash_ids(k["file_ids"])`, a SET hash → **not per-file**, so
`in_flight(propose)` is NOT derivable from a per-file key — scope `in_flight` derivation to the
file-keyed stages and omit it from `eligible(propose)` per ELIG-02, see Pitfall 5 below).

**Ledger accessors to reuse (do not reinvent)** — `src/phaze/services/scheduling_ledger.py`:
`get_ledger_rows` (`:122`, returns every row for `ledger − live keys`),
`insert_ledger_if_absent` (`:95`, `ON CONFLICT DO NOTHING`),
`upsert_ledger_entry` (`:61`), `clear_ledger_entry` (`:117`).

---

### `tests/shared/test_stage_resolver.py` (DB-free unit — DERIV-02/03, precedence, ELIG-03)

**Analog:** `tests/shared/core/test_task_split.py` (a `shared`-bucket, DB-free unit test).

**Bucket:** `shared`. No PG, no `pytestmark = pytest.mark.integration`. Fast run:
`uv run pytest tests/shared/test_stage_resolver.py -x`.

**What to assert:** every stage × {not_started, in_flight, done, failed} through the DB-free
`resolve_status(stage, scalars)`; the precedence proof (a file with BOTH `failed_at` set AND
`inflight=True` → `IN_FLIGHT`, not `FAILED`); and the **ELIG-03 terminal-failed-analyze**
regression — `eligible(f, "analyze")` is `False` when analyze status is `FAILED`
(`eligible(analyze) == (analyze_status == NOT_STARTED)`).

**ELIG-03 precedent to mirror** — `src/phaze/tasks/reenqueue.py:177-187` (`_select_done_analyze_ids`):
```python
def _select_done_analyze_ids() -> Any:
    # ANALYSIS_FAILED is DELIBERATELY treated as analyze-DONE here so a genuinely
    # un-analyzable file is NEVER auto-looped by recover_orphaned_work. ...
    # Do NOT add ANALYSIS_FAILED to a "pending" query here; that would re-introduce the auto-loop.
    return select(FileRecord.id).where(FileRecord.state.in_([FileState.ANALYZED, FileState.ANALYSIS_FAILED]))
```
The derived model's equivalent: a failed analyze is absent from the eligible set. Name a test
`test_terminal_failed_analyze*` so `uv run pytest -k "terminal_failed_analyze"` selects it
(RESEARCH §Test Map).

**Agent-boundary guard to respect** — `tests/shared/core/test_task_split.py:33` runs banned-import
checks in a subprocess; `enums/stage.py` must stay import-clean of `phaze.models`/`phaze.database`/
`sqlalchemy`. Consider extending that test (or a sibling) to assert `enums.stage` stays DB-free.

---

### `tests/shared/test_stage_eligibility_dag.py` (DB-free unit — ELIG-01/02 topology + conjuncts)

**Analog:** same as above (`tests/shared/core/test_task_split.py`, `shared` bucket, DB-free).

**What to assert:** `ELIGIBILITY_DAG` topology (enrich stages map to `()` — ELIG-01
independence: a `discovered` file simultaneously eligible for metadata/fingerprint/analyze in
any order); and `eligible()` downstream conjuncts (ELIG-02): tracklist = `done(fp) AND NOT
exists(tracklist)`; propose = `done(metadata) AND done(analyze)`; review = `exists(proposal)`;
apply = `exists(approved proposal)`.

---

### `tests/integration/test_stage_status_equivalence.py` (real-PG parametrized — DERIV-04/05, INFLIGHT-01/02, ELIG-04)

**Analog:** `tests/integration/conftest.py` (the real-PG harness — `create_async_engine`,
connectivity-probe `pytest.skip`, `saq_jobs` auto-created on `queue.connect()`) +
`tests/integration/test_pg_dedup.py` (async-def integration test shape). **The parametrized
SQL⇔Python equivalence matrix itself is novel — no existing test compares a SQL builder to a
Python twin, so this is the one file with no exact precedent (flag for the planner).**

**Bucket:** `integration`. Declare `pytestmark = pytest.mark.integration` explicitly
(belt-and-suspenders alongside the path auto-mark). Run: `just integration-test` (ephemeral PG
`:5433`).

**Harness fixture pattern** (`tests/integration/conftest.py:53-56, 110-130`) — reuse the DSN
derivation + connectivity-probe skip:
```python
BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "...")).replace("postgresql+asyncpg://", "postgresql://")
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")
# ...
try:
    probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
except psycopg.OperationalError as exc:
    pytest.skip(f"Postgres broker unavailable: {exc}")   # bare `uv run pytest` skips, not errors
```
A `db_session` `AsyncSession` bound to `SA_DSN` runs the `ColumnElement` builders; the same DB's
`saq_jobs` (auto-created on `queue.connect()`) backs the INFLIGHT-02 degrade sub-test.

**Parametrized matrix shape** (RESEARCH §Validation Architecture — the drift-lock):
```python
@pytest.mark.parametrize("stage,seed_fn,expected", CASES)   # rows=7 stages × cols=4 statuses (+edges)
async def test_sql_equals_python(db_session, stage, seed_fn, expected):
    file_id = await seed_fn(db_session)                     # writes output rows + optional ledger row
    sql_status = await eval_sql_status(db_session, stage, file_id)   # run the CASE ladder in a SELECT
    scalars   = await load_scalars(db_session, stage, file_id)       # same rows → DB-free resolver
    py_status = resolve_status(stage, scalars)
    assert sql_status == py_status == expected                       # the anti-drift lock
```
Required edge cells: analyze `completed_at NULL` → `not_started`; analyze `failed_at` set + ledger
row → `in_flight` (precedence); fingerprint `[success, failed]` → `done` (DERIV-05); fingerprint
`[failed]`-only stays eligible (ELIG-04); metadata failure-only row → `failed` not `done` (D-03).

**INFLIGHT-02 SAVEPOINT-degrade sub-test:** seed a file `in_flight` via a ledger row; run the
corroborating `saq_jobs` read against (a) present table and (b) a `DROP/RENAME TABLE saq_jobs`
inside the test — assert (a) enriches the queued-vs-active detail, (b) rolls back the nested scope
alone, returns the safe default, and `in_flight` still reads `True` from the ledger with no raise.
Name it `test_*savepoint_degrade*` (`uv run pytest -k "savepoint_degrade"`).

---

## Shared Patterns

### SAVEPOINT-wrapped corroborating `saq_jobs` read (D-02, INFLIGHT-02)
**Source:** `src/phaze/services/pipeline.py:488-499` (`get_stage_busy_counts`) — the verbatim
in-tree degrade idiom (7 occurrences: `pipeline.py` `get_stage_busy_counts:488`,
`get_live_job_keys:524`, `count_inflight_jobs`; `reenqueue.py` `backfill_ledger_from_saq_jobs:499`;
`review.py:61,104,173,220`).
**Apply to:** the `saq_detail` helper in `services/stage_status.py`.
```python
_STAGE_BUSY_SQL = text("SELECT split_part(key, ':', 1) AS fn, COUNT(*) AS n FROM saq_jobs WHERE status IN ('queued', 'active') GROUP BY fn")

out: dict[str, int] = {"metadata": 0, "analyze": 0, "fingerprint": 0}
try:
    async with session.begin_nested():                 # SAVEPOINT — rolls back ALONE on error
        rows = (await session.execute(_STAGE_BUSY_SQL)).all()
except Exception:
    logger.warning("stage_busy_degraded", exc_info=True)
    return out                                          # degrade → keep in_flight from the ledger
```
**Why `begin_nested()` not `rollback()`:** a plain `session.rollback()` expires the caller's
already-loaded ORM objects and 500s the next lazy load; the SAVEPOINT recovers the aborted
transaction without expiring them (`pipeline.py:479-485` docstring; RESEARCH Pitfall 2). Static
SQL only — the sole literals are the status allowlist (`'queued'`, `'active'`); no interpolated
operand (T-45 read-only-probe discipline). `saq_jobs` has NO `function` column — bucket by the
key prefix via `split_part(key, ':', 1)`. **Alembic must NEVER reference `saq_jobs`** (Phase-77
banner carried forward; Phase 78 adds no migration).

### Correlated anti-join (`~exists` / `exists`), never `LEFT JOIN..IS NULL` / `NOT IN`
**Source:** `src/phaze/services/pipeline.py:1390` (`get_untracked_files`) and `:1418-1426`
(`get_proposal_pending_batches`).
**Apply to:** every `done`/`failed`/`eligible` builder in `services/stage_status.py`.
Pure ORM / bound params, NO f-string SQL. The Phase-77 partial indexes (`ix_analysis_completed`,
`ix_analysis_failed`, `ix_metadata_failed`, `ix_fprint_success`) already back these EXISTS probes.

### Deterministic ledger key — reuse `STAGE_TO_FUNCTION`, never re-spell
**Source:** `src/phaze/tasks/_shared/stage_control.py:51` (`STAGE_TO_FUNCTION`) +
`src/phaze/tasks/_shared/deterministic_key.py:78-95` (`_KEY_BUILDERS`, the
`"<function>:<file_id>"` format) + `src/phaze/models/scheduling_ledger.py:59` (the `key` PK).
**Apply to:** `inflight_clause()` in `stage_status.py` and the Python twin's key construction.
A re-spelled key silently mismatches the real ledger PK.

### DB-free `StrEnum` + agent import boundary
**Source:** `src/phaze/enums/execution.py:1-27` (docstring + `enum.StrEnum`);
`src/phaze/enums/__init__.py:1-7` (the boundary rationale).
**Apply to:** `enums/stage.py`. Enforced by `tests/shared/core/test_task_split.py` — NO
`phaze.models` / `phaze.database` / `sqlalchemy` imports in the resolver module.

---

## No Analog Found

| File / Concern | Role | Data Flow | Reason |
|----------------|------|-----------|--------|
| `tests/integration/test_stage_status_equivalence.py` (the parametrized **SQL⇔Python equivalence matrix**) | test | request-response | No existing test compares a `ColumnElement` builder against a hand-written Python twin. The **harness** (real-PG session, connectivity-probe skip, `saq_jobs` auto-create) is fully precedented in `tests/integration/conftest.py`; the **equivalence-assertion pattern** (`sql_status == py_status == expected`) is novel to this phase — this is intentional, it is the DERIV-04 drift-lock (D-04). Use the RESEARCH §Validation-Architecture skeleton. |
| `in_flight(propose)` derivation | — | — | `generate_proposals` is keyed on `sha256(sorted file_ids)` (a SET hash, `deterministic_key.py:85`), NOT per-file — no per-file ledger key exists. **Out of scope** for Phase 78: ELIG-02 defines propose eligibility purely as `done(metadata) AND done(analyze)`; omit `in_flight` from `eligible(propose)` and document the limitation (RESEARCH Pitfall 5 / Open Q1). |

---

## Metadata

**Analog search scope:** `src/phaze/enums/`, `src/phaze/services/`, `src/phaze/tasks/_shared/`,
`src/phaze/tasks/reenqueue.py`, `src/phaze/models/`, `tests/integration/`, `tests/shared/`,
`tests/buckets.json`.
**Files scanned:** ~18 (enums/execution, enums/__init__, services/pipeline, services/scheduling_ledger,
tasks/reenqueue, tasks/_shared/stage_control, tasks/_shared/deterministic_key,
models/{analysis,metadata,fingerprint,execution,proposal,tracklist,scheduling_ledger},
tests/integration/conftest, tests/integration/test_pg_dedup, tests/shared/core/test_task_split,
tests/shared/test_partition_guard, tests/buckets.json).
**Pattern extraction date:** 2026-07-08
</content>
</invoke>
