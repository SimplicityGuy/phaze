---
phase: 81-per-stage-failure-persistence-retry-paths
reviewed: 2026-07-08T00:00:00Z
depth: standard
files_reviewed: 20
files_reviewed_list:
  - alembic/versions/033_add_analysis_completed_xor_failed.py
  - src/phaze/enums/stage.py
  - src/phaze/models/analysis.py
  - src/phaze/routers/agent_analysis.py
  - src/phaze/routers/agent_fingerprint.py
  - src/phaze/routers/agent_metadata.py
  - src/phaze/routers/pipeline.py
  - src/phaze/schemas/agent_metadata.py
  - src/phaze/services/agent_client.py
  - src/phaze/services/pipeline.py
  - src/phaze/services/stage_status.py
  - src/phaze/tasks/metadata_extraction.py
  - src/phaze/templates/pipeline/partials/metadata_retry_response.html
  - tests/analyze/routers/test_agent_analysis_failure.py
  - tests/fingerprint/routers/test_agent_fingerprint_failure.py
  - tests/integration/routers/test_pipeline_metadata_retry.py
  - tests/integration/test_migrations/test_migration_033_additive_check.py
  - tests/integration/test_stage_status_equivalence.py
  - tests/metadata/routers/test_agent_metadata.py
  - tests/metadata/tasks/test_metadata_extraction.py
findings:
  critical: 2
  critical_fixed: 2
  critical_open: 0
  warning: 6
  info: 4
  total: 12
status: issues_found
criticals_resolved: true
resolution_commits: [a6398d33, 1ff92265]
---

# Phase 81: Code Review Report

**Reviewed:** 2026-07-08
**Depth:** standard
**Files Reviewed:** 20
**Status:** issues_found

## Summary

The five review-focus areas the workflow called out are, with one exception, clean.

- **XOR CHECK vs writers (focus 1):** No in-app path can violate the migration-033 CHECK. `report_analysis_failed` (agent_analysis.py:359-365) writes `failed_at` and `analysis_completed_at=None` in one statement; `put_analysis`'s conflict branch (agent_analysis.py:214-217) clears `failed_at` unconditionally before the completion `UPDATE` stamps `analysis_completed_at` (agent_analysis.py:252), and both live in the same transaction, so the row lock serializes a concurrent failure report behind them. `post_analysis_progress` touches neither column. The empty-body `put_analysis` path takes `ON CONFLICT DO NOTHING` and never stamps completion, so it cannot mix either.
- **Migration 033 (focus 2):** Ordering is correct (cleanup at line 70, `create_check_constraint` at line 73), `downgrade()` is DDL-only, and the `down_revision` chain `031 → 032 → 033` is linear with a single head. One data-hygiene defect, see WR-04.
- **XSS in the HTMX partial (focus 4):** Clean. `metadata_retry_response.html` interpolates only `{{ count }}` (an `int`) into HTML text content — no attribute context, no JS context, no filename, no operator free-text. Jinja autoescape covers it. This is not a repeat of the `_diff_row.html` incident.
- **Queue misrouting (focus 5):** Clean. `retry_metadata_failed` resolves through `enqueue_router.resolve_queue_for_task("extract_file_metadata", ...)`, catches `NoActiveAgentError` and returns without enqueuing. There is no default-queue fallthrough, and `tests/integration/routers/test_pipeline_metadata_retry.py:95-97` pins the resolved queue name.
- **Payload completeness (focus 6):** Clean. `_enqueue_extraction_jobs` (pipeline.py:1197-1204) builds the full four-field `ExtractMetadataPayload`, and the test asserts `set(payload) == {"file_id", "original_path", "file_type", "agent_id"}`.
- **Version skew (focus 7):** Clean. `body: MetadataFailurePayload | None = None` with no `Body()` wrapper binds `None` on a bodyless POST; `agent_client.report_metadata_failed` omits the `json=` kwarg entirely when `payload is None`, so no `Content-Type` is sent. Covered by `test_metadata_failed_bodyless_persists_marker_and_clears_ledger`.

The two critical findings are elsewhere. The first is a dual-write divergence that FAIL-01 *introduces* into an endpoint the phase did not touch: `retry_analysis_failed` clears `files.state` but leaves `analysis.failed_at` set, so the two halves of the marker/state pair disagree the moment an operator uses the analyze retry button. The second is a total-function violation in the new SQL twin: `domain_completed_clause` raises `KeyError` for four of the seven `Stage` values it accepts.

**Twin drift (focus 3)** is real but narrower than a blocker: `domain_completed()` and `domain_completed_clause()` agree on the eleven enrich-stage cells the equivalence test seeds, but diverge on `in_flight ∧ failed` rows, which FAIL-03's retry now makes routinely reachable (WR-02).

## Resolution (orchestrator, wave-2 close)

Both criticals were independently reproduced and then FIXED before phase verification. Each fix was
proven non-vacuous by reverting it and observing the new tests go red.

| ID | Status | Fix commit | Evidence |
|----|--------|-----------|----------|
| CR-01 | ✅ fixed | `1ff92265` | 4 new integration tests; 3 fail without the fix |
| CR-02 | ✅ fixed | `a6398d33` | 17 new DB-free contract tests; 12 fail without the guards |

- **CR-01** — `retry_analysis_failed` now clears `analysis.failed_at` / `error_message` in the same
  transaction as the state flip. Safe because migration 033's CHECK guarantees
  `analysis_completed_at IS NULL` on a failed row, so the cleared row derives `not_started`.
  The Phase-30 no-active-agent guard still mutates nothing.
- **CR-02** — both twins now raise an identical `ValueError` for the four downstream stages instead of
  a bare `KeyError` (SQL) / silently returning `True` on `DONE` (Python). Fail-loud rather than
  defaulting, matching `reenqueue.py`'s existing "live-keys-only ... no domain predicate" classification.
  Reviewer understated this one: the twins also diverged on non-failed downstream rows.

Warnings WR-01 and WR-02 remain OPEN and are carried into the phase's deferred items — see
`deferred-items.md`. WR-02 in particular is a live hole in the drift-lock now that FAIL-03's retry
routinely produces `in_flight ∧ failed` rows.

## Critical Issues

### CR-01: `retry_analysis_failed` clears the state half of the FAIL-01 dual-write but leaves the marker half set

**File:** `src/phaze/routers/pipeline.py:937-939`
**Issue:** FAIL-01 makes `analysis.failed_at` a durable twin of `files.state = ANALYSIS_FAILED` — `report_analysis_failed` (agent_analysis.py:359-369) writes both in one transaction, and its docstring states the state write "stays live until three `files.state` readers cut over in Phases 80/82." The pre-existing bulk-retry endpoint in the same file was not updated: it flips `f.state = FileState.FINGERPRINTED` and commits, but never touches the `analysis` row. After one click of the analyze "Retry failed" button, every retried file carries `state='fingerprinted'` **and** `analysis.failed_at IS NOT NULL`.

Three consequences follow, all of which the phase exists to prevent:

1. `stage_status_case(Stage.ANALYZE)` derives `failed` (via `failed_clause`, stage_status.py:128) for a file whose `files.state` says it is queued for analysis. The Phase 79 shadow-compare gate compares exactly these two derivations and will go red on every retried file.
2. `domain_completed_clause(Stage.ANALYZE)` returns `True` (because `FAILURE_IS_TERMINAL[ANALYZE]` adds the `failed_clause` disjunct), so Phase 80's recovery will classify a *freshly re-enqueued* file as "we tried and it is un-processable" and skip it. If the `process_file` job is lost before it runs, the file is stranded with no automatic recovery — the exact 44.5K-over-enqueue-guard inversion.
3. `eligible(status_map, Stage.ANALYZE)` returns `False` for a `FAILED` analyze (`ELIGIBLE_AFTER_FAILURE[ANALYZE] = False`), so no eligibility-driven producer will pick the file up either.

`put_analysis`'s clear-on-success eventually repairs the row, but only if the retry job actually runs and succeeds. Nothing in the phase's test suite covers the retry path against the new marker.

Note the asymmetry with FAIL-03: `retry_metadata_failed` deliberately leaves `metadata.failed_at` because clearing it would make a zero-metadata row read `DONE` (a row-presence stage). Analyze has no such hazard — `done(analyze)` keys on `analysis_completed_at`, which is already NULL on a failed row, so clearing `failed_at` yields `not_started`, which is precisely the correct derived status for a file that has just been re-enqueued.

**Fix:**
```python
    # RESEARCH Pitfall 3: flip out of the terminal bucket and COMMIT before any enqueue so the
    # red count drops on the next 5s poll regardless of the enqueue outcome.
    for f in files:
        f.state = FileState.FINGERPRINTED
    # FAIL-01 dual-write parity: the retry must clear BOTH halves. `analysis_completed_at` is
    # already NULL on a failed row, so clearing `failed_at` derives `not_started` (correct for a
    # re-enqueued file) instead of leaving `failed(analyze)` durable while `files.state` says
    # otherwise. Guarded by the XOR CHECK either way.
    await session.execute(
        update(AnalysisResult)
        .where(AnalysisResult.file_id.in_([f.id for f in files]))
        .values(failed_at=None, error_message=None)
    )
    await session.commit()
```
Add a regression test asserting that after `POST /pipeline/analysis-failed/retry` no file satisfies both `files.state != 'analysis_failed'` and `analysis.failed_at IS NOT NULL`.

---

### CR-02: `domain_completed_clause` raises `KeyError` for four of the seven `Stage` values it accepts

**File:** `src/phaze/services/stage_status.py:183`
**Issue:** `FAILURE_IS_TERMINAL` is populated for `ANALYZE`, `METADATA` and `FINGERPRINT` only (enums/stage.py:87). `domain_completed_clause` subscripts it unconditionally:

```python
    if FAILURE_IS_TERMINAL[stage]:
```

so `domain_completed_clause(Stage.TRACKLIST)`, `(Stage.PROPOSE)`, `(Stage.REVIEW)` and `(Stage.APPLY)` all raise `KeyError` before any SQL is built. The function's signature promises totality over `Stage`, its sibling builders (`done_clause`, `failed_clause`, `inflight_clause`) all handle every stage and end with an explicit `raise ValueError(f"unknown stage: {stage!r}")`, and the module docstring advertises this layer as "ONE place to drop a per-stage predicate into a `.where(...)`" for *every* later-phase reader. A Phase-82 reader that loops `for stage in Stage:` crashes.

The Python twin has the same latent hole in a narrower form (enums/stage.py:197): `st is Status.FAILED and FAILURE_IS_TERMINAL[stage]` short-circuits for `DONE`/`NOT_STARTED`/`IN_FLIGHT`, but `resolve_status` happily returns `Status.FAILED` for `PROPOSE`/`REVIEW`/`APPLY` (via `_presence_status`, which takes a `failed` scalar and is fed a real `RenameProposal.status == 'failed'` probe by the equivalence test's `load_scalars`). So `domain_completed({Stage.PROPOSE: Status.FAILED}, Stage.PROPOSE)` raises `KeyError` today.

The equivalence test (`DOMAIN_COMPLETED_CASES`) exercises enrich stages only, so neither hole is caught.

**Fix:** make the terminality lookup total and keep the two twins symmetric.
```python
# enums/stage.py
FAILURE_IS_TERMINAL: dict[Stage, bool] = {
    Stage.ANALYZE: True,
    Stage.METADATA: True,
    Stage.FINGERPRINT: False,
    # Downstream presence stages: a FAILED row is not a terminal "un-processable" fact.
    Stage.TRACKLIST: False,
    Stage.PROPOSE: False,
    Stage.REVIEW: False,
    Stage.APPLY: False,
}
```
Or, if the table must stay enrich-only by design, guard both readers with `FAILURE_IS_TERMINAL.get(stage, False)` and extend `DOMAIN_COMPLETED_CASES` with a `(Stage.PROPOSE, seed_propose_failed_still_done, True)` cell plus a downstream `not_started` cell so the totality is pinned.

## Warnings

### WR-01: `report_metadata_failed` does not null the payload columns on conflict, contradicting its own contract

**File:** `src/phaze/routers/agent_metadata.py:156-157`
**Issue:** The `ON CONFLICT (file_id) DO UPDATE` sets only `failed_at` and `error_message`:

```python
stmt = stmt.on_conflict_do_update(index_elements=["file_id"], set_={"failed_at": now, "error_message": error_message})
```

The `INSERT` branch produces a payload-NULL row, but the conflict branch does not. Three places assert the opposite:

- `agent_metadata.py:126` — "upsert a `metadata` row with `failed_at` set and payload columns NULL"
- `services/pipeline.py:1348` — "persisted by the 81-03 writer as a `metadata` row with `failed_at` set and the payload columns NULL"
- `tests/integration/routers/test_pipeline_metadata_retry.py:61-66` — `_make_failed_metadata` docstring, "`failed_at` set, payload columns NULL"

The conflict branch is reachable: `POST /api/v1/extract-metadata` re-enqueues **all** music/video files "regardless of state for backfill" (pipeline.py:1214), so a file that already has complete tags can hit a terminal extraction failure (file moved by apply, permission change, corrupt header). The result is a row with real `artist`/`title`/`duration` **and** `failed_at` set. `done(metadata)` is `EXISTS metadata WHERE failed_at IS NULL`, so the file derives `FAILED`, drops out of `eligible(PROPOSE)` (whose upstream conjunct requires `done(metadata)`), and lands in `get_metadata_failed_files` — despite holding perfectly usable metadata.

No test covers a failure report against a row that already carries payload; every existing case starts from an empty or absent row.

**Fix:** decide which is true and make code and docs agree. If the docstrings are the contract, null the payload in the conflict branch so the row really is failure-only:
```python
_PAYLOAD_COLUMNS = ("artist", "title", "album", "year", "genre", "track_number", "duration", "bitrate", "raw_tags")
stmt = stmt.on_conflict_do_update(
    index_elements=["file_id"],
    set_={"failed_at": now, "error_message": error_message, **dict.fromkeys(_PAYLOAD_COLUMNS)},
)
```
If instead retaining the stale payload is intentional (mirroring `report_analysis_failed`, which retains `bpm`/`musical_key` while clearing only the completion discriminator), correct all three docstrings and add a test pinning `artist is not None and failed_at is not None → derived FAILED`.

---

### WR-02: `domain_completed_clause` ignores `in_flight`; its Python twin does not — and FAIL-03 makes the divergent state routine

**File:** `src/phaze/services/stage_status.py:183-185`, `src/phaze/enums/stage.py:196-197`
**Issue:** The SQL twin is `or_(done_clause, failed_clause)` with no `inflight_clause` disjunct. The Python twin consumes a `status_map` produced by `resolve_status`, whose precedence ladder puts `IN_FLIGHT` above `FAILED`. So for a row that is simultaneously in flight and carrying a failure marker:

- Python: `resolve_status → IN_FLIGHT` → `domain_completed → False`
- SQL: `failed_clause` is true → `domain_completed_clause → True`

The equivalence test acknowledges this in a comment (`test_stage_status_equivalence.py:421-427`) and simply excludes the `*_inflight` seeds from `DOMAIN_COMPLETED_CASES`. That was defensible when `in_flight ∧ failed` was unreachable — every failure writer clears the ledger row in the same transaction. FAIL-03 changes that: `retry_metadata_failed` deliberately leaves `metadata.failed_at` in place (D-11) and enqueues, and the `before_enqueue` hook writes a `extract_file_metadata:<file_id>` ledger row. Every file in a metadata bulk retry is therefore `in_flight ∧ failed` until the job completes. CR-01 produces the same shape for analyze.

The divergence direction is currently benign (SQL over-reports domain-complete, so recovery skips a running job), but it is exactly the drift the DERIV-04/D-17 lock exists to prevent, and it is now silently untested-by-construction rather than untested-by-omission.

**Fix:** either make the twins genuinely ledger-agnostic on both sides (have `domain_completed` take a `Status` derived without `inflight`, or accept `(done, failed)` booleans), or add the `inflight` term to both:
```python
def domain_completed_clause(stage: Stage) -> ColumnElement[bool]:
    if FAILURE_IS_TERMINAL.get(stage, False):
        return and_(~inflight_clause(stage), or_(done_clause(stage), failed_clause(stage)))
    return and_(~inflight_clause(stage), done_clause(stage))
```
Then unskip the `*_inflight` seeds in `DOMAIN_COMPLETED_CASES` so the lock actually covers the cell.

---

### WR-03: identity comparison against `StrEnum` members makes `eligible` and `domain_completed` silently wrong for string-valued status maps

**File:** `src/phaze/enums/stage.py:197`, `src/phaze/enums/stage.py:224`
**Issue:** Both predicates mix `is` / `is not` identity checks with `in` / `!=` value checks against the same `Status` `StrEnum`, inside the same expression:

```python
return st is Status.DONE or (st is Status.FAILED and FAILURE_IS_TERMINAL[stage])          # line 197
return status not in (Status.DONE, Status.IN_FLIGHT) and (status is not Status.FAILED or ELIGIBLE_AFTER_FAILURE[stage])  # line 224
...
return has_approved_proposal and status_map.get(Stage.APPLY, Status.NOT_STARTED) != Status.DONE   # line 226
```

`Status` is a `StrEnum`, so `"failed" in (Status.DONE, Status.IN_FLIGHT)` and `"failed" != Status.DONE` both behave correctly for a plain `str`, but `"failed" is Status.FAILED` is `False`. The consequence is not a crash but a silent inversion:

- `eligible({Stage.ANALYZE: "failed"}, Stage.ANALYZE)` → `True` (a terminally-failed analyze becomes auto-eligible — the 44.5K over-enqueue class this table exists to guard).
- `domain_completed({Stage.METADATA: "failed"}, Stage.METADATA)` → `False` (a terminal failure stops counting as domain-complete, so recovery re-runs it forever).

Raw-string status maps are not hypothetical: the SQL twin `stage_status_case` emits `Status.X.value` string labels (stage_status.py:203-206), and the equivalence test's own `eval_sql_status` returns `str`. mypy would catch a typed caller, but any reader that builds `status_map` from a SQL `SELECT` without an explicit `Status(label)` coercion gets the wrong answer with no error.

**Fix:** use value equality throughout, matching line 226.
```python
return st == Status.DONE or (st == Status.FAILED and FAILURE_IS_TERMINAL.get(stage, False))
...
return status not in (Status.DONE, Status.IN_FLIGHT) and (status != Status.FAILED or ELIGIBLE_AFTER_FAILURE[stage])
```
Add a parametrized unit test feeding the raw `.value` strings through both predicates and asserting identical results to the enum members.

---

### WR-04: migration 033's cleanup nulls `failed_at` but strands `error_message` on rows it converts to DONE

**File:** `alembic/versions/033_add_analysis_completed_xor_failed.py:63`
**Issue:**
```python
_CLEANUP_MIXED_ROWS = "UPDATE analysis SET failed_at = NULL WHERE analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL"
```
Every mixed row on the live corpus was produced by 032's `_BACKFILL_ANALYZE_FAILED`, which stamps `error_message = 'backfilled from ANALYSIS_FAILED'` alongside `failed_at`. After the cleanup those rows derive `DONE` (`analysis_completed_at IS NOT NULL`) while permanently carrying a failure explanation in `error_message`. Every other writer keeps the two columns coupled: `put_analysis` clears both (agent_analysis.py:216), `report_analysis_failed` writes both (agent_analysis.py:364). The migration is the only place that decouples them, and because these rows are already `DONE` nothing will ever re-analyze them and repair the field. The integration test asserts `failed_at`/`analysis_completed_at` but never inspects `error_message`, so the inconsistency is unpinned.

**Fix:**
```python
_CLEANUP_MIXED_ROWS = (
    "UPDATE analysis SET failed_at = NULL, error_message = NULL "
    "WHERE analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL"
)
```
and assert `mixed[2] is None` for `error_message` in `test_upgrade_033_cleans_mixed_rows_before_check_then_downgrade_reverses`. (The existing source-order assertion `"SET analysis_completed_at" not in cleanup_sql` still passes; the D-04 "done wins" guarantee is unaffected.)

---

### WR-05: `put_fingerprint` / `report_fingerprint_failed` docstrings attribute `eligible()`'s behavior to the wrong table — the exact D-15 conflation

**File:** `src/phaze/routers/agent_fingerprint.py:35-36`, `src/phaze/routers/agent_fingerprint.py:85`
**Issue:** Both docstrings say:

> "`FAILURE_IS_TERMINAL[fingerprint] = False`, so `eligible(FINGERPRINT)` stays True for a FAILED engine (ELIG-04 …)"

`eligible()` does not read `FAILURE_IS_TERMINAL`. It reads `ELIGIBLE_AFTER_FAILURE[stage]` (enums/stage.py:224). `FAILURE_IS_TERMINAL` is consumed only by `domain_completed` / `domain_completed_clause`. `enums/stage.py:72-86` opens with "two ORTHOGONAL axes for the three enrich stages — conflating them is a live trap," and then the only two consumer docstrings written this phase conflate them. The two tables happen to be exact negations for all three enrich stages today, so the sentence reads as true; a maintainer who later needs a stage where they differ (a terminal-but-retryable failure, say) will flip the wrong table because the authoritative endpoint docs point at it.

**Fix:** Replace both references with `ELIGIBLE_AFTER_FAILURE[fingerprint] = True` and, where terminality is genuinely the point (the `domain_completed` / recovery sentence at line 85), keep `FAILURE_IS_TERMINAL` but say `domain_completed`, not `eligible`.

---

### WR-06: `retry_metadata_failed` enqueues an unbounded set inline, inside the request's still-open transaction

**File:** `src/phaze/routers/pipeline.py:982-1006`
**Issue:** `get_metadata_failed_files` (services/pipeline.py:1358) applies no `LIMIT` and no state filter — it returns every file with a metadata failure marker. `retry_metadata_failed` then `await`s `_enqueue_extraction_jobs` inline, one `queue.enqueue` per file. Both sibling producers of the same task (`trigger_metadata_extraction` at pipeline.py:1230 and `trigger_extraction_ui` at pipeline.py:1255) dispatch that same coroutine via `asyncio.create_task` precisely because the loop is unbounded.

A corpus-wide metadata failure (agent misconfiguration, a mount that vanished — both have happened here) makes this a single HTMX POST that performs N sequential broker round-trips under the browser's / proxy's request timeout, while holding the `get_session` transaction open the whole time. If the request is cut off midway there is no record of which files were enqueued and which were not; the endpoint has no partial-progress or resume path. Note that `retry_analysis_failed` at least `commit()`s its state flip before its enqueue loop, so its work is durable before the loop starts; FAIL-03's D-11 "no state flip" means this endpoint has no such checkpoint.

**Fix:** mirror the two sibling producers and hand the loop to the background-task set, or bound the batch:
```python
    task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, files, agent_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
```
If the loop stays inline, `await session.commit()` (or `rollback()`) before it so the read transaction is not held across N broker calls. Note the background-task form requires the `FileRecord` attributes to be read before the session closes — `expire_on_commit` semantics apply, same as the existing producers.

## Info

### IN-01: Omitted `error` composes the literal string `"crashed: None"` into the persisted marker

**File:** `src/phaze/routers/agent_analysis.py:353`, `src/phaze/routers/agent_metadata.py:150`
**Issue:** `error` is `str | None = None` on both failure payloads, so `f"{body.reason}: {body.error}"` yields `"crashed: None"` / `"timeout: None"` when the agent omits it. `tests/analyze/routers/test_agent_analysis_failure.py:159` pins this as intended, but the string leaks a Python repr into an operator-facing triage column.
**Fix:** `error_message = f"{body.reason}: {body.error}" if body.error else body.reason`.

### IN-02: `_ERROR_MESSAGE_MAX = 2000` duplicated across two routers

**File:** `src/phaze/routers/agent_analysis.py:69`, `src/phaze/routers/agent_metadata.py:25`
**Issue:** The bound is defined twice with identical comments explaining it must mirror the other. It also duplicates `Field(max_length=2000)` in both schemas. Four copies of one number.
**Fix:** Hoist to a single constant (e.g. `phaze.schemas.agent_analysis.ERROR_DETAIL_MAX`) and reference it from both the `Field(max_length=...)` declarations and both truncations.

### IN-03: `metadata_retry_response.html` documents a card and a stats-poll wiring that do not exist

**File:** `src/phaze/templates/pipeline/partials/metadata_retry_response.html:1-7`
**Issue:** The header comment describes the fragment as "HTMX ack for the 'Retry failed' bulk action on the Metadata Health card" and says "the 5s stats poll keeps re-pushing the metadata-failed card untouched." Neither exists: nothing in `src/phaze/templates/` posts to `/pipeline/metadata-failed/retry`, and there is no metadata-failed card or stats counter. 81-06-SUMMARY.md:96 confirms the button render and `/pipeline/stats` count are Phase-87 scope. The endpoint and template are correct but currently unreachable from the UI.
**Fix:** Reword the comment to state the intent ("consumed by the Phase-87 Metadata Health card; no caller yet") so a reader does not go hunting for a card that has not been built.

### IN-04: Stale source-line reference in the fingerprint docstring

**File:** `src/phaze/routers/agent_fingerprint.py:36`
**Issue:** Cites `enums/stage.py:186` for `eligible` / ELIG-04. Line 186 is `def domain_completed`; `eligible` is at line 200.
**Fix:** Drop the line number and cite the symbol (`phaze.enums.stage.eligible`), which is what the rest of the codebase's cross-references do and which does not rot on the next edit.

---

_Reviewed: 2026-07-08_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
