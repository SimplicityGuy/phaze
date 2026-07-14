# Phase 82: Counts & Pending-Set Cutover - Research

**Researched:** 2026-07-10
**Domain:** Derived-status read cutover (SQLAlchemy 2.x async `ColumnElement` composition; FastAPI/Jinja/HTMX stats surface; Postgres query-plan measurement)
**Confidence:** HIGH (all building blocks read directly from `main`; zero new dependencies; no external docs needed)

## Summary

Phase 82 is a **pure reader cutover** entirely inside the existing codebase. Every predicate it needs already exists on `main` in `services/stage_status.py` (Phase 78) — `done_clause` / `failed_clause` / `inflight_clause` / `domain_completed_clause` / `stage_status_case` / `dedup_resolved_clause` / `awaiting_candidate_clause` — plus the pure-Python truth model in `enums/stage.py` (`eligible`, `resolve_status`, `ELIGIBLE_AFTER_FAILURE`, `FAILURE_IS_TERMINAL`). The one new SQL builder, `eligible_clause(stage)`, is a mechanical composition of clauses already present, and the DERIV-04 equivalence harness (`tests/integration/test_stage_status_equivalence.py`) is a mature, parametrized fixture matrix that extends cleanly to lock it against `eligible()`. There is **no missing writer and no data-repair migration** — Phase 80's `036` already backfilled the analyze corpus (verified in ROADMAP + auto-memory); Phase 82 only *verifies* it. No web/library research was required: every claim below is sourced from code read this session.

The three enrich pending sets (`get_metadata_pending_files`, `get_fingerprint_pending_files`, and the analyze set `get_discovered_files_with_duration`) each cut over to the single composed predicate `eligible_clause(stage) ∧ ~dedup_resolved_clause() ∧ file_type IN MUSIC_VIDEO_TYPES`. `get_pipeline_stats`'s linear `GROUP BY FileRecord.state` is removed entirely and its seven consumed keys re-expressed from `get_stage_progress` (which already derives every DAG node from output tables with `_safe_count` degrade). `get_stage_progress`'s three enrich nodes extend from `{done, total}` to a four-bucket `{not_started, in_flight, done, failed, total}` via one `GROUP BY stage_status_case(stage)` per enrich stage.

**Primary recommendation:** Add `eligible_clause(stage)` to `services/stage_status.py` composing `~done_clause ∧ ~inflight_clause` for metadata/fingerprint and `~done_clause ∧ ~inflight_clause ∧ ~failed_clause` for analyze (driven by `ELIGIBLE_AFTER_FAILURE`, never inlined); lock it with an `ELIGIBLE_CASES` extension of the existing equivalence harness; then cut the three pending sets and the stats/DAG counts over to the derived layer, guarded by an AST source-scan (mutation-tested) + a behavioral state/marker-disagreement divergence test. Sequence as 4 waves matching the CONTEXT's four seams. Measure PERF-02 on a local synthetic ~200K seed at migration HEAD (`≥036`); do not build DENORM-01 unless `/pipeline/stats` exceeds ~1s.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-00a: Writers dual-write.** `FileRecord.state` keeps being stamped; only *reliance* on it (reads) is replaced. The `state` write dies in Phase 90.
- **D-00b: `in_flight` authority = `scheduling_ledger`** (78 D-01). `saq_jobs` is corroborating only, SAVEPOINT-isolated, never flips the boolean, degrade-safe.
- **D-00c: `in_flight(ANALYZE)` spans the `cloud_job` sidecar** (Phase 83). `AWAITING_CLOUD`/`PUSHING`/`PUSHED`/`LOCAL_ANALYZING` all derive in-flight, so the analyze pending set auto-excludes cloud-in-flight files with no extra clause.
- **D-00d: Per-stage failure policy is fully encoded** in `enums/stage.py`: `ELIGIBLE_AFTER_FAILURE = {ANALYZE: False, METADATA: True, FINGERPRINT: True}`. `eligible_clause` must mirror this table, not inline it.
- **D-00e: The shadow-compare gate (Phase 79) must stay green** across the cutover (implication, not equality; soft allowlist). Introduce no new hard divergence.
- **D-01: Add `eligible_clause(stage)` to `services/stage_status.py`; drift-lock it against Python `eligible()` via Phase-78's DERIV-04 harness.** All three enrich pending sets compose `eligible_clause(stage) ∧ ~dedup_resolved_clause() ∧ FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)`. (Rejected: hand-composing the negations inline in `pipeline.py` — three drift-prone copies.)
- **D-02: Phase 82 owns NO backfill migration** — Phase 80's `036_backfill_analysis_completed_at.py` already repaired the corpus. VERIFICATION must assert deploy target ≥ Alembic `036` AND `COUNT(files WHERE state='analyzed' AND analysis_completed_at IS NULL AND failed_at IS NULL) = 0` before the analyze pending-set flip is trusted.
- **D-03: Apply `~dedup_resolved_clause()` in all three enrich pending sets; keep `eligible()`/`eligible_clause()` dedup-agnostic.** Dedup is a file-level predicate composed at the `pipeline.py` query level, NOT baked into the eligibility primitives or the DERIV-04 harness.
- **D-04: Extend `get_stage_progress` (not a new function).** Three enrich nodes return `{not_started, in_flight, done, failed, total}` via `stage_status_case(stage)`; downstream nodes keep `{done, total}`. (Rejected: a separate `get_enrich_stage_buckets()`.)
- **D-05: Remove `get_pipeline_stats`'s linear `GROUP BY FileRecord.state` in full; derive all counts from output tables now.** Tail counts from output-table row existence (as `get_stage_progress` already does). `notYetEnriched` re-expressed as `metadata.total − metadata.done`. `stats_bar.html` key remap expected. Front-runs nothing in Phase 86. (Rejected: enrich-only removal leaving the tail on `state`.)
- **D-06: Measure PERF-02 on a LOCAL synthetic-seed ~200K corpus at migration HEAD (`≥036`, so the 032 partial indexes exist), via EXPLAIN ANALYZE + full-endpoint timing** — NOT a live lux probe (prod at Alembic ~031 lacks 032 indexes → invalid plans). A live read-only COUNT may be a supplementary sanity check only.
- **D-07: PASS budget = full `/pipeline/stats` endpoint `< ~1s` at 200K.** Record the measured number in VERIFICATION regardless of pass/fail. DENORM-01 stays deferred unless over budget.

### Claude's Discretion
- **Four-bucket return shape** — nested `{node: {bucket: int}}` vs flatter form; constrained to keep four-bucket-sums-to-`total` per enrich stage and every read `_safe_count`-degrade-safe (never 500 the 5s poll).
- **`stats_bar.html` template churn** — the exact new key set replacing the `FileState`-keyed dict.
- **The 200K synthetic-seed harness** — shape, coverage distribution, whether it reuses an existing fixture.
- **Manual-trigger endpoint alignment** — whether `trigger_metadata_extraction` / `trigger_extraction_ui` / `trigger_fingerprint(_ui)` route through the narrowed helpers (Phase-42 "UI/API/recovery must not drift" precedent).
- **Plan/PR decomposition** — four named seams; small blast-radius per PR is the milestone's standing rule.

### Deferred Ideas (OUT OF SCOPE)
- **DENORM-01** — denormalized stored stage-bitmap column; built ONLY if PERF-02 proves the derived query too slow.
- **Per-file stage matrix / failure-retry UI / eligibility trace / priority stepper** → Phase 87.
- **`get_pushing_count` / `get_pushed_count` unowned gap** — carried from 83/84, not this phase.
- **`find_duplicate_groups` nondeterministic pagination** (`dedup.py:81`) — pre-existing, its own quick task.
- **`proposals.status` authority / `_TERMINAL_FILE_STATES`** → Phase 86.
- **`services/dedup.py` + `get_fingerprint_progress`** → Phase 84 (merged); this phase only consumes `dedup_resolved_clause()`.
- **`files.state` column drop + `FileState` enum deletion + remaining `.state=` writers** → Phase 90.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| READ-01 | The three enrich pending sets derived from `stage_status` (not `FileRecord.state`); metadata/fingerprint/analyze each surface every not-done, not-in-flight file independent of the others; cross-stage deadlock gone. | OQ1 (`eligible_clause`), OQ2 (three pending-set diffs), OQ3 (all-orderings test), OQ6 (anti-drift guard). Every predicate exists in `stage_status.py`; the composition is `eligible_clause(stage) ∧ ~dedup_resolved_clause() ∧ file_type IN MUSIC_VIDEO_TYPES`. |
| READ-02 | `get_pipeline_stats` reports per-stage counts from output tables (linear `GROUP BY state` removed); DAG shows four-bucket per-stage counts (not_started/in_flight/done/failed) incl. visible failed count per enrich stage. | OQ4 (`get_stage_progress` four-bucket via `stage_status_case`; `get_pipeline_stats` removal; consumer inventory + `stats_bar.html` remap). |
| PERF-02 | `/pipeline/stats` poll latency at 200K measured + recorded in VERIFICATION; no denormalized column unless measurement proves too slow (YAGNI). | OQ5 (synthetic-seed harness + EXPLAIN ANALYZE methodology; index-usage verification; test-DB env). |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Eligibility SQL predicate (`eligible_clause`) | API / Backend (`services/stage_status.py`) | — | Single source of truth for SQL-side per-stage eligibility; drift-locked to the DB-free `enums/stage.py` twin. |
| Pending-set queries (3 enrich) | API / Backend (`services/pipeline.py`) | Database (partial indexes) | Pure SELECTs composing the predicate layer; consumed by manual triggers + recovery producer. |
| Four-bucket DAG counts | API / Backend (`services/pipeline.py::get_stage_progress`) | Database (`GROUP BY stage_status_case`) | Derived counts fan out on the 5s poll; must be degrade-safe. |
| Stats-bar rendering | Frontend Server (Jinja `stats_bar.html`) + Backend context builder | Browser (Alpine `$store.pipeline` OOB seeds) | Server computes ints; HTMX OOB swap pushes them into the Alpine store; template keys must remap. |
| PERF-02 measurement | Database (EXPLAIN ANALYZE) + Backend (endpoint timing) | — | Query plan + full-endpoint latency at 200K on local seed at migration HEAD. |
| Corpus-repair verification (`036`) | Database (live/shadow gate) | — | Verify-only; no new migration. |

## Standard Stack

No new dependencies. This phase is a pure application-code cutover over the existing stack (per CONTEXT and milestone rule "zero new dependencies").

### Core (already present, verified in code this session)
| Component | Location | Purpose | Why Standard |
|-----------|----------|---------|--------------|
| `stage_status.py` clause builders | `src/phaze/services/stage_status.py` | `done`/`failed`/`inflight`/`domain_completed`/`stage_status_case`/`dedup_resolved`/`awaiting_candidate` `ColumnElement[bool]` builders | [VERIFIED: codebase] Phase 78 single-source predicate module; drift-locked to `enums/stage.py`. |
| `enums/stage.py` truth model | `src/phaze/enums/stage.py` | `eligible()`, `resolve_status()`, `ELIGIBLE_AFTER_FAILURE`, `FAILURE_IS_TERMINAL`, `ELIGIBILITY_DAG` | [VERIFIED: codebase] DB-free; the Python side `eligible_clause` mirrors. |
| `get_stage_progress` | `services/pipeline.py:302` | Per-DAG-node derived counts w/ `_safe_count` degrade | [VERIFIED: codebase] Already output-table-derived; extend, don't replace. |
| DERIV-04 equivalence harness | `tests/integration/test_stage_status_equivalence.py` | Parametrized SQL⇔Python drift-lock, real-PG | [VERIFIED: codebase] Extend with `ELIGIBLE_CASES`. |
| AST source-scan guard pattern | `tests/shared/test_dedup_fingerprint_source_scan.py` | Mutation-tested anti-drift over source | [VERIFIED: codebase] Phase 84 D-14 model to mirror. |
| Behavioral divergence guard pattern | `tests/integration/test_dedup_divergence.py` | Inconsistent-corpus "derived reader wins" test | [VERIFIED: codebase] Phase 84 model to mirror. |

**Installation:** none. `uv sync` covers the existing environment.

## Package Legitimacy Audit

Not applicable — this phase installs no external packages. All work composes existing first-party modules. [VERIFIED: codebase — `CLAUDE.md` "zero new dependencies" milestone rule + CONTEXT out-of-scope list].

## Architecture Patterns

### System Data Flow (the cutover surface)

```
                          ┌─────────────────────────────────────────────┐
                          │  enums/stage.py  (DB-free truth model)        │
                          │  eligible(), ELIGIBLE_AFTER_FAILURE           │
                          └───────────────▲─────────────────────────────┘
                                          │  DERIV-04 equivalence lock
                                          │  (test_stage_status_equivalence.py
                                          │   + NEW ELIGIBLE_CASES)
                          ┌───────────────┴─────────────────────────────┐
                          │  services/stage_status.py                    │
                          │  done/failed/inflight/dedup_resolved clauses │
                          │  + NEW eligible_clause(stage)                │
                          │  + stage_status_case(stage)                  │
                          └───┬───────────────────────────┬─────────────┘
                              │                            │
        eligible_clause ∧ ~dedup ∧ file_type        stage_status_case GROUP BY
                              │                            │
              ┌───────────────┴──────────┐        ┌────────┴───────────────────┐
              ▼               ▼           ▼        ▼                            ▼
   get_metadata_pending  get_fp_pending  analyze set     get_stage_progress   (get_pipeline_stats
   (:1370)               (:1403)         get_discovered_  (:302) 4-bucket       GROUP BY state  ── REMOVED)
              │               │          _files_with_     enrich nodes         │
              │               │          duration(:1098)       │               │ keys re-expressed
              ▼               ▼               ▼                 ▼               ▼  from get_stage_progress
   trigger_metadata_*  trigger_fingerprint_*  /api/v1/analyze   routers/pipeline.py         stats_bar.html
   (routers/pipeline)  (routers/pipeline)    /pipeline/analyze  _build_dag_context          (key remap)
   + reenqueue.py (Phase 80 recovery producer — shares the SAME helpers, D-03 anti-drift)   notYetEnriched
                                                                                            = metadata.total − done
```

### Pattern 1: `eligible_clause(stage)` — the new SQL builder (OQ1)
**What:** SQL twin of the Python `eligible()`, defined ONLY for the three enrich stages (mirroring `domain_completed_clause`'s enrich-only guard).
**Composition (drives off `ELIGIBLE_AFTER_FAILURE`, never inlines the analyze carve-out):**
```python
# Source: services/stage_status.py (proposed — mirrors enums/stage.py:237-243)
from phaze.enums.stage import ELIGIBLE_AFTER_FAILURE, Stage
from sqlalchemy import and_, not_

def eligible_clause(stage: Stage) -> ColumnElement[bool]:
    """SQL twin of enums.stage.eligible() for the three ENRICH stages only.

    Python truth (enums/stage.py:243):
      status not in (DONE, IN_FLIGHT) and (status != FAILED or ELIGIBLE_AFTER_FAILURE[stage])

    stage_status_case precedence is in_flight ≻ done ≻ failed ≻ not_started, so:
      status NOT in (DONE, IN_FLIGHT)  ==  ~inflight_clause ∧ ~done_clause
      the FAILED carve-out            ==  (drop ~failed_clause when ELIGIBLE_AFTER_FAILURE True)

    => metadata/fingerprint (True):  ~inflight_clause ∧ ~done_clause
       analyze (False):              ~inflight_clause ∧ ~done_clause ∧ ~failed_clause
    """
    if stage not in ELIGIBLE_AFTER_FAILURE:  # same enrich-only guard shape as domain_completed_clause
        got = getattr(stage, "value", stage)
        raise ValueError(f"eligible_clause is defined only for the enrich stages ...; got {got!r}")
    conjuncts = [not_(inflight_clause(stage)), not_(done_clause(stage))]
    if not ELIGIBLE_AFTER_FAILURE[stage]:      # analyze: a FAILED analyze is terminal (ELIG-03)
        conjuncts.append(not_(failed_clause(stage)))
    return and_(*conjuncts)
```
**Why this is provably equivalent to the Python truth table:**
- `status not in (DONE, IN_FLIGHT)` maps to `~inflight_clause ∧ ~done_clause` **because** the CASE precedence (`in_flight ≻ done ≻ failed ≻ not_started`, `stage_status_case`) makes those two clauses mutually exclusive per file; a file is neither IN_FLIGHT nor DONE iff both negations hold. A FAILED or NOT_STARTED file satisfies both negations — exactly the two states the Python predicate leaves eligible for metadata/fingerprint.
- For analyze (`ELIGIBLE_AFTER_FAILURE[ANALYZE] = False`), adding `~failed_clause` excludes the FAILED state, leaving only NOT_STARTED — matching `status != FAILED` collapsing the OR to eligible-iff-NOT_STARTED.
- **`has_approved_proposal` is irrelevant here** — it only affects `eligible(APPLY)` (`enums/stage.py:244-245`), and `eligible_clause` is enrich-only, so the signature stays a clean single `stage` param with no approval flag. [VERIFIED: codebase, `enums/stage.py:215,244`]

**Caller-join note:** like `done_clause`/`failed_clause`, `eligible_clause` uses correlated `~exists(... == FileRecord.id)` internally, so the enclosing query MUST select from / join `FileRecord` (the pending-set queries already do). No `CloudJob` join is needed at the `eligible_clause` level — `inflight_clause(ANALYZE)`'s cloud-sidecar span is inside `inflight_clause` itself (see OQ2c). *(Verify during planning: confirm `inflight_clause(ANALYZE)` already composes the `cloud_job` states — see Open Questions Q-A.)*

### Pattern 2: Extend the DERIV-04 harness with `ELIGIBLE_CASES` (OQ1)
**What:** Add a third parametrized matrix to `tests/integration/test_stage_status_equivalence.py` proving `eligible_clause` SQL == `eligible()` Python == expected, reusing the EXISTING seed fns.
**Example:**
```python
# Source: extends tests/integration/test_stage_status_equivalence.py (mirrors DOMAIN_COMPLETED_CASES:445)
ELIGIBLE_CASES: list[tuple[Stage, Callable[[AsyncSession], Awaitable[uuid.UUID]], bool]] = [
    # metadata: eligible when NOT_STARTED or FAILED; not when DONE or IN_FLIGHT (ELIG-01/04)
    (Stage.METADATA, seed_metadata_none, True),
    (Stage.METADATA, seed_metadata_done, False),
    (Stage.METADATA, seed_metadata_failed_only, True),   # ELIGIBLE_AFTER_FAILURE True
    (Stage.METADATA, seed_metadata_inflight, False),
    # fingerprint: same shape (ELIG-04 — failed stays eligible)
    (Stage.FINGERPRINT, seed_fp_none, True),
    (Stage.FINGERPRINT, seed_fp_success, False),
    (Stage.FINGERPRINT, seed_fp_success_and_failed, False),
    (Stage.FINGERPRINT, seed_fp_failed_only, True),       # DERIV-05 failed-only stays eligible
    (Stage.FINGERPRINT, seed_fp_inflight, False),
    # analyze: eligible ONLY when NOT_STARTED — failed is TERMINAL (ELIG-03, 44.5K guard)
    (Stage.ANALYZE, seed_analysis_none, True),
    (Stage.ANALYZE, seed_analysis_partial, True),         # completed_at NULL → not_started → eligible
    (Stage.ANALYZE, seed_analysis_completed, False),
    (Stage.ANALYZE, seed_analysis_failed, False),         # ← the ELIG-03 carve-out; RED if the analyze
                                                          #   ~failed_clause conjunct is dropped
    (Stage.ANALYZE, seed_analysis_failed_inflight, False),
]

async def eval_sql_eligible(session, stage, file_id) -> bool:
    from phaze.services.stage_status import eligible_clause  # lazy: keeps --co green in RED state
    result = await session.execute(select(eligible_clause(stage)).where(FileRecord.id == file_id))
    return bool(result.scalar_one())

@pytest.mark.parametrize("stage,seed_fn,expected", ELIGIBLE_CASES)
async def test_eligible_sql_equals_python(db_session, stage, seed_fn, expected) -> None:
    file_id = await seed_fn(db_session)
    sql_eligible = await eval_sql_eligible(db_session, stage, file_id)
    py_status = resolve_status(stage, await load_scalars(db_session, stage, file_id))
    py_eligible = eligible({stage: py_status}, stage)
    assert sql_eligible == py_eligible == expected
```
The single load-bearing anti-drift cell is `(Stage.ANALYZE, seed_analysis_failed, False)`: it goes RED if a future edit drops the analyze `~failed_clause` conjunct (the ELIG-03 44.5K over-enqueue guard). The `db_session` fixture, `_new_file`, seed fns, `load_scalars`, and lazy-import RED-state idiom are ALL already present — the extension is additive, no fixture work. [VERIFIED: codebase, lines 116-412]

### Pattern 3: Four-bucket via one GROUP BY per enrich stage (OQ4)
**What:** A degrade-safe helper that runs `GROUP BY stage_status_case(stage)` scoped to music/video files, returning `{not_started, in_flight, done, failed}` (zero-filled), so the four buckets **sum to** `music_video_total` by construction.
```python
# Source: proposed for services/pipeline.py (mirrors _safe_count degrade discipline :282)
async def _safe_bucket_counts(session, stage: Stage) -> dict[str, int]:
    out = {s.value: 0 for s in Status}  # not_started/in_flight/done/failed → 0
    stmt = (
        select(stage_status_case(stage).label("st"), func.count(FileRecord.id))
        .where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
        .group_by(stage_status_case(stage))
    )
    try:
        for label, n in (await session.execute(stmt)).all():
            if label in out:
                out[label] = int(n)
    except Exception:
        logger.warning("stage_bucket_degraded", stage=stage.value, exc_info=True)
        try: await session.rollback()
        except Exception: logger.warning("stage_bucket_rollback_failed", stage=stage.value, exc_info=True)
    return out  # sums to music_video_total on the happy path; all-zero on degrade
```
Then in `get_stage_progress`, the three enrich nodes become e.g. `{**buckets, "total": music_video_total}` (flat shape recommended — see Open Question Q-B for nested-vs-flat, which is Claude's discretion). Downstream nodes unchanged.
**Invariant + test:** `not_started + in_flight + done + failed == total` per enrich stage on a consistent corpus. Note the degrade edge: if `_safe_bucket_counts` degrades to all-zero while `music_video_total` is nonzero, the sum invariant is intentionally violated (fail-safe to zero, never 500) — the invariant test must assert it on a **healthy** query only. [VERIFIED: codebase, `stage_status_case` :265, `_safe_count` :282, `MUSIC_VIDEO_TYPES` :45]

### Anti-Patterns to Avoid
- **Inlining the analyze carve-out** in `eligible_clause` (`if stage is ANALYZE`) instead of driving off `ELIGIBLE_AFTER_FAILURE[stage]` — reintroduces the exact two-place-truth drift the milestone exists to kill (D-00d).
- **Baking `dedup_resolved_clause()` into `eligible_clause`** — dedup is a file-level predicate; Phase 84 deliberately kept it out of the `Stage` ladders and the DERIV-04 harness (D-03). Compose it at the `pipeline.py` query level.
- **Adding `~inflight_clause` to `domain_completed_clause`** — documented TRAP (`stage_status.py:214-221`, `test_stage_status_equivalence.py:429-444`); silently disables the secondary over-enqueue net. Not in scope here but do not touch it.
- **Line-oriented `grep` guard** instead of AST walk — Phase 83 shipped two toothless guards (project memory `feedback_mutation_test_guard_tests`); mutation-test every syntactic form.
- **A second derived-counting path** (`get_enrich_stage_buckets()`) — rejected by D-04; extend `get_stage_progress`.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-stage eligibility SQL | Inline `~done ∧ ~inflight (∧ ~failed)` in each pending set | `eligible_clause(stage)` in `stage_status.py` | One source of truth, DERIV-04-locked; three inline copies drift (D-01). |
| 4-way status CASE | A fresh CASE in `get_stage_progress` | `stage_status_case(stage)` | Already locked equal to the Python resolver; single CASE definition also feeds Phase 87. |
| dedup exclusion | `state != DUPLICATE_RESOLVED` | `~dedup_resolved_clause()` | State read is exactly what the cutover removes; marker is authority (Phase 84). |
| in-flight (incl. cloud) detection | Re-spell ledger key / re-list cloud states | `inflight_clause(stage)` | Ledger-authoritative (D-00b); analyze span already includes the cloud sidecar (D-00c). |
| Degrade-safe counting | Bare `session.execute` in the poll path | `_safe_count` / new `_safe_bucket_counts` w/ SAVEPOINT-or-rollback | The 5s poll must never 500 (INFLIGHT-02 discipline). |
| Anti-drift proof | `grep` for `.state` | AST `ast.walk` scan + mutation tests | Phase-83 grep guards were toothless (memory `feedback_mutation_test_guard_tests`). |

**Key insight:** Every hard part of this phase was already solved and locked in Phases 77–84. Phase 82's risk is *composition correctness* and *query-plan scale*, not new logic.

## Detailed Findings on the Open Questions

### OQ1 — `eligible_clause` composition + harness extension
Covered by Patterns 1 & 2 above. Summary: enrich-only guard, drive off `ELIGIBLE_AFTER_FAILURE`, compose `~inflight ∧ ~done (∧ ~failed for analyze)`; extend the equivalence harness with `ELIGIBLE_CASES` reusing existing seed fns. `has_approved_proposal` is not relevant (apply-only). [VERIFIED: codebase]

### OQ2 — The three pending-set rewrites (concrete diffs)

**(a) `get_metadata_pending_files` (`pipeline.py:1370`)**
Current: `select(FileRecord).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))` — returns ALL music/video (stateless idempotent set).
Target:
```python
stmt = select(FileRecord).where(
    FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
    eligible_clause(Stage.METADATA),
    ~dedup_resolved_clause(),
)
```
Effect: narrows from "all music/video" to "not-metadata-done, not-metadata-in-flight, not-dedup-resolved". A metadata-done file drops out (previously re-enqueued relying on the deterministic-key dedup); a metadata-FAILED file stays in (ELIGIBLE_AFTER_FAILURE True → auto-retry). [VERIFIED: codebase :1379]

**(b) `get_fingerprint_pending_files` (`pipeline.py:1403`) — the UNION collapses**
Current: `get_files_by_state(METADATA_EXTRACTED)` UNION (join FingerprintResult status='failed' AND state != FINGERPRINTED), de-duped by id. The `state == METADATA_EXTRACTED` gate **is** the cross-stage deadlock.
Target:
```python
stmt = select(FileRecord).where(
    FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
    eligible_clause(Stage.FINGERPRINT),
    ~dedup_resolved_clause(),
)
result = await session.execute(stmt)
return list(result.scalars().all())
```
**The failed-retry UNION collapses** — `eligible_clause(FINGERPRINT)` is `~inflight ∧ ~done`, and `failed_clause(FINGERPRINT)` (one engine failed, none succeeded) is a NOT_STARTED-or-FAILED-derived not-done state, so a failed-fingerprint file already satisfies `~done_clause` and is included with no extra UNION. `ELIGIBLE_AFTER_FAILURE[FINGERPRINT] = True` means no `~failed_clause` conjunct, so the failed set is subsumed. The manual de-dup-by-id loop disappears (a single WHERE returns each file once). [VERIFIED: codebase :1418-1435, `failed_clause` :157-162, `ELIGIBLE_AFTER_FAILURE` :88] *(Verify during planning: confirm a `[failed]`-only fingerprint file is returned by the new query in a real-PG test — this is the concrete collapse proof.)*

**(c) Analyze set — `get_discovered_files_with_duration` (`pipeline.py:1098`)**
Current: `select(FileRecord, FileMetadata.duration).outerjoin(FileMetadata, ...).where(FileRecord.state == DISCOVERED)`.
Target — **KEEP the LEFT OUTER JOIN** (cloud duration-router reads `FileMetadata.duration`), swap the WHERE:
```python
stmt = (
    select(FileRecord, FileMetadata.duration)
    .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
    .where(
        FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
        eligible_clause(Stage.ANALYZE),
        ~dedup_resolved_clause(),
    )
)
```
- **`file_type IN MUSIC_VIDEO_TYPES` is NEWLY added** — the current `state == DISCOVERED` filter is file-type-agnostic; the derived analyze set must scope to music/video (D-01), matching the other two sets and `get_stage_progress`'s denominator. [VERIFIED: codebase :1106-1110]
- **Cloud-in-flight auto-exclusion (D-00c):** `inflight_clause(ANALYZE)` must span the `cloud_job` sidecar states (AWAITING_CLOUD/PUSHING/PUSHED/LOCAL_ANALYZING). **This is the single point to verify at plan-time** — Open Question Q-A. If confirmed, no extra clause is needed and the analyze set never routes a cloud-in-flight file.
- Effect: the set expands from "only DISCOVERED" to "every not-analyze-done, not-analyze-in-flight, not-terminally-failed, not-dedup-resolved music/video file" — including files that are metadata-done but not analyze-done (deadlock dissolved). A terminally-FAILED analyze is excluded (ELIG-03, the 44.5K guard) via the analyze `~failed_clause` conjunct.

**(d) Manual-trigger endpoint alignment (Phase-42 precedent):** All three helpers are consumed by BOTH the manual trigger endpoints AND the Phase-80 recovery producer (`reenqueue.py`), by design (`pipeline.py:1359-1367`, "ONE definition of pending per stage"). [VERIFIED: codebase grep]
- `trigger_metadata_extraction` (`routers/pipeline.py:1236`), `trigger_extraction_ui` (:1262) → `get_metadata_pending_files`.
- `trigger_fingerprint` (:1315), `trigger_fingerprint_ui` (:1354) → `get_fingerprint_pending_files`.
- `/api/v1/analyze` (:399), `/pipeline/analyze` (:719) → `get_discovered_files_with_duration`.
Because all three route through the shared helpers, narrowing the helper narrows manual + API + recovery in lockstep automatically — the Phase-42 anti-drift guarantee holds with no per-endpoint edit. **Recommendation:** keep this routing; add a regression asserting the manual trigger and recovery enqueue the SAME set (Phase-42 precedent). One nuance: `get_metadata_failed_files` (:1384, the FAIL-03 operator retry set) is a SEPARATE endpoint and stays as-is (it deliberately returns only failed rows). [VERIFIED: codebase]

### OQ3 — The SC#1 all-orderings test
**Goal:** prove a single file completes all three enrich stages in ANY order, each pending set independent, and detect the OLD deadlock (RED against pre-cutover code).
**Design:**
- **Location:** `tests/integration/` (real-PG, mirrors the `db_session` fixture in `test_dedup_divergence.py:27` / `test_stage_status_equivalence.py:78`). Suggest `tests/integration/test_enrich_pending_independence.py`.
- **Corpus:** one music/video `FileRecord` in `state='discovered'` with NO output rows.
- **Assertion matrix (the independence proof):** for each of the 6 permutations of {metadata, fingerprint, analyze}, drive the file through the stages in that order by writing the stage's OUTPUT row (metadata row / fingerprint success row / analysis row w/ `analysis_completed_at`) — NOT by mutating `FileRecord.state`. After each partial completion, assert:
  - the just-completed stage's pending set EXCLUDES the file;
  - the two not-yet-done stages' pending sets STILL INCLUDE the file (independence);
  - after all three, all three pending sets exclude it.
- **Deadlock-detection (must go RED on pre-cutover code):** the decisive cell is asserting `get_discovered_files_with_duration` INCLUDES a file whose metadata is already done (a fingerprint/analyze-first ordering). Pre-cutover, `get_discovered_files_with_duration` filters `state == DISCOVERED`; once metadata extraction stamps `state = METADATA_EXTRACTED`, the file vanishes from the analyze set — the exact deadlock. So a cell that (1) writes a metadata row AND advances `state` to `METADATA_EXTRACTED` (dual-write reality), then (2) asserts the file is STILL in the analyze pending set, is GREEN post-cutover (derived) and RED pre-cutover (state-gated). Similarly for `get_fingerprint_pending_files` currently gated on `state == METADATA_EXTRACTED`: assert a file with `state='discovered'` but no fingerprint row IS in the fingerprint pending set — RED pre-cutover (only METADATA_EXTRACTED files appear), GREEN post-cutover.
- **Dual-write caveat (D-00a):** because writers still stamp `.state`, the test must set `state` to whatever the real writer would set (e.g. `METADATA_EXTRACTED` after metadata) so the assertion proves the *derived reader ignores state*, not that state is absent. This mirrors `test_dedup_divergence.py`'s inconsistent-corpus approach.

### OQ4 — `get_stage_progress` four-bucket + `get_pipeline_stats` removal (concrete shape)

**Four-bucket:** Pattern 3 above — one `GROUP BY stage_status_case(stage)` per enrich stage via `_safe_bucket_counts`, flat `{not_started, in_flight, done, failed, total}` recommended (Q-B is discretion). Each bucket is degrade-safe (SAVEPOINT-or-rollback all-zero). `total` stays `music_video_total`. Downstream nodes keep `{done, total}`.

**`get_pipeline_stats` removal — full consumer inventory (READ-02 / D-05):** `get_pipeline_stats` (`services/pipeline.py:61`) has exactly THREE call sites, ALL in `routers/pipeline.py`:
1. `:240` — inside `_build_dag_context`, only for `notYetEnriched = max(stats["discovered"] − stats["metadata_extracted"], 0)`.
2. `:485` — `build_dashboard_context` (feeds `stats_bar.html` + `queue_progress_percent(stats["analyzed"], ...)` at :506).
3. `:629` — `pipeline_stats_partial` (the `/pipeline/stats` HTMX poll; feeds `stats_bar.html` + `queue_progress_percent(stats["analyzed"], ...)` at :636).
[VERIFIED: codebase grep — no other importer; `shell.py` imports `get_stage_progress` and the two pending helpers but NOT `get_pipeline_stats`.]

**The seven consumed keys and their derived re-expression** (all from `get_stage_progress`, which every caller already computes via `_build_dag_context`):
| Old key (FileState-derived) | Consumed at | Derived re-expression |
|---|---|---|
| `stats["discovered"]` | :241 notYetEnriched, `stats_bar.html:3,47` | `stage_progress["discovery"]["done"]` (COUNT all files) |
| `stats["metadata_extracted"]` | :241 notYetEnriched, `stats_bar.html:48` | `stage_progress["metadata"]["done"]` (⇒ notYetEnriched = `metadata.total − metadata.done`, D-05) |
| `stats["fingerprinted"]` | `stats_bar.html:7` | `stage_progress["fingerprint"]["done"]` |
| `stats["analyzed"]` | :506/:636 `queue_progress`, `stats_bar.html:11,49` | `stage_progress["analyze"]["done"]` |
| `stats["proposal_generated"]` | `stats_bar.html:15` | `stage_progress["proposals"]["done"]` |
| `stats["approved"]` | `stats_bar.html:19` | `stage_progress["execute"]["total"]` (distinct approved proposals) |
| `stats["executed"]` | `stats_bar.html:23` | `stage_progress["execute"]["done"]` |
[VERIFIED: codebase — `stats_bar.html` lines 3,7,11,15,19,23,47,48,49; `routers/pipeline.py` 241,506,636]

**`stats_bar.html` key remap (Claude's discretion, D-05):** the template currently reads `stats.<filestate>`. Recommended: pass a small derived dict (e.g. `stats_bar = {"discovered": ..., "fingerprinted": ..., ...}` built from `stage_progress`) so the template's SIX visible cards + THREE OOB `x-init` store writes (`stats.discovered`, `stats.metadata_extracted`, `stats.analyzed` at lines 47-49) remap with minimal churn. Note the OOB store writes push into `$store.pipeline.discovered / .metadataExtracted / .analyzed` — those Alpine store keys can stay; only their server-side source changes. `queue_progress_percent(stats["analyzed"], ...)` becomes `queue_progress_percent(stage_progress["analyze"]["done"], ...)`.

**Semantic shift to flag in SUMMARY (per CONTEXT "number changes are the fix"):** `stats.metadata_extracted` historically counted files in the *linear* `METADATA_EXTRACTED` state (a file leaves it on advancing to FINGERPRINTED/ANALYZED). The derived `metadata.done` counts *every* file with a metadata row regardless of downstream progress. These numbers legitimately differ post-cutover — this is the deadlock dissolving, not a regression.

### OQ5 — PERF-02 synthetic-seed harness + EXPLAIN ANALYZE methodology

**Existing reusable infrastructure:** NONE for 200K seeding. [VERIFIED: codebase — `scripts/` has no seed/bench; `justfile` has `test-db`/`integration-test`/`db-*`/`shadow-compare` but no bench recipe.] A new seed harness is required. This is Claude's discretion (D-06); recommend a `scripts/seed_perf_corpus.py` (uv-run, idempotent, argument for N) + a `just perf-seed` / `just perf-explain` recipe pair in the `db` group.

**Seed shape (realistic per-stage coverage):** ~200K `FileRecord` (music/video types) at migration HEAD (`alembic upgrade head`, ≥036, so 032 partial indexes exist). Distribute output-table rows to mimic a mid-pipeline corpus so the derived anti-joins hit realistic selectivity, e.g.:
- ~70% with a `metadata` row (some `failed_at` set), ~55% with a `fingerprint_results` success row, ~40% with an `analysis` row (`analysis_completed_at` set), ~5% analyze `failed_at` (terminal), ~1% `cloud_job` awaiting/pushing/pushed rows, ~2% `dedup_resolution` markers, and a few thousand `scheduling_ledger` rows (in-flight). Bulk-insert via `execute` with `executemany`/COPY-style batches (asyncpg) to keep seeding minutes not hours.
- Seed both `state` (dual-write realism) and the output rows so the corpus is internally consistent (shadow gate stays green).

**Queries to EXPLAIN ANALYZE (the hot paths):**
1. `get_metadata_pending_files` SELECT.
2. `get_fingerprint_pending_files` SELECT.
3. `get_discovered_files_with_duration` SELECT (with the LEFT JOIN).
4. Each of the three `GROUP BY stage_status_case(stage)` four-bucket queries.
5. The full `/pipeline/stats` endpoint (end-to-end timing — it fans out many `_safe_count` reads + the three GROUP BYs).

**Method:** run `EXPLAIN (ANALYZE, BUFFERS, VERBOSE) <query>` via `session.execute(text("EXPLAIN (ANALYZE, BUFFERS) ..."))` or psql. Record actual time, rows, and — critically — confirm **Index Scan / Index Only Scan on the 032 partial indexes** (`ix_fprint_success`, `ix_analysis_completed`, `ix_analysis_failed`, `ix_metadata_failed`, `ix_cloud_job_awaiting`), NOT Seq Scan, for the `~exists` anti-joins. [VERIFIED: codebase — index names in `032_add_derived_status_schema.py:150-156`]. A Seq Scan on any of these at 200K is the signal DENORM-01 (D-07) may be needed.
**Endpoint timing:** hit `GET /pipeline/stats` against the seeded DB with `httpx.AsyncClient` (or `time curl`) N times, record p50/p95. PASS = `< ~1s` (D-07). Record the number in VERIFICATION **regardless** of pass/fail (the number licenses the YAGNI decision to skip DENORM-01).

**Env caveats:** test-DB on port **5433** (`test_db_port`), test-Redis **6380** (memory `reference_migrations_test_db_port`); the local full-suite flakes under colima VM pressure (memory `reference_local_fullsuite_colima_flake`) — run the perf seed/measure in isolation, not inside the full pytest run. Do NOT probe live lux (Alembic ~031, no 032 indexes → invalid plan, D-06). A live read-only COUNT (memory `reference_lux_readonly_pg_probe`) may only sanity-check the synthetic corpus's stage-coverage distribution.

### OQ6 — The anti-drift guard (mutation-tested, dual-write-aware)

Mirror Phase 84's TWO-part discipline exactly:

**(a) AST source-scan guard** (`tests/shared/test_pending_set_source_scan.py`, modeled on `test_dedup_fingerprint_source_scan.py`). Scope: the three pending-set functions in `services/pipeline.py`. Scan for `FileState.DISCOVERED` / `FileState.METADATA_EXTRACTED` / `FileState.FINGERPRINTED` in **READ positions only** — inside `ast.Compare` OR as ANY positional/keyword arg of `.where()`/`.filter()`/`.filter_by()`/`.having()`. Reuse the proven `_in_compare`, `_in_where_arg` (walks BOTH `Call.args` positionally AND `Call.keywords`, incl. `keyword.arg is None` splat), and `_filestate_occurrences` helpers verbatim. **Dual-write reality (D-00a):** a bare "state string absent" assertion is impossible because writers still stamp `.state`; but the three pending-set FUNCTIONS are pure readers with NO `.state=` write, so within those functions the invariant is "zero FileState READ occurrences of the enrich states" — cleaner than dedup.py's "exactly one write". **Mutation directions (permanent, hermetic — mutate crafted STRINGS not files):** (1) positional `.where(a, FileRecord.state == FileState.METADATA_EXTRACTED)` → RED; (2) keyword `.filter_by(state=FileState.DISCOVERED)` → RED; (3) `.where(**{"whereclause": FileRecord.state == ...})` splat → RED; (4) a docstring mention of `METADATA_EXTRACTED` → GREEN (not an occurrence). Break the real source, watch RED, restore — proving teeth (memory `feedback_mutation_test_guard_tests`).

**(b) Behavioral divergence guard** (`tests/integration/test_pending_set_divergence.py`, modeled on `test_dedup_divergence.py`). Seed an **inconsistent** corpus where `state` and the derived status DISAGREE, and assert the derived reader wins:
- **File A:** metadata output row present (derived metadata=DONE) BUT `state='discovered'` → must be EXCLUDED from `get_metadata_pending_files` (derived wins; stale `discovered` state must NOT resurface it).
- **File B:** NO fingerprint row (derived fingerprint=NOT_STARTED, eligible) BUT `state='analyzed'` (a downstream linear state that pre-cutover would exclude it from the fingerprint set) → must be INCLUDED in `get_fingerprint_pending_files`.
- **File C:** NO analysis row (derived analyze eligible) BUT `state='fingerprinted'` → must be INCLUDED in the analyze set.
Each assertion is designed so reverting THAT reader's predicate back to a `FileRecord.state`-based filter inverts it (the `MUTATION:` comment per test). This is the behavioral complement the source scan cannot see. Cover all three pending sets.

### OQ7 — Plan/PR decomposition (recommended waves)

Four seams, small blast-radius per PR (milestone rule). Recommended dependency ordering:

| Wave | Plan | Scope | Depends on | Coupling notes |
|------|------|-------|-----------|----------------|
| 1 | 82-01 | `eligible_clause(stage)` in `stage_status.py` + `ELIGIBLE_CASES` harness extension | — (pure add; behind shadow gate) | Foundational; every pending-set cut composes it. Purely additive — no reader wired yet, safe to land first. |
| 2 | 82-02 | Three pending-set cutovers + `~dedup_resolved_clause` + `file_type` scope + AST source-scan guard + behavioral divergence guard + SC#1 all-orderings test | 82-01 | The behavior-changing seam (READ-01). Manual-trigger + recovery align automatically via shared helpers. Verify D-02 live/shadow gate (`036` applied, analyze invariant clean) here. |
| 3 | 82-03 | `get_stage_progress` four-bucket (+`_safe_bucket_counts`) + `get_pipeline_stats` removal + three-caller migration + `stats_bar.html` key remap + sums-to-total invariant test | 82-01 (uses `stage_status_case`, already on main; not strictly 82-02 but sequence after to keep shadow gate reasoning simple) | READ-02. Independent file surface from 82-02 (services/pipeline.py counts vs pending sets) but same module — sequence to avoid merge churn. |
| 4 | 82-04 | PERF-02 synthetic-seed harness + EXPLAIN ANALYZE + endpoint timing + VERIFICATION record + DENORM-01 go/no-go | 82-02, 82-03 (measures the cutover queries) | Measurement deliverable; must run after both reader cutovers land so it measures the real hot paths. |

**Cross-seam coupling flags:** 82-02 and 82-03 both edit `services/pipeline.py` — pre-assign function ownership (pending sets vs `get_stage_progress`/`get_pipeline_stats`) to avoid add/add collisions if run in parallel (memory `reference_worktree_agents_spawn_at_main`); recommend sequential (2 → 3) given the shared module. All four sit behind the standing shadow-compare gate (D-00e) which must stay green after each.

## Common Pitfalls

### Pitfall 1: Forgetting the newly-required `file_type` scope on the analyze set
**What goes wrong:** `get_discovered_files_with_duration` currently has NO `file_type` filter (relies on `state == DISCOVERED`). Dropping the state filter without adding `file_type.in_(MUSIC_VIDEO_TYPES)` would enqueue non-music files for analysis.
**How to avoid:** add the `file_type` conjunct explicitly (D-01 mandates it on all three sets). Test with a non-music discovered file asserted ABSENT from the analyze set.

### Pitfall 2: Assuming `inflight_clause(ANALYZE)` already spans the cloud sidecar
**What goes wrong:** D-00c asserts the analyze pending set auto-excludes AWAITING_CLOUD/PUSHING/PUSHED/LOCAL_ANALYZING because `inflight_clause(ANALYZE)` composes them. But `inflight_clause` as read this session (`stage_status.py:176-193`) only checks the `scheduling_ledger` key — it does NOT visibly reference `cloud_job`. This must be CONFIRMED at plan-time (Open Question Q-A). If the cloud states are represented as ledger rows (Phase 83 may write a ledger row for held/pushing files) the claim holds; if not, the analyze set needs an explicit `~awaiting/pushing/pushed` conjunct.
**How to avoid:** read Phase 83's writer + `inflight_clause` carefully; add a real-PG regression seeding a `cloud_job(status='pushing')` file and asserting it is ABSENT from the analyze pending set. This is the single sharpest correctness risk in the phase.

### Pitfall 3: The four-bucket sum-to-total invariant broken by degrade
**What goes wrong:** a degraded `_safe_bucket_counts` returns all-zero while `total` (`music_video_total`) is nonzero → the invariant test flakes.
**How to avoid:** assert the invariant only on a healthy corpus/query; document the fail-safe-to-zero behavior; never make the invariant a runtime assertion in the poll path.

### Pitfall 4: `stats_bar.html` OOB store-write semantic drift
**What goes wrong:** the three OOB `x-init` writes (`stats_bar.html:47-49`) push into `$store.pipeline.discovered/.metadataExtracted/.analyzed` — driving the DAG canvas bindings + button `:disabled` gating. Remapping the server source but mis-naming the store key silently breaks the live poll gating.
**How to avoid:** keep the Alpine store keys stable; change only the server-side value source; assert the poll partial still emits the same three OOB ids.

### Pitfall 5: Live-vs-synthetic plan divergence for PERF-02
**What goes wrong:** measuring on prod/lux (Alembic ~031) yields Seq Scans (no 032 indexes) → a false "too slow → build DENORM-01" conclusion.
**How to avoid:** measure ONLY on a local corpus at migration HEAD; verify `EXPLAIN` shows the 032 partial indexes in use (D-06).

## Runtime State Inventory

This is a reader cutover, not a rename/refactor/migration phase — but the CONTEXT's D-02 makes a runtime-state check load-bearing, so:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `analysis.analysis_completed_at` on ~1001 pre-`036` production `analyzed` rows (would derive NOT-done and re-enqueue for 4h re-analysis). | **None new** — Phase 80's `036` already backfilled them. VERIFICATION asserts deploy target ≥036 AND `COUNT(files WHERE state='analyzed' AND analysis_completed_at IS NULL AND failed_at IS NULL) = 0` (D-02). |
| Live service config | n8n/Datadog/etc. — none relevant; this phase adds no service config. | None — verified by scope (pure code readers). |
| OS-registered state | None. | None — verified: no schedulers/pm2/systemd touched. |
| Secrets/env vars | Test-DB env `TEST_DATABASE_URL` (5433) + `MIGRATIONS_TEST_DATABASE_URL` + `PHAZE_REDIS_URL` (6380) for the equivalence/divergence real-PG tests. | Export both DB URLs before running integration tests (memory `reference_migrations_test_db_port`). No new secrets. |
| Build artifacts | None — no packaging/pyproject change. | None. |

**The canonical question — "after every file is updated, what runtime systems still have the old string cached?":** the only durable-state risk is the analyze-corpus `analysis_completed_at` backfill, already owned by `036`. Phase 82 verifies, never re-repairs. [VERIFIED: codebase + ROADMAP Phase 80 §036 + memory `project_analyzed_invariant_red_on_deploy` (now stale per D-02)].

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (ephemeral test-DB) | equivalence/divergence/all-orderings real-PG tests + PERF-02 seed | via `just test-db` (postgres:18-alpine :5433) | 18 | — (integration tests `pytest.skip` when PG down) |
| uv / Python 3.14 | all work | ✓ (project constraint) | 3.14 | — |
| Alembic HEAD (≥036) | PERF-02 seed at migration HEAD; D-02 verify | `just db-upgrade` | ≥036 | — (blocking for PERF-02) |
| Redis (ephemeral) | not directly (SAQ not read here) | via `just test-db` (:6380) | 7+ | — |

**Missing dependencies with no fallback:** none — the local ephemeral Postgres provides everything. PERF-02 needs the DB at migration HEAD; that is a `just db-upgrade` step, not a missing tool.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (via `uv run pytest`) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) + `tests/conftest.py`; per-bucket `tests/buckets.json` |
| Quick run command | `uv run pytest tests/integration/test_stage_status_equivalence.py -x` (or the specific new test file) |
| Full suite command | `just integration-test` (spins ephemeral PG :5433 + Redis :6380, exports both DB URLs) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| READ-01 | `eligible_clause` SQL == Python `eligible()` for every enrich cell | integration (real-PG) | `uv run pytest tests/integration/test_stage_status_equivalence.py -k eligible -x` | ⚠️ extend existing file (add `ELIGIBLE_CASES`) |
| READ-01 | One file completes all 3 enrich stages in ANY order; sets independent; RED pre-cutover | integration | `uv run pytest tests/integration/test_enrich_pending_independence.py -x` | ❌ Wave 0 |
| READ-01 | Derived pending readers win over stale `state` (inconsistent corpus) | integration | `uv run pytest tests/integration/test_pending_set_divergence.py -x` | ❌ Wave 0 |
| READ-01 | No `FileState` READ reintroduced in the 3 pending sets (AST, mutation-tested) | unit (DB-free) | `uv run pytest tests/shared/test_pending_set_source_scan.py -x` | ❌ Wave 0 |
| READ-01 | Cloud-in-flight file excluded from analyze set (D-00c) | integration | `uv run pytest tests/integration/test_enrich_pending_independence.py -k cloud -x` | ❌ Wave 0 |
| READ-02 | Four-bucket per enrich stage sums to total; visible failed count | integration | `uv run pytest tests/shared/routers/test_pipeline.py -k bucket -x` (or new) | ❌ Wave 0 |
| READ-02 | `get_pipeline_stats` `GROUP BY state` removed; stats keys derived; 3 callers migrated | unit + integration | `uv run pytest tests/shared/routers/test_pipeline.py -x` | ⚠️ extend existing |
| PERF-02 | `/pipeline/stats` < ~1s at 200K; 032 indexes used | manual/bench (recorded) | `just perf-seed && just perf-explain` (new) | ❌ Wave 0 |
| D-02 | Deploy target ≥036 AND analyze invariant clean | live/shadow gate (recorded) | `just shadow-compare` + a COUNT probe | existing gate; add assertion to VERIFICATION |

### Sampling Rate
- **Per task commit:** the specific new/changed test file (`uv run pytest <file> -x`).
- **Per wave merge:** `just integration-test` (or `just test-bucket integration`) — the real-PG suite must be green.
- **Phase gate:** full suite green + shadow-compare green (D-00e) + PERF-02 number recorded in VERIFICATION before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/integration/test_enrich_pending_independence.py` — SC#1 all-orderings + cloud-exclusion (READ-01)
- [ ] `tests/integration/test_pending_set_divergence.py` — behavioral state/derived-disagreement guard (READ-01)
- [ ] `tests/shared/test_pending_set_source_scan.py` — AST mutation-tested source guard (READ-01)
- [ ] `tests/integration/test_stage_status_equivalence.py` — ADD `ELIGIBLE_CASES` + `test_eligible_sql_equals_python` (READ-01)
- [ ] Four-bucket sum-to-total invariant test (READ-02) — extend `tests/shared/routers/test_pipeline.py` or new `tests/integration/test_stage_progress_buckets.py`
- [ ] `scripts/seed_perf_corpus.py` + `just perf-seed`/`just perf-explain` recipes (PERF-02)
- [ ] No framework install needed — pytest/pytest-asyncio present; ephemeral PG via `just test-db`.

## Security Domain

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | No auth surface touched (internal reader cutover). |
| V3 Session Management | no | — |
| V4 Access Control | no | Admin-only single-user tool; no new endpoints. |
| V5 Input Validation | yes (low) | All queries are pure ORM / bound params / `ColumnElement` composition; NO f-string SQL (project rule T-42-03). The only raw SQL in the touched modules is the static SAVEPOINT-isolated `saq_detail`/`_STAGE_BUSY_SQL` with a fixed status allowlist (no interpolation) — untouched by this phase. |
| V6 Cryptography | no | — |

### Known Threat Patterns for {SQLAlchemy async / FastAPI reader}
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection via dynamic filter | Tampering | Pure ORM `ColumnElement` builders + bound params; no string interpolation (verified across `stage_status.py` / `pipeline.py`). |
| DoS via hot-poll query blowup at scale | Denial of Service | PERF-02 measurement + `_safe_count`/SAVEPOINT degrade (never 500 the 5s poll); DENORM-01 escape hatch if measured too slow. |
| Over-enqueue (44.5K incident class) | (availability/cost) | ELIG-03 terminal-analyze via analyze `~failed_clause` in `eligible_clause`; ledger-authoritative in-flight (D-00b); the `ELIGIBLE_CASES` anti-drift cell `(ANALYZE, seed_analysis_failed, False)`. |

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Linear `FileRecord.state` gates pending sets + `GROUP BY state` counts | Derived per-stage `stage_status` from output tables | This phase (READ-01/02) | Cross-stage deadlock dissolves; per-stage failed counts visible; per-stage numbers legitimately jump. |
| `get_fingerprint_pending_files` = state-gate UNION failed-retry | `eligible_clause(FINGERPRINT)` (UNION collapses) | This phase | One WHERE, no de-dup loop. |

**Deprecated/outdated:**
- Auto-memory `project_analyzed_invariant_red_on_deploy` ("nothing in 032-035 backfills `analysis_completed_at`") is **STALE** — Phase 80's `036` (outside 032-035) does the backfill (D-02). Do not re-add a backfill.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `inflight_clause(ANALYZE)` already spans the `cloud_job` sidecar states so the analyze pending set auto-excludes cloud-in-flight files with no extra clause (D-00c). Read of `inflight_clause` (`stage_status.py:176-193`) shows only a `scheduling_ledger` key check — the cloud span was not directly visible this session. | OQ2c, Pitfall 2, Open Q-A | If wrong, the analyze pending set would re-route AWAITING_CLOUD/PUSHING files → double-dispatch. Planner MUST verify Phase 83's writer + `inflight_clause` and add an explicit cloud-state conjunct + regression if the span is not already present. |
| A2 | The four buckets sum to `music_video_total` because `stage_status_case` scoped to music/video partitions every such file into exactly one of the 4 states. Holds by CASE exhaustiveness; assumed no music/video file is silently excluded by a join. | OQ4, Pattern 3 | Low — CASE `else_=not_started` guarantees exhaustiveness. A failed sum test would surface it. |
| A3 | `stats.approved` (FileState.APPROVED count) is best re-expressed as `stage_progress["execute"]["total"]` (distinct approved proposals). The two are semantically close but not identical (a file can have an approved proposal without being in APPROVED state under the linear model). | OQ4 table | Medium — cosmetic count on the stats bar. Confirm the desired semantic at plan-time; both are defensible and D-05 licenses output-table derivation. |
| A4 | No existing 200K seed/bench harness exists to reuse. | OQ5 | Low — grep of `scripts/`+`justfile` confirmed absence; a new harness is Claude's discretion (D-06). |

## Open Questions

1. **Q-A (HIGH priority): Does `inflight_clause(ANALYZE)` compose the `cloud_job` sidecar states?**
   - What we know: D-00c asserts it does; `inflight_clause` as read only checks `scheduling_ledger`.
   - What's unclear: whether Phase 83 writes a ledger row for AWAITING_CLOUD/PUSHING/PUSHED/LOCAL_ANALYZING files (making the ledger check sufficient) OR whether the analyze pending set needs an explicit cloud-state exclusion.
   - Recommendation: planner reads Phase 83's writer (`hold_awaiting_cloud`) + `inflight_clause` in full; add a real-PG regression seeding a `cloud_job(status='pushing')` file and asserting analyze-pending exclusion (Pitfall 2). Treat A1 as unverified until this test is green.

2. **Q-B (discretion): Four-bucket return shape — nested `{node: {bucket: int}}` vs flat.**
   - Recommendation: flat `{not_started, in_flight, done, failed, total}` per enrich node keeps the existing `{done, total}` downstream shape and minimizes `_build_dag_context` churn; either satisfies D-04's sum-and-degrade constraints.

3. **Q-C (discretion): `stats_bar.html` — pass a derived `stats_bar` dict vs rename `stats` keys.**
   - Recommendation: build a small derived dict from `get_stage_progress` in the two context builders so the template edit is a mechanical key remap; keep the three OOB Alpine store keys stable (Pitfall 4).

## Sources

### Primary (HIGH confidence — read this session from `main`)
- `src/phaze/enums/stage.py` — `eligible()`, `ELIGIBLE_AFTER_FAILURE`, `FAILURE_IS_TERMINAL`, `resolve_status`, `ELIGIBILITY_DAG` (full file).
- `src/phaze/services/stage_status.py` — all clause builders + `stage_status_case` + D-01 decision record (full file).
- `src/phaze/services/pipeline.py` — `get_pipeline_stats:61`, `get_stage_progress:302`, `_safe_count:282`, `MUSIC_VIDEO_TYPES:45`, pending sets `:1098/:1370/:1403`, `get_metadata_failed_files:1384`.
- `src/phaze/routers/pipeline.py` — `_build_dag_context:240`, `build_dashboard_context:485`, `pipeline_stats_partial:629`, trigger endpoints `:1227-1354`, analyze callers `:399/:719`.
- `src/phaze/templates/pipeline/partials/stats_bar.html` — consumed keys (full file).
- `tests/integration/test_stage_status_equivalence.py` — DERIV-04 harness (full file).
- `tests/shared/test_dedup_fingerprint_source_scan.py` — AST guard pattern (full file).
- `tests/integration/test_dedup_divergence.py` — behavioral divergence pattern (head).
- `alembic/versions/032_add_derived_status_schema.py:150-156` — partial index names.
- `justfile` — `test-db`/`integration-test`/`db-*`/`shadow-compare` recipes; ports 5433/6380.
- `.planning/phases/82-counts-pending-set-cutover/82-CONTEXT.md`, `.planning/REQUIREMENTS.md` (READ-01/02, PERF-02, ELIG/DERIV/INFLIGHT/DENORM), `.planning/ROADMAP.md` (Phase 82 + upstream phases).

### Secondary (MEDIUM confidence — project auto-memory, cross-checked against code where possible)
- `reference_migrations_test_db_port` (5433/6380 env), `reference_local_fullsuite_colima_flake`, `feedback_mutation_test_guard_tests`, `project_analyzed_invariant_red_on_deploy` (noted STALE per D-02), `reference_lux_readonly_pg_probe`.

### Tertiary (LOW confidence)
- None — no web/library research was required for this code-internal phase.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — zero new deps; every component read from `main` this session.
- Architecture (`eligible_clause`, four-bucket, pending diffs, consumer inventory): HIGH — direct code reads; equivalence proof mechanical.
- Cloud-in-flight auto-exclusion (A1/Q-A): MEDIUM — asserted by D-00c but not directly verified in `inflight_clause`; flagged for plan-time verification.
- PERF-02 methodology: HIGH on method, MEDIUM on seed-distribution specifics (discretion).
- Pitfalls / anti-drift: HIGH — mirrors proven Phase 84 patterns.

**Research date:** 2026-07-10
**Valid until:** 2026-08-09 (stable — internal code; only invalidated by further edits to `stage_status.py` / `pipeline.py` before planning).
