# Phase 68: Backend Protocol + 3 Implementations - Research

**Researched:** 2026-07-03
**Domain:** Behavior-preserving refactor — collapse the `if active_cloud_kind == compute/kueue` dispatch switch into a `Backend` protocol (`is_available`/`in_flight_count`/`dispatch`/`reconcile`) with Local/Compute/Kueue implementations; add nullable `cloud_job.backend_id`; define+prove uniform per-backend in-flight accounting.
**Confidence:** HIGH — grounded in the live source (all cited files read this session), not training data.

## Summary

Phase 68 is a **re-home, not a rewrite**. Every dispatch body already exists as an isolated async
function: compute push (`tasks/push.py` + the `report_pushed` HTTP callback in `routers/agent_push.py`),
Kueue staging (`services/cloud_staging.py::_stage_file_to_s3`), Kueue submit
(`tasks/submit_cloud_job.py`), Kueue reconcile (`tasks/reconcile_cloud_jobs.py`), and local
(`process_file`). The `Backend` protocol is a thin adapter over these. The Phase-67 transitional shim
(`config.active_cloud_kind`/`active_cap` `@property` accessors, plus the `if/elif` fork inside
`stage_cloud_window`) is the exact seam Phase 68 removes.

**Two live-code facts confirmed that drive the phase:** (1) `tasks/push.py` writes **NO** `cloud_job`
row today — grep-confirmed, the compute row is a brand-new artifact (Pitfall 1 premise, D-02/D-03).
(2) `get_cloud_window_count` (`pipeline.py:1243`) counts `FileRecord.state IN {PUSHING, PUSHED}` — this
stays the drain's slot math (D-02a) and is the RHS of the D-02 equivalence invariant.

**The single sharpest finding the planner must resolve** (details in Open Questions Q1): the CONTEXT
D-07 names only `active_cloud_kind`/`active_cap` for removal, but the live `config.py` tags **five**
`active_*` accessors as "removed in Phase 68 (BACK-01)" — `active_cloud_kind`, `active_cap`,
`active_compute_scratch_dir`, `active_kube`, `active_bucket` — and they are read at **8 call sites
across 6 modules**, three of them (`kube_staging`, `s3_staging`, `agent_push`) being the exact
single-cluster config reads that D-05 says must stay **verbatim** until Phase 70. Removing all five
forces per-backend config plumbing that D-05 explicitly defers. The behavior-preserving reading is:
remove only the two **dispatch selectors** (`active_cloud_kind`/`active_cap`), keep the three
**config-value** accessors as the single-cluster source the re-homed bodies read, and re-tag their
docstrings to Phase 70.

**Primary recommendation:** Create `src/phaze/services/backends.py` housing the `Backend` Protocol,
`LocalBackend`/`ComputeAgentBackend`/`KueueBackend` implementations (each holding its bound
`BackendConfig` submodel — instantiate per-registry-entry), and a `resolve_backends()` boot function
that owns the raise-on-`>1`-non-local guard (relocated from `config._single_non_local`). Re-home the
existing bodies verbatim; add nullable `cloud_job.backend_id` via additive migration `029`; write the
compute `cloud_job` row in-txn before/with the `PUSHING` flip (D-03); assert the D-02 invariant as a
characterization test.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Backend protocol + resolution | Control plane (`services/`) | — | Dispatch orchestration is control-only; kube/S3 creds live control-side (DIST-01) |
| `dispatch()` (state flip + row upsert) | Control plane (drain txn) | — | Owns the FileState mutation and `cloud_job` upsert in ONE caller-passed session (D-03) |
| `is_available()` gate | Control plane | Agent heartbeat / kube probe (read-only) | GATE-1 asymmetry (compute needs agent; kueue skips) lives here |
| `in_flight_count()` | Control plane (Postgres) | — | `COUNT(cloud_job WHERE backend_id AND status IN in-flight)` — a pure DB read |
| `reconcile()` | Control plane cron | kube API / HTTP callback | Kueue: cron poll; compute: existing `/pushed`+callback path |
| Actual byte transport | Agent (fileserver/compute) | — | Unchanged — rsync push / S3 PUT stay agent-side (§5) |
| Migration (`backend_id`) | Alembic (control) | — | Additive DDL, nullable, no backfill (D-06) |

## Standard Stack

Zero new dependencies (locked upstream in 67-CONTEXT). Pure application-code refactor on the pinned
stack. Relevant existing libraries:

| Library | Version (pinned) | Role in this phase |
|---------|------------------|--------------------|
| SQLAlchemy (async) | 2.0.x | `cloud_job` ORM + `pg_insert().on_conflict_do_update` upsert idiom (already used) |
| Alembic | 1.18.x | Additive migration `029_add_cloud_job_backend_id.py` (async env, `compare_type=True`) |
| pydantic v2 | 2.10.x | `BackendConfig` discriminated union (Phase 67, `config_backends.py`) the impls bind to |
| kr8s | 0.20.15 | Kueue submit/reconcile (re-homed verbatim — token hack stays, D-05) |
| pytest + pytest-asyncio | — | `asyncio_mode = "auto"`; existing fakes `tests/_queue_fakes.py`, `tests/kube_fakes.py` |

`typing.Protocol` (stdlib) for the `Backend` seam — structural typing, no runtime cost, matches the
design §4.2 shape.

## Package Legitimacy Audit

**Not applicable.** Phase 68 installs **zero** external packages (confirmed against 67-CONTEXT "zero
new dependencies" lock and the refactor-only scope). No registry verification or slopcheck run required.

## Architecture Patterns

### System Architecture Diagram (dispatch data flow — Phase 68 target)

```
                   stage_cloud_window cron (*/5)  [drain — UNCHANGED skeleton]
                              │
                   pg_advisory_xact_lock(5_000_504)          ← kept (D-02a)
                              │
                   cloud_enabled gate? ──no──▶ {staged:0}    ← config.py:454 KEPT (D-07)
                              │ yes
                   window = get_cloud_window_count()          ← pipeline.py:1243, FileState{PUSHING,PUSHED} (D-02a)
                   slots  = backend.cap - window              ← was active_cap
                              │
                   candidates = FIFO SKIP LOCKED
                              │
                   GATE 2: fileserver agent online? ──no──▶ hold
                              │ yes
                   for file in candidates:
                        backend.dispatch(file, session) ◀──── REPLACES the if/elif fork
                              │
        ┌─────────────────────┼─────────────────────────────┐
        ▼                     ▼                              ▼
  LocalBackend         ComputeAgentBackend             KueueBackend
  (rank 99)            is_available: agent GATE-1       is_available: kube probe, GATE-1 SKIPPED
  always available     dispatch: flip PUSHING +         dispatch: flip PUSHING +
                       write cloud_job(backend_id)      _stage_file_to_s3 (writes cloud_job UPLOADING)
                       IN-TXN (D-03) + enqueue          [verbatim, single-cluster D-05]
                       push_file                        │
                       │                                 ▼
                       ▼                          agent PUT → report_uploaded → PUSHED
                 rsync → report_pushed             → submit_cloud_job → cloud_job SUBMITTED
                 (HTTP) → PUSHED + process_file     → reconcile_cloud_jobs (SUBMITTED→RUNNING→terminal)
                              │
                   session.commit()  [SINGLE post-loop commit — kept]
```

### Recommended Module Placement

```
src/phaze/services/
  backends.py          # NEW: Backend Protocol + 3 impls + resolve_backends() boot guard
  cloud_staging.py     # re-homed as KueueBackend.dispatch body (verbatim, D-05)
  kube_staging.py      # KueueBackend.reconcile helpers (verbatim; token hack stays, D-05)
  s3_staging.py        # KueueBackend S3 leg (verbatim, D-05)
  pipeline.py          # get_cloud_window_count UNCHANGED (D-02a)
```

### Pattern 1: Instantiate backends per-registry-entry (recommended)
**What:** `resolve_backends(settings) -> list[Backend]` builds one impl instance per `BackendConfig`
entry, each holding its bound submodel (`ComputeBackend`/`KueueBackend`/`LocalBackend` + resolved
buckets).
**Why (evidence):** The re-homed staging bodies read per-entry config (`KubeConfig`, `BucketConfig`
from `config_backends.py`). Binding the config to the instance is the natural Phase-69 extension point
(N backends) with zero Phase-68 behavior change. Lazy resolution would re-derive the singleton on every
call and has no advantage while N=1-non-local.
**Caveat (D-05):** In Phase 68 the Kueue `dispatch`/`reconcile` bodies keep calling
`kube_staging`/`s3_staging` which read the **single-cluster** `active_kube`/`active_bucket` accessors.
So the `KueueBackend` instance holds its config but the verbatim bodies still read the singleton — that
is intentional and behavior-preserving (per-entry client plumbing is Phase 70 / MKUE-01). See Q1.

### Pattern 2: `dispatch(file, session)` owns state flip + row upsert in ONE txn (D-03)
**What:** The drain passes its advisory-locked session into `dispatch`; the backend flips
`FileState → PUSHING` **and** upserts the `cloud_job(backend_id)` row in that same session, never a
separate commit. The drain's single post-loop `session.commit()` (release_awaiting_cloud.py:193) commits
both atomically.
**Source:** Mirrors the existing kueue branch — `_stage_file_to_s3` upserts `cloud_job` + mutates state
in the drain's session and relies on the post-loop commit (cloud_staging.py:71-125 is the no-commit
core precisely so the advisory lock survives the loop, Landmine L1).

### Anti-Patterns to Avoid
- **Making `is_available()` uniform** (all backends check "is an agent online"): reintroduces Landmine
  L2 for Kueue — files wedge in `AWAITING_CLOUD` forever. Kueue `is_available` must NOT depend on a
  compute agent (Pitfall 10 / D-01a).
- **Writing the compute `cloud_job` row after a separate commit** from the `PUSHING` flip: Pitfall 4
  limbo — a committed `PUSHING` with no reconcilable row shrinks capacity silently.
- **Letting `is_available()`/`dispatch()` raise** out of the drain: violates the cron no-op discipline
  (`stage_cloud_window` never raises; every early return is a clean hold). Degrade to a hold.
- **Removing all five `active_*` accessors:** forces per-cluster config plumbing D-05 defers to Phase 70
  (Q1).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| `cloud_job` idempotent write | New INSERT logic | `pg_insert(CloudJob).on_conflict_do_update(index_elements=["file_id"])` (cloud_staging.py:104-124, submit_cloud_job.py:79-100) | Unique FK on `file_id`; the upsert idiom (PK stamped OUT of `set_`) is established + tested |
| In-flight counting | FileState scan + new count path in parallel | Single substrate: `COUNT(cloud_job WHERE backend_id AND status IN in-flight)` | Pitfall 1 — two sources double/under-count |
| Advisory-lock serialization | New lock scheme | Existing `pg_advisory_xact_lock(5_000_504)` in the drain | Kept as-is (D-02a); reconcile lock is Phase 69 |
| Additive column migration | Hand-written DDL | `op.add_column(... nullable=True)` mirroring `026_add_cloud_job_kube_columns.py` | Reversible, autogen-compatible, test harness exists |
| String-enum membership | New PG enum type | Extend the `ck_cloud_job_status_enum` CHECK list (no migration needed unless adding a status) | `CloudJobStatus` is string-backed by design |

**Key insight:** `backend_id` needs **no** CHECK/enum change — it's a plain nullable `String`/`UUID`
column. The status set is unchanged this phase.

## Runtime State Inventory

Behavior-preserving refactor with an additive migration. Explicit audit:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `cloud_job` rows: a1/k8s paths **never deployed live** (67-CONTEXT D-11); the only live deploy is all-local (`cloud_enabled=False`) → **~zero** cloud_job rows exist. New `backend_id` column NULL on any stray/terminal rows. | Additive nullable column, **no backfill** (D-06). Verified: nothing live to migrate. |
| Live service config | `backends.toml` (Phase 67) is the sole config surface; the live homelab deploy runs zero-config implicit-local (no `backends.toml`) → `cloud_enabled=False`, drain no-ops before any Phase-68 code path. | None — Phase 68 is invisible to the live all-local deploy. |
| OS-registered state | SAQ CronJobs on the controller (`stage_cloud_window` `*/5`, `reconcile_cloud_jobs` `*/5`, controller.py:280-302). Registered by code, not OS. | None — same cron registration; only the fork *inside* `stage_cloud_window` changes. |
| Secrets/env vars | Per-entry `*_file` TOML secrets (Phase 67) + control-plane env `_FILE` secrets. No secret **names** change. | None — code rename only; `KubeConfig.sa_token`/`BucketConfig.*` reads unchanged. |
| Build artifacts | No compiled artifacts; pure Python. `uv sync` unaffected (no dep change). | None. |

**Canonical question — after every file is updated, what runtime state still holds the old shape?**
Only the SAQ cron *registration* references the task functions; those function names
(`stage_cloud_window`, `reconcile_cloud_jobs`) do NOT change, so no re-registration is needed. The
`cloud_job` table gains a nullable column with no data migration.

## Common Pitfalls

(Full analysis in `.planning/research/PITFALLS.md`; the four that bite in Phase 68:)

### Pitfall 1: Double/under-counting compute in-flight (CONFIRMED premise)
**What goes wrong:** `tasks/push.py` writes no `cloud_job` today — grep-confirmed (it calls
`api.report_pushed()` over HTTP; the row-writing seam is `report_pushed` → `scheduling_ledger` +
`process_file` enqueue, agent_push.py:118-131). Phase 68 adds a compute `cloud_job` row. If ANY count
path reads BOTH the FileState window AND the new row for the same file, a cap-of-2 backend dispatches
3-4.
**How to avoid:** ONE substrate. `in_flight_count(backend)` = `COUNT(cloud_job WHERE backend_id AND
status IN {UPLOADING,UPLOADED,SUBMITTED,RUNNING})`. The drain keeps `get_cloud_window_count`
(FileState) for *slot math* in Phase 68 (D-02a) — nothing consults `in_flight_count` for cap yet, which
is why the double-count can't bite until Phase 69. Assert `sum(in_flight_count) == get_cloud_window_count`
as the characterization proof.
**Warning signs:** the invariant assertion diverges; cap-of-2 dispatches 3.

### Pitfall 4: Dispatch-partial limbo (D-03 write ordering)
**What goes wrong:** The refactor moves the `PUSHING` flip (drain) and the `cloud_job` write (backend
body) into different objects; easy to commit the flip while losing the row → file consumes a slot
forever with no reconcilable row.
**How to avoid:** `dispatch(file, session)` does both in the caller's session; the single post-loop
`commit` (release_awaiting_cloud.py:193) commits both. Verify: no in-flight FileState without a live
non-terminal `cloud_job` row (invariant test).

### Pitfall 10: GATE asymmetry silently lost (D-01a)
**What goes wrong:** Making `is_available()` uniform erases compute's GATE-1 (`select_active_agent(
kind="compute")`, release_awaiting_cloud.py:145-150) vs kueue's deliberate skip (L142-144 comment,
"Landmine L2").
**How to avoid:** Per-kind bodies — `ComputeAgentBackend.is_available` checks the compute agent;
`KueueBackend.is_available` does a kube/LocalQueue probe with **no** compute-agent dependency;
`LocalBackend.is_available` → always True. GATE-2 (fileserver, release_awaiting_cloud.py:165-169) stays
a **scheduler-level** precondition for any non-local dispatch, NOT folded into `is_available`.

### Pitfall 2 (FLAGGED, not fixed here): drain↔reconcile race
`reconcile_cloud_jobs` takes **no** advisory lock (confirmed — only `pg_advisory` call is
release_awaiting_cloud.py:138). Phase 68 makes reconcile `backend_id`-aware but the lock change lands
in Phase 69 (D-02a, deferred). Do NOT attempt the lock here.

## Code Examples

### D-03: compute dispatch writes cloud_job in-txn before/with the PUSHING flip
```python
# Source: pattern from services/cloud_staging.py:104-125 (the kueue upsert the compute path mirrors)
# ComputeAgentBackend.dispatch(file, session, task_router):
file.state = FileState.PUSHING                          # flip (was release_awaiting_cloud.py:179)
stmt = pg_insert(CloudJob).values(
    id=uuid.uuid4(), file_id=file.id, backend_id=self.id,
    s3_key="",                                          # compute has no S3 leg — see Q2 (s3_key NOT NULL today)
    status=CloudJobStatus.SUBMITTED.value,             # or a compute-appropriate in-flight status — Claude's discretion
).on_conflict_do_update(index_elements=["file_id"],
    set_={"backend_id": ..., "status": ...})
await session.execute(stmt)                             # SAME session as the flip; drain's post-loop commit is atomic
job = await _enqueue_push_file(push_queue, file, fileserver_agent.id)  # verbatim
```

### D-02 equivalence invariant (characterization test)
```python
# Assert the new substrate matches the old count for the single-backend case:
per_backend = sum(await b.in_flight_count(session) for b in backends)
window = await get_cloud_window_count(session)          # pipeline.py:1243, FileState{PUSHING,PUSHED}
assert per_backend == window
```

### Migration 029 (additive, nullable, no backfill — mirror 026)
```python
# Source: alembic/versions/026_add_cloud_job_kube_columns.py (verbatim pattern)
revision = "029"; down_revision = "028"                 # 028 is current head (verified)
def upgrade() -> None:
    op.add_column("cloud_job", sa.Column("backend_id", sa.String(255), nullable=True))
    # NO CHECK change (backend_id is free text/config-derived id); NO backfill (D-06)
def downgrade() -> None:
    op.drop_column("cloud_job", "backend_id")
# CRITICAL: touch ONLY cloud_job — never saq_jobs (020 banner).
```

## State of the Art

| Old Approach (post-67) | Phase-68 Approach | Impact |
|------------------------|-------------------|--------|
| `if cfg.active_cloud_kind == compute/kueue` fork in `stage_cloud_window` | `backend.dispatch(file, session)` | The `if/elif` seam removed; per-kind bodies |
| `active_cloud_kind`/`active_cap` `@property` shims | Removed; resolved backend + `backend.cap` | D-07 |
| compute in-flight = FileState window only; NO `cloud_job` row | compute writes a `cloud_job(backend_id)` row (D-03) | Row is new artifact; count substrate unified (proven, not yet consulted for cap) |
| `>1`-non-local raise inside `config._single_non_local` (lazy on accessor read) | Relocated into `resolve_backends()`/boot (D-07) | Fail-fast at boot, not at first accessor read |

**Deprecated/outdated:**
- `.planning/research/SUMMARY.md` §62 ("parameterized `kube_staging` in Phase 68") is **OVERRIDDEN**
  by D-05 — per-cluster work is Phase 70. Do not pull forward.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | In-flight (non-terminal) `CloudJobStatus` set = {UPLOADING, UPLOADED, SUBMITTED, RUNNING}; terminal = {SUCCEEDED, FAILED} | Pitfall 1 / Q2 | Wrong set breaks the D-02 invariant. `[ASSUMED]` from the StrEnum semantics — this is explicitly Claude's Discretion in CONTEXT; planner/user confirms |
| A2 | Only `active_cloud_kind`/`active_cap` should be removed in Phase 68; the three config-value accessors stay to Phase 70 | Q1 | If planner removes all five, D-05 "verbatim single-cluster" is violated (needs per-entry plumbing). Flagged as the top contradiction |
| A3 | Compute `cloud_job` needs a terminalization seam to keep the invariant true in production | Q2 | If left un-terminalized, prod `in_flight_count` diverges (harmless in 68 since not consulted, but tech-debt to 69) |
| A4 | Migration head is `028`; next is `029` | Migration | Verified via `ls alembic/versions/` — LOW risk |

## Open Questions (RESOLVED 2026-07-03 at plan-time — see 68-CONTEXT.md "Plan-time resolutions")

> **RESOLVED:** Q1 → **D-09**, Q2 → **D-08**, Q3 → **D-10** (68-CONTEXT.md). The plans implement all
> three. Retained below for the reasoning that led to each resolution.

### Q1 — Which `active_*` accessors does Phase 68 remove? (TOP contradiction) — RESOLVED: D-09 (remove only the 2 dispatch selectors; keep+re-tag the 3 config-value accessors to Phase 70)
- **What we know:** D-07 names `active_cloud_kind` (config.py:481) + `active_cap` (config.py:489). But
  `config.py` tags **five** accessors "removed in Phase 68 (BACK-01)": also `active_compute_scratch_dir`
  (:495, read at agent_push.py:122), `active_kube` (:501, read at kube_staging.py:84), `active_bucket`
  (:507, read at s3_staging.py:78). Full reader list:
  - `active_cloud_kind`: release_awaiting_cloud.py:145,180; routers/pipeline.py:575,810; routers/agent_s3.py:113; tasks/controller.py:179
  - `active_cap`: release_awaiting_cloud.py:131
  - `active_compute_scratch_dir`: routers/agent_push.py:122
  - `active_kube`: services/kube_staging.py:84
  - `active_bucket`: services/s3_staging.py:78
- **What's unclear:** D-05 says `kube_staging`/`s3_staging` stay **verbatim single-cluster** until
  Phase 70. Removing `active_kube`/`active_bucket` requires those bodies to take per-entry config — the
  exact parameterization D-05 defers.
- **Recommendation:** Remove ONLY the two **dispatch selectors** (`active_cloud_kind`, `active_cap`) —
  the protocol replaces them. **Keep** `active_kube`/`active_bucket`/`active_compute_scratch_dir` as the
  single-cluster config source the verbatim bodies read, and re-tag their docstrings "Phase 70 (MKUE-01)".
  This is behavior-preserving and honors D-05. **BUT** all `active_cloud_kind` readers outside the drain
  (pipeline.py:575 dashboard `cloud_lane_kind`, pipeline.py:810 kueue-vs-compute ledger-seed fork,
  agent_s3.py:113 defensive guard, controller.py:179 LocalQueue-probe gate) must be rewired to a
  registry-derived read (e.g. a `backend.kind` lookup or a kept `resolved_backend_kind` helper) — this is
  real Wave work, not just deleting two properties. **Planner must confirm scope and resolve the
  config.py docstring/D-07 discrepancy.**

### Q2 — How is the compute `cloud_job` row terminalized? (sharpest correctness edge) — RESOLVED: D-08 (write + terminalize live: nullable s3_key, dispatch writes row in-txn, report_pushed callback terminalizes)
- **What we know:** Kueue `cloud_job` is terminalized by `reconcile_cloud_jobs` (→SUCCEEDED) and by
  `report_uploaded`/submit chain. Compute has **no** `cloud_job` today; its FileState leaves the window
  via `put_analysis` (→ANALYZED) or `report_analysis_failed`. Design §5 says `put_analysis` is
  **untouched** this phase.
- **What's unclear:** If the new compute `cloud_job` row is written at dispatch but never terminalized,
  `in_flight_count(compute)` grows unbounded and diverges from `get_cloud_window_count` in production.
  In Phase 68 this is *harmless* (D-02a: nothing consults per-backend count for cap) but it's a limbo
  landmine for Phase 69.
- **Also unclear:** `cloud_job.s3_key` is `NOT NULL` today (cloud_job.py:74). A compute row has no S3
  object. Planner must decide: sentinel `""`, make `s3_key` nullable in migration 029, or a compute-
  specific key. This is a schema decision the migration touches.
- **Recommendation:** Planner chooses one of: (a) add a compute terminalization hook in
  `ComputeAgentBackend.reconcile()` / the `report_pushed`→analysis-complete seam (may nudge the §5
  "untouched" boundary — flag to user), or (b) scope the D-02 invariant as a **characterization-test-only**
  assertion over constructed states in Phase 68 and explicitly defer production terminalization wiring to
  Phase 69 with a documented `truth`. Option (b) is more faithful to "lay + prove, don't flip" (D-02).

### Q3 — In-flight `CloudJobStatus` set (Claude's Discretion) — RESOLVED: D-10 ({UPLOADING, UPLOADED, SUBMITTED, RUNNING})
Recommend non-terminal = {UPLOADING, UPLOADED, SUBMITTED, RUNNING}; terminal = {SUCCEEDED, FAILED}.
For compute rows (no S3/submit lifecycle), pick a single in-flight status (e.g. SUBMITTED or RUNNING)
that the terminalization seam from Q2 clears. Confirm at plan-time.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (test) | Migration integration test (`phaze_migrations_test` DB) | via `just test-db` (port 5433) | 16+ | Static revision-id assertions run WITHOUT a DB (mirrors test_migration_026) |
| uv | All commands | ✓ (project constraint) | — | — |
| kr8s | Kueue reconcile re-home (verbatim) | ✓ pinned | 0.20.15 | fakes in `tests/kube_fakes.py` |

No new external dependencies. No missing blocking dependencies.

## Validation Architecture

Test framework confirmed: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`, `testpaths = ["tests"]`).

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (asyncio_mode=auto) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/analyze/ tests/shared/ -x` |
| Full suite command | `uv run pytest` (or `just test` / `just test-cov`) |
| Migration integration | `just integration-test` (needs `just test-db` → `phaze_migrations_test`) |

### Existing harness to reuse (mocking boundaries)
- **`tests/_queue_fakes.py`** — `DedupFakeQueue`, `DedupFakeTaskRouter` (models SAQ deterministic-key
  dedup), `seed_active_agent`. This is how `stage_cloud_window` is tested today
  (`tests/analyze/core/test_staging_cron.py`).
- **`tests/kube_fakes.py`** — `fake_job`, `fake_workload`, `fake_local_queue` for kube reconcile.
- **`get_settings` monkeypatch** — existing tests pin a `_StubCfg` exposing `active_cap`,
  `cloud_enabled`, `active_cloud_kind` (test_staging_cron.py:44-71). Phase 68 tests substitute a
  resolved-`backends` stub / `resolve_backends()` fake.
- **`async_sessionmaker` on the test engine** — `ctx = {"async_session": sm, "queue": ..., "task_router": ...}`.

### Layers for a behavior-preserving refactor

**Layer 1 — Golden characterization snapshot (D-01, the acceptance gate) → BACK-04**
Record the observable side-effect sequence over the **{compute, kueue, local} × {agent up, agent down}**
matrix on today's post-67 code, then assert unchanged after the refactor. Observable side effects to
capture per cell:
- which agent gate is **checked vs deliberately skipped** (compute: `select_active_agent(kind="compute")`
  called; kueue: NOT called — D-01a asymmetry, capturable by asserting the `select_active_agent` mock's
  call args)
- the **staging call** made (`_stage_file_to_s3` for kueue vs `_enqueue_push_file` for compute)
- the **FileState transition** (`AWAITING_CLOUD → PUSHING`)
- the **`cloud_job` upsert** (present for kueue today; NEW for compute)
- the **enqueue** (`s3_upload` vs `push_file`; dedup no-op counted as skipped)
- the **tally** returned (`{"staged": N, "skipped": M}`)
Mechanism: `unittest.mock.AsyncMock` on `select_active_agent`, `_stage_file_to_s3`, `_enqueue_push_file`,
`task_router.queue_for`; serialize the ordered call log + resulting DB FileState/cloud_job rows to a
snapshot fixture (JSON or an inline expected dict). Feasible in the existing harness — test_staging_cron
already asserts these individually.
Matrix truths: compute+down → `{staged:0}` no-op (GATE-1); kueue+down → proceeds (GATE-1 skipped).

**Layer 2 — Equivalence invariant (D-02) → BACK-03**
`sum(in_flight_count(b)) == get_cloud_window_count()` for the single-backend case. Assert over
constructed FileState/`cloud_job` states. In-flight status set = {UPLOADING,UPLOADED,SUBMITTED,RUNNING}
(Q3). Guards against Pitfall 1 double-count. (Note Q2: decide whether this is prod-live or
characterization-only in Phase 68.)

**Layer 3 — Per-backend protocol unit tests (≥12 cells) → BACK-01/BACK-02/BACK-03**
Each of the 3 impls × 4 methods (`is_available`/`in_flight_count`/`dispatch`/`reconcile`):
- `LocalBackend.is_available` → always True; `ComputeAgentBackend.is_available` → compute-agent
  heartbeat (GATE-1); `KueueBackend.is_available` → kube probe, no compute dependency, returns bool
  never raises (Pitfall 8 posture even though multi-Kueue is Phase 70).
- `dispatch` D-03 atomicity: kill/rollback between flip and row-write → assert no limbo row (FileState
  in-flight ⟺ live non-terminal `cloud_job`).
- `in_flight_count` → correct `COUNT(... WHERE backend_id AND status IN in-flight)`.

**Layer 4 — Migration test (029) → BACK-02**
Mirror `tests/integration/test_migrations/test_migration_026_kube_columns.py`: static revision-id/
down-revision assertions WITHOUT a DB (additive-only, bare-number `029`, revises `028`); integration
body upgrades 028→029, asserts `backend_id` column exists + nullable + no backfill, downgrades and
asserts it's gone. Grep-assert the migration never references `saq_jobs`.

**Layer 5 — Call-site rewire regression (Q1)** → guards that removing `active_cloud_kind` doesn't
change the dashboard `cloud_lane_kind`, the pipeline ledger-seed fork (pipeline.py:810), the agent_s3
guard, and the controller LocalQueue-probe gate.

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Command | File Exists? |
|-----|----------|-----------|---------|-------------|
| BACK-01 | Backend protocol + 3 impls replace if/elif | unit | `uv run pytest tests/analyze/services/test_backends.py -x` | ❌ Wave 0 |
| BACK-02 | nullable `backend_id` additive migration | migration | `uv run pytest tests/integration/test_migrations/test_migration_029_backend_id.py` | ❌ Wave 0 |
| BACK-03 | uniform `in_flight_count`; D-02 invariant | unit | `uv run pytest tests/analyze/services/test_backends.py::test_in_flight_equivalence` | ❌ Wave 0 |
| BACK-04 | golden side-effect snapshot unchanged (D-01) | characterization | `uv run pytest tests/analyze/core/test_dispatch_snapshot.py -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/analyze/ tests/shared/ -x`
- **Per wave merge:** `uv run pytest` (full suite; note colima flake recipe — re-run failed subset in
  isolation, do NOT set `PHAZE_QUEUE_URL=redis`)
- **Phase gate:** full suite green + `just integration-test` (migration) before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/analyze/services/test_backends.py` — protocol unit tests (Layer 3) + invariant (Layer 2)
- [ ] `tests/analyze/core/test_dispatch_snapshot.py` — golden matrix (Layer 1) covering BACK-04
- [ ] `tests/integration/test_migrations/test_migration_029_backend_id.py` — migration (Layer 4)
- [ ] Snapshot fixture shape/serialization (Claude's Discretion — recommend inline expected-dict per cell)
- [ ] Framework install: none — pytest/pytest-asyncio already present.

## Security Domain

`security_enforcement` not explicitly false → enabled. This phase is control-plane refactor with no new
external input surface.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | No new auth surface; agent callbacks unchanged (§5) |
| V4 Access Control | no | DIST-01 preserved — control plane stays sole S3 importer/presigner; pods/agents credential-free |
| V5 Input Validation | minor | `backend_id` is a config-derived server value, never untrusted input |
| V6 Cryptography | no | kube SA token / S3 creds are `SecretStr`; token hack left as-is (D-05), never logged |
| V7 Logging | yes | `log_effective_registry` logs only `{id,kind,rank,cap}` — never SecretStr / `*_file` paths (config.py:525-533) |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Secret material in resolved-registry logs | Information Disclosure | Log id/kind/rank/cap projection only (already implemented) |
| Backend raw kube/S3 error surfaced to UI | Information Disclosure | Sanitize backend error surfacing; attribute to entry `id` (relevant Phase 71 UI; keep discipline now) |
| DIST-01 media-plane boundary | Elevation of Privilege | Unchanged — no pod/agent gains bucket creds; presigned URLs only |

## Sources

### Primary (HIGH confidence — read directly this session)
- `src/phaze/tasks/release_awaiting_cloud.py` (drain, advisory lock, GATE-1/2, if/elif fork, single commit)
- `src/phaze/tasks/push.py` (compute rsync — CONFIRMED writes no cloud_job)
- `src/phaze/routers/agent_push.py` (report_pushed → scheduling_ledger + process_file; active_compute_scratch_dir:122)
- `src/phaze/models/cloud_job.py` (CloudJob, CloudJobStatus 6 members, unique FK, string CHECK, s3_key NOT NULL)
- `src/phaze/services/cloud_staging.py` (`_stage_file_to_s3` upsert idiom, no-commit core L1)
- `src/phaze/services/pipeline.py:1243` (get_cloud_window_count — FileState{PUSHING,PUSHED})
- `src/phaze/services/s3_staging.py:78` (active_bucket), `src/phaze/services/kube_staging.py:84,92-111` (active_kube + token hack)
- `src/phaze/tasks/submit_cloud_job.py`, `src/phaze/tasks/reconcile_cloud_jobs.py` (no advisory lock; SUBMITTED/RUNNING iteration)
- `src/phaze/config.py:430-533` (cloud_enabled, _single_non_local raise, five active_* accessors, log_effective_registry)
- `src/phaze/config_backends.py` (Backend/Kube/Bucket submodels the impls bind to)
- `src/phaze/routers/pipeline.py:575,810`, `src/phaze/routers/agent_s3.py:113`, `src/phaze/tasks/controller.py:179,280-302` (active_cloud_kind readers + cron registration)
- `alembic/env.py`, `alembic/versions/026_*.py`, `tests/integration/test_migrations/test_migration_026_kube_columns.py` (migration recipe + test pattern)
- `tests/analyze/core/test_staging_cron.py`, `tests/_queue_fakes.py`, `tests/kube_fakes.py` (test harness)
- `.planning/phases/68-backend-protocol-3-implementations/68-CONTEXT.md` (D-01..D-07 — authoritative)
- `.planning/phases/67-backend-registry-config-model/67-CONTEXT.md` (D-11..D-14 no-back-compat pivot)
- `.planning/research/PITFALLS.md` (Pitfalls 1,2,4,10)
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` §4.2/4.4/4.5/5

### Secondary
- `.planning/research/SUMMARY.md` §51-63 (integration map; §62 OVERRIDDEN by D-05)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — zero new deps, all libraries pinned + in-use
- Architecture (protocol placement, re-home seams): HIGH — every body located + read
- Migration recipe: HIGH — verified head=028, 026 pattern + test harness exist
- Pitfalls: HIGH — grounded in PITFALLS.md + confirmed against live code
- Compute cloud_job terminalization (Q2) & accessor-removal scope (Q1): MEDIUM — genuine CONTEXT vs
  live-code tensions the planner must resolve (not research gaps — flagged explicitly)

**Research date:** 2026-07-03
**Valid until:** 2026-08-02 (stable internal codebase; re-verify line numbers if the branch advances)
