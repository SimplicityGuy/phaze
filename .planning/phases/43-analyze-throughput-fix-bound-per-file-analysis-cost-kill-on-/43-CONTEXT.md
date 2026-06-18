# Phase 43: Analyze Throughput Fix - Context

**Gathered:** 2026-06-17
**Status:** Ready for planning
**Source:** Derived from debug session `.planning/debug/analyze-4h-timeouts.md` (root-caused live on nox/lux) + user design decisions 2026-06-17

<domain>
## Phase Boundary

Backend-only fix to make the Analyze stage (essentia `process_file`) actually drain. Long DJ/concert
analysis legitimately exceeds the 4h SAQ timeout (root-caused 2026-06-17: **72 timeouts vs 60
completions over ~57h**; analysis cost is **O(file duration)**). This phase bounds per-file cost,
kills runaway essentia children deterministically, stops wasteful retries, and surfaces analysis
outcomes (analyzed / sampled / failed) in the file state machine. Redeployable to the homelab
immediately. **In scope:** worker analysis code, process pool, enqueue policy, control API + models +
migration, config. **Out of scope (Phase 44):** all dashboard/UI work (straggler counts, sampled
badge, deepen-analysis re-trigger). **Out of scope (Backlog):** distributed cloud analysis.

## Root cause + live state (authoritative facts; do not re-investigate)

- Timeout/retries set at `src/phaze/services/analysis_enqueue.py:78` (`timeout=14400`) and `:83`
  (`retries=2`). This single helper (`enqueue_process_file`) is the ONLY `process_file` producer
  (both the dashboard `trigger_analysis` and the Phase-42 recovery path funnel through it).
- `worker_max_jobs=8` but `worker_process_pool_size=4` (`src/phaze/config.py:222,225`): 8 SAQ
  coroutine slots, only 4 real compute slots. Jobs awaiting an executor seat still burn their
  wall-clock timeout.
- `analyze_file` (`src/phaze/services/analysis.py:472`) runs two passes, both O(duration): FINE
  (30s window every 30s → RhythmExtractor2013 + KeyExtractor) and COARSE (180s window every 180s →
  12 TF model inferences). A 3h set = ~360 fine + ~60 coarse windows. `_iter_windows` (`:364`)
  generates the window list; `_analyze_fine_windows` (`:411`) / `_analyze_coarse_windows` (`:435`)
  iterate it.
- `run_in_process_pool` (`src/phaze/tasks/pool.py:16`) uses `loop.run_in_executor` over a
  `ProcessPoolExecutor` (`:11`). On SAQ `wait_for` timeout the awaiting coroutine is cancelled but
  `concurrent.futures` CANNOT cancel an already-started child → essentia keeps computing (leaked
  compute, pool starvation). Live: worker at 483% CPU / 28.6 GiB.
- `put_analysis` (`src/phaze/routers/agent_analysis.py:69`) upserts `AnalysisResult` but NEVER sets
  `FileRecord.state`. Live (lux, read-only): ALL 11428 files are `discovered` including the 79 with
  analysis rows. So success does not advance state either — not just timeout. Latent bug:
  `trigger_analysis`/`recover_orphaned_work` select `get_files_by_state(DISCOVERED)`, so any
  re-trigger re-enqueues all 11428 (incl. done).
- Only per-window consumers are visualization: BPM sparklines (`src/phaze/routers/proposals.py:89`)
  + proposal detail (`:269`). NO algorithm needs contiguous windows → sampling/striding is safe.
- The agent worker is **Postgres-free** (HTTP-only, D-25). It talks to the control plane via
  `PhazeAgentClient` (`src/phaze/services/agent_client.py`) → `/api/internal/agent/*` endpoints.
  Any failure/coverage marking MUST go through a control API call, not a direct DB write.
- `AnalysisResult` model: `src/phaze/models/analysis.py:12`; `AnalysisWindow`: `:27`. `FileState`
  enum: `src/phaze/models/file.py` (StrEnum stored as `String(30)` → adding enum values needs NO
  DB migration, just code; `ANALYZED` and `FAILED` already exist).
- Payload schema: `ProcessFilePayload` (build in `analysis_enqueue.py`); analysis write payload
  `AnalysisWritePayload` / window payload `AnalysisWindowPayload` in `src/phaze/schemas/agent_analysis.py`.
- SAQ version `saq[postgres]>=0.26.4` (Postgres broker; job.timeout enforced via
  `asyncio.wait_for(asyncio.shield(task), timeout)` in `saq/worker.py:368`).

</domain>

<decisions>
## Implementation Decisions (LOCKED — user, 2026-06-17)

### Bound per-file cost — cap + even stride
- Cap window counts per file: **60 fine / 30 coarse** (config-exposed via `AgentSettings`, mirror
  the existing `analysis_*` config pattern in `config.py`).
- When a file's natural window count exceeds the cap, **stride evenly across the WHOLE file** (not
  first-N) so coverage spans the entire set. Under the cap → analyze every window (unchanged).
- Implement in the window-generation/iteration path (`_iter_windows` and/or the fine/coarse
  iterators in `analysis.py`). Sparklines/aggregate (`_representative_features`) remain valid.

### Mark "incompletely analyzed" (sampled) — NEW requirement
- When a file was strided (sampled, not exhaustive), record that it is **partially analyzed / more
  data available**, so it can be re-run later with a higher/unbounded budget. Three outcomes:
  **fully analyzed · sampled(partial) · failed**.
- `analyze_file` returns coverage info: fine_analyzed/fine_total, coarse_analyzed/coarse_total, and
  a `sampled` boolean (true when any pass was strided). Persist on the analysis row (coverage
  columns → Alembic migration) so Phase 44 can badge it and a "deepen" re-run can target it.

### Kill-on-timeout (gating)
- Replace the bare `ProcessPoolExecutor` path with a **killable** mechanism: `pebble.ProcessPool`
  (new dependency) with a hard per-task timeout that SIGKILLs + recycles the worker process, OR an
  equivalent (multiprocessing/asyncio subprocess hard-killed on timeout). Add an **inner** analysis
  timeout BELOW the SAQ job timeout so the kill is deterministic (not reliant on SAQ cancel, which
  can't reach the child). Defense-in-depth even after bounding (a single window could still wedge).

### State-machine full fix
- Set `FileState.ANALYZED` on a successful analysis PUT (control side, in/around `put_analysis`).
- Add `FileState.ANALYSIS_FAILED` (new enum value) set on terminal failure.
- Persist sampled/coverage on the analysis row (Alembic migration adds the columns).
- Because the worker is Postgres-free, terminal-failure + coverage reporting needs a **control API**
  path the worker calls (extend `put_analysis` payload for coverage; add a failure-report endpoint or
  field for terminal failure). Fixing state also fixes the latent "re-enqueue all 11428" bug.

### Retry policy (CORRECTED 2026-06-17 after research — SAQ semantics)
- **KEEP `retries=2`** at `analysis_enqueue.py:83` (do NOT change to 1). In SAQ `retryable = retries
  > attempts` with `attempts` starting at 1, so `retries=2` = exactly **one** real retry; and
  `apply_project_job_defaults` CLOBBERS any job left at the SAQ default `retries==1` up to
  `worker_max_retries` (4) — so literal `retries=1` would both mean "0 retries" AND get clobbered.
  `retries=2` is the faithful implementation of "retry once for transient errors."
- Treat **`TimeoutError` as terminal**: the SAQ job timeout is enforced OUTSIDE the task
  (`asyncio.wait_for(shield(task), job.timeout)`) so the task body cannot catch it. Instead the
  **inner pebble timeout** (set below the SAQ timeout) raises a catchable `TimeoutError`/`ProcessExpired`
  inside `process_file` (`src/phaze/tasks/functions.py:114`); on that, the worker reports
  `ANALYSIS_FAILED` via the control API and does NOT re-raise-for-retry (no wasteful re-run).
- Lower the SAQ `process_file` timeout from `14400` to **~2h (7200s)** at `analysis_enqueue.py:78`
  (outer safety net; the inner pebble timeout, set lower, does the real killing).

### Kill mechanism — `pebble` (CONFIRMED dependency, 2026-06-17)
- Add `pebble` via `uv add` (mature, pure-Python, no compiled wheel → cp314-safe). Use
  `ProcessPool.schedule(func, args=, timeout=)` → SIGKILLs + recycles the child on timeout and raises
  a catchable `TimeoutError`. Bridge to async with `asyncio.wrap_future` (NOT `run_in_executor`).
  Replaces the `ProcessPoolExecutor` in `src/phaze/tasks/pool.py`.
- Lower the SAQ `process_file` timeout from `14400` to **~2h (7200s)** at `analysis_enqueue.py:78`
  (inner analysis timeout does the real killing; 2h is the outer safety net).

### Claude's Discretion (planner decides specifics)
- Exact pebble API usage + pool sizing interplay (`worker_process_pool_size`), and whether to bump it.
- Exact coverage column names/types on `AnalysisResult` and the Alembic migration shape.
- Whether terminal-failure reporting is a new endpoint vs a field on an existing one; whether
  `ANALYSIS_FAILED` reporting is worker-driven (on retries-exhausted/timeout) — prefer worker-driven.
- Exact config knob names (follow existing `analysis_*` naming).
- Coverage/`sampled` exposure on `AnalysisWritePayload`.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Root cause + decisions
- `.planning/debug/analyze-4h-timeouts.md` — full root-cause evidence trail + all locked decisions + sequencing.

### Code touch points
- `src/phaze/services/analysis_enqueue.py` — timeout/retries/key (enqueue policy single source).
- `src/phaze/services/analysis.py` — `analyze_file`, `_iter_windows`, fine/coarse iterators, `_representative_features` (cap+stride + coverage emit).
- `src/phaze/tasks/pool.py` — `run_in_process_pool` / `ProcessPoolExecutor` (kill-on-timeout).
- `src/phaze/tasks/functions.py` — `process_file` task body (timeout-terminal handling, coverage forwarding).
- `src/phaze/routers/agent_analysis.py` — `put_analysis` (set ANALYZED, accept coverage; failure-report path).
- `src/phaze/schemas/agent_analysis.py` — `AnalysisWritePayload`, `AnalysisWindowPayload` (coverage fields).
- `src/phaze/models/analysis.py` / `src/phaze/models/file.py` — coverage columns + `FileState.ANALYSIS_FAILED`.
- `src/phaze/config.py` — `AgentSettings` (`analysis_*` cap config; worker_* knobs).
- `src/phaze/services/agent_client.py` — `PhazeAgentClient` (HTTP path the worker uses).

</canonical_refs>

<specifics>
## Specific Ideas

- Caps: 60 fine / 30 coarse. SAQ timeout 7200s. retries=1, TimeoutError terminal.
- New dependency candidate: `pebble` (killable ProcessPool). Verify cp314 + uv compatibility.
- Alembic migration adds coverage columns to `analysis` (e.g. windows_analyzed/total per tier + sampled bool).
- New `FileState.ANALYSIS_FAILED` value (string enum, no DB migration for the enum itself).
- Min 85% coverage; `uv run` for everything; ruff/mypy clean; pre-commit must pass (no --no-verify).

</specifics>

<deferred>
## Deferred Ideas

- All UI (straggler/failed count, "sampled — more available" badge, "deepen analysis" re-trigger) → **Phase 44**.
- Distributed cloud analysis (S3 staging, GCP/OCI burst) → **Backlog** (gated on post-redeploy re-measure).
- Bumping `worker_process_pool_size` / second on-prem agent → consider in Phase 44/Backlog, not required here.

</deferred>

---

*Phase: 43-analyze-throughput-fix*
*Context derived 2026-06-17 from debug session analyze-4h-timeouts*
