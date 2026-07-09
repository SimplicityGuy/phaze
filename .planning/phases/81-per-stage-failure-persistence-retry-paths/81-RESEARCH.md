# Phase 81: Per-Stage Failure Persistence & Retry Paths - Research

**Researched:** 2026-07-08
**Domain:** Durable per-stage failure markers, DB CHECK-constrained mutual exclusion, Alembic migration 033, FastAPI optional-body version-skew, DB-free eligibility/terminality tables
**Confidence:** HIGH (every cited anchor read at HEAD; FastAPI semantics confirmed via official docs)

## Summary

This phase is **not** a design exercise — 81-CONTEXT locks 18 decisions (D-01..D-18) with `file:line`
anchors. This research **verified every anchor against HEAD** (branch `SimplicityGuy/phase-81`, alembic
head `032`), surfaced the implementation landmines the decisions imply, and produced the Validation
Architecture. **No decision is impossible or unsafe — there are no design BLOCKERS.**

Three anchor-drift corrections and one incomplete doc-enumeration were found. All are *pointer* fixes,
not design problems, but the planner must use the corrected anchors:
1. **D-11/D-15 `eligible()` line refs are off by a few lines** — the "not in (DONE, IN_FLIGHT)" metadata/
   fingerprint branch is `enums/stage.py:186-187` (CONTEXT says `:190`); the ANALYZE carve-out is `:188-189`;
   `:190` is actually the APPLY branch.
2. **D-18 join anchors point at the wrong file** — `_trackid_engine_badge` and the two aliased per-engine
   joins live in **`src/phaze/services/pipeline.py:864` and `:939-940`**, NOT `routers/pipeline.py`. (The
   `routers/pipeline.py:937` FINGERPRINTED writer *is* correct.)
3. **D-08's doc-renumber list is INCOMPLETE** — ROADMAP **lines 492 and 494** also spell migration "033"
   and must be renumbered, but D-08 lists only 21/25/36/281/485. Also REQUIREMENTS **MIG-02 (line 96) does
   NOT contain "033"** (it says "the destructive migration"); only **MIG-04 (line 98)** carries the number.

**Primary recommendation:** Plan the writer edits (D-05/D-13), the migration `033` (D-06/D-08/D-09), the
DB-free tables + `domain_completed` twin (D-14/D-15/D-17), the metadata failure marker + optional-body
endpoint (D-10) and bulk retry (D-12), and the fingerprint regression-tests-only deliverable (D-18) —
against the corrected anchors below. Gate the migration on the existing autogenerate-emptiness test
(`test_migration_032_additive_schema.py` is the exact template) and the standing shadow gate + equivalence
test staying green.

## User Constraints (from CONTEXT.md)

### Locked Decisions
Verbatim from 81-CONTEXT `<decisions>` D-01..D-18. Reproduced by reference (the CONTEXT is the source of
truth; this research does not re-litigate any of them). Key load-bearing ones the planner must cite:

- **D-05:** `report_analysis_failed` **dual-writes** `analysis.failed_at` + `error_message` **and** keeps
  `state = FileState.ANALYSIS_FAILED`, same transaction. Three live `files.state` readers stay working
  until Phase 80/82: `tasks/reenqueue.py::_select_done_analyze_ids`, `get_analysis_failed_files`,
  `get_pipeline_stats`.
- **D-06:** DB `CHECK (NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL))` on `analysis`,
  mirrored into ORM `__table_args__`. `put_analysis` success clears `failed_at`; `report_analysis_failed`
  clears `analysis_completed_at`.
- **D-07:** `analysis.error_message = f"{reason}: {error}"` truncated to the `Text` bound; reuses the
  existing `AnalysisFailurePayload` (`reason` Literal + bounded `error`). No schema change.
- **D-08:** Phase 81 ships migration `033`; Phase 90 renumbers its destructive migration `033→034`. Doc
  churn in scope.
- **D-09:** Migration `033` cleanup **clears `failed_at`, keeps `analysis_completed_at`** on mixed rows,
  and must run **before** `create_check_constraint`.
- **D-10:** `report_metadata_failed` gains `body: MetadataFailurePayload | None = None`. **Bodyless POST
  from an old agent must return 200 and still clear the ledger** (CR-02 guard).
- **D-11:** FAIL-03 retry **leaves the failure row in place and re-enqueues**. Never clear `failed_at` in
  place (would make a zero-metadata file read DONE forever).
- **D-12:** FAIL-03 = bulk operator endpoint `POST /pipeline/metadata-failed/retry`, HTMX fragment,
  mirroring `retry_analysis_failed`'s guard ordering; new `get_metadata_failed_files` query.
- **D-13:** Both `put_analysis` and `put_metadata` must **unconditionally** clear `failed_at` +
  `error_message` on success (not `exclude_unset`-driven). `put_metadata`'s empty-body
  `on_conflict_do_nothing` branch must still clear on an existing row.
- **D-14/D-15:** Create `FAILURE_IS_TERMINAL` + `ELIGIBLE_AFTER_FAILURE` (two axes) in DB-free
  `enums/stage.py`. Refactor `eligible()`'s inlined ANALYZE carve-out into `ELIGIBLE_AFTER_FAILURE`.
- **D-16:** `eligible()` refactor is semantics-preserving; ELIG-01..04 pass **unchanged**.
- **D-17:** Ship the tables + pure `domain_completed(status_map, stage)` in `enums/stage.py` + the
  `domain_completed_clause()` SQL twin in `services/stage_status.py`, drift-locked by extending Phase 78's
  parametrized equivalence test **now**.
- **D-18:** **No new fingerprint writer.** FAIL-04 deliverable is regression tests + docstrings.

### Claude's Discretion
CONTEXT states "**None** — every gray area was decided." Four items were explicitly *left to research/
planning*; this research resolves them below (see "Discretion Items Resolved").

### Deferred Ideas (OUT OF SCOPE)
- Mixed-engine fingerprint retry hole (Phase 82 / DERIV-05).
- `MAX_FINGERPRINT_ATTEMPTS` bound (no requirement asks for it).
- UI surface for failed metadata — failed-count chip + retry button (Phase 82 READ-02). FAIL-03 ships a
  **backend endpoint only**.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| FAIL-01 | `analyze` failures persist a durable marker, backfilled from `ANALYSIS_FAILED` | D-05 dual-write in `report_analysis_failed` (`agent_analysis.py:329`); backfill already shipped in `032:73-82` (D-03 — go-forward writer only); D-06 CHECK + D-13 clear-on-success |
| FAIL-02 | `metadata` failures persist a marker instead of nothing | `report_metadata_failed` (`agent_metadata.py:99`) currently clears ledger only; insert `metadata` row `failed_at` set, payload NULL; `done_clause(metadata)` (`stage_status.py:101`) already reads it as FAILED |
| FAIL-03 | Operator retry path for terminally-failed metadata | New `POST /pipeline/metadata-failed/retry` mirroring `retry_analysis_failed` (`pipeline.py:884-951`); `eligible(metadata)` already admits FAILED (`enums/stage.py:186-187`) |
| FAIL-04 | `fingerprint` failure keeps persisting via `fingerprint_results.status='failed'`, auto-retryable | `put_fingerprint` (`agent_fingerprint.py:39-48`) upserts status+error; `report_fingerprint_failed` (`:60`) persists nothing by design. Deliverable = regression tests + docstrings only (D-18) |

## Anchor Verification (highest-value output)

**Legend:** ✅ verified as stated · ⚠️ drift (corrected) · Every claim `[VERIFIED: read at HEAD]`.

| CONTEXT anchor | Status | Finding |
|----------------|--------|---------|
| `services/stage_status.py:101` `done_clause(metadata)` | ✅ | `EXISTS metadata WHERE file_id=… AND failed_at IS NULL`. A payload-NULL failure row reads FAILED, never DONE. |
| `services/stage_status.py:130` `failed_clause(metadata)` | ✅ | `EXISTS metadata WHERE file_id=… AND failed_at IS NOT NULL`. |
| `services/stage_status.py:128` `failed_clause(analyze)` | ✅ | `EXISTS analysis WHERE file_id=… AND failed_at IS NOT NULL`. |
| `enums/stage.py:75-84` `_analyze_status` `done ≻ failed` | ✅ | `if completed_at is not None: DONE` precedes `if failed_at is not None: FAILED`. Confirms D-09 cleanup keeps `done` on mixed rows without changing derived status. |
| `enums/stage.py:190` `eligible()` metadata "not in (DONE, IN_FLIGHT)" | ⚠️ | **Actual `:186-187`** (metadata/fingerprint branch). `:188-189` is the ANALYZE carve-out (`== NOT_STARTED`). **`:190` is the APPLY branch.** Refactor target for D-15. |
| `FAILURE_IS_TERMINAL` in any `.py` | ✅ (absent) | `rg "FAILURE_IS_TERMINAL\|ELIGIBLE_AFTER_FAILURE\|domain_completed" src/` → **no matches**. D-14/D-17 are net-new. (Note: `tasks/reenqueue.py` has a *separate* `_DOMAIN_COMPLETED_STAGES`/`is_done` recovery mechanism — adjacency, not collision; Phase 80 wires the new helper into it.) |
| `agent_analysis.py:198-210` `put_analysis` upsert | ✅ | `set_={k: stmt.excluded[k] for k in dumped}` (`:205`); empty-body `on_conflict_do_nothing` at `:207-210`. D-13 must add unconditional `failed_at=None, error_message=None` to BOTH branches (and to a fresh INSERT). |
| `agent_analysis.py:329` `report_analysis_failed` writes `state=ANALYSIS_FAILED`, no `failed_at` | ✅ | Handler `:310-345`. Writes only `FileRecord.state`, `clear_ledger_entry` (`:333`), `_delete_staged_object_if_cloud` (`:336`), `commit` (`:337`). D-05 adds the `analysis` upsert into this same txn; D-06 requires it also clear `analysis_completed_at`. |
| `agent_metadata.py:65-80` `put_metadata` upsert + empty-body branch | ✅ | `dumped = model_dump(exclude_unset=True)` (`:65`); `on_conflict_do_update` (`:74-77`) / empty-body `on_conflict_do_nothing` (`:78-81`). **D-13's sharper hazard confirmed:** the empty-body branch never clears the marker. |
| `agent_metadata.py:99` `report_metadata_failed` persists nothing | ✅ | Handler `:98-125`: `clear_ledger_entry` (`:121`) + `commit` only. **No `body` param today** (D-10 adds it). |
| `agent_fingerprint.py:22` `put_fingerprint` / `:60` `report_fingerprint_failed` | ✅ | `put_fingerprint` (`:21-56`) upserts `status`+`error_message` from `FingerprintWriteRequest`; `report_fingerprint_failed` (`:60-89`) clears ledger only, persists no row. Both confirm D-18. |
| `services/fingerprint.py:103,105` | ◻︎ not directly read | D-18's "IngestResult → `status='failed'` row" producer. The router `put_fingerprint` upsert (verified) is the DB write; `services/fingerprint.py` is the agent-side producer. Spot-read recommended during planning, but D-18 needs no change to it. |
| `routers/pipeline.py:884-951` `retry_analysis_failed` | ✅ | Complete FAIL-03 donor: `get_analysis_failed_files` → `NoActiveAgentError` guard (`:922-928`, returns without enqueue/mutation) → flip `FINGERPRINTED` + `commit` (`:936-938`) → enqueue loop (`:940-943`). Response template + context vars below. |
| `routers/pipeline.py:937` sole FINGERPRINTED writer | ✅ | `f.state = FileState.FINGERPRINTED`. Matches `shadow_compare.py` soft-allowlist note. |
| `routers/pipeline.py:864` `_trackid_engine_badge`, `:939-940` aliased joins | ⚠️ | **Wrong file.** Both live in **`services/pipeline.py`**: `_trackid_engine_badge` at `services/pipeline.py:864`; the two aliased outer-joins `audfprint.engine == _TRACKID_ENGINE_AUDFPRINT` / `panako.engine == _TRACKID_ENGINE_PANAKO` at `services/pipeline.py:939-940`. This is what a synthetic `engine='_task'` row would poison — D-18's rationale holds, anchor corrected. |
| `alembic/versions/032:74-80` `_BACKFILL_ANALYZE_FAILED` | ✅ | `ON CONFLICT (file_id) DO UPDATE SET failed_at = COALESCE(analysis.failed_at, EXCLUDED.failed_at)` with **no `analysis_completed_at` guard** → confirmed the D-09 mixed-row source. |
| `services/agent_client.py:401` `report_metadata_failed(file_id)` | ✅ | Signature `(self, file_id)`, no body, `_request("POST", url)` with no json. D-10 widens to accept the optional payload and send it when present. |
| `tasks/metadata_extraction.py:74-80` terminal-ack call site | ✅ | `except Exception:` (**no `as exc`**) → on `not job.retryable` calls `api.report_metadata_failed(payload.file_id)` → bare `raise`. **The exception is logged (`exc_info=True`) but NOT bound to a variable** — D-10's `error` detail requires adding `as exc` and composing a payload. |
| `schemas/agent_analysis.py:114` `AnalysisFailurePayload` | ✅ | `:114-127`: `reason: Literal["timeout","crashed","error"]`, `error: str \| None = Field(default=None, max_length=2000)`, `extra='forbid'`. D-10's `MetadataFailurePayload` copies this shape (add to `schemas/agent_metadata.py`). |
| `services/shadow_compare.py:30-32,150` soft allowlist | ✅ | Exactly `{fingerprinted, local_analyzing}`, `soft=True`, "need not imply fingerprint success". Must stay unchanged (D-04). |
| `tasks/reenqueue.py::_select_done_analyze_ids` + `get_analysis_failed_files` + `get_pipeline_stats` | ✅ | `_select_done_analyze_ids` (`reenqueue.py:177`) selects `{ANALYZED, ANALYSIS_FAILED}`; `get_analysis_failed_files` (`services/pipeline.py:1057`) = `get_files_by_state(ANALYSIS_FAILED)`; `get_pipeline_stats` (`services/pipeline.py:58`) groups by `files.state`. All three read `files.state` — D-05's dual-write keeps them alive. |

## Migration 033 Mechanics

**Head confirmed:** `alembic/versions/` tops out at `032_add_derived_status_schema.py` (`revision="032"`,
`down_revision="031"`). **`033` is genuinely next; nothing else claims it.** `[VERIFIED: ls alembic/versions]`

**Template:** `032_add_derived_status_schema.py` is the exact pattern to copy — static-SQL constants,
`op.execute(sa.text(...))`, DDL-only `downgrade()`, `Revises`/`revision` header.

### D-06 CHECK constraint DDL + ORM mirror

The `analysis` model already has `__table_args__` (`models/analysis.py:49-52`, two partial indexes, **no
CHECK yet**). D-08/77-precedent: the CHECK must be mirrored so `alembic revision --autogenerate` stays
empty. In-repo precedent for a named CHECK in `__table_args__`: `models/pipeline_stage_control.py`
(`CheckConstraint("priority BETWEEN 0 AND 100", name="priority_range")`), also `cloud_job.py`, `agent.py`.

**Migration side (`033.upgrade`), order is mandatory (D-09):**
```python
# (1) D-09 mixed-row cleanup FIRST — clears failed_at, keeps analysis_completed_at (done ≻ failed).
op.execute(sa.text("""
    UPDATE analysis
       SET failed_at = NULL
     WHERE analysis_completed_at IS NOT NULL
       AND failed_at IS NOT NULL
"""))
# (2) THEN the CHECK — a pre-existing mixed row would abort create_check_constraint otherwise.
op.create_check_constraint(
    "analysis_completed_xor_failed",           # -> ck_analysis_analysis_completed_xor_failed via naming convention
    "analysis",
    "NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)",
)
```
**ORM mirror (`models/analysis.py` `__table_args__`):**
```python
from sqlalchemy import CheckConstraint
__table_args__ = (
    Index("ix_analysis_completed", "file_id", postgresql_where=text("analysis_completed_at IS NOT NULL")),
    Index("ix_analysis_failed", "file_id", postgresql_where=text("failed_at IS NOT NULL")),
    CheckConstraint("NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)",
                    name="analysis_completed_xor_failed"),
)
```
**Naming caveat:** the repo's `ck_%(table_name)s_%(constraint_name)s` convention re-prefixes — pass the
bare name (`analysis_completed_xor_failed`), as `032` does for `status_enum` (see `032:66-67` comment).
Confirm the rendered constraint name matches the autogenerate probe (below) so the empty-diff holds.

**`downgrade()`:** `op.drop_constraint("analysis_completed_xor_failed", "analysis", type_="check")`. The
D-09 UPDATE is **not** reversed (016/032 precedent — best-effort DDL reversal only).

**Autogenerate-emptiness is a REAL test in this repo** `[VERIFIED]`:
`tests/integration/test_migrations/test_migration_032_additive_schema.py` runs alembic's autogenerate
(`compare_metadata`/`produce_migrations`) against `Base.metadata` after upgrading to the migration head and
asserts an **EMPTY diff** for that migration's objects (PERF-01 SC#2), then exercises `downgrade`. **Phase 81
must add `test_migration_033_*.py` in the same directory** asserting: (a) upgrade adds the CHECK + runs the
cleanup, (b) autogenerate diff is empty for the CHECK, (c) a pre-seeded mixed row is cleaned before the
CHECK, (d) down/up round-trips. Migration tests run in the **`integration`** bucket. The migrations test DB
is provisioned by the `justfile` (`phaze_migrations_test`, `MIGRATIONS_TEST_DATABASE_URL`, justfile `:191-215`).

### D-08 doc renumber — CORRECTED and COMPLETE enumeration

⚠️ **CONTEXT D-08's list is incomplete.** The full set of lines naming migration "033" that must become "034":

| File | Line | Current text (abbrev.) | In D-08 list? |
|------|------|------------------------|---------------|
| `.planning/ROADMAP.md` | 21 | "additive `032` → … → destructive `033`" | yes |
| `.planning/ROADMAP.md` | 25 | Phase 79 entry "before `033` (MIG-02)" | yes |
| `.planning/ROADMAP.md` | 36 | Phase 90 title "Destructive Migration `033`" | yes |
| `.planning/ROADMAP.md` | 281 | table row "Destructive Migration 033" | yes |
| `.planning/ROADMAP.md` | 485 | "### Phase 90: Destructive Migration `033`" | yes |
| `.planning/ROADMAP.md` | **492** | "Migration `033` (in one transaction…)" | **NO — MISSED** |
| `.planning/ROADMAP.md` | **494** | "`033.downgrade()` documents the enum reconstruction…" | **NO — MISSED** |
| `.planning/REQUIREMENTS.md` | 98 (MIG-04) | "Migration `033` is destructive and lands last" | D-08 says "MIG-02/MIG-04" |
| `.planning/REQUIREMENTS.md` | 96 (MIG-02) | "…before the destructive migration" — **no "033" string** | **D-08 over-lists this** |
| `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` | (grep at plan time) | references destructive "033" | yes |

**Recommendation:** the renumber is a coherent unit — land it in this PR (D-08 says in scope). But the
planner MUST grep `033` across ROADMAP/REQUIREMENTS/PARALLEL-ENRICH-DAG-DESIGN.md at plan time rather than
trust the enumerated line numbers, because (a) 492/494 were missed and (b) line numbers shift as this PR's
own edits land. **Do not renumber Phase 81's OWN new `033` migration file** — only the *destructive* (Phase
90) references.

**Will `just docs-drift` trip?** `just docs-drift` = `uv run pytest tests/shared/core/test_requirements_traceability.py`
(justfile `:97`). The guard parses **requirement-ID checkboxes ↔ ROADMAP phase mapping**; it does not assert
on migration-number prose. Renumbering "033"→"034" in prose should not trip it. **Confidence MEDIUM** —
recommend running `just docs-drift` after the edit to confirm (it lives in the `shared` bucket).

## Discretion Items Resolved

1. **Ordering of `report_analysis_failed`'s new `analysis` upsert vs. `clear_ledger_entry` +
   `_delete_staged_object_if_cloud` + `commit`.** All four must be in the **same transaction** (Phase 45
   L-02, verified `agent_analysis.py:330-337`). Recommended order: (a) `analysis` upsert stamping
   `failed_at` + `error_message` **and clearing `analysis_completed_at`** (D-06), (b) `FileRecord.state =
   ANALYSIS_FAILED`, (c) `clear_ledger_entry`, (d) `_delete_staged_object_if_cloud`, (e) single `commit`.
   The upsert must handle the "no `analysis` row exists" case (a pure analyze failure never wrote one) —
   `pg_insert(...).on_conflict_do_update` on `file_id`, exactly like the `032` backfill's INSERT..ON CONFLICT.

2. **Where does `get_metadata_failed_files` live?** **`services/pipeline.py`**, alongside its donor
   `get_analysis_failed_files` (`services/pipeline.py:1057`) and `get_metadata_pending_files` (`:1330`) —
   not the router. Unlike the donor (a `get_files_by_state` one-liner), metadata has no `FileState`, so the
   query joins `FileRecord → FileMetadata WHERE metadata.failed_at IS NOT NULL` (reuse the `failed_clause(
   METADATA)` shape from `stage_status.py:130`, or a direct correlated `exists`). Pure ORM, no interpolation.

3. **Does the FAIL-03 HTMX fragment reuse an existing template?** `retry_analysis_failed` renders
   **`pipeline/partials/retry_failed_response.html`** with context `{"request", "count": int,
   "no_active_agent": bool}` (`pipeline.py:914-949`, three render sites: empty, no-agent, success). The
   metadata retry can reuse this exact template + context vars (the fragment is stage-agnostic). Confirm the
   template text isn't analyze-worded before reusing; if it is, add a `stage`/label context var or a sibling
   `metadata_retry_response.html`. **Recommendation:** reuse `retry_failed_response.html` unless its copy
   hard-codes "analysis".

4. **Does the Phase 90 `033→034` rename land in this PR?** Yes (D-08, in scope) — but see the corrected/
   complete line enumeration above; grep, don't trust line numbers.

## FastAPI Optional-Body Semantics (D-10 / CR-02 guard)

**Question:** does a **bodyless POST** to an endpoint with `body: MetadataFailurePayload | None = None`
return **200**, not 422? **Answer: YES.** `[CITED: fastapi.tiangolo.com/tutorial/body-multiple-params]`

The FastAPI official tutorial (`docs/en/docs/tutorial/body-multiple-params.md`) documents exactly this
construct: `item: Item | None = None` — "The `item` body parameter is optional due to its `None` default."
A single Pydantic-model parameter with a `None` default is **not required**; a request omitting the body
binds the parameter to `None` and the handler runs normally (200). `[VERIFIED: Context7 /fastapi/fastapi]`

**Precise construct to use (matches the CR-02 requirement):**
```python
@router.post("/{file_id}/failed", status_code=status.HTTP_200_OK, response_model=MetadataFailureResponse)
async def report_metadata_failed(
    file_id: uuid.UUID,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: MetadataFailurePayload | None = None,   # NO Body(...) wrapper, NO embed
) -> MetadataFailureResponse: ...
```
**Edge cases (all resolve to 200 → None body):**
- No `Content-Type`, zero-length body → `None`.
- `Content-Type: application/json` with empty body → `None` (FastAPI treats missing optional body as `None`).
- Body literal `null` → `None`.
- A **present** JSON body with an unknown field → **422** (because `MetadataFailurePayload` keeps
  `extra='forbid'`, AUTH-01) — this is correct and desired; it only affects *new* agents that opt in.

**Why this is the worst-failure-mode guard:** a *required* body param would 422 an old (bodyless) agent →
the terminal-ack never reaches `report_metadata_failed` → `extract_file_metadata:<file_id>` is never
cleared → `recover_orphaned_work` re-enqueues forever (the CR-02 unbounded loop). **Test both paths**
(bodyless → 200 + ledger cleared; with body → 200 + `error_message` populated). Keep `extra='forbid'`.

## Terminality / Eligibility Tables (D-14/D-15/D-17)

`enums/stage.py` is **stdlib-only, DB-free** (`test_stage_resolver.py` enforces no `sqlalchemy`/`phaze.models`
import — verified in the module docstring `:9-11`). D-15's two dicts and `domain_completed()` must keep this.

```python
# D-15: two axes — conflating them is a live trap.
FAILURE_IS_TERMINAL:    dict[Stage, bool] = {Stage.ANALYZE: True,  Stage.METADATA: True,  Stage.FINGERPRINT: False}
ELIGIBLE_AFTER_FAILURE: dict[Stage, bool] = {Stage.ANALYZE: False, Stage.METADATA: True,  Stage.FINGERPRINT: True}

# D-17: pure helper (Python twin).
def domain_completed(status_map, stage) -> bool:
    st = status_map.get(stage, Status.NOT_STARTED)
    return st is Status.DONE or (st is Status.FAILED and FAILURE_IS_TERMINAL[stage])
```
**D-16 refactor (semantics-preserving):** `eligible()`'s current three enrich branches (`enums/stage.py:186-189`)
collapse using `ELIGIBLE_AFTER_FAILURE` — for METADATA/FINGERPRINT/ANALYZE, `eligible = status not in (DONE,
IN_FLIGHT) and (status != FAILED or ELIGIBLE_AFTER_FAILURE[stage])`. This yields identical truth for all three:
ANALYZE (`ELIGIBLE_AFTER_FAILURE=False`) → eligible iff `NOT_STARTED`; METADATA/FINGERPRINT (`True`) → eligible
iff `NOT_STARTED or FAILED`. **ELIG-01..04 must pass unchanged** (`tests/shared/test_stage_eligibility_dag.py`).

**SQL twin (D-17):** `domain_completed_clause(stage)` in `services/stage_status.py` = `or_(done_clause(stage),
and_(failed_clause(stage), <FAILURE_IS_TERMINAL[stage] as literal>))`. When `FAILURE_IS_TERMINAL[stage]` is
False, the second disjunct collapses to `false()`. Ship it **this phase** and lock it against the Python twin
by extending the equivalence test (below) — do NOT land the Python helper and SQL twin one phase apart.

## Test Surface / How To Run

**Test buckets** (`tests/buckets.json`): `discovery, metadata, fingerprint, analyze, identify, review,
agents, integration, shared`. Run a single bucket in isolation with `just test-bucket <name>` (DB buckets
serial, DB-free buckets `XDIST="-n auto"`). **New tests must pass in bucket isolation**, not just the full
suite (known hazards: `get_settings` lru_cache leak, `saq_jobs` stub poison).

| Touched surface | Test file / bucket |
|-----------------|--------------------|
| `report_analysis_failed`, `put_analysis` clear-on-success | `tests/analyze/routers/test_agent_analysis*.py` — **analyze** bucket |
| `report_metadata_failed` (bodyless + with body), `put_metadata` clear-on-success + empty-body branch | `tests/metadata/routers/test_agent_metadata.py` (existing failed/clear tests at `:299-333`) — **metadata** bucket |
| `report_fingerprint_failed` no-row regression, per-engine eligible | `tests/fingerprint/routers/test_agent_fingerprint*.py` — **fingerprint** bucket |
| `POST /pipeline/metadata-failed/retry` (mirror `retry_analysis_failed` tests) + `get_metadata_failed_files` | `tests/*/routers/test_pipeline*.py` / **integration** bucket |
| `domain_completed`, `FAILURE_IS_TERMINAL`, `ELIGIBLE_AFTER_FAILURE`, `eligible()` refactor | DB-free — `tests/shared/test_stage_eligibility_dag.py`, `tests/shared/test_stage_resolver.py` — **shared** bucket |
| `domain_completed_clause` ↔ `domain_completed` equivalence | `tests/integration/test_stage_status_equivalence.py` — **integration** bucket |
| Migration 033 up/down + autogenerate-empty | `tests/integration/test_migrations/test_migration_033_*.py` (template: `test_migration_032_additive_schema.py`) — **integration** bucket |
| Shadow gate stays green | `tests/integration/test_shadow_compare.py` + `tests/shared/test_shadow_compare_cli.py` |
| docs-drift after renumber | `just docs-drift` → `tests/shared/core/test_requirements_traceability.py` — **shared** bucket |

**Equivalence test to extend (D-17)** `[VERIFIED: tests/integration/test_stage_status_equivalence.py]`: a
parametrized `_CASES` list of `(Stage, seed_fn, expected_status)` tuples (`:308-336`) seeds rows, then
compares `resolve_status(stage, load_scalars(...))` (Python) against `select(stage_status_case(stage))` (SQL)
per cell. Existing cells already cover the relevant states: `(ANALYZE, seed_analysis_failed, "failed")`
(`:311`), `(METADATA, seed_metadata_failed_only, "failed")` (`:316`), `(FINGERPRINT, seed_fp_failed_only,
"failed")` (`:322`), `(FINGERPRINT, seed_fp_success_and_failed, "done")` (`:321`). **D-17 extension:** add a
parallel parametrized cell set asserting `domain_completed(load_scalars(...), stage) == <bool(
domain_completed_clause result)>` reusing the same seed fns — so the Python table and SQL twin can never
drift (the exact drift Phase 78 D-04 closed). `load_scalars` (`:351`) already reads each stage's rows into
the DB-free scalar dict.

**Router test pattern** `[VERIFIED: tests/conftest.py]`: `client` fixture = `AsyncClient(ASGITransport(
app=app))` (`:213-217`); an authed-agent variant sets `Authorization: Bearer <token>` (`:246-257`) so
`Depends(get_authenticated_agent)` passes; `seed_test_agent` seeds the agent + token. Agent endpoints
(`report_*_failed`, `put_*`) use the authed client; operator `pipeline.py` endpoints use the plain `client`.
Existing metadata failed/ledger tests (`test_agent_metadata.py:299-333`) are the direct template for D-10/D-13.

## Common Pitfalls

### Pitfall 1: D-13 clear driven by `exclude_unset` (silent rows)
**What goes wrong:** `put_analysis`/`put_metadata` build `set_` from `model_dump(exclude_unset=True)`;
`failed_at` is never in the agent body, so a successful retry after a failure leaves `failed_at` set → the
file reads `failed` forever, and for analyze it **violates D-06's CHECK** (both `analysis_completed_at` and
`failed_at` non-NULL → INSERT/UPDATE aborts). **Avoid:** add `failed_at=None, error_message=None`
**unconditionally** to the SET clause (and for analyze, this is also what makes the completion branch satisfy
the CHECK). **Sharper for `put_metadata`:** the empty-body `on_conflict_do_nothing` branch (`:78-81`) never
clears — an empty-body success PUT after a failure must still `UPDATE metadata SET failed_at=NULL,
error_message=NULL WHERE file_id=…` on an existing row.

### Pitfall 2: D-09 ordering (CHECK before cleanup → migration aborts)
Mixed rows already exist in the live corpus (`032` backfill with no `completed_at` guard). If
`create_check_constraint` runs first, the constraint validation fails on the pre-existing mixed row and the
whole migration aborts. **Cleanup UPDATE must run first.** Verified precedent that mixed rows are real:
`032:79-81`.

### Pitfall 3: Clearing `failed_at` in place on the metadata retry (D-11 trap)
The metadata failure row has payload columns NULL. `done(metadata)` = "row present AND `failed_at IS NULL`".
If the retry cleared `failed_at` **without** re-extracting, a zero-metadata file would read **DONE** and never
be extracted again. **D-11: leave the row, re-enqueue; `put_metadata`'s clear-on-success wipes `failed_at`
only when real metadata lands.**

### Pitfall 4: Synthetic fingerprint failure row poisons per-engine joins (D-18)
A `fingerprint_results(engine='_task', status='failed')` sentinel row would (a) satisfy `failed_clause(
fingerprint)`'s "at least one engine failed" and (b) show up as an unknown engine in the two aliased joins at
**`services/pipeline.py:939-940`** feeding `_trackid_engine_badge` (`services/pipeline.py:864`). **D-18: write
no row** — `report_fingerprint_failed` clears the ledger only; the real per-engine `status='failed'` row from
`put_fingerprint` is the durable marker.

### Pitfall 5: NoActiveAgentError → default-queue fallthrough (Phase 30 regression)
The FAIL-03 endpoint must resolve the per-agent queue **once**, catch `NoActiveAgentError`, and return
**without enqueuing or mutating state** — never fall through to the consumer-less default queue (the Phase 30
44.5K-job incident). Mirror `retry_analysis_failed:920-928` exactly. FAIL-03 is *simpler* than its donor:
metadata has no terminal `FileState`, so there is **no bucket flip** before enqueuing (skip the
`f.state = FINGERPRINTED` step). Commit before enqueue; the deterministic `extract_file_metadata:<file_id>`
key dedups in-flight files.

## Don't Hand-Roll

| Problem | Don't build | Use instead | Why |
|---------|-------------|-------------|-----|
| Metadata failure payload schema | New ad-hoc model | Copy `AnalysisFailurePayload` shape (`Literal reason` + bounded `error` + `extra='forbid'`) | D-10; consistency + AUTH-01 |
| Bulk metadata retry endpoint | New guard logic | Mirror `retry_analysis_failed` (`pipeline.py:884-951`) | Phase-30-hardened guard ordering |
| Failed-metadata list query | Raw SQL | `failed_clause(METADATA)` shape / correlated `exists` in `services/pipeline.py` | SQLi hygiene (B608), reuse |
| `done`/`failed` SQL predicates | New CASE | `done_clause`/`failed_clause`/`stage_status_case` | Locked by equivalence test |
| Migration + empty-diff assertion | Ad-hoc test | Copy `test_migration_032_additive_schema.py` | PERF-01 empty-diff contract |
| HTMX retry fragment | New template | `pipeline/partials/retry_failed_response.html` | Stage-agnostic, 3 render sites |

## Validation Architecture

**REQUIRED (Nyquist). `workflow.nyquist_validation` not disabled.** For each requirement: observable signal,
sampling point, anti-drift check.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | `pytest` + `pytest-asyncio` (via `uv run`) |
| Config | `pyproject.toml`; per-bucket via `tests/buckets.json` |
| Quick run (per touched bucket) | `uv run just test-bucket metadata` / `analyze` / `fingerprint` / `shared` |
| Migration/equivalence/shadow | `uv run just test-bucket integration` |
| Full suite gate | `pre-commit run --all-files` + combined-coverage gate (90% floor) |
| Migration DB | `just` provisions `phaze_migrations_test` (`MIGRATIONS_TEST_DATABASE_URL`) |

### FAIL-01..04 → Test Map
| Req | Observable signal | Sampling point | Anti-drift check |
|-----|-------------------|----------------|------------------|
| FAIL-01 | After `POST /analysis/{id}/failed`: `analysis.failed_at` NOT NULL, `error_message == "reason: error"`, `analysis_completed_at` NULL, `state=ANALYSIS_FAILED`, ledger cleared; D-06 CHECK holds | analyze-bucket router test + DB assertion; **migration 033** up/down/up (CHECK + mixed-row cleanup) in integration bucket | equivalence cell `(ANALYZE, seed_analysis_failed, "failed")` already green; new autogenerate-empty test; shadow gate green (no derived-status change) |
| FAIL-02 | After `POST /metadata/{id}/failed` (bodyless AND with body): a `metadata` row exists with `failed_at` NOT NULL, payload cols NULL; `done(metadata)` derives **FAILED** not DONE; ledger cleared; 200 both ways | metadata-bucket router test (both body paths) + `resolve_status(METADATA)` assertion | equivalence cell `(METADATA, seed_metadata_failed_only, "failed")` extended; `extra='forbid'` rejects unknown field (422) |
| FAIL-03 | `POST /pipeline/metadata-failed/retry` re-enqueues every `metadata.failed_at IS NOT NULL` file, **leaves the row**, returns HTMX fragment; `NoActiveAgentError` → no enqueue/no mutation | integration-bucket endpoint test mirroring `retry_analysis_failed` tests; `get_metadata_failed_files` unit test | assert failure row survives a retry that hasn't succeeded (D-11); assert no default-queue fallthrough (Pitfall 5) |
| FAIL-04 | `report_fingerprint_failed` persists **no** `fingerprint_results` row (only clears ledger); a per-engine `status='failed'` row keeps the file `eligible(fingerprint)` and `_trackid_engine_badge` unpoisoned | fingerprint-bucket regression tests + docstring assertions | assert row count unchanged after `report_fingerprint_failed`; assert `services/pipeline.py:939-940` joins see no `engine='_task'` |

### Sampling Rate
- **Per task commit:** the touched bucket (`just test-bucket <bucket>`), in isolation.
- **Per wave merge:** `integration` + `shared` buckets (equivalence, migration, shadow, docs-drift).
- **Phase gate:** full suite green + `just docs-drift` green + shadow gate green before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `schemas/agent_metadata.py::MetadataFailurePayload` — new model (copies `AnalysisFailurePayload`).
- [ ] `tests/integration/test_migrations/test_migration_033_*.py` — new (template `032`).
- [ ] `domain_completed` equivalence cells in `test_stage_status_equivalence.py` — extend `_CASES`.
- [ ] Framework already present — no install needed.

## Runtime State Inventory

Rename/refactor-adjacent (D-08 doc renumber + `eligible()` refactor). Explicit sweep:

| Category | Items found | Action |
|----------|-------------|--------|
| Stored data | **Mixed `analysis` rows** (`completed_at` + `failed_at` both set) exist in the LIVE corpus from `032`'s unguarded backfill (D-09). | **Data migration** (033 cleanup UPDATE) — not just code. |
| Live service config | None — no external service embeds a renamed string. Migration `033` runs via alembic on deploy. | None. |
| OS-registered state | None. | None. |
| Secrets/env vars | None — no key rename. `MIGRATIONS_TEST_DATABASE_URL` is test-only, unchanged. | None. |
| Build artifacts | None — no package rename. New migration file is picked up automatically by alembic. | None. |

**Canonical question — after every file is updated, what runtime state still has the old shape?** The
**live-corpus mixed `analysis` rows**. They are cleaned by the 033 UPDATE (D-09), which must precede the CHECK.
Nothing else persists a stale form.

## Environment Availability

| Dependency | Required by | Available | Fallback |
|------------|-------------|-----------|----------|
| PostgreSQL (`phaze_test` + `phaze_migrations_test`) | migration/equivalence/router tests | via `just` provisioner (justfile `:191-215`) | none — required |
| `uv` | all commands | project constraint | none |
| Redis | not needed this phase (SAQ queue is Postgres since Phase 36) | n/a | n/a |

No new external tool. No new package (CONTEXT: "Zero new dependencies").

## Assumptions Log

| # | Claim | Section | Risk if wrong |
|---|-------|---------|---------------|
| A1 | `services/fingerprint.py:103,105` produces the `status='failed'` `IngestResult` row exactly as D-18 states | Anchor table | LOW — router `put_fingerprint` upsert (verified) is the DB write; spot-read at plan time |
| A2 | `just docs-drift` won't trip on "033"→"034" prose | Migration 033 | LOW — guard is requirement-ID↔phase; run it after edit to confirm |
| A3 | `retry_failed_response.html` copy is stage-agnostic enough to reuse for metadata | Discretion #3 | LOW — read the template at plan time; add a label var if it hard-codes "analysis" |
| A4 | Empty-body/no-Content-Type POST binds optional Pydantic param to `None` (→200) in this FastAPI version | FastAPI section | LOW — documented pattern (Context7); test both paths explicitly regardless |

## Open Questions

1. **Constraint name collision.** `analysis_completed_xor_failed` → convention-prefixed
   `ck_analysis_analysis_completed_xor_failed`. Verify the rendered name matches the autogenerate probe so the
   empty-diff holds (the `032` `status_enum` bare-name comment `:66-67` is the precedent to follow).
   *Recommendation:* assert the constraint name in the new migration test.

2. **`report_analysis_failed` upsert when no `analysis` row exists.** A pure analyze failure never wrote an
   `analysis` row. The D-05 writer must `INSERT..ON CONFLICT (file_id) DO UPDATE` (like the `032` backfill),
   not a bare UPDATE. *Recommendation:* explicit in the plan; add a "failed with no prior row" test cell.

## Sources

### Primary (HIGH confidence)
- Codebase at HEAD (`SimplicityGuy/phase-81`): `enums/stage.py`, `services/stage_status.py`,
  `services/pipeline.py`, `routers/agent_analysis.py`, `routers/agent_metadata.py`,
  `routers/agent_fingerprint.py`, `routers/pipeline.py`, `services/agent_client.py`,
  `tasks/metadata_extraction.py`, `tasks/reenqueue.py`, `services/shadow_compare.py`,
  `models/analysis.py`, `models/metadata.py`, `schemas/agent_analysis.py`, `schemas/agent_metadata.py`,
  `alembic/versions/032_add_derived_status_schema.py`, `justfile`, `tests/buckets.json`, `tests/conftest.py`,
  `tests/integration/test_stage_status_equivalence.py`, `tests/integration/test_migrations/`,
  `.planning/ROADMAP.md`, `.planning/REQUIREMENTS.md`.
- FastAPI official docs — `docs/en/docs/tutorial/body-multiple-params.md` (optional Pydantic body param with
  `None` default) via Context7 `/fastapi/fastapi`.

### Secondary (MEDIUM confidence)
- `just docs-drift` guard behavior inferred from `tests/shared/core/test_requirements_traceability.py` name +
  justfile recipe (not line-by-line read).

## Metadata

**Confidence breakdown:**
- Anchor verification: HIGH — every anchor read at HEAD; drifts corrected with exact file:line.
- Migration 033 mechanics: HIGH — `032` template + real autogenerate-empty test verified.
- FastAPI optional-body (D-10): HIGH — official docs pattern confirmed; edge cases enumerated.
- Doc-renumber completeness (D-08): HIGH — grepped ROADMAP/REQUIREMENTS; found 2 missed lines + 1 over-list.
- `just docs-drift` non-trip: MEDIUM — guard not read line-by-line; run to confirm.

**Research date:** 2026-07-08
**Valid until:** 2026-08-07 (stable internal codebase; re-verify anchors if other phase-8x branches merge first)
