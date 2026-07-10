# Phase 82: Counts & Pending-Set Cutover - Pattern Map

**Mapped:** 2026-07-10
**Files analyzed:** 11 (5 source modified, 1 template modified, 5 test/script new-or-extended)
**Analogs found:** 11 / 11 (all in-repo; zero new deps, zero migration)

> Pure READER cutover in `src/phaze/`. Every predicate this phase composes already exists on `main`
> in `services/stage_status.py` (Phase 78). The one new builder (`eligible_clause`) and every new test
> has a mature, load-bearing analog in the same tree. Copy shape verbatim — do not invent.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/services/stage_status.py` — NEW `eligible_clause(stage)` | service (predicate builder) | transform (SQL `ColumnElement`) | `domain_completed_clause` (same file, `:196`) | exact |
| `src/phaze/services/pipeline.py` — `get_metadata_pending_files` (`:1370`) | service (query) | request-response (pending SELECT) | `get_metadata_failed_files` (same file, `:1384`) | exact |
| `src/phaze/services/pipeline.py` — `get_fingerprint_pending_files` (`:1403`) | service (query) | request-response (pending SELECT) | `get_metadata_failed_files` (`:1384`) + `eligible_clause` | exact |
| `src/phaze/services/pipeline.py` — `get_discovered_files_with_duration` (`:1098`) | service (query) | request-response (pending SELECT + LEFT JOIN) | itself (keep JOIN, swap WHERE) + `get_awaiting_cloud_count` (`:1115`) | exact |
| `src/phaze/services/pipeline.py` — `get_stage_progress` four-bucket (`:302`) | service (derived counts) | CRUD (aggregate GROUP BY) | `get_stage_progress` self + `_safe_count` (`:282`) + `get_stage_controls` (`:423`) | exact |
| `src/phaze/services/pipeline.py` — `get_pipeline_stats` removal (`:61`) | service (derived counts) | CRUD (aggregate) | replaced by `get_stage_progress` (`:302`) | exact |
| `src/phaze/routers/pipeline.py` — 3 callers (`:240,485,629`) | route (context builder) | request-response | the caller sites themselves | exact |
| `src/phaze/templates/pipeline/partials/stats_bar.html` | component (Jinja partial) | request-response (HTMX OOB) | itself (key remap) | exact |
| `tests/integration/test_stage_status_equivalence.py` — ADD `ELIGIBLE_CASES` | test | event-driven (parametrized real-PG) | `DOMAIN_COMPLETED_CASES` block (same file, `:445`) | exact |
| `tests/shared/test_pending_set_source_scan.py` — NEW | test (AST guard) | transform (source scan) | `tests/shared/test_dedup_fingerprint_source_scan.py` | exact |
| `tests/integration/test_pending_set_divergence.py` — NEW | test (behavioral) | event-driven (inconsistent corpus) | `tests/integration/test_dedup_divergence.py` | exact |
| `tests/integration/test_enrich_pending_independence.py` — NEW (SC#1 all-orderings + cloud) | test | event-driven | `test_dedup_divergence.py` fixture + `test_stage_status_equivalence.py` seeds | role-match |
| Four-bucket sum-to-total test — NEW/extend | test | event-driven | `test_stage_status_equivalence.py` seeds + Pattern 3 | role-match |
| `scripts/seed_perf_corpus.py` + `just perf-*` — NEW | utility + config | batch (bulk seed) | `scripts/coverage_floor.py` (uv-run script shape) + `justfile` `db` group (`:458`) | partial (no seed analog) |

---

## Pattern Assignments

### `src/phaze/services/stage_status.py` — NEW `eligible_clause(stage)` (service, transform)

**Analog:** `domain_completed_clause` (`services/stage_status.py:196-228`) — the enrich-only-guarded builder that composes LOCKED sibling clauses verbatim and drives a per-stage table off `enums/stage.py`.

**Enrich-only guard pattern to copy** (`domain_completed_clause`, `:222-225`) — mirror the `ValueError` shape exactly (same message form, same `getattr(stage, "value", stage)` raw-str handling):
```python
if stage not in FAILURE_IS_TERMINAL:
    got = getattr(stage, "value", stage)
    raise ValueError(f"domain_completed_clause is defined only for the enrich stages {sorted(s.value for s in FAILURE_IS_TERMINAL)}; got {got!r}")
```
For `eligible_clause`, gate on `ELIGIBLE_AFTER_FAILURE` (also enrich-only-keyed, `enums/stage.py:88`) instead of `FAILURE_IS_TERMINAL`.

**Table-driven composition pattern** (`domain_completed_clause`, `:226-228`) — drop the failure conjunct off the table, NEVER inline `if stage is ANALYZE`:
```python
if FAILURE_IS_TERMINAL[stage]:
    return or_(done_clause(stage), failed_clause(stage))
return done_clause(stage)
```
`eligible_clause` inverts this: always `~inflight_clause(stage) ∧ ~done_clause(stage)`, and append `~not_(failed_clause(stage))` **only when** `not ELIGIBLE_AFTER_FAILURE[stage]` (analyze). The RESEARCH proposed body (`82-RESEARCH.md:132-151`) is the exact target — it matches this analog's discipline.

**Python truth being mirrored** (`enums/stage.py:243`):
```python
return status not in (Status.DONE, Status.IN_FLIGHT) and (status != Status.FAILED or ELIGIBLE_AFTER_FAILURE[stage])
```
`status not in (DONE, IN_FLIGHT)` ⇔ `~inflight_clause ∧ ~done_clause` (CASE precedence `in_flight ≻ done ≻ failed ≻ not_started` makes the two negations mutually exclusive per file). `has_approved_proposal` is APPLY-only (`enums/stage.py:245`) — irrelevant here; signature stays a single `stage` param.

**Imports already present in the module** (`stage_status.py:62,65`): `and_, not_, exists, false` from sqlalchemy; `FAILURE_IS_TERMINAL, Stage, Status` from `phaze.enums.stage`. Add `ELIGIBLE_AFTER_FAILURE` to the existing `from phaze.enums.stage import ...` line.

**Correlated-`exists` join contract** (docstring pattern at `dedup_resolved_clause:91-112` and `done_clause:118`): `eligible_clause` composes builders that use `~exists(... == FileRecord.id)`, so the enclosing query MUST select-from/join `FileRecord`. Document this in the docstring (the pending-set queries already do). No `CloudJob` join at this level.

> **LANDMINE (Q-A / D-00c, sharpest correctness risk):** `inflight_clause(ANALYZE)` (`stage_status.py:176-193`)
> reads **ONLY** `scheduling_ledger` on key `"process_file:<file_id>"` (`STAGE_TO_FUNCTION["analyze"] == "process_file"`,
> `stage_control.py:53`). It does **NOT** reference `cloud_job`. So a cloud-dispatched file
> (`AWAITING_CLOUD`/`PUSHING`/`PUSHED`) is auto-excluded from the analyze pending set **only if** its
> local `process_file:<id>` ledger row survives cloud hand-off. The planner MUST either prove the ledger
> row survives until cloud-terminal, OR add an explicit `~exists(cloud_job WHERE status IN ('awaiting','pushing','pushed'))`
> conjunct to the analyze pending set (composed at the `pipeline.py` query level, like `~dedup_resolved_clause()`,
> NOT inside `eligible_clause`). A real-PG regression seeding `cloud_job(status='pushing')` and asserting
> ABSENCE from the analyze set is mandatory (mirrors `awaiting_candidate_clause`'s cloud-join note at `:254-256`).

---

### `src/phaze/services/pipeline.py` — the three pending-set rewrites (service, request-response)

**Analog A — the correlated-`exists` pending set:** `get_metadata_failed_files` (`pipeline.py:1384-1400`). It already returns a pending set via a single `.where(exists(select(...).where(... == FileRecord.id, ...)))` composed off a `stage_status` clause shape. Copy this single-WHERE structure for all three cutovers:
```python
stmt = select(FileRecord).where(exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id, FileMetadata.failed_at.isnot(None))))
result = await session.execute(stmt)
return list(result.scalars().all())
```

**Analog B — composing LOCKED clauses at the query level:** `get_awaiting_cloud_count` (`pipeline.py:1115-1136`) shows the canonical "compose `stage_status` clause builders verbatim in a `.where()`, with the required `FileRecord` join for the correlated `~exists` to resolve":
```python
select(func.count(CloudJob.id)).select_from(CloudJob).join(FileRecord, FileRecord.id == CloudJob.file_id).where(awaiting_candidate_clause())
```

**Target for each (per `82-RESEARCH.md:250-294`, D-01/D-03):**
- `get_metadata_pending_files` (`:1370`): `.where(file_type.in_(MUSIC_VIDEO_TYPES), eligible_clause(Stage.METADATA), ~dedup_resolved_clause())`. Current body is `select(FileRecord).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))` (`:1379`).
- `get_fingerprint_pending_files` (`:1403`): same shape with `Stage.FINGERPRINT`. **The `get_files_by_state(METADATA_EXTRACTED)` UNION + manual de-dup-by-id loop (`:1418-1435`) COLLAPSES** to one WHERE — `eligible_clause(FINGERPRINT)` (`~inflight ∧ ~done`) already subsumes the failed-retry set (`ELIGIBLE_AFTER_FAILURE[FINGERPRINT]=True` → no `~failed` conjunct).
- `get_discovered_files_with_duration` (`:1098`): **KEEP** the `.outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)` (`:1108`, cloud duration-router reads it); swap `.where(FileRecord.state == FileState.DISCOVERED)` (`:1109`) → `.where(file_type.in_(MUSIC_VIDEO_TYPES), eligible_clause(Stage.ANALYZE), ~dedup_resolved_clause())`. **`file_type` scope is NEWLY required** (current query is file-type-agnostic — Pitfall 1).

**Shared-helper anti-drift context** (`pipeline.py:1359-1367`): these three helpers are the single "pending" definition consumed by BOTH the manual triggers AND the Phase-80 recovery producer (`reenqueue.py`). Narrowing the helper narrows all three call paths in lockstep (Phase-42 precedent) — do NOT add per-endpoint filters. `get_metadata_failed_files` (`:1384`) is a SEPARATE operator-retry endpoint and stays as-is.

**Import note:** add `eligible_clause` (and confirm `dedup_resolved_clause`, `Stage` are imported) to `pipeline.py`'s imports from `phaze.services.stage_status` / `phaze.enums.stage`.

---

### `src/phaze/services/pipeline.py` — `get_stage_progress` four-bucket + `_safe_bucket_counts` (service, CRUD)

**Analog:** `get_stage_progress` itself (`pipeline.py:302-413`) + `_safe_count` (`:282-299`) + `get_stage_controls` (`:423-447`, the degrade-to-defaults idiom).

**Degrade discipline to copy verbatim** (`_safe_count`, `:291-299`) — log → guarded rollback → safe default, so the 5s poll never 500s:
```python
try:
    return int((await session.execute(stmt)).scalar() or 0)
except Exception:
    logger.warning("stage_progress_degraded", node=node, exc_info=True)
    try:
        await session.rollback()
    except Exception:
        logger.warning("stage_progress_rollback_failed", node=node, exc_info=True)
    return 0
```

**New `_safe_bucket_counts` target** (`82-RESEARCH.md:205-220`) — one `GROUP BY stage_status_case(stage)` scoped to `MUSIC_VIDEO_TYPES` (`:45`), zero-filled `{s.value: 0 for s in Status}`, wrapped in the identical try/except-rollback. `stage_status_case(stage)` is the LOCKED CASE at `stage_status.py:265-284` — reuse it, do NOT author a fresh CASE (D-04).

**Return-shape extension pattern** (`get_stage_progress` return dict, `:371-413`): the three enrich nodes currently return `{"done": ..., "total": music_video_total}` (`:373-388`). Extend to `{**buckets, "total": music_video_total}` (flat `{not_started, in_flight, done, failed, total}` recommended — Q-B discretion). Downstream nodes (`scan_search`, `scrape`, `match`, `proposals`, `execute`) keep `{done, total}` untouched. `music_video_total` is already computed at `:333-337`.

**Invariant:** `not_started + in_flight + done + failed == total` per enrich stage on a healthy query (all-zero on degrade is the intentional fail-safe — assert the invariant only on a healthy corpus, Pitfall 3).

---

### `src/phaze/services/pipeline.py` — `get_pipeline_stats` removal + 3 router callers (service + route)

**Analog:** the caller sites are self-documenting; `get_stage_progress` (`:302`) is the derived replacement.

**`get_pipeline_stats` (`:61-71`) is removed in full** (D-05, SC#2 — no `state`-keyed GROUP BY survives). Current body:
```python
stmt = select(FileRecord.state, func.count(FileRecord.id)).group_by(FileRecord.state)
```

**Seven consumed keys → derived re-expression** (`82-RESEARCH.md:325-334`) — all from `get_stage_progress`:
| Old key | Consumed at | Derived re-expression |
|---|---|---|
| `stats["discovered"]` | `routers/pipeline.py:241`, `stats_bar.html:3,47` | `stage_progress["discovery"]["done"]` |
| `stats["metadata_extracted"]` | `:241` notYetEnriched, `stats_bar.html:48` | `stage_progress["metadata"]["done"]` |
| `stats["fingerprinted"]` | `stats_bar.html:7` | `stage_progress["fingerprint"]["done"]` |
| `stats["analyzed"]` | `:506/:636` `queue_progress_percent`, `stats_bar.html:11,49` | `stage_progress["analyze"]["done"]` |
| `stats["proposal_generated"]` | `stats_bar.html:15` | `stage_progress["proposals"]["done"]` |
| `stats["approved"]` | `stats_bar.html:19` | `stage_progress["execute"]["total"]` |
| `stats["executed"]` | `stats_bar.html:23` | `stage_progress["execute"]["done"]` |

**Caller migration sites** (verified via grep, exactly three + the import at `routers/pipeline.py:46`):
- `:240` `_build_dag_context` → `notYetEnriched = max(metadata.total − metadata.done, 0)` (D-05).
- `:485` `build_dashboard_context` → `queue_progress_percent(stage_progress["analyze"]["done"], activity["agent_busy"])` (was `:506`).
- `:629` `pipeline_stats_partial` → same `queue_progress` swap (was `:636`).

`queue_progress_percent` (`pipeline.py:264-279`) signature is unchanged — only its first arg's source changes.

---

### `src/phaze/templates/pipeline/partials/stats_bar.html` (component, HTMX)

**Analog:** the template itself (key remap, Q-C discretion).

**Six visible cards** read `stats.<filestate>` at lines 3, 7, 11, 15, 19, 23. **Three OOB `x-init` store writes** at lines 47-49 push into `$store.pipeline.discovered / .metadataExtracted / .analyzed`. Recommended (D-05): the two context builders pass a small derived `stats` dict built from `stage_progress` so the template edit is a mechanical value-source remap. **Keep the Alpine `$store.pipeline.*` keys STABLE** (Pitfall 4) — they drive the DAG canvas bindings + button `:disabled` gating; only the server-side source changes. The `dag.items()` OOB loop (`:66-68`) and the per-card partial includes below are untouched.

---

### `tests/integration/test_stage_status_equivalence.py` — ADD `ELIGIBLE_CASES` (test, real-PG)

**Analog:** the `DOMAIN_COMPLETED_CASES` block in the SAME file (`:445-483`) — an existing SECOND parametrized matrix added alongside the primary `CASES`. Copy its structure exactly for a THIRD matrix.

**Reuse ALL existing infra** — no new fixtures:
- `db_session` fixture (`:78-109`), `_new_file` (`:116`), `_seed_ledger` (`:134`), and every `seed_*` fn (`:148-300`).
- The lazy-import-in-helper idiom (`eval_sql_domain_completed:463-468`) keeps `pytest --co` green while `eligible_clause` doesn't exist yet (TDD RED):
```python
async def eval_sql_domain_completed(session, stage, file_id) -> bool:
    from phaze.services.stage_status import domain_completed_clause  # lazy: keeps --co green in the RED state
    result = await session.execute(select(domain_completed_clause(stage)).where(FileRecord.id == file_id))
    return bool(result.scalar_one())
```
- The three-way assertion (`test_domain_completed_sql_equals_python:471-483`): `assert sql == py == expected`. Python side is `eligible(status_map, stage)` (`enums/stage.py:215`), reading via `resolve_status(stage, await load_scalars(...))`.

**The single load-bearing anti-drift cell:** `(Stage.ANALYZE, seed_analysis_failed, False)` (`82-RESEARCH.md:181`) — goes RED if a future edit drops the analyze `~failed_clause` conjunct (ELIG-03 / 44.5K guard). `seed_analysis_failed` already exists (`:166`). The full `ELIGIBLE_CASES` matrix is proposed at `82-RESEARCH.md:165-184`.

---

### `tests/shared/test_pending_set_source_scan.py` — NEW AST guard (test, transform)

**Analog:** `tests/shared/test_dedup_fingerprint_source_scan.py` (Phase 84 D-14) — the mutation-tested AST source scan. **This is the highest-leverage analog for the phase.**

**Copy the helper battery VERBATIM** (`:50-117`): `_filestate_occurrences` (matches `FileState.<member>` attribute chains, docstring-blind), `_in_compare` (`:79`), `_in_where_arg` (`:84-100` — walks BOTH positional `Call.args` AND `Call.keywords`, catches `keyword.arg is None` splat), and `_classify` (`:103-117`). These encode the two Phase-83 blind spots (positional-arg reads, keyword splat) the project-memory `feedback_mutation_test_guard_tests` warns about.

**Scope difference from the analog:** dedup.py had "exactly ONE write, zero reads". The three pending-set FUNCTIONS in `pipeline.py` are pure readers with NO `.state=` write, so the invariant is cleaner: **ZERO `FileState` READ occurrences** of `DISCOVERED` / `METADATA_EXTRACTED` / `FINGERPRINTED` inside those functions (a bare "state absent" assertion is impossible — dual-write D-00a still stamps `.state` elsewhere in the module; scope the scan to the three function bodies).

**Mutation directions to encode permanently** (mirror `:168-234`, mutate crafted STRINGS not files):
1. positional `.where(a, FileRecord.state == FileState.METADATA_EXTRACTED)` → RED (`test_guard_flags_positional_where_read:168`).
2. keyword `.filter_by(state=FileState.DISCOVERED)` → RED (`test_guard_flags_keyword_filter_by_read:187`).
3. `.where(**{"whereclause": FileRecord.state == ...})` splat → RED (verify the `keyword.arg is None` path).
4. a docstring mention of `METADATA_EXTRACTED` → GREEN (`test_guard_ignores_fingerprinted_docstring:226`).

> **Break real source → watch RED → restore** to prove teeth. A green guard against buggy source proves
> nothing (`feedback_mutation_test_guard_tests`; Phase 83 shipped two toothless guards). Mutate EVERY
> syntactic form and check for false positives on `.where()` READERS you keep (e.g. `FileRecord.file_type.in_`).

---

### `tests/integration/test_pending_set_divergence.py` — NEW behavioral guard (test, real-PG)

**Analog:** `tests/integration/test_dedup_divergence.py` (Phase 84) — seeds an INCONSISTENT corpus (marker ≢ state) and asserts the derived reader wins where a `state`-based reader would invert. **Second-highest-leverage analog.**

**Copy the fixture + `_TARGET_DB` guard verbatim** (`:63-125`): the `_TARGET_DB.endswith("_test")` destructive-DB guard (`:69-75`), the `db_session` fixture (`:83-107`, connectivity-probe skip + `Base.metadata.create_all` + FK agent seed + rollback teardown), and the `_file(session, *, sha256, state)` seed helper (`:110-125`).

**The divergence discipline** (`_seed_inconsistent_corpus:128-140` + per-reader tests `:155-213`) — each test carries a `MUTATION:` comment naming the exact `state`-based revert that inverts it. Copy for all three pending sets (`82-RESEARCH.md:366-370`):
- **File A:** metadata output row present (derived metadata=DONE) BUT `state='discovered'` → EXCLUDED from `get_metadata_pending_files`.
- **File B:** NO fingerprint row (derived NOT_STARTED, eligible) BUT `state='analyzed'` → INCLUDED in `get_fingerprint_pending_files`.
- **File C:** NO analysis row (derived eligible) BUT `state='fingerprinted'` → INCLUDED in the analyze set.

Each MUTATION comment: reverting THAT reader's predicate to a `FileRecord.state`-based filter inverts the assertion.

---

### `tests/integration/test_enrich_pending_independence.py` — NEW (SC#1 all-orderings + cloud-exclusion)

**Analog:** `test_dedup_divergence.py` fixture (`:83-107`) for the real-PG harness + `test_stage_status_equivalence.py` seed fns (`:148-300`) for writing OUTPUT rows (metadata / fingerprint-success / analysis-with-`completed_at`) rather than mutating `state`.

**Design** (`82-RESEARCH.md:303-312`): one music/video file in `state='discovered'`, drive through all 6 permutations of {metadata, fingerprint, analyze} by writing the stage's output row. After each partial: completed stage EXCLUDES the file; not-yet-done stages STILL INCLUDE it (independence). Dual-write caveat (D-00a): set `state` to what the real writer would set (e.g. `METADATA_EXTRACTED`) so the test proves the derived reader IGNORES state.

**Deadlock-detection cell (RED pre-cutover):** write a metadata row AND advance `state='metadata_extracted'`, then assert the file is STILL in the analyze pending set — GREEN post-cutover (derived), RED pre-cutover (`state == DISCOVERED` gate drops it).

**Cloud-exclusion cell (D-00c / Q-A, mandatory):** seed `cloud_job(status='pushing')` and assert ABSENCE from the analyze pending set. This is the single decisive test for the Q-A landmine — it goes RED if the ledger row does NOT survive cloud hand-off and no explicit cloud conjunct was added.

---

### `scripts/seed_perf_corpus.py` + `just perf-seed` / `just perf-explain` — NEW (utility + config)

**Analog:** partial. No 200K seed/bench harness exists (`scripts/` has `coverage_floor.py`, `download-models.sh`, `update-project.sh`, `classify-changed-files.sh`, none a seed). Use `scripts/coverage_floor.py` as the uv-run standalone-script shape and the `justfile` `[group('db')]` recipes (`:458-478`: `db-upgrade`, `db-revision`, `db-current`, `db-downgrade`) + `test-db` (`:136`) / `integration-test` (`:209`) as the recipe-authoring pattern.

**Constraints** (`82-RESEARCH.md:340-358`): ~200K `FileRecord` (music/video) at migration HEAD (`≥036`, so 032 partial indexes exist — `just db-upgrade`). Distribute output rows to realistic selectivity (~70% metadata, ~55% fingerprint-success, ~40% analysis-completed, ~5% analyze-failed, ~1% cloud_job, ~2% dedup markers, few-thousand scheduling_ledger). Bulk-insert via asyncpg executemany/COPY batches. Seed `state` too (dual-write realism, shadow gate stays green).

**EXPLAIN targets:** the 3 pending SELECTs + 3 `GROUP BY stage_status_case` + full `/pipeline/stats` endpoint. Verify Index/Index-Only Scan on the 032 partial indexes (`ix_fprint_success`, `ix_analysis_completed`, `ix_analysis_failed`, `ix_metadata_failed`, `ix_cloud_job_awaiting`) NOT Seq Scan. PASS budget `< ~1s` (D-07). Record the number in VERIFICATION regardless — it licenses the DENORM-01 YAGNI decision.

**Env:** test-DB port **5433**, test-Redis **6380**; run perf seed/measure in ISOLATION (colima full-suite flake). Do NOT probe live lux (Alembic ~031, no 032 indexes → invalid plan).

---

## Shared Patterns

### Degrade-safe counting (never 500 the 5s poll)
**Source:** `_safe_count` (`pipeline.py:282-299`) + `saq_detail` `begin_nested()` SAVEPOINT (`stage_status.py:293-313`) + `get_stage_controls` degrade-to-defaults (`pipeline.py:423-447`).
**Apply to:** the new `_safe_bucket_counts` and every read in the four-bucket / stats path.
```python
# _safe_count (pipeline.py:291) — log → guarded rollback → safe default
try:
    return int((await session.execute(stmt)).scalar() or 0)
except Exception:
    logger.warning("stage_progress_degraded", node=node, exc_info=True)
    try:
        await session.rollback()
    except Exception:
        logger.warning("stage_progress_rollback_failed", node=node, exc_info=True)
    return 0
```
```python
# SAVEPOINT-isolated corroborating read (stage_status.py:304) — for any read that must not poison the outer txn
try:
    async with session.begin_nested():
        rows = (await session.execute(_SAQ_DETAIL_SQL)).all()
except Exception:
    logger.warning("saq_detail_degraded", exc_info=True)
    return out
```

### Composing LOCKED clause builders verbatim (DERIV-04 guarantee)
**Source:** `awaiting_candidate_clause` (`stage_status.py:231-262`), `get_awaiting_cloud_count` (`pipeline.py:1115-1136`).
**Apply to:** `eligible_clause` (composes `~inflight_clause`, `~done_clause`, `~failed_clause`) + all three pending-set queries.
- Never re-spell a predicate; always call `done_clause` / `failed_clause` / `inflight_clause` / `dedup_resolved_clause`.
- The correlated `~exists(... == FileRecord.id)` requires the enclosing query to select-from/join `FileRecord` (INNER-join `FileRecord` when the driving table is `CloudJob`, as `get_awaiting_cloud_count:1134` does).
- Keep dedup file-level: compose `~dedup_resolved_clause()` at the `pipeline.py` query level, NOT inside `eligible_clause` (D-03).

### Table-driven per-stage carve-outs (no inlined `if stage is ANALYZE`)
**Source:** `domain_completed_clause` off `FAILURE_IS_TERMINAL` (`stage_status.py:226`); `eligible()` off `ELIGIBLE_AFTER_FAILURE` (`enums/stage.py:249`).
**Apply to:** `eligible_clause` — drive the analyze `~failed_clause` conjunct off `ELIGIBLE_AFTER_FAILURE[stage]`, never a literal stage check (Anti-Pattern, `82-RESEARCH.md:226`).

### Mutation-tested guard discipline (a green guard proves nothing)
**Source:** `test_dedup_fingerprint_source_scan.py` (AST + crafted-string mutations) + `test_dedup_divergence.py` (inconsistent-corpus behavioral).
**Apply to:** both new guards. Break real source → watch RED → restore. Cover every syntactic form (positional, keyword, splat) and check for false positives on readers you keep.

### Real-PG test harness (connectivity-probe skip + rollback teardown)
**Source:** `db_session` fixture in `test_stage_status_equivalence.py:78-109` and `test_dedup_divergence.py:83-107` (identical shape).
**Apply to:** all new integration tests. Reuse the `psycopg` probe → `pytest.skip`, `Base.metadata.create_all`, `_LEGACY_AGENT_ID = "legacy-application-server"` FK seed, rollback-at-teardown. `test_dedup_divergence.py` additionally guards `_TARGET_DB.endswith("_test")` before running (destructive-DB safety).

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `scripts/seed_perf_corpus.py` | utility | batch (200K bulk seed) | No seed/bench harness exists in `scripts/` or `justfile`. Author fresh; borrow only the uv-run standalone shape from `scripts/coverage_floor.py` and the `[group('db')]` recipe form from `justfile:458`. |

---

## Metadata

**Analog search scope:** `src/phaze/services/{stage_status,pipeline}.py`, `src/phaze/enums/stage.py`, `src/phaze/routers/pipeline.py`, `src/phaze/templates/pipeline/partials/stats_bar.html`, `src/phaze/tasks/_shared/stage_control.py`, `tests/integration/{test_stage_status_equivalence,test_dedup_divergence}.py`, `tests/shared/test_dedup_fingerprint_source_scan.py`, `scripts/`, `justfile`.
**Files scanned:** 10 read in full or targeted; grep across routers + stage_control + justfile.
**Pattern extraction date:** 2026-07-10
