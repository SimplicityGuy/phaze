# Phase 80: Recovery / Re-enqueue Cutover - Pattern Map

**Mapped:** 2026-07-10
**Files analyzed:** 9 (6 modified, 3 created)
**Analogs found:** 8 / 9 (1 file — the `= ANY(array)` bind — has NO in-repo analog; see §No Analog Found)

> Every analog below was read at HEAD `32623a59` and excerpted verbatim. Line numbers are load-bearing.
> Three CONTEXT citations are DRIFTED — corrected here so no plan repeats them:
> 1. Recovery test is `tests/analyze/tasks/test_recovery.py`, **NOT** root `tests/test_recovery.py` (partition guard fails CI on root tests).
> 2. `select_active_agent(kind="compute")` (PROV-01, do NOT touch) is at `reenqueue.py:382`, not `:374`.
> 3. `shadow_compare._cloud_awaiting` is at `:80`, not `:82`.
> 4. `get_cloud_staging_candidates` def is at `pipeline.py:1269` (`.where` at `:1310-1314`); "~1300" is approximate.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/tasks/reenqueue.py` *(mod)* | task (control-only) | event-driven / batch | `_in_flight_cloud_job_ids` **in the same file** (`:212`) | exact (in-file template) |
| `src/phaze/tasks/reconcile_cloud_jobs.py` *(mod)* | task (control-only) | event-driven | `routers/agent_s3.py:212` + `agent_push.py:285` spill callers | exact (same helper, same CAS) |
| `src/phaze/services/stage_status.py` *(mod)* | service (predicate builders) | transform (SQL clause) | `domain_completed_clause:195` **in the same file** | exact (sibling builder) |
| `src/phaze/services/pipeline.py` *(mod)* | service (query layer) | CRUD read | `get_awaiting_cloud_count:1116` ⇄ `get_cloud_staging_candidates:1269` (byte-identical inline) | exact |
| `tests/analyze/tasks/test_recovery.py` *(mod)* | test (unit/integration) | request-response | totality test `test_every_keyed_function_is_predicate_covered_xor_live_keys_only:705` **in the same file** | exact |
| `tests/integration/test_stage_status_equivalence.py` *(mod)* | test (equivalence) | transform | its own SCOPE comment `:415-427` | exact (comment amend) |
| `alembic/versions/036_backfill_analysis_completed_at.py` *(new)* | migration | batch (data backfill) | `034_backfill_cloud_awaiting.py` + `032`'s `_BACKFILL_ANALYZE_FAILED:73` | exact |
| `tests/integration/test_migrations/test_migration_036_*.py` *(new)* | test (migration) | batch | `test_migration_034_backfill_cloud_awaiting.py` | exact |
| `tests/shared/test_reenqueue_reconcile_source_scan.py` *(new)* | test (AST source-guard) | transform | `tests/shared/test_dedup_fingerprint_source_scan.py` (Phase 84) | role-match + extension needed |

---

## Pattern Assignments

### `src/phaze/tasks/reenqueue.py` (task, event-driven)

**Analog:** `_in_flight_cloud_job_ids` **at `:212` in the same file** — the ONE helper that is already
sidecar-derived (no `FileRecord.state`). It is the template the other three (`_select_done_analyze_ids:177`,
`_select_done_push_ids:190`, `_get_awaiting_cloud_ids:200`) must become. **`_in_flight_cloud_job_ids`
needs NO change.**

**BEFORE — the three `FileRecord.state` readers to cut over:**
```python
# :187  _select_done_analyze_ids  — STATE READ
return select(FileRecord.id).where(FileRecord.state.in_([FileState.ANALYZED, FileState.ANALYSIS_FAILED]))

# :197  _select_done_push_ids  — STATE READ
return select(FileRecord.id).where(FileRecord.state.in_([FileState.PUSHED, FileState.ANALYZED, FileState.ANALYSIS_FAILED]))

# :209  _get_awaiting_cloud_ids  — STATE READ
return {str(fid) for fid in (await session.scalars(select(FileRecord.id).where(FileRecord.state == FileState.AWAITING_CLOUD))).all()}
```

**TEMPLATE — the correct sidecar-derived shape (`:227`, keep unchanged, copy its shape):**
```python
# _in_flight_cloud_job_ids — reads cloud_job sidecar, NOT FileRecord.state
return {str(fid) for fid in (await session.scalars(select(CloudJob.file_id).where(CloudJob.status.in_([s.value for s in IN_FLIGHT])))).all()}
```

**AFTER — derive via the LOCKED predicate builders (ledger-scoped per D-06):**
- `done[analyze]` → `select(FileRecord.id).where(FileRecord.id == <fids array bind>, domain_completed_clause(Stage.ANALYZE))`
- `done[metadata]` → `... domain_completed_clause(Stage.METADATA)` **then apply the D-10 `enqueued_at` gate at the call site** (`is_domain_completed`), NOT in SQL.
- `done[fingerprint]` → `... done_clause(Stage.FINGERPRINT)` (failed fingerprint auto-retries — done ONLY).
- `push_done` → `cloud_job.status == 'succeeded'  OR  domain_completed_clause(Stage.ANALYZE)` (D-07).

**Import deltas (Landmines 7, 8):**
- DROP `get_metadata_pending_files`, `get_fingerprint_pending_files` from the `pipeline` import block (`:79`, `:81`) — D-05.
- DROP `FileState` from `:74` (and `FileRecord` if it becomes unused after the reads go).
- ADD `from phaze.services.stage_status import domain_completed_clause, done_clause` (control-plane safe — see Open Question 1).
- `is_domain_completed:242` metadata/fingerprint branches flip from `fid not in done_sets[_METADATA_PENDING]` → `fid in done_sets[_METADATA_DONE]` (the double-negation cut, D-05).

**Deviation from analog:** the template reads a single-column status filter with a small bounded set
(`IN_FLIGHT` literals). The new helpers filter by the LOCKED clause builders AND scope to the ledger's
`fids` — so they need the **`= ANY(array)` bind** for `fids` (see §No Analog Found). Do NOT copy the
`.in_(...)` shape for `fids` (crashes >32767 binds).

**Do NOT touch:** the three module-docstring reframes (`:11`, `:20`, `:41`) — update in place, never
delete (Landmine 7). The `select_active_agent(kind="compute")` at `:382` (PROV-01, deferred).

---

### `src/phaze/tasks/reconcile_cloud_jobs.py` (task, event-driven)

**Analog:** the two existing spill-mode `hold_awaiting_cloud` callers — `routers/agent_s3.py:212` and
`routers/agent_push.py:285`. Reconcile becomes the **fourth** caller (D-12).

**BEFORE — the at-cap block to rewrite (`:200-219`, verbatim), inside `_handle_no_callback_terminal`:**
```python
cfg = cast("ControlSettings", get_settings())
old_bucket_id = cloud_job.staging_bucket  # captured pre-mutation
bucket = s3_staging.resolve_bucket_config(cfg, old_bucket_id)
with contextlib.suppress(Exception):
    if bucket is not None:
        await s3_staging.delete_staged_object(file_id, bucket)   # MKUE-04 clean-before-flip — PRESERVE
cloud_job.status = CloudJobStatus.FAILED.value            # ⚠ Landmine 3 — REMOVE (autoflush races the CAS)
cloud_job.inadmissible = False                             # keep inline
cloud_job.cloud_phase = None                               # WR-01 — subsumed by clear_cloud_phase=True
cloud_job.staging_bucket = None                            # keep inline
await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.AWAITING_CLOUD))  # ⚠ D-04 — REMOVE THIS WRITE
await session.commit()                                    # releases the per-row advisory lock — PRESERVE ordering
await kube_staging.delete_job(name, kube)                 # POST-commit — PRESERVE (D-04 status-read-vs-GC)
```

**AFTER — spill-mode CAS via the helper (call-shape from `agent_s3.py:212`):**
```python
# NO pre-mutation of cloud_job.status (Landmine 3). The CAS owns the status write.
old_bucket_id = cloud_job.staging_bucket
bucket = s3_staging.resolve_bucket_config(cfg, old_bucket_id)
with contextlib.suppress(Exception):
    if bucket is not None:
        await s3_staging.delete_staged_object(file_id, bucket)   # STILL under the advisory lock, BEFORE commit
file = <cloud_job.file  OR  SELECT FileRecord WHERE id == file_id>   # helper CAS dereferences file.id
await hold_awaiting_cloud(
    session, file,
    attempts=cap,                                                    # budget-spent marker, NOT an increment
    expect_status=(CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value),
    clear_cloud_phase=True,                                          # WR-01: off the "Running" tile
)
cloud_job.inadmissible = False        # stays inline (helper does not stamp it)
cloud_job.staging_bucket = None       # stays inline (D-12: helper's param set is minimal)
await session.commit()
await kube_staging.delete_job(name, kube)
```

**Helper signature to match (RESEARCH §Citation, `backends.py:86`):**
```python
async def hold_awaiting_cloud(session, file, *, attempts=0, expect_status=None, clear_cloud_phase=False) -> bool:
```
Spill mode (`expect_status` non-empty) is a rowcount-guarded `UPDATE cloud_job … WHERE file_id=… AND
status IN expect_status`; it does NOT write `file.state`, returns `rowcount>0`.

**Import deltas (Landmine 8):** DROP `FileRecord, FileState` from `:45` (unused after the write goes;
ruff `F401` fails the commit otherwise). ADD `hold_awaiting_cloud` to the `services.backends` import —
but NOTE `resolve_backends` is imported **function-locally at `:349`** to break the
`backends ↔ reconcile_cloud_jobs` module-top cycle. `hold_awaiting_cloud` is a plain function in the
same module; confirm it can be imported at module top without re-triggering the cycle, else import it
function-locally too.

**PRESERVE byte-for-byte:** the MKUE-04 clean-before-flip ordering (`delete_staged_object` under the
still-held `pg_advisory_xact_lock(5_000_504)` BEFORE the commit); `cloud_job.attempts` NOT incremented
on the at-cap path; `delete_job` POST-commit.

**Deviation from the agent_s3/agent_push analogs (D-04, the whole point of the phase):** those two
siblings KEEP the gated `update(FileRecord)…values(state=AWAITING_CLOUD)` dual-write behind the CAS
bool (83 D-00c, dies Phase 90). Reconcile **drops it entirely** — no `FileRecord` write of any kind.
This fixes a HARD shadow violation live on `main` today (§Shared Patterns → Shadow gate).

---

### `src/phaze/services/stage_status.py` (service, transform)

**Analog:** `domain_completed_clause` at `:195` **in the same module** — the sibling that composes the
LOCKED `inflight_clause:175` + `done_clause:114`/`failed_clause:145` verbatim.

**Add the D-08/D-09 builder beside them** (RESEARCH's proposed signature — name is discretionary):
```python
def awaiting_candidate_clause() -> ColumnElement[bool]:
    """D-08/D-09: a genuinely-parked awaiting cloud candidate. Composes the LOCKED inflight/domain builders
    verbatim. Callers provide the CloudJob⋈FileRecord join so the correlated ~exists(... == FileRecord.id) resolves."""
    return and_(
        CloudJob.status == CloudJobStatus.AWAITING.value,
        ~inflight_clause(Stage.ANALYZE),
        ~domain_completed_clause(Stage.ANALYZE),
    )
```

**Import delta:** the module imports NINE models at `:66-74` (analysis, dedup_resolution, execution,
file, fingerprint, metadata, proposal, scheduling_ledger, tracklist) — **`cloud_job` is NOT among them.**
ADD `from phaze.models.cloud_job import CloudJob, CloudJobStatus`. **No cycle** — `models/cloud_job.py`
imports only SQLAlchemy + `models.base` (verified RESEARCH §Clause Extraction). This does NOT violate
83 D-12's `pushing_clause`/`pushed_clause` rejection: that needed the `backends.toml` registry; this
needs only a status literal.

**Also (D-11):** add the rejected-option rationale to `domain_completed_clause`'s docstring (`:195-212`):
`~inflight_clause(stage)` MUST NEVER be added here — every recovery candidate is a ledger row by
construction, so the disjunct would make `domain_completed` return `False` for every candidate and
disable the secondary over-enqueue net (the 44.5K incident class), while staying a silent no-op for the
drain/card (whose tests stay green).

**Deviation from analog:** `domain_completed_clause` takes a `stage` arg and raises `ValueError` for
non-enrich stages (it is in the `Stage` dispatch ladder, drift-locked by the equivalence test). The new
`awaiting_candidate_clause()` takes NO arg and is NOT a `Stage`-dispatch builder — like
`dedup_resolved_clause():90`, it must stay OUT of the equivalence test's ladder (that test raises on
unknown stages). Model its "out-of-ladder" placement on `dedup_resolved_clause`, not on
`domain_completed_clause`.

---

### `src/phaze/services/pipeline.py` (service, CRUD read)

**Analog:** the TWO byte-identical inline spellings — `get_awaiting_cloud_count:1116` (`.where` at
`:1136-1140`) and `get_cloud_staging_candidates:1269` (`.where` at `:1310-1314`). RESEARCH confirmed
same three conjuncts, same order, no divergence.

**BEFORE (both sites, verbatim):**
```python
.where(
    CloudJob.status == CloudJobStatus.AWAITING.value,
    ~inflight_clause(Stage.ANALYZE),
    ~domain_completed_clause(Stage.ANALYZE),
)
```

**AFTER (both sites):**
```python
.where(awaiting_candidate_clause())
```

**Deviation:** `get_cloud_staging_candidates` chains `.with_for_update(of=CloudJob, skip_locked=True)`
and `.order_by(FileRecord.created_at.asc())` AFTER the `.where` — leave those intact, only the `.where`
conjuncts collapse to the builder call. `get_awaiting_cloud_count` wraps in `_safe_count(...)` — leave
that intact. Both keep their INNER `join(FileRecord, ...)` so the correlated `~exists` resolves.

**Import delta:** ADD `awaiting_candidate_clause` to the existing `services.stage_status` import in
`pipeline.py`. Recovery's new `_get_awaiting_cloud_ids` becomes the THIRD consumer of the builder.

---

### `tests/analyze/tasks/test_recovery.py` (test) — CORRECTED PATH (Landmine 2)

**Analog:** the totality tests **in the same file** — `test_every_keyed_function_is_predicate_covered_xor_live_keys_only:705`
(parametrized over `sorted(_KEY_BUILDERS)`, asserts `covered != live_keys_only`) and
`test_domain_completed_stages_are_exactly_the_four_agent_stages:717`. SC-2/SC-3 belong beside these.

**Reuse the existing fixtures/seed helpers — do NOT reinvent seeds:**
```python
_make_file(*, file_type="mp3", state=FileState.DISCOVERED) -> FileRecord   # :94  fully-populated seed
_agent_payload(function, file_id) -> dict                                  # :109 stored ledger payload
_seed_ledger(session, ...)                                                 # :119 ledger row seeder
_make_ctx(async_engine, router, controller_queue)                          # :88  controller-shaped ctx
_patch_inflight(monkeypatch, value)   /  _patch_live_keys(monkeypatch, keys)  # :70 / :79  stubs
DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent                      # from tests._queue_fakes
```
Existing exemplars to copy: `test_never_scheduled_files_are_left_alone:141` (SC-2 headline pattern),
`test_analyze_done_row_is_excluded:343`, `test_awaiting_cloud_file_stays_pending_in_recovery:569`,
`test_single_owner_in_flight_cloud_job_skips_ledger_recovery:1056`.

**New cases to append (RESEARCH §Validation, all must go RED under a stated mutation):**
- **SC-2** — a `discovered` file with NO ledger row is NOT enqueued. Mutation: iterate corpus instead of `get_ledger_rows` → RED.
- **SC-3** — an `analysis_failed` file WITH a surviving `process_file` ledger row is domain-complete and NOT re-enqueued (`FAILURE_IS_TERMINAL[analyze]` at the recovery layer). Mutation: drop the `failed_clause` disjunct from `domain_completed_clause(ANALYZE)` → RED.
- **D-10 Cell A** (`enqueued_at > failed_at`, orphaned operator retry) → metadata NOT domain-complete → re-drives.
- **D-10 Cell B** (`enqueued_at < failed_at`, callback-partial-failure) → metadata IS domain-complete → stays terminal. Reverting the `enqueued_at <= failed_at` gate makes Cell A RED (proves non-vacuity).
- **D-11** — both metadata cells resolve correctly; adding `~inflight_clause` to `domain_completed_clause` makes Cell B (terminal) go RED.

Note `is_domain_completed` will gain a `SchedulingLedger.enqueued_at` vs `metadata.failed_at` comparison
at the call site (D-10) — the test imports `SchedulingLedger` (already at `:43`) and `AnalysisResult`/
`CloudJob`/`CloudJobStatus` (already at `:40-41`).

---

### `tests/integration/test_stage_status_equivalence.py` (test) — comment amend only

**Analog:** its own SCOPE comment + `*_inflight` seed exclusion at `:415-427`.

**Deviation:** amend ONLY the prose — add the D-11 rejected-option rationale (why `~inflight_clause` must
never enter `domain_completed_clause`). Keep the `*_inflight` seed exclusion. Do NOT change the
`DOMAIN_COMPLETED_CASES` structure. The DERIV-04 equivalence test stays green even under the D-11 trap
(it is a silent no-op for drain/card) — which is WHY the recovery-layer regression, not this test, is
the real lock.

---

### `alembic/versions/036_backfill_analysis_completed_at.py` (migration, batch) — NEW

**Analog:** `034_backfill_cloud_awaiting.py` (structure/contract) + `032`'s `_BACKFILL_ANALYZE_FAILED:73`
(the `files.state`-sourced backfill shape).

**Copy from `034` verbatim:** the module-docstring contract block (SYNC `def upgrade()`, STATIC
parameter-free SQL, touches NO ORM schema so autogenerate stays EMPTY, documented downgrade); the
`revision`/`down_revision`/`branch_labels`/`depends_on` header; the `op.execute(sa.text(_BACKFILL_...))`
body; the `saq_jobs`-never-referenced CRITICAL banner.

**Header:** `revision = "036"`, `down_revision = "034"` — wait: chain tip is `035` (`down_revision="034"`),
so **`036` must set `down_revision = "035"`.** (`034`'s own header shows the pattern: `revision="034"`,
`down_revision="033"`.)

**Backfill SQL (D-13, verified valid against the NAND — Landmine 6):**
```sql
UPDATE analysis a SET analysis_completed_at = a.updated_at
FROM files f
WHERE a.file_id = f.id
  AND f.state = 'analyzed'
  AND a.analysis_completed_at IS NULL
  AND a.failed_at IS NULL;          -- MANDATORY: 033's constraint is a NAND (models/analysis.py:56)
```
`analysis_completed_at` exists since `028`; `AnalysisResult` carries `updated_at` via `TimestampMixin`
(RESEARCH resolved (a)). Column-value is immaterial (`done_clause(ANALYZE)` tests only `IS NOT NULL`).

**Downgrade — precedent for a documented no-op/lossy:** `032`'s downgrade (`:167-185`) explicitly does
NOT reverse its set-based backfills ("no-op, 016 precedent"); `034`'s downgrade is documented-LOSSY.
For `036` (pure backfill of a pre-existing column) a faithful reverse is impossible — ship a
**documented no-op `downgrade()`** ("data-only backfill of a column that predates this migration;
pre-existing NULLs are indistinguishable from backfilled values; nothing to reverse").

**Ships INSIDE Phase 80's PR** (D-13, blocking prerequisite — atomic with the cutover, mirrors `033`
shipping with Phase 81's writers).

---

### `tests/integration/test_migrations/test_migration_036_*.py` (test, batch) — NEW

**Analog:** `test_migration_034_backfill_cloud_awaiting.py` — copy its exact structure.

**DB-free static asserts to copy (`034` test `:84-107`):**
- `test_revision_identifiers_are_bare_numbers` — `revision == "036"`, `down_revision == "035"`.
- `test_migration_never_references_saq_jobs` — banner-aware line scan (`:92-96`).
- `test_backfill_sql_is_static_and_parameter_free` — assert the SQL literals present, no `.format(`/f-string.

**Exact conftest imports + `_MIGRATION_PATH` (`034` test `:44-52`):**
```python
from tests.integration.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, downgrade_to, upgrade_to,
)
_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "036_backfill_analysis_completed_at.py"
```
Plus `_load_migration_036()` via `importlib.util.spec_from_file_location` (name starts with a digit,
`034` test `:74-81`), and `_diffs_touching_036` empty-diff scope (`_O36_TABLES/INDEXES/COLUMNS` all
empty — `036` touches no ORM schema, so the diff must be EMPTY).

**Integration body (adapt `034` test `:156-231` to D-13):** seed a small corpus at `035`, `upgrade_to
"036"`, assert:
1. every `state='analyzed'` + `failed_at IS NULL` + `analysis_completed_at IS NULL` file now has `analysis_completed_at` set;
2. a control `analysis_failed` file (has `failed_at`) is UNCHANGED and does NOT trip the NAND;
3. a control non-`analyzed` file gets nothing;
4. idempotency — re-run the backfill statement, inert;
5. `files.state` byte-unchanged (read-only on `files`);
6. empty autogenerate diff; documented no-op downgrade runs.

**⚠ 5433 footgun (RESEARCH §(c), MANDATORY):** `conftest.py:37` defaults `MIGRATIONS_TEST_DATABASE_URL`
to port **5432**, but `just test-db` provisions **5433** and `just test-bucket` does NOT export the URL.
Copy the `034` test's docstring FOOTGUN banner (`:21-29`) and run with both DB URLs exported:
```bash
export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test"
just test-bucket integration
```

---

### `tests/shared/test_reenqueue_reconcile_source_scan.py` (test, AST source-guard) — NEW

**Primary analog:** `tests/shared/test_dedup_fingerprint_source_scan.py` (Phase 84) — the
**mutation-proven** AST scanner. **This is the STRONGER of the two candidate exemplars** because it
(a) targets NAMED files (not an rglob), (b) already closes both Phase-83 blind spots — the line-split
`.values(...)` chain that defeats grep, and the positional `.where(a, b, c)` / `keyword.arg is None`
that defeats a naive AST rule — and (c) has a clean-absence assertion path (its `fingerprint.py` case
asserts ZERO occurrences), which is EXACTLY Phase 80's shape (both target files end at clean absence,
**no allow-list needed**).

**Copy verbatim the scanner core:**
```python
_WHERE_FUNCS = frozenset({"where", "filter", "filter_by", "having"})

def _in_compare(occ, tree) -> bool:      # :79  read inside an ast.Compare
def _in_where_arg(occ, tree) -> bool:    # :84  walks Call.args AND Call.keywords (both Phase-83 blind spots)
def _classify(source, member) -> (all, writes, reads, other):  # :103  ast.parse then classify
```
And the crafted-STRING mutation self-tests (`:168-234`) — `test_guard_flags_positional_where_read`,
`test_guard_flags_keyword_filter_by_read`, `test_guard_flags_compare_read`,
`test_guard_ignores_fingerprinted_docstring` (the false-positive GREEN check). Mutate crafted strings,
NEVER the real files — DB-free, hermetic.

**Secondary exemplar:** `tests/analyze/services/test_single_awaiting_writer.py` (Phase 83, D-02) —
an rglob-the-whole-tree single-writer allowlist scan (`_ALLOWED_WRITERS = {backends.py}`). It is
STRONGER in one narrow dimension: it handles WRITE forms the dedup scanner does not — dict-literal
binds (`_dict_writes_awaiting:41`), subscript-mutation binds (`_name_binds_awaiting_status:50`), and
`.values(**splat)` where `keyword.arg is None` (`:113`). **Weaker overall for Phase 80**: it is an
allowlist model, and Phase 80 wants clean-absence in TWO named modules. Borrow its `.values(**splat)`
handling if the guard must also catch a re-added `update(FileRecord).values(state=...)` write — though
clean-absence of `FileState.<member>` already catches that write for free (any occurrence is flagged).

**DEVIATION (extension the dedup scanner does NOT cover):** the dedup scanner keys ONLY on
`FileState.<member>` occurrences (`_filestate_occurrences:50`). Phase 80 must ALSO flag bare
`FileRecord.state` / `file.state` attribute READS (RESEARCH forms #1, #2, #4, #6) and
`getattr(_, "state")` (form #5). ADD an `ast.Attribute` scan with `.attr == "state"` whose base is
`Name("FileRecord")` or a FileRecord-bound local, in a non-assignment-target position, plus a
`getattr(_, "state")` Call scan. Encode ALL of forms #1–#6 (RESEARCH §(b) table) as crafted-string
mutation tests, each RED, plus one GREEN false-positive check confirming a legitimate `cloud_job.status`
read (`.attr == "status"`, not `"state"`) is NOT flagged.

**Targets:** `src/phaze/tasks/reenqueue.py` and `src/phaze/tasks/reconcile_cloud_jobs.py`, both at
clean absence after the cutover. `_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "phaze"`
(copy `:41`). Placement in `tests/shared/` matches the Phase-84 guard's home (hermetic, no DB).

**Mutation-test discipline (project rule `feedback_mutation_test_guard_tests`):** for each of #1–#6,
break the real file to reintroduce that form, watch the guard go RED, restore. A guard never seen RED
is worthless — Phase 83 shipped two toothless guards exactly this way.

---

## Shared Patterns

### Ledger-scoped read-once-per-run (applies to `reenqueue.py`)
**Source:** `_build_done_sets:136` + `_in_flight_cloud_job_ids:212` + `recover_orphaned_work:342-349`.
Every set (`rows`, `live`, `done_sets`, `in_flight`) is read EXACTLY ONCE per run, then used as
in-memory set membership. D-06 keeps this shape but bounds each done-set query to the ledger's `fids`
(read at `:342` via `get_ledger_rows`), so it is O(|ledger|), never O(200K corpus).
```python
rows = await get_ledger_rows(session)
fids = {_natural_id(r) for r in rows} - {None}   # scope every done-set query to these
```

### Spill-mode CAS via the single writer (applies to `reconcile_cloud_jobs.py`)
**Source:** `services/backends.py:86` `hold_awaiting_cloud`; call-shape at `agent_s3.py:212`,
`agent_push.py:285`. The CAS runs FIRST and returns a bool; any `FileRecord`/cleanup/ledger work is
gated behind `rowcount>0`. D-12 adopts the CAS half; **D-04 drops the `FileRecord` half entirely**
(the deviation from both siblings).

### LOCKED predicate builders — compose, never re-spell (applies to `stage_status.py`, `pipeline.py`, `reenqueue.py`)
**Source:** `stage_status.py` `done_clause:114` / `failed_clause:145` / `inflight_clause:175` /
`domain_completed_clause:195`. All new clause consumers reuse these VERBATIM (DERIV-04 lock). A
re-spelling breaks `tests/integration/test_stage_status_equivalence.py`. Phase-77 partial indexes
(`ix_analysis_completed`, `ix_analysis_failed`, `ix_metadata_failed`, `ix_fprint_success`,
`ix_cloud_job_awaiting`) back each probe — keep the per-stage query shape so each index drives its own
query (RESEARCH §(d)), do NOT collapse to a single `stage_status_case`.

### Shadow gate — implication, not equality; D-04+D-12 fix a live HARD violation
**Source:** `shadow_compare.py` `_cloud_awaiting:80`, `_cloud_job_exists:68`, invariants `awaiting_cloud:131`
/ `pushing:133` / `pushed:134`. On `main` today reconcile writes `state=AWAITING_CLOUD` alongside
`cloud_job.status=FAILED` — violating `AWAITING_CLOUD ⇒ cloud_job(status='awaiting')`. Retiring the
state write makes the spilled kueue file stay at `state=PUSHED` (from `agent_s3.py:128`), which
satisfies `pushed`/`pushing` (any `cloud_job` row) → strictly MORE green. Assert this fix explicitly in
the reconcile spill test (CONTEXT §specifics #1).

### Migration contract (applies to `036`)
**Source:** `034`/`032` module docstrings. SYNC `def upgrade()`; STATIC parameter-free SQL (no
interpolation/f-string/`.format`/model import); touches NO ORM schema → empty autogenerate diff;
`saq_jobs` NEVER referenced; documented downgrade. `032:73` is the exact `files.state`-sourced backfill
shape D-13 mirrors.

### AST source-guard over grep (applies to the new guard)
**Source:** `test_dedup_fingerprint_source_scan.py`. `ast.walk` a parsed tree, key on `ast.Attribute`
nodes (never a line grep — a docstring mention of the token is not an `ast.Attribute`), walk BOTH
`Call.args` and `Call.keywords`. Clean-absence assertion (zero occurrences) is simpler and stronger
than an allowlist. Encode mutation directions as crafted-string tests.

---

## No Analog Found

| File / concern | Role | Data Flow | Reason |
|----------------|------|-----------|--------|
| the `fids` **`= ANY(array)`** bind in `reenqueue.py`'s ledger-scoped done queries | task query | transform | **RESEARCH confirmed NO `= ANY(array)` idiom exists anywhere in the codebase** — every filter uses `.in_(...)`. A bare `.in_(fids)` is a latent crash past 32767 binds (asyncpg Int16 param cap); the ledger hit ~44.5K rows in the 2026-06-18 incident (Landmine 5). |

**This is the phase's only genuinely NEW pattern.** Two acceptable routes:

1. **PRIMARY (RESEARCH recommendation) — single Postgres array bind:**
   ```python
   from sqlalchemy import ARRAY
   from sqlalchemy.dialects.postgresql import UUID as PGUUID
   stmt = select(FileRecord.id).where(
       FileRecord.id == sa.func.any(sa.bindparam("ids", value=list(fids), type_=ARRAY(PGUUID(as_uuid=True)))),
       domain_completed_clause(Stage.ANALYZE),
   )
   ```
   One bind param (the array) — no 32767 ceiling, index still usable. Cite SQLAlchemy `sa.func.any` +
   `ARRAY(PGUUID)`.

2. **FALLBACK — chunk-and-union (closest in-repo idiom):** `pipeline.py:1502`
   (`get_proposal_pending_batches`) sorts file-id strings then chunks:
   ```python
   file_ids = sorted(str(f.id) for f in result.scalars().all())
   return [file_ids[i : i + batch_size] for i in range(0, len(file_ids), batch_size)]
   ```
   Chunk `fids` into batches of ≤10 000, run one `.in_(chunk)` per batch, union the id-sets in Python.

**Do NOT ship a bare `.in_(fids)` without one of these.**

---

## Metadata

**Analog search scope:** `src/phaze/tasks/`, `src/phaze/services/`, `src/phaze/routers/`,
`src/phaze/models/`, `alembic/versions/`, `tests/shared/`, `tests/analyze/`, `tests/integration/`.
**Files scanned (read in full or targeted):** `reenqueue.py`, `reconcile_cloud_jobs.py`,
`stage_status.py`, `backends.py` (hold+enqueue region), `pipeline.py` (targeted), `032`, `034`,
`test_dedup_fingerprint_source_scan.py`, `test_single_awaiting_writer.py` (targeted),
`test_migration_034_*.py`, `test_recovery.py` (targeted), `agent_push.py`/`agent_s3.py` (spill region),
`scheduling_ledger.py` (model).
**Pattern extraction date:** 2026-07-10
</content>
</invoke>
