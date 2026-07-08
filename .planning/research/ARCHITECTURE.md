# Architecture Research — Derived Stage Status (2026.7.5 Parallel Enrich DAG)

**Domain:** Internal refactor of a shipped two-host distributed pipeline (FastAPI + SQLAlchemy async + SAQ-on-Postgres)
**Researched:** 2026-07-08
**Confidence:** HIGH (every claim below is traced to a file:line in the tree at `SimplicityGuy/true-parallel`, off `main` @ `ce0c6434`)
**Reads:** `.planning/PROJECT.md`, `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md`, `src/phaze/services/{pipeline,backends,dedup,fingerprint,proposal,proposal_queries,stage_control,scheduling_ledger}.py`, `src/phaze/tasks/{reenqueue,reconcile_cloud_jobs,release_awaiting_cloud}.py`, `src/phaze/tasks/_shared/{stage_control,deterministic_key}.py`, `src/phaze/routers/{pipeline,agent_metadata,agent_fingerprint,agent_analysis,agent_push,agent_s3,agent_files,duplicates}.py`, `src/phaze/models/*`, `alembic/versions/*`

---

## 0. Executive answer

The derived-status layer is a **new leaf service module** (`src/phaze/services/stage_status.py`) plus a **new DB-free enum module** (`src/phaze/enums/stage.py`), built on a `StageSpec` registry that emits *SQLAlchemy `ColumnElement[bool]` factories* rather than executing queries. Every one of the four consumers (pending-set SQL, aggregate-count SQL, per-file UI status, recovery's `is_domain_completed`) composes the SAME `done_exists()` / `failed_exists()` predicate objects. `in_flight` is **never** joined into SQL — it is a separate, already-existing, degrade-safe `saq_jobs` key-set read (`services/pipeline.py:510 get_live_job_keys`), and the `StageSpec` carries only the SAQ *function name* (the key prefix) so the caller can do set membership in Python.

Two of the design's five open decisions get a **different** recommendation than the design's tentative one:

- **D-01: `in_flight = saq_jobs(queued|active)` ALONE**, not the union with `scheduling_ledger`. The union creates a permanent-stuck class that is strictly worse than the crash window it closes. See §5.1 for the code-grounded argument.
- **D-03: a new `analyze_route` table**, not a new `cloud_job.status='awaiting'` CHECK member — because `cloud_job.status` is the substrate for *five* independent cap/recovery/admission predicates and widening it is exactly the blast radius that produced the over-enqueue incidents.

And one improvement to the build order: **the shadow-compare gate must exist before the readers flip, not after.** It is a *standing* check run twice (post-backfill, and again as the gate into the destructive migration), not a one-shot pre-drop script.

---

## 1. System overview — where the new layer sits

```
┌───────────────────────────────────────────────────────────────────────────┐
│  AGENT-SAFE (Postgres-free; tests/test_task_split.py enforces)            │
├───────────────────────────────────────────────────────────────────────────┤
│  phaze/enums/stage.py            [NEW]  Stage, StageStatus (StrEnum)      │
│  phaze/enums/execution.py        [exists — the precedent for this module] │
│  phaze/tasks/_shared/stage_control.py   STAGE_TO_FUNCTION  [MODIFIED]     │
│  phaze/tasks/_shared/deterministic_key.py  _KEY_BUILDERS   [unchanged]    │
└───────────────────────────────────────────────────────────────────────────┘
                                    │ (import; no models, no sqlalchemy.ext.asyncio)
                                    ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  CONTROL-SIDE DERIVATION LAYER  (the milestone's center of gravity)       │
├───────────────────────────────────────────────────────────────────────────┤
│  phaze/services/stage_status.py                                    [NEW]  │
│                                                                           │
│   STAGE_SPECS: Mapping[Stage, StageSpec]                                  │
│   ├─ done_exists()      -> ColumnElement[bool]   (correlated EXISTS)      │
│   ├─ failed_exists()    -> ColumnElement[bool] | None                     │
│   ├─ saq_function       -> str | None            (the saq_jobs key prefix)│
│   ├─ failure_is_terminal-> bool                                           │
│   └─ upstream           -> tuple[Stage, ...]                              │
│                                                                           │
│   resolve_status(done, failed, in_flight) -> StageStatus   (pure)         │
│   eligible(status, spec)                  -> bool          (pure)         │
│   domain_completed_stmt(stage)            -> Select        (SQL)          │
│   pending_stmt(stage)                     -> Select        (SQL)          │
│   done_count_stmt(stage)                  -> Select        (SQL)          │
│   bulk_stage_status(session, live_keys)   -> per-file dict (SQL + Python) │
└───────────────────────────────────────────────────────────────────────────┘
      │                │                │                 │
      │(a) pending SQL │(b) count SQL   │(c) UI per-file  │(d) recovery
      ▼                ▼                ▼                 ▼
 services/       services/         routers/         tasks/reenqueue.py
 pipeline.py     pipeline.py       pipeline.py      is_domain_completed
 (3 enrich sets) get_stage_progress shell.py        _build_done_sets
                 get_pipeline_stats templates/
      │                                                   │
      └───────────────────┬───────────────────────────────┘
                          │  in_flight (NEVER in SQL)
                          ▼
        services/pipeline.py:510  get_live_job_keys(session) -> set[str]
        static SQL + session.begin_nested() SAVEPOINT + degrade-to-set()
                          │
                          ▼
                    saq_jobs  (SAQ-owned, never Alembic-managed)
```

### 1.1 Persistence after the refactor

| Fact | Today | After |
|---|---|---|
| discovery done | `files` row | unchanged |
| metadata done | `state == METADATA_EXTRACTED` | `EXISTS metadata WHERE file_id=… AND failed_at IS NULL` |
| metadata failed | **nothing** (latent bug) | `metadata.failed_at IS NOT NULL` **[NEW COLUMN]** |
| fingerprint done | `state == FINGERPRINTED` (only written by a rollback path) | `EXISTS fingerprint_results WHERE status IN ('success','completed')` |
| fingerprint failed | `fingerprint_results.status='failed'` (per-engine, soft) | unchanged — reuse |
| analyze done | `state == ANALYZED` | `analysis.analysis_completed_at IS NOT NULL` |
| analyze failed | `state == ANALYSIS_FAILED` | `analysis.analysis_failed_at IS NOT NULL` **[NEW COLUMN]** |
| in flight (any stage) | `state` in a lane value, or nothing | `saq_jobs.key = '<fn>:<file_id>' AND status IN ('queued','active')` |
| routed to cloud (`AWAITING_CLOUD`) | `files.state` | `analyze_route.route = 'cloud'` **[NEW TABLE]** |
| locally spilled (`LOCAL_ANALYZING`) | `files.state` | **derived** — `analyze_route.route='local'` (the flip `LocalBackend.dispatch` already performs, on a different column) |
| pushing / pushed | `files.state` | `cloud_job.status ∈ {submitted}` / `{pushed}` **[NEW CHECK member]** |
| duplicate resolved | `files.state` | `dedup_resolution` row **[NEW TABLE]** |
| approved / rejected | `files.state` *and* `proposals.status` | `proposals.status` (already authoritative) |
| executed / moved / unchanged / failed | `files.state` | `execution_log JOIN proposals` + `proposals.status` |

---

## 2. Q1 — Where does `stage_status()` live?

### Verdict: a NEW module, `src/phaze/services/stage_status.py`. Do not extend `pipeline.py`.

**Evidence from the existing service-layer conventions** (`src/phaze/services/`, 38 modules):

1. **The codebase already splits by concept, and already splits *out of* `pipeline.py`.** `pipeline_counters.py`, `route_control.py`, `stage_control.py`, `backend_selection.py` are all single-concept modules. `proposal.py` (behavior) vs `proposal_queries.py` (SQL), and `execution_dispatch.py` vs `execution_queries.py`, establish the "one noun, one module" convention. `stage_status` is a noun.

2. **`pipeline.py` is 1,553 lines and is the milestone's most-modified file.** It was flagged at 65.5% coverage in the 2026.7.0 retro and the per-module floor was then raised to 90 (memory `05a7e7f`). Adding ~250 lines of the milestone's core logic there fights the per-module gate and makes the whole milestone's coverage story hostage to `pipeline.py`'s existing tail.

3. **`pipeline.py` imports `FileState` 35 times** (`git grep -c FileState` → 35). The derivation layer must be `FileState`-free from line one, so migration `033` is a *deletion* from `pipeline.py`, not an edit inside the module that defines the replacement.

4. **Import-cycle risk.** `tasks/reenqueue.py:77` already imports four symbols from `services/pipeline.py`; `services/backends.py:58-59` imports from `tasks/reconcile_cloud_jobs` and `tasks/release_awaiting_cloud`. Putting the registry in `pipeline.py` makes `reenqueue → pipeline` carry it, and `release_awaiting_cloud`/`backends` would then need `pipeline` too. A new **leaf** module whose only imports are `phaze.models.*` + `phaze.enums.stage` + `phaze.tasks._shared.stage_control` stays acyclic and importable from every consumer.

5. **The agent boundary forces a two-module split.** `tasks/_shared/stage_control.py:23-28` carries a hard rule: *"this module must NOT import `phaze.database`, `phaze.tasks.session`, or `sqlalchemy.ext.asyncio`."* A `StageSpec` holding `Callable[[], ColumnElement[bool]]` needs `phaze.models.*` and therefore **cannot** live in `_shared`. So:
   - `phaze/enums/stage.py` **[NEW, DB-free]** — `Stage` and `StageStatus` StrEnums. Mirrors `phaze/enums/execution.py`, whose docstring states exactly this purpose: *"DB-free enum definitions shared between SQLAlchemy models and Pydantic schemas … the agent worker is forbidden from importing `phaze.database` / `phaze.models`."*
   - `tasks/_shared/stage_control.py` **[MODIFIED]** — re-key `STAGE_TO_FUNCTION` on `Stage`, keep it agent-safe.
   - `services/stage_status.py` **[NEW, control-only]** — the `StageSpec` registry, the predicate factories, the pure precedence resolver, the bulk helpers.

**One landmine:** `services/fingerprint.py` is imported by the *agent worker*, which is why its DB imports at lines 270-271 are function-local (`# noqa: PLC0415`). When `get_fingerprint_progress` is rewritten to consume `stage_status`, that import must ALSO be function-local. Same discipline; do not hoist it.

---

## 3. Q2 — The single-source-of-truth structure

### 3.1 The problem, precisely

Four consumers need the same per-stage predicates. Today they are four independent hand-written expressions that have already drifted:

| Consumer | Today's location | Today's expression for `analyze` |
|---|---|---|
| (a) pending SQL | `services/pipeline.py:1095` `get_discovered_files_with_duration` | `state == DISCOVERED` |
| (b) aggregate count SQL | `services/pipeline.py:383` `get_stage_progress` | `COUNT(DISTINCT analysis.file_id)` ← **over-counts; ignores `analysis_completed_at`** |
| (c) per-file UI status | `services/pipeline.py:766` `_ANALYZE_STAGE_STATES` + `:843` `completed = state == ANALYZED` | five enum members OR bare `analysis` row existence |
| (d) recovery done-set | `tasks/reenqueue.py:187` `_select_done_analyze_ids` | `state IN (ANALYZED, ANALYSIS_FAILED)` |
| (e) proposal convergence | `services/pipeline.py:1420` `get_proposal_pending_batches` | `analysis_completed_at IS NOT NULL` ← **the only correct one** |

Five expressions, three different semantics, for one concept. (b) vs (e) is design §4.1 bug 7.

### 3.2 The `StageSpec` registry — concrete shape

`STAGE_TO_FUNCTION` (`tasks/_shared/stage_control.py:51`) is already a partial version keyed on the three agent stages. Keep it; make it the *DB-free projection* of the registry, and enforce agreement with a totality test (precedent: `reenqueue.py:436` `_ALL_KEYED_FUNCTIONS = tuple(_KEY_BUILDERS)` + the T-45-17 totality test in `test_recovery.py`; and `pipeline.py:463` `_BUSY_FUNCTION_TO_STAGE`, already built as `{fn: stage for stage, fn in STAGE_TO_FUNCTION.items()}` — the inverse-map pattern this design generalizes).

```python
# src/phaze/enums/stage.py  [NEW — DB-free, agent-safe]
class Stage(enum.StrEnum):
    DISCOVERY = "discovery"
    METADATA = "metadata"
    FINGERPRINT = "fingerprint"
    ANALYZE = "analyze"
    TRACKLIST = "tracklist"
    PROPOSE = "propose"
    APPLY = "apply"

class StageStatus(enum.StrEnum):
    NOT_STARTED = "not_started"
    IN_FLIGHT = "in_flight"
    DONE = "done"
    FAILED = "failed"
```

```python
# src/phaze/services/stage_status.py  [NEW — control-only]
@dataclass(frozen=True, slots=True)
class StageSpec:
    stage: Stage
    saq_function: str | None                                  # the saq_jobs key prefix; None = not a queued stage
    done_exists: Callable[[], ColumnElement[bool]]            # correlated EXISTS over FileRecord.id
    failed_exists: Callable[[], ColumnElement[bool]] | None
    failure_is_terminal: bool
    upstream: tuple[Stage, ...]
```

The predicate factories are **correlated `EXISTS` builders**, not executed queries:

```python
def _metadata_done() -> ColumnElement[bool]:
    return exists(select(FileMetadata.id).where(
        FileMetadata.file_id == FileRecord.id,
        FileMetadata.failed_at.is_(None),          # D-02: a failure row is NOT done
    ))

def _analyze_done() -> ColumnElement[bool]:
    return exists(select(AnalysisResult.id).where(
        AnalysisResult.file_id == FileRecord.id,
        AnalysisResult.analysis_completed_at.is_not(None),
    ))

def _fingerprint_done() -> ColumnElement[bool]:
    # N rows per file; ANY successful engine == done  (matches get_stage_progress:377 today)
    return exists(select(FingerprintResult.id).where(
        FingerprintResult.file_id == FileRecord.id,
        FingerprintResult.status.in_(("success", "completed")),
    ))

def _fingerprint_failed() -> ColumnElement[bool]:
    # "done beats failed": failed means NO engine succeeded
    return and_(~_fingerprint_done(), exists(select(FingerprintResult.id).where(
        FingerprintResult.file_id == FileRecord.id, FingerprintResult.status == "failed")))
```

Each of the four consumers is a *composition*, not a re-derivation:

```python
# (a) pending SQL  — the three enrich stages have NO upstream (design §3)
def pending_stmt(stage: Stage) -> Select[tuple[FileRecord]]:
    spec = STAGE_SPECS[stage]
    stmt = select(FileRecord).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES), ~spec.done_exists())
    if spec.failed_exists is not None and spec.failure_is_terminal:
        stmt = stmt.where(~spec.failed_exists())
    for up in spec.upstream:
        stmt = stmt.where(STAGE_SPECS[up].done_exists())
    return stmt
    # the in_flight term is applied by the CALLER, in Python, from get_live_job_keys(). See §4.

# (b) aggregate count SQL — ONE definition, so done_count == |{f : done(f)}| by construction
def done_count_stmt(stage: Stage) -> Select[tuple[int]]:
    return select(func.count(FileRecord.id)).where(STAGE_SPECS[stage].done_exists())

# (c) per-file UI status — ONE query, 2 correlated EXISTS per stage, labelled
def bulk_status_stmt(stages: Sequence[Stage]) -> Select[Any]:
    cols = [FileRecord.id]
    for s in stages:
        spec = STAGE_SPECS[s]
        cols.append(spec.done_exists().label(f"{s}_done"))
        cols.append((spec.failed_exists() if spec.failed_exists else false()).label(f"{s}_failed"))
    return select(*cols).select_from(FileRecord)

# (d) recovery — replaces reenqueue.py::_select_done_analyze_ids / _select_done_push_ids
def domain_completed_stmt(stage: Stage) -> Select[tuple[uuid.UUID]]:
    spec = STAGE_SPECS[stage]
    pred = spec.done_exists()
    if spec.failed_exists is not None and spec.failure_is_terminal:
        pred = or_(pred, spec.failed_exists())
    return select(FileRecord.id).where(pred)
```

And the precedence rule (`in_flight ≻ done ≻ failed ≻ not_started`, design §2.3) is a **single pure function** — no I/O, unit-testable, the one place that rule is ever encoded:

```python
def resolve_status(*, done: bool, failed: bool, in_flight: bool) -> StageStatus:
    if in_flight:  return StageStatus.IN_FLIGHT
    if done:       return StageStatus.DONE
    if failed:     return StageStatus.FAILED
    return StageStatus.NOT_STARTED

def eligible(status: StageStatus, spec: StageSpec) -> bool:
    if status in (StageStatus.DONE, StageStatus.IN_FLIGHT): return False
    if status is StageStatus.FAILED: return not spec.failure_is_terminal
    return True   # NOT_STARTED  (upstream is enforced in SQL, not here)
```

### 3.3 Anti-drift enforcement (the mechanism, not the hope)

A `tests/shared/test_stage_status.py` asserting, in one place:

1. `set(STAGE_SPECS) == set(Stage)` — totality (mirrors T-45-17).
2. `{s: spec.saq_function for s, spec in STAGE_SPECS.items() if spec.saq_function} == STAGE_TO_FUNCTION` — the DB-free projection cannot drift.
3. `{spec.saq_function …} ⊆ set(_KEY_BUILDERS)` — every stage function is actually keyed (`deterministic_key.py:77`); an unkeyed function would silently make `in_flight` always False.
4. `STAGE_SPECS[Stage.ANALYZE].failure_is_terminal is True` — the load-bearing invariant behind the 44.5K-job incident (`reenqueue.py:179-186`).
5. `STAGE_SPECS[Stage.FINGERPRINT].failure_is_terminal is False` — the deliberate D-16 auto-retry asymmetry.
6. `STAGE_SPECS[s].upstream == ()` for `s ∈ {METADATA, FINGERPRINT, ANALYZE}` — **the milestone's thesis, as an executable assertion.**

Plus a `git grep`-style static guard (precedent: `test_task_split.py`, Phase 30's AST guard against `default`-queue enqueues): assert `FileState` appears in zero files under `src/` after `033`.

---

## 4. Q3 — The `saq_jobs` coupling: `in_flight` is NOT a join

### 4.1 Can it be joined? Technically yes. Should it? No.

Constraints, all pre-existing and non-negotiable:

- `saq_jobs` is SAQ-owned; every migration since `020` carries the banner (`alembic/versions/031_add_route_control.py:12`: *"It must NEVER reference `saq_jobs`"*).
- Every read is static SQL inside `session.begin_nested()` and degrades to a safe default. Instances of the idiom: `pipeline.py:466 get_stage_busy_counts`, `:510 get_live_job_keys`, `:542/:580/:621/:649` (search/scan/scrape/match busy), `:1447 count_inflight_jobs`, `:1518 get_straggler_count`, `reenqueue.py:472 backfill_ledger_from_saq_jobs`.
- The `/pipeline/stats` poll runs every 5 s and must never 500.

Joining `saq_jobs` into the ORM anti-join would:
- put the *entire* pending query inside the SAVEPOINT, so a `saq_jobs` hiccup takes out the pending query **and the trigger button silently no-ops** (degrade-to-empty = "nothing to do");
- require an ad-hoc `sqlalchemy.table()` shim or `text()` subquery, breaking the "static SQL, no bound operator input" property the existing sites maintain;
- couple an ORM query to a table Alembic cannot see (autogenerate hazard).

### 4.2 The shape that works — and is already in the tree

`get_live_job_keys(session) -> set[str]` (`pipeline.py:510`) already returns exactly `{'<function>:<file_id>'}` for `status IN ('queued','active')`, and degrades to `set()`. `recover_orphaned_work` already subtracts it in Python (`reenqueue.py:351`). Generalize that shape:

```python
# stage_status.py — one degradable read per call site, reusing pipeline.get_live_job_keys
async def in_flight_ids(session, stage: Stage) -> set[str]:
    """file-id strings with a live saq_jobs row for this stage's function. Degrades to set()."""
    fn = STAGE_SPECS[stage].saq_function
    if fn is None:
        return set()
    prefix = f"{fn}:"
    return {k[len(prefix):] for k in await get_live_job_keys(session) if k.startswith(prefix)}
```

Then:

| Consumer | `in_flight` source | Degrade default | Consequence of degrade |
|---|---|---|---|
| (a) pending set | `in_flight_ids(stage)`, subtracted in Python | `set()` | pending set **over**-includes a running file → it is re-enqueued → SAQ's deterministic-key dedup returns `None` → counted `skipped`. **The documented T-42-05 / T-45-09 backstop** (`pipeline.py:1461`, `reenqueue.py:100-105`). Safe. |
| (b) aggregate counts | `get_stage_busy_counts()` — **already exists, already separate** | `{m:0,a:0,f:0}` | busy pill reads 0. Cosmetic. |
| (c) per-file UI | one `get_live_job_keys()` per render, Python membership | `set()` | a running row shows `not_started`. Cosmetic. |
| (d) recovery | `get_live_job_keys()` — **already used verbatim** | `set()` | every ledger row looks orphaned → replay → dedup no-ops the live ones. Already accepted (T-45-09). |

**The critical property:** the degrade default is uniformly *over*-inclusive, and over-inclusion is harmless because the deterministic key (`deterministic_key.py:99 apply_deterministic_key`, the single `before_enqueue` chokepoint) collapses every duplicate to a no-op. If `in_flight` were joined into SQL, the degrade would be *under*-inclusive (empty pending set), which silently breaks the trigger buttons. **Over-include, never under-include** is the rule.

### 4.3 So: `in_flight` is first-class *in the model*, not *in the SQL*

`StageSpec.saq_function` is the only thing the registry knows about `saq_jobs` — a string prefix. That is the whole coupling. `stage_status(file, stage)` is composed:

```python
async def stage_status_bulk(session, stages) -> dict[uuid.UUID, dict[Stage, StageStatus]]:
    rows = (await session.execute(bulk_status_stmt(stages))).all()          # ORM, Alembic-visible tables only
    live = {s: await in_flight_ids(session, s) for s in stages}             # SAVEPOINT + degrade, per stage
    return {r.id: {s: resolve_status(done=r[f"{s}_done"], failed=r[f"{s}_failed"],
                                     in_flight=str(r.id) in live[s]) for s in stages} for r in rows}
```

Two round-trips, no joins across the boundary, no Alembic exposure.

**One measured risk to record in VERIFICATION:** `get_live_job_keys()` materializes *every* queued/active key. During the 2026-06 incidents that set reached 44,500 rows; a bulk enqueue of the 200 K corpus makes it 200 K keys ≈ 10 MB of Python strings, on the 5 s poll. Mitigation: add an optional `function=` argument to `get_live_job_keys` that pushes `split_part(key, ':', 1) = :fn` into the static SQL (the `_STAGE_BUSY_SQL` at `pipeline.py:458` already proves `split_part` works here), and measure the poll. **Record the number.** The design's Risks table calls out exactly this class.

---

## 5. Q4 / D-01 — `scheduling_ledger` vs `saq_jobs` for `in_flight`

### 5.1 Recommendation: `in_flight = saq_jobs(queued|active)` ALONE. Reject the union.

The design's D-01 recommends the union with the rationale: *"Using `saq_jobs` alone re-opens the crash-window that the ledger was introduced to close (a worker dies mid-job → no `saq_jobs` row, no output row → the file reads `not_started` and gets re-enqueued by every poll)."*

Every clause of that rationale is falsified by the tree:

**(1) The broker is Postgres. `saq_jobs(queued|active)` already survives worker and controller death.**
`tasks/reenqueue.py:11-18` — *"THE DURABILITY REFRAME (Phase 42 … READ THIS BEFORE 'RESTORING' ANYTHING): Phase 36 migrated the SAQ broker from Redis to Postgres (`saq_jobs` table, `PostgresQueue`). Queued and active jobs are now DURABLE across a controller restart — SAQ re-dequeues the surviving `saq_jobs` rows itself, and reclaims timed-out `active` jobs on its own."* The crash window D-01 cites was closed by the broker migration, not by the ledger. The ledger closes a *different* window: broker truncate / restore-from-backup / fresh migration (`reenqueue.py:14-16`, `models/scheduling_ledger.py:13-15`).

**(2) Nothing enqueues on a poll.** The only producers are the manual DAG trigger endpoints and `recover_orphaned_work` (gated on `count_inflight_jobs() == 0` unless `force=True`). `reenqueue.py:17-18` — *"Steady state produces ZERO automatic enqueues — DO NOT re-introduce a steady-state auto-advance cron."* The "re-enqueued by every poll" hazard does not exist here.

**(3) The union creates a permanent-stuck class.** The ledger row is cleared only on *terminal*, and for the three enrich stages the clear happens in the **control-side callback handlers**, not in SAQ's `after_process`: `deterministic_key.py:198-214` clears only when `job.queue.ledger_sessionmaker` is present, and it is present **only on control-side queues** (`deterministic_key.py:137-140`) — the agent worker is Postgres-free by construction. So the clears live at `agent_metadata.py:93` (`put_metadata`), `report_metadata_failed`, `agent_fingerprint.py:54` (`put_fingerprint`), `report_fingerprint_failed`, `agent_analysis.put_analysis`, `agent_analysis.py:331` (`report_analysis_failed`).

  A hard-killed agent process (OOM, `docker kill`, node reboot) posts no callback. The `saq_jobs` row is reclaimed and swept; the ledger row survives **until an operator clicks Recover**. Under the union that file reads `in_flight` **forever**, is permanently ineligible (`eligible = NOT done ∧ NOT in_flight`), and renders "running" in every UI surface. That is a *new* invisible-stuck class strictly worse than the one being closed — and it is precisely the class this milestone exists to eliminate (design §4.1 bug 1, bug 4).

**(4) Precedence makes it worse.** Design §2.3 fixes `in_flight ≻ done`. A stale ledger row would therefore **mask a completed stage**. Normally the clear and the output write share a transaction — but not always: `agent_fingerprint.put_fingerprint` clears the single per-file key `fingerprint_file:<id>` on the **first engine's** PUT (its docstring at `agent_fingerprint.py:48-53` says so), while a second engine's job may still be `active`. The ledger's granularity already is not the stage's granularity. `saq_jobs` is.

**(5) The degrade direction inverts.** Under saq_jobs-alone, a degraded read gives `in_flight = ∅` → pending over-includes → dedup no-ops. Under the union, a degraded `saq_jobs` read collapses `in_flight` to *ledger-only* — and the ledger is Alembic-managed and never degrades, so `in_flight` becomes "everything ever scheduled and not terminal", the pending set **under**-includes, and the trigger button silently does nothing with no error. Under-inclusion is the worse mode under a degrade-to-safe-default discipline (§4.2).

### 5.2 What breaks if `in_flight` is `saq_jobs` alone?

Exactly one thing: the ~10-minute window after SAQ sweeps a terminal row whose callback never fired. In that window the file reads `not_started` and is eligible.

- **metadata / fingerprint:** eligible → the next bulk trigger re-enqueues it. Cheap, idempotent, correct — a self-heal, and it is what happens today (a hard-killed metadata job leaves the file `DISCOVERED`, which `pipeline.py:1339` already returns).
- **analyze:** eligible → a bulk "Analyze all" would sweep it in. **Same as today** (a hard-killed analyze leaves `DISCOVERED`, `get_discovered_files_with_duration`'s exact pending set, `pipeline.py:1106`). No regression. This is *not* the 44.5K incident: that was `recover_orphaned_work(force=True)` sweeping never-scheduled backlog (`reenqueue.py:20-27`), which the ledger fixed and which this change does not touch.
- `FAILURE_IS_TERMINAL[analyze] = true` still guards the case that matters: a *recorded* analyze failure never re-enters the pending set.

### 5.3 What breaks if it is the union?

- The permanent-stuck class of §5.1(3): a crashed agent job makes a file eternally `in_flight`, un-triggerable, misrendered, with no recourse short of Recover (`force=True` — the button whose blast radius the ledger exists to contain).
- `in_flight ≻ done` masking (§5.1(4)).
- Under-inclusive degrade (§5.1(5)).
- **A double-owner conflict with `recover_orphaned_work`.** Recovery's orphan predicate (`reenqueue.py:351`) is `ledger − live_keys − domain_completed − in_flight_cloud_job`. If `stage_status`'s `in_flight` also swallows the ledger, "orphaned" and "in_flight" become the *same* set with opposite meanings: recovery says "replay it", `stage_status` says "it's running, leave it". Two components disagreeing about one file is the exact double-owner vector `SCHED-05` (`reenqueue.py:212-227`) was written to close.

### 5.4 What the ledger keeps doing

Nothing changes for `scheduling_ledger`. It stays recovery's durable, orthogonal "was scheduled, not yet terminal" record. `recover_orphaned_work` keeps `orphaned = ledger − live − domain_completed`, with `domain_completed` now sourced from `STAGE_SPECS` instead of hand-rolled state lists (§3.2 (d)).

**Optional, cheap, worth adding:** a *separate* diagnostic predicate for the residual window —

```python
def scheduled_not_terminal(stage: Stage) -> ColumnElement[bool]:
    fn = STAGE_SPECS[stage].saq_function
    return exists(select(SchedulingLedger.key).where(
        SchedulingLedger.key == f"{fn}:" + cast(FileRecord.id, String)))
```

(the `cast(FileRecord.id, String)` concatenation idiom is in-tree at `pipeline.py:1289`). Surface `scheduled_not_terminal ∧ ¬in_flight ∧ ¬done ∧ ¬failed` as a **"stale scheduled" operator card** — the honest signal for "a worker died and never acked". Never fold it into `in_flight`.

---

## 6. D-02 — Failure markers

### Recommendation: columns on the existing output tables. Confirm the design, with three precisions.

| Stage | Marker | Migration | `done` predicate change |
|---|---|---|---|
| `analyze` | `analysis.analysis_failed_at TIMESTAMPTZ NULL` + `analysis.error_message TEXT NULL` | `032` | none — `done` already keys on `analysis_completed_at IS NOT NULL` (`models/analysis.py:38`), so `(completed_at NULL, failed_at NOT NULL)` is unambiguous |
| `metadata` | `metadata.failed_at TIMESTAMPTZ NULL` + `metadata.error_message TEXT NULL` | `032` | **tighten** to `EXISTS metadata WHERE file_id=… AND failed_at IS NULL` — a failure inserts a metadata row with NULL payload columns |
| `fingerprint` | reuse `fingerprint_results.status='failed'` | none | `failed ≡ ¬done ∧ EXISTS(status='failed')` |

**Precision 1 — the `≤1 row` invariant survives.** `metadata.file_id` and `analysis.file_id` both carry `unique=True` (`models/metadata.py:18`, `models/analysis.py:19`). A failure marker is a column on the one row, not a second row. A generic `stage_failure(file_id, stage, …)` table would need its own FK, its own uniqueness constraint, a second write path in every `/failed` handler, and a second table in every `NOT EXISTS` anti-join. Columns win on every axis.

**Precision 2 — `report_analysis_failed` becomes an `analysis` upsert.** Today it writes only `files` (`agent_analysis.py:329`). It must now `pg_insert(AnalysisResult) … on_conflict_do_update(index_elements=["file_id"], set_={"analysis_failed_at": now(), "error_message": …})`. The pattern is two functions up: `put_analysis_progress` (`agent_analysis.py:288-302`) does a counter-only upsert on the same table with an explicit `id=uuid.uuid4()` (because `AnalysisResult.id` has a Python-side default `pg_insert` bypasses). Copy that idiom exactly, including the explicit `id`.

**Precision 3 — `report_fingerprint_failed` should keep persisting nothing, and that is now *correct*.** The design's §2.3 lists it as a gap. But `FAILURE_IS_TERMINAL[fingerprint] = false` (design §3): a failed fingerprint auto-retries. A hard exception before any engine PUT therefore *should* leave the file `not_started` and re-eligible — a marker would be inert at best, and writing a synthetic `fingerprint_results(engine='_task', status='failed')` row would poison the two aliased per-engine joins at `pipeline.py:939-940` and `_trackid_engine_badge` (`pipeline.py:864`). **Leave it as a ledger-clear-only ack, and add a regression test asserting a hard-failed fingerprint is back in the fingerprint pending set.** Document the asymmetry in the docstring.

---

## 7. D-03 — `AWAITING_CLOUD` and `LOCAL_ANALYZING`

### 7.1 `LOCAL_ANALYZING` → no storage at all

`models/file.py:51-63` documents its *entire* purpose: *"it removes the file from `get_cloud_staging_candidates` (which selects `state == AWAITING_CLOUD`) while its local `process_file` is in flight, so a locally-spilled file can NOT be double-dispatched to a cloud backend once a slot frees."*

That job is served by flipping the **routing decision**, not a lane state. `LocalBackend.dispatch` (`backends.py:234`) currently writes `file.state = LOCAL_ANALYZING`; it becomes `analyze_route.route = 'local'`. The candidate query selects `route = 'cloud'`. Identical guarantee, one column, no new state. The `in_flight(analyze)` derivation is the belt to that suspender.

### 7.2 `AWAITING_CLOUD` → a NEW `analyze_route` table, not a `cloud_job.status` member

The design offers two options. **Recommend `analyze_route`.** Reasoning, grounded in the four writers and seven readers:

**Writers of `AWAITING_CLOUD` today:**
| Site | Context | `cloud_job` row exists? |
|---|---|---|
| `routers/pipeline.py:345` (duration router) | first routing decision | **no** |
| `routers/agent_push.py:261` (`/mismatch` over-cap spill) | terminalizes `cloud_job → FAILED, attempts=max` | yes |
| `routers/agent_s3.py:195` (`report_upload_failed` spill) | terminalizes `cloud_job → FAILED, attempts=max` | yes |
| `tasks/reconcile_cloud_jobs.py:212` (submit-cap spill) | terminalizes `cloud_job → FAILED` | yes |

**Readers of `cloud_job.status`:** `_BaseBackend.in_flight_count` (`backends.py:178` — the **per-backend cap**), `KueueBackend.reconcile` (`backends.py:470`), `reenqueue._in_flight_cloud_job_ids` (`reenqueue.py:227` — the **single-recovery-owner** guard, SCHED-05), `pipeline.get_inadmissible_count` (`pipeline.py:1143`), `backends._admission_by_backend_id` (`backends.py:613`), plus the `IN_FLIGHT` tuple (`backends.py:76`) they all key on.

Adding `'awaiting'` to `ck_cloud_job_status_enum` puts a new member into the substrate of **five independent cap / recovery / admission predicates** — every one a load-bearing guard against double-dispatch or over-enqueue, the exact incident class this project has bled from four times. And it would *destroy information*: the three spill writers currently record `cloud_job.status = FAILED` **and** `files.state = AWAITING_CLOUD` simultaneously — two facts, one row can't hold both.

`analyze_route` keeps the two facts orthogonal:

```python
# src/phaze/models/analyze_route.py  [NEW]
class AnalyzeRoute(TimestampMixin, Base):
    __tablename__ = "analyze_route"
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), primary_key=True)
    route: Mapped[str] = mapped_column(String(8), nullable=False)   # 'local' | 'cloud'
    __table_args__ = (CheckConstraint("route IN ('local', 'cloud')", name="route_enum"),)
```

Then:
- the three spill writers keep their `cloud_job → FAILED` write **byte-identical** and simply *stop* writing `files.state`; `analyze_route.route` is already `'cloud'` and stays so;
- the duration router (`pipeline.py:345`) upserts `analyze_route(file_id, 'cloud')` instead of the state flip;
- `LocalBackend.dispatch` flips `route → 'local'`;
- the drain candidate (`get_cloud_staging_candidates`, `pipeline.py:1248`) becomes:

```sql
SELECT files.* FROM files JOIN analyze_route ar ON ar.file_id = files.id
WHERE ar.route = 'cloud'
  AND NOT EXISTS (SELECT 1 FROM cloud_job cj WHERE cj.file_id = files.id
                  AND cj.status IN ('uploading','uploaded','submitted','running','pushed'))
  AND NOT <analyze done>  AND NOT <analyze failed>
ORDER BY files.created_at ASC LIMIT :n
FOR UPDATE OF analyze_route SKIP LOCKED;
```

⚠ **The `'pushed'` term is load-bearing and easy to miss.** Today `files.state = PUSHED` keeps a landed-but-analyzing file out of the drain. `agent_push.py:146` sets `cloud_job.status = SUCCEEDED` on `/pushed`, and `SUCCEEDED` is *terminal* (excluded from `IN_FLIGHT`, `backends.py:76`). So without a new marker, a compute-pushed file would be **re-picked by the drain while its remote analysis is still running.** Fix: add `CloudJobStatus.PUSHED = "pushed"` to the CHECK, have `/pushed` write `PUSHED` instead of `SUCCEEDED`, and define **two** status sets:

```python
IN_FLIGHT    = (UPLOADING, UPLOADED, SUBMITTED, RUNNING)      # unchanged — drives CAPS
NOT_TERMINAL = (*IN_FLIGHT, PUSHED)                            # NEW — drives DRAIN CANDIDACY
```

Keeping `IN_FLIGHT` unchanged preserves the compute cap semantics byte-identically (a slot frees at land, exactly as today) and preserves `_in_flight_cloud_job_ids`, `get_inadmissible_count`, `_admission_by_backend_id` — while `NOT_TERMINAL` restores the `PUSHED` exclusion the drain used to get from `files.state`. Record this as a decision; a plan that adds `PUSHED` to `IN_FLIGHT` "for tidiness" silently changes the compute cap from *pushes-in-flight* to *analyses-in-flight*.

### 7.3 A bonus the roadmapper should exploit: the CAS guards collapse to one row

`agent_push.py:120-127` and `:255-262` and `agent_s3.py:126-128` each perform a **two-row CAS**: `UPDATE files SET state=X WHERE id=? AND state='pushing'` (checking `rowcount`), *then* `UPDATE cloud_job SET status=…`. `agent_s3.py:195` (`report_upload_failed`) is missing the guard entirely — design §4.1 bug 6.

Under `cloud_job.status` ownership both statements merge:

```python
res = await session.execute(
    update(CloudJob).where(CloudJob.file_id == file_id, CloudJob.status == CloudJobStatus.SUBMITTED.value)
                    .values(status=CloudJobStatus.PUSHED.value))
if res.rowcount == 0:
    return PushedResponse(file_id=file_id)   # idempotent no-op
```

One row, one CAS, structurally impossible to have "one guard present, its sibling missing". Bug 6 doesn't get fixed — it becomes unrepresentable. Same for `/mismatch` (`agent_push.py:255`) and `reconcile_cloud_jobs.py:212`.

### 7.4 `DUPLICATE_RESOLVED` → a new `dedup_resolution` table

```python
class DedupResolution(TimestampMixin, Base):
    __tablename__ = "dedup_resolution"
    file_id: Mapped[uuid.UUID] = mapped_column(..., ForeignKey("files.id"), primary_key=True)
    canonical_file_id: Mapped[uuid.UUID] = mapped_column(..., ForeignKey("files.id"), nullable=False)
```

`services/dedup.py` has 9 `state != DUPLICATE_RESOLVED` sites (lines 78, 90, 128, 141, 188, 209, 221, 235, 260) → all become `~exists(select(DedupResolution.file_id).where(DedupResolution.file_id == FileRecord.id))`. `resolve_group` (`:268`) inserts rows; `undo_resolve` (`:274-286`) **deletes** them. Simplification worth noting: `undo_resolve` currently round-trips a `previous_state` blob through the HTTP layer (`routers/duplicates.py:151, 177, 214, 242`) purely to restore a clobbered enum. With a marker row, undo is `DELETE FROM dedup_resolution WHERE file_id = ANY(:ids)` and the `previous_state` payload disappears from the router contract. Real code deleted, not moved.

---

## 8. D-04 — the metadata "backfill" button

### Recommendation: yes, `eligible = NOT done ∧ NOT in_flight`. And rewrite `is_domain_completed` in the *same phase*, not after.

`get_metadata_pending_files` (`pipeline.py:1330-1341`) returns **every** music/video file, forever — 200 K rows into memory, on every trigger, relying on the deterministic key to suppress re-runs. Under derivation it becomes a `NOT EXISTS` anti-join. Strict improvement.

**The coupling that must not be missed.** `reenqueue.py:266` defines metadata's domain-completed as *"absent from `get_metadata_pending_files`"*. Because the pending set is currently *everything*, that branch is **structurally inert** — always `False`. The moment the pending set gains a `NOT done` term, `is_domain_completed` starts firing. And the moment it gains a `NOT in_flight` term, "absent from pending" means `done OR in_flight` — recovery would treat an in-flight-but-not-live-keyed row as domain-completed and skip its replay.

(It happens to work out, because orphaned rows satisfy `key NOT IN live` so `in_flight` is false for them. But recovery's correctness would then silently depend on the pending set's `in_flight` term through a double negation, via a function whose docstring says "we reuse those queries' membership". That is a trap.)

**Do this instead:** delete the pending-set reuse from `_build_done_sets` (`reenqueue.py:136-167`) and define recovery's predicate directly from the registry:

```
domain_completed(stage) ≡ done(stage) ∨ (failed(stage) ∧ FAILURE_IS_TERMINAL[stage])
```

- `process_file` → `analysis_completed_at IS NOT NULL ∨ analysis_failed_at IS NOT NULL`. **Preserves the load-bearing warning at `reenqueue.py:179-186` verbatim** (a failed 4-hour analysis is deliberately analyze-DONE, never auto-loops).
- `extract_file_metadata` → `EXISTS metadata` (either `failed_at` null or not — a failed metadata is terminal, so both count).
- `fingerprint_file` → `done(fingerprint)` only. `failure_is_terminal=False`, so a failed fingerprint re-drives. Matches today.
- `push_file` → `cloud_job.status IN ('pushed','succeeded') ∨ done(analyze) ∨ failed(analyze)` (replaces `_select_done_push_ids`, `reenqueue.py:190-197`).

`_get_awaiting_cloud_ids` (`reenqueue.py:200-209`) → `analyze_route.route = 'cloud' ∧ ¬EXISTS(cloud_job in NOT_TERMINAL)`.
`_in_flight_cloud_job_ids` (`reenqueue.py:212-227`) → **unchanged** (`IN_FLIGHT` is unchanged).

**Also re-check PROV-01.** Design §9 says "not fixing PROV-01, though `reenqueue.py` is heavily touched, so re-check overlap." Confirmed: `recover_orphaned_work:390` still does `select_active_agent(session, kind="compute")` — a single-active-compute pick Phase 72 retired everywhere else (memory GAP-01). Rewriting `_build_done_sets` puts a plan directly adjacent. **Recommend: do not fix it here** (a feature with Phase-45-class over-enqueue risk), but add a `# PROV-01: still single-active-compute` breadcrumb at that line and a test pinning current behavior so the refactor cannot silently change it.

---

## 9. D-05 — which latent bugs land here

| # | Bug | Verdict | Reason |
|---|---|---|---|
| 1 | Enrich deadlock (`pipeline.py:1106` vs `:1359` vs `agent_metadata.py:89`) | **HERE** | It *is* the milestone. |
| 3 | Rescan wipes progress (`ingestion.py:114`, `agent_files.py:132`: `"state": excluded.state`) | **HERE, FIRST phase** | Two-line deletion from the `on_conflict_do_update` `set_` dict; enum-independent; strict fix; shippable no-flag day one. |
| 4 | Metadata failures invisible (`agent_metadata.report_metadata_failed`) | **HERE** | The `metadata.failed_at` marker is the fix (D-02). |
| 7 | `get_stage_progress` over-counts analyze done (`pipeline.py:383`) | **HERE** | Deleted by construction the moment `done_count_stmt(ANALYZE)` composes `_analyze_done()`. |
| 5 | `store_proposals` can regress a `MOVED` file (`proposal.py:39` `_TERMINAL_FILE_STATES`) | **HERE, by evaporation** | `store_proposals` stops writing `file_record.state` (`proposal.py:373`); `propose.done` is proposal-row existence. Guard and bug both cease to exist. |
| 6 | `report_upload_failed` has no CAS guard (`agent_s3.py:195`) | **HERE, by evaporation** | §7.3 — the two-row CAS collapses to a one-row `cloud_job` CAS that is structurally guarded. |
| 2 | Tag writing permanently dead — `state == EXECUTED` never written | **HERE (forced), OWN phase + UAT** | ⚠ The one behavior-*reviving* change. The gate is at `tag_writer.py:185`, `review.py:109,251`, `tags.py:44,174,179,336,422`, `cue.py:48,89,251`, `tracklists.py:138,600,897` — 14 sites. Replacing `state == EXECUTED` with `EXISTS(execution_log JOIN proposals WHERE proposals.file_id = files.id AND execution_log.status='completed')` (the exact join `execute_done_stmt` at `pipeline.py:361-366` uses) will make previously-invisible files appear in the Tag-write review queue + CUE-eligible set for the first time, on a *file-mutating* surface. Own requirement, own PR, own live UAT. Do not bundle with the enum drop. |

`FileState.FAILED` has zero writers and zero readers — it disappears silently.

---

## 10. Q6 — Data-flow changes (the seams)

### 10.1 The three enrich pending sets — the deadlock, dissolved

```
BEFORE                                          AFTER
─────────────────────────────────────────────   ────────────────────────────────────────────────
analyze      : state == DISCOVERED              analyze      : ¬done(analyze) ∧ ¬failed(analyze)
               (pipeline.py:1106)                              ∧ ¬in_flight(analyze)
fingerprint  : state == METADATA_EXTRACTED      fingerprint  : ¬done(fp) ∧ ¬in_flight(fp)
               ∪ (fp.status='failed' ∧                          [failed ⇒ still eligible, D-16]
                  state != FINGERPRINTED)
               (pipeline.py:1344-1376)
metadata     : ALL music/video files            metadata     : ¬done(md) ∧ ¬failed(md) ∧ ¬in_flight(md)
               (pipeline.py:1330)

  ⇒ mutually exclusive; no file can            ⇒ zero upstream; every discovered file eligible
    complete all three                            for all three, in any order
```

`get_discovered_files_with_duration` (`pipeline.py:1095`) is the *sole* analyze pending source for both the API trigger (`routers/pipeline.py:393`) and the UI trigger (`routers/pipeline.py:711`). It keeps its `(FileRecord, duration)` shape and its `OUTER JOIN metadata` (duration is needed by `_route_discovered_by_duration`); only the `WHERE` changes. **Rename it** — `get_analyze_pending_files` — `discovered` is a lie after this.

### 10.2 Full seam inventory

| Seam | File:line | Change |
|---|---|---|
| analyze pending | `services/pipeline.py:1095` | `state == DISCOVERED` → `pending_stmt(ANALYZE)`; rename |
| fingerprint pending | `services/pipeline.py:1344` | drop the `get_files_by_state(METADATA_EXTRACTED)` base + `state != FINGERPRINTED` filter → `pending_stmt(FINGERPRINT)` (the failed-retry union is *absorbed* by `failure_is_terminal=False`) |
| metadata pending | `services/pipeline.py:1330` | all-music/video → `pending_stmt(METADATA)` (**D-04 behavior change**) |
| proposal pending | `services/pipeline.py:1396` | `state IN (ANALYZED, METADATA_EXTRACTED)` term deleted; the two `EXISTS` terms already ARE `done(metadata) ∧ done(analyze)` → `pending_stmt(PROPOSE)`. **Sorting-before-chunking stays load-bearing** (`:1403`, the set-hash key) |
| `get_pipeline_stats` | `services/pipeline.py:58-68` | `GROUP BY state` → per-stage `done_count_stmt`. **Collapses into `get_stage_progress`.** Callers (`routers/pipeline.py:479, 621`) + `PIPELINE_STAGES` (`:46-55`) go with it |
| `get_stage_progress` | `services/pipeline.py:299` | `analyze.done` gains `analysis_completed_at IS NOT NULL` (bug 7); all `done` composed from `done_count_stmt` |
| `notYetEnriched` | `routers/pipeline.py:240` | `stats["discovered"] − stats["metadata_extracted"]` (subtraction of two *disjoint* buckets — nonsense) → `stage["metadata"]["total"] − stage["metadata"]["done"]`. **Zero new queries; both numbers already in `get_stage_progress`.** |
| `get_files_by_state` | `services/pipeline.py:744` | **DELETE** (its two callers are both rewritten) |
| `get_analyze_stage_files` | `services/pipeline.py:775` | `_ANALYZE_STAGE_STATES` + `completed = state == ANALYZED` → `bulk_stage_status`; the `cloud_job`-derived `lane` untouched |
| `get_analysis_failed_{files,count}` | `services/pipeline.py:1057, 1068` | → `analysis.analysis_failed_at IS NOT NULL` |
| `get_{awaiting_cloud,pushing,pushed}_count` | `services/pipeline.py:1112, 1206, 1224` | → `analyze_route` / `cloud_job.status` counts; keep `_safe_count` |
| `get_cloud_staging_candidates` | `services/pipeline.py:1248` | → the §7.2 join; `FOR UPDATE OF analyze_route SKIP LOCKED` |
| `_backfill_candidates_stmt` | `services/pipeline.py:1267` | `state == ANALYSIS_FAILED` → `analysis_failed_at IS NOT NULL`. **Keep the `scheduling_ledger` EXISTS term** (`:1289`) — the KROUTE-05 over-enqueue guard |
| `_build_done_sets`, `is_domain_completed`, `_select_done_analyze_ids`, `_select_done_push_ids`, `_get_awaiting_cloud_ids` | `tasks/reenqueue.py:136-268` | → `domain_completed_stmt` (§8). `_in_flight_cloud_job_ids` unchanged |
| `reconcile_cloud_jobs` | `tasks/reconcile_cloud_jobs.py:212` | drop `files.state = AWAITING_CLOUD` (the `cloud_job → FAILED` write carries the fact). **Preserve delete-after-record ordering (D-04) + per-row commit** |
| `stage_cloud_window` | `tasks/release_awaiting_cloud.py:187, 239` | reads new candidate query; `pg_advisory_xact_lock` + single post-loop commit untouched |
| `LocalBackend.dispatch` | `services/backends.py:234` | `state = LOCAL_ANALYZING` → `analyze_route.route = 'local'` |
| `ComputeAgentBackend.dispatch` | `services/backends.py:325` | `state = PUSHING` → drop (the `cloud_job` upsert at `:326-339` already writes `status=SUBMITTED`) |
| `KueueBackend.dispatch` | `services/backends.py:438` | `state = PUSHING` → drop. **Preserve CR-01 gate-before-mutate** — `_stage_file_to_s3` must precede any mutation |
| `/pushed`, `/mismatch` | `routers/agent_push.py:126, 261` | two-row CAS → one-row `cloud_job` CAS (§7.3) |
| `report_uploaded`, `report_upload_failed` | `routers/agent_s3.py:128, 195` | idem; bug 6 fixed by construction |
| `put_analysis` | `routers/agent_analysis.py:235` | drop `state = ANALYZED`; keep the `analysis_completed_at = func.now()` stamp (`:240`) — it *is* the done marker |
| `report_analysis_failed` | `routers/agent_analysis.py:329` | `state = ANALYSIS_FAILED` → `analysis` upsert with `analysis_failed_at` (§6 Precision 2) |
| `put_metadata` | `routers/agent_metadata.py:89` | **delete the `DISCOVERED → METADATA_EXTRACTED` UPDATE** (PR #221's patch; what closed the analyze door) |
| `report_metadata_failed` | `routers/agent_metadata.py:96+` | now writes `metadata(file_id, failed_at, error_message)` |
| `report_fingerprint_failed` | `routers/agent_fingerprint.py:59` | **unchanged** — ledger clear only (§6 Precision 3) |
| `services/dedup.py` | 9 sites + `resolve_group:268` + `undo_resolve:274` | → `dedup_resolution` marker; undo becomes a DELETE |
| `routers/duplicates.py` | `:151, 177, 214, 242` | `previous_state` payload leaves the undo contract |
| `services/fingerprint.py:256` | `get_fingerprint_progress` | 8-member `eligible_states` set → `total = music/video count`, `completed = done(fp)`, `failed = failed(fp)`. **Function-local DB import** (agent boundary, `:270`) |
| `services/proposal.py:39, 373` | `_TERMINAL_FILE_STATES`, `state = PROPOSAL_GENERATED` | **DELETE both** |
| `services/proposal_queries.py:166,168,186,188` | approve/reject file-state cascade | **DELETE** — `proposals.status` already authoritative |
| `routers/agent_proposals.py:115` | `file_record.state = new_file_state.value` | **DELETE** — `execution_log` + `proposals.status` carry the outcome |
| `routers/pipeline.py:345` | duration router `state = AWAITING_CLOUD` | → `analyze_route` upsert |
| `routers/pipeline.py:816` | backfill `state = DISCOVERED` | → clear `analysis.analysis_failed_at` |
| `routers/pipeline.py:935` | `retry_analysis_failed` → `FINGERPRINTED` | → clear `analysis_failed_at`. ⚠ the **only** writer of `FINGERPRINTED` (design §6.1) |
| `routers/pipeline.py:857` | `held_files = [… if state == AWAITING_CLOUD]` | → `analyze_route.route == 'cloud'` |
| 14 dead `EXECUTED` gates | `tag_writer.py:185`, `review.py:109,251`, `tags.py:44,174,179,336,422`, `cue.py:48,89,251`, `tracklists.py:138,600,897` | → `EXISTS(execution_log JOIN proposals …)`. **Own phase + UAT** (§9 bug 2) |
| `services/ingestion.py:114`, `routers/agent_files.py:132` | `"state": excluded.state` in `on_conflict_do_update` | **DELETE the key** (bug 3). Do it first |
| `templates/pipeline/partials/metadata_workspace.html:43,50` | raw enum in a "State" column | → per-stage status chips from `bulk_stage_status`. **Use `\|tojson`, not `\|e`, in Alpine JS context** (memory: XSS on apostrophe filenames) |
| `templates/pipeline/partials/analyze_workspace.html:81-86` | `f.state == 'awaiting_cloud' / 'analysis_failed'` | → derived lane + status |
| `templates/proposals/partials/proposal_row.html:46` | `proposal.file.state == "executed"` | → an `executed` boolean in the row dict |

### 10.3 Indexes

Mirror into `__table_args__` (house style: `alembic/versions/019_…py:72` + `models/proposal.py:57-60`):

```python
Index("ix_analysis_completed", "file_id", postgresql_where=text("analysis_completed_at IS NOT NULL"))
Index("ix_analysis_failed",    "file_id", postgresql_where=text("analysis_failed_at IS NOT NULL"))
Index("ix_metadata_ok",        "file_id", postgresql_where=text("failed_at IS NULL"))
Index("ix_fprint_success",     "file_id", postgresql_where=text("status IN ('success','completed')"))
Index("ix_analyze_route_cloud","file_id", postgresql_where=text("route = 'cloud'"))
```

Precedent for partial indexes: `018` (`WHERE tier='fine'`), `019` (`WHERE status='pending'`), `012` (`WHERE status='live'`), `014` (`WHERE revoked_at IS NULL`). Drop `ix_files_state` (`models/file.py:98`) in `033`.

---

## 11. Q7 — New vs modified components

### NEW files

| Path | Purpose |
|---|---|
| `src/phaze/enums/stage.py` | `Stage`, `StageStatus` StrEnums. DB-free, agent-safe. Mirrors `enums/execution.py`. |
| `src/phaze/services/stage_status.py` | `StageSpec`, `STAGE_SPECS`, `resolve_status`, `eligible`, `pending_stmt`, `done_count_stmt`, `domain_completed_stmt`, `bulk_stage_status`, `in_flight_ids`. Control-only leaf module. |
| `src/phaze/models/analyze_route.py` | `AnalyzeRoute` — the routing-decision sidecar (D-03). |
| `src/phaze/models/dedup_resolution.py` | `DedupResolution` — the dedup marker (§7.4). |
| `src/phaze/services/shadow_compare.py` | The §6.2 implication checks, as a library. |
| `src/phaze/scripts/shadow_compare.py` | Runnable entry (`just shadow-compare`) against a live corpus. |
| `alembic/versions/032_derive_stage_status.py` | Additive: 4 marker columns, 2 tables, `cloud_job.status` CHECK += `'pushed'`, 5 partial indexes, idempotent backfill from `files.state`. **Never references `saq_jobs`.** |
| `alembic/versions/033_drop_files_state.py` | Destructive: drop `ix_files_state`, drop `files.state`. |
| `tests/shared/test_stage_status.py` | Pure-unit: precedence table, eligibility table, the 6 anti-drift assertions of §3.3. |
| `tests/integration/test_migrations/test_032_*.py`, `…test_033_*.py` | Per-migration integration tests (house rule). |

### MODIFIED files

`src/phaze/tasks/_shared/stage_control.py` (re-key on `Stage`) ·
`src/phaze/models/{file,analysis,metadata,cloud_job,__init__}.py` ·
`src/phaze/services/{pipeline,backends,dedup,fingerprint,proposal,proposal_queries,ingestion,review,tag_writer}.py` ·
`src/phaze/tasks/{reenqueue,reconcile_cloud_jobs,release_awaiting_cloud}.py` ·
`src/phaze/routers/{pipeline,agent_files,agent_metadata,agent_analysis,agent_push,agent_s3,agent_proposals,duplicates,tags,cue,tracklists,shell}.py` ·
`src/phaze/templates/pipeline/partials/{metadata_workspace,analyze_workspace}.html`, `templates/proposals/partials/proposal_row.html` ·
`justfile` (add `shadow-compare`) · `docs/*` (runbook: drain before `033`)

### DELETED at `033`

`FileState` (`models/file.py:20-71`) · `files.state` column · `ix_files_state` · `PIPELINE_STAGES` (`pipeline.py:46`) · `get_files_by_state` (`pipeline.py:744`) · `_ANALYZE_STAGE_STATES` (`pipeline.py:766`) · `get_pipeline_stats` (`pipeline.py:58`) · `_TERMINAL_FILE_STATES` (`proposal.py:39`) · 20 `.state = …` writer statements.

**Agent boundary is safe.** `git grep FileState` across `tasks/agent_worker.py`, `services/agent_client.py`, `schemas/agent_push.py`, `config.py` returns **only docstrings/comments**. Deleting the enum does not cross the Postgres-free boundary. (`models/__init__.py:8,33` re-exports it — control-side.)

---

## 12. Q5 — Build order

### 12.1 The design's order, and what's wrong with it

> derivation layer → failure markers → sidecars → readers → UI → shadow-compare + destructive migration

Two problems:

1. **The derivation layer cannot precede the markers.** `failed_exists()` for `analyze` needs `analysis.analysis_failed_at` to exist. Schema first, or schema alongside.
2. **The shadow-compare is placed where it is least useful.** Its whole value is comparing two live representations. It must run *immediately after the backfill* (does the historical corpus derive correctly?) and *again* before the drop (do the new writers keep both in sync?). Running it once at the end, after the readers have flipped and `files.state` is a vestigial write-only column, tests almost nothing.

### 12.2 Recommended order

```
A ─ Additive schema + backfill + dual-write + rescan fix        [migration 032]
│     ├─ 4 marker columns, analyze_route, dedup_resolution,
│     │  cloud_job.status += 'pushed', 5 partial indexes
│     ├─ idempotent backfill FROM files.state (ON CONFLICT DO NOTHING)
│     ├─ writers write BOTH representations
│     ├─ bug 3: delete `"state": excluded.state` from the two upserts
│     └─ readers: UNTOUCHED
│     ⇒ SHIPPABLE, NO FLAG, ZERO reader-visible behavior change
│
B ─ Derivation layer (enums/stage.py, services/stage_status.py)  [no readers wired]
│     └─ + tests/shared/test_stage_status.py (the 6 anti-drift assertions)
│     ⇒ SHIPPABLE, NO FLAG. Dead code by construction → vulture whitelist entry
│
C ─ Shadow-compare, run for real on the live corpus              [THE GATE]
│     ⇒ must PASS before any of D. If it fails, the backfill is wrong,
│       and finding that out AFTER the readers flip is a production incident.
│
D ─ Readers, one seam per plan, ordered by blast radius:
│   D1 recovery      tasks/reenqueue.py            ← FIRST. See R2 below.
│   D2 counts        get_stage_progress + get_pipeline_stats + notYetEnriched
│   D3 pending sets  the three enrich sets + get_proposal_pending_batches
│                    ⇒ THE DEADLOCK IS FIXED HERE. Everything before is scaffolding.
│   D4 cloud         drain candidates + 3 dispatch flips + 4 CAS guards  ← ONE PLAN
│   D5 dedup         services/dedup.py + routers/duplicates.py
│   D6 fingerprint   get_fingerprint_progress
│   D7 apply/EXECUTED gates (14 sites)   ← OWN PR + LIVE UAT (behavior-reviving)
│   D8 proposals     proposal.py / proposal_queries.py / agent_proposals.py
│
E ─ UI: metadata_workspace, analyze_workspace, proposal_row      [presentation-only]
│
F ─ Destructive                                                  [migration 033]
      Gate: shadow-compare green on the live corpus
          + cloud-push lanes drained (`--profile drain`)
      ├─ remove the 20 `.state = …` writers
      ├─ drop ix_files_state, drop files.state
      └─ delete FileState + the static guard (`FileState` in 0 files under src/)
```

### 12.3 Dependency reasoning and half-migrated-state risks

**R1 — the cardinal rule: readers before writers, always.**
Readers flipped while writers still dual-write is *benign* (`files.state` becomes vestigial). Writers removed before readers flip is *catastrophic* (a reader sees a frozen state). Therefore **writer removal is the LAST step, same phase as the column drop.** Never remove a writer in D.

**R2 — why D1 (recovery) goes BEFORE D3 (pending sets).**
Today `is_domain_completed`'s metadata/fingerprint branches are *defined as* "absent from the pending set" (`reenqueue.py:266-268`). If D3 lands first, recovery's correctness silently starts depending on the pending set's new `in_flight` term through a double negation. Flipping recovery first is safe in the other direction: `domain_completed_stmt` is purely derived and consults no pending set, and the A backfill guarantees `{ANALYZED, ANALYSIS_FAILED} ≡ {completed_at NOT NULL} ∪ {failed_at NOT NULL}`. So D1 is independently correct while D3 is still legacy. **Do D1 first. Ship the `FAILURE_IS_TERMINAL[analyze]` regression test in the same PR, test-first.** This is the 44.5K-job seam.

**R3 — D4 must be one atomic plan.**
The drain candidate query, the three `Backend.dispatch` flips, and the four CAS guards are one consistency domain. Landing the candidate query (`route='cloud'`) before `LocalBackend.dispatch` flips `route='local'` means a locally-spilled file stays a drain candidate → double-dispatch. Landing `/pushed`'s `cloud_job → 'pushed'` before the candidate query grows its `'pushed'` exclusion (§7.2) means a landed-but-analyzing file gets re-picked. Ship them together, under the existing `pg_advisory_xact_lock(5_000_504)`, and preserve KueueBackend's CR-01 gate-before-mutate ordering (`backends.py:416-424` — moving the flip earlier makes SQLAlchemy autoflush persist a limbo PUSHING row).

**R4 — both migrations quiesce.**
The design's §6.2 requires `--profile drain` before `033`. It is *also* required before `032`: the backfill snapshots `files.state` for `PUSHING`/`uploading` files mid-rsync/mid-S3-upload. Mitigate by making the `032` backfill **idempotent** (`INSERT … ON CONFLICT DO NOTHING`) and shipping a re-runnable `just backfill-analyze-route`, so a file caught mid-flight between A and D can be repaired without a second migration.

**R5 — D7 (EXECUTED gates) is a behavior change, not a refactor.**
See §9 bug 2. It revives a dead path across 200 K files on a *file-mutating* surface. Own requirement, own PR, own live UAT.

### 12.4 What is independently shippable behind no flag?

| Phase | No flag? | Why |
|---|---|---|
| A | ✅ | Purely additive. Zero readers changed. Only observable delta: `report_metadata_failed` persists a marker nothing yet reads. |
| B | ✅ | Dead code by construction. `vulture` whitelist entry (2026.7.0 precedent). |
| C | ✅ | Read-only check + `just` recipe. |
| D1–D6, D8 | ✅ | Each is a `WHERE`-clause swap over a backfilled-equivalent representation, provable by the C gate. |
| D7 | ⚠ | Behavior-reviving. UAT-gated. |
| E | ✅ | Presentation-only. |
| F | ⚠ | Gated on C green + drained lanes. |

**No feature flag is warranted anywhere.** The dual-representation window *is* the flag: `files.state` remains written and readable through A→E, and the shadow-compare is the runtime assertion both agree. A boolean toggle on top would double the test matrix for a single-user admin tool with a two-step migration.

---

## 13. Architectural patterns to follow (all already in-tree)

### Pattern 1 — Predicate factories, not query functions
`StageSpec.done_exists` returns a `ColumnElement[bool]`, not a result set — the only way four consumers with four different query shapes (SELECT rows / COUNT / labelled columns / SELECT id) can share one definition. **Precedent:** `pipeline.py:1267 _backfill_candidates_stmt` returns a `Select` reused by both `count_backfill_candidates` and `get_backfill_candidates`, with the docstring calling out the anti-drift intent.

### Pattern 2 — SAVEPOINT + degrade-to-over-inclusive, never `session.rollback()`
Every `saq_jobs` read: `async with session.begin_nested(): …` / `except: log; return <safe default>`. A plain `session.rollback()` expires the dashboard's already-loaded ORM objects and 500s the page on the next lazy load — `pipeline.py:481-485` documents this. The safe default must be the one that *over*-includes (§4.2).

### Pattern 3 — Record-don't-rederive
`cloud_job.backend_id` / `cloud_job.staging_bucket` are read, never re-derived (Phase 70/73, `backends.py:441`, `agent_push.py:101`). `analyze_route.route` joins that family: the duration router *decides*; every downstream reader *reads*.

### Pattern 4 — DB-free enum module + control-side registry
`enums/execution.py` ↔ `models/execution.py`. `enums/stage.py` ↔ `services/stage_status.py`. The agent worker imports the enum; only the control plane imports the registry.

### Pattern 5 — Totality tests over registries
`reenqueue.py:436` (`_ALL_KEYED_FUNCTIONS = tuple(_KEY_BUILDERS)` + the T-45-17 test) and `pipeline.py:463` (`_BUSY_FUNCTION_TO_STAGE` computed inverse). Never hand-write an inverse map; compute it and test the bijection.

## 14. Anti-patterns to avoid

### AP-1 — Joining `saq_jobs` into the pending anti-join
*Tempting:* one round-trip; `in_flight` becomes "part of the query".
*Wrong:* drags an Alembic-invisible, SAQ-owned table into the ORM, puts the whole pending query inside a SAVEPOINT, and inverts the degrade from over-inclusive (safe, dedup-backstopped) to under-inclusive (a silently-inert trigger button).
*Instead:* §4.2 — a separate degradable key-set read, subtracted in Python.

### AP-2 — Folding `scheduling_ledger` into `in_flight`
*Tempting:* it looks like durability.
*Wrong:* §5.1 — the broker already provides that durability; the ledger clear is callback-driven (lossy on hard kills); `in_flight ≻ done` masks completion; and it creates a two-owner disagreement with `recover_orphaned_work`.
*Instead:* keep the ledger recovery-only; expose `scheduled_not_terminal` as a separate diagnostic.

### AP-3 — Widening `cloud_job.status` to carry the routing decision
*Tempting:* no new table.
*Wrong:* §7.2 — `cloud_job.status` is the substrate for five cap/recovery/admission predicates and the `IN_FLIGHT` tuple. Widening it forces re-auditing all five, and it destroys the "this backend failed *and* the file is still routed to cloud" pair the three spill writers record.
*Instead:* `analyze_route`, orthogonal.

### AP-4 — Adding `PUSHED` to `IN_FLIGHT`
*Tempting:* symmetry.
*Wrong:* it silently changes the compute cap from *pushes-in-flight* to *analyses-in-flight*. That may even be right — but it is a routing-policy change, and design §9 says "no change to routing *policy*."
*Instead:* two sets — `IN_FLIGHT` (caps, unchanged) and `NOT_TERMINAL = IN_FLIGHT ∪ {PUSHED}` (drain candidacy).

### AP-5 — Adding a denormalized stage-bitmap column
Design §5, explicit YAGNI. Derive; add partial indexes; **measure the 5 s poll and record the number in VERIFICATION.** Denormalize only against a measurement.

### AP-6 — "Fixing" `FINGERPRINTED` in the migration
Design §6.1: `FINGERPRINTED`'s only writer is `retry_analysis_failed` (`routers/pipeline.py:935`), rolling a file *out of* `ANALYSIS_FAILED`. Such files may have no successful `fingerprint_results` row. Under derivation they correctly become `fingerprint: not_started` and get re-fingerprinted. **This is the one documented, expected shadow-compare divergence.** Assert *implication*, never equality (§6.2), and note it in the migration docstring.

---

## 15. Integration points

### Internal boundaries

| Boundary | Communication | Considerations |
|---|---|---|
| `services/stage_status.py` → `saq_jobs` | via `pipeline.get_live_job_keys` only | static SQL, SAVEPOINT, degrade to `set()`. Alembic never touches it. Add a `function=` filter to bound the key-set at 200 K scale (§4.3) |
| `enums/stage.py` → agent worker | import only | must stay free of `phaze.models`, `phaze.database`, `sqlalchemy.ext.asyncio` (`tasks/_shared/stage_control.py:23-28`; `tests/test_task_split.py` enforces) |
| `services/fingerprint.py` → `stage_status` | **function-local import** | the agent worker imports this module (`fingerprint.py:266-271`) |
| `tasks/reenqueue.py` → `stage_status` | direct | control-only module; already imports `services/pipeline` |
| `services/backends.py` → `analyze_route` | direct, caller's session, **no commit** | the drain owns the single post-loop commit under `pg_advisory_xact_lock` (Landmine L1, `backends.py:318-320`) |
| Alembic → `saq_jobs` | **forbidden** | banner in every migration since `020`; `032`/`033` must carry it |
| `/pipeline/stats` (5 s) → everything | must never 500 | every new read gets `_safe_count` or a SAVEPOINT |

### Test-bucket placement (`tests/buckets.json`, one bucket per file, `tests/shared/test_partition_guard.py`)

| New test | Bucket |
|---|---|
| `test_stage_status.py` (pure registry + precedence) | `shared` |
| pending-set + counts regressions | `metadata`, `fingerprint`, `analyze` respectively |
| recovery / `is_domain_completed` | `agents` (where `test_recovery.py` lives) |
| migration `032`/`033` + shadow-compare | `integration` |

Every new test must pass via `just test-bucket <bucket>` **in isolation** (memory: `get_settings` `lru_cache` leak + `saq_jobs` stub poison), and DB tests need `TEST_DATABASE_URL` / `PHAZE_QUEUE_URL` pointed at `:5433`.

---

## 16. Open decisions — consolidated recommendations

| ID | Decision | **Recommendation** | Grounding |
|---|---|---|---|
| **D-01** | `in_flight` = `saq_jobs` alone, or `∪ scheduling_ledger`? | **`saq_jobs(queued\|active)` ALONE.** Ledger stays recovery-only; add a separate `scheduled_not_terminal` diagnostic. | `reenqueue.py:11-18` (broker already durable); `deterministic_key.py:198-214` (ledger clear callback-driven → lossy on hard kill); design §2.3 (`in_flight ≻ done` would mask completion); `reenqueue.py:212-227` (union = two-owner disagreement with SCHED-05). §5 |
| **D-02** | Failure markers: columns, or a generic `stage_failure` table? | **Columns.** `analysis.analysis_failed_at` + `error_message`; `metadata.failed_at` + `error_message`; tighten `done(metadata)` to `failed_at IS NULL`. `report_fingerprint_failed` deliberately persists nothing (its failure is non-terminal). | `models/{metadata,analysis}.py` `unique(file_id)`; `agent_analysis.py:288-302` upsert idiom; design §3 `FAILURE_IS_TERMINAL[fingerprint]=false`. §6 |
| **D-03** | `AWAITING_CLOUD` / `LOCAL_ANALYZING`? | **`LOCAL_ANALYZING`: fully derived** — `LocalBackend.dispatch` flips `analyze_route.route='local'`; no storage. **`AWAITING_CLOUD`: a NEW `analyze_route(file_id PK, route)` table**, NOT a `cloud_job.status` member. **PLUS** `CloudJobStatus.PUSHED` + a `NOT_TERMINAL = IN_FLIGHT ∪ {PUSHED}` set for drain candidacy — `IN_FLIGHT` (caps) unchanged. | `models/file.py:51-63` (LOCAL_ANALYZING's sole job); `backends.py:76,178`, `reenqueue.py:227`, `pipeline.py:1143`, `backends.py:613` (five readers of `cloud_job.status`); `agent_push.py:146` (`/pushed` → SUCCEEDED, terminal → would re-enter the drain). §7 |
| **D-04** | Does the metadata button keep re-enqueueing done files? | **No.** `eligible = ¬done ∧ ¬in_flight`. And **rewrite `is_domain_completed` from the registry in the same phase** — do NOT keep the "absent from pending set" formulation. Ship recovery (D1) *before* the pending sets (D3). | `pipeline.py:1330-1341` (pending = everything ⇒ `reenqueue.py:266` structurally inert); §8, §12.3 R2 |
| **D-05** | Fix the 6 latent bugs here, or split? | **1, 3, 4, 7 here** (they are the removal). **5, 6 evaporate** (no guard left). **2 (dead `EXECUTED` gates) forced here but OWN phase + live UAT** — it revives tag-writing across 200 K files on a file-mutating surface. Bug 3 (rescan wipe) ships in phase A as a two-line deletion. | §9 |

---

## 17. Confidence

| Area | Confidence | Reason |
|---|---|---|
| Module placement (Q1) | **HIGH** | Read all 38 `services/` module names + the `_shared` boundary rule + the `enums/execution.py` precedent verbatim |
| `StageSpec` shape (Q2) | **HIGH** | Composition of existing in-tree idioms (`_backfill_candidates_stmt` → `Select`; `_BUSY_FUNCTION_TO_STAGE` computed inverse); no new dependency |
| `saq_jobs` decoupling (Q3) | **HIGH** | Multiple existing SAVEPOINT reads; `get_live_job_keys` already returns exactly the needed set; the dedup backstop is documented at `pipeline.py:1461` and `reenqueue.py:100-105` |
| D-01 (reject the union) | **HIGH** | Every clause of the design's rationale contradicted by a specific line; the permanent-stuck failure mode is mechanical |
| D-03 (`analyze_route` + `PUSHED`) | **MEDIUM-HIGH** | The `/pushed → SUCCEEDED → drain re-picks` hazard is inferred from `agent_push.py:146` + `backends.py:76` + `pipeline.py:1259`. **Verify with a live/integration test before committing the drain-candidate query** — the sharpest new-regression risk in the milestone |
| Build order (Q5) | **HIGH** | R2 (recovery-before-pending) derives directly from `reenqueue.py:266`'s definition; R3/R4 from the advisory-lock + quiesce constraints already in the design |
| Seam inventory (Q6) | **HIGH** | `git grep`-verified, 23 source files, line numbers cited |
| 200 K-scale poll latency | **LOW** | Not measured. Partial indexes are the plan; the `get_live_job_keys` set size is the unmeasured risk (§4.3). **Must be measured and recorded in VERIFICATION**, per the design's own Risks table |

---

*Architecture research for: phaze 2026.7.5 Parallel Enrich DAG*
*Researched: 2026-07-08 · tree: `SimplicityGuy/true-parallel` off `main` @ `ce0c6434`*
