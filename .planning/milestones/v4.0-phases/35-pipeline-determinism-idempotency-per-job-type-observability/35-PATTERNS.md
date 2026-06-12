# Phase 35: Pipeline Determinism, Idempotency & Per-Job-Type Observability - Pattern Map

**Mapped:** 2026-06-11
**Files analyzed:** 14 (8 modified, 4 created, 2 deletions-within-file)
**Analogs found:** 13 / 14 (1 net-new shape — the SVG DAG canvas — has a partial structural analog only)

This map is surgical: the deep technical questions are already resolved in `35-RESEARCH.md` (SAQ hook order, `after_process` existence, proposals migration hazard, the Q5 stats-source mismatch). This document pins **which existing file each new/modified file copies its shape from**, with file:line + excerpts the planner drops straight into plan actions.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/tasks/_shared/queue_defaults.py` (extend OR new sibling `deterministic_key.py`) | middleware (SAQ before_enqueue hook) | event-driven | `tasks/_shared/queue_defaults.py` `apply_project_job_defaults` | **exact** |
| `src/phaze/tasks/_shared/*` completion counter hook (new `after_process` callback) | middleware (SAQ after_process hook) | event-driven | `services/proposal.py:221` `check_rate_limit` (Redis INCR/EXPIRE) + `queue_defaults.py` hook shape | role-match |
| `src/phaze/services/*` maintained per-function counters service (new) | service | event-driven / CRUD (Redis) | `services/proposal.py:221` `check_rate_limit` | role-match |
| `src/phaze/services/pipeline.py` `get_stage_progress` (new fn) | service | CRUD (read) | `services/pipeline.py:34` `get_pipeline_stats` / `:47` `get_queue_activity` | **exact** |
| `src/phaze/services/proposal.py` `store_proposals` (convert to upsert) | service | CRUD (write) | `routers/agent_metadata.py:56-69` `on_conflict_do_update` | **exact** |
| `alembic/versions/019_*.py` partial unique index + pre-dedupe | migration | batch / transform | `alembic/versions/018_add_analysis_window_table.py` (partial index via `postgresql_where`) | role-match |
| `src/phaze/models/proposal.py` `__table_args__` partial Index | model | — | `models/proposal.py:53` existing `Index(...)` | **exact** |
| `src/phaze/tasks/controller.py` `settings` (wire `after_process` + register key hook) | config (SAQ worker settings) | event-driven | `tasks/controller.py:133-166` existing settings dict | **exact** |
| `src/phaze/tasks/agent_worker.py` `settings` (same wiring) | config (SAQ worker settings) | event-driven | `tasks/agent_worker.py:179-208` existing settings dict | **exact** |
| `src/phaze/main.py:103` + `agent_task_router.py:99` (register key hook alongside defaults) | config / wiring | event-driven | `main.py:103`, `agent_task_router.py:99` existing `register_before_enqueue` | **exact** |
| `src/phaze/routers/agent_files.py` (REMOVE auto-enqueue `:143`) | controller | request-response | n/a — deletion (D-06) | deletion |
| `src/phaze/services/ingestion.py` (REMOVE auto-enqueue `:190`) | service | batch | n/a — deletion (D-06) | deletion |
| `src/phaze/templates/pipeline/partials/dag_canvas.html` (new) + `dashboard.html` shell | component (Jinja partial) | request-response / store-driven | `templates/pipeline/partials/stage_cards.html` + `stats_bar.html` (OOB-swap seed) | role-match (structural) |
| `src/phaze/routers/pipeline.py` `dashboard()` + `pipeline_stats_partial()` context | controller | request-response | existing `_enqueue_proposal_jobs:67` + stats context | **exact** |
| `tests/_queue_fakes.py` extend `DedupFakeQueue` (counter-hook capture / key assertions) | test | event-driven | `tests/_queue_fakes.py:176` `DedupFakeQueue` | **exact** |

---

## Pattern Assignments

### 1. Central deterministic-key hook — `tasks/_shared/queue_defaults.py` (extend) or new `deterministic_key.py` (middleware, event-driven)

**Analog:** `src/phaze/tasks/_shared/queue_defaults.py` `apply_project_job_defaults` (lines 62-88) — this IS the proven `before_enqueue` mutation seam (RESEARCH Q1 VERIFIED a hook can set `job.key`).

**Hook body pattern to copy** (`queue_defaults.py:62-85`):
```python
async def apply_project_job_defaults(job: Job) -> None:
    cfg = _config.get_settings()
    if job.timeout == _SAQ_DEFAULT_TIMEOUT:
        job.timeout = cfg.worker_job_timeout
    # ... mutates job attributes in place; SAQ awaits before persisting to Redis
```

**New hook shape** (registry-driven, from RESEARCH Q1 recommendation — set the key UNCONDITIONALLY for registered functions, unlike the guarded defaults hook):
```python
_KEY_BUILDERS = {
    "process_file":               lambda k: k["file_id"],
    "extract_file_metadata":      lambda k: k["file_id"],
    "fingerprint_file":           lambda k: k["file_id"],
    "scan_live_set":              lambda k: k["file_id"],
    "search_tracklist":           lambda k: k["file_id"],
    "scrape_and_store_tracklist": lambda k: k["tracklist_id"],
    "match_tracklist_to_discogs": lambda k: k["tracklist_id"],
    "generate_proposals":         lambda k: _hash_ids(k["file_ids"]),  # batch-hash, Q1/Q3
}
async def apply_deterministic_key(job: Job) -> None:
    build = _KEY_BUILDERS.get(job.function)
    if build:
        job.key = f"{job.function}:{build(job.kwargs or {})}"
```

**Natural-id template already in-repo** — `services/analysis_enqueue.py:32-40`:
```python
def process_file_job_key(file_id: uuid.UUID) -> str:
    return f"process_file:{file_id}"
```
The registry's `process_file` entry computes the IDENTICAL string, so the existing keyed path stays a no-op-equivalent (RESEARCH Q1) — keep `analysis_enqueue.py` as the documented per-task helper template.

**Key reuse note:** `job.function` and `job.kwargs` are already populated by hook time (RESEARCH Q1, `saq/queue/base.py:332-355`). No payload threading needed.

---

### 2. Completion counter hook + maintained counters service (middleware + service, event-driven)

**Analog (Redis INCR/EXPIRE precedent):** `src/phaze/services/proposal.py:221-241` `check_rate_limit`:
```python
key = "phaze:llm:rpm"
count: int = await redis_pool.incr(key)
if count == 1:
    await redis_pool.expire(key, 60)
```
Mirror this exactly for the maintained `enqueued[function]` / `completed[function]` counters (namespaced keys, e.g. `phaze:pipeline:enqueued:<function>`).

**Enqueued-side increment:** fold into the SAME `before_enqueue` hook from §1 (one hook does key + `INCR enqueued[job.function]`).

**Completed-side hook — use SAQ `after_process` (RESEARCH Q2: CONTEXT was WRONG; it DOES exist in 0.26.4).** It is a **Worker constructor kwarg**, NOT a `register_*` method, so it is added to the `settings` dict (see §8). The callback receives the job context:
```python
async def increment_completed(ctx: dict) -> None:
    job = ctx["job"]
    if job.status == Status.COMPLETE:
        await redis.incr(f"phaze:pipeline:completed:{job.function}")
```
Best-effort by design — the DB reconcile (§4) is the backstop (D-03 mandatory).

---

### 3. `get_stage_progress` — new per-stage output-table count query (service, CRUD read)

**🔴 RESEARCH Q5 is the biggest planning impact:** `get_pipeline_stats` (`pipeline.py:34-44`) groups by the LINEAR `FileRecord.state` enum and CANNOT report parallel-stage "done" counts. Do **not** reuse it for Metadata/Fingerprint/Analyze.

**Analog (query shape + the all-stages-present-default idiom):** `services/pipeline.py:34-44`:
```python
stmt = select(FileRecord.state, func.count(FileRecord.id)).group_by(FileRecord.state)
result = await session.execute(stmt)
counts: dict[str, int] = {row[0]: row[1] for row in result.all()}
return {stage.value: counts.get(stage.value, 0) for stage in PIPELINE_STAGES}
```

**New query** = `COUNT(DISTINCT file_id)` (or `tracklist_id`) per stage OUTPUT table. The per-node DB-truth source map is in `35-UI-SPEC.md` "Counter / Denominator Display Rule" and RESEARCH Q5 table:
| Node | done source | denominator |
|---|---|---|
| Extract Metadata | distinct `file_id` in `metadata` | music/video file count |
| Fingerprint | distinct `file_id` in `fingerprint_results` (status='completed') | music/video file count |
| Analyze | distinct `file_id` in `analysis` | music/video file count |
| Scan/Search | distinct `file_id` in `tracklists` | **NONE — counter-only, render `done / —`** |
| Scrape | distinct `tracklist_id` in `tracklist_versions` | `COUNT(tracklists)` |
| Match | distinct `tracklist_id` in `discogs_links` | `COUNT(tracklists)` |
| Proposals | distinct `file_id` in `proposals` | convergence set (`routers/pipeline.py:116-128`) |
| Execute | distinct `file_id` in `execution_log` (completed) | approved-proposal count |

**Reuse for in-flight states:** `get_queue_activity` (`pipeline.py:47-104`) already returns `agent_busy`/`controller_busy` — reuse for the "Agent busy" / "Controller busy" gated-trigger reasons (no new query). Copy its per-source `try`/degrade-to-0 failure isolation if the new counts can touch Redis.

---

### 4. `store_proposals` raw-INSERT → upsert (service, CRUD write)

**Current non-idempotent code** (`services/proposal.py:289-298`) — the lone outlier; `session.add(RenameProposal(...))` per proposal, fresh uuid each run:
```python
record = RenameProposal(file_id=uuid.UUID(fid), proposed_filename=..., status=ProposalStatus.PENDING, ...)
session.add(record)
```

**Analog upsert to mirror** — `routers/agent_metadata.py:56-69` (`agent_fingerprint.py:40` is the second example):
```python
stmt = pg_insert(FileMetadata).values([payload])
stmt = stmt.on_conflict_do_update(
    index_elements=["file_id"],
    set_={k: stmt.excluded[k] for k in dumped},
)
await session.execute(stmt)
```

**D-04 exact shape** (from RESEARCH Q3 — partial-index conflict target + DB-level status guard):
```python
stmt = pg_insert(RenameProposal).values(**row).on_conflict_do_update(
    index_elements=["file_id"],
    index_where=(RenameProposal.status == "pending"),
    set_={"proposed_filename": ..., "proposed_path": ..., "confidence": ...,
          "context_used": ..., "reason": ..., "updated_at": func.now()},
)
```
**PK-stamp gotcha (carry from the metadata analog, `agent_metadata.py:39-55`):** `RenameProposal.id` has only a Python-side `default=uuid.uuid4` (`models/proposal.py:42`), which `pg_insert(...).values()` BYPASSES. Stamp `row["id"] = uuid.uuid4()` explicitly or the fresh INSERT raises `NotNullViolationError`. `ON CONFLICT DO UPDATE` keeps the existing row's id (don't put `id` in `set_`).

---

### 5. Alembic migration 019 — partial unique index + pre-dedupe (migration, batch/transform)

**🔴 BLOCKING DATA HAZARD (RESEARCH Q3):** the live 11,428-file archive almost certainly has multiple PENDING proposals per file. `CREATE UNIQUE INDEX` will ABORT unless the migration collapses duplicates FIRST. One revision, two ordered ops.

**Analog:** `alembic/versions/018_add_analysis_window_table.py` — latest revision is `018`, so new file is `019_*` with `down_revision = "018"`. The partial-index idiom to copy (`018:71`):
```python
op.create_index("ix_analysis_window_bpm_fine", "analysis_window", ["bpm"], postgresql_where=sa.text("tier = 'fine'"))
```

**New migration shape:**
```python
revision = "019"
down_revision = "018"

def upgrade() -> None:
    # Op 1: collapse existing duplicate PENDING rows to one-per-file (keep most-recent
    # created_at), per RESEARCH Q3 — else op 2 aborts.
    op.execute(sa.text("""
        DELETE FROM proposals p USING (
            SELECT id, row_number() OVER (PARTITION BY file_id ORDER BY created_at DESC) AS rn
            FROM proposals WHERE status = 'pending'
        ) d WHERE p.id = d.id AND d.rn > 1
    """))
    # Op 2: partial unique index = the on_conflict_do_update target for §4.
    op.create_index("uq_proposals_file_id_pending", "proposals", ["file_id"],
                    unique=True, postgresql_where=sa.text("status = 'pending'"))
```
Mirror the `downgrade()` drop-index pattern from `018:78-85`.

**Model sync (`models/proposal.py:53`):** add the partial Index to `__table_args__` so autogenerate/ORM stays in sync (RESEARCH Q3) — existing analog in the same line:
```python
__table_args__ = (
    Index("ix_proposals_status", "status"),
    Index("uq_proposals_file_id_pending", "file_id", unique=True, postgresql_where=text("status = 'pending'")),
)
```

---

### 6. SAQ worker settings wiring — `tasks/controller.py` + `tasks/agent_worker.py` (config, event-driven)

**Analog (both files, identical pattern):** `controller.py:133-166` and `agent_worker.py:179-208`.

The `before_enqueue` registration to MIRROR for the new key hook (`controller.py:139`, `agent_worker.py:185`, `agent_task_router.py:99`, `main.py:103` — all four register the same defaults hook today):
```python
queue.register_before_enqueue(apply_project_job_defaults)
# add alongside:
queue.register_before_enqueue(apply_deterministic_key)
```

The `settings` dict where the new `after_process` completion hook is wired (`controller.py:142-166` / `agent_worker.py:188-208`) — it is a Worker kwarg, so it goes into the dict, NOT a register call:
```python
settings = {
    "queue": queue,
    "functions": [...],
    "concurrency": get_settings().worker_max_jobs,
    "startup": startup,
    "shutdown": shutdown,
    "after_process": increment_completed,   # NEW (Worker kwarg per RESEARCH Q2)
}
```
Wire it in **both** worker entrypoints (agent worker runs in a separate container but shares central Redis — RESEARCH Q2).

---

### 7. Remove auto-enqueue paths (D-06, deletions)

**`routers/agent_files.py:130-162`** — delete the entire "Auto-enqueue extract_file_metadata for INSERTed music/video files" block (the `for row in rows: ... task_router.enqueue_for_agent("extract_file_metadata", ...)` loop). Keep the upsert (`:126`) and the `return FileUpsertResponse(...)`.

**`services/ingestion.py:183-191`** — delete the "Auto-enqueue tag extraction for newly discovered files" block:
```python
if queue is not None and file_records:
    for record in file_records:
        ...
        await queue.enqueue("extract_file_metadata", file_id=str(record["id"]))
```
After removal, `search_tracklist` falls back to filename parsing when metadata is absent (CONTEXT D-06 soft-dep note, `35-STAGE-DEPENDENCIES.md:16`) — no hard break.

---

### 8. DAG canvas Jinja partial (component, store-driven) — replaces `stage_cards.html` + `processing_card.html`

**Analogs (two load-bearing patterns):**

**(a) Per-button trigger + Alpine loading + `:disabled` store gate** — `stage_cards.html:23-33`:
```html
<div class="border rounded-lg p-4 ..." x-data="{ loading: false }">
  <button hx-post="/pipeline/analyze" hx-target="#analyze-response" hx-swap="innerHTML"
          @click="loading = true"
          :disabled="loading || $store.pipeline.discovered === 0 || $store.pipeline.agentBusy > 0"
          class="... disabled:opacity-50 disabled:cursor-not-allowed">Run Analysis</button>
</div>
```
Each DAG node trigger reuses this `x-data="{ loading: false }"` + `@click="loading = true"` + `hx-post` + `*-response` slot pattern. Gating predicates per node are LOCKED in `35-UI-SPEC.md` "Trigger Gating Contract". **Topology correction (UI-SPEC line 243):** Fingerprint must gate on `$store.pipeline.discovered` (NOT `metadataExtracted` as `stage_cards.html:73` does today).

**(b) Static-vs-OOB split + same-id store seed** — `stats_bar.html:39-59` (THE load-bearing lesson):
- Initial full-page render: in-place `<p x-init="$store.pipeline.X = {{ ... }}">` seeds (`stage_cards.html:12-13, 20`).
- 5s `/pipeline/stats` poll: the SAME ids emitted with `hx-swap-oob="true"` (`stats_bar.html:46-59`), gated behind `{% if oob_counts %}` so they only fire on the poll, never the full-page include (avoids duplicate-id DOM).
- The SVG `<svg>` edge layer + node frames + `<button>`/`<a>` elements render ONCE (never swapped — a blunt swap clobbers an in-flight click). Only the store-bound counts/bars/pills/`:disabled` update.

**Store extension** — `base.html:97`:
```js
Alpine.store('pipeline', { discovered: 0, analyzed: 0, metadataExtracted: 0, agentBusy: 0, controllerBusy: 0 });
```
Add per-node sub-keys (`metadataDone/metadataTotal`, `fingerprintDone/...`, `analyzeDone/analyzeTotal/analyzeActive`, `tracklistDone`, `scrapeDone/...`, `matchDone/...`, `proposalsDone/...`, `approved`, `executedDone/...`) per `35-UI-SPEC.md` "Store extension". Every key MUST be seeded on the full-page render (no `undefined` pre-first-poll).

**Router context** — `routers/pipeline.py` `dashboard()` (full-page seed) and `pipeline_stats_partial()` (`/pipeline/stats` poll) both need the `get_stage_progress` (§3) + counters (§2) data added to their template context. Existing background-enqueue analog at `routers/pipeline.py:67-70`.

---

### 9. Test fakes — extend `DedupFakeQueue` (test, event-driven)

**Analog:** `tests/_queue_fakes.py:176-220` `DedupFakeQueue` already models SAQ's deterministic-key dedup no-op (returns `None` on a live key, `finish(key)` clears it). Extend per RESEARCH Q1/§6 drift-guard: a test over `CONTROLLER_TASKS ∪ AGENT_TASKS` asserting EVERY routable task name produces a deterministic (non-uuid) key after the hook runs, so a future task added without a `_KEY_BUILDERS` entry fails loud. The `key` kwarg already routes into `_JOB_CONTROL_FIELDS` (`:53, :202-204`), so capture is already wired; add a counter-hook capture if asserting INCR side effects.

---

## Shared Patterns

### Idempotent upsert (house style)
**Source:** `routers/agent_metadata.py:56-69`, `routers/agent_fingerprint.py:40`, `routers/agent_execution.py:77` (`on_conflict_do_nothing(index_elements=["id"])`).
**Apply to:** `store_proposals` (§4). Everything else is already idempotent (RESEARCH Q4 confirms `execution_log` ✅, `tag_write_log` is intentionally append-only — DO NOT add an upsert there, it would erase the audit trail).
```python
stmt = pg_insert(Model).values([payload]).on_conflict_do_update(index_elements=[...], set_={...})
```

### SAQ before_enqueue hook mutation
**Source:** `tasks/_shared/queue_defaults.py:62-85`; registered at `main.py:103`, `agent_task_router.py:99`, `controller.py:139`, `agent_worker.py:185`.
**Apply to:** the new central deterministic-key hook (§1) + enqueued counter (§2) — register on the SAME four seams.

### Redis INCR/EXPIRE maintained counter
**Source:** `services/proposal.py:232-241` `check_rate_limit`.
**Apply to:** the maintained per-function enqueued/completed counters (§2).

### Live count via `$store.pipeline` + 5s OOB-swap (no new poll, no SSE)
**Source:** `stats_bar.html:39-59` (OOB seeds) + `stage_cards.html:12-33` (in-place seeds + `:disabled` gates) + `base.html:97` (store).
**Apply to:** every DAG node count/bar/pill/trigger (§8).

### Partial index via `postgresql_where`
**Source:** `alembic/versions/018_add_analysis_window_table.py:71`.
**Apply to:** the proposals partial unique index migration (§5).

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `templates/pipeline/partials/dag_canvas.html` (SVG node-edge layer) | component | store-driven | No existing SVG/node-edge canvas in the codebase. The TRIGGER/store/OOB mechanics have a strong analog (`stage_cards.html` + `stats_bar.html`), but the SVG `<svg>` edge layer, anchor-derived cubic-bézier `d` strings, and `NODE_LAYOUT` constant map are net-new — author from `35-UI-SPEC.md` "Edge construction contract". Planner should treat the SVG geometry as greenfield and the live-data binding as copy-from-analog. |

---

## Metadata

**Analog search scope:** `src/phaze/tasks/_shared/`, `src/phaze/services/`, `src/phaze/routers/`, `src/phaze/templates/pipeline/partials/`, `src/phaze/models/`, `alembic/versions/`, `tests/`.
**Files scanned:** ~16 read in full or targeted ranges.
**Pattern extraction date:** 2026-06-11
**Path corrections honored (from RESEARCH):** tracklist enqueue sites are in `routers/tracklists.py` (not `services/tracklists.py`); SAQ installed minor is `0.26.4` and DOES expose `after_process`.
