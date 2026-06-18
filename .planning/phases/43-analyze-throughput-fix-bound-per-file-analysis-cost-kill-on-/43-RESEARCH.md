# Phase 43: Analyze Throughput Fix - Research

**Researched:** 2026-06-17
**Domain:** killable CPU process pools (essentia), windowed-analysis sampling, Alembic column add, SAQ retry/terminal semantics, worker→control internal API
**Confidence:** HIGH (most surfaces read directly from source; pebble API verified from official docs + PyPI)

## Summary

The phase is fully scoped by `43-CONTEXT.md` and the debug trail — all *design* decisions are LOCKED (cap 60/30 + even stride, pebble kill-on-timeout, inner timeout below SAQ, `ANALYSIS_FAILED` state, coverage columns, SAQ timeout→7200s, worker-driven terminal reporting). This research answers only the HOW: exact library APIs and the repo's own conventions the planner must wire to.

Five concrete mechanics dominate: (1) `pebble.ProcessPool.schedule(..., timeout=)` returns a `concurrent.futures.Future`-compatible `ProcessFuture` that **SIGKILLs + recycles** the worker on timeout and raises `TimeoutError` *inside* `process_file` (catchable — unlike the current `ProcessPoolExecutor`, which cannot cancel a running child); (2) even-stride sampling is a pure post-`_iter_windows` downsample preserving the original `window_index`; (3) coverage columns follow the hand-written numbered-migration pattern (next rev `021`); (4) the worker reports back exclusively through `PhazeAgentClient` → `/api/internal/agent/*`; (5) SAQ enforces the job timeout *outside* the task, so terminal classification must be **worker-driven** off the inner pebble `TimeoutError` plus `ctx["job"].attempts`/`.retries`.

**Primary recommendation:** Add `pebble>=5.2.0`; rewrite `pool.py` to own a `pebble.ProcessPool` and expose a `run_in_process_pool(ctx, func, *args, timeout=...)` that `await asyncio.wrap_future(pool.schedule(...))`. Catch `TimeoutError` in `process_file`, report terminal failure via a **new** `report_analysis_failed` client method/endpoint, and set `FileState.ANALYZED` inside `put_analysis`. **Do NOT set `retries=1` literally** — see Pitfall 1 (it is the SAQ default and gets clobbered to 4).

## Two LOCKED-decision traps the planner MUST honor (not re-litigate, but get right)

These are *locked* but have a non-obvious correct implementation — flagged so the plan does not implement the literal text wrongly:

- **`retries=1` is the SAQ default → `apply_project_job_defaults` clobbers it to `worker_max_retries` (4).** See Pitfall 1. To express "one real retry for transient errors," use `retries=2` (2 attempts; `2 != 1` dodges the clobber). [VERIFIED: `src/phaze/tasks/_shared/queue_defaults.py:57,82-83`]
- **SAQ enforces job timeout *outside* the task**, so `process_file` cannot catch the *SAQ-level* timeout. The inner pebble timeout (locked) is what makes a catchable `TimeoutError`. See Pitfall 2. [VERIFIED: `saq/worker.py:368` `asyncio.wait_for(asyncio.shield(task), job.timeout)`]

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Kill-on-timeout of essentia child | Agent worker (process pool) | — | CPU work runs only on the agent; the kill must reach the OS child it owns |
| Cap + even-stride windowing | Agent worker (`analysis.py`) | — | Pure compute decision; lives where windows are generated |
| Coverage emit (analyzed/total, sampled) | Agent worker → control API | Control DB (columns) | Worker computes; control persists (worker is Postgres-free, D-25) |
| Set `FileState.ANALYZED` / `ANALYSIS_FAILED` | Control API (`agent_analysis.py`) | — | Only the control plane touches Postgres; worker calls via HTTP |
| Terminal vs transient classification | Agent worker (`functions.py`) | — | Only the worker sees the `TimeoutError` / `ctx["job"]` retry state |
| Enqueue policy (timeout/retries) | Control (enqueue helper) | — | `analysis_enqueue.py` is the single producer seam |

## Standard Stack

### Core (new dependency only — everything else already in repo)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `pebble` | `>=5.2.0` | Killable `ProcessPool` with per-task hard timeout (SIGKILL + worker recycle) | The de-facto pure-Python answer to "`concurrent.futures` can't kill a running child." User-locked candidate in CONTEXT. `ProcessFuture` is `concurrent.futures.Future`-compatible, so it drops into the existing async seam. [VERIFIED: pypi.org/pypi/Pebble/json — v5.2.0, `requires_python >=3.8`] |

**Installation:**
```bash
uv add 'pebble>=5.2.0'
```

**Version verification (run this session):**
```
pebble: PyPI latest = 5.2.0, requires_python = ">=3.8"   [VERIFIED: PyPI JSON API, 2026-06-17]
```
`pebble` is **pure Python** (thin wrappers over stdlib `multiprocessing` + `concurrent.futures`) with no compiled extension, so the `cp314` wheel concern that gates `essentia-tensorflow` does **not** apply — it installs cleanly under Python 3.14 / `uv`. Its trove classifiers list only `Programming Language :: Python :: 3` (no per-minor pins, no `<3.14` cap), so 3.14 is not excluded. [VERIFIED: PyPI JSON classifiers]

### Already present (use as-is)
| Library | Version | Role this phase |
|---------|---------|-----------------|
| `saq[postgres]` | `>=0.26.4` | Job timeout/retry semantics (see Pitfalls 1–2) |
| `alembic` | `>=1.18.4` | Coverage-columns migration (rev `021`) |
| `sqlalchemy` | `>=2.0.51` | `op.add_column` on `analysis`; `FileRecord` state update |
| `pydantic` | (FastAPI dep) | Extend `AnalysisWritePayload`; new failure payload |
| `respx` / `pytest-asyncio` | dev | Mock HTTP + async tests (`asyncio_mode = "auto"`) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `pebble.ProcessPool` | `multiprocessing.Process` + `.terminate()`/SIGKILL wrapper | More code, manual result/exception marshalling + join/kill timing; pebble already does exactly this with a `Future` API. Only fall back if pebble is rejected at slopcheck gate. |
| `pebble.ProcessPool` | `asyncio.create_subprocess_exec` + `proc.kill()` | Requires a separate analysis entry-point CLI + IPC serialization of the (large) result dict; heavier than in-process pool. |

## Package Legitimacy Audit

> slopcheck in this environment did not support `--json` / stdin `scan`; the gate ran degraded. `pebble` was verified directly against the PyPI JSON API and is a long-established package (noxdafox/pebble), but per the provenance rule it is tagged `[ASSUMED]` for the planner to gate behind a verify checkpoint before install.

| Package | Registry | Age | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-------------|-----------|-------------|
| `pebble` | PyPI (v5.2.0) | long-established (noxdafox/pebble) | github.com/noxdafox/pebble | not-run (env lacked `--json`/scan-stdin) | `[ASSUMED]` — planner adds `checkpoint:human-verify` before `uv add` |

**Removed [SLOP]:** none. **Flagged [SUS]:** none (slopcheck unavailable; `pebble` is user-named in CONTEXT, not Claude-invented).

---

## Focus Question Answers

### Q1 — `pebble.ProcessPool` for killable CPU work (cp314 + uv)

**Verified API** [CITED: pythonhosted.org/Pebble/ ProcessPool reference; confirmed `ProcessFuture extends concurrent.futures.Future`]:

```python
from pebble import ProcessPool
from concurrent.futures import TimeoutError as FuturesTimeoutError  # == builtins.TimeoutError on 3.11+

pool = ProcessPool(max_workers=settings.worker_process_pool_size, max_tasks=1)
future = pool.schedule(func, args=(arg1, arg2), timeout=inner_timeout_sec)
# future is a pebble.ProcessFuture (subclass of concurrent.futures.Future)
```

- **(a) per-task hard timeout:** `schedule(func, args=(...), timeout=secs)`. Timeout is per *scheduled task*, exactly what the locked "inner analysis timeout" needs.
- **(b) catch on kill:** `future.result()` raises `concurrent.futures.TimeoutError` (which **is** `builtins.TimeoutError` on Python 3.11+) when the task is killed for exceeding `timeout`. A worker dying *unexpectedly* (e.g. essentia segfault/OOM) raises `pebble.ProcessExpired` instead — catch both. [CITED: pythonhosted.org/Pebble/]
- **(c) actual SIGKILL + recycle:** "once expired it will force the timed out task to be interrupted and the worker will be restarted" — pebble terminates the OS child and spins a fresh worker automatically. Set `max_tasks=1` to also recycle after every successful task (defensive against essentia's ~7 GiB/file leak; see live evidence 28.6 GiB/4 slots). [CITED: pythonhosted.org/Pebble/]

**Keep it async** — pebble exposes a plain `Future`, so do **not** use `loop.run_in_executor` (that needs an `Executor`, not a future). Use `asyncio.wrap_future`:

```python
# pool.py rewrite shape
async def run_in_process_pool(ctx, func, *args, timeout: float | None = None):
    pool: ProcessPool = ctx["process_pool"]
    future = pool.schedule(func, args=args, timeout=timeout)
    return await asyncio.wrap_future(future)   # bridges concurrent.futures.Future → awaitable
```

**Exactly what changes in `pool.py`** vs today [VERIFIED: `src/phaze/tasks/pool.py:11-23`]:

| Today (`ProcessPoolExecutor`) | After (`pebble.ProcessPool`) |
|-------------------------------|------------------------------|
| `create_process_pool()` → `ProcessPoolExecutor(max_workers=N)` | → `ProcessPool(max_workers=N, max_tasks=1)` |
| `await loop.run_in_executor(pool, func, *args)` | `await asyncio.wrap_future(pool.schedule(func, args=args, timeout=t))` |
| No per-task timeout; SAQ-cancel **cannot** reach the child → leaked compute | `timeout=` SIGKILLs + recycles the child; raises catchable `TimeoutError` |
| `pool.shutdown(wait=True)` in `agent_worker.shutdown` | `pool.stop(); pool.join()` (pebble API) — update `agent_worker.py:163` |

Signature change: callers must pass `timeout`. The only production caller is `functions.process_file` (`functions.py:125`); the inner timeout comes from a new config knob (Discretion: name it `analysis_inner_timeout_sec`, default ~6600, i.e. below the 7200 SAQ net). `test_pool.py` constructs `ProcessPoolExecutor` directly and asserts `_max_workers` — it must be rewritten for the pebble pool. [VERIFIED: `tests/test_tasks/test_pool.py`]

`max_tasks` semantics confirmed: ">0 ⇒ each worker restarted after that many tasks." [CITED: pythonhosted.org/Pebble/]

**Fallback if pebble is rejected at the verify checkpoint:** a `multiprocessing.Process` wrapper — run `func` in a child writing its result to a `Queue`; `proc.join(timeout)`; on timeout `proc.kill()` (SIGKILL) + `proc.join()` and raise `TimeoutError`. More code and manual exception marshalling; pebble is strongly preferred.

### Q2 — Cap + even-stride windowing

`_iter_windows(total_sec, win_sec, min_sec, *, drop_short_trailing)` returns the **full** `list[(idx, start, end)]` with `idx` contiguous from 0 [VERIFIED: `analysis.py:364-382`]. Cleanest approach: a pure **post-generation downsample** that preserves the original `idx`, applied inside `_analyze_fine_windows`/`_analyze_coarse_windows` after the `_iter_windows` call (keep `_iter_windows` untouched and side-effect-free for testability):

```python
def _stride_to_cap(windows: list[tuple[int, float, float]], cap: int) -> tuple[list[...], bool]:
    """Even-stride down to <=cap windows, preserving original idx. Returns (kept, sampled)."""
    n = len(windows)
    if cap <= 0 or n <= cap:
        return windows, False
    # endpoints-inclusive even stride: positions 0 .. n-1 mapped across cap picks
    picks = {round(i * (n - 1) / (cap - 1)) for i in range(cap)}  # set dedups collisions
    kept = [windows[p] for p in sorted(picks)]
    return kept, True
```

- **Math:** `round(i*(n-1)/(cap-1))` for `i in 0..cap-1` spans the **whole file** (first and last window always included), satisfying "stride across the WHOLE file, not first-N." Use a `set` to dedup the rare rounding collision (yields ≤ cap, never > cap).
- **`window_index` semantics preserved:** kept tuples keep their original `idx`, stored as `window_index`. Sparklines render correctly: `_build_sparklines` orders fine windows **by `window_index`** and the detail view orders by `(tier, window_index)` [VERIFIED: `proposals.py:269`], so monotonic-with-gaps indices sort fine. Each ribbon's width is its own `(end-start)/total_sec`, so a sampled file's ribbons tile to **<100%** of the bar — a *cosmetic* gap, not a correctness issue (belongs to Phase 44's "sampled" badge; flag, don't fix here).
- **Aggregations hold under sampling:** `aggregate_bpm` (median of BPMs), `aggregate_key`/`aggregate_dominant` (duration-weighted modal), `aggregate_danceability` (mean) are all **order-independent reductions over whatever windows exist** [VERIFIED: `analysis.py:294-335`]. `_representative_features` picks the longest-duration coarse window [VERIFIED: `analysis.py:459-469`] — still valid on a sampled subset. No algorithm needs contiguity (confirmed: only viz consumers).
- **Caps wiring:** add `analysis_fine_cap` (default 60) and `analysis_coarse_cap` (default 30) to `AgentSettings`, mirroring the existing `analysis_fine_window_sec` Field pattern [VERIFIED: `config.py:427-443`]; thread overrides into `analyze_file(...)` like the existing `fine_window_sec`/etc. kwargs.
- **Coverage emit:** `analyze_file` returns `fine_analyzed`/`fine_total`/`coarse_analyzed`/`coarse_total` and `sampled = (fine_sampled or coarse_sampled)`. `*_total` = `len(windows)` **before** strerm (the natural count); `*_analyzed` = count actually analyzed (post-stride, minus per-window skips). Note per-window failure isolation already skips bad windows [VERIFIED: `analysis.py:429,453`], so `analyzed` should count successful appends, not the stride target.

### Q3 — Repo Alembic migration conventions

[VERIFIED: `alembic/` listing + `env.py` + migration 018]

- **Style:** hand-written, sequentially **numbered** files: `018_add_analysis_window_table.py`, `019_…`, `020_add_pipeline_stage_control.py`. Next revision is **`021`**. `revision: str = "021"`, `down_revision = "020"`.
- **Async env:** `env.py` uses `async_engine_from_config` + `connection.run_sync(do_run_migrations)`; `compare_type=True`; URL from `settings.database_url`. Models imported via `from phaze.models import *` so `Base.metadata` is complete (autogenerate is *available*, but the repo hand-writes — follow that).
- **Pattern for the column add** (file `alembic/versions/021_add_analysis_coverage_columns.py`, mirror 018's header/docstring style):

```python
revision: str = "021"
down_revision: str | Sequence[str] | None = "020"

def upgrade() -> None:
    op.add_column("analysis", sa.Column("fine_windows_analyzed", sa.Integer(), nullable=True))
    op.add_column("analysis", sa.Column("fine_windows_total", sa.Integer(), nullable=True))
    op.add_column("analysis", sa.Column("coarse_windows_analyzed", sa.Integer(), nullable=True))
    op.add_column("analysis", sa.Column("coarse_windows_total", sa.Integer(), nullable=True))
    op.add_column("analysis", sa.Column("sampled", sa.Boolean(), nullable=True))

def downgrade() -> None:
    for col in ("sampled", "coarse_windows_total", "coarse_windows_analyzed",
                "fine_windows_total", "fine_windows_analyzed"):
        op.drop_column("analysis", col)
```

- **All nullable** so the existing 79 `analysis` rows (and any pre-fix writes) backfill `NULL` with no data migration. Add matching `Mapped[... | None]` columns to `AnalysisResult` (`models/analysis.py:12`). Column names are Discretion — names above are a suggestion.
- **`FileState.ANALYSIS_FAILED` needs NO migration:** `FileRecord.state` is `String(30)` storing a `StrEnum` value; adding an enum member is code-only [VERIFIED: `models/file.py:20-44,59`].

### Q4 — Worker→control internal API pattern

**Worked example chain** (`put_analysis`) [VERIFIED across `agent_client.py:253`, `routers/agent_analysis.py:68`, `schemas/agent_analysis.py:48`]:

```
process_file (worker)
  → api.put_analysis(file_id, AnalysisWritePayload(...))            # agent_client.py:253
     → PUT /api/internal/agent/analysis/{file_id}  (model_dump(mode="json", exclude_unset=True))
        → put_analysis router handler                               # agent_analysis.py:68
           - auth: Depends(get_authenticated_agent)  (agent_id from token, NEVER body; AUTH-01)
           - pg_insert(...).on_conflict_do_update (field-level LWW via exclude_unset)
           - child windows replaced in same txn (guarded body.windows is not None)
           - session.commit()
```

**Add (a) coverage on the write path — minimal surface:**
1. Add the five coverage fields to `AnalysisWritePayload` (all `… | None = None`) [`schemas/agent_analysis.py:48`].
2. Add their column names to `_ANALYSIS_COLUMN_FIELDS` (`agent_analysis.py:52`) so they land in real columns **instead of** being funneled into the `features` JSONB overflow. (Today only `bpm/musical_key/mood/style/fingerprint/features` are "real"; everything else funnels to JSONB.)
3. Populate them in `process_file` from `analyze_file`'s return.

**Add (b) terminal-failure reporting + set ANALYZED — recommended shape:**
- **Set `FileState.ANALYZED` inside `put_analysis`** on a successful (non-empty) upsert: add an `UPDATE files SET state='analyzed' WHERE id=:file_id` in the same transaction before commit. This also fixes the latent "re-enqueue all 11428" bug (files leave `DISCOVERED`). Note: `put_analysis` does not currently import `FileRecord` — add the import + an `update()` statement. (Empty-body PUT is a no-op today; only advance state when the row is actually written.)
- **Terminal failure = NEW endpoint, not a field on `put_analysis`** (clean separation; `put_analysis` is strictly the success path). Recommend:
  - Client method `report_analysis_failed(file_id, AnalysisFailurePayload(reason, error))` on `PhazeAgentClient`, routed through the existing `_request` tenacity funnel.
  - `POST /api/internal/agent/analysis/{file_id}/failed` → handler sets `FileState.ANALYSIS_FAILED` (auth dep identical to `put_analysis`; `file_id` from path only, AUTH-01).
  - New schema `AnalysisFailurePayload` in `schemas/agent_analysis.py` (`reason: Literal[...]`, `error: str | None`, `extra="forbid"`).
  This is the smallest surface consistent with the existing one-method-per-endpoint convention (D-10) and the Postgres-free worker (D-25).

### Q5 — Where the worker knows a job is terminal

**SAQ enforces the timeout *outside* the task body** [VERIFIED: `saq/worker.py:368`]:
```python
result = await asyncio.wait_for(asyncio.shield(task), job.timeout)   # times out → task.cancel() → raise
```
On timeout SAQ re-raises `asyncio.TimeoutError` (a `builtins.TimeoutError`/`OSError`, **not** `CancelledError` on 3.11+), caught in the worker's `except Exception:` → `if job.retryable: await job.retry(...)` else abort. The cancellation surfaces *inside* `process_file` as a `CancelledError` at the `await` point — **not** a clean, catchable signal. So you **cannot** reliably mark `ANALYSIS_FAILED` off the SAQ-level timeout.

**The fix (locked) makes it catchable:** the **inner pebble timeout fires first** (set below the 7200s SAQ net) and raises `TimeoutError` *synchronously inside* `process_file`. Therefore:

- **TimeoutError = worker-driven terminal.** In `process_file`, wrap the analysis await:
  ```python
  try:
      analysis = await run_in_process_pool(ctx, _load_analyze_file(), path, models, timeout=inner)
  except TimeoutError:
      await api.report_analysis_failed(payload.file_id, AnalysisFailurePayload(reason="timeout"))
      return {"file_id": str(payload.file_id), "status": "analysis_failed"}   # NORMAL return → SAQ COMPLETE → NO retry
  ```
  Returning normally makes SAQ mark the job COMPLETE → no re-run, regardless of `retries`. This satisfies "TimeoutError terminal, do NOT re-run" *independently* of the SAQ retries value. Also catch `pebble.ProcessExpired` (essentia OOM/segfault) the same terminal way.

- **Retries-exhausted detection for *other* (transient) errors:** `ctx["job"]` is available (SAQ injects `context = {**self.context, "job": job}`); the repo already reads `ctx.get("job")` [VERIFIED: `execution.py:383`, `deterministic_key.py:125`]. `job.attempts` is incremented **at dequeue, before** the function runs [VERIFIED: `saq/worker.py` `job.attempts += 1`], and `retryable = retries > attempts` [VERIFIED: `saq/job.py:261-262`]. So inside `process_file`, on a non-timeout exception, check `not ctx["job"].retryable` (or `attempts >= retries`) to know it is the **terminal** attempt → `report_analysis_failed(reason="error")` then re-raise (let SAQ record the abort). On a non-terminal attempt, just re-raise so SAQ retries.

  **SAQ retries arithmetic (critical):** `retries` is *total attempts*, not *extra* retries. `retries=1` ⇒ `1 > 1` (after attempt 1) ⇒ **0 retries**. "Retry once for transient" ⇒ `retries=2` (2 attempts). See Pitfall 1 — and note `retries=1` is also the clobbered SAQ default.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Killing a runaway essentia child | Custom SIGKILL/join/timeout loop | `pebble.ProcessPool(timeout=)` | Race-free terminate + auto worker recycle + `Future` API already solved |
| Awaiting a `concurrent.futures.Future` | Polling / `run_in_executor` over a future | `asyncio.wrap_future(future)` | Stdlib bridge; `run_in_executor` needs an Executor, not a future |
| "Is this the last attempt?" | Counting attempts in Redis/meta | `ctx["job"].retryable` / `.attempts` vs `.retries` | SAQ already tracks it; repo already reads `ctx["job"]` |
| Even sampling preserving index | Re-numbering windows 0..cap | Keep original `idx`, stride positions | Sparklines order by `window_index`; re-numbering corrupts time mapping |

## Runtime State Inventory

> Refactor/behavior-change phase touching live queue + DB. Inventory of runtime state that a code-only change does NOT fix:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `analysis` table: 79 rows have NULL coverage columns after migration; all 11428 `files.state` stuck at `DISCOVERED` (incl. the 79 analyzed) | Coverage backfill is acceptable as NULL (no migration of old rows). State backfill: the 79 analyzed files won't auto-advance — a one-off reconcile (or natural re-analysis) sets them `ANALYZED`. Planner: decide whether to backfill state for the 79 or leave to re-run. |
| Live service config | 11356 `process_file` jobs durable-queued on `phaze-agent-nox` (Postgres broker) carry the OLD `timeout=14400`/`retries=2` baked at enqueue time | New enqueue policy applies only to NEWLY enqueued jobs. The 11356 in-flight jobs keep their old timeout until re-enqueued. Planner: note that a queue purge + re-enqueue (operator step, post-deploy) is needed for the fix to reach the backlog — likely a homelab redeploy prompt, not phase code. |
| OS-registered state | None — worker is a Docker container, no host registrations | None |
| Secrets/env vars | New config knobs (`analysis_*_cap`, `analysis_inner_timeout_sec`) read from `AgentSettings`; no secrets | Set defaults in `config.py`; document env aliases (`PHAZE_ANALYSIS_*`) |
| Build artifacts | `pebble` added to `pyproject.toml` + `uv.lock`; agent Docker image must rebuild to install it | Rebuild agent image on redeploy (Dockerfile already `uv sync`-based) |

## Common Pitfalls

### Pitfall 1: Setting `retries=1` literally → 4 attempts, not 1 (or 0)
**What goes wrong:** `apply_project_job_defaults` (a `before_enqueue` hook) overrides `retries` **only when it equals the SAQ default (1)**, replacing it with `worker_max_retries` (**4**). Setting `retries=1` at enqueue therefore yields **4 attempts** — the opposite of the intent. Separately, SAQ's `retryable = retries > attempts` means even an un-clobbered `retries=1` = **0 retries**.
**Why it happens:** `_SAQ_DEFAULT_RETRIES = 1`; current code deliberately uses `retries=2` *to avoid this clobber* (see the inline comment at `analysis_enqueue.py:83`).
**How to avoid:** To express "one real retry for transient errors," set `retries=2` (2 attempts; `2 != 1` dodges the clobber; matches the existing comment's reasoning). TimeoutError terminality is enforced worker-side (Q5), independent of this value. Surface this to the user — the locked text literally says `retries=1`. [VERIFIED: `queue_defaults.py:57,82-83`; `saq/job.py:261`]
**Warning signs:** A timed-out-then-fixed file still re-runs 3+ times; logs show `attempts=2,3,4`.

### Pitfall 2: Expecting `process_file` to catch the SAQ timeout
**What goes wrong:** Writing `except TimeoutError` around the analysis await and expecting it to fire on the 7200s SAQ limit. It won't — SAQ's `wait_for` lives in the *worker loop*, outside the task; the task only sees a `CancelledError`.
**How to avoid:** Rely on the **inner pebble timeout** (locked) set strictly below the SAQ timeout, so pebble raises a real `TimeoutError` inside the task before SAQ ever cancels. [VERIFIED: `saq/worker.py:355-372`]
**Warning signs:** `ANALYSIS_FAILED` never gets set on long files; worker logs show `CancelledError`/`TimeoutError` at the SAQ layer with no failure-report HTTP call.

### Pitfall 3: Coverage fields silently funneled into `features` JSONB
**What goes wrong:** Adding coverage fields to `AnalysisWritePayload` but forgetting `_ANALYSIS_COLUMN_FIELDS` → the router's overflow funnel dumps them into the `features` JSONB instead of the new columns.
**How to avoid:** Add the new column names to `_ANALYSIS_COLUMN_FIELDS` (`agent_analysis.py:52`) in the same change. [VERIFIED: `agent_analysis.py:111-121`]

### Pitfall 4: pebble pool shutdown API differs from ProcessPoolExecutor
**What goes wrong:** `agent_worker.shutdown` calls `pool.shutdown(wait=True)` (`agent_worker.py:163`) — pebble uses `pool.stop()` + `pool.join()`.
**How to avoid:** Update the shutdown hook when swapping pools. [VERIFIED: `agent_worker.py:161-163`]

## Code Examples

### Inner-timeout-aware `process_file` skeleton (terminal classification)
```python
# functions.py — combines Q1 + Q5
api: PhazeAgentClient = ctx["api_client"]
job = ctx.get("job")
try:
    analysis = await run_in_process_pool(
        ctx, _load_analyze_file(), payload.original_path, payload.models_path,
        timeout=settings.analysis_inner_timeout_sec,
    )
except TimeoutError:                       # pebble per-task kill (inner)
    await api.report_analysis_failed(payload.file_id, AnalysisFailurePayload(reason="timeout"))
    return {"file_id": str(payload.file_id), "status": "analysis_failed"}   # COMPLETE → no retry
except ProcessExpired:                      # essentia OOM / segfault
    await api.report_analysis_failed(payload.file_id, AnalysisFailurePayload(reason="crashed"))
    return {"file_id": str(payload.file_id), "status": "analysis_failed"}
except Exception:
    if job is not None and not job.retryable:    # retries exhausted → terminal
        await api.report_analysis_failed(payload.file_id, AnalysisFailurePayload(reason="error"))
    raise                                        # let SAQ retry (if retryable) or record abort
# ... success path: build AnalysisWritePayload incl. coverage fields, api.put_analysis(...)
```

### pebble pool (`pool.py`)
```python
from pebble import ProcessPool

def create_process_pool() -> ProcessPool:
    return ProcessPool(max_workers=settings.worker_process_pool_size, max_tasks=1)

async def run_in_process_pool(ctx, func, *args, timeout=None):
    pool: ProcessPool = ctx["process_pool"]
    return await asyncio.wrap_future(pool.schedule(func, args=args, timeout=timeout))
```

## Validation Architecture

> `workflow.nyquist_validation` not disabled → section included. Framework: **pytest 9.1 + pytest-asyncio (`asyncio_mode = "auto"`)** + **respx** for HTTP. Min **85% coverage** (CLAUDE.md). All commands `uv run`.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.1.x + pytest-asyncio 1.4 (`asyncio_mode="auto"`) + respx |
| Config file | `pyproject.toml` (`[tool.pytest…]`, `asyncio_mode = "auto"`) |
| Quick run | `uv run pytest tests/test_services/test_analysis.py tests/test_tasks/test_functions.py tests/test_tasks/test_pool.py -x` |
| Full suite | `uv run pytest --cov --cov-report=term-missing` |

### What to measure (sampling/measurement strategy)
essentia is heavy and x86-only — **never run real essentia in unit tests**; mock at the `analyze_file` / process-pool boundary (existing `test_functions.py` already mocks `analyze_file` and the pool via `MagicMock`/`patch`). Measure behaviors at the seams:

| Behavior (REQ) | Measure | Granularity | Test type | Command |
|----------------|---------|-------------|-----------|---------|
| Bounded cost (cap+stride) | `_stride_to_cap`: `len(kept) <= cap`, first & last idx kept, idx preserved, even spacing; `sampled` flag correctness | pure function | unit | `pytest tests/test_services/test_analysis.py -k stride` |
| Aggregations valid under sampling | `aggregate_*` on a strided subset == reduction over that subset | pure function | unit | `pytest tests/test_services/test_analysis.py -k aggregate` |
| Kill-on-timeout actually kills | schedule a real sleeping child via pebble with tiny `timeout`; assert `TimeoutError` AND child PID gone (pool recycled) | process pool (real pebble, fast dummy fn — NOT essentia) | unit/integration | `pytest tests/test_tasks/test_pool.py -k timeout` |
| TimeoutError → terminal, no retry | mock pool to raise `TimeoutError`; assert `report_analysis_failed` called once, return `status="analysis_failed"`, `put_analysis` NOT called | task body | unit (respx/AsyncMock) | `pytest tests/test_tasks/test_functions.py -k timeout` |
| Retries-exhausted → terminal | mock pool to raise generic `Exception`; `ctx["job"].retryable=False` → assert failure reported + re-raised; `retryable=True` → re-raised, NOT reported | task body | unit | `pytest tests/test_tasks/test_functions.py -k retry` |
| State transition ANALYZED | `put_analysis` with a non-empty body sets `files.state='analyzed'` | router + DB | integration | `pytest tests/test_routers -k analysis_state` |
| State transition ANALYSIS_FAILED | new failure endpoint sets `files.state='analysis_failed'`; auth dep enforced | router + DB | integration | `pytest tests/test_routers -k analysis_failed` |
| Coverage columns land in columns (not JSONB) | PUT with coverage fields → columns populated, `features` untouched | router + DB | integration | `pytest tests/test_routers -k coverage` |
| Enqueue policy | `enqueue_process_file` emits `timeout=7200`, chosen `retries`; survives `apply_project_job_defaults` unchanged | enqueue helper | unit | `pytest tests/test_services/test_analysis_enqueue.py` |
| Client method | `report_analysis_failed` PUTs/POSTs correct path/body via respx; 4xx not retried | client | unit | `pytest tests/test_services/test_agent_client_endpoints.py` |

### Sampling rate
- **Per task commit:** the targeted `-k` quick run for the touched seam (< 30s).
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` (full + 85% gate).
- **Phase gate:** full suite green + `pre-commit run --all-files` (ruff/mypy/bandit) before `/gsd:verify-work`. No `--no-verify`.

### Wave 0 gaps
- [ ] `tests/test_services/test_analysis.py` — add `_stride_to_cap` + coverage-emit cases (file exists).
- [ ] `tests/test_tasks/test_pool.py` — **rewrite** for `pebble.ProcessPool` (current asserts `ProcessPoolExecutor._max_workers`); add a real-pebble timeout-kills test with a trivial sleeper fn.
- [ ] `tests/test_tasks/test_functions.py` — add TimeoutError/ProcessExpired/retries-exhausted branches (file exists, mocks established).
- [ ] `tests/test_routers/` — add ANALYZED + ANALYSIS_FAILED + coverage-column integration tests (DB-backed).
- [ ] Framework install: none — pytest stack present; only `pebble` added to deps.

## Security Domain

> `security_enforcement` not disabled → included. New endpoint inherits the established agent-internal controls.

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V4 Access Control | yes | `Depends(get_authenticated_agent)`; `file_id` from **path only**, never body (AUTH-01) — mirror `put_analysis` |
| V5 Input Validation | yes | `AnalysisFailurePayload` with `extra="forbid"`, `reason: Literal[...]`; bound `error` string length |
| V6 Cryptography | no | n/a (no new crypto; bearer token already handled) |

| Pattern | STRIDE | Mitigation |
|---------|--------|------------|
| Forged `file_id`/agent in failure report | Spoofing/Tampering | path-only `file_id`, agent from token (existing pattern) |
| DoS via huge `error` payload | DoS | `max_length` on `error`; `extra="forbid"` |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `pebble` 5.2.0 installs and runs under cp314/uv (pure-Python, no compiled ext) | Standard Stack / Q1 | If a 3.14 incompat surfaces, fall back to the `multiprocessing.Process`+SIGKILL wrapper (Q1 fallback). Verify with `uv add` + a smoke import in CI before building on it. |
| A2 | "Retry once for transient" intends SAQ `retries=2` (CONTEXT literal `retries=1` = 0 retries / clobbered to 4) | Pitfall 1 / Q5 | Wrong value → wasteful re-runs or zero resilience. **User confirmation recommended.** |
| A3 | Leaving the 79 already-analyzed files' `state` at `DISCOVERED` (no state backfill) is acceptable | Runtime State Inventory | If not, planner adds a one-off reconcile task. |
| A4 | Coverage column names/types (`*_windows_analyzed/total` Integer, `sampled` Boolean) | Q3 | Names are Discretion; low risk. |

## Open Questions

1. **`retries=1` literal vs intent (A2).** What we know: SAQ `retries=1` = 0 retries AND is clobbered to 4 by the defaults hook. What's unclear: whether the user wants 0, 1, or "one" retry. Recommendation: use `retries=2` ("one retry"), confirm with user at planning/discuss.
2. **Backlog re-enqueue reach.** What we know: 11356 queued jobs carry the old `timeout=14400`. What's unclear: whether this phase includes the operator purge+re-enqueue or defers to a homelab redeploy prompt. Recommendation: code the policy here; treat backlog re-enqueue as a deploy step (out of phase code).
3. **Inner timeout value.** Default ~6600s (below 7200 SAQ net) is a guess; with caps 60/30 the real per-file cost should be far lower. Recommendation: set generously below the SAQ net; tune after post-deploy re-measure.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| `pebble` | kill-on-timeout pool | ✗ (to add) | 5.2.0 (PyPI) | `multiprocessing.Process`+SIGKILL wrapper |
| `essentia-tensorflow` | analysis (x86 only) | mocked in tests | dev1438 | tests mock the boundary; no real essentia in CI |
| Postgres + SAQ broker | integration tests | present in repo CI | — | — |

**No blocking missing deps** — `pebble` is a normal `uv add`; everything else is in-repo.

## Sources

### Primary (HIGH confidence)
- Repo source (read this session): `services/analysis.py`, `tasks/pool.py`, `tasks/functions.py`, `routers/agent_analysis.py`, `services/agent_client.py`, `schemas/agent_analysis.py`, `models/analysis.py`, `models/file.py`, `services/analysis_enqueue.py`, `tasks/_shared/queue_defaults.py`, `tasks/agent_worker.py`, `config.py`, `alembic/env.py`, `alembic/versions/018_*.py`, `tests/test_tasks/test_pool.py`, `tests/test_tasks/test_functions.py`.
- `.venv/.../saq/worker.py` (timeout/retry loop) and `saq/job.py` (`retryable`, `attempts`, defaults) — installed 0.26.4.
- PyPI JSON API for `Pebble` — v5.2.0, `requires_python >=3.8`, classifiers.
- pythonhosted.org/Pebble/ — ProcessPool `schedule(timeout=)`, `ProcessFuture`, timeout-kills-and-restarts-worker, `max_tasks`, `ProcessExpired`.

### Secondary (MEDIUM)
- `43-CONTEXT.md` + `.planning/debug/analyze-4h-timeouts.md` (locked decisions + root cause).

## Metadata

**Confidence breakdown:**
- pebble API + cp314 fit: HIGH — official docs + PyPI metadata; pure-Python removes the wheel risk (smoke-verify on install per A1).
- Cap+stride / aggregation safety: HIGH — read all aggregation + viz consumers; reductions are order-independent.
- Alembic + worker→control pattern: HIGH — read the exact files and a recent migration.
- SAQ terminal/retry semantics: HIGH — read worker.py + job.py + queue_defaults.py directly (surfaced the `retries=1` clobber + arithmetic).

**Research date:** 2026-06-17
**Valid until:** ~2026-07-17 (stable; re-verify pebble version on `uv add`).
