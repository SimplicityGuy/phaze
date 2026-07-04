# Phase 68: Backend Protocol + 3 Implementations - Pattern Map

**Mapped:** 2026-07-03
**Files analyzed:** 11 (4 new, 7 modified)
**Analogs found:** 11 / 11 (every "new" file re-homes an existing body — behavior-preserving refactor)

> **Read-first for the planner.** This phase is a **re-home, not a rewrite**. Every dispatch body
> already exists as an isolated async function; the `Backend` protocol is a thin adapter over them.
> The excerpts below are the exact bodies to lift, with the file + line ranges and the invariants
> (advisory-lock survival, no-commit core, in-txn write ordering, GATE asymmetry) that MUST be
> preserved. Line numbers verified live this session (valid until the branch advances).

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/services/backends.py` *(NEW)* | service (protocol + 3 impls + `resolve_backends`) | dispatch / event-driven | `services/cloud_staging.py` (`_stage_file_to_s3`) + `tasks/push.py` + `tasks/submit_cloud_job.py`/`services/kube_staging.py` + `process_file` | exact (thin adapter over these bodies) |
| `alembic/versions/029_add_cloud_job_backend_id.py` *(NEW)* | migration | DDL (additive) | `alembic/versions/026_add_cloud_job_kube_columns.py` | exact |
| `tests/analyze/services/test_backends.py` *(NEW)* | test (protocol unit + D-02 invariant) | request-response / CRUD | `tests/analyze/core/test_staging_cron.py` + `tests/_queue_fakes.py` + `tests/kube_fakes.py` | exact (same harness) |
| `tests/analyze/core/test_dispatch_snapshot.py` *(NEW)* | test (D-01 golden matrix) | characterization | `tests/analyze/core/test_staging_cron.py` (side-effect assertions) | exact (same harness) |
| `tests/integration/test_migrations/test_migration_029_backend_id.py` *(NEW)* | test (migration) | DDL integration | `tests/integration/test_migrations/test_migration_026_kube_columns.py` | exact |
| `src/phaze/tasks/release_awaiting_cloud.py` *(MOD)* | task (drain cron) | event-driven / batch | *self* — replace the `if/elif` fork with `backend.dispatch()` | in-place refactor |
| `src/phaze/routers/agent_push.py` *(MOD `report_pushed`)* | controller (callback) | request-response | *self* — add compute `cloud_job` terminal write (D-08) | in-place refactor |
| `src/phaze/config.py` *(MOD)* | config | — | *self* — remove 2 accessors, keep+re-tag 3 (D-09), relocate `_single_non_local` raise | in-place refactor |
| `src/phaze/models/cloud_job.py` *(MOD)* | model | — | *self* — add nullable `backend_id`, make `s3_key` nullable (D-08) | in-place refactor |
| `src/phaze/tasks/push.py` *(MOD — body re-homed)* | task (rsync push) | file-I/O / transport | *self* → becomes `ComputeAgentBackend.dispatch` enqueue leg | body re-home |
| `src/phaze/routers/pipeline.py` + `routers/agent_s3.py` + `tasks/controller.py` *(MOD — rewire)* | controller/config readers | — | *self* — rewire `active_cloud_kind` readers to registry-derived (D-09) | in-place refactor |

---

## Pattern Assignments

### `src/phaze/services/backends.py` (NEW — Backend Protocol + 3 impls + `resolve_backends`)

This is the phase's center of gravity. It houses `typing.Protocol` (stdlib, structural typing —
design §4.2 shape) and three impls, each **re-homing** an existing body verbatim.

**Protocol shape** (design §4.2; planner finalizes signatures — from `68-CONTEXT.md` §specifics L237-248):
```python
class Backend(Protocol):
    id: str
    rank: int
    cap: int
    async def is_available(self, ...) -> bool
    async def in_flight_count(self, session, ...) -> int
    async def dispatch(self, file, session, ...) -> None
    async def reconcile(self, ...) -> None
```

**Config the impls bind to** — `src/phaze/config_backends.py:67-133` (Phase-67 discriminated union). Bind
one impl instance per registry entry (Research Pattern 1, per-registry-entry). `LocalBackend`/
`ComputeBackend`/`KueueBackend` pydantic submodels carry `id`/`rank`/`cap`; `ComputeBackend.scratch_dir`
(:89) and `KueueBackend.kube`/`.buckets` (:116-117) are the per-entry config the re-homed bodies read.

**Imports pattern to copy** (from `services/cloud_staging.py:22-49` — the closest stateless-service analog):
```python
from __future__ import annotations
import uuid
from typing import TYPE_CHECKING, cast
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog
from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.services.enqueue_router import select_active_agent
if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from phaze.models.file import FileRecord
    from phaze.services.agent_task_router import AgentTaskRouter
logger = structlog.get_logger(__name__)
```

**`ComputeAgentBackend.dispatch` body** — re-home the enqueue leg from `release_awaiting_cloud.py:176-192`
(the `else` branch), and add the **new** in-txn `cloud_job` write (Pitfall 1 / D-03). The enqueue helper
`_enqueue_push_file` lives at `release_awaiting_cloud.py:82-107`; move it or import it. The `cloud_job`
upsert mirrors the kueue idiom in `cloud_staging.py:104-125`:
```python
# D-03: flip PUSHING + upsert cloud_job in the SAME caller-passed session, BEFORE/WITH the flip.
file.state = FileState.PUSHING
stmt = pg_insert(CloudJob).values(
    id=uuid.uuid4(), file_id=file.id, backend_id=self.id,
    s3_key=None,                                  # compute has no S3 object → s3_key nullable (D-08)
    status=CloudJobStatus.SUBMITTED.value,        # single compute in-flight status (D-10 / Q3)
).on_conflict_do_update(
    index_elements=["file_id"],                   # id OUT of set_ (PK immutable — cloud_staging.py:116-117)
    set_={"backend_id": stmt.excluded.backend_id, "status": stmt.excluded.status},
)
await session.execute(stmt)                        # SAME session; drain's post-loop commit is atomic
job = await _enqueue_push_file(push_queue, file, fileserver_agent.id)  # verbatim
```
> **Never commit inside `dispatch`.** The drain owns the single post-loop `session.commit()`
> (`release_awaiting_cloud.py:193`). A mid-body commit releases `pg_advisory_xact_lock` and re-opens the
> over-stage class (Landmine L1, documented at `cloud_staging.py:71-77`).

**`ComputeAgentBackend.is_available`** — re-home GATE-1 from `release_awaiting_cloud.py:145-150`:
```python
try:
    await select_active_agent(session, kind="compute")
    return True
except NoActiveAgentError:
    return False        # degrade to a hold, NEVER raise (cron no-op discipline)
```

**`KueueBackend.dispatch` body** — call the **no-commit core** `_stage_file_to_s3` verbatim
(`cloud_staging.py:71-148`), exactly as the current kueue branch does at `release_awaiting_cloud.py:180-186`.
It already upserts the `cloud_job` row (`UPLOADING`) + enqueues `s3_upload` in the caller's session.
**D-05: keep single-cluster** — it reads `active_kube`/`active_bucket` via `kube_staging.py:74-89` /
`s3_staging.py`; do NOT parameterize per-cluster (Phase 70).

**`KueueBackend.is_available`** — kube/LocalQueue probe with **NO compute-agent dependency** (D-01a /
Pitfall 10). Re-home the probe from `services/kube_staging.py:250-266` (`get_local_queue`) wrapped so a
`kr8s.NotFoundError`/`ServerError` returns `False` rather than raising (see the controller's non-fatal
catch pattern at `tasks/controller.py:178-185`).

**`KueueBackend.reconcile` body** — re-home `tasks/reconcile_cloud_jobs.py:282-322` (the cron body:
iterate `cloud_job` rows in `{SUBMITTED, RUNNING}`, per-row guard with `session.rollback()`). **Make it
`backend_id`-aware** this phase (add `backend_id` to the row filter), but do NOT add the advisory lock
(Pitfall 2 — Phase 69).

**`LocalBackend`** — `is_available` → always `True`; `dispatch` → the existing `process_file` local path;
`in_flight_count` → 0 (no cloud_job rows) or the local-window count per planner's call.

**`in_flight_count(session)`** (all backends, D-02/D-10) — a pure DB read:
```python
from sqlalchemy import func, select
IN_FLIGHT = (CloudJobStatus.UPLOADING, CloudJobStatus.UPLOADED, CloudJobStatus.SUBMITTED, CloudJobStatus.RUNNING)
count = (await session.execute(
    select(func.count(CloudJob.id)).where(
        CloudJob.backend_id == self.id,
        CloudJob.status.in_([s.value for s in IN_FLIGHT]),
    )
)).scalar() or 0
```
Model the COUNT idiom on `services/pipeline.py:1252-1254` (`get_cloud_window_count`).

**`resolve_backends(settings) -> list[Backend]` + boot guard** — relocate the raise-on-`>1`-non-local
guard from `config.py:463-478` (`_single_non_local`) into this boot function (D-07 fail-fast-at-boot).
Preserve the exact message style (names the offending backend ids). The `cloud_enabled` gate STAYS in
config (`config.py:453-461`).

---

### `alembic/versions/029_add_cloud_job_backend_id.py` (NEW — additive, nullable, no backfill)

**Analog:** `alembic/versions/026_add_cloud_job_kube_columns.py` (verbatim recipe). Head is **028**
(verified `ls alembic/versions/`), so `029` revises `028`.

**Revision header + upgrade/downgrade** (mirror `026_*.py:31-66`):
```python
revision: str = "029"
down_revision: str | Sequence[str] | None = "028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column("cloud_job", sa.Column("backend_id", sa.String(255), nullable=True))
    op.alter_column("cloud_job", "s3_key", nullable=True)   # D-08: compute has no S3 object
    # NO CHECK change (backend_id is free-text/config-derived); NO backfill (D-06)

def downgrade() -> None:
    op.alter_column("cloud_job", "s3_key", nullable=False)  # reverse in reverse order
    op.drop_column("cloud_job", "backend_id")
```
> **CRITICAL (026 banner, :22-25):** touch ONLY `cloud_job`. Never reference `saq_jobs` (SAQ owns it —
> 020 banner). A grep-style test asserts this (see below).
> **Note vs 026:** `backend_id` needs **no** CHECK/enum change (Research "Don't Hand-Roll" — it is a
> plain nullable `String`). The `026` status-CHECK swap dance (:54-57) does NOT apply here; the only
> extra verb vs a pure add-column is the `s3_key` `alter_column` for D-08.

---

### `tests/integration/test_migrations/test_migration_029_backend_id.py` (NEW)

**Analog:** `tests/integration/test_migrations/test_migration_026_kube_columns.py` (verbatim structure).

**Copy these three test shapes:**
1. **Static revision-id assertions, no DB** (`026 test:37-59`): `_load_migration_029()` via
   `importlib.util.spec_from_file_location` (name starts with a digit); assert `revision == "029"`,
   `down_revision == "028"`, `branch_labels is None`.
2. **Grep-assert never touches saq_jobs** (`026 test:62-66`): read the migration file, assert no
   non-comment line contains `saq_jobs`.
3. **Integration body** (`026 test:82-188`): `downgrade_to(cfg, "base")` → `upgrade_to(cfg, "028")` →
   assert `backend_id` absent + `s3_key NOT NULL` → `upgrade_to(cfg, "029")` → assert `backend_id`
   exists + nullable + `s3_key` now nullable (insert a compute-shaped row with `s3_key = NULL`) →
   `downgrade_to(cfg, "028")` → assert `backend_id` gone + `s3_key NOT NULL` again. Reuse the helpers
   `_build_alembic_config`, `downgrade_to`, `upgrade_to`, `MIGRATIONS_TEST_DATABASE_URL`, `_seed_file`
   from `026 test:26-79`.

---

### `tests/analyze/services/test_backends.py` (NEW — protocol unit + D-02 invariant)

**Analog:** `tests/analyze/core/test_staging_cron.py` (the harness) + `tests/_queue_fakes.py` +
`tests/kube_fakes.py`.

**Harness to copy** (`test_staging_cron.py:74-103`):
- `_make_ctx` — `{"async_session": async_sessionmaker(...), "queue": DedupFakeQueue, "task_router": DedupFakeTaskRouter}`.
- `_make_file` — fully-populated `FileRecord` (AWAITING_CLOUD default).
- `_states_for` — re-read persisted state (`session.expire_all()` first).
- `seed_active_agent(session, kind="compute"|"fileserver")` from `tests/_queue_fakes.py:331-353`.
- `DedupFakeQueue`/`DedupFakeTaskRouter` from `tests/_queue_fakes.py:217-280` (models SAQ deterministic-key dedup;
  `.captured` = `(task, payload)` pairs, `.captured_policy` = job-control kwargs).
- S3 SDK stubs — `AsyncMock` on `s3_staging.create_multipart_upload`/`presign_upload_parts`
  (`test_staging_cron.py:261-264`).
- Kube fakes — `fake_job`/`fake_workload`/`fake_local_queue` from `tests/kube_fakes.py:21-73` for
  `KueueBackend.is_available`/`reconcile`.

**Settings stub** — replace `_StubCfg` (`test_staging_cron.py:44-71`) with a `resolve_backends()` fake or
a resolved-`backends` list stub; monkeypatch `phaze.services.backends.get_settings` / `resolve_backends`.

**D-02 equivalence invariant** (`68-RESEARCH.md:242-247`, Layer 2):
```python
per_backend = sum(await b.in_flight_count(session) for b in backends)
window = await get_cloud_window_count(session)     # pipeline.py:1243
assert per_backend == window
```

**Protocol unit cells (≥12, Layer 3):** 3 impls × 4 methods. Key assertions:
- `LocalBackend.is_available` → always True; `ComputeAgentBackend.is_available` → compute-agent heartbeat
  (GATE-1); `KueueBackend.is_available` → kube probe, **no** compute dependency, returns bool never raises.
- `dispatch` D-03 atomicity: no committed in-flight FileState without a live non-terminal `cloud_job` row.
- `in_flight_count` → correct `COUNT(... WHERE backend_id AND status IN in-flight)`.

---

### `tests/analyze/core/test_dispatch_snapshot.py` (NEW — D-01 golden matrix, BACK-04)

**Analog:** `tests/analyze/core/test_staging_cron.py` — the individual side-effect assertions there
(`:149-160` push branch; `:290-297` kueue branch; `:180-182` compute-down no-op; `:273-297`
kueue-skips-GATE-1) are exactly the observations the snapshot aggregates.

**Matrix:** `{compute, kueue, local} × {agent up, agent down}` (`68-CONTEXT.md:250-258`). Per cell, capture
the **ordered side-effect log** (`68-RESEARCH.md:366-382`, Layer 1):
- which gate is **checked vs skipped** — `AsyncMock` on `select_active_agent`; assert compute calls it
  (`kind="compute"`), kueue does NOT (D-01a asymmetry).
- staging call — `_stage_file_to_s3` (kueue) vs `_enqueue_push_file`/`push_file` enqueue (compute).
- FileState transition — `AWAITING_CLOUD → PUSHING`.
- `cloud_job` upsert — present for kueue today; **NEW** for compute.
- enqueue task — `s3_upload` vs `push_file` (dedup no-op counted as skipped).
- tally — `{"staged": N, "skipped": M}`.

**Mechanism:** `unittest.mock.AsyncMock` on `select_active_agent`, `_stage_file_to_s3`, `_enqueue_push_file`,
`task_router.queue_for`; serialize the ordered call log + resulting DB rows to an inline expected-dict per
cell (fixture shape is Claude's Discretion — Research recommends inline expected-dict).
**Matrix truths:** compute+down → `{staged:0}` (GATE-1 holds); kueue+down → proceeds (GATE-1 skipped).

---

### `src/phaze/tasks/release_awaiting_cloud.py` (MOD — `stage_cloud_window`)

**Self-refactor.** Keep the skeleton (advisory lock `:138`, `cloud_enabled` gate `:127-128`, window/slots
math `:153-156`, FIFO SKIP-LOCKED claim `:159`, GATE-2 fileserver `:165-169`, **single post-loop commit**
`:193`). Replace **only**:
- L131 `max_in_flight = cfg.active_cap` → `backend.cap` (resolved backend).
- L145-150 (GATE-1 compute fork) → move into `ComputeAgentBackend.is_available` / backend resolution.
- L176-192 (the per-file `if active_cloud_kind == "kueue" … else …` fork) → `await backend.dispatch(file, session, …)`.
> **D-02a: the drain KEEPS reading `get_cloud_window_count`** (`:153`) for slot math this phase — nothing
> consults per-backend `in_flight_count` for cap consumption yet (that flip is Phase 69 / SCHED-02).
> Preserve the cron no-op discipline: `dispatch`/`is_available` failures degrade to holds, never raise.

---

### `src/phaze/routers/agent_push.py` (MOD — `report_pushed`, D-08)

**Self-refactor.** The current `report_pushed` (`:62-139`) is compute's terminalization path (§4.2:
"compute: existing /pushed + callback path"). Add a **`cloud_job` terminal write** so the D-02 invariant
holds live (D-08). Insert it **inside the existing single committed transaction** — after the guarded
`PUSHING → PUSHED` UPDATE (`:103-108`) and before `session.commit()` (`:131`) — mirroring the WR-02
idempotency guard already there (only write when `res.rowcount != 0`):
```python
# D-08: terminalize compute's cloud_job row in the SAME txn as PUSHING→PUSHED (keeps D-02 invariant true live)
await session.execute(
    update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.SUCCEEDED.value)
)
```
> **Design §5 boundary:** `put_analysis` result return stays untouched — this change is in the `/pushed`
> push-completion callback only (a deliberate minimal nudge, per D-08). `active_compute_scratch_dir`
> read at `:122` STAYS (D-09 retained accessor); only its docstring tag re-points to Phase 70.

---

### `src/phaze/config.py` (MOD — accessor removal + retag, D-07/D-09)

**Self-refactor.** From the live block `config.py:453-533`:
- **Remove** `active_cloud_kind` (`:480-486`) and `active_cap` (`:488-492`) — the two dispatch **selectors**.
- **Keep + re-tag** `active_compute_scratch_dir` (`:494-498`), `active_kube` (`:500-504`), `active_bucket`
  (`:506-523`) — the three config-**value** accessors the verbatim single-cluster bodies read (D-05).
  Their docstrings currently say "removed in Phase 68 (BACK-01)" — **re-point to "Phase 70 (MKUE-01)"**.
- **Keep** `cloud_enabled` (`:453-461`) — the registry on/off gate (structural foundation for BEUI-02).
- **Relocate** the `_single_non_local` raise (`:463-478`) into `resolve_backends()` (D-07 boot guard).

**`active_cloud_kind` reader rewire (D-09, 8 readers / 6 modules)** — resolve through registry-derived
kind instead of the deleted accessor:
| Reader | Line | Rewire to |
|--------|------|-----------|
| `routers/pipeline.py` dashboard `cloud_lane_kind` | `575` | registry-derived kind (`"local"` if not `cloud_enabled` else single non-local kind) |
| `routers/pipeline.py` ledger-seed fork (`== "kueue"`) | `810` | registry-derived kind check |
| `routers/agent_s3.py` defensive guard | `113` | registry-derived kind |
| `tasks/controller.py` LocalQueue-probe gate | `179` | registry-derived kind (keep the try/except degrade at `:178-185`) |
| `tasks/release_awaiting_cloud.py` | `145,180` | subsumed by `backend.dispatch()` refactor above |
> Validation Layer 5 (Q1) guards these rewires don't change the dashboard label / ledger-seed fork /
> agent_s3 guard / controller probe gate behavior.

---

### `src/phaze/models/cloud_job.py` (MOD — add `backend_id`, D-08)

**Self-refactor.** Add the nullable column mirroring the existing optional columns (`:79-91`):
```python
backend_id: Mapped[str | None] = mapped_column(String(255), nullable=True)   # config-derived (D-06)
```
Make `s3_key` nullable (`:74` currently `nullable=False`) — D-08. **No** `CheckConstraint` change
(the status set is unchanged this phase — `:93-102` stays). `CloudJobStatus` in-flight set for
`in_flight_count` = `{UPLOADING, UPLOADED, SUBMITTED, RUNNING}`; terminal = `{SUCCEEDED, FAILED}` (D-10;
confirm against the 6 live members at `:38-46`).

---

## Shared Patterns

### `cloud_job` idempotent upsert (in-txn, no-commit)
**Source:** `src/phaze/services/cloud_staging.py:104-125`
**Apply to:** `ComputeAgentBackend.dispatch` (new row), `report_pushed` (terminal write).
```python
stmt = pg_insert(CloudJob).values(id=uuid.uuid4(), file_id=file.id, ...)   # PK stamped explicitly (CR-01)
stmt = stmt.on_conflict_do_update(index_elements=["file_id"], set_={...})  # id OUT of set_ (PK immutable)
await session.execute(stmt)                                                # caller owns the commit
```
Rule (Pitfall 4 / D-03): the row is written **in the same session and before/with** the `PUSHING` flip,
never after a separate commit.

### Advisory-lock survival / single post-loop commit
**Source:** `src/phaze/tasks/release_awaiting_cloud.py:133-193` + the no-commit core rationale at
`cloud_staging.py:71-77` (Landmine L1).
**Apply to:** every backend `dispatch` — it must NOT commit; the drain's `session.commit()` at
`release_awaiting_cloud.py:193` commits the whole tick atomically so `pg_advisory_xact_lock(5_000_504)`
survives the loop.

### Cron / gate no-op discipline (never raise)
**Source:** `release_awaiting_cloud.py:20-30` (module docstring) + GATE try/except `:145-150,165-169`;
`reconcile_cloud_jobs.py:315-319` (per-row guard).
**Apply to:** `is_available`, `dispatch`, `reconcile` — a failure/absent-agent degrades to a clean hold
(`{"staged":0}`), never a raise.

### GATE asymmetry (D-01a / Pitfall 10)
**Source:** `release_awaiting_cloud.py:140-150` (compute GATE-1) vs `:142-144` comment (kueue skips it).
**Apply to:** `ComputeAgentBackend.is_available` checks the compute agent; `KueueBackend.is_available`
does a kube probe with **no** compute dependency. GATE-2 (fileserver, `:165-169`) stays a
**scheduler-level** precondition for any non-local dispatch — NOT folded into `is_available`.

### Fail-fast boot guard (relocated `>1`-non-local raise)
**Source:** `config.py:463-478` (`_single_non_local`) + the discriminated-union validator style at
`config.py:430-451` and `config_backends.py:91-104,119-125`.
**Apply to:** `resolve_backends()` — raise at boot naming the offending backend ids (D-07).

### Migration: additive nullable + saq_jobs banner
**Source:** `alembic/versions/026_add_cloud_job_kube_columns.py:22-66`.
**Apply to:** `029_*.py` — additive nullable column, reversible, touch ONLY `cloud_job`.

### Test harness (control-shaped ctx + dedup fakes)
**Source:** `tests/analyze/core/test_staging_cron.py:74-103` + `tests/_queue_fakes.py:217-353` +
`tests/kube_fakes.py:21-73`.
**Apply to:** all three new test files.

---

## No Analog Found

None. Every new file re-homes an existing body or mirrors an existing test/migration. The only genuinely
**new artifact** is the compute `cloud_job` row (`tasks/push.py` writes none today — grep-confirmed,
Pitfall 1 premise), but its write idiom is copied verbatim from `cloud_staging.py:104-125`.

---

## Metadata

**Analog search scope:** `src/phaze/{services,tasks,routers,models,config*}`, `alembic/versions/`,
`tests/{analyze,integration,_queue_fakes.py,kube_fakes.py}`.
**Files read this session:** `release_awaiting_cloud.py`, `push.py`, `cloud_job.py`, `cloud_staging.py`,
`config.py:425-539`, `agent_push.py`, `026_*.py` + its test, `test_staging_cron.py`, `pipeline.py:1230-1274`,
`kube_staging.py`, `kube_fakes.py`, `reconcile_cloud_jobs.py:260-322`, `config_backends.py`, `_queue_fakes.py`,
`pipeline.py:568-579/802-819`, `controller.py:172-186`.
**Migration head verified:** `028` (next = `029`).
**Pattern extraction date:** 2026-07-03
</content>
</invoke>
