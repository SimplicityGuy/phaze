# Phase 55: Routing, state & ledger integration (the live seam) - Pattern Map

**Mapped:** 2026-06-28
**Files analyzed:** 22 (new + modified)
**Analogs found:** 22 / 22 (this phase is pure composition of existing tested seams — every file has an in-repo analog)

> RESEARCH.md already pins every edit site with `file:line`. This map **verifies** those analogs against the
> live source and extracts the concrete excerpt the executor copies. Where RESEARCH and the verified source
> diverge, the verified line is authoritative and flagged.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/config.py` (cloud_target field + 3 validators) | config | request-response (startup) | existing `cloud_burst_enabled` field + `_enforce_*_when_cloud_enabled` validators (same file) | exact (in-place rewrite) |
| `src/phaze/tasks/release_awaiting_cloud.py` (k8s stage branch) | task (cron) | event-driven / batch | `stage_cloud_window` a1 loop (same file) | exact (extend) |
| `src/phaze/services/cloud_staging.py` (no-commit core extract) | service | file-I/O / CRUD | `stage_file_to_s3` (same file) | exact (refactor-extract) |
| `src/phaze/routers/agent_s3.py` (`report_uploaded` extend) | route (callback) | request-response | `routers/agent_push.py::report_pushed` | exact |
| `alembic/versions/027_add_cloud_job_cloud_phase.py` (new) | migration | DDL | `alembic/versions/026_add_cloud_job_kube_columns.py` | exact |
| `src/phaze/models/cloud_job.py` (`cloud_phase` col + `CloudPhase` enum) | model | — | `CloudJobStatus` + `status` column (same file) | exact |
| `src/phaze/tasks/submit_cloud_job.py` (seed `cloud_phase`) | task | CRUD | the existing `pg_insert(...).on_conflict_do_update` upsert (same file) | exact (extend) |
| `src/phaze/tasks/reconcile_cloud_jobs.py` (`cloud_phase` writes) | task (cron) | event-driven | `_reconcile_one` status writes + `_record_success` (same file) | exact (co-write) |
| `src/phaze/services/pipeline.py` (`get_cloud_phase_counts`, ledger-scoped backfill query) | service | CRUD (read) | `get_inadmissible_count` + `_backfill_candidates_stmt` (same file) | exact |
| `src/phaze/routers/pipeline.py` (card wiring + backfill k8s fork) | route | request-response | `inadmissible_count` seeding (498/564) + `trigger_backfill_cloud` (657) | exact |
| `templates/pipeline/partials/admission_state_card.html` (new) | component | — | `partials/inadmissible_card.html` (structure) + `staged_pushing_card.html` (tiles) | exact (per 55-UI-SPEC) |
| `templates/pipeline/dashboard.html` (include) | component | — | inadmissible_card include at `dashboard.html:35` | exact |
| `templates/pipeline/partials/stats_bar.html` (OOB re-push) | component | — | inadmissible_card OOB include at `stats_bar.html:90` | exact |
| `templates/pipeline/partials/backfill_response.html` (copy) | component | — | self (string edit only) | exact |
| `tests/test_no_default_queue_producers.py` (AST guard ext) | test | static (ast) | self (existing `_ProducerVisitor` scanner) | exact |
| `tests/test_config/test_cloud_target.py` (new) | test | unit | `tests/test_config/test_cloud_burst_toggle.py` (replaces) | role-match |
| `tests/test_staging_cron.py` (k8s case) | test | unit | existing a1 window-cap cases (same file) | exact |
| `tests/test_routers/test_agent_s3.py` (k8s post-staging) | test | unit | existing `report_uploaded` idempotency cases (same file) | exact |
| `tests/test_routers/test_pipeline.py` (backfill-k8s) | test | unit | existing backfill cases `833-868` (same file) | exact |
| `tests/test_migrations/test_migration_027_cloud_phase.py` (new) | test | unit | `tests/test_migrations/test_migration_025_cloud_job.py` | role-match |
| `tests/test_tasks/test_submit_cloud_job.py` / `test_reconcile_cloud_jobs.py` (extend) | test | unit | self (existing seed/condition cases) | exact |

---

## Pattern Assignments

### `src/phaze/routers/agent_s3.py` — extend `report_uploaded` (route, request-response) — D-01b, KROUTE-03/04

**Analog:** `src/phaze/routers/agent_push.py::report_pushed` (the a1 sibling that already does the PUSHED flip + downstream enqueue).

**Signature problem (VERIFIED):** `report_uploaded` (`agent_s3.py:58-64`) has **no `request: Request`** param — it cannot reach `app.state` for the routed enqueue. Its sibling `report_upload_failed` already takes one (`agent_s3.py:108`). Add `request: Request` mirroring that sibling.

**PUSHED flip to add (mirror `agent_push.py:103-117`)** — the load-bearing rowcount-guarded idempotent state flip, inserted in `report_uploaded` AFTER the existing cloud_job `UPLOADING→UPLOADED` flip (`agent_s3.py:86-99`):
```python
res = cast(
    "CursorResult[Any]",
    await session.execute(
        update(FileRecord)
        .where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING)
        .values(state=FileState.PUSHED)
    ),
)
if res.rowcount == 0:
    await session.commit()  # already advanced: idempotent no-op, no re-enqueue
    return UploadedResponse(file_id=file_id)
```
`FileRecord` + `FileState` are already imported (`agent_s3.py:40`); `CursorResult`/`cast` already imported (`agent_s3.py:28,32`).

**Routed `submit_cloud_job` enqueue to add (mirror the routed-enqueue idiom):** `submit_cloud_job` is in `CONTROLLER_TASKS` (`enqueue_router.py:51`). Route through `enqueue_router` — NEVER a raw `controller_queue.enqueue` (KROUTE-04 / Pitfall: anti-pattern). Use the deterministic key from `submit_cloud_job_key(file_id)` (`submit_cloud_job.py:44`):
```python
routed = await enqueue_router.resolve_queue_for_task("submit_cloud_job", request.app.state, session)
await routed.queue.enqueue("submit_cloud_job", key=submit_cloud_job_key(file_id), file_id=str(file_id))
```

**Defensive `cloud_target == "k8s"` guard (RESEARCH A2, recommended):** the S3 callbacks fire only for k8s today (a1 uses rsync), so a guard is defensive-only — but wrap the PUSHED-flip + submit so a future a1-on-S3 path preserves today's cloud_job-only behavior. `get_settings()` + `cast("ControlSettings", ...)` is already the idiom here (see `agent_s3.py:128`).

---

### `src/phaze/tasks/release_awaiting_cloud.py` — k8s branch in `stage_cloud_window` (task/cron, event-driven) — D-01a, KROUTE-02

**Analog:** the existing a1 loop in the same function (`release_awaiting_cloud.py:109-176`). The fork keys on `cfg.cloud_target` and reuses ALL window math (advisory lock `:134`, `get_cloud_window_count` `:144`, `get_cloud_staging_candidates` `:150`, single post-loop commit `:173`).

**Master-gate rewrite (VERIFIED `release_awaiting_cloud.py:125`):**
```python
# was:  if not cfg.cloud_burst_enabled:  # type: ignore[attr-defined]
if cfg.cloud_target == "local":  # type: ignore[attr-defined]
    return {"staged": 0, "skipped": 0}
```

**Landmine 2 — GATE 1 skip for k8s (VERIFIED `:136-141`):** GATE 1 requires an online **compute** agent. K8s has no persistent compute agent (ephemeral Kueue pods), so on the k8s branch GATE 1 must be **skipped** or every k8s file wedges in AWAITING_CLOUD forever. GATE 2 (fileserver, `:154-160`) **stays** for both targets — the fileserver owns the media mount and runs the S3 upload.

**The per-candidate fork (VERIFIED `:164-172`):** the a1 loop flips `file.state = FileState.PUSHING` then `_enqueue_push_file(...)`. For k8s, flip PUSHING identically, then call the **no-commit S3-staging core** (see next section) instead of `_enqueue_push_file`. Keep the single `await session.commit()` after the loop (`:173`) — do NOT commit per-candidate (Landmine 1).

---

### `src/phaze/services/cloud_staging.py` — extract a no-commit core of `stage_file_to_s3` (service, file-I/O) — Landmine 1, L1

**Analog / target:** `stage_file_to_s3` (`cloud_staging.py:52-126`). Its terminal `await session.commit()` (`cloud_staging.py:119`) is the landmine: called per-candidate inside the advisory-locked window loop it would commit mid-loop → release `pg_advisory_xact_lock` → release the `FOR UPDATE SKIP LOCKED` row locks → a concurrent tick over-stages past `cloud_max_in_flight`.

**Pattern:** extract `_stage_file_to_s3(session, file, task_router)` containing the body `cloud_staging.py:69-118` (agent gate → `create_multipart_upload` → `presign_upload_parts` → `pg_insert(CloudJob).on_conflict_do_update` upsert → `queue.enqueue("s3_upload", ...)`) but **WITHOUT** the final `commit()`. The existing public `stage_file_to_s3` keeps its `commit()` and delegates to the core (it is still called committing by `redrive_upload`, `cloud_staging.py:143`). The cron's loop then owns the single post-loop commit for both branches. The upsert idiom to preserve verbatim (`cloud_staging.py:81-102`):
```python
stmt = pg_insert(CloudJob).values(id=uuid.uuid4(), file_id=file.id,
    s3_key=s3_staging.staged_object_key(file.id), status=CloudJobStatus.UPLOADING.value, upload_id=upload_id)
stmt = stmt.on_conflict_do_update(index_elements=["file_id"],
    set_={"s3_key": stmt.excluded.s3_key, "status": stmt.excluded.status, "upload_id": stmt.excluded.upload_id})
await session.execute(stmt)
```

---

### `src/phaze/config.py` — `cloud_target` field + per-target validators (config) — D-02, KROUTE-01

**Analog:** the existing `cloud_burst_enabled` field (`config.py:405-409`) and the two `_enforce_*_when_cloud_enabled` model validators (`config.py:599-636`).

**Field HARD REPLACE (VERIFIED `config.py:405-409`):** `Literal` is already imported (RESEARCH cites `config.py:14`). Replace the bool with:
```python
cloud_target: Literal["local", "a1", "k8s"] = Field(
    default="local",
    validation_alias=AliasChoices("PHAZE_CLOUD_TARGET", "cloud_target"),
    description="...",  # 'local' (default) == cloud off; 'a1' = rsync→compute; 'k8s' = S3→Kueue
)
```
No back-compat alias for `PHAZE_CLOUD_BURST_ENABLED` (D-02).

**Per-target validator split (CRITICAL — do NOT collapse to a single `!= "local"` gate; RESEARCH Pitfall 3):** the two existing validators are per-target, not "cloud on":
- `_enforce_s3_config_when_cloud_enabled` (`config.py:599-615`) is the **k8s** concern (S3 is the k8s byte path). Re-key `if self.cloud_burst_enabled:` → `if self.cloud_target == "k8s":`. Keep the `s3_bucket` + `s3_endpoint_url` requirement.
- `_enforce_compute_scratch_dir_when_cloud_enabled` (`config.py:617-636`) is the **a1** concern (rsync scratch path). Re-key `if self.cloud_burst_enabled and not self.compute_scratch_dir:` → `if self.cloud_target == "a1" and not self.compute_scratch_dir:`.

**NEW validator `_enforce_kube_config_when_k8s` (pulls KDEPLOY-02 forward):** mirror the two `@model_validator(mode="after")` above exactly. The kube fields exist optional today (`config.py:533-547`):
```python
@model_validator(mode="after")
def _enforce_kube_config_when_k8s(self) -> "ControlSettings":
    if self.cloud_target == "k8s":
        if not self.kube_api_url:
            raise ValueError("PHAZE_KUBE_API_URL is required when PHAZE_CLOUD_TARGET is 'k8s' ...")
        if not self.kube_namespace:
            raise ValueError("PHAZE_KUBE_NAMESPACE is required when PHAZE_CLOUD_TARGET is 'k8s' ...")
        if not self.kube_local_queue:
            raise ValueError("PHAZE_KUBE_LOCAL_QUEUE is required when PHAZE_CLOUD_TARGET is 'k8s' ...")
    return self
```

**Downstream call-site re-keys (VERIFIED grep):** `release_awaiting_cloud.py:125` (above); `routers/pipeline.py:373/618/710` (`settings.cloud_burst_enabled` arg → `settings.cloud_target != "local"`); `routers/pipeline.py:682` backfill gate (`if not settings.cloud_burst_enabled:` → `if settings.cloud_target == "local":`). Verify with `uv run mypy .` — `Literal` comparisons are statically exhaustive.

---

### `alembic/versions/027_add_cloud_job_cloud_phase.py` — new migration (migration, DDL) — D-04

**Analog:** `alembic/versions/026_add_cloud_job_kube_columns.py` (VERIFIED, additive/reversible, `cloud_job`-only, never touches `saq_jobs`). Mirror its CHECK-constraint naming idiom exactly.

```python
revision: str = "027"
down_revision: str | Sequence[str] | None = "026"

_CLOUD_PHASE_ENUM = "cloud_phase IN ('queued_behind_quota', 'admitted', 'running', 'finished')"

def upgrade() -> None:
    op.add_column("cloud_job", sa.Column("cloud_phase", sa.String(20), nullable=True))  # nullable: a1/local stay NULL, in-flight rows backfill lazily
    op.create_check_constraint("cloud_phase_enum", "cloud_job", _CLOUD_PHASE_ENUM)

def downgrade() -> None:
    op.drop_constraint("cloud_phase_enum", "cloud_job", type_="check")
    op.drop_column("cloud_job", "cloud_phase")
```
**Naming gotcha (from `026:54-55`):** pass the BARE name `"cloud_phase_enum"` to `create_check_constraint` — the `ck_%(table_name)s_%(constraint_name)s` convention re-applies the `ck_cloud_job_` prefix (passing an already-prefixed name double-prefixes it). Use a name DISTINCT from `status_enum`. Migration test mirrors `tests/test_migrations/test_migration_025_cloud_job.py` for the upgrade/downgrade round-trip.

---

### `src/phaze/models/cloud_job.py` — `CloudPhase` enum + `cloud_phase` column (model) — D-04

**Analog:** `CloudJobStatus` StrEnum (`cloud_job.py:30-46`) + the `status` column (`:60`) + the `status_enum` `CheckConstraint` (`:74-78`). The module docstring (`:11-12`) already reserves `cloud_phase` for this phase's own migration.

**Add (string-backed StrEnum, the documented precedent):**
```python
class CloudPhase(enum.StrEnum):
    QUEUED_BEHIND_QUOTA = "queued_behind_quota"
    ADMITTED = "admitted"
    RUNNING = "running"
    FINISHED = "finished"
```
```python
# in CloudJob, alongside the other nullable kube columns:
cloud_phase: Mapped[str | None] = mapped_column(String(20), nullable=True)
```
Add a parallel `CheckConstraint("cloud_phase IN (...)", name="cloud_phase_enum")` to `__table_args__` (mirror `:74-78`). NULL for a1/local files (Kueue admission is k8s-only — RESEARCH Open Q3).

---

### `src/phaze/tasks/submit_cloud_job.py` — seed `cloud_phase` (task, CRUD) — D-04

**Analog:** the existing `pg_insert(CloudJob)...on_conflict_do_update` upsert in the same function (`submit_cloud_job.py:79-96`).

Add `cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value` to BOTH the `.values(...)` (`:79-87`) and the `set_={...}` (`:92-95`) — a re-submit resets the admission progression:
```python
stmt = pg_insert(CloudJob).values(id=uuid.uuid4(), file_id=fid,
    s3_key=s3_staging.staged_object_key(fid), status=CloudJobStatus.SUBMITTED.value,
    kueue_workload=name, cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value)
stmt = stmt.on_conflict_do_update(index_elements=["file_id"],
    set_={"status": stmt.excluded.status, "kueue_workload": stmt.excluded.kueue_workload,
          "cloud_phase": stmt.excluded.cloud_phase})
```

---

### `src/phaze/tasks/reconcile_cloud_jobs.py` — co-write `cloud_phase` (task/cron, event-driven) — D-04

**Analog:** `_reconcile_one` (`reconcile_cloud_jobs.py:188-263`) + `_record_success` (`:124-136`). It already maps Job/Workload conditions to `status` writes — co-write `cloud_phase` alongside (keep it ORTHOGONAL to the existing `inadmissible` fault flag — RESEARCH: `cloud_phase` is admission *progression*, not the fault flag).

Co-write sites (VERIFIED):
- Healthy `Pending` branch (`:246-251`) → set `cloud_phase = CloudPhase.QUEUED_BEHIND_QUOTA.value`.
- Admitted / `QuotaReserved=True` branch (`:255-261`, already advances `status` SUBMITTED→RUNNING) → co-write `cloud_phase = CloudPhase.ADMITTED.value` then `RUNNING.value`.
- `_record_success` (`:132`, already sets `status=SUCCEEDED`) → co-write `cloud_phase = CloudPhase.FINISHED.value` before the commit at `:134`.

Each branch already does its own `await session.commit()` after the mutation — add the `cloud_phase` assignment before that commit.

---

### `src/phaze/services/pipeline.py` — `get_cloud_phase_counts` + ledger-scoped backfill query (service, CRUD-read) — D-03/D-04, KROUTE-06/05

**Analog (cards):** `get_inadmissible_count` (`pipeline.py:820-840`) — the degrade-safe `_safe_count` (`pipeline.py:273`) pattern that returns 0 on any DB error so the hot 5s poll never 500s. Add four per-phase counts (or one `get_cloud_phase_counts` returning a dict):
```python
return await _safe_count(
    session,
    select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == CloudPhase.RUNNING.value),
    node="cloud_phase_running",
)
```
Repeat per `CloudPhase` member with distinct `node=` tags. `CloudJob` already imported here.

**Analog (Landmine 4 — ledger-scoped backfill):** `_backfill_candidates_stmt` (`pipeline.py:917-928`) filters only `ANALYSIS_FAILED ∧ duration >= threshold` — it does NOT require a ledger row. D-03 needs "previously-scheduled work only". Add an `EXISTS` predicate against `scheduling_ledger` keyed `'process_file:' || file.id` (mirroring the v5.0 recover-over-enqueue fix):
```python
.where(
    FileRecord.state == FileState.ANALYSIS_FAILED,
    FileMetadata.duration >= threshold_sec,
    exists().where(SchedulingLedger.key == "process_file:" + cast(FileRecord.id, String)),  # ledger-scoped
)
```
Use bound params / ORM only (no f-string SQL — T-49-02). The `threshold_sec` stays a bound int (`:921-922`).

---

### `src/phaze/routers/pipeline.py` — card wiring + backfill k8s fork (route, request-response) — KROUTE-06/05, D-03

**Card wiring analog (VERIFIED):** `inadmissible_count` is seeded in BOTH contexts — `dashboard()` (`pipeline.py:498`, into the context dict at `:519`) and `pipeline_stats_partial()` (`pipeline.py:564`, into the context at `:582`). Seed the four `cloud_phase` counts identically in both (the OOB swap on the 5s poll keeps the cards live). No router try/except — the service owns the degrade (`get_inadmissible_count` idiom at `:496-498`).

**Backfill k8s fork analog:** `trigger_backfill_cloud` (`pipeline.py:657-729`). The existing a1 path: gate (`:682`), `count_backfill_candidates` (`:690`), reset to `DISCOVERED` + commit BEFORE enqueue (`:699-703`), route via `_route_discovered_by_duration` (`:705`), then **seed a `process_file:<id>` ledger row for HELD files** (`:718-727`, `insert_ledger_if_absent(... key=process_file_job_key(file.id) ...)`).

**Landmine 3 — the k8s fork must NOT seed that ledger row:** for k8s, a `process_file:<id>` ledger row lets `recover_orphaned_work` replay the file onto a LOCAL agent queue (CLOUDROUTE-02 hazard). The k8s branch resets to `DISCOVERED` → router holds in `AWAITING_CLOUD` → `stage_cloud_window` k8s branch picks it up — **with NO ledger seed** (skip `:714-727` for k8s). Use the new ledger-scoped candidate query. RESEARCH Open Q2: branch the existing `/pipeline/backfill-cloud` endpoint on `cloud_target` (one surface) vs a distinct endpoint is the planner's call — either satisfies D-03.

---

### `templates/pipeline/partials/admission_state_card.html` — new card (component) — KROUTE-06, 55-UI-SPEC

**Analog (structure + OOB gating):** `partials/inadmissible_card.html` (VERIFIED, read in full). **Analog (tile markup):** `staged_pushing_card.html:21-25` (per 55-UI-SPEC). The carrier/body split to copy from `inadmissible_card.html:19-33`:
```jinja
<section id="admission-state-card"
         aria-labelledby="admission-state-heading"
         {% if oob %}hx-swap-oob="true"{% endif %}
         class="border border-gray-200 dark:border-phaze-border rounded-lg p-4 space-y-3">
  {% if queued_behind_quota_count or admitted_count or running_count or finished_count %}
    ...heading + grid of per-phase tiles...
  {% endif %}
</section>
```
Per 55-UI-SPEC: empty `<section>` carrier ALWAYS emitted (stable OOB target); body gated on `{% if any count %}`; per-tile gated on its own `{% if <phase>_count %}`; static autoescaped strings only; NO `role="alert"` (these are healthy progression, not a fault — the amber `role="alert"` stays EXCLUSIVE to inadmissible_card). Hues per 55-UI-SPEC Color table (gray/blue/violet/green; amber RESERVED).

---

### `templates/pipeline/dashboard.html` + `stats_bar.html` — mount + OOB re-push (component)

**Dashboard analog (VERIFIED `dashboard.html:35`):** `{% include "pipeline/partials/inadmissible_card.html" %}` sits OUTSIDE `#pipeline-stats` (`:39`). Add the admission card include immediately after it (same OUTSIDE-the-poll-target placement).

**stats_bar analog (VERIFIED `stats_bar.html:90`):** `{% with oob = True %}{% include "pipeline/partials/inadmissible_card.html" %}{% endwith %}` inside the `{% if oob_counts %}` block (`:46`). Add the admission card OOB include alongside it (mirrors siblings at `:73/78/83/84/90`).

---

### `tests/test_no_default_queue_producers.py` — AST guard extension (test, static) — KROUTE-04

**Analog:** the existing `_ProducerVisitor` AST scanner (VERIFIED, read in full). It already walks every `.py` under `src/phaze/routers` + `src/phaze/services` (`:47-50`) and flags `*.state.queue` / unnamed `Queue.from_url`. The new k8s enqueue site (`submit_cloud_job` in `report_uploaded`) lives in `routers/` — already in scan scope.
- **Positive routing (already covered):** `test_every_controller_task_routes_to_controller_queue` (`:172-180`) already asserts `submit_cloud_job` ∈ `CONTROLLER_TASKS` routes to the controller queue. Confirm `report_uploaded` uses `resolve_queue_for_task("submit_cloud_job", ...)`, not a raw enqueue (the static scan catches `*.state.queue`).
- **Extension (no-whole-backlog property):** add an AST/membership assertion that the k8s backfill candidate query is the bounded ledger-scoped filter, not a bare `state == ANALYSIS_FAILED` sweep — mirror the meta-test idiom at `:127-164`.

---

## Shared Patterns

### Idempotent rowcount-guarded state flip
**Source:** `src/phaze/routers/agent_push.py:103-117` (and the cloud_job flip at `agent_s3.py:86-97`).
**Apply to:** the `report_uploaded` PUSHING→PUSHED flip; any new guarded `UPDATE ... WHERE state=X`.
```python
res = cast("CursorResult[Any]", await session.execute(
    update(FileRecord).where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING).values(state=FileState.PUSHED)))
if res.rowcount == 0:
    await session.commit()  # idempotent no-op
    return ...
```

### Routed controller enqueue (Phase 30 single seam)
**Source:** `enqueue_router.resolve_queue_for_task` (`enqueue_router.py`); usage idiom `cloud_staging.py:111-118`, re-enqueue `reconcile_cloud_jobs.py:119-121`.
**Apply to:** EVERY new k8s enqueue (the `submit_cloud_job` enqueue in `report_uploaded`). Never `request.app.state.queue` (default queue) and never a raw `controller_queue.enqueue`. AST-guarded by `test_no_default_queue_producers.py`.

### Degrade-safe card count (`_safe_count`)
**Source:** `src/phaze/services/pipeline.py:273` (`_safe_count`); model read `get_inadmissible_count` (`:820-840`).
**Apply to:** all `get_cloud_phase_counts` reads. Returns 0 on any DB error so the hot 5s `/pipeline/stats` poll never 500s. NB: the load-bearing `get_cloud_window_count` (`:884`) is deliberately NOT degrade-safe — do not copy `_safe_count` onto window/backpressure counts.

### Additive, reversible, `cloud_job`-scoped migration
**Source:** `alembic/versions/026_add_cloud_job_kube_columns.py`.
**Apply to:** migration 027. NEVER reference `saq_jobs` (SAQ owns it — 020 CRITICAL banner). Bare CHECK-constraint name (the naming convention re-prefixes). Round-trip test mirrors `test_migration_025_cloud_job.py`.

### Single post-loop commit under advisory lock (no nested commit)
**Source:** `stage_cloud_window` (`release_awaiting_cloud.py:134` lock → `:173` single commit).
**Apply to:** the k8s stage branch — call the no-commit `_stage_file_to_s3` core per candidate, commit ONCE after the loop. A nested commit (the current public `stage_file_to_s3` at `cloud_staging.py:119`) releases the `pg_advisory_xact_lock` mid-loop and re-opens the over-stage class (Landmine 1).

### OOB-swap dashboard card (carrier-always / body-conditional)
**Source:** `partials/inadmissible_card.html:19-33`; mount `dashboard.html:35`; OOB re-push `stats_bar.html:90`; dual-context seeding `routers/pipeline.py:498,564`.
**Apply to:** the admission_state_card. Same `id` on inline + OOB render; carrier always emitted; counts seeded in both `dashboard()` and `pipeline_stats_partial()`.

---

## No Analog Found

None. Every file in this phase has a verified in-repo analog — this is a composition phase (RESEARCH "Key insight: Phases 52–54 deliberately built every k8s leg standalone and unwired; this phase is almost entirely composition").

---

## CONTEXT vs codebase discrepancies (flag for planner)

| CONTEXT/research reference | Verified reality | Action |
|----------------------------|------------------|--------|
| `.env.example.agent` (CONTEXT D-02) | Does NOT exist; only `.env.example` (no cloud vars today). `cloud_target` is ControlSettings (control plane), not agent. | Do not create a phantom file; ADD `PHAZE_CLOUD_TARGET` to `.env.example` + control-plane compose env (RESEARCH A1). |
| "migrate docker-compose*.yml" rename | No `cloud_burst`/`cloud_target` present today → it's an ADD, not a rename. | Add to control-plane service env only. |
| `templates/.../backfill_response.html:15` copy | Says "cloud_burst_enabled=false" | Update copy to `cloud_target=local` (string edit). |

---

## Metadata

**Analog search scope:** `src/phaze/{routers,services,tasks,models,config.py}`, `alembic/versions/`, `templates/pipeline/`, `tests/`
**Files scanned (read for excerpts):** `routers/agent_push.py`, `routers/agent_s3.py`, `routers/pipeline.py`, `tasks/release_awaiting_cloud.py`, `tasks/submit_cloud_job.py`, `tasks/reconcile_cloud_jobs.py`, `services/cloud_staging.py`, `services/pipeline.py`, `models/cloud_job.py`, `config.py`, `alembic/versions/026_*.py`, `templates/pipeline/partials/inadmissible_card.html`, `templates/pipeline/dashboard.html`, `templates/pipeline/partials/stats_bar.html`, `tests/test_no_default_queue_producers.py`
**Pattern extraction date:** 2026-06-28
