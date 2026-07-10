# Phase 80: Recovery / Re-enqueue Cutover — Research

**Researched:** 2026-07-10
**Domain:** Recovery/reconcile cutover from `FileRecord.state` reads to the Phase-78/81/83 derived layer
**Confidence:** HIGH (every claim below verified against the working tree at HEAD `32623a59`, which carries `main` @ `09cefc6d` plus three docs-only commits)
**Scope of this research:** DE-RISK EXECUTION. CONTEXT.md D-01..D-14 are LOCKED and are NOT re-litigated here. This document verifies the code facts the plans will be built on and resolves the "Claude's Discretion" items with evidence.

---

## Summary

1. **All 30+ line-number citations in CONTEXT.md are accurate** except three: `select_active_agent(kind="compute")` is at `reenqueue.py:382` (focus item 6 cited `:374`); `shadow_compare._cloud_awaiting` is at `:80` (CONTEXT cited `:82`); and the SC-2 regression test's home is `tests/analyze/tasks/test_recovery.py`, **not** the repo-root `tests/test_recovery.py` the canonical-refs section names (the bucket partition guard forbids root-level tests). None of the three affect the design; all affect where a plan writes/reads.
2. **The teeth-having source-guard idiom already exists**: `tests/shared/test_dedup_fingerprint_source_scan.py` (Phase 84) is the mutation-proven AST scanner that fixed exactly the two Phase-83 toothless-guard blind spots. Phase 80's "zero `FileRecord.state` reads" guard should be a near-copy of it. Both target files end the phase with **clean absence** of `FileRecord.state`/`FileState`, so the guard needs no allow-list (simpler and stronger than the dedup guard, which has one allowed writer).
3. **`AnalysisResult` carries `updated_at`** via `TimestampMixin` (`models/analysis.py:13`). D-13's `a.updated_at` source is valid. Note the value is a best-effort proxy (true completion time was never recorded — that is *why* the backfill exists); `done_clause(ANALYZE)` only tests `IS NOT NULL`, so `updated_at` vs `created_at` is immaterial to cutover correctness.
4. **Migration `036` is genuinely the next free number** (chain tip is `035`, `down_revision="034"`). The column `analysis_completed_at` already exists since `028`, so `036` is data-only with an empty autogenerate diff. Prod-at-`031` means `032→036` all run in one first-deploy sweep; `036` runs *after* `032`, and because `032` only sets `failed_at` for `analysis_failed` files, the `failed_at IS NULL` guard correctly selects `analyzed` files — ordering is safe.
5. **The two D-08 inline clauses are byte-identical** (`pipeline.py:1136-1140` and `:1310-1314`). The extracted builder belongs in `stage_status.py`; adding `CloudJob` needs one new import and creates **no cycle** (`models/cloud_job.py` imports only SQLAlchemy + Base).
6. **D-04+D-12 fix a HARD shadow violation that exists on `main` today** — reconcile currently writes `state=AWAITING_CLOUD` while stamping `cloud_job.status=FAILED`, violating the `awaiting_cloud` implication invariant (`shadow_compare.py:131`). Retiring the state write makes the gate strictly *more* green.
7. **`036` MUST be sequenced before any live-corpus shadow-compare assertion**: 1001 prod `analyzed` files have `analysis_completed_at` NULL; until `036` runs, `done_clause(ANALYZE)` returns False for them and the `analyzed` HARD invariant is RED (project memory + folded todo).
8. **PROV-01 is untouched**: Phase 80 changes done-set derivation, not the `select_active_agent(session, kind="compute")` call at `reenqueue.py:382`. Report-only, as design §9 asks.
9. **Test buckets**: recovery/reenqueue/reconcile all live under `tests/analyze/…` → `just test-bucket analyze`; the equivalence test + migration `036` test live under `tests/integration/` → `just test-bucket integration`. The migration harness defaults to port **5432** (`conftest.py:37`) but `just test-db` provisions **5433** and `just test-bucket` does not export the URL — the exact export is mandatory.
10. **`just docs-drift` does NOT check migration numbers** (verified: zero migration references in `test_requirements_traceability.py`); it cross-checks requirement-ID ↔ checkbox ↔ Traceability Status only. The D-14 prose edits are safe as long as they do not alter MIG-04's checkbox/ID/Status.

---

## Citation Verification

Working tree HEAD is `32623a59` = `main` @ `09cefc6d` + 3 docs-only commits. Source files are byte-identical to the context's baseline.

| Symbol / claim | Cited | Actual | Status |
|---|---|---|---|
| `stage_status.py` `inflight_clause` | :175 | 175 | ✅ OK |
| `stage_status.py` `domain_completed_clause` | :195 | 195 | ✅ OK |
| `stage_status.py` `done_clause(ANALYZE)` requires `analysis_completed_at IS NOT NULL` | :123 | 123 | ✅ OK |
| `stage_status.py` `done_clause`/`failed_clause`/`stage_status_case` | — | 114 / 145 / 222 | ✅ OK |
| `enums/stage.py` `FAILURE_IS_TERMINAL` | :87 | 87 | ✅ OK |
| `enums/stage.py` `domain_completed` | :186 | 186 | ✅ OK |
| `enums/stage.py` `resolve_status` / `eligible` | — | 154 / 215 | ✅ OK |
| `backends.py` `hold_awaiting_cloud` | :86 | 86 | ✅ OK |
| `backends.py` `_enqueue_push_file` | :154 | 154 | ✅ OK |
| `reenqueue.py` `_build_done_sets` | :136 | 136 | ✅ OK |
| `reenqueue.py` `_select_done_analyze_ids` (state read) | :177 (:187) | 177 (187) | ✅ OK |
| `reenqueue.py` `_select_done_push_ids` (state read) | :190 (:197) | 190 (197) | ✅ OK |
| `reenqueue.py` `_get_awaiting_cloud_ids` (state read) | :200 (:209) | 200 (209) | ✅ OK |
| `reenqueue.py` `_in_flight_cloud_job_ids` (sidecar; no change) | :212 | 212 | ✅ OK |
| `reenqueue.py` `is_domain_completed` | :242 | 242 | ✅ OK |
| `reenqueue.py` pending-fn imports | :79 / :81 | 79 / 81 | ✅ OK |
| `reenqueue.py` `select_active_agent(kind="compute")` (PROV-01) | **:374** (focus 6) | **382** | ⚠️ **DRIFT** |
| `reconcile_cloud_jobs.py` at-cap spill write | :212 | 212 | ✅ OK |
| `reconcile_cloud_jobs.py` MKUE-04 clean-before-flip block | :174-219 | handler 145-219, at-cap block 178-219 | ✅ OK |
| `reconcile_cloud_jobs.py` function-local `resolve_backends` import | — | 349 | ✅ OK |
| `reconcile_cloud_jobs.py` `FileState` import | :45 | 45 | ✅ OK |
| `pipeline.py` `get_awaiting_cloud_count` | :1116 | 1116 | ✅ OK |
| `pipeline.py` `get_cloud_staging_candidates` | "~1300" (D-08) | 1269 | ⚠️ minor (CONTEXT self-inconsistent; correct is 1269) |
| `pipeline.py` `get_metadata_pending_files` | :1382 | 1382 | ✅ OK |
| `pipeline.py` `get_fingerprint_pending_files` | :1415 | 1415 | ✅ OK |
| `pipeline.py` `count_inflight_jobs` / `get_live_job_keys` | — | 1518 / 514 | ✅ OK |
| `shadow_compare.py` `_cloud_job_exists` | :68 | 68 | ✅ OK |
| `shadow_compare.py` `_cloud_awaiting` | **:82** | **80** | ⚠️ **DRIFT** (2 lines) |
| `shadow_compare.py` `awaiting_cloud` / `pushing` / `pushed` invariants | :131 / :133 / :134 | 131 / 133 / 134 | ✅ OK |
| `models/analysis.py` NAND CheckConstraint | :56 | 56 | ✅ OK |
| `models/scheduling_ledger.py` `enqueued_at` | :63 | 63 | ✅ OK |
| `models/cloud_job.py` `CloudJobStatus` (no `pushed` member) | — | 30-50: uploading/uploaded/failed/submitted/running/succeeded/awaiting | ✅ OK (no `pushed`) |
| `routers/pipeline.py` `retry_analysis_failed` clears `failed_at` | :956 | 956 (def at 891) | ✅ OK |
| `routers/pipeline.py` `retry_metadata_failed` leaves `failed_at` | :974 | 974 | ✅ OK |
| Canonical ref: `tests/test_recovery.py` | root | **`tests/analyze/tasks/test_recovery.py`** | ⚠️ **DRIFT** (no root-level tests allowed) |
| Equivalence SCOPE comment / `*_inflight` seed exclusion | :415-427 | 415-427 (`DOMAIN_COMPLETED_CASES` at 427; file is 513 lines) | ✅ OK |

**Verdict:** the design is sound; three drifts to correct in plans — (1) PROV-01 site is `:382` not `:374`; (2) `_cloud_awaiting` is `:80`; (3) the recovery test lives at `tests/analyze/tasks/test_recovery.py`.

`backends.py:86` `hold_awaiting_cloud` **exact signature** (D-12 caller must match):
```python
async def hold_awaiting_cloud(
    session: AsyncSession,
    file: FileRecord,
    *,
    attempts: int = 0,
    expect_status: Sequence[str] | None = None,   # None => hold mode; non-empty => spill-mode CAS
    clear_cloud_phase: bool = False,
) -> bool:
```
D-12's call passes `attempts=cap`, `expect_status=(SUBMITTED.value, RUNNING.value)`, `clear_cloud_phase=True`. Spill mode does **not** write `file.state` and does **not** touch `FileRecord` — it is a rowcount-guarded `UPDATE cloud_job … WHERE file_id=… AND status IN expect_status` returning `rowcount>0`. `inadmissible=False` / `staging_bucket=None` are **not** stamped by the helper (D-12: keep them inline).

`_enqueue_push_file` (`backends.py:154`) is the **only** producer of `push_file` ledger rows; `KueueBackend` uses `_stage_file_to_s3` and never enqueues `push_file` — so a `push_file` ledger row implies compute (D-07 verified).

---

## Resolved Discretion Items

### (a) `AnalysisResult.updated_at` source — RESOLVED: valid, use `updated_at`

`models/analysis.py:13` → `class AnalysisResult(TimestampMixin, Base)`. `scheduling_ledger.py:38` documents that `TimestampMixin` provides `updated_at`. So `a.updated_at` in D-13's SQL resolves.

**Caveat the plan should record:** neither `updated_at` nor `created_at` is the *true* analyze-completion time (that datum was never persisted; the backfill exists precisely because of that gap). For an `analyzed` file the `analysis` row is written once at completion, so `updated_at ≈ created_at ≈ completion`. Because `done_clause(ANALYZE)` tests only `analysis_completed_at IS NOT NULL` (`stage_status.py:123`), the *value* is immaterial to recovery correctness — only NULL-ness matters. Recommend `updated_at` per D-13 (mirrors `032`).

### (b) "Zero `FileRecord.state` reads" guard — RESOLVED: copy the Phase-84 AST scanner

**Existing idioms found:**
- `tests/shared/test_dedup_fingerprint_source_scan.py` (Phase 84, READ-04) — the **mutation-proven** AST scanner. Its module docstring explicitly enumerates the two Phase-83 blind spots it fixed.
- `tests/analyze/services/test_single_awaiting_writer.py` (Phase 83, D-02) — AST scan for `.values(status=AWAITING)` writes, walks dict-literal + subscript-mutation binds.

**Why Phase 83's *earlier* guards were toothless** (from the Phase-84 docstring + project memory `feedback_mutation_test_guard_tests`):
1. A line-oriented `grep` false-negatived because SQLAlchemy splits a `.values(...)` / attribute chain across physical lines — the token the grep keyed on never appeared on one line.
2. An AST rule keyed on `keyword.arg` (or on a chained `.where(<Compare>)`) was **blind to positional `.where(a, b, c)` reads** — the nine real read-sites passed the clause positionally, and to `.where(**splat)` where `keyword.arg is None`.

**Recommended guard for Phase 80** — model it on `test_dedup_fingerprint_source_scan.py`, but note both target files end the phase with **clean absence** of any `FileRecord.state`/`FileState` reference (reenqueue: remove the reads at 187/197/209 + drop the `FileState` import at 74; reconcile: remove the write at 212 + drop the `FileRecord, FileState` import at 45). Clean absence means **no allow-list is needed** — the guard is simply "zero occurrences," which is strictly stronger and cannot false-positive.

The guard must flag every syntactic form a drifter could reach for. Verified the current code uses forms (1)+(2); enumerate all for mutation tests:

| # | Syntactic form (mutation) | Present in code today | Guard mechanism |
|---|---|---|---|
| 1 | `select(FileRecord.id).where(FileRecord.state.in_([...]))` — attribute + `.in_()` method call | reenqueue 187, 197 | `ast.Attribute` with `.attr=="state"` whose `.value` is `Name("FileRecord")`, anywhere |
| 2 | `.where(FileRecord.state == FileState.X)` — `ast.Compare` | reenqueue 209 | same attribute scan; also `FileState.<member>` occurrence scan |
| 3 | `update(FileRecord).where(...).values(state=FileState.X)` — the **removed write** | reconcile 212 | `FileState.<member>` occurrence anywhere (clean-absence) |
| 4 | `file.state` — read off an instance variable | (none after cutover) | `ast.Attribute` with `.attr=="state"` in a non-assignment-target position |
| 5 | `getattr(file, "state")` | (none) | `ast.Call` to `getattr` with a `Constant("state")` arg |
| 6 | `FileRecord.state` passed **positionally** into `.where(a, b, state_read)` | (none) | walk `Call.args` **and** `Call.keywords` for the two `.where`-family funcs (the Phase-83 blind spot the Phase-84 scanner already closes) |

Simplest robust shape (clean-absence): walk the AST of `reenqueue.py` and `reconcile_cloud_jobs.py`; assert **zero** `ast.Attribute` nodes with `.attr == "state"` whose base resolves to `FileRecord` (or any local bound to a `FileRecord`), **zero** `FileState.<member>` attribute accesses, and **zero** `getattr(_, "state")` calls. Encode mutation directions #1–#6 as crafted-string tests (never touch the real files — DB-free, hermetic), each asserting the mutated source is flagged, plus one GREEN false-positive check confirming a legitimate `cloud_job.status` read (`.attr=="status"`, not `"state"`) is **not** flagged.

**Mutation-test discipline (project rule):** for each of #1–#6, break the real file to reintroduce that form, watch the guard go RED, restore. A guard that has never been seen RED is worthless.

### (c) Test-bucket placement — RESOLVED

Buckets are **directory-based** (`tests/buckets.json` = discovery, metadata, fingerprint, analyze, identify, review, agents, integration, shared). `tests/shared/test_partition_guard.py` fails CI if any test file escapes a bucket dir (a root-level `tests/test_recovery.py` would be an **offender** — hence the canonical-ref drift).

| Test file | Bucket | Invocation |
|---|---|---|
| `tests/analyze/tasks/test_recovery.py` (SC-2 never-scheduled + SC-3 analyze-terminal + D-10 both-cells) | analyze | `just test-bucket analyze` |
| `tests/analyze/core/test_reenqueue.py` | analyze | `just test-bucket analyze` |
| `tests/analyze/tasks/test_reconcile_cloud_jobs.py` (D-12 spill swap + zero-state-read guard) | analyze | `just test-bucket analyze` |
| `tests/integration/test_stage_status_equivalence.py` (D-11 SCOPE amendment) | integration | `just test-bucket integration` |
| new `tests/integration/test_migrations/test_migration_036_*.py` | integration | see below |
| the two AST source-guard files | shared or analyze (hermetic, no DB) | `just test-bucket <bucket>` — placement is discretionary; `shared` matches the Phase-84 guard's home |

**5433 footgun (mandatory env exports).** `tests/integration/test_migrations/conftest.py:37` defaults `MIGRATIONS_TEST_DATABASE_URL` to `…@localhost:5432/phaze_migrations_test`, but `just test-db` provisions Postgres on **5433** and `just test-bucket` does **not** export the URL. Run migration/integration tests as:
```bash
just test-db   # provisions 5433 / redis 6380
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test"
export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test"
export PHAZE_REDIS_URL="redis://localhost:6380/0"
just test-bucket integration
```
Without both DB URLs the migration harness silently talks to 5432 and fails like an infra flake (the `test_migration_034_*` docstring documents this exact trap).

### (d) Ledger-scoped done-set helper shape — RECOMMEND: one query per stage (not `stage_status_case`)

Today `_build_done_sets` (reenqueue 136-165) builds **four separate queries**: `_ANALYZE_DONE`/`_PUSH_DONE` (state reads) and `_METADATA_PENDING`/`_FINGERPRINT_PENDING` (pipeline pending fns). D-05/D-06 rewrites these to derive `done` directly.

**Recommendation: keep the per-stage query shape** (3–4 targeted queries, each `WHERE id IN fids AND <clause>`), not a single `stage_status_case` bucketed in Python. Rationale:
- The Phase-77 **partial indexes** are `IS NOT NULL`/status-shaped (`ix_analysis_completed` on `analysis_completed_at IS NOT NULL`; `ix_analysis_failed`; `ix_metadata_failed`; `ix_fprint_success`; `ix_cloud_job_awaiting`). A per-stage `domain_completed_clause(stage)` / `done_clause(stage)` filter lets each of these indexes drive its own query.
- `stage_status_case` (`stage_status.py:222`) emits a CASE that evaluates **multiple** correlated `exists()` subqueries per row for one stage; used across all stages it forces broad predicate evaluation and cannot selectively hit the narrow partials.
- Per-stage reuses the LOCKED clause builders **verbatim** (DERIV-04 lock), which is the whole point of the derivation layer.

Concrete: `done[analyze] = SELECT id WHERE id IN fids AND domain_completed_clause(ANALYZE)`; `done[metadata] = … domain_completed_clause(METADATA)` **then apply the D-10 `enqueued_at` gate at the call site** (`is_domain_completed`), not in SQL (both twins stay ledger-agnostic); `done[fingerprint] = … done_clause(FINGERPRINT)`; `push_done = … (cloud_job.status='succeeded' OR domain_completed_clause(ANALYZE))` per D-07. That is 3–4 queries, all ledger-scoped, all index-eligible. The renames of `_ANALYZE_DONE`/`_PUSH_DONE`/`_METADATA_PENDING`/`_FINGERPRINT_PENDING` to done-semantics constants are cosmetic and discretionary; renaming `_METADATA_PENDING`→`_METADATA_DONE` (etc.) is recommended so the constant name stops lying after the inversion.

### (e) Bind-parameter chunking for `id IN fids` — RECOMMEND: single PG array bind (`= ANY`)

- **Realistic ledger size:** normally small (a row per scheduled-but-not-cleared job — hundreds to low thousands in steady state). BUT the 2026-06-18 incident proves the ledger can transiently reach **~44.5K rows**. So a naive `.in_(fids)` is unsafe at the tail.
- **asyncpg limit:** the PostgreSQL wire protocol caps bind parameters at **32767** per statement (Int16). `.in_(fids)` expands to one bind per element, so >32767 fids raises at execute time.
- **Existing idiom in the codebase:** there is **no** `= ANY(array)` idiom anywhere (`grep` found none); every filter uses `.in_(...)`. There *is* a size-based chunk idiom for a different path — `pipeline.py:1472` sorts file-id strings then chunks into `batch_size` groups (proposal generation).

**Recommendation:** bind the fids as a **single Postgres array** to sidestep the param-count limit entirely:
```python
from sqlalchemy.dialects.postgresql import UUID as PGUUID
# one bind param (the array), no 32767 ceiling, index still usable:
stmt = select(FileRecord.id).where(FileRecord.id == sa.func.any(sa.bindparam("ids", value=list(fids), type_=ARRAY(PGUUID(as_uuid=True)))))
```
This is cleaner and faster than chunk-and-union. If the team prefers a project-native pattern over introducing `= ANY`, fall back to chunking `fids` into batches of ≤10 000 and union the id-sets in Python (mirrors the `batch_size` chunk at `pipeline.py:1472`). Either is acceptable; the array bind is the primary recommendation. **Do not** ship a bare `.in_(fids)` without one of these — it is a latent crash at the incident-scale tail.

---

## Migration 036

**Chain tip & next free number — CONFIRMED.** `alembic/versions/` tops out at `035_reconcile_dedup_resolution.py` (`revision="035"`, `down_revision="034"`). `036` is genuinely free.

**Column already exists.** `analysis.analysis_completed_at` was added in `028_add_analysis_completed_at.py` (and is ORM-mapped at `models/analysis.py:38`). So `036` is **data-only** and its `alembic revision --autogenerate` diff against the `036` head must be **EMPTY** (assert this, mirroring `034`'s empty-diff test).

**Prod-at-`031` ordering — SAFE.** Project memory confirms prod is at Alembic `031`; `032`–`035` are unreleased. On first deploy the sweep runs `032→033→034→035→036` in order. `036` runs **after** `032`. `032` sets `analysis.failed_at` only for `analysis_failed` files (`_BACKFILL_ANALYZE_FAILED`) and never touches `analysis_completed_at`. So when `036` runs `WHERE f.state='analyzed' AND a.failed_at IS NULL AND a.analysis_completed_at IS NULL`, the `analyzed` corpus is untouched by `032` and correctly selected. No re-ordering hazard.

**NAND-constraint validity — CONFIRMED.** `models/analysis.py:56`: `CheckConstraint("NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)", name="analysis_completed_xor_failed")`. This is a **NAND**, not an XOR — both-NULL is legal. Setting `analysis_completed_at` on a row that has `failed_at` set would violate it. The `failed_at IS NULL` guard in D-13's SQL is therefore **mandatory**; without it the migration aborts on any `analysis_failed` row. D-13's SQL is valid under the constraint.

**D-13 SQL (verified valid):**
```sql
UPDATE analysis a SET analysis_completed_at = a.updated_at
FROM files f
WHERE a.file_id = f.id
  AND f.state = 'analyzed'
  AND a.analysis_completed_at IS NULL
  AND a.failed_at IS NULL;
```

**Test harness pattern to follow.** Mirror `tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py`:
- **DB-free asserts:** static `revision`/`down_revision` id check; the `saq_jobs`-banner assertion (migrations must NEVER reference `saq_jobs`); empty-autogenerate-diff scope (`036` touches no ORM schema).
- **Integration body:** seed a small corpus at `035`, upgrade `035→036`, assert (1) every `state='analyzed'` file with `failed_at IS NULL` + `analysis_completed_at IS NULL` now has `analysis_completed_at` set; (2) a control `analysis_failed` file (has `failed_at`) is UNCHANGED and does not trip the NAND; (3) a control non-`analyzed` file gets nothing; (4) idempotency — re-running the backfill is inert; (5) `files.state` byte-unchanged (read-only on `files`).
- Import `MIGRATIONS_TEST_DATABASE_URL`, `_build_alembic_config`, `downgrade_to`, `upgrade_to` from `tests/integration/test_migrations/conftest.py`. `_MIGRATION_PATH = parents[3] / "alembic/versions/036_*.py"`.

**Downgrade.** `034` and `035` both ship a `downgrade()` (documented-lossy — `034` deletes the awaiting rows). For a pure backfill of a pre-existing column, a reliable reverse is impossible (cannot distinguish backfilled from genuine values). Recommend a **documented no-op `downgrade()`** with a rationale comment ("data-only backfill of a column that predates this migration; nothing to reverse; the pre-existing NULLs are indistinguishable from backfilled values"). This matches the precedent that some downgrades are explicitly lossy/no-op.

**Atomicity (D-13).** `036` ships **inside Phase 80's PR**, alongside the reader cutover, so there is no window where `reenqueue.py` derives `done(analyze)` against an un-backfilled corpus. Mirrors `033` shipping with Phase 81's writers.

---

## Clause Extraction (D-08/D-09)

**The two inline spellings are byte-identical.** Verbatim:

`get_awaiting_cloud_count` (`pipeline.py:1136-1140`):
```python
.where(
    CloudJob.status == CloudJobStatus.AWAITING.value,
    ~inflight_clause(Stage.ANALYZE),
    ~domain_completed_clause(Stage.ANALYZE),
),
```
`get_cloud_staging_candidates` (`pipeline.py:1310-1314`):
```python
.where(
    CloudJob.status == CloudJobStatus.AWAITING.value,
    ~inflight_clause(Stage.ANALYZE),
    ~domain_completed_clause(Stage.ANALYZE),
)
```
Same three conjuncts, same order. **No divergence.**

**No import cycle.** `stage_status.py` today imports **nine** models (analysis, dedup_resolution, execution, file, fingerprint, metadata, proposal, scheduling_ledger, tracklist — lines 66-74; CONTEXT said "seven," minor undercount) plus `enums.stage` and `tasks._shared.stage_control`. It does **not** yet import `cloud_job`. Adding `from phaze.models.cloud_job import CloudJob, CloudJobStatus` is safe: `models/cloud_job.py` imports only SQLAlchemy + `models.base` (no service imports), so there is no cycle. This is exactly why D-09 rejects `services/backends.py` (which has a managed `backends↔reconcile_cloud_jobs` module-top cycle) — `stage_status.py` has no such entanglement, and the clause needs only a status literal (no `backends.toml` config), so 83 D-12's `pushing_clause`/`pushed_clause` rejection does not apply.

**Proposed builder signature** (returns a composed `ColumnElement[bool]`; caller supplies the `CloudJob⋈FileRecord` join, exactly as all three current call sites do):
```python
def awaiting_candidate_clause() -> ColumnElement[bool]:
    """D-08/D-09: a genuinely-parked awaiting cloud candidate (status='awaiting', not analyze-in-flight,
    analyze not domain-completed). Composes the LOCKED inflight_clause/domain_completed_clause verbatim.
    Callers provide the CloudJob-to-FileRecord join so the correlated ~exists(... == FileRecord.id) resolves."""
    return and_(
        CloudJob.status == CloudJobStatus.AWAITING.value,
        ~inflight_clause(Stage.ANALYZE),
        ~domain_completed_clause(Stage.ANALYZE),
    )
```
All three consumers — `get_awaiting_cloud_count`, `get_cloud_staging_candidates`, and reenqueue's new `_get_awaiting_cloud_ids` — replace their inline `.where(...)` conjuncts with `.where(awaiting_candidate_clause())`. Builder name is discretionary (D-08); `awaiting_candidate_clause` reads well beside `inflight_clause`/`domain_completed_clause`.

---

## Doc De-numbering (D-14)

**The `034` collision is real and confirmed.** `034` names two different things in the planning docs:
- Phase 83's **shipped** `034_backfill_cloud_awaiting.py` — historical record at `ROADMAP.md:416` ("83-02-PLAN.md — Corpus-repair migration `034`"). **Keeps its literal number** (81 D-08: dated/historical records stay literal).
- Phase 90's **planned destructive** migration — hardcoded as `034` at `REQUIREMENTS.md:98` (MIG-04), the milestone header (`ROADMAP.md` ~line 21: "destructive `034`"), the Phase 90 title (`### Phase 90: Destructive Migration `034`…`), Phase 90 SC#1/#2, and `PARALLEL-ENRICH-DAG-DESIGN.md`. **These get de-numbered** to "the destructive migration (number assigned at plan time)."

Note Phase 80's own migration is `036`; Phase 90's destructive migration will therefore be `037`+ (whatever is free when Phase 90 plans), **never** `034`. De-numbering ends the churn (81 D-08 `033→034`; 83 predicted `034→035` but never applied; 84 took `035`).

**Planner guidance:** the CONTEXT line numbers (`:21,:36,:535,:542`) are approximate and the doc may have drifted — **grep for the destructive-migration `034` mentions by content**, not by line, and leave `ROADMAP.md:416` (the Phase-83 historical `034`) untouched.

**`just docs-drift` is SAFE and does NOT catch this class.** Verified: `tests/shared/core/test_requirements_traceability.py` cross-checks **requirement-ID ↔ ROADMAP `- [x] Phase NN` checkbox ↔ `{NN}-VERIFICATION.md status: passed ↔ Traceability Status column`** — it is a pure filesystem structural guard with **zero** references to migration numbers, alembic, or `03x` (grep returned nothing). Editing MIG-04's prose to drop the `034` literal does not touch its `- [ ]` checkbox, its `MIG-04` id, or its Traceability Status, so the guard stays green. **Constraint:** the D-14 edits must not alter any requirement checkbox/ID/Status — prose only.

---

## PROV-01 Overlap

**Confirmed: Phase 80 does not touch PROV-01 and does not make it harder to fix.**

- The single-active-compute call is `reenqueue.py:382`: `compute_agent = await select_active_agent(session, kind="compute")` (focus item 6 cited `:374` — that is drift; actual is **382**).
- Phase 80's edits are confined to the **done-set derivation** helpers (`_build_done_sets:136`, `_select_done_analyze_ids:177`, `_select_done_push_ids:190`, `_get_awaiting_cloud_ids:200`, `is_domain_completed:242`) and the pending-import drop (`:79/:81`). None of these is the agent-selection path at 382.
- `recover_orphaned_work` (301) calls `select_active_agent(..., kind="compute")` at 382, `kind="fileserver"` at 399, and the kind-agnostic form at 414 — all in the dispatch/routing tail, untouched by the done-set cutover.

PROV-01 (N-compute-aware orphan recovery) remains a clean, separable v2 change to the routing tail. **Report only — do not fix** (design §9, 2026.7.2 close-out deferral).

---

## Shadow-Compare Impact

**`just shadow-compare`** = `uv run python -m phaze.cli.shadow_compare` (justfile). The gate contract is **implication, not equality** (79 D-04 / 83 D-00d).

**The `awaiting_cloud` HARD invariant (`shadow_compare.py:131`)** asserts `state == AWAITING_CLOUD ⇒ ∃ cloud_job(status='awaiting')` (`_cloud_awaiting`, `soft=False`).

**D-04 makes it MORE green — confirmed, and it fixes a live HARD violation.** On `main` today, reconcile's at-cap spill writes `state=AWAITING_CLOUD` (`:212`) while stamping `cloud_job.status=FAILED` (`:208`). That satisfies the invariant's LHS but violates its RHS → a **current HARD divergence** (CONTEXT §specifics #1, verified). After D-04+D-12:
- The `state=AWAITING_CLOUD` write is gone → the spilled kueue file stays at `state=PUSHED` (from `agent_s3.py:128`), so it no longer trips the `awaiting_cloud` LHS at all → **fewer LHS-true rows → strictly more green**.
- D-12's spill-mode CAS flips `cloud_job.status` from `SUBMITTED/RUNNING` to `'awaiting'` (not `FAILED`), so any downstream `_cloud_awaiting` check is satisfied where it matters.

**`pushed` invariant (`:134`)** = `_cloud_job_exists` — **any** `cloud_job` row regardless of status (`shadow_compare.py:68`, loosened per RESEARCH A3/OQ1). Verified: `exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id))`. So a spilled file at `state=PUSHED` carrying an `awaiting` row satisfies `pushed`. Gate stays green. (Same for `pushing` at `:133`.)

**`analyzed` invariant (`:126`)** = `done_clause(Stage.ANALYZE)`, `soft=False` → requires `analysis_completed_at IS NOT NULL`. **This will be RED on first deploy for ~1001 prod files** until `036` runs (folded todo + project memory, verified). **Sequencing rule for the plan:** migration `036` MUST run before any live-corpus shadow-compare assertion (SC-4 / the deferred 79 D-02 run). Within Phase 80's PR the migration and the cutover land atomically, so the phase's own test-DB shadow-compare (seeded corpus) is green; the *prod* first-deploy gate depends on `036` having run. Do not gate CI on a live-corpus run that predates `036`.

---

## Validation Architecture

Nyquist validation is enabled (`.planning/config.json` has no `workflow.nyquist_validation: false`). Test framework: **pytest + pytest-asyncio**, `uv run pytest` only (never bare `pytest`). Directory-bucketed; per-bucket coverage combined to a 95% floor (`coverage-combine`, `--fail-under=95`); per-module floor via `scripts/coverage_floor.py` (90% project rule).

**Quick run (per commit):** `just test-bucket analyze` (recovery/reenqueue/reconcile).
**Full gate (per wave / phase):** `just integration-test` (spins ephemeral 5433/6380, exports both DB URLs, runs `tests/`).
**Phase gate:** full suite green + `036`-seeded shadow-compare green before `/gsd:verify-work`.

### The 4 ROADMAP success criteria → validation layers + required RED mutation

| SC | Behavior | Layer | Test file / command | **Mutation that MUST turn it RED** |
|----|----------|-------|---------------------|-----------------------------------|
| **SC-1** | `recover_orphaned_work` + `reconcile_cloud_jobs` derive done/in-flight with **zero `FileRecord.state` reads**; at-cap spill writes the sidecar, not `FileRecord.state`; MKUE-04 clean-before-flip preserved under the advisory lock | **source-assertion** (AST) + **integration** (behavioral) | AST guard in `tests/shared/` (new) + `tests/analyze/tasks/test_reconcile_cloud_jobs.py` · `just test-bucket analyze` | (a) reintroduce `FileRecord.state.in_([...])` in `reenqueue.py` in each form #1–#6 → AST guard RED; (b) re-add `update(FileRecord)…values(state=…)` at reconcile:212 → guard RED + spill test asserts `cloud_job.status='awaiting'` (not FAILED) RED; (c) move the `delete_staged_object` to *after* `session.commit()` → Pitfall-9 ordering test RED |
| **SC-2** | Scheduling-ledger recovery contract + "only previously-scheduled work recovers" preserved; a never-scheduled `discovered` file (no ledger row) is NOT recovered | **integration** | `tests/analyze/tasks/test_recovery.py` (beside the `_DOMAIN_COMPLETED_STAGES` totality test) · `just test-bucket analyze` | Seed a `discovered` file with **no** ledger row; assert `recover_orphaned_work` does not enqueue it. Mutation: make recovery iterate the corpus instead of `get_ledger_rows` → test RED (re-creates the 44.5K over-enqueue class) |
| **SC-3** | A failed **analyze** is never produced by any automatic recovery path — `FAILURE_IS_TERMINAL[analyze]` encoded at the **recovery** layer, not just derivation | **integration** | `tests/analyze/tasks/test_recovery.py` · `just test-bucket analyze` | Seed an `analysis_failed` file with a surviving `process_file` ledger row; assert it is treated domain-complete and NOT re-enqueued. Mutation: drop the `failed_clause` disjunct from `domain_completed_clause(ANALYZE)` (or bypass it in `is_domain_completed`) → test RED |
| **SC-4** | Shadow-compare (Phase 79) stays green after cutover | **integration + migration** | `036`-seeded corpus → `just shadow-compare`; migration test `tests/integration/test_migrations/test_migration_036_*.py` · `just test-bucket integration` (with 5433 exports) | Skip/disable `036` → seeded `analyzed` rows have NULL `analysis_completed_at` → `analyzed` HARD invariant RED. Also: re-add the `state=AWAITING_CLOUD` spill write → `awaiting_cloud` invariant RED |

### D-10 both-metadata-cells test (WR-02) — REQUIRED

`tests/analyze/tasks/test_recovery.py`, exercising `is_domain_completed` with `SchedulingLedger.enqueued_at` vs `metadata.failed_at`:
- **Cell A — orphaned operator retry** (`enqueued_at > failed_at`): metadata is NOT domain-complete → file MUST re-drive. Mutation: flip the comparison to `>=`/`<` → RED.
- **Cell B — callback-partial-failure** (`enqueued_at < failed_at`): metadata IS domain-complete → file MUST stay terminal. Mutation: drop the `enqueued_at <= failed_at` gate (revert to bare `done ∨ failed`) → Cell A goes RED (the fix's own revert proves non-vacuity).

Analyze has **no** D-10 cell because `retry_analysis_failed` clears `failed_at` (`routers/pipeline.py:956`); `retry_metadata_failed` deliberately leaves it (`:994`) — assert this asymmetry so a future symmetric-retry change trips the test.

### D-11 trap regression — REQUIRED

Guard that `~inflight_clause(stage)` is **never** added to `domain_completed_clause`. Two layers:
1. `tests/analyze/tasks/test_recovery.py`: a `reenqueue` regression proving **both** metadata cells resolve correctly (above) — since *every* recovery candidate is a ledger row, adding the `~inflight` disjunct would make `domain_completed` return False for all of them (disabling the secondary over-enqueue net), so Cell B (terminal) would go RED.
2. `tests/integration/test_stage_status_equivalence.py:415-427`: extend the existing SCOPE comment (already documents the ledger-agnostic design + `*_inflight` seed exclusion) with the D-11 rejected-option rationale; add the same note to `domain_completed_clause`'s docstring. Mutation: add `~inflight_clause` to `domain_completed_clause` → the DERIV-04 equivalence test stays green (the trap is a silent no-op for drain/card) **but** the SC-2/D-10 recovery regressions go RED — which is exactly why the recovery-layer test is the real lock, not the equivalence test.

**Wave-0 gap:** no test files need creating from scratch — `test_recovery.py`, `test_reenqueue.py`, `test_reconcile_cloud_jobs.py`, `test_stage_status_equivalence.py` all exist. New additions: the two AST source-guard test files, the `036` migration test, and the SC-2/SC-3/D-10/D-11 cases appended to `test_recovery.py`.

---

## Landmines

1. **`select_active_agent(kind="compute")` is at `reenqueue.py:382`, not `:374`.** A plan that edits "around 374" will edit the wrong region. PROV-01's site is 382.
2. **The recovery test is `tests/analyze/tasks/test_recovery.py`, not root `tests/test_recovery.py`.** The partition guard (`tests/shared/test_partition_guard.py`) fails CI on any root-level test. CONTEXT's canonical-refs and §specifics both say the wrong path.
3. **D-12 autoflush race.** Do NOT pre-mutate `cloud_job.status` on the loaded ORM object before calling `hold_awaiting_cloud(expect_status=(SUBMITTED,RUNNING))` — autoflush would write the new status and the CAS `WHERE status IN (...)` would then miss its own row. Today's code sets `cloud_job.status = FAILED` at `reconcile:208`; that line must be **removed** (the CAS owns the status write), and the remaining inline clears (`inadmissible=False`, `staging_bucket=None`) must be applied *after* the CAS or without touching `status`.
4. **`hold_awaiting_cloud` spill mode sets status to `'awaiting'`, not `FAILED`.** The current at-cap path stamps `FAILED` (which decrements in-flight). `'awaiting'` is deliberately **out** of `IN_FLIGHT`, so it also does not inflate in-flight counts — behavior is preserved, but a plan author expecting the old `FAILED` terminal must understand the semantics changed to a re-drivable park (correct per D-12).
5. **Never ship `.in_(fids)` unchunked.** The ledger reached ~44.5K rows in the 2026-06-18 incident; >32767 binds crash asyncpg. Use the `= ANY(array)` bind (discretion e).
6. **`036` must carry the `failed_at IS NULL` guard.** The `033` constraint is a NAND — omitting the guard aborts the migration on every `analysis_failed` row. (CONTEXT is correct; flagged because it is the single most likely silent break.)
7. **Do not delete the three `reenqueue.py` module-docstring reframes** (Phase-42 durability, Phase-45 ledger, domain-completed contract). Update in place — they are the institutional record of two production incidents (§specifics).
8. **Dropping the `FileState` import from `reconcile_cloud_jobs.py:45`** (and `FileRecord`, now unused after `:212` is removed) is a *consequence* of D-04, not scope creep — but ruff `F401` will fail the commit if the import is left dangling. Verified: after removing `:212`, `FileRecord`/`FileState` have no other use in that file (only the docstring mentions them).
9. **The `034` de-numbering must leave `ROADMAP.md:416` (Phase-83 historical `034`) literal.** Only the Phase-90 *destructive* `034` references get de-numbered. Grep by content, not line number.

---

## Open Questions (RESOLVED at plan time)

1. **`stage_control.STAGE_TO_FUNCTION` / `_shared` import from `reenqueue.py`?** The ledger-scoped done queries reuse `domain_completed_clause`/`done_clause` from `services/stage_status.py`, which reenqueue can import (it already imports from `services`). No blocker identified, but the plan should confirm `reenqueue.py` importing `services.stage_status` does not trip `tests/test_task_split.py` (the control-only import boundary — `reenqueue.py` must never be importable from the agent worker). `stage_status.py` imports only models + enums + `tasks._shared.stage_control`; it is control-plane-safe, so the import should be clean. **Resolvable by:** running `just test-bucket shared` (or the specific `test_task_split.py`) after wiring the import. Low risk.
2. **Exact `036` downgrade policy.** Recommended documented no-op; if the team wants a reversible downgrade, the only faithful option is to archive the pre-backfill NULL set inside the migration — out of proportion for a data-only backfill. **Resolvable at plan time** by the migration author choosing no-op (recommended) vs. archive.
3. **Whether `push_done` is one query or folded into the analyze query.** D-07's `push_done = cloud_job.status='succeeded' OR domain_completed_clause(ANALYZE)` spans two tables; it can be one query with a `join`/`or_`, or the analyze-done set reused + a separate `succeeded` set unioned in Python. Both are correct; a micro-optimization left to the implementer (discretion d).

---

## RESEARCH COMPLETE

- **Design is sound; three citation drifts to fix in plans:** PROV-01 site is `reenqueue.py:382` (not 374); `shadow_compare._cloud_awaiting` is `:80` (not 82); the recovery test lives at `tests/analyze/tasks/test_recovery.py` (not root — the partition guard forbids root tests).
- **The guard idiom is solved:** copy `tests/shared/test_dedup_fingerprint_source_scan.py` (Phase-84, mutation-proven, already closes both Phase-83 blind spots); both target files end at clean absence, so no allow-list is needed — enumerate mutation forms #1–#6 and watch each go RED.
- **Migration `036` verified end-to-end:** next free number, data-only (column exists since `028`), NAND guard mandatory, `032→036` first-deploy ordering safe, mirror `test_migration_034_*` for the harness; sequence `036` before any live-corpus shadow-compare (the 1001-row `analyzed` RED).
- **D-04+D-12 fix a HARD shadow violation that exists on `main` today** (reconcile writing `state=AWAITING_CLOUD` alongside `cloud_job.status=FAILED`); retiring the write is strictly more green — but mind the autoflush race (Landmine 3) and the `= ANY(array)` bind (Landmine 5).
- **Discretion resolved:** `updated_at` (valid via TimestampMixin); per-stage index-friendly done queries; `= ANY(array)` binds; extracted `awaiting_candidate_clause()` in `stage_status.py` (no cycle — `CloudJob` import is clean); buckets = `just test-bucket analyze` + `just test-bucket integration` with the mandatory 5433 env exports.
