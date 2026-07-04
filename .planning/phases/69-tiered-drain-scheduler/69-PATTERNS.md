# Phase 69: Tiered Drain Scheduler - Pattern Map

**Mapped:** 2026-07-04
**Files analyzed:** 12 (7 modified source + 1 new source + 4 test suites — 3 new/extended)
**Analogs found:** 12 / 12 (every new/changed file has a concrete in-repo analog; zero new dependencies)

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| **NEW** `src/phaze/services/backend_selection.py` | service (pure, sync) | transform | `src/phaze/tasks/reenqueue.py` `is_domain_completed`/`_natural_id` (pure predicate helpers) | role-match (pure decision fn) |
| **MOD** `src/phaze/tasks/release_awaiting_cloud.py` `stage_cloud_window` | task (cron) | batch / event-driven | itself (Phase-50 drain body) — self-analog | exact (generalize in place) |
| **MOD** `src/phaze/services/backends.py` `resolve_backends` / `KueueBackend.reconcile` | service (protocol impls) | transform / CRUD | itself (Phase-68 protocol module) — self-analog | exact |
| **MOD** `src/phaze/tasks/reconcile_cloud_jobs.py` `_handle_no_callback_terminal` / loop | task (cron) | event-driven / CRUD | itself + `KueueBackend.reconcile` (backends.py:316) | exact |
| **MOD** `src/phaze/tasks/reenqueue.py` `recover_orphaned_work` (AST guard) | task (cron) | batch / transform | itself — `_get_awaiting_cloud_ids` (reenqueue.py:190) | exact (mirror the held-id set) |
| **MOD** `src/phaze/services/pipeline.py` retire `get_cloud_window_count` | service (query) | CRUD (read) | `_BaseBackend.in_flight_count` (backends.py:157) already replaces it | exact (substrate exists) |
| **MOD** `src/phaze/config.py` add `cloud_spill_to_local_after_seconds` | config | config | `cloud_route_threshold_sec` (config.py:562) | exact |
| **MOD (compute spill)** `src/phaze/routers/agent_push.py` / `agent_s3.py` terminal | route (callback) | request-response | `agent_push.py:184-197` / `agent_s3.py:177-183` (self, flip terminal target) | exact |
| **NEW** `tests/analyze/services/test_backend_selection.py` | test (unit, pure) | — | `tests/shared/config/test_cloud_route_threshold.py` (pure, no DB) + `tests/analyze/services/test_backends.py` | role-match |
| **EXT** `tests/analyze/core/test_staging_cron.py` | test (integration) | — | itself (per-backend + overshoot + awaiting-untouched cells) | exact |
| **MOD** `tests/analyze/tasks/test_reconcile_cloud_jobs.py` | test (integration) | — | itself (`test_max_attempts_cap_then_analysis_failed` → spill-back) | exact |
| **EXT** `tests/analyze/tasks/test_recovery.py` | test (integration) | — | itself (`test_held_process_file_row_*` cells) | exact |
| **NEW** config test in `tests/shared/config/` | test (unit, pure) | — | `tests/shared/config/test_cloud_route_threshold.py` | exact |

---

## Pattern Assignments

### NEW `src/phaze/services/backend_selection.py` (service, pure transform)

**Analog:** `src/phaze/tasks/reenqueue.py` (pure predicate helpers `is_domain_completed`, `_natural_id`) — the repo's idiom for a **pure, synchronous, fully-typed decision function with no I/O**, unit-tested with no DB. The exact `select_backend` algorithm is already specified in RESEARCH § "Pattern 2" (lines 149-180); this file is that pure function.

**Module-header + import idiom to copy** (from `release_awaiting_cloud.py:38-53` and `backends.py:36-64`):
```python
from __future__ import annotations
from typing import TYPE_CHECKING
import structlog

if TYPE_CHECKING:
    import datetime
    from phaze.config import ControlSettings
    from phaze.models.file import FileRecord
    from phaze.services.backends import Backend

logger = structlog.get_logger(__name__)
```
- Keep type-only imports under `TYPE_CHECKING` (CLAUDE.md ruff `TCH` idiom; avoids the `backends`↔selection cycle).
- Pure function returns `Backend | None` (mypy strict — full annotation). `None` = "hold this file this tick" (never raise — feeds the cron no-op discipline).

**Local-detection subtlety (RESEARCH Open Q4, line 519):** detect local by `isinstance(b, LocalBackend)` / `kind`, NOT `rank == 99`. Analog: `release_awaiting_cloud.py:104` (`next(b for b in resolve_backends(cfg) if not isinstance(b, LocalBackend))`) and `resolve_backends` at `backends.py:378` (`if not isinstance(backend, LocalBackend)`).

**Snapshot shape** the function consumes (built by the drain, see next file): `dict[str, {"backend": Backend, "available": bool, "remaining": int, "cap": int}]`.

---

### MOD `src/phaze/tasks/release_awaiting_cloud.py` — `stage_cloud_window` (task/cron, batch)

**Analog:** itself — the Phase-50 drain is the exact shape to generalize. The **load-bearing structure is already there**: acquire advisory lock → snapshot count once → FIFO candidate claim → per-candidate `backend.dispatch()` loop → **single** post-loop `commit()`.

**Advisory-lock + single-commit boundary to preserve** (release_awaiting_cloud.py:65, 109-114, 173):
```python
_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY = 5_000_504   # KEEP THIS KEY (RESEARCH Q1: single key, no per-backend keys)

async with ctx["async_session"]() as session:
    await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"),
                          {"lock_key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
    # ... snapshot + candidate loop ...
    await session.commit()   # SINGLE commit; dispatch NEVER commits (releases lock mid-tick = over-stage class)
```

**Change 1 — replace the single-backend pick (lines 102-107)** with the once-per-tick snapshot (RESEARCH Code Examples lines 453-476):
```python
from phaze.services.backends import LocalBackend, resolve_backends   # keep the deferred import (breaks backends↔drain cycle)
backends = resolve_backends(cfg)   # Phase 69: no longer raises on >1 non-local
snapshot = {
    b.id: {"backend": b, "available": await b.is_available(session),
           "remaining": b.cap - await b.in_flight_count(session), "cap": b.cap}
    for b in backends
}   # M probes ONCE per tick — NEVER re-probe inside the candidate loop (Pitfall 1)
```

**Change 2 — replace the `slots`/`get_cloud_window_count` window (lines 128-134)** with per-backend `remaining[]`; candidate limit = `sum(remaining over non-local) + staleness-eligible local headroom`.

**Change 3 — per-candidate selection (replace the single `backend.dispatch` loop at lines 154-172)**:
```python
for file in candidates:
    b = select_backend(file, snapshot, saq_now(), cfg)   # pure, in-memory
    if b is None:
        continue    # clean hold — file stays AWAITING_CLOUD (no state change)
    try:
        dispatched = await b.dispatch(file, session, task_router)
    except NoActiveAgentError:      # KEEP the existing per-file catch (lines 162-168) — cron NEVER raises
        ...
    snapshot[b.id]["remaining"] -= 1     # local decrement per claim
```

**Cron no-op discipline to preserve** (the whole file is built on it): every early return is `{"staged": 0, "skipped": ...}`, never a raise (module docstring lines 20-29; T-50-cron-raise). `select_backend` returning `None` is the new hold path.

---

### MOD `src/phaze/services/backends.py` — `resolve_backends` + `KueueBackend.reconcile` (service)

**Analog:** itself (Phase-68 module).

**Change 1 — remove the >1-non-local boot guard (lines 378-384).** Delete the `if len(non_local) > 1: raise ValueError(...)` block — "multi-backend dispatch lands in Phase 69" is now. Same for the twin guard in `resolved_non_local_kind` (lines 401-406) for the drain path (RESEARCH "Deprecated/obsolete" lines 490-491). Verify no remaining caller depends on the ≤1 invariant (pipeline dashboard / backfill / agent_s3 per WR-01 comment at line 397).

**Change 2 — `KueueBackend.reconcile` (lines 316-358) becomes the shared per-backend reconcile owner** and shares the advisory lock per-row. The backend_id-scoped query is already correct (`CloudJob.backend_id == self.id`, line 336) and the per-row `session.get` + `session.rollback()` guard (lines 347-357) is the exact structure to keep. **Add** the per-row `pg_advisory_xact_lock(5_000_504)` acquisition at the top of each iteration (RESEARCH Code Examples lines 435-450):
```python
for cloud_job_id in cloud_job_ids:
    try:
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                              {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})   # Q1: share the drain's lock, per-row
        cloud_job = await session.get(CloudJob, cloud_job_id)
        ...
        await _reconcile_one(...)   # commits per row → releases the xact lock (per-row granularity is REQUIRED, Pitfall 2)
    except Exception:
        await session.rollback()
        logger.warning(...)
```
- Import the lock key from `release_awaiting_cloud` (already imports `push_file_job_key` from it, line 55 — no new cycle).
- **Do NOT** wrap the whole reconcile tick in one lock (Pitfall 2, RESEARCH lines 412-415): the lock releases on the first per-row commit and breaks the delete-after-record ordering.

**Terminal-behavior change (SCHED-03)** flows through `_reconcile_one`/`_handle_no_callback_terminal` — see next file.

---

### MOD `src/phaze/tasks/reconcile_cloud_jobs.py` — `_handle_no_callback_terminal` (task/cron)

**Analog:** itself. The at-cap terminal today (lines 164-174) marks `ANALYSIS_FAILED`; Phase 69 flips it to **spill-back to `AWAITING_CLOUD`** (Pitfall 3, RESEARCH lines 417-420, 333).

**Current at-cap block to modify (lines 164-174):**
```python
if next_attempt > cap:
    cloud_job.status = CloudJobStatus.FAILED.value
    ...
    await session.execute(update(FileRecord).where(FileRecord.id == file_id)
                          .values(state=FileState.ANALYSIS_FAILED))   # ← Phase 69: → FileState.AWAITING_CLOUD
    await session.commit()
    ...
```
**Phase-69 semantics:** at the *cloud* cap, terminalize the `cloud_job` (FAILED → decrement in-flight) and set the file back to `AWAITING_CLOUD` (re-stamps `updated_at` = fresh staleness clock, RESEARCH lines 268-275). The next tick's `select_backend` sees `attempts >= cap` and routes to local. `ANALYSIS_FAILED` now comes only from local failure or the global ceiling (D-04).

**Delete-after-record ordering to PRESERVE (D-04, lines 168-171):** record + `commit()` BEFORE `delete_staged_object` / `delete_job`. The reconcile-only-decrements invariant (RESEARCH lines 220-237) is the correctness proof the planner lifts — reconcile must never *claim* an in-flight slot.

**Attempt counter reuse:** `cloud_job.attempts` (models/cloud_job.py:87, `Integer server_default="0"`) is the persistent anti-thrash bound; increment site is already here (line 181). Survives `on_conflict_do_update` because `dispatch` keeps the row id (backends.py:257-261).

**Also:** the standalone `reconcile_cloud_jobs` cron loop (lines 282-322) is superseded by the per-backend `for b in resolve_backends(cfg): await b.reconcile(session, ctx)` dispatch (RESEARCH lines 380, 135) — the global un-scoped query at line 297 is the double-owner vector to remove (SCHED-05).

---

### MOD `src/phaze/tasks/reenqueue.py` — `recover_orphaned_work` AST guard (task/cron, SCHED-05)

**Analog:** itself — `_get_awaiting_cloud_ids` (lines 190-199) is the **exact mirror** for the new in-flight-cloud_job exclusion set.

**Add an `in_flight_cloud_job_file_ids` set** (one query, mirroring `_get_awaiting_cloud_ids`):
```python
async def _in_flight_cloud_job_ids(session: AsyncSession) -> set[str]:
    """File-id strings with a live cloud_job row (any backend_id) — owned by backend reconcile/callback, not the ledger."""
    return {str(fid) for fid in (await session.scalars(
        select(CloudJob.file_id).where(CloudJob.status.in_([s.value for s in IN_FLIGHT]))
    )).all()}
```
- `IN_FLIGHT` = `{UPLOADING, UPLOADED, SUBMITTED, RUNNING}` (backends.py:72-77) — import or re-declare the set.

**Exclude those file-ids in the orphan comprehension (line 318):**
```python
in_flight = await _in_flight_cloud_job_ids(session)
orphaned = [r for r in rows if r.key not in live
            and not is_domain_completed(r, done_sets)
            and _natural_id(r) not in in_flight]   # ← NEW: single-owner-per-kind (SCHED-05)
```
**Why:** after Phase-68 BACK-03, a compute file has BOTH an in-flight `cloud_job` row AND a `process_file`/`push_file` ledger row (RESEARCH lines 372-376). Excluding it here keeps the backend reconcile/callback as the single owner — prevents the 44.5k-over-enqueue incident class. **Keep** the existing AWAITING_CLOUD held-file path (lines 335-340) for files with NO in-flight cloud_job.

---

### MOD `src/phaze/services/pipeline.py` — retire `get_cloud_window_count` (service, read)

**Analog / replacement:** `_BaseBackend.in_flight_count` (backends.py:157-169) is the per-backend substrate that already replaces the global window count. `get_cloud_window_count` (pipeline.py:1243-1254, global `COUNT(state IN {PUSHING,PUSHED})`) is retired; the drain reads per-backend `in_flight_count()` from the snapshot instead. **Keep `get_cloud_staging_candidates` (lines 1257-1273) unchanged** — still the FIFO `FOR UPDATE SKIP LOCKED` claim; only its `limit` argument changes (now sum of non-local `remaining`). Grep for other `get_cloud_window_count` callers (dashboard counters) before deleting; convert them to `sum(in_flight_count)` if any remain.

---

### MOD `src/phaze/config.py` — add `cloud_spill_to_local_after_seconds` (config)

**Analog:** `cloud_route_threshold_sec` (config.py:562-568) — copy the bounded-int-Field idiom verbatim.
```python
cloud_spill_to_local_after_seconds: int = Field(
    default=900,               # 15 min (RESEARCH Open Q3, line 517; Claude's discretion D-02)
    gt=0,
    lt=86400,                  # one-day cap, mirrors cloud_route_threshold_sec
    validation_alias=AliasChoices("PHAZE_CLOUD_SPILL_TO_LOCAL_AFTER_SECONDS", "cloud_spill_to_local_after_seconds"),
    description="Seconds a long file waits in AWAITING_CLOUD while higher-rank backends are FULL before slow local becomes an eligible spill target (Phase 69, D-02). Default 900 (15 min); offline backends spill immediately (D-03).",
)
```
- Lives on `ControlSettings` (control plane owns routing). The old global `cloud_max_in_flight` is already retired (config.py:570-575, Phase 67 REG-04) — nothing to remove there; `cap` lives per-backend in `config_backends.py:76`.

---

### MOD `src/phaze/routers/agent_push.py` / `agent_s3.py` — compute failure spill (route/callback, SCHED-03)

**Analog:** the current terminal blocks themselves — `agent_push.py:184-197` (`report_push_mismatch` at cap) and `agent_s3.py:177-183` both do `update(FileRecord)...values(state=FileState.ANALYSIS_FAILED)`. RESEARCH lines 329-334 flag this as the **least-developed path**: a compute push/analysis failure must (i) increment `cloud_job.attempts` and (ii) return the file to `AWAITING_CLOUD` for spillover (mirroring the Kueue reconcile spill), rather than `ANALYSIS_FAILED` directly. Same `update(FileRecord)...values(state=...)` idiom, flipped terminal target + an attempts bump. Planner should treat this as a distinct task (compute uniformity).

---

## Shared Patterns

### Advisory-lock count-and-claim (SCHED-02) — the load-bearing correctness primitive
**Source:** `release_awaiting_cloud.py:65,114,173` — `_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY = 5_000_504`, `pg_advisory_xact_lock(:key)`, single post-loop commit.
**Apply to:** the drain (whole-tick lock, single commit) AND reconcile (`backends.py` `KueueBackend.reconcile` + `reconcile_cloud_jobs.py` — **per-row** lock re-acquired after each commit).
**Key facts:** ONE lock key only (no per-backend keys, no deadlock ordering). Reconcile-only-decrements → single lock is sufficient (RESEARCH lines 220-237). `pg_advisory_xact_lock` auto-releases on every commit — that dictates whole-tick for the drain (one commit) vs per-row for reconcile (many commits).

### "Cron never raises" no-op discipline
**Source:** `release_awaiting_cloud.py:20-29` (module docstring) + per-file `except NoActiveAgentError` (lines 162-168); `backends.py:30-33,296-299` (`is_available` catches any probe failure → `False`); `reconcile_cloud_jobs.py:315-319` (per-row `except Exception: session.rollback()`).
**Apply to:** ALL new selection/spill/black-hole paths. `select_backend` returns `None` (hold) rather than raising; every early return is a clean `{"staged":0,"skipped":...}` / no-op tally.

### Bounded pydantic config-field
**Source:** `config.py:562-568` (`cloud_route_threshold_sec`), `593-599` (`cloud_submit_max_attempts`) — `Field(default=..., gt=0, lt=..., validation_alias=AliasChoices("PHAZE_*", "..."), description=...)`.
**Apply to:** the one new `cloud_spill_to_local_after_seconds` knob. Test analog: `tests/shared/config/test_cloud_route_threshold.py` (default / env-alias / rejects-zero / rejects-too-large — pure, no DB).

### `updated_at` as the staleness "waited-since" signal (ZERO migration)
**Source:** `models/base.py:24-28` — `TimestampMixin.updated_at = mapped_column(server_default=func.now(), onupdate=func.now())`. `onupdate` re-stamps on every UPDATE.
**Apply to:** `select_backend`'s staleness gate: `(now - file.updated_at).total_seconds() >= cfg.cloud_spill_to_local_after_seconds`. RESEARCH Q2 (lines 259-291) verified no non-drain writer touches a parked `AWAITING_CLOUD` row → `updated_at` == entry-time. **Ship the guard test** (`test_staging_cron.py -k awaiting_untouched`, RESEARCH line 559) asserting this invariant. Fallback if it breaks: additive `awaiting_since` column = migration 030 (flag only).

### `cloud_job.attempts` as the anti-thrash bound (ZERO migration)
**Source:** `models/cloud_job.py:87` (`attempts: Integer, server_default="0"`); compared to `cloud_submit_max_attempts` at `reconcile_cloud_jobs.py:164`; survives `on_conflict_do_update` (backends.py:257-261).
**Apply to:** `select_backend` step-2 attempt-exclusion (`attempts >= cfg.cloud_submit_max_attempts` → cloud/kueue-ineligible, local only). **FLAGGED (RESEARCH A1, lines 336-343):** this is a *total-cloud* budget, not literal per-(file,backend) — Reading 1, deferring per-backend scoping to Phase 70. Planner/discuss must confirm.

### Per-row ORM re-fetch under the rollback guard
**Source:** `reconcile_cloud_jobs.py:301-319` and `backends.py:343-357` — capture primitive `cloud_job_ids` first (rollback expires the identity map), then `session.get(CloudJob, id)` fresh inside the loop.
**Apply to:** any per-row reconcile mutation in this phase (the added per-row advisory-lock acquisition slots into the top of this exact loop).

---

## Validation Architecture Map (from RESEARCH § Validation Architecture)

| Test file | New/Extend | Analog to copy structure from |
|-----------|-----------|-------------------------------|
| `tests/analyze/services/test_backend_selection.py` | **NEW** (Wave 0) — pure `select_backend`: rank-first, staleness full/offline, attempt-exclusion, tie-break | `tests/shared/config/test_cloud_route_threshold.py` (pure, no DB) + `tests/analyze/services/test_backends.py` (backend construction helpers `_local()`/`_compute()`/`_kueue()`) |
| `tests/analyze/core/test_staging_cron.py` | **EXTEND** — multi-backend drain, per-backend overshoot (generalize `test_k8s_overlapping_ticks_never_exceed_window`:390), `awaiting_untouched` guard | self (fakes `DedupFakeQueue`, `DedupFakeTaskRouter`, `seed_active_agent`, `fake_local_queue`; helpers `_make_file`/`_patch_settings`/`_make_ctx`) |
| `tests/analyze/tasks/test_reconcile_cloud_jobs.py` | **MODIFY** — `test_max_attempts_cap_then_analysis_failed` (line 338) → assert spill-back to AWAITING_CLOUD; add cap-safe-under-concurrent-drain | self (`_patch_cap`, `_seed`, `_read_cloud_job`, `_read_file` helpers) |
| `tests/analyze/tasks/test_recovery.py` | **EXTEND** — single-owner: compute file with in-flight cloud_job recovered by exactly one path | self (`test_held_process_file_row_*` cells:609-671, `_seed_ledger`/`_patch_inflight`/`_patch_live_keys`) |
| `tests/analyze/services/test_backends.py` | **EXTEND** — reconcile backend_id-scoped; compute rows untouched by kueue reconcile | self (`test_kueue_reconcile_reads_own_backend_rows`:270) |
| config test in `tests/shared/config/` | **NEW** — `cloud_spill_to_local_after_seconds` default/env-alias/bounds | `tests/shared/config/test_cloud_route_threshold.py` (4 cells verbatim shape) |

**Commands (CLAUDE.md — `uv run` mandatory):**
- Per task: `uv run pytest tests/analyze/services/test_backend_selection.py tests/analyze/core/test_staging_cron.py -x`
- Per wave: `just test-bucket analyze`
- Phase gate: `just integration-test` (baseline 2566 passed, 96.89% cov) + 85% floor.

---

## No Analog Found

None. Every changed/new file has a concrete in-repo analog (self-analog for the 7 modified files; `reenqueue.py` pure helpers for the one new pure service; `test_cloud_route_threshold.py` for the new config test). This phase adds **policy over existing substrate** — no new plumbing, zero new dependencies (RESEARCH § Standard Stack).

---

## Metadata

**Analog search scope:** `src/phaze/tasks/` (release_awaiting_cloud, reconcile_cloud_jobs, reenqueue), `src/phaze/services/` (backends, pipeline), `src/phaze/models/` (cloud_job, base), `src/phaze/config.py`, `src/phaze/config_backends.py`, `src/phaze/routers/` (agent_push, agent_s3), `tests/analyze/{core,services,tasks}/`, `tests/shared/config/`.
**Files scanned:** 14 source + 5 test files (targeted grep + full read of the 8 primary analogs).
**Pattern extraction date:** 2026-07-04
