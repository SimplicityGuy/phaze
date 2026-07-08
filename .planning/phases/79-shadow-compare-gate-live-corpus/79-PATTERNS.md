# Phase 79: Shadow-Compare Gate (live corpus) - Pattern Map

**Mapped:** 2026-07-08
**Files analyzed:** 4 (3 new, 1 modified)
**Analogs found:** 4 / 4 (all exact/role matches — this phase is composition, not new derivation)

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/services/shadow_compare.py` (NEW) | service | batch / transform (corpus-wide anti-join → Report) | `src/phaze/services/stage_status.py` | role-match (same dir, its direct dependency) |
| `src/phaze/cli/shadow_compare.py` (NEW) | CLI entrypoint | request-response (argparse → asyncio.run → exit code) | `src/phaze/cli/__init__.py` | exact (same `python -m` argparse+async_session shape) |
| `tests/integration/test_shadow_compare.py` (NEW) | test | CRUD (seed fixture corpus → assert report) | `tests/integration/test_stage_status_equivalence.py` | exact (reuse `db_session` + seed helpers verbatim) |
| `justfile` (MODIFY — add `shadow-compare` recipe) | config | — | `justfile:457-480` `[group('db')]` recipes | exact |

**Key architectural fact (from RESEARCH):** the derived side MUST reuse `done_clause(stage)` / `failed_clause(stage)` — NEVER `stage_status_case(stage)`. The CASE ladder puts `in_flight ≻ done`, so a legitimately-`ANALYZED` file with a queued re-analysis ledger row would resolve `in_flight` and false-flag. The gate asserts membership implications (`state=X ⇒ done(...)`), which map to the un-laddered correlated `exists()` predicates.

---

## Pattern Assignments

### `src/phaze/services/shadow_compare.py` (service, batch/transform)

**Analog:** `src/phaze/services/stage_status.py` (its direct dependency — lives beside it, reuses its builders)

**Reuse the P78 builders directly (D-03).** These are the "done" predicates for the 8 clean invariants (`stage_status.py:89-116`):
```python
def done_clause(stage: Stage) -> ColumnElement[bool]:
    if stage is Stage.ANALYZE:
        # DERIV-03: completion discriminator, NOT bare row existence.
        return exists(select(AnalysisResult.id).where(AnalysisResult.file_id == FileRecord.id, AnalysisResult.analysis_completed_at.isnot(None)))
    if stage is Stage.METADATA:
        return exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id, FileMetadata.failed_at.is_(None)))
    if stage in (Stage.PROPOSE, Stage.REVIEW):
        return exists(select(RenameProposal.id).where(RenameProposal.file_id == FileRecord.id))
    ...
```
`failed_clause(Stage.ANALYZE)` (`stage_status.py:127-128`) → `ANALYSIS_FAILED` invariant:
```python
return exists(select(AnalysisResult.id).where(AnalysisResult.file_id == FileRecord.id, AnalysisResult.failed_at.isnot(None)))
```

**Import block to copy** (mirror `stage_status.py:58-74`):
```python
from __future__ import annotations
from typing import TYPE_CHECKING
from sqlalchemy import ColumnElement, exists, func, select
import structlog
from phaze.enums.stage import Stage
from phaze.services.stage_status import done_clause, failed_clause
from phaze.models.file import FileRecord, FileState
from phaze.models.cloud_job import CloudJob
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.proposal import RenameProposal
if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
logger = structlog.get_logger(__name__)
```

**Raw-ORM-column predicates for the 8 Phase-78-gap invariants** (no P78 builder exists — assert with a correlated `exists()` in house style; NEVER `LEFT JOIN ... IS NULL` / `not_in()`, per RESEARCH anti-pattern and the `stage_status.py:29` docstring). Concrete column shapes verified:
- `CloudJob` (`models/cloud_job.py`): `file_id` unique FK (line 76), `status: String(16)` (line 82). `CloudJobStatus`: `awaiting`, `uploading`, `uploaded`, `submitted`, `running`, `succeeded`, `failed`.
  - `AWAITING_CLOUD ⇒ exists(CloudJob WHERE file_id AND status=='awaiting')` — exact status (unambiguous, RESEARCH A3).
  - `PUSHING`/`PUSHED ⇒ exists(CloudJob WHERE file_id)` — **row-existence only** (RESEARCH OQ1/A3 recommendation: a live-cloud file may have advanced past `uploading`/`uploaded`). Document the loosening in the invariant comment.
- `DedupResolution` (`models/dedup_resolution.py`): `file_id` unique FK (line 35). `DUPLICATE_RESOLVED ⇒ exists(DedupResolution WHERE file_id)`.
- `RenameProposal` (`models/proposal.py`): `file_id` FK (line 43), `status: String(20)` (line 47). `ProposalStatus`: `pending`, `approved`, `rejected`, `executed`, `failed`.
  - `APPROVED/REJECTED/EXECUTED/FAILED/MOVED/UNCHANGED ⇒ exists(RenameProposal WHERE file_id AND status==<mapped>)`. Apply-outcome joint-write (RESEARCH A1, `agent_proposals.py:114`): `MOVED → status='executed'`, `UNCHANGED → status='failed'`. Assert against `proposals.status`, NOT `execution_log` (which can legitimately be absent).

**Divergence query (per invariant), copy from RESEARCH Code Examples:**
```python
count = (await session.execute(
    select(func.count(FileRecord.id)).where(FileRecord.state == inv.state, ~inv.predicate())
)).scalar_one()
sample = (await session.execute(
    select(FileRecord.id).where(FileRecord.state == inv.state, ~inv.predicate()).limit(sample_cap)
)).scalars().all()   # drop .limit() when verbose
```

**Invariant-as-data registry (D-06 allowlist):** each entry = `(name, state_value, predicate_factory, hard|soft, §6.1-doc-ref)`. `FINGERPRINTED` and `LOCAL_ANALYZING` carry `soft=True` (counted, printed "expected divergence (§6.1)", never flip exit). `DISCOVERED` gets NO invariant (documented vacuous placeholder, not a silent gap). Docstring MUST call out the D-03 circularity (invariants 5,6,7,8,15 assert rows `032` created *from* `files.state`).

**FileState source of truth:** all 17 members at `src/phaze/models/file.py:20-71` (`FileState` StrEnum). Full invariant→column map is in `79-RESEARCH.md` "The 17-value invariant table".

---

### `src/phaze/cli/shadow_compare.py` (CLI, request-response)

**Analog:** `src/phaze/cli/__init__.py` (exact — the established `phaze` argparse + `asyncio.run` + `async_session` pattern)

**Entrypoint shape** (mirror `cli/__init__.py:134-184`):
```python
from __future__ import annotations
import argparse, asyncio, sys
from phaze.database import async_session          # cli/__init__.py:34
from phaze.logging_config import configure_logging # cli/__init__.py:35

def main(argv: list[str] | None = None) -> int:
    configure_logging()                            # cli/__init__.py:139 — FIRST, before DB
    args = _build_parser().parse_args(argv)
    report = asyncio.run(_run(args.database_url, args.sample_cap, args.verbose))
    print(report.render(verbose=args.verbose))
    return 1 if report.hard_fail_total else 0

if __name__ == "__main__":
    raise SystemExit(main())                        # cli/__init__.py:183-184 — exact
```

**Argparse (`type=int` for sample-cap, V5 input validation):**
```python
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shadow-compare", description="State↔derived shadow-compare gate (MIG-02).")
    parser.add_argument("--sample-cap", type=int, default=20, help="Max divergent file_ids sampled per invariant.")
    parser.add_argument("--verbose", action="store_true", help="Emit the full divergence set (uncap).")
    parser.add_argument("--database-url", default=None, help="Target DB DSN (for a live-corpus restore); defaults to app settings.")
    return parser
```

**Async session helper** (mirror `cli/__init__.py:97-100` `_run_add`):
```python
async def _run(database_url, sample_cap, verbose):
    async with async_session() as session:   # cli/__init__.py:99 idiom
        return await run_shadow_compare(session, sample_cap=sample_cap, verbose=verbose)
```
When `--database-url` is passed, build a target engine from it (a live restore) — but NEVER print the full DSN (Security: Information Disclosure; `cli/__init__.py:16-17` token-never-logged discipline). Print only host/db name if anything.

---

### `tests/integration/test_shadow_compare.py` (test, CRUD)

**Analog:** `tests/integration/test_stage_status_equivalence.py` (exact — copy `db_session` fixture + `_new_file`/seed helpers verbatim)

**Bucket marker + DSN derivation** (copy `test_stage_status_equivalence.py:65-73`):
```python
pytestmark = pytest.mark.integration   # lands in the `integration` bucket (test_partition_guard enforces one-bucket-per-file)

BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://")
SA_DSN = (os.environ.get("TEST_DATABASE_URL") or BROKER_DSN).replace("postgresql://", "postgresql+asyncpg://")
```

**`db_session` fixture — copy verbatim (`test_stage_status_equivalence.py:78-109`):** probes broker connectivity with `psycopg`, `pytest.skip`s if PG down, `Base.metadata.create_all`, seeds the `_LEGACY_AGENT_ID = "legacy-application-server"` Agent (the `files.agent_id` FK is `ON DELETE RESTRICT`), yields, rolls back at teardown. This is the exact hermetic real-PG harness the gate needs.

**`_new_file` seed helper — copy and add `state=` param (`test_stage_status_equivalence.py:116-131`):**
```python
async def _new_file(session: AsyncSession, *, state: str = "discovered") -> uuid.UUID:
    fid = uuid.uuid4()
    session.add(FileRecord(id=fid, sha256_hash=uuid.uuid4().hex, original_path=f"/media/{fid}.mp3",
                           original_filename=f"{fid}.mp3", current_path=f"/media/{fid}.mp3",
                           file_type="mp3", file_size=1234, state=state))
    await session.flush()
    return fid
```

**Reuse existing per-table seed helpers directly** — `seed_analysis_completed`/`seed_analysis_failed`/`seed_metadata_done`/`seed_metadata_failed_only`/`seed_propose_done`/`seed_apply_done` etc. (`test_stage_status_equivalence.py:148-300`) already cover metadata/analysis/fingerprint/proposal/execution rows. Add new helpers only for the gap tables: `CloudJob(file_id=fid, status='awaiting')`, `DedupResolution(file_id=fid)`, `RenameProposal(file_id=fid, status='approved'/'rejected'/'executed'/'failed')`.

**Required test cells (from RESEARCH Validation Architecture — each is non-vacuous):**
- `-k divergent` — every HARD invariant flags a seeded divergence (state=X, derived FALSE) → `report.hard_fail_total > 0`.
- `-k consistent` — every HARD invariant passes on a consistent corpus → zero HARD divergence.
- `-k implication` — a MORE-derived-than-scalar file (state=`metadata_extracted` but ALSO analysis-completed) does NOT flag (implication, not equality).
- `-k allowlist` — a seeded FINGERPRINTED/LOCAL_ANALYZING divergence is counted but `hard_fail_total == 0`.
- CLI exit: `main()` returns 1 on a seeded-divergent corpus.
- registry unit cell: `DISCOVERED` absent from `INVARIANTS`; soft set == `{FINGERPRINTED, LOCAL_ANALYZING}`.

**Parametrize pattern** (mirror `test_stage_status_equivalence.py:306-337, 400-412`): a `CASES` list of tuples + `@pytest.mark.parametrize`. Consider parametrizing over `INVARIANTS` itself so pytest and CLI share one definition (D-01 "no logic duplicated").

---

### `justfile` (MODIFY — add `[group('db')] shadow-compare`)

**Analog:** the `[group('db')]` recipes at `justfile:457-480` (`db-upgrade`, `db-revision`, `db-current`, `db-downgrade`, `db-history`)

**Recipe to add (matches the group + `uv run` house style):**
```make
[doc('Run the state↔derived shadow-compare gate against the target DB (MIG-02). Exit nonzero on hard divergence.')]
[group('db')]
shadow-compare *ARGS:
    uv run python -m phaze.cli.shadow_compare {{ARGS}}
```
Note the existing recipes use `[doc(...)]` + `[group('db')]` attributes then `uv run <cmd>` — match exactly (the `*ARGS` variadic threads `--verbose`/`--sample-cap`/`--database-url` through).

---

## Shared Patterns

### Correlated `~exists(...)` anti-join (house style)
**Source:** `src/phaze/services/stage_status.py:29-31` docstring + all `done_clause`/`failed_clause` bodies.
**Apply to:** every invariant predicate in `shadow_compare.py`.
```python
exists(select(Model.id).where(Model.file_id == FileRecord.id, <condition>))   # divergent = ~exists(...)
```
NEVER `LEFT JOIN ... IS NULL` or `not_in(subquery)` — a grep guard (`LEFT JOIN|not_in\(`) was used in Phase 78-02. All operands are ORM columns / bound params (no `text()` interpolation → avoids bandit B608).

### Real-PG hermetic session (skip-if-down)
**Source:** `tests/integration/test_stage_status_equivalence.py:78-109` `db_session` fixture.
**Apply to:** `test_shadow_compare.py`. Copy verbatim; it is proven, bucket-correct, per-test rollback, and skips (not errors) when `:5433` PG is absent.

### argparse + `asyncio.run` + `async_session` CLI
**Source:** `src/phaze/cli/__init__.py:134-184`.
**Apply to:** `cli/shadow_compare.py`. `configure_logging()` first, validate before opening a session, `raise SystemExit(main())` guard, secrets/DSN print-only-never-logged.

### `_DONE_FP` fingerprint spelling (if ever touched)
**Source:** `stage_status.py:86` `_DONE_FP = ("success", "completed")` → renders `= ANY (ARRAY[...])`, matches `ix_fprint_success` (Phase-59 WR-02).
**Apply to:** N/A for the hard invariants (`FINGERPRINTED` is allowlisted, never asserted) — noted so a future maintainer reuses `stage_status._DONE_FP` rather than re-spelling `.in_(...)`.

## No Analog Found

None. Every deliverable has a close in-tree analog — this phase is composition + reporting + a fixture corpus over already-existing derivation logic, not new derivation. The only genuinely-new surface is the `Report`/`INVARIANTS` dataclass shape (Claude's discretion, D-05), which has no direct analog but is plain first-party dataclass code.

## Metadata

**Analog search scope:** `src/phaze/services/`, `src/phaze/cli/`, `src/phaze/models/`, `src/phaze/enums/`, `tests/integration/`, `justfile`
**Files scanned:** 7 read in full/targeted (`stage_status.py`, `cli/__init__.py`, `test_stage_status_equivalence.py`, `enums/stage.py`, `models/file.py`, `justfile` db-group, model shapes for `cloud_job`/`dedup_resolution`/`proposal`)
**Pattern extraction date:** 2026-07-08
</content>
</invoke>
