# Phase 44: Analyze Observability UI — Pattern Map

**Mapped:** 2026-06-18
**Files analyzed:** 8 (6 modified, 2 new)
**Analogs found:** 8 / 8 (every new/modified file has a concrete in-repo analog)

This phase EXTENDS an existing pipeline dashboard and surfaces backend state shipped in
Phase 43. No new analysis math. Every assignment below copies a real, current pattern that
already lives in the same module the new code lands in.

> **Critical upstream finding (read before planning the straggler query — D-01):**
> The SAQ Postgres `saq_jobs` table has **only these columns**:
> `key TEXT PK, lock_key SERIAL, job BYTEA, queue TEXT, status TEXT, priority SMALLINT,
> group_key TEXT, scheduled BIGINT, expire_at BIGINT`
> (from `saq/queue/postgres_migrations.py:17-26`).
> There is **NO `started` / `touched` SQL column** — SAQ stores `started`/`touched`
> (epoch **milliseconds**, `saq.utils.now()`) **inside the serialized `job BYTEA` blob**
> (`saq/job.py:132-133`), NOT as queryable columns. A straggler "age" predicate therefore
> **cannot** be a pure `WHERE now() - started > threshold` SQL filter the way
> `_STAGE_BUSY_SQL` filters on `status`. The planner must pick one of:
> (a) filter on the queryable `scheduled BIGINT` column (`scheduled` is set to dequeue time
> for active jobs — verify semantics), or
> (b) deserialize the `job` blob in Python (read `started`, compute age) — matching how
> `saq/queue/postgres.py` itself deserializes via `self.deserialize(job_bytes, status)`.
> Do NOT plan a `split_part(... started ...)` style SQL age filter against a non-existent
> column. This is the single biggest risk in the phase.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/services/pipeline.py` (straggler count/list) | service | request-response (read) | `get_stage_busy_counts` + `_STAGE_BUSY_SQL` / `count_inflight_jobs` (same file) | exact (extend in place) |
| `src/phaze/services/pipeline.py` (ANALYSIS_FAILED count/list) | service | CRUD (read) | `get_files_by_state` + `get_pipeline_stats` (same file) | exact |
| `src/phaze/routers/pipeline.py` (dashboard context wiring) | router | request-response | `dashboard()` + `pipeline_stats_partial()` + `_build_dag_context()` (same file) | exact |
| `src/phaze/routers/pipeline.py` (deepen POST endpoint) | router | request-response → enqueue | `tracklists.rescrape_tracklist` (single-resource re-trigger) + `trigger_analysis_ui` | role-match |
| `src/phaze/templates/pipeline/dashboard.html` (straggler/failed card) | template | — | existing `{% include %}` card slots on `dashboard.html` | exact |
| `src/phaze/templates/.../sampled_badge.html` (NEW) | template (partial) | — | `tracklists/partials/confidence_badge.html` + `source_badge.html` | exact |
| `src/phaze/schemas/agent_tasks.py` (`ProcessFilePayload` caps) | schema | — | `ExecuteApprovedBatchPayload.sub_batch_index` optional-field default (same file) | exact |
| `src/phaze/services/analysis_enqueue.py` (`enqueue_process_file` caps) | service | event-driven (producer) | `enqueue_process_file` itself (extend signature + payload) | exact (extend in place) |
| `src/phaze/tasks/functions.py` (`process_file` cap threading) | task | transform | existing AgentSettings→`analyze_file` cap threading at lines 153-163 | exact (extend in place) |

---

## Shared Patterns

These three cross-cutting patterns apply to almost every file in this phase. Copy them
verbatim — they encode the incident scar tissue (never-500 poll, no default-queue
misrouting, no `file_id`-only payloads).

### Shared Pattern A — Never-500 degrade for the hot 5s `/pipeline/stats` poll (D-06)
**Source:** `src/phaze/services/pipeline.py` — `get_stage_busy_counts` (lines 328-360),
`get_search_busy_count` (373-397), `count_inflight_jobs` (695-717).
**Apply to:** EVERY new `saq_jobs` read (straggler) and `files` read (ANALYSIS_FAILED) added
to `services/pipeline.py`.

The exact `saq_jobs` SAVEPOINT idiom — copy this shape, change only the query + post-loop:

```python
async def get_stage_busy_counts(session: AsyncSession) -> dict[str, int]:
    out: dict[str, int] = {"metadata": 0, "analyze": 0, "fingerprint": 0}
    try:
        async with session.begin_nested():          # SAVEPOINT — NOT session.rollback()
            rows = (await session.execute(_STAGE_BUSY_SQL)).all()
    except Exception:
        logger.warning("stage_busy_degraded", exc_info=True)
        return out                                   # degrade to zero/empty, NEVER raise
    for row in rows:
        ...
    return out
```

Why `session.begin_nested()` and not `session.rollback()` (load-bearing, repeated in every
docstring 341-347 / 381-387): a plain `session.rollback()` would **expire the dashboard's
already-loaded ORM objects** (`agents`, `recent_scans`) and 500 the page on the next lazy
load. The SAVEPOINT rolls back ONLY the failed read and recovers the aborted Postgres
transaction without poisoning later queries.

For pure single-scalar COUNTs there is also `_safe_count` (lines 143-160) which rolls back
on error — but for the straggler `saq_jobs` read you want the **SAVEPOINT** variant above
(it touches the broker table that may be absent pre-migration), and for the `files`
ANALYSIS_FAILED count either `_safe_count` or a SAVEPOINT is acceptable (follow the
ANALYZED-count precedent in `get_pipeline_stats`).

### Shared Pattern B — Static SQL, no interpolated operator input (T-t7k-01)
**Source:** `_STAGE_BUSY_SQL` (line 320), `_INFLIGHT_COUNT_SQL` (line 692).
**Apply to:** the straggler `saq_jobs` query.

```python
_STAGE_BUSY_SQL = text("SELECT split_part(key, ':', 1) AS fn, COUNT(*) AS n FROM saq_jobs WHERE status IN ('queued', 'active') GROUP BY fn")
_INFLIGHT_COUNT_SQL = text("SELECT COUNT(*) FROM saq_jobs WHERE status IN ('queued', 'active')")
```

The only literals allowed in the SQL are `split_part`, the `status` allowlist, and a
function-name constant. The straggler query keys on the `process_file` prefix
(`split_part(key, ':', 1) = 'process_file'`) and `status = 'active'` (a straggler is
*running*, not queued — distinct from the busy gates which count `queued`+`active`). The age
threshold value, if expressed in SQL, must be a **bound parameter**, never an f-string.

### Shared Pattern C — Enqueue routing + full-payload discipline (D-05, MANDATORY incident guards)
**Source:** `src/phaze/services/analysis_enqueue.py::enqueue_process_file` (43-87) and the
router producers `_enqueue_analysis_jobs` (221-241).
**Apply to:** the deepen re-enqueue.

Two non-negotiable rules, both with their own past incident:
1. **Never the default queue (Phase 30).** Resolve via
   `enqueue_router.resolve_queue_for_task("process_file", request.app.state, session)` —
   the consumer-less default queue stranded 11,428 jobs.
2. **Always the COMPLETE `ProcessFilePayload` (v4.0.8 incident).** A `file_id`-only enqueue
   dead-letters every job under `extra="forbid"`. Build all fields and serialize with
   `**payload.model_dump(mode="json")` so the UUID round-trips as a string.
3. **Funnel through `enqueue_process_file`** (do NOT hand-roll a second producer) so the
   deterministic key `process_file:<file_id>` stays identical across all paths and SAQ's
   dedup collapses an in-flight repeat to a no-op (D-05 note: re-deepening an *in-flight*
   file dedups to no-op; re-deepening an already-`ANALYZED` file with no live job is a fresh
   enqueue).

---

## Pattern Assignments

### `src/phaze/services/pipeline.py` — straggler count/list (service, read)

**Analog:** `get_stage_busy_counts` / `get_search_busy_count` / `count_inflight_jobs` (same file).
**Data source:** `saq_jobs` (D-01) — NOT `files` (no state-change timestamp exists).

Model the new `get_straggler_count` / `get_straggler_jobs` on `get_search_busy_count`
(single-prefix bucket, line 373) but: (1) filter `status = 'active'` only (a straggler is
running), (2) restrict to the `process_file` prefix, (3) add the running-age predicate —
**subject to the BYTEA caveat in the banner above.** Copy Shared Pattern A's SAVEPOINT
wrapper and Shared Pattern B's static-SQL/bound-param discipline verbatim.

Threshold knob (Claude's discretion, D-01): add `straggler_threshold_sec` to config
mirroring the existing `analysis_inner_timeout_sec` Field at `config.py:446-452`
(default tie-in: 6600s):

```python
analysis_inner_timeout_sec: int = Field(
    default=6600, gt=0, lt=7200,
    validation_alias=AliasChoices("PHAZE_ANALYSIS_INNER_TIMEOUT_SEC", "analysis_inner_timeout_sec"),
    description="...",
)
```

### `src/phaze/services/pipeline.py` — ANALYSIS_FAILED count/list (service, read)

**Analog:** `get_files_by_state` (565-577) for the list; `get_pipeline_stats` (52-62) for the count.
**Data source:** indexed `files.state = 'analysis_failed'` (D-02; `ix_files_state` exists at
`models/file.py:74`). `FileState.ANALYSIS_FAILED = "analysis_failed"` is at `models/file.py:39`.

The list is a one-liner reuse:
```python
async def get_files_by_state(session: AsyncSession, state: FileState) -> list[FileRecord]:
    stmt = select(FileRecord).where(FileRecord.state == state)
    result = await session.execute(stmt)
    return list(result.scalars().all())
```
Call `get_files_by_state(session, FileState.ANALYSIS_FAILED)`. The count is a `func.count`
on the same predicate — wrap in `_safe_count` (lines 143-160) for poll-safety. Note D-02:
`ANALYSIS_FAILED` is intentionally ABSENT from `PIPELINE_STAGES` (lines 40-49) — Phase 44
surfaces it as its own bucket; do NOT add it to `PIPELINE_STAGES` (that would double-count it
in the linear stat bar). Straggler (still grinding) and failed (gave up) are two distinct
buckets.

### `src/phaze/routers/pipeline.py` — dashboard wiring (router, request-response)

**Analog:** `dashboard()` (312-364), `pipeline_stats_partial()` (367-401), `_build_dag_context()` (118-204).

Both `dashboard()` and `pipeline_stats_partial()` must call the new service reads and add
them to the template context — they share the 5s poll. The established wiring comment idiom
(lines 175-178) documents WHY no try/except wraps the call (the service owns the degrade):

```python
# get_stage_busy_counts owns the never-500 degrade (all-zeros on any DB error), so NO
# try/except is added here; these ints ride the same dag.items() seed + OOB loop.
busy = await get_stage_busy_counts(session)
dag["analyzeBusy"] = int(busy["analyze"])
```

Add the straggler/failed values to the `context` dict in BOTH the dashboard (lines 353-363)
and the stats partial (lines 392-400), exactly mirroring how `**activity` / `**dag_ctx` are
spread. Import the new service functions into the existing `from phaze.services.pipeline import (...)`
block (lines 24-43).

### `src/phaze/routers/pipeline.py` — deepen-analysis POST endpoint (router → enqueue)

**Analog (endpoint shape):** `tracklists.rescrape_tracklist` (`routers/tracklists.py:395-414`)
— a single-resource path-param re-trigger that resolves the queue and re-enqueues:
```python
@router.post("/{tracklist_id}/rescrape", response_class=HTMLResponse)
async def rescrape_tracklist(request: Request, tracklist_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if tracklist:
        routed = await resolve_queue_for_task("scrape_and_store_tracklist", request.app.state, session)
        await routed.queue.enqueue("scrape_and_store_tracklist", tracklist_id=str(tracklist_id))
    ...
```

**Analog (routing + cast idiom for `process_file`):** `trigger_analysis_ui` (404-431):
```python
try:
    routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session)
except enqueue_router.NoActiveAgentError:
    no_active_agent = True
else:
    agent_id = cast("str", routed.agent_id)   # AGENT_TASK -> agent_id never None
    ...
```

The new endpoint (suggest `POST /pipeline/files/{file_id}/deepen`): load the `FileRecord` by
id (like `rescrape_tracklist` loads the tracklist), resolve the `process_file` queue via
`enqueue_router`, then call the **extended** `enqueue_process_file(queue, file, agent_id,
models_path, fine_cap=..., coarse_cap=...)` with the elevated/sentinel cap (`0` →
`_stride_to_cap` no-op → analyze ALL windows, per `analysis.py:391` + D-04). Return an HTMX
fragment. Use `settings.models_path` (as `trigger_analysis` does at line 276). Honor the
`NoActiveAgentError` branch (`process_file` is an agent task).

### `src/phaze/schemas/agent_tasks.py` — `ProcessFilePayload` cap override (schema)

**Analog:** the optional-field-with-default idiom on the same file —
`ExecuteApprovedBatchPayload.sub_batch_index: int = 0` (line 118) and
`ExecuteBatchProposalItem.sha256_hash: str | None = None` (line 102). Both show the
"new optional field, default preserves legacy callers" pattern under `extra="forbid"`.

Current target (lines 28-37):
```python
class ProcessFilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: uuid.UUID
    original_path: str
    file_type: str
    agent_id: str
    models_path: str
```
Add (D-04, Claude's discretion on shape — two `*_cap` fields thread most cleanly into
`analyze_file`'s existing kwargs):
```python
    fine_cap: int | None = None    # None -> worker uses AgentSettings 60 default
    coarse_cap: int | None = None  # None -> worker uses AgentSettings 30 default
```
Defaults of `None` keep every existing producer (the bulk `_enqueue_analysis_jobs` path)
valid under `extra="forbid"` without change.

### `src/phaze/services/analysis_enqueue.py` — `enqueue_process_file` cap pass-through (service, producer)

**Analog:** `enqueue_process_file` itself (43-87) — extend the signature + payload build.

Current build (57-63):
```python
payload = ProcessFilePayload(
    file_id=file.id,
    original_path=file.original_path,
    file_type=file.file_type,
    agent_id=agent_id,
    models_path=models_path,
)
```
Extend the signature with `fine_cap: int | None = None, coarse_cap: int | None = None`
(defaults preserve the bulk `_enqueue_analysis_jobs` caller at `routers/pipeline.py:240-241`,
which passes positionally) and add `fine_cap=fine_cap, coarse_cap=coarse_cap` to the payload.
Keep the deterministic key (`process_file_job_key`, lines 32-40), the `timeout=7200` /
`retries=2` policy (80-86), and the `await queue.connect()` idempotent open (69). Do NOT
fork a second producer (D-05 anti-drift).

### `src/phaze/tasks/functions.py` — `process_file` cap threading (task, transform)

**Analog:** the EXISTING AgentSettings→`analyze_file` cap threading in the same function
(lines 153-163):
```python
cfg = _agent_settings()
try:
    analysis = await run_in_process_pool(
        ctx, _load_analyze_file(),
        payload.original_path, payload.models_path,
        timeout=cfg.analysis_inner_timeout_sec,
        fine_cap=cfg.analysis_fine_cap,
        coarse_cap=cfg.analysis_coarse_cap,
    )
```
Change ONLY the two cap kwargs to prefer the payload override, falling back to AgentSettings
(D-04): `fine_cap=payload.fine_cap if payload.fine_cap is not None else cfg.analysis_fine_cap`
(same for coarse). `payload` is already `ProcessFilePayload.model_validate(kwargs)` (line 141),
so the new fields are available with no other change. Leave the terminal-classification
exception handlers (164-184) and the coverage forwarding (193-214) untouched. `analyze_file`
already accepts `fine_cap`/`coarse_cap` kwargs (`services/analysis.py:520-528`), and
`cap <= 0` is the documented "analyze ALL windows" no-op (`_stride_to_cap`, `analysis.py:391`).

### `src/phaze/templates/pipeline/dashboard.html` — straggler/failed card (template)

**Analog:** the existing `{% include %}` card slots on the same file (lines 13-23):
```html
<!-- Phase 27: Trigger Scan card -->
{% include "pipeline/partials/trigger_scan_card.html" %}
...
<div id="pipeline-stats" hx-get="/pipeline/stats" hx-trigger="every 5s" hx-swap="innerHTML">
    {% include "pipeline/partials/stats_bar.html" %}
</div>
```
Add a new `{% include "pipeline/partials/straggler_failed_card.html" %}` slot. Claude's
discretion (D-01): inline counts vs an HTMX-expanded drill-down — if a drill-down, follow the
`hx-get` + partial idiom shown by the `pipeline-stats` block (an `hx-get` to a new
`/pipeline/...` partial route on the same router). The card's count values come through the
dashboard/stats context wired above.

### `src/phaze/templates/proposals/partials/sampled_badge.html` — NEW badge partial (template)

**Analog:** `tracklists/partials/confidence_badge.html` (the cleanest "render-if-present,
else nothing" badge) and `source_badge.html`:
```html
{% if confidence is not none %}
{% if confidence >= 90 %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400">{{ "%.0f"|format(confidence) }}%</span>
...
{% endif %}
{% endif %}
```
Copy this structure exactly. Gate on `analysis.sampled` (D-03): NULL/false `sampled` → render
NOTHING (pre-Phase-43 rows carry NULL coverage; treat NULL as "not sampled", never an error).
When `sampled` is true, render the pill (suggest amber/yellow like `confidence_badge`'s
mid-tier) with the four coverage counts in the `title=` tooltip
(`fine_windows_analyzed/total`, `coarse_windows_analyzed/total`) — e.g.
`title="fine 60/412 windows — sampled"`. The coverage columns are on `AnalysisResult`
(`models/analysis.py:28-32`). Render it from the proposals row / `analysis_timeline.html`
detail (`templates/proposals/partials/analysis_timeline.html`) — the natural per-file
analysis surface and the home of the "Deepen analysis" button (D-04).

---

## Test Patterns

**Analog:** `tests/test_services/test_pipeline.py` — already has the degrade-test idiom for
every `saq_jobs` reader. Copy these for the new straggler reader:
- `test_get_stage_busy_counts_buckets_by_function_prefix` (line 245) — happy-path bucketing
  with a fake session whose `begin_nested()` returns a `_NullSavepoint` (line 230).
- `test_get_stage_busy_counts_degrades_on_db_error` (282) — forces the read to raise, asserts
  zeros and no raise.
- `test_get_stage_busy_counts_degrade_does_not_poison_session` (303) — `DROP TABLE IF EXISTS
  saq_jobs` then asserts the outer session still works.

For the enqueue cap pass-through: `tests/test_services/test_analysis_enqueue.py` (existing) —
assert the new `fine_cap`/`coarse_cap` land in the serialized payload and the deterministic
key is unchanged. For the deepen endpoint: mirror the router HTMX trigger tests. 85% coverage
is mandatory (CLAUDE.md).

---

## No Analog Found

None. Every new/modified file maps to a concrete in-repo analog. The only NOVEL element is the
**straggler running-age predicate**, which has no existing analog because no current query
reads job *age* — and the `saq_jobs` BYTEA caveat (top banner) means the planner must design
that one predicate explicitly rather than copy an existing age filter.

---

## Metadata

**Analog search scope:** `src/phaze/services/`, `src/phaze/routers/`, `src/phaze/templates/`,
`src/phaze/schemas/`, `src/phaze/tasks/`, `src/phaze/models/`, `src/phaze/config.py`,
`tests/test_services/`, plus the installed `saq` package DDL.
**Files scanned:** ~18 source files + SAQ package internals.
**Pattern extraction date:** 2026-06-18
