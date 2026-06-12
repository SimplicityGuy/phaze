# Phase 35: Pipeline Determinism, Idempotency & Per-Job-Type Observability — Research

**Researched:** 2026-06-11
**Domain:** SAQ 0.26.4 enqueue/dedup internals; Postgres upsert idempotency; per-stage DB counters
**Confidence:** HIGH (all claims verified against installed SAQ source + project code at file:line)
**Scope:** Surgical — resolves only the Claude's-Discretion technical questions in 35-CONTEXT.md. Locked decisions (D-01..D-06), the stage DAG (35-STAGE-DEPENDENCIES.md), and Variant B design are NOT re-litigated.

SAQ version verified: `saq 0.26.4` (`uv pip show saq`) — installed at `.venv/lib/python3.14/site-packages/saq`. CONTEXT references "0.26.x"; the installed minor is `.4`, not `.3`. All source citations below are from `.4`.

---

## Q1 — Central deterministic-key enforcement via `before_enqueue` hook (D-05)

### ANSWER: A `before_enqueue` hook CAN set/override `job.key` and it IS honored by dedup. VERIFIED.

Enqueue codepath order (`saq/queue/base.py:314-357`):
1. `enqueue()` builds the `Job` and splits incoming kwargs: any kwarg matching a `Job` dataclass field becomes a Job attribute; everything else lands in `job.kwargs` (`base.py:332-346`). So by hook time, **`job.kwargs` already contains the payload** (`file_id`, `file_ids`, `tracklist_id`, etc.).
2. `await self._before_enqueue(job)` runs every registered hook (`base.py:355` → `base.py:529-530`) — **before** persistence.
3. `return await self._enqueue(job)` (`base.py:357`) does the Redis dedup write.

Dedup keys off `job.key`: `job.id = job_id(job.key)` = `f"saq:job:{queue}:{key}"` (`redis.py:96-97`), and the dedup Lua script no-ops when that id already exists (`redis.py:447-458`: `if not ZSCORE(incomplete, job_id) and EXISTS(job_id)==0 then SET... else return nil`). `Job.key` defaults to a random `uuid1` via `dataclasses.field(default_factory=get_default_job_key)` (`job.py:22-23, 120`). **Mutating `job.key` inside a `before_enqueue` hook therefore deterministically changes the dedup identity.** The existing `apply_project_job_defaults` hook already mutates `job.timeout/retries/ttl` at this exact point and the worker sees the mutation (`queue_defaults.py:62-85`, docstring line 73-74) — proving in-project that hook mutations persist.

### RECOMMENDED SEAM: extend the `before_enqueue` hook with a function→key-builder registry. (Not the router.)

Reasons, with evidence:
- The hook is the **only** universal chokepoint. It is registered on the controller queue (`main.py:103`), on every per-agent queue (`agent_task_router.py:99`), and in both worker settings modules (`tasks/controller.py:139`, `tasks/agent_worker.py:185` per CONTEXT). Every job — direct `queue.enqueue(...)` call sites AND `agent_task_router.enqueue_for_agent` — flows through it. This is the literal definition of D-05's "no call site can drift."
- The router seam (`enqueue_router.resolve_queue_for_task`) only *resolves which queue* — it does **not** perform the enqueue (`enqueue_router.py:120-143` returns a `RoutedQueue`; the actual `queue.enqueue` happens back at the 9 call sites and in `agent_task_router.py:148`). Threading a key-builder there would require editing every call site + `enqueue_for_agent` — strictly less central than the hook. Reject.

Recommended implementation shape (new sibling hook or extend the existing one):
```python
# function name -> callable(job.kwargs) -> natural id string
_KEY_BUILDERS = {
    "process_file":               lambda k: k["file_id"],
    "extract_file_metadata":      lambda k: k["file_id"],
    "fingerprint_file":           lambda k: k["file_id"],
    "scan_live_set":              lambda k: k["file_id"],
    "search_tracklist":           lambda k: k["file_id"],
    "scrape_and_store_tracklist": lambda k: k["tracklist_id"],
    "match_tracklist_to_discogs": lambda k: k["tracklist_id"],
    "generate_proposals":         lambda k: _hash_ids(k["file_ids"]),  # batch-hash, see Q3 note
}
async def apply_deterministic_key(job):
    build = _KEY_BUILDERS.get(job.function)
    if build:
        job.key = f"{job.function}:{build(job.kwargs or {})}"
```
Register it on the same three seams that register `apply_project_job_defaults`. Order vs. the defaults hook is irrelevant (they touch disjoint fields). Keep `process_file` in the registry too: the registry would compute `process_file:<file_id>`, **identical** to what `analysis_enqueue.process_file_job_key` already sets (`analysis_enqueue.py:40`), so the existing keyed path stays a no-op-equivalent — no conflict, and the helper can stay as the documented template.

**Idempotent override is intended, not a bug.** Unlike the defaults hook (which guards `if job.timeout == SAQ_DEFAULT`), the key hook should *unconditionally* set the key for registered functions — that is the anti-drift guarantee. A call site passing its own `key=` is overridden to the canonical one.

### Natural-id map for the 9 sites (all VERIFIED present in payload as a `job.kwargs` field)

| Task | Enqueue site | Payload field → natural id | Present? |
|---|---|---|---|
| `process_file` | `analysis_enqueue.py:64` | `file_id` (already keyed — template) | ✅ |
| `extract_file_metadata` | `routers/pipeline.py:305` | `file_id` | ✅ |
| `fingerprint_file` | `routers/pipeline.py:377` | `file_id` | ✅ |
| `scan_live_set` | `routers/tracklists.py:232` | `file_id` | ✅ |
| `search_tracklist` | `routers/tracklists.py:458` | `file_id` | ✅ |
| `scrape_and_store_tracklist` | `routers/tracklists.py:384` | `tracklist_id` | ✅ |
| `match_tracklist_to_discogs` | `routers/tracklists.py:658` | `tracklist_id` | ✅ |
| `generate_proposals` | `routers/pipeline.py:70` | `file_ids` (LIST) + `batch_index` | ⚠️ no scalar id — see below |
| `extract_file_metadata` (auto) | `services/ingestion.py:190` + `routers/agent_files.py:143` | `file_id` | n/a — **REMOVE** per D-06 |

**Note (CONTEXT path correction):** the tracklist sites live in `src/phaze/routers/tracklists.py` (verified), **not** `src/phaze/services/tracklists.py` as listed in CONTEXT's canonical_refs. Line numbers (232/458/384/658) match. The planner should target `routers/tracklists.py`.

**`generate_proposals` is the one outlier:** it is a **batch** task — `queue.enqueue("generate_proposals", file_ids=batch, batch_index=idx)` (`routers/pipeline.py:70`). There is no single natural file id. **Recommend `generate_proposals:<sha256(sorted(file_ids))>`** (a batch-hash). This dedups an exact-duplicate batch re-click; it does NOT and need not provide per-file idempotency — that is owned entirely by the proposals-table upsert (Q3). Reject `batch_index` as the key (re-running over a changed ready-set re-numbers batches → false dedup or false uniqueness). This keeps D-04 per-file idempotency intact at the DB layer while still preventing the exact double-enqueue the incident was about.

**MIGRATION/RISK:** None (code-only). Risk: a future task added without a `_KEY_BUILDERS` entry silently falls back to a random uuid key. Mitigate by failing loud in tests — extend `DedupFakeQueue` assertions (`tests/_queue_fakes.py`) to require a deterministic key for every routable task name in `CONTROLLER_TASKS ∪ AGENT_TASKS`.

---

## Q2 — Completion-side counter hook + reconcile source (D-02 / D-03)

### ANSWER: CONTEXT is WRONG that "there is no public after-process hook." SAQ 0.26.4 HAS an `after_process` worker hook. VERIFIED.

`Worker.__init__` accepts `after_process` (and `before_process`) lifecycle callbacks (`worker.py:68, 98, 116-117`). They run in `_after_process` (`worker.py:175-178`), invoked from the job-processing `finally` block (`worker.py:434-437`) for **every** terminal outcome (success, failure, abort). The callback receives the job `context` dict which contains the `Job` (`worker.py:359`: `{**self.context, "job": job}`) and `context["exception"]` when it failed (`worker.py:398-399`). So a completion increment can read `ctx["job"].function` and `ctx["job"].status` (== `Status.COMPLETE`) to bump a per-function "completed" counter.

Caveat: `after_process` is a **Worker constructor kwarg**, not a `register_*` method — it must be wired into `tasks/controller.py` and `tasks/agent_worker.py` settings (the two worker entrypoints), not onto the API-side `Queue`. The agent worker runs in a separate container but connects to the **same central Redis**, so its `INCR` lands in the same counter the dashboard reads.

### RECOMMENDED: use the `after_process` hook for the completed-counter INCR, but keep the DB reconcile as the authority (D-03 mandatory regardless).

- Enqueued counter: `INCR` in the **same `before_enqueue` hook** from Q1 (one hook does key + `enqueued[function]++`). Mirror the `check_rate_limit` `INCR`/`EXPIRE` precedent (`services/proposal.py:232-241`).
- Completed counter: `after_process` hook → `INCR completed[function]` when `ctx["job"].status == COMPLETE`. This is now a *clean* mechanism (CONTEXT had assumed it didn't exist and pre-authorized "best-effort or omit"). It is still best-effort by design because the reconcile is the backstop.
- **Reconcile-on-read is the truth and is mandatory** — but see Q5: the current `get_pipeline_stats()` is the WRONG reconcile source for the parallel stages. The reconcile must count stage **output tables**, not `FileRecord.state`. This is the most consequential finding in this document.

**MIGRATION/RISK:** None (code-only). Risk: counters drift on purge/restart — fully absorbed by the per-read DB reconcile (the design pin). Counters are a fast cache; DB is truth.

---

## Q3 — `generate_proposals` upsert conflict target (D-04)

### ANSWER: The `proposals` table today allows UNLIMITED rows per file and `store_proposals` does raw INSERTs. There is NO unique constraint usable as a conflict target. VERIFIED — a migration is required.

`models/proposal.py:37-53`: PK is `id` (uuid4, server-generated). `file_id` is a plain FK, **not unique**. The only index is `ix_proposals_status` on `status` (non-unique). `services/proposal.py:289-298`: `store_proposals` builds `RenameProposal(...)` and `session.add(record)` per proposal — **raw INSERT, fresh uuid each time**. Re-running "Generate Proposals" on a file today appends duplicate PENDING rows. This is the lone non-idempotent task write (CONTEXT "established patterns" confirms; everything else uses `on_conflict_do_*`).

### RECOMMENDED: partial unique index on `(file_id) WHERE status = 'pending'` + `on_conflict_do_update` targeting it.

```sql
CREATE UNIQUE INDEX uq_proposals_file_id_pending
    ON proposals (file_id) WHERE status = 'pending';
```
```python
stmt = pg_insert(RenameProposal).values(**row).on_conflict_do_update(
    index_elements=["file_id"],
    index_where=(RenameProposal.status == "pending"),
    set_={ "proposed_filename": ..., "proposed_path": ..., "confidence": ...,
           "context_used": ..., "reason": ..., "updated_at": func.now() },
)
```
Why this exactly satisfies D-04:
- The partial index covers **only** PENDING rows, so `ON CONFLICT` fires **only** against an existing pending proposal → overwrites it in place. The status guard `WHERE status='pending'` is enforced **at the DB level** (D-04's explicit requirement: "not just in app code").
- APPROVED / EXECUTED / REJECTED / FAILED rows are **outside** the index → never a conflict target → **never touched**. Human approvals are structurally protected.
- Multiple non-pending history rows per file remain legal (the table keeps approval history). Mirror the established `on_conflict_do_update` pattern at `routers/agent_metadata.py:61` / `agent_fingerprint.py:40`.

Match `RenameProposal.__table_args__` to the partial index (add an SQLAlchemy `Index(..., postgresql_where=...)` so autogenerate/ORM stays in sync).

### MIGRATION/RISK — ⚠️ blocking data hazard

The live DB (11,428-file archive, history of raw inserts) **almost certainly already has multiple PENDING proposals per file**. Creating a unique index will **fail** on existing duplicates. The Alembic migration MUST, in order: (1) collapse existing duplicate pending rows to one-per-file (keep most-recent `created_at`, delete the rest — or demote older ones), THEN (2) create the partial unique index. Plan this as two ops in one migration. Without the dedupe step the migration aborts on the homelab.

Edge note (non-blocking): if a file already has an APPROVED proposal and `generate_proposals` is somehow re-run, the upsert would INSERT a *new* pending row alongside the approved one (no conflict, partial index excludes approved). In practice the convergence-gate query (`routers/pipeline.py:116-128`) selects files in `ANALYZED`/`METADATA_EXTRACTED` state, and an approved file is in `APPROVED` state, so it won't be re-selected. Acceptable; no extra app guard required.

---

## Q4 — Idempotency audit: `execution_log` + `tag_write_log` (D-04 item 2)

### `execution_log` — VERIFIED idempotent. No change.
`routers/agent_execution.py:77`: `pg_insert(ExecutionLog).values([payload]).on_conflict_do_nothing(index_elements=["id"])`. Confirmed exactly as CONTEXT claims. Replay POST is a silent no-op (agent supplies `id`, persisted in SAQ job state). The PATCH path is also idempotent for same-status terminal retries (`agent_execution.py:96-98`).

### `tag_write_log` — NO gap. Leave as append-only. Do NOT add an upsert.
`tag_write_log` is an **append-only audit trail by design** (`models/tag_write_log.py:29-33` docstring: "Records every tag write attempt with before/after snapshots"; "Follows the ExecutionLog append-only pattern"). PK `id` is a fresh uuid4; `file_id` is a non-unique FK; reads use "latest by `written_at desc`" (`routers/tags.py:97`). `execute_tag_write` does `session.add(log_entry)` with a new uuid each call (`services/tag_writer.py:204-214`).

Adding an upsert here would **destroy the audit history**, which is the table's entire purpose. Re-run protection for tag writes belongs upstream: tag writes only happen inside `execute_approved_batch` (the terminal task), whose duplicate-run suppression comes from its deterministic job key + the `execution_log` `on_conflict_do_nothing`. **Recommendation: no fix.** This is the "audit and add upsert only if a gap is found" carve-out resolving to "no gap."

Secondary note for the planner: `execute_approved_batch` is an `AGENT_TASK` (`enqueue_router.py:67`) but is **not** in the 9-site scope list and is not in the Q1 `_KEY_BUILDERS` map above. If the planner wants belt-and-suspenders suppression of duplicate audit rows from an accidental double-trigger, add `execute_approved_batch:<batch_id>` to the registry (verify `batch_id` is in its payload at the enqueue site before doing so). Optional — not required by D-04.

---

## Q5 — DAG counter denominators — ⚠️ the current stats source CANNOT drive the parallel DAG

### ANSWER: `get_pipeline_stats()` groups by `FileRecord.state`, a SINGLE LINEAR enum per file. It structurally cannot report parallel-stage "done" counts. VERIFIED.

`services/pipeline.py:34-44`: `get_pipeline_stats` runs `select(state, count(id)).group_by(state)`. But `FileRecord.state` is a single `StrEnum` value (`models/file.py:20-44, 59`) — a file holds **exactly one** state. Per 35-STAGE-DEPENDENCIES.md, `extract_file_metadata` / `fingerprint_file` / `process_file` are **mutually parallel and independent**. A file that has been fingerprinted AND analyzed still carries only one `state` string — so `count(state == 'fingerprinted')` is **not** "how many files are fingerprinted." The linear state machine and the parallel DAG are fundamentally different shapes. Using `get_pipeline_stats` as the per-node reconcile (D-03) would report wrong "done" numbers for every parallel node.

### RECOMMENDED: reconcile each DAG node against its stage OUTPUT table, via a NEW per-stage count query.

DB-truth "done" per node = `COUNT(DISTINCT file_id)` (or tracklist_id) in the stage's write target (table names all verified via `grep __tablename__`):

| DAG node | DB-truth "done" source (VERIFIED table) | Denominator "total" source | Clean denom? |
|---|---|---|---|
| Discovery | `COUNT(files)` (`files`) | itself (root) | ✅ |
| Extract Metadata | `COUNT(DISTINCT file_id)` in `metadata` | music/video files (filter from `routers/pipeline.py:318-319`) | ✅ |
| Fingerprint | `COUNT(DISTINCT file_id)` in `fingerprint_results` (status='completed') | music/video files | ✅ |
| Analyze | `COUNT(DISTINCT file_id)` in `analysis` | music/video files | ✅ |
| scan_live_set / search_tracklist | `COUNT(DISTINCT file_id)` in `tracklists` | ⚠️ no clean denom (operator-selected / live-set subset) | ❌ counter-only |
| scrape_and_store_tracklist | `COUNT(DISTINCT tracklist_id)` in `tracklist_versions` | `COUNT(tracklists)` | ✅ |
| match_tracklist_to_discogs | `COUNT(DISTINCT tracklist_id)` in `discogs_links` | `COUNT(tracklists)` | ✅ |
| Proposals | `COUNT(DISTINCT file_id)` in `proposals` (or `state==PROPOSAL_GENERATED`) | convergence-gate set: files with both `metadata` AND `analysis` (the `routers/pipeline.py:116-128` query) | ✅ |
| Execute | `COUNT(DISTINCT file_id)` in `execution_log` (status completed) / `state==EXECUTED` | approved proposals | ✅ |

Two flags for the planner:
1. **Build a new `get_stage_progress(session)` (or extend `get_pipeline_stats`)** that returns per-stage output-table distinct-file counts. The existing `get_pipeline_stats` (linear state) stays useful only for the strictly-linear tail (Proposals/Approved/Executed) where state IS the truth; the parallel Tier-1 nodes need the new query. Do **not** reuse the linear `group_by(state)` for Metadata/Fingerprint/Analyze.
2. **The tracklist branch (scan_live_set/search_tracklist) has no clean DB denominator** — there's no table that says "these N files *should* get a tracklist." For that node the maintained counter (enqueued) is the only "total" signal; the `tracklists`-table count is the reconcilable "done." The planner must accept counter-as-denominator for this one node (matches D-02's "maintained counters" decision) and not fabricate a denominator.

Live queue depth for the "active/agent-busy" node states already exists: `get_queue_activity()` (`services/pipeline.py:47-104`) returns `agent_busy`/`controller_busy` — reuse for the gated-trigger "Agent busy" disable reason, exactly as Phase 34 does.

**MIGRATION/RISK:** None (read-only queries). Risk: `COUNT(DISTINCT file_id)` over large output tables on every 5s poll could be slow at archive scale. All these tables already have a `file_id`/`tracklist_id` index (verified: `ix_*_file_id` on metadata/fingerprint/tag_write_log; per-engine UQ on fingerprint). If counts get hot, cache them in the maintained Redis counter and let the reconcile run on a slower cadence than the 5s UI poll (D-03 allows "and/or periodically").

---

## Open risks for the planner

1. **🔴 Stats-source mismatch (Q5) is the biggest planning impact.** `get_pipeline_stats()` (group-by linear `state`) cannot drive the parallel DAG nodes. Plan a new per-stage output-table count query (`metadata`/`fingerprint_results`/`analysis`/`tracklists`/`tracklist_versions`/`discogs_links`/`proposals`/`execution_log`). Treat this as a first-class work item, not a tweak.
2. **🔴 Proposals migration must dedupe before indexing (Q3).** The live DB likely has multiple PENDING rows per file; `CREATE UNIQUE INDEX ... WHERE status='pending'` will abort unless the migration collapses duplicates first. One Alembic revision, two ordered ops.
3. **🟡 CONTEXT factual correction (Q2):** SAQ 0.26.4 *does* expose an `after_process` worker hook (`worker.py:98,175,435`). The completion-increment is therefore a clean mechanism, not the "best-effort/omit" fallback CONTEXT assumed. DB reconcile remains mandatory regardless (D-03 unchanged).
4. **🟡 `generate_proposals` is a batch task (Q1/Q3).** Job key = batch-hash of `file_ids`; per-file idempotency lives in the proposals upsert, not the job key. Do not key it by a single file_id.
5. **🟡 CONTEXT path correction:** tracklist enqueue sites are in `src/phaze/routers/tracklists.py` (not `services/tracklists.py`); line numbers 232/384/458/658 are correct.
6. **🟢 Drift guard:** any future routable task without a `_KEY_BUILDERS` entry silently reverts to a random key. Add a test over `CONTROLLER_TASKS ∪ AGENT_TASKS` asserting every name produces a deterministic key (extend `DedupFakeQueue`, `tests/_queue_fakes.py`).
7. **🟢 `tag_write_log` is intentionally append-only (Q4)** — do not "fix" it with an upsert; that would erase the audit trail. No gap.

## Sources

### Primary (HIGH — installed source + project code)
- SAQ 0.26.4 source: `saq/queue/base.py:314-357,529-530` (enqueue→hook→dedup order), `saq/queue/redis.py:96-97,447-458` (key→job_id→dedup Lua), `saq/job.py:22-23,84,120` (key default factory), `saq/worker.py:68,98,116-117,175-178,355-437` (after_process lifecycle hook).
- Project: `services/analysis_enqueue.py`, `services/enqueue_router.py`, `services/agent_task_router.py:99,148`, `tasks/_shared/queue_defaults.py`, `main.py:103`, `routers/pipeline.py:70,305,377`, `routers/tracklists.py:232,384,458,658`, `services/ingestion.py:190`, `services/pipeline.py:34-104`, `models/proposal.py`, `services/proposal.py:232-298`, `models/tag_write_log.py`, `services/tag_writer.py:204-214`, `routers/agent_execution.py:77`, `models/file.py:20-59`, table names via `grep __tablename__`.

## Metadata
- Standard stack: HIGH (existing project stack, no new deps).
- SAQ hook/dedup behavior: HIGH (read installed 0.26.4 source line-by-line).
- Proposals/stats schema: HIGH (read models + queries).
- Research date: 2026-06-11. Valid until: SAQ minor bump or proposals/pipeline schema change.

## RESEARCH COMPLETE
