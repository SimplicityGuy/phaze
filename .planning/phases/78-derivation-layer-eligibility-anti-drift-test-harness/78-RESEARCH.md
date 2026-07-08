# Phase 78: Derivation Layer, Eligibility & Anti-Drift Test Harness - Research

**Researched:** 2026-07-08
**Domain:** Dual-form (SQL `ColumnElement[bool]` + DB-free Python) predicate authoring; SQLAlchemy 2.0 `EXISTS`/`NOT EXISTS` anti-joins over 1:1 and 1:N output tables; SAVEPOINT-isolated `saq_jobs` reads; parametrized SQL⇔Python equivalence testing on a live ~200K-file corpus.
**Confidence:** HIGH (every column, table, key format, index, and idiom below verified against the live tree at `SimplicityGuy/phase-78`)

## Summary

Phase 78 is **pure additive scaffolding plus an anti-drift test harness** — two new modules (`src/phaze/enums/stage.py` DB-free, `src/phaze/services/stage_status.py` SQLAlchemy) and their tests, with **zero edits to any existing reader or writer**. Everything the plan needs is already precedented in-tree and every locked decision (D-01..D-04) maps onto an existing idiom: the `begin_nested()` SAVEPOINT degrade pattern (seven verbatim occurrences in `pipeline.py`/`review.py`/`reenqueue.py`), the `~exists(...)` anti-join (`get_untracked_files`), the deterministic `"<function>:<file_id>"` ledger key (`deterministic_key._KEY_BUILDERS`), and the Phase 77 partial indexes (`ix_analysis_completed`, `ix_analysis_failed`, `ix_metadata_failed`, `ix_fprint_success`) that already exist and are shaped to exactly these predicates. **Zero new dependencies.**

Three findings materially shape the plan and are **not** obvious from CONTEXT.md. **(1)** SQLAlchemy has **no supported facility to evaluate a `ColumnElement[bool]` against a plain-Python row** — the private `sqlalchemy.orm.evaluator._EvaluatorCompiler` (which backs `synchronize_session="evaluate"`) does not reliably support `IS NOT NULL` / `= ANY(ARRAY[...])` and is internal API. "Authored exactly once" (DERIV-01/SC#2) therefore means the SQL builder is written once *and never duplicated across callers*; the Python resolver is a deliberate hand-written twin, and the **DERIV-04 parametrized equivalence test is the real lock** (exactly as D-04 states). **(2)** `execution_log` has **no `file_id`** — it references `proposal_id` only `[VERIFIED: models/execution.py:30]`. Any predicate touching `execution_log` (the downstream `apply` stage) must join through `proposals`; `done(apply)` cannot be a bare `EXISTS execution_log`. **(3)** `generate_proposals` is keyed by an **order-independent set-hash of `file_ids`**, not per file `[VERIFIED: deterministic_key.py:85]` — so `in_flight(propose, file)` is **not derivable from a per-file ledger key**. The three enrich stages (the ELIG-01 target) and `search_tracklist`/`push_file` are all file-keyed and derive cleanly; downstream `propose` in-flight is a genuine gap to flag (acceptable for additive Phase 78 since ELIG-02 defines downstream eligibility purely as upstream conjuncts).

**Primary recommendation:** Author each stage's `done`/`failed` predicate as a `ColumnElement[bool]` builder in `services/stage_status.py` (the single SQL source of truth) and its scalar twin in the `enums/stage.py` resolver; derive `in_flight` from a `scheduling_ledger` row-exists on key `f"{STAGE_TO_FUNCTION[stage]}:{file_id}"` (D-01 authoritative), with a SAVEPOINT-wrapped `saq_jobs` read that only enriches queued-vs-active detail (D-02). Use `EXISTS`/`~exists` (never `LEFT JOIN..IS NULL`, never `NOT IN`) for the fingerprint 1:N aggregation and every anti-join. Lock SQL⇔Python with a parametrized fixture matrix in the `integration` bucket; put the ELIG-03 terminal-failed-analyze regression and the DB-free resolver tests in `shared`.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01 (INFLIGHT-03 — the required written decision record):** `in_flight(file, stage)`'s authoritative source is the **`scheduling_ledger`** — a ledger row for the `(file, stage-function)` key means `in_flight`. `saq_jobs` is a **corroborating signal only**, not authoritative. Rationale: the ledger is written at the same single `before_enqueue` chokepoint that creates the `saq_jobs` row (so `ledger ⊇ saq_jobs` keys in the normal path), it is **durable** (survives a broker truncate/restore and outlives a crashed job's lost `saq_jobs` row), and it decouples the hot 5s `/pipeline/stats` poll from live-broker coupling. Satisfies the safety property: a crashed-mid-run / callback-lost file keeps its ledger row and reads `in_flight`, never falsely `not_started` (guards the 44.5K over-enqueue class). Chosen over the strict **ledger-alone** (loses the corroboration hook) and the **`saq_jobs ∪ scheduling_ledger` union** (makes the live broker load-bearing on the hot path, enlarges the false-positive-stuck set).
- **D-02 (INFLIGHT-02):** The **`in_flight` boolean = a ledger row exists** — full stop. This is also the **degrade-safe default**: the `saq_jobs` read is static SQL wrapped in a `begin_nested()` SAVEPOINT, used **only** to enrich observability / DAG busy pills with queued-vs-active detail; it **never flips the boolean**. On ANY `saq_jobs` error, drop the detail and keep `in_flight` from the ledger. `/pipeline/stats` never 500s on a broker read hiccup; Alembic never references `saq_jobs`.
- **D-03 (DERIV-03):** `done(metadata)` = a `metadata` row is present **AND `failed_at IS NULL`.** Honors the Phase 77 D-02 handoff (a metadata failure inserts a row with `failed_at` set → a failure-only row derives NOT-done → failed). Additive-safe today (Phase 77 skipped the metadata backfill, so every existing row has `failed_at = NULL`). Bare row-presence was rejected.
- **D-04 (DERIV-01/DERIV-04):** Two-module split, equivalence test as the real lock:
  - **`enums/stage.py`** (DB-free, agent-safe — no SQLAlchemy model imports): the `Stage` / `Status` enums, the **eligibility DAG topology**, and the **pure-Python per-row resolver** over plain scalars.
  - **`services/stage_status.py`**: the SQLAlchemy **`ColumnElement[bool]` builders** that compose into `.where(...)`.
  - **DERIV-04 parametrized equivalence test** asserts SQL-derived == Python-derived for every stage across the full fixture matrix. Author-once via shared comparison expressions where idioms coincide; the test is authoritative where they diverge (`IS NOT NULL`, `IN (...)`).

### Locked by ROADMAP success criteria (carried in as-is)
- Precedence **`in_flight ≻ done ≻ failed ≻ not_started`** (DERIV-02).
- Per-stage `done`: `fingerprint_results.status IN ('success','completed')` any engine (spell `= ANY (ARRAY[...])`); `analysis.analysis_completed_at IS NOT NULL` (not bare row existence); `tracklists`/`proposals`/`execution_log` presence for downstream (DERIV-03).
- **DERIV-05** multi-row aggregation: one `success` + one `failed` fingerprint engine derives `done`.
- **ELIG-01** the three enrich stages have **no upstream**; every `discovered` file is simultaneously eligible for all three, any order; `eligible = NOT done AND NOT in_flight`.
- **ELIG-02** downstream eligibility is pure over `stage_status`: tracklist = fingerprint-done & not-tracklisted; propose = metadata-done AND analyze-done; review = a proposal exists; apply = an approved proposal exists.
- **ELIG-03** a **failed analyze is terminal** — never auto-eligible / auto-re-enqueued (retry is manual-only); regression test asserts it is absent from the analyze pending/eligible set.
- **ELIG-04** a **failed fingerprint stays eligible** (auto-retry preserved, consistent with D-16).

### Claude's Discretion
- Exact fixture-matrix shape for the DERIV-04 equivalence test, the internal signature of the shared predicate builders, and the precise SAVEPOINT/degrade helper.

### Deferred Ideas (OUT OF SCOPE — later phases)
- Reader/writer cutover to derived status (DAG busy pills reading `in_flight`, pending-set queries using `eligible()`) — Phases 79 (shadow-compare) then 80+ (cutover). **Not Phase 78.**
- Tightening any metadata *writer* to set `failed_at` on failure — writer-side, Phase 81. Phase 78 only reads the column.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| **DERIV-01** | Single predicate module, one source of truth per stage's `done`/`failed` as reusable `ColumnElement[bool]` builders composing into SQL + a Python resolver; no stage predicate written twice. | §Architecture Pattern 1 (dual-form authoring) + §Pitfall 1 (no supported ColumnElement→Python eval; the test is the lock). SQL builders live once in `stage_status.py`; callers import them. |
| **DERIV-02** | Pure `stage_status(file, stage) -> {not_started\|in_flight\|done\|failed}`, precedence `in_flight ≻ done ≻ failed ≻ not_started`. | §Pattern 2 (resolver precedence ladder). Both SQL and Python evaluate the same 4-way ladder. |
| **DERIV-03** | Correct per-stage `done`: metadata row present & not failure-only; any fingerprint engine `success`/`completed`; `analysis.analysis_completed_at IS NOT NULL`; downstream `tracklists`/`proposals`/`execution_log` presence. | §Per-Stage Predicate Table (verified column/table names). `execution_log` join-through-`proposals` caveat flagged. |
| **DERIV-04** | Parametrized equivalence test proves SQL-derived == Python-derived for every stage across a fixture matrix. | §Validation Architecture (fixture matrix shape, integration bucket, real-PG harness). |
| **DERIV-05** | Multi-row aggregation: one `success` + one `failed` fingerprint engine derives `done`. | §Pattern 3 (`EXISTS(success)` beats bare row-existence; the 1:N aggregation). Fixture row in the matrix. |
| **ELIG-01** | Three enrich stages `eligible iff NOT done AND NOT in_flight`, each independent; every `discovered` file eligible for all three, any order. | §Eligibility Predicate Table. File-keyed ledger derives `in_flight` cleanly for all three. |
| **ELIG-02** | Downstream eligibility pure over `stage_status`: tracklist / propose / review / apply conjuncts. | §Eligibility Predicate Table (verified against `get_untracked_files`, `get_proposal_pending_batches`, proposals/execution_log). |
| **ELIG-03** | Failed analyze terminal — not auto-eligible, never auto-re-enqueued; regression test asserts absence from analyze pending/eligible set. | §Pattern 4 + §Validation Architecture. Mirrors the existing `_select_done_analyze_ids` treatment of `ANALYSIS_FAILED` as analyze-DONE. |
| **ELIG-04** | Failed fingerprint stays eligible (auto-retry). | §Eligibility Predicate Table. `done(fp)=EXISTS(success)`; a failed-only file is NOT done and (no in-flight) stays eligible. |
| **INFLIGHT-01** | `in_flight(file, stage)` true when active/queued work exists for `(file, stage-function)`; first-class input to eligibility + busy pills. | §Pattern 5 (ledger key format) + §Open Questions (batch-keyed `propose` gap). |
| **INFLIGHT-02** | Every `saq_jobs` read is static SQL in a `begin_nested()` SAVEPOINT, degrades to a safe default; Alembic never references `saq_jobs`. | §Pattern 6 (verbatim SAVEPOINT idiom, 7 in-tree occurrences). |
| **INFLIGHT-03** | Written D-01 decision record. | Recorded verbatim in §User Constraints D-01 above; the plan must carry it into a decision-record artifact. |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| DB-free per-row status resolver + eligibility DAG topology | Agent (compute/file-server, Postgres-free) | — | `enums/stage.py` must import with NO SQLAlchemy/`phaze.models`/`phaze.database` (Phase 26 D-03 boundary, enforced by `tests/test_task_split.py`). |
| SQL `ColumnElement[bool]` predicate builders | API / Backend (control-side) | Database | `stage_status.py` composes into `.where()` for set-based SELECTs the control plane runs. |
| `in_flight` ledger derivation | API / Backend | Database | Reads `scheduling_ledger` (control-only table); the ledger row-exists is the authoritative boolean (D-01). |
| SAVEPOINT-wrapped `saq_jobs` detail read | API / Backend | Database | Corroborating queued-vs-active enrichment only (D-02); never on the agent path. |
| Anti-drift equivalence test | Database (real-PG integration) | — | Needs a live Postgres to run the SQL builders and compare to the Python resolver over the same seeded rows. |

## Standard Stack

No new packages. Milestone hard constraint is **zero new dependencies** `[VERIFIED: REQUIREMENTS.md Out of Scope]`. Everything uses the installed stack:

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| SQLAlchemy | 2.0.51+ (installed) | `ColumnElement[bool]` builders, `exists()`/`~exists()`, `select().where()` | `pyproject.toml` pins `sqlalchemy>=2.0.51` `[VERIFIED]`. `exists()` and `~exists()` are the in-tree anti-join idiom (`pipeline.py:1388`). |
| Python `enum.StrEnum` | 3.14 stdlib | `Stage` / `Status` enums (DB-free) | Exact precedent: `enums/execution.py::ExecutionStatus`, `FileState`, `CloudJobStatus` are all `enum.StrEnum` `[VERIFIED]`. |
| asyncpg | 0.30.x (installed) | Async driver for the equivalence test | Tests use `create_async_engine` (`tests/integration/conftest.py:38`) `[VERIFIED]`. |
| pytest / pytest-asyncio | installed | Parametrized equivalence test + resolver unit tests | `@pytest.mark.parametrize` over the fixture matrix; real-PG via `just integration-test`. |

**Installation:** none. `uv sync` already provides all of the above. Run everything via `uv run …` (CLAUDE.md: `uv` only, never bare `pytest`/`python`/`mypy`).

## Package Legitimacy Audit

**Not applicable — this phase installs no external packages.** Milestone hard constraint: existing PostgreSQL + SQLAlchemy 2.x stack suffices `[VERIFIED: REQUIREMENTS.md Out of Scope]`. No `slopcheck` / registry verification required.

## Architecture Patterns

### System Data Flow (derivation layer)

```
                         ┌─────────────────────────────────────────────┐
                         │  enums/stage.py  (DB-FREE, agent-safe)       │
   plain scalars ───────▶│  Stage/Status enums                         │
   (a dict per file,     │  ELIGIBILITY_DAG topology (upstream map)     │──▶ status: Status
    no SQLAlchemy)       │  resolve_status(scalars) precedence ladder  │──▶ eligible: bool
                         │  eligible(status_map, stage)                 │
                         └─────────────────────────────────────────────┘
                                          ▲  (SAME predicate semantics, locked by test)
                                          │
   ┌──────────────────────────────────────────────────────────────────┐
   │  services/stage_status.py  (SQLAlchemy, control-side)             │
   │  done_clause(stage)   -> ColumnElement[bool]  (EXISTS / IS NOT …) │
   │  failed_clause(stage) -> ColumnElement[bool]                      │
   │  inflight_clause(stage)-> ColumnElement[bool] (ledger row-exists) │
   │  stage_status_case(stage) -> CASE ladder (in_flight≻done≻failed)  │
   └──────────────────────────────────────────────────────────────────┘
        │ composes into .where()                     │ SAVEPOINT-only detail
        ▼                                             ▼
   output tables (read-only):                   saq_jobs (corroborating,
     metadata, analysis, fingerprint_results,     begin_nested() wrapped,
     tracklists, proposals, execution_log,         queued-vs-active pill detail
     dedup_resolution   +  scheduling_ledger        ONLY — never flips boolean)
        │
        ▼
   DERIV-04 equivalence test (real PG): seed rows → run SQL builder AND Python
   resolver over the SAME scalars → assert equal for every (stage × fixture).

   NOTHING in Phase 78 wires these into an existing reader/writer (additive-only).
```

### Recommended Project Structure

```
src/phaze/enums/
├── __init__.py           # (unchanged)
├── execution.py          # (unchanged — ExecutionStatus precedent)
└── stage.py              # NEW: Stage/Status enums, ELIGIBILITY_DAG, resolve_status(), eligible()

src/phaze/services/
└── stage_status.py       # NEW: done_clause/failed_clause/inflight_clause/stage_status_case builders

tests/shared/
├── test_stage_resolver.py        # NEW: DB-free resolver + precedence + ELIG-03 terminal-failed (no PG)
└── test_stage_eligibility_dag.py # NEW: topology / eligible() conjuncts (no PG)

tests/integration/
└── test_stage_status_equivalence.py  # NEW: DERIV-04 SQL⇔Python matrix + INFLIGHT-02 SAVEPOINT-degrade (real PG)
```

### Pattern 1: Dual-form predicate authoring (the DERIV-01 "author once" reading)

**What:** Each stage has ONE `done`/`failed`/`in_flight` *specification*. The SQL side is a `ColumnElement[bool]` builder in `stage_status.py`, imported by every caller (so it is never re-spelled across `pipeline.py`, `reenqueue.py`, etc. in later cutover phases). The Python side is a hand-written twin in the DB-free resolver.

**Why not literally one function:** SQLAlchemy exposes **no supported API to evaluate a `ColumnElement[bool]` against a plain-Python row** (see Pitfall 1). The idioms genuinely diverge — `AnalysisResult.analysis_completed_at.isnot(None)` vs Python `completed_at is not None`; `status = ANY(ARRAY[...])` vs `any(s in _DONE_FP for s in statuses)`. D-04 anticipates exactly this: "the test is authoritative where they diverge (`IS NOT NULL`, `IN (...)`)."

**Recommended SQL builder shape** (correlated `EXISTS`, so it composes into any `select(FileRecord).where(...)`):
```python
# services/stage_status.py  — Source idiom: pipeline.py:1388 (~exists), :1420 (exists+isnot)  [VERIFIED]
from sqlalchemy import ColumnElement, exists, select
from phaze.models.analysis import AnalysisResult

def analyze_done_clause() -> ColumnElement[bool]:
    return exists(
        select(AnalysisResult.id).where(
            AnalysisResult.file_id == FileRecord.id,
            AnalysisResult.analysis_completed_at.isnot(None),   # NOT bare row existence (DERIV-03)
        )
    )
```

**Recommended Python resolver shape** (scalars only — a compute agent passes a dict it already has):
```python
# enums/stage.py  — DB-FREE. No phaze.models import (test_task_split boundary).
def _analyze_status(*, completed_at, failed_at, inflight: bool) -> Status:
    if inflight:            return Status.IN_FLIGHT   # precedence ladder (DERIV-02)
    if completed_at is not None: return Status.DONE
    if failed_at is not None:    return Status.FAILED
    return Status.NOT_STARTED
```

### Pattern 2: The precedence ladder (`in_flight ≻ done ≻ failed ≻ not_started`)

Both forms evaluate the SAME 4-way ladder. SQL is a `case()`:
```python
# services/stage_status.py
from sqlalchemy import case
def analyze_status_case() -> ColumnElement[str]:
    return case(
        (inflight_clause("analyze"), "in_flight"),
        (analyze_done_clause(),      "done"),
        (analyze_failed_clause(),    "failed"),
        else_="not_started",
    )
```
The equivalence test compares this label to `resolve_status(...)`'s `.value`. Precedence is load-bearing: a file that failed then was manually re-enqueued has BOTH a `failed_at` and a live ledger row → must read `in_flight`, not `failed`.

### Pattern 3: Fingerprint 1:N `done` via `EXISTS(success)` (DERIV-05)

`fingerprint_results` is **1:N** — unique on `(file_id, engine)`, not on `file_id` `[VERIFIED: fingerprint.py:26]`. A file has one row per engine (e.g. audfprint + panako). `done(fingerprint)` = **any** engine succeeded:
```python
def fingerprint_done_clause() -> ColumnElement[bool]:
    return exists(
        select(FingerprintResult.id).where(
            FingerprintResult.file_id == FileRecord.id,
            FingerprintResult.status.in_(("success", "completed")),   # SA renders = ANY(ARRAY[...])
        )
    )
```
Python twin over the list of engine statuses:
```python
_DONE_FP = frozenset({"success", "completed"})
def _fp_status(*, engine_statuses: list[str], inflight: bool) -> Status:
    if inflight:                                     return Status.IN_FLIGHT
    if any(s in _DONE_FP for s in engine_statuses):  return Status.DONE     # one success wins (DERIV-05)
    if any(s == "failed" for s in engine_statuses):  return Status.FAILED
    return Status.NOT_STARTED
```
The DERIV-05 fixture (`[success, failed]` → `done`) is the exact aggregation guard. This reuses the Phase-59 WR-02 `status IN ('success','completed')` spelling (PR #189) `[VERIFIED: pipeline.py get_fingerprint_pending_files uses status=="failed"; ix_fprint_success uses = ANY(ARRAY['success','completed'])]`.

### Pattern 4: Terminal failed-analyze at the shared predicate (ELIG-03)

`eligible(analyze)` = `NOT done AND NOT in_flight` **AND NOT failed** — a failed analyze must be structurally absent from the eligible/pending set. This mirrors the existing recovery precedent verbatim: `_select_done_analyze_ids` treats `ANALYSIS_FAILED` as analyze-**DONE** so `recover_orphaned_work` never auto-loops an un-analyzable file; the operator-gated `POST /pipeline/analysis-failed/retry` flips the file OUT of the failed state before re-enqueue `[VERIFIED: reenqueue.py:177-187]`. In the derived model the equivalent is: `eligible(f, "analyze") = analyze_status(f) == NOT_STARTED` (since `done`/`failed`/`in_flight` all exclude it). ELIG-04's fingerprint contrast: `done(fp)=EXISTS(success)`, so a failed-only fingerprint file is NOT done → with no in-flight it stays eligible (auto-retry preserved).

### Pattern 5: `in_flight` from the ledger key (D-01, INFLIGHT-01)

The scheduling-ledger PK is the deterministic `"<function>:<natural_id>"` key `[VERIFIED: scheduling_ledger.py:59, deterministic_key.py:116]`. For the file-keyed stages the natural id IS the file id:

| Stage | `STAGE_TO_FUNCTION` | ledger key for file `F` |
|-------|---------------------|-------------------------|
| metadata | `extract_file_metadata` | `f"extract_file_metadata:{F}"` |
| analyze | `process_file` | `f"process_file:{F}"` |
| fingerprint | `fingerprint_file` | `f"fingerprint_file:{F}"` |
| tracklist (search) | `search_tracklist` | `f"search_tracklist:{F}"` |
| push | `push_file` | `f"push_file:{F}"` |

`[VERIFIED: stage_control.py:51 STAGE_TO_FUNCTION; deterministic_key.py:77-95 _KEY_BUILDERS]`

SQL `in_flight` clause = ledger row-exists on the computed key:
```python
from phaze.models.scheduling_ledger import SchedulingLedger
def inflight_clause(function: str) -> ColumnElement[bool]:
    # key column is the PK "<function>:<file_id>"; concat with the FK'd file id.
    return exists(
        select(SchedulingLedger.key).where(
            SchedulingLedger.key == func.concat(function + ":", cast(FileRecord.id, String))
        )
    )
```
Python twin: `inflight = ledger_key in live_ledger_key_set` (the agent passes the set it was given, or `False` — the degrade-safe default). **D-02: the ledger row-exists IS the boolean.** The `saq_jobs` read (Pattern 6) never enters this decision.

> **⚠ Batch-keyed exception (see Open Questions):** `generate_proposals` is keyed `f"generate_proposals:{sha256(sorted file_ids)}"` — a SET hash, NOT per file `[VERIFIED: deterministic_key.py:85, _hash_ids]`. `in_flight(propose, file)` is therefore **not** derivable from a per-file ledger key. The three enrich stages (ELIG-01's target) and `search_tracklist`/`push` are all file-keyed and unaffected.

### Pattern 6: SAVEPOINT-wrapped corroborating `saq_jobs` read (D-02, INFLIGHT-02)

The **verbatim** in-tree degrade idiom (7 occurrences: `pipeline.py` `get_stage_busy_counts:489`, `get_live_job_keys:524`, `count_inflight_jobs:1464`; `reenqueue.py` `backfill_ledger_from_saq_jobs:500`; `review.py:61,104,173,220`) `[VERIFIED]`:
```python
# Static SQL — only literals are the status allowlist (no interpolated operator input, T-45 discipline).
_SAQ_DETAIL_SQL = text("SELECT key, status FROM saq_jobs WHERE status IN ('queued', 'active')")

async def saq_detail(session: AsyncSession) -> dict[str, str]:
    """Queued-vs-active detail ONLY — enriches busy pills; NEVER flips in_flight (D-02)."""
    try:
        async with session.begin_nested():           # SAVEPOINT — rolls back ALONE on error
            rows = (await session.execute(_SAQ_DETAIL_SQL)).all()
    except Exception:
        logger.warning("saq_detail_degraded", exc_info=True)
        return {}                                     # degrade → keep in_flight from the ledger
    return {row[0]: row[1] for row in rows}
```
Why `begin_nested()` not a plain `rollback()`: a plain rollback expires the caller's already-loaded ORM objects and 500s the page on the next lazy load; the SAVEPOINT recovers the aborted Postgres transaction WITHOUT expiring them `[VERIFIED: pipeline.py:479-485 docstring]`. `saq_jobs` has **no `function` column** — the deterministic key prefix is the only way to bucket by stage (`split_part(key, ':', 1)`) `[VERIFIED: pipeline.py:453-458]`.

### Anti-Patterns to Avoid
- **`LEFT JOIN fingerprint_results ... WHERE fr.id IS NULL`** for "not fingerprinted" — under-counts. A file with a `failed`-only row has a matching row, so the join is non-NULL and the file is wrongly excluded from eligible. Use `~exists(success-row)`.
- **`FileRecord.id.not_in(select(...))`** anti-join at corpus scale — the milestone notes a `>170s` cliff at ~200K and 3-valued-logic NULL hazards. Use `~exists(...)`.
- **Evaluating a `ColumnElement` in Python** via `sqlalchemy.orm.evaluator` — private API, incomplete `IS NULL`/`ANY` support (Pitfall 1). Hand-write the twin; the test locks them.
- **Reading `saq_jobs` to decide the `in_flight` boolean** — violates D-01/D-02. The ledger is authoritative; `saq_jobs` is detail-only.
- **`import phaze.models` in `enums/stage.py`** — breaks the agent Postgres-free boundary (`test_task_split.py`). The resolver takes plain scalars.
- **Wiring any builder into an existing reader/writer this phase** — additive-only; that is Phases 79-90.
- **A bare `EXISTS execution_log` for `done(apply)`** — `execution_log` has no `file_id`; must join through `proposals`.

## Per-Stage `done` / `failed` Predicate Table (DERIV-03, verified column names)

| Stage | Table(s) | `done` predicate | `failed` predicate | Notes |
|-------|----------|------------------|--------------------|-------|
| **metadata** | `metadata` (1:1, unique `file_id`) | `EXISTS row WHERE failed_at IS NULL` (D-03) | `EXISTS row WHERE failed_at IS NOT NULL` | `failed_at`/`error_message` are Phase-77 nullable cols `[VERIFIED: metadata.py:33]`. Today all `failed_at=NULL` (no backfill) → `done` == row-exists, unchanged. |
| **analyze** | `analysis` (1:1, unique `file_id`) | `EXISTS row WHERE analysis_completed_at IS NOT NULL` (NOT bare existence) | `EXISTS row WHERE failed_at IS NOT NULL` | Partial row upserted at analysis START has `completed_at=NULL` `[VERIFIED: analysis.py:34-44]`. `ix_analysis_completed`/`ix_analysis_failed` support both. |
| **fingerprint** | `fingerprint_results` (1:N, unique `file_id`+`engine`) | `EXISTS row WHERE status IN ('success','completed')` (any engine, DERIV-05) | `NOT EXISTS(success) AND EXISTS(status='failed')` | `ix_fprint_success` (`= ANY(ARRAY['success','completed'])`) supports the EXISTS + its anti-join `[VERIFIED: fingerprint.py:30]`. |
| **tracklist** | `tracklists` (`file_id` nullable FK) | `EXISTS Tracklist WHERE file_id = F` | (no failure marker — stays eligible) | `ix_tracklists_file_id` supports it `[VERIFIED: tracklist.py:46]`. `get_untracked_files` uses `~exists` here `[VERIFIED: pipeline.py:1390]`. |
| **propose** | `proposals` (`file_id` FK, `status`) | `EXISTS RenameProposal WHERE file_id = F` | `EXISTS proposal WHERE status='failed'` | `ProposalStatus`: pending/approved/rejected/executed/failed `[VERIFIED: proposal.py:30-34]`. |
| **review** | `proposals` | `EXISTS proposal` (ELIG-02: "a proposal exists") | — | Same table; "review-eligible" = a proposal awaits a decision. |
| **apply** | `execution_log` **JOIN** `proposals` | `EXISTS execution_log e JOIN proposals p ON e.proposal_id=p.id WHERE p.file_id=F AND e.status='completed'` | `... AND e.status='failed'` | **`execution_log` has NO `file_id`** — join through `proposals` `[VERIFIED: execution.py:30]`. `ExecutionStatus`: pending/in_progress/completed/failed `[VERIFIED: enums/execution.py]`. This is the Phase-85 `applied(f)` shape, defined (not wired) here. |

## Eligibility Predicate Table (ELIG-01..04, verified against existing pending sets)

| Stage | `eligible(f)` | Current pending-set analogue (for parity) |
|-------|---------------|--------------------------------------------|
| metadata | `NOT done(metadata) AND NOT in_flight(metadata)` (no upstream) | `get_metadata_pending_files` = all music/video files `[VERIFIED: pipeline.py:1339]` |
| fingerprint | `NOT done(fp) AND NOT in_flight(fp)` (no upstream; failed-only stays eligible, ELIG-04) | `get_fingerprint_pending_files` = METADATA_EXTRACTED ∪ failed-fp-retry `[VERIFIED: pipeline.py:1359-1367]` |
| analyze | `analyze_status == not_started` (failed is terminal, ELIG-03) | `_select_done_analyze_ids` treats ANALYSIS_FAILED as done `[VERIFIED: reenqueue.py:187]` |
| tracklist | `done(fingerprint) AND NOT exists(tracklist for f)` | `get_untracked_files` `~exists(Tracklist)` `[VERIFIED: pipeline.py:1390]` |
| propose | `done(metadata) AND done(analyze)` (AND not already proposed) | `get_proposal_pending_batches`: state∈{ANALYZED,METADATA_EXTRACTED} AND `exists(FileMetadata)` AND `exists(AnalysisResult WHERE analysis_completed_at IS NOT NULL)` `[VERIFIED: pipeline.py:1410-1428]` |
| review | `exists(proposal for f)` | proposals table |
| apply | `exists(approved proposal for f)` (ELIG-02) | `proposals.status = 'approved'` |

**The ELIG-01 independence claim is directly supported:** `get_metadata_pending_files` (all music/video, no state gate) and the derived `NOT done AND NOT in_flight` per stage have no cross-stage term, so a `discovered` file is simultaneously eligible for metadata, fingerprint, and analyze in any order — the milestone thesis.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Degrade-safe `saq_jobs` read | New try/rollback wrapper | `async with session.begin_nested()` + return safe default | 7 verbatim in-tree occurrences; a plain rollback expires ORM objects and 500s the poll (Pitfall 2). |
| Anti-join "not done for stage" | `LEFT JOIN..IS NULL` / `NOT IN` | `~exists(select(...).where(...))` | `pipeline.py:1388` precedent; correct for 1:N + no 170s cliff (Pitfall 3). |
| Ledger key construction | Ad-hoc f-string in the new module | Reuse `STAGE_TO_FUNCTION` + the `"<function>:<file_id>"` format from `_KEY_BUILDERS` | Single source of truth; a re-spelled key silently mismatches the real ledger PK. |
| ColumnElement→Python evaluation | `sqlalchemy.orm.evaluator._EvaluatorCompiler` | Hand-written resolver + DERIV-04 equivalence test | Private API, incomplete `IS NULL`/`ANY` (Pitfall 1); the test is the intended lock (D-04). |
| Real-PG test harness | New engine/session spin-up | `tests/integration/conftest.py` `create_async_engine` + `just integration-test` (`:5433`) | Established fixture + connectivity-probe skip `[VERIFIED]`. |

**Key insight:** every predicate, key, index, and degrade idiom this phase needs already exists in-tree — Phase 78 *relocates and unifies* them behind two modules and proves the relocation is faithful with the equivalence test. Any bespoke SQL or a second key format is a smell.

## Runtime State Inventory

Phase 78 writes **no** data and registers **no** state — it adds two pure modules + tests that READ existing tables. The migration-era state was all landed in Phase 77.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None written. Reads `metadata`/`analysis`/`fingerprint_results`/`tracklists`/`proposals`/`execution_log`/`dedup_resolution`/`scheduling_ledger`/`saq_jobs` read-only. | None — additive, no writes. |
| Live service config | None — no external service embeds this module. | None. |
| OS-registered state | None. | None. |
| Secrets/env vars | Equivalence test reuses `TEST_DATABASE_URL`/`PHAZE_QUEUE_URL` (integration harness `:5433`) `[VERIFIED: conftest.py:53]`. No new secret. | None. |
| Build artifacts | None — no package rename, no new dependency. | None. |

**The canonical rename-phase question does not apply** (this is not a rename). The one runtime coupling to note: the equivalence test needs the live `saq_jobs` table to exist for the SAVEPOINT-degrade test — the integration harness auto-creates it on `queue.connect()` `[VERIFIED: conftest.py:9]`.

## Common Pitfalls

### Pitfall 1: expecting to author the predicate ONCE and evaluate it both ways
**What goes wrong:** A plan that says "write the `ColumnElement` once, evaluate it in Python for the resolver" will look for a `.evaluate(row)` that does not exist. `sqlalchemy.orm.evaluator._EvaluatorCompiler` is private, drives only `synchronize_session="evaluate"`, and raises `UnevaluatableError` on constructs it can't handle (correlated `EXISTS`, `ANY(ARRAY)`, some `IS NULL` paths).
**Why it happens:** DERIV-01's "authored exactly once as a reusable `ColumnElement[bool]` builder" reads like one function for both worlds; it actually means the SQL builder is written once and *not duplicated across callers* (the later-phase cutover imports it), while the Python resolver is a deliberate twin.
**How to avoid:** Two parallel implementations; the DERIV-04 parametrized equivalence test over a real PG is the authoritative lock (exactly D-04). Keep the two files tiny and side-by-side so a reviewer sees both halves of each predicate.
**Warning signs:** any import of `sqlalchemy.orm.evaluator`; a resolver that constructs SQLAlchemy objects.

### Pitfall 2: a plain `session.rollback()` on the degrade path 500s the poll
**What goes wrong:** Wrapping the `saq_jobs` read in `try/except` + `await session.rollback()` recovers the aborted transaction but **expires every already-loaded ORM object** on the session; the next lazy attribute access on the dashboard's `agents`/`recent_scans` raises and the 5s `/pipeline/stats` poll 500s.
**Why it happens:** SQLAlchemy expires all instances on `rollback()`; `begin_nested()` (SAVEPOINT) rolls back only the nested scope.
**How to avoid:** Use `async with session.begin_nested():` verbatim (Pattern 6). This is why all 7 in-tree occurrences use it `[VERIFIED: pipeline.py:479-485]`.
**Warning signs:** a degrade test that passes in isolation but the page 500s under a real broker hiccup with other loaded objects.

### Pitfall 3: `NOT IN (subquery)` / `LEFT JOIN..IS NULL` for the eligibility anti-joins
**What goes wrong:** At ~200K files, `FileRecord.id.not_in(select(fp.file_id))` hits a `>170s` planner cliff and mis-handles NULLs (3-valued logic drops rows if the subquery yields any NULL). `LEFT JOIN fingerprint_results IS NULL` under-counts the 1:N table (a failed-only file has a matching row).
**Why it happens:** the fingerprint table is 1:N and large; the correct question is "does a *success* row exist," an EXISTS probe, not a row-absence join.
**How to avoid:** `~exists(select(FingerprintResult.id).where(file_id==F, status.in_((...))))`. The Phase-77 `ix_fprint_success` partial index makes both the positive EXISTS and its negation index-only `[VERIFIED: fingerprint.py:30]`.
**Warning signs:** eligible/pending counts that drift when a file has both a failed and a success fingerprint row.

### Pitfall 4: `done(apply)` written as a bare `execution_log` existence
**What goes wrong:** `exists(select(ExecutionLog.id).where(ExecutionLog.file_id == F))` — there is no `ExecutionLog.file_id`; it fails to compile (or, worse, a plan invents a phantom column).
**Why it happens:** `execution_log` keys on `proposal_id`, not `file_id` `[VERIFIED: execution.py:30]`.
**How to avoid:** join through `proposals`: `exists(select(ExecutionLog.id).join(RenameProposal, ExecutionLog.proposal_id==RenameProposal.id).where(RenameProposal.file_id==F, ExecutionLog.status=='completed'))`. This is the Phase-85 `applied(f)` shape — define it now, wire it later.
**Warning signs:** an `AttributeError: ExecutionLog.file_id` at import/compile.

### Pitfall 5: `in_flight(propose)` derived from a per-file ledger key
**What goes wrong:** Computing `f"generate_proposals:{file_id}"` and probing the ledger never matches — the real key is `f"generate_proposals:{sha256(sorted file_ids)}"` `[VERIFIED: deterministic_key.py:85]`.
**How to avoid:** Scope `in_flight` derivation to the file-keyed functions (the three enrich + `search_tracklist` + `push_file`). For `propose`, either omit `in_flight` from `eligible()` (ELIG-02 defines it purely as `done(metadata) AND done(analyze)`) or flag it as a documented Phase-78 limitation (see Open Questions). Additive Phase 78 does not need to solve it.
**Warning signs:** a propose in-flight test that can never go green.

## Code Examples

### DB-free resolver skeleton (enums/stage.py)
```python
# Source pattern: enums/execution.py (StrEnum, DB-free)  [VERIFIED]
from __future__ import annotations
import enum

class Stage(enum.StrEnum):
    METADATA = "metadata"; ANALYZE = "analyze"; FINGERPRINT = "fingerprint"
    TRACKLIST = "tracklist"; PROPOSE = "propose"; REVIEW = "review"; APPLY = "apply"

class Status(enum.StrEnum):
    NOT_STARTED = "not_started"; IN_FLIGHT = "in_flight"; DONE = "done"; FAILED = "failed"

# Upstream topology (ELIG-01: enrich stages map to ()) — the eligibility DAG.
ELIGIBILITY_DAG: dict[Stage, tuple[Stage, ...]] = {
    Stage.METADATA: (), Stage.ANALYZE: (), Stage.FINGERPRINT: (),
    Stage.TRACKLIST: (Stage.FINGERPRINT,),
    Stage.PROPOSE: (Stage.METADATA, Stage.ANALYZE),
    Stage.REVIEW: (Stage.PROPOSE,), Stage.APPLY: (Stage.REVIEW,),
}
```

### Parametrized equivalence test skeleton (DERIV-04)
```python
# tests/integration/test_stage_status_equivalence.py  — real PG, integration bucket
# Source harness: tests/integration/conftest.py (create_async_engine, connectivity-probe skip)  [VERIFIED]
import pytest
pytestmark = pytest.mark.integration

# (stage, seed_fn, expected_status) — one row per cell of the matrix.
CASES = [
    ("analyze", seed_analysis_completed,       "done"),
    ("analyze", seed_analysis_partial,         "not_started"),   # completed_at NULL
    ("analyze", seed_analysis_failed,          "failed"),
    ("analyze", seed_analysis_failed_inflight, "in_flight"),     # precedence: ledger row wins
    ("fingerprint", seed_fp_success_and_failed,"done"),          # DERIV-05 aggregation
    ("fingerprint", seed_fp_failed_only,       "failed"),
    ("metadata", seed_metadata_failed_only,    "failed"),        # D-03: failure-only ≠ done
    # ... every stage × {not_started, in_flight, done, failed}
]

@pytest.mark.parametrize("stage,seed_fn,expected", CASES)
async def test_sql_equals_python(db_session, stage, seed_fn, expected):
    file_id = await seed_fn(db_session)                          # writes output rows + optional ledger row
    # SQL side: run the ColumnElement CASE ladder in a SELECT
    sql_status = await eval_sql_status(db_session, stage, file_id)
    # Python side: read the SAME scalars, feed the DB-free resolver
    scalars = await load_scalars(db_session, stage, file_id)
    py_status = resolve_status(stage, scalars)
    assert sql_status == py_status == expected                   # the drift-lock
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Linear `files.state` scalar as pipeline authority (`get_pipeline_stats` `GROUP BY state`) | Per-stage derived status from output tables + ledger | Phase 78 (module) / 82 (readers cut over) | Phase 78 builds + proves the module; `get_pipeline_stats` still `GROUP BY state` `[VERIFIED: pipeline.py:64]` — untouched until Phase 82. |
| `in_flight` inferred from live `saq_jobs` alone | `scheduling_ledger` authoritative, `saq_jobs` corroborating (D-01) | Phase 45 (ledger) → Phase 78 (derivation) | Durable across broker loss; hot poll decoupled from the broker. |
| Fingerprint "done" scattered across callers | One `EXISTS(status IN ('success','completed'))` builder | Phase 59 WR-02 (PR #189) → Phase 78 (unified) | Single spelling, index-backed. |

**Deprecated/outdated:** nothing removed in Phase 78 (additive-only). `files.state`, `FileState`, and the linear readers are retired in Phases 80-90.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `sqlalchemy.orm.evaluator` cannot faithfully evaluate the `EXISTS`/`ANY`/`IS NULL` predicates this phase needs, so two parallel implementations are required. | Pitfall 1 | If a future SA version exposed a supported evaluator, a single-source authoring could replace the twin. Low risk — the equivalence test remains correct either way; it just becomes belt-and-suspenders. |
| A2 | For additive Phase 78, `eligible(propose)` need not include an `in_flight` term (ELIG-02 defines it as `done(metadata) AND done(analyze)`), so the batch-keyed-ledger gap (Pitfall 5) is deferrable. | Pattern 5 / Open Q1 | If the planner wants propose in-flight now, a non-per-file mechanism (probe `saq_jobs` for any `generate_proposals` key containing the file — expensive) is needed. Recommend deferring to the cutover phase. Flag at discuss/plan time. |
| A3 | The `done(review)`/`done(apply)` semantics (review = proposal exists; apply = approved proposal / completed execution_log) match ELIG-02's wording and the Phase-85/86 intent. | Per-Stage table | If review "done" should mean "a decision was made" (approved OR rejected) rather than "a proposal exists," the review predicate shifts. ELIG-02 says "a proposal exists"; carried as-is. Confirm with planner. |
| A4 | `MUSIC_VIDEO_TYPES` file-type gating (the metadata/tracklist pending sets) is the correct population filter to mirror in the derived eligibility, i.e. non-music files are simply never eligible for enrich stages. | Eligibility table | If a companion/non-media file should surface, the filter differs. Matches `get_metadata_pending_files` exactly `[VERIFIED]`. Low risk. |

## Open Questions

1. **`in_flight(propose)` from a batch set-hash key.**
   - What we know: enrich stages + `search_tracklist` + `push` are file-keyed and derive cleanly; `generate_proposals` is keyed on `sha256(sorted file_ids)` `[VERIFIED]`.
   - What's unclear: whether Phase 78 must expose a per-file `in_flight(propose)` at all.
   - Recommendation: **No.** ELIG-02 defines propose eligibility as `done(metadata) AND done(analyze)`; scope `in_flight` derivation to file-keyed stages and document the limitation. The reader-cutover phase can decide whether propose needs an in-flight guard (likely via `saq_jobs` batch-membership at that point).

2. **`done(review)` — "proposal exists" vs "decision made".**
   - What we know: ELIG-02 says review-eligible = "a proposal exists"; a separate "done(review)" is not explicitly required by DERIV-03 (review is not an output-table stage the way metadata/analyze are).
   - Recommendation: model `review` eligibility as `exists(proposal)` and treat `apply` as the terminal output stage (`execution_log` through `proposals`). Confirm the exact review/apply status semantics with the planner; they harden in Phases 85/86.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (ephemeral) | DERIV-04 equivalence test + INFLIGHT-02 SAVEPOINT test | ✓ via `just test-db` / `just integration-test` | 16+ container (`:5433`) | Connectivity-probe `pytest.skip` `[VERIFIED: conftest.py:117]` |
| `saq_jobs` table | INFLIGHT-02 SAVEPOINT-degrade test | ✓ auto-created on `queue.connect()` | — | drop-table sub-case exercises the degrade path |
| sqlalchemy / asyncpg / pytest-asyncio | modules + tests | ✓ (`uv sync`) | 2.0.51 / installed | — |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** none — all in the existing stack.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) + `tests/buckets.json` per-bucket isolation |
| Quick run command | `uv run pytest tests/shared/test_stage_resolver.py -x` (DB-free, fast) |
| Full suite command | `just integration-test` (ephemeral PG `:5433` for the equivalence + SAVEPOINT tests) |
| Buckets | **`shared`** for the DB-free resolver/DAG/ELIG-03 tests; **`integration`** for the real-PG DERIV-04 equivalence + INFLIGHT-02 degrade test. One bucket per file, enforced by `tests/shared/test_partition_guard.py` `[VERIFIED]`. |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DERIV-02 | Resolver returns correct status; precedence `in_flight≻done≻failed≻not_started` for every stage | unit (DB-free) | `uv run pytest tests/shared/test_stage_resolver.py -x` | ❌ Wave 0 |
| DERIV-03 | Each `done` predicate uses the correct column (metadata not-failure-only; analyze `completed_at IS NOT NULL`; fp any-engine success) | unit + integration | `uv run pytest tests/shared/test_stage_resolver.py tests/integration/test_stage_status_equivalence.py -x` | ❌ Wave 0 |
| DERIV-04 | **SQL-derived == Python-derived for every (stage × fixture)** | integration (real PG) | `uv run pytest tests/integration/test_stage_status_equivalence.py -x` | ❌ Wave 0 |
| DERIV-05 | `[success, failed]` fingerprint file derives `done` (both forms) | integration | (same file, one matrix cell) | ❌ Wave 0 |
| ELIG-01 | Every enrich stage independent; a `discovered` file eligible for all three | unit (DB-free) | `uv run pytest tests/shared/test_stage_eligibility_dag.py -x` | ❌ Wave 0 |
| ELIG-02 | Downstream conjuncts (tracklist/propose/review/apply) | unit + integration | (both files) | ❌ Wave 0 |
| ELIG-03 | **Failed analyze absent from analyze eligible/pending set; never produced by an automatic path** | unit (DB-free) + integration regression | `uv run pytest -k "terminal_failed_analyze" -x` | ❌ Wave 0 |
| ELIG-04 | Failed-only fingerprint stays eligible | integration | (equivalence matrix cell) | ❌ Wave 0 |
| INFLIGHT-01 | `in_flight` true iff ledger row exists on `"<function>:<file_id>"` | integration | (equivalence matrix cell, seed a ledger row) | ❌ Wave 0 |
| INFLIGHT-02 | **`saq_jobs` read is SAVEPOINT-wrapped; drop the `saq_jobs` table → `in_flight` still resolves from ledger, no raise** | integration | `uv run pytest -k "savepoint_degrade" -x` | ❌ Wave 0 |
| INFLIGHT-03 | Written D-01 decision record present | doc/static | decision-record artifact + `stage_status.py` module docstring | ❌ Wave 0 |

### DERIV-04 fixture matrix (the drift-lock shape)
A 2-D matrix: **rows = the 7 stages**, **columns = the 4 statuses** (`not_started`, `in_flight`, `done`, `failed`) plus edge cells:
- `analyze`: partial row (`completed_at NULL`) → `not_started`; completed → `done`; `failed_at` set → `failed`; `failed_at` set **+ ledger row** → `in_flight` (precedence proof).
- `fingerprint`: no rows → `not_started`; `[failed]` → `failed`; `[success]` → `done`; **`[success, failed]` → `done`** (DERIV-05); ledger row present → `in_flight`.
- `metadata`: row `failed_at NULL` → `done`; **row `failed_at` set (failure-only) → `failed`, NOT `done`** (D-03); no row → `not_started`.
- downstream: seed `proposals`/`tracklists`/`execution_log`(+`proposals`) rows per cell.
Each cell asserts `sql_status == py_status == expected`. The matrix is the anti-drift guarantee.

### INFLIGHT-02 SAVEPOINT-degrade sub-test
Seed a file `in_flight` via a ledger row; run the corroborating `saq_jobs` read against (a) a present table and (b) a `DROP TABLE saq_jobs` / renamed table inside the test — assert (a) enriches the queued-vs-active detail and (b) rolls back the nested scope alone, returns the safe default, and **`in_flight` still reads `True` from the ledger** with no exception surfaced. Mirrors the `get_stage_busy_counts` isolation `[VERIFIED: pipeline.py:488-493]`.

### Sampling Rate
- **Per task commit:** `uv run pytest <touched test file> -x` (the DB-free resolver test after `enums/stage.py`; the equivalence test after `stage_status.py`).
- **Per wave merge:** `just test-bucket shared` and `just test-bucket integration` in isolation (per-bucket hermeticity enforced) `[VERIFIED: reference_ci_bucket_isolation]`.
- **Phase gate:** `just integration-test` green + `uv run ruff check .` + `uv run ruff format --check .` + `uv run mypy .` + `pre-commit run --all-files`; ≥90% coverage on the two new modules (per-module floor is 90 `[VERIFIED: project_v8… cov floor 85→90]`).

### Wave 0 Gaps
- [ ] `tests/shared/test_stage_resolver.py` — DERIV-02/03, precedence, ELIG-03 terminal-failed (DB-free).
- [ ] `tests/shared/test_stage_eligibility_dag.py` — ELIG-01/02 topology + `eligible()` conjuncts (DB-free).
- [ ] `tests/integration/test_stage_status_equivalence.py` — DERIV-04 matrix + DERIV-05 + INFLIGHT-01/02 (real PG).
- [ ] Framework install: none — pytest/pytest-asyncio present.
- [ ] A shared seed-helpers module (or fixtures in the integration file) writing output rows + optional ledger rows per matrix cell.

## Security Domain

`security_enforcement` is not `false` in config (treated enabled). This phase adds read-only derivation modules + tests; it writes no data, exposes no new endpoint, and interpolates no operator input.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | yes | All new SQL is `ColumnElement` builders / bound params + one static `text("… status IN ('queued','active')")` literal (no interpolation), mirroring the T-45 read-only-probe discipline `[VERIFIED: pipeline.py:507]`. |
| V6 Cryptography | no | No crypto touched. |
| V2/V3/V4 (Authn/Session/Access) | no | No auth/session/access-control change; no endpoint added (readers wire in later phases). |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection via a derived predicate | Tampering | `ColumnElement`/`exists()`/bound params only; the single `saq_jobs` `text()` uses a static status allowlist, no interpolated operand. |
| Migration/derivation touching SAQ-owned `saq_jobs` | Tampering / queue-state repudiation | D-02: `saq_jobs` is read-only, SAVEPOINT-wrapped, detail-only, and **Alembic never references it** (Phase-77 banner carried forward — but Phase 78 adds no migration). |
| Agent boundary leak (Postgres import on the agent path) | Elevation / boundary violation | `enums/stage.py` imports no `phaze.models`/`phaze.database`; enforced by `tests/test_task_split.py`. |

## Sources

### Primary (HIGH confidence — verified in this session against the live tree)
- `src/phaze/models/{analysis,metadata,fingerprint,proposal,execution,tracklist,cloud_job,dedup_resolution,scheduling_ledger,file}.py` — column/table names, 1:1 vs 1:N cardinality, `analysis_completed_at`/`failed_at` cols, `execution_log.proposal_id` (no `file_id`), `ProposalStatus`/`ExecutionStatus`/`FileState` enum members, Phase-77 partial indexes.
- `src/phaze/tasks/_shared/deterministic_key.py` — `_KEY_BUILDERS` (file-keyed vs batch-hash), `"<function>:<natural_id>"` key format, the before/after-enqueue ledger hooks.
- `src/phaze/tasks/_shared/stage_control.py` — `STAGE_TO_FUNCTION` (metadata→extract_file_metadata, analyze→process_file, fingerprint→fingerprint_file).
- `src/phaze/services/pipeline.py` — `get_metadata_pending_files`/`get_fingerprint_pending_files`/`get_untracked_files`/`get_proposal_pending_batches` (the pending-set semantics to mirror); the 7 `begin_nested()` SAVEPOINT reads; `get_pipeline_stats` `GROUP BY state` (untouched); `_STAGE_BUSY_SQL`/`_LIVE_KEYS_SQL` (no `function` column, key-prefix bucketing).
- `src/phaze/tasks/reenqueue.py` — `_select_done_analyze_ids` (ANALYSIS_FAILED treated as analyze-DONE — the ELIG-03 precedent), `backfill_ledger_from_saq_jobs` SAVEPOINT degrade, `is_domain_completed`.
- `src/phaze/services/scheduling_ledger.py` — `get_ledger_rows`/`insert_ledger_if_absent`/`routing_for_function`; the ledger key PK.
- `src/phaze/enums/execution.py`, `src/phaze/enums/__init__.py` — DB-free StrEnum precedent + the agent import boundary.
- `tests/integration/conftest.py`, `tests/buckets.json`, `tests/shared/test_partition_guard.py` — real-PG harness (`create_async_engine`, connectivity-probe skip), bucket set, one-bucket-per-file guard.
- `.planning/phases/78-*/78-CONTEXT.md`, `.planning/phases/77-*/77-{CONTEXT,RESEARCH}.md`, `.planning/{ROADMAP,REQUIREMENTS}.md` — locked D-01..D-04, Phase-77 handoff (D-02 `done(metadata)` tightening; Pitfall 1 `= ANY(ARRAY[...])`), requirement text, Phase 78-90 sequencing.

### Secondary / Tertiary
- None required — every claim verified against primary in-repo sources. SQLAlchemy 2.0 `exists()`/`~exists()`/`case()`/`begin_nested()` semantics confirmed against in-tree usage rather than external docs (the codebase is the authoritative pattern source here).

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — zero new deps; every idiom (StrEnum, `exists`, `begin_nested`, ledger key) verified in-tree.
- Per-stage predicates & column names: HIGH — read directly from the models (incl. the `execution_log` no-`file_id` and fingerprint 1:N findings).
- `in_flight` derivation & ledger key: HIGH — `_KEY_BUILDERS` + `STAGE_TO_FUNCTION` + `scheduling_ledger` PK all confirmed; the batch-keyed-propose gap is a MEDIUM-confidence *scoping* recommendation (verified fact, deferrable judgment).
- Dual-form authoring / no-Python-eval: HIGH — SQLAlchemy exposes no supported `ColumnElement`→row evaluator; D-04 already prescribes the equivalence-test lock.

**Research date:** 2026-07-08
**Valid until:** 2026-08-07 (stable — internal codebase, no fast-moving external deps)

## RESEARCH COMPLETE
