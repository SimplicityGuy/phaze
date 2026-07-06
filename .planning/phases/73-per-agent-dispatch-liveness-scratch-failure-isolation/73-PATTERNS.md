# Phase 73: Per-Agent Dispatch, Liveness, Scratch & Failure Isolation - Pattern Map

**Mapped:** 2026-07-05
**Files analyzed:** 11 (6 source modified, 1 source deletion, 4 test surfaces)
**Analogs found:** 11 / 11 (every change has a verbatim in-repo twin — this phase is the deliberate compute-side mirror of the shipped Phase-70 multi-Kueue work)

> **Prime directive for the planner:** This is a *rewiring*, not new construction. Every Phase-73 change has a **verbatim Phase-70 `KueueBackend` / `s3_staging` analog already in `src/phaze/services/backends.py`**. Copy the twin's structure (per-entry binding recorded at construction → stamp the destination on the dispatch record → downstream readers READ the recorded value → per-backend `WHERE backend_id == self.id` scoping → per-backend snapshot try/except isolation). Diverge only where the transport differs (rsync-push host/scratch_dir vs S3 `staging_bucket`).

---

## File Classification

| Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---------------|------|-----------|----------------|---------------|
| `src/phaze/services/backends.py` (`ComputeAgentBackend.dispatch` + `_enqueue_push_file`) | service | event-driven (dispatch stamp) | `KueueBackend.dispatch` + `resolve_bucket_config`, same file | **exact twin** |
| `src/phaze/routers/agent_push.py` (`/pushed`, `/mismatch`) | route/controller | request-response (callback) | `KueueBackend.reconcile` scoping (backend_id) + existing `/pushed` WR-02 guard | role + flow match |
| `src/phaze/tasks/push.py` (`_build_rsync_argv`, `_require_push_config`) | task/utility | file-I/O (rsync transform) | itself (payload-read swap); no cross-file analog needed | self / role-match |
| `src/phaze/schemas/agent_tasks.py` (`PushFilePayload`) | model | transform (validation) | `PushFilePayload`'s own `original_path`/`file_type` validators | **exact (same model)** |
| `src/phaze/config_backends.py` (`ComputeBackend`) | config | transform (validation) | `ComputeBackend._require_dispatch_fields` (same model) + `KueueBackend._require_kube` | **exact (same model)** |
| `src/phaze/config.py` (`active_compute_scratch_dir`) | config | — (property DELETION) | Phase-70 retired `active_kube` / `active_bucket` module-globals (same file, L503-510) | **exact precedent** |
| `src/phaze/tasks/release_awaiting_cloud.py` | task | batch (drain tick) | **unchanged — regression-test surface only** (D-08) | reuse-as-is |
| `tests/analyze/services/test_backends.py` | test | — | `test_kueue_reconcile_scope_ignores_other_backend_rows` (L704) | exact twin |
| `tests/agents/routers/test_agent_push.py` | test | — | `test_pushed_transitions_...` (L223) + `_patch_settings` (L136) | extend |
| `tests/analyze/core/test_push_pipeline.py` | test | — | `test_rsync_argv_remote_dest_is_file_id_not_filename` (L137) | extend |
| `tests/analyze/services/test_backend_selection.py` | test | — | `test_spill_when_lowest_rank_full_picks_next_rank` (L95) | extend |
| `tests/analyze/services/test_compute_binding_golden.py` | test | — | `test_single_compute_registry_resolution_is_byte_identical` (L64) | extend |

---

## Pattern Assignments

### `src/phaze/services/backends.py` — `ComputeAgentBackend.dispatch` + `_enqueue_push_file` (service, event-driven)

**Analog: `KueueBackend.dispatch` (backends.py:365-402) — the verbatim stamp twin.**

Kueue stamps `staging_bucket` on the `cloud_job` upsert; compute must stamp the `dest_*` destination onto the `PushFilePayload`. `self.config` IS the `ComputeBackend` submodel (bound at construction in `resolve_backends`), so `dispatch` already has `scratch_dir` / `agent_ref` / the new host field in hand — **no re-lookup at the dispatch site** (this is the record-don't-rederive origin point).

**The Kueue stamp to mirror** (backends.py:399-401):
```python
# Record backend_id + the D-06 staging_bucket in the SAME uncommitted session (MKUE-02/D-01):
# in_flight_count is backend_id-scoped, and presign/cleanup read staging_bucket authoritatively.
await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(backend_id=self.id, staging_bucket=bucket_id))
```

**Current compute dispatch (backends.py:280-316)** already writes `cloud_job(backend_id=self.id)` via `pg_insert(...).on_conflict_do_update(index_elements=["file_id"], ...)` (D-05 one-row-per-file re-upsert on spill — no schema change). The change: pass the destination into `_enqueue_push_file`.

**Current `_enqueue_push_file` payload build (backends.py:95-100) — the stamp site:**
```python
payload = PushFilePayload(
    file_id=file.id,
    original_path=file.original_path,
    file_type=file.file_type,
    agent_id=agent_id,
)
```
→ D-02: extend the call so `dispatch` passes `self.config`'s `scratch_dir` + push-host (+ optional `ssh_user`) and `_enqueue_push_file` stamps them onto the payload as `dest_scratch_dir` / `dest_host` / `dest_ssh_user`. `dispatch` (L313) already calls `_enqueue_push_file(push_queue, file, fileserver_agent.id)` — widen its signature to carry the destination (the `ComputeBackend` config or the three resolved values).

---

### `src/phaze/schemas/agent_tasks.py` — `PushFilePayload` (model, transform)

**Analog: `PushFilePayload`'s own existing validators (agent_tasks.py:72-84) — mirror them for the new fields.**

**Existing model + validator idiom to copy (agent_tasks.py:62-84):**
```python
model_config = ConfigDict(extra="forbid")

file_id: uuid.UUID
original_path: str
file_type: str
agent_id: str

@field_validator("original_path")
@classmethod
def _original_path_absolute(cls, v: str) -> str:
    if not v.startswith("/"):
        raise ValueError("original_path must be an absolute path")
    return v

@field_validator("file_type")
@classmethod
def _file_type_alnum(cls, v: str) -> str:
    if not v.isalnum():
        raise ValueError("file_type must be alphanumeric ([A-Za-z0-9]+)")
    return v
```

**Add (discretion: flat fields, per RESEARCH recommendation):**
- `dest_host: str` — validator rejecting shell metachars / whitespace (it lands in the `ssh` remote spec).
- `dest_scratch_dir: str` — `_dest_scratch_absolute` validator identical in shape to `_original_path_absolute` (must start with `/`).
- `dest_ssh_user: str | None = None` — optional; when `None`, `_build_rsync_argv` falls back to `cfg.push_ssh_user` (preserves ≤1-compute behavior byte-identical, RESEARCH Open-Q2/A3).

**Critical:** keep `extra="forbid"`. Defaults must preserve the *five-field* bulk producers elsewhere (mirrors the `ProcessFilePayload` `expected_sha256=None`/`scratch_path=None` default-preservation note at agent_tasks.py:44-51). Since `dest_host`/`dest_scratch_dir` are **required** for a push, confirm both build sites (`_enqueue_push_file` AND the `/mismatch` re-drive — see Shared Pattern "Landmine 1") always supply them.

---

### `src/phaze/tasks/push.py` — `_build_rsync_argv` + `_require_push_config` (task, file-I/O)

**Analog: itself — swap three `cfg.*` reads for `payload.*` reads. No cross-file twin needed.**

**Current destination build (push.py:99) — THE line to switch (D-02/D-04):**
```python
remote_dest = f"{cfg.push_ssh_user}@{cfg.push_ssh_host}:{cfg.cloud_scratch_dir}/{payload.file_id}.{payload.file_type}"
```
→ switch to `payload.dest_ssh_user (or cfg.push_ssh_user fallback) @ payload.dest_host : payload.dest_scratch_dir / {file_id}.{file_type}`.

**Invariants to keep byte-identical** (push.py:100-109): the `--` argv terminator (L106), `-e "ssh …"` single element (L104-105), `StrictHostKeyChecking=yes` + `UserKnownHostsFile` (L95-96), `BatchMode=yes`. The remote path stays `<scratch_dir>/<file_id>.<file_type>` (server UUID, never the untrusted filename).

**`_require_push_config` (push.py:112-119) — LANDMINE 2 (see Shared Patterns):**
```python
missing = [
    name for name in ("push_ssh_host", "push_ssh_user", "cloud_scratch_dir", "push_ssh_key", "push_known_hosts") if getattr(cfg, name) is None
]
```
→ Drop `push_ssh_host` + `cloud_scratch_dir` from the fileserver's required set (payload carries them). **KEEP `push_ssh_key` + `push_known_hosts`** (secret material stays agent-side, D-03). Keep `push_ssh_user` (the `dest_ssh_user is None` fallback source). Keep the WR-03 timeout-layering check (L127-136) untouched.

---

### `src/phaze/config_backends.py` — `ComputeBackend` (config, transform)

**Analog: `ComputeBackend._require_dispatch_fields` (config_backends.py:91-104) — extend the same id-tagged validator; also see `KueueBackend._require_kube` (L119-125).**

**Current submodel + id-tagged fail-fast idiom (config_backends.py:79-104):**
```python
class ComputeBackend(BaseModel):
    kind: Literal["compute"]
    id: str
    rank: int = Field(ge=0, lt=1000)
    cap: int = Field(gt=0, lt=1000)
    agent_ref: str | None = None
    scratch_dir: str | None = None  # was ControlSettings.compute_scratch_dir (D-13)

    @model_validator(mode="after")
    def _require_dispatch_fields(self) -> "ComputeBackend":
        if not self.agent_ref:
            raise ValueError(f"backend {self.id!r} (kind=compute) requires an agent_ref")
        if not self.scratch_dir:
            raise ValueError(f"backend {self.id!r} (kind=compute) requires a scratch_dir")
        return self
```

**Add the push host (D-01, discretion: name it `push_host` per RESEARCH Open-Q3):** declare `push_host: str | None = None` and append a matching id-tagged clause to `_require_dispatch_fields` in the exact style above (`raise ValueError(f"backend {self.id!r} (kind=compute) requires a push_host")`). Follow the `scratch_dir` fail-fast precedent — a missing host must fail at construction, not at push time.

---

### `src/phaze/routers/agent_push.py` — `/pushed` + `/mismatch` (route, request-response)

**Analog: `KueueBackend.reconcile`'s `WHERE backend_id == self.id` scoping (backends.py:426-432) + the existing `/pushed` WR-02 rowcount guard (agent_push.py:106-129).** The callback equivalent of Kueue's per-backend cron scoping is: read the `cloud_job` row by `file_id`, then resolve its **recorded** `backend_id` to the `ComputeBackend` — never `select_active_agent(kind="compute")`.

**The five single-global reads to switch** (all in this file unless noted):

| # | Current (line) | Switch to | Decision |
|---|----------------|-----------|----------|
| 1 | `push.py:99` `cfg.push_ssh_*`+`cfg.cloud_scratch_dir` | `payload.dest_*` | D-02/D-04 |
| 2 | `push.py:115` `_require_push_config` required set | drop `push_ssh_host`+`cloud_scratch_dir`; keep secrets | D-03/D-04 |
| 3 | `agent_push.py:133` `settings.active_compute_scratch_dir` | resolved `ComputeBackend.scratch_dir` | D-06 |
| 4 | `agent_push.py:93,131` `select_active_agent(kind="compute")`+`queue_for(compute_agent.id)` | resolve from `cloud_job.backend_id → agent_ref`, then `queue_for(agent_ref)` | D-06/D-07 |
| 5 | `agent_push.py:232-237` `/mismatch` re-drive builds a **destination-less** payload | **must ALSO stamp `dest_*`** (Landmine 1) | D-02 |

**Current `/pushed` compute-agent gate to REPLACE (agent_push.py:92-96):**
```python
try:
    compute_agent = await select_active_agent(session, kind="compute")
except NoActiveAgentError:
    logger.warning("report_pushed held: no compute agent online", file_id=str(file_id), agent_id=agent.id)
    return PushedResponse(file_id=file_id)
```

**Current `/pushed` scratch + queue reads to REPLACE (agent_push.py:131-133):**
```python
compute_queue = request.app.state.task_router.queue_for(compute_agent.id)
# TRANSITIONAL — Phase 68: registry-derived reduction accessor (removed with the Backend protocol).
scratch_path = f"{settings.active_compute_scratch_dir}/{file_id}.{file.file_type}"
```

**The D-06/D-07 rewrite (mirror RESEARCH Code Examples):**
```python
cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
backend = resolve_compute_backend(settings, cloud_job.backend_id if cloud_job else None)   # NEW helper (see Shared)
if backend is None:
    return PushedResponse(file_id=file_id)                    # clean 200 hold (unattributed/spilled)
if agent.id != backend.agent_ref:                            # D-07 reporter validation (the MCOMP-06 security core)
    logger.warning("report_pushed rejected: reporter != dispatched agent", file_id=str(file_id), reporter=agent.id, expected=backend.agent_ref)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="reporting agent is not the dispatched compute agent")
scratch_path = f"{backend.scratch_dir}/{file_id}.{file.file_type}"
compute_queue = request.app.state.task_router.queue_for(backend.agent_ref)
```

**Keep byte-identical:** the WR-02 `update(...).where(state == FileState.PUSHING)` rowcount==0 idempotent-no-op guard (L106-120), the `cloud_job SUCCEEDED` terminalization (L129), `enqueue_process_file(...)` with the ORM-pinned `expected_sha256=file.sha256_hash` (L134-141), the single `session.commit()`. **D-07 anti-pattern:** never re-stamp `backend_id` from the reporting token — validate-and-reject, never validate-and-adopt.

**`/mismatch` (agent_push.py:153-261):** same reporter validation before the re-drive; AND stamp `dest_*` onto the rebuilt payload (L232-237) — see Landmine 1.

---

### `src/phaze/config.py` — `active_compute_scratch_dir` (config, DELETION)

**Analog: the Phase-70 retired `active_kube` / `active_bucket` module-globals in the same file (config.py:503-510) — the exact "retire the last transitional global" precedent.**

The property (config.py:483-501) has **exactly one runtime reader**: `agent_push.py:133` — which is precisely the line D-06 rewires. Once D-06 lands, delete the property outright (RESEARCH reader audit confirms all other matches are docstrings/comments/tests). Follow the Phase-70 pattern of replacing the global read comment with a "RETIRED — resolved per-file from the recorded `backend_id`" note.

---

### `src/phaze/tasks/release_awaiting_cloud.py` — drain tick (task, batch) — UNCHANGED (D-08)

**Analog: itself. Regression-test surface ONLY — no code change.** The per-backend snapshot try/except isolation (release_awaiting_cloud.py:151-157) already degrades a flaky compute lane to 0 slots without failing the tick; `select_backend` already iterates the rank-ordered N-backend snapshot. Phase 73 adds MCOMP-04/05 regressions against this live machinery.

**The live isolation to prove (release_awaiting_cloud.py:151-157):**
```python
try:
    available = await backend.is_available(session)
    remaining = max(0, backend.cap - await backend.in_flight_count(session))
except Exception:
    logger.warning("stage_cloud_window: backend snapshot probe failed -> treating as unavailable (0 slots)", backend_id=backend.id)
    snapshot[backend.id] = {"backend": backend, "available": False, "remaining": 0, "cap": backend.cap}
    continue
```

---

## Shared Patterns

### The `resolve_compute_backend` inverse-lookup helper (NEW — the one genuinely new function)
**Source template:** `s3_staging.resolve_bucket_config` (s3_staging.py:91-105) — the AUTHORITATIVE inverse of a `pick`.
**Apply to:** both `/pushed` and `/mismatch` (and available to `dispatch` if factored).
Add to `services/backends.py` (pure, ORM-free — reads only `cfg.backends`):
```python
def resolve_compute_backend(cfg: ControlSettings, backend_id: str | None) -> ComputeBackend | None:
    if backend_id is None:
        return None
    return {b.id: b for b in cfg.backends if b.kind == "compute"}.get(backend_id)
```
Mirrors `resolve_bucket_config`'s `None`-guard + dict-comprehension-`.get()` shape exactly. Single test surface.

### Record-don't-rederive (MKUE-01 discipline)
**Source:** `KueueBackend` (bind `self.config` at construction → stamp on dispatch → read recorded value downstream).
**Apply to:** every Phase-73 reader. `dispatch` stamps `dest_*` + `backend_id`; `_build_rsync_argv`, `/pushed`, `/mismatch` all READ the recorded value. **Never** call `select_active_agent(kind="compute")` or `active_compute_scratch_dir` downstream of dispatch.

### Reporter authorization (D-07 — the MCOMP-06 security property, V4 Access Control)
**Source:** existing `get_authenticated_agent` token dependency (agent_push.py:47,69) gives identity; D-07 adds one equality check.
**Apply to:** `/pushed` + `/mismatch`. `if agent.id != backend.agent_ref: raise HTTPException(403)` — reject, do NOT terminalize. A stale/wrong/duplicate agent can never mis-attribute another agent's file.

### Per-entry agent resolution (reuse Phase 72)
**Source:** `select_agent_by_id(session, agent_id, kind="compute")` (enqueue_router.py:131), already used by `ComputeAgentBackend.is_available` (backends.py:275) and `_agent_ref()` (backends.py:251-263).
**Apply to:** liveness gating — already delivered; MCOMP-02 needs a regression only.

### LANDMINE 1 — the `/mismatch` re-drive builds a destination-less payload
**Source of the bug:** agent_push.py:232-237 rebuilds a fresh `PushFilePayload(file_id, original_path, file_type, agent_id)` with **no destination**. Under D-02 this now re-drives a push to nowhere.
**Fix:** resolve `cloud_job.backend_id → ComputeBackend` via `resolve_compute_backend` and stamp `dest_*`, identically to dispatch. Consider factoring the payload-build into one helper both `_enqueue_push_file` and this re-drive call.

### LANDMINE 2 — `cloud_scratch_dir` has TWO readers with opposite fates
**Source:** `push.py:99` (fileserver, remote target → **retire**) vs `agent_worker.py:103` (compute agent, local janitor dir → **KEEP**). Do NOT delete the `cloud_scratch_dir` field from `AgentSettings`; only stop the fileserver's `push.py` from reading it as the destination. Drop only `cloud_scratch_dir` + `push_ssh_host` from `_require_push_config`'s fileserver set.

### KNOWN LIMITATION (document, do NOT widen) — `reenqueue.py:374`
`recover_orphaned_work` still re-drives held files to `select_active_agent(kind="compute")` (single-active). RESEARCH Pitfall 3 / Open-Q1: **out of scope** for Phase 73 (not a dispatch/push/reconcile seam). Flag as a PROV-01 backlog follow-up in the plan; silently widening recovery re-drive risks the 44.5k over-enqueue incident class. Do not change it.

### Secret custody (D-03, V6)
`push_ssh_key` + `push_known_hosts` (`SecretStr`) stay agent-side, materialized to 0600 temp files, shredded in `finally` (push.py). Only non-secret `dest_host`/`dest_scratch_dir`/`dest_ssh_user` travel in the payload. Log projections stay `{id, kind, rank, cap}` / `backend_id` only.

---

## Test Pattern Assignments

### `tests/analyze/services/test_backends.py` (MCOMP-02 liveness, MCOMP-05 isolation)
**Analog: `test_kueue_reconcile_scope_ignores_other_backend_rows` (L704-723)** — the verbatim per-backend attribution twin (seeds a kueue row + a sibling `compute-a1` row; asserts only the scoped one is touched). Mirror it for compute reconcile-attribution / no-cross-contamination.
- Fixture: `backends_toml_env` (test_backends.py fixture) + the `_LOCAL_2KUEUE_HEAD` / `_TWO_BUCKETS` TOML-block builders (L756-936) → extend to a `local + 2-compute` registry with **distinct `scratch_dir`/`push_host`/`agent_ref`** per compute entry.
- Liveness (MCOMP-02): mirror `test_compute_is_available_true_when_bound_agent_online` (L335) + `test_compute_is_available_reads_bound_ref_not_single_active_pick` (L361) for N compute entries — agent A online, agent B offline → only A's lane eligible.
- Isolation (MCOMP-05): one flaky compute `is_available` raises/times out → its lane degrades to `{available: False, remaining: 0}`, the `stage_cloud_window` tick COMPLETES, healthy lanes still dispatch. Test against `release_awaiting_cloud.stage_cloud_window` (the try/except at L151-157).

### `tests/agents/routers/test_agent_push.py` (MCOMP-06 reporter validation, D-06 scratch resolution)
**Analog: `test_pushed_transitions_clears_ledger_and_enqueues_process_file` (L223) + `_patch_settings` helper (L136) + `test_pushed_scratch_path_resolves_under_local_2kueue_1compute` (L262).**
- New: wrong-reporter 403 + no-terminalization (D-07). Seed a `cloud_job(backend_id="compute-a")`; call `/pushed` as agent `compute-b` → assert 403 and the file stays PUSHING, `cloud_job` NOT SUCCEEDED.
- New/extend: per-backend scratch resolution — assert `scratch_path == f"{backend.scratch_dir}/{file_id}.{ext}"` resolved from the recorded `backend_id`, and `process_file` queued on the file's OWN `agent_ref` (Pitfall 4), NOT `select_active_agent`.
- Extend `_mismatch` cases (L373+) so the re-driven payload carries `dest_*` (Landmine 1).

### `tests/analyze/core/test_push_pipeline.py` (MCOMP-03 argv unit)
**Analog: `test_rsync_argv_remote_dest_is_file_id_not_filename` (L137-144)** — asserts the exact `remote_dest` string.
- New: `_build_rsync_argv` reads `payload.dest_*`, NOT `cfg.*`. Two payloads with distinct `dest_host`/`dest_scratch_dir` → two distinct `remote_dest` strings; confirm no `cfg.push_ssh_host`/`cfg.cloud_scratch_dir` leaks into the argv.
- Extend `_require_push_config` cases (L271-286) for the reduced fileserver required set.

### `tests/analyze/services/test_backend_selection.py` (MCOMP-04 rank/cap spread)
**Analog: `test_spill_when_lowest_rank_full_picks_next_rank` (L95) + `test_rank_first_picks_lowest_rank_available` (L86).** Note the `_compute(id=..., rank=..., cap=...)` helper (L38) builds pure selection backends (no config needed).
- New: N compute backends rank-ordered — the drain fills lower-`rank` up to `cap`, then spills to the next-rank compute lane (free arm64 lower rank preferred over paid x86).

### `tests/analyze/services/test_compute_binding_golden.py` (behavior-preservation)
**Analog: `test_single_compute_registry_resolution_is_byte_identical` (L64) — asserts the exact `scratch_path` string `/srv/scratch/…mp3` (L81-85).**
- Extend: assert the ≤1-compute `remote_dest` (from `_build_rsync_argv`) AND `/pushed` `scratch_path` stay **byte-identical** after the payload carries the single destination (with `dest_ssh_user=None` → `cfg.push_ssh_user` fallback proving A3).

---

## No Analog Found

None. Every Phase-73 change maps to an in-repo twin (Phase-70 `KueueBackend` / `s3_staging` for the core rewiring; same-model validators for the schema/config; existing test cells for every regression).

---

## Metadata

**Analog search scope:** `src/phaze/services/`, `src/phaze/routers/`, `src/phaze/tasks/`, `src/phaze/schemas/`, `src/phaze/config*.py`, `tests/analyze/`, `tests/agents/routers/`
**Files scanned:** 12 source + 6 test (line numbers verified against live Phase-73 branch, matching RESEARCH.md within ±2 lines)
**Pattern extraction date:** 2026-07-05
