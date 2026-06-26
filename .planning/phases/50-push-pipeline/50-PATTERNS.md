# Phase 50: Push pipeline - Pattern Map

**Mapped:** 2026-06-25
**Files analyzed:** 18 (3 new modules/templates + 15 modified)
**Analogs found:** 17 / 18 (only the rsync-subprocess core of `push.py` has no in-repo precedent)

This phase is ~90% brownfield wiring of locked, well-established patterns. Almost every new
artifact has a near-exact in-repo analog whose code should be copied with minimal change. The
one genuinely novel surface is the rsync-over-SSH `asyncio.create_subprocess_exec` call inside
`push_file` — no `subprocess`/`rsync`/`ssh` precedent exists in `src/`, so the planner should use
the RESEARCH §"rsync-over-SSH from asyncio" block as the source-of-truth excerpt for that core.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/tasks/push.py` (NEW) | task (agent) | file-I/O / transfer | `src/phaze/tasks/functions.py` (`process_file`) | role-match (transport core is novel) |
| `src/phaze/tasks/release_awaiting_cloud.py` → `stage_cloud_window` (EVOLVE) | task (controller cron) | event-driven / batch | `src/phaze/tasks/release_awaiting_cloud.py` (itself, Phase 49) | exact |
| `src/phaze/tasks/_shared/deterministic_key.py` (MODIFY) | utility | transform | existing `_KEY_BUILDERS` entries (same file) | exact |
| `src/phaze/models/file.py` (MODIFY) | model (enum) | n/a | `AWAITING_CLOUD` / `ANALYSIS_FAILED` (same file) | exact |
| `src/phaze/schemas/agent_tasks.py` (MODIFY) | schema | request-response | `ProcessFilePayload` + `fine_cap`/`coarse_cap` additions (same file) | exact |
| `src/phaze/tasks/functions.py` (MODIFY) | task (agent) | file-I/O | `process_file` body (same file) | exact |
| `src/phaze/tasks/agent_worker.py` (MODIFY) | config (worker startup) | event-driven | `startup()` + `functions[]` list (same file) | exact |
| `src/phaze/services/enqueue_router.py` (MODIFY) | service (router) | transform | `AGENT_TASKS` frozenset (same file) | exact |
| `src/phaze/services/pipeline_counters.py` (MODIFY) | service | transform | `PIPELINE_FUNCTIONS` tuple (same file) | exact |
| `src/phaze/tasks/reenqueue.py` (MODIFY) | task (controller) | event-driven | `_DOMAIN_COMPLETED_STAGES` + `held_agent_rows` partition (same file) | exact |
| `src/phaze/tasks/controller.py` (MODIFY) | config (worker) | event-driven | `CronJob(release_awaiting_cloud, "*/5 …")` (same file) | exact |
| `src/phaze/routers/pipeline.py` (MODIFY) | router | request-response | `_route_discovered_by_duration` + dashboard card surfacing (same file) | exact |
| `src/phaze/routers/agent_push.py` (NEW) or extend `agent_analysis.py` | router (internal API) | request-response | `src/phaze/routers/agent_analysis.py` (`put_analysis` / `report_analysis_failed`) | exact |
| `src/phaze/services/analysis_enqueue.py` (MODIFY) | service (producer) | CRUD/enqueue | `enqueue_process_file` (same file) | exact |
| `src/phaze/services/pipeline.py` (MODIFY) | service | CRUD (count) | `get_awaiting_cloud_count` + `_safe_count` (same file) | exact |
| `src/phaze/config.py` (MODIFY) | config | n/a | `cloud_route_threshold_sec` + `SECRET_FILE_FIELDS` (same file) | exact |
| `src/phaze/services/agent_client.py` (MODIFY) | service (HTTP client) | request-response | `report_analysis_failed` method (same file) | exact |
| `src/phaze/templates/pipeline/partials/*_card.html` (NEW ×2) | component (template) | n/a | `awaiting_cloud_card.html` | exact |

## Pattern Assignments

### `src/phaze/tasks/push.py` (NEW — agent task, file-I/O transfer)

**Analog:** `src/phaze/tasks/functions.py` (`process_file`) for the agent-task envelope + the
Postgres-free import boundary. The rsync core is novel (use RESEARCH §"rsync-over-SSH from asyncio").

**Agent-task envelope to copy** (`functions.py:1-9, 41-54, 139-148`): module docstring asserts
the boundary ("This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy. Enforced
by tests/test_task_split.py"); `_agent_settings()` resolves `get_settings()` and narrows to
`AgentSettings` (push.py needs the same to read `push_ssh_host`/`cloud_scratch_dir`/etc.); the task
signature is `async def push_file(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:` with
`payload = PushFilePayload.model_validate(kwargs)` and `api: PhazeAgentClient = ctx["api_client"]`.

**Settings-narrowing pattern** (`functions.py:41-54`):
```python
def _agent_settings() -> AgentSettings:
    cfg = get_settings()
    if not isinstance(cfg, AgentSettings):  # pragma: no cover
        msg = f"process_file requires PHAZE_ROLE=agent; get_settings() returned {type(cfg).__name__}"
        raise RuntimeError(msg)
    return cfg
```

**On-success control callback** (mirror `functions.py:201` `await api.put_analysis(...)` and the
`report_analysis_failed` calls at `:173, :177, :185`): on rc==0, call the new
`api.report_pushed(file_id)`; on terminal rsync failure call `api.report_push_mismatch(...)` /
re-raise so SAQ records the attempt (see the retryable-vs-terminal split at `functions.py:179-189`).

**rsync core (NOVEL — no in-repo analog).** Source: RESEARCH §"rsync-over-SSH from asyncio"
(50-RESEARCH.md lines ~197-224). Key constraints from that block + CLAUDE.md ruff `S` rules:
`asyncio.create_subprocess_exec` with a **list argv, never `shell=True`** (bandit `S602`/`S603`);
`-e "ssh -i KEY -o StrictHostKeyChecking=yes -o UserKnownHostsFile=KNOWN_HOSTS -o BatchMode=yes"`
as a single argv element; `--partial-dir=.rsync-partial` + default temp-rename for atomicity (NO
`--inplace`); `--timeout`; remote path `<scratch_dir>/<file_id>.<ext>` (UUID-derived, never the
untrusted filename). A justified inline bandit comment may be needed on the exec line.

**Off-loop work convention** (`agent_worker.py:120`, `scan.py:268`): never block the loop —
`await asyncio.to_thread(...)` for any sync/CPU step (the janitor sweep uses this).

---

### `stage_cloud_window` cron — EVOLVE `src/phaze/tasks/release_awaiting_cloud.py` (controller cron)

**Analog:** `release_awaiting_cloud.py` (the WHOLE file is the template — Phase 49, verified).

**Cron body shape to copy** (`release_awaiting_cloud.py:44-92`): open
`ctx["async_session"]()`; SCAN a state via `get_files_by_state(session, FileState.AWAITING_CLOUD)`;
GATE on `select_active_agent(session, kind="compute")` wrapped in `try/except NoActiveAgentError`
→ clean zero no-op (D-04 hold); resolve the **fileserver** queue for `push_file` via
`ctx["task_router"].queue_for(fileserver_agent.id)`; loop-enqueue; `await session.commit()` once;
return `{"staged": N, "skipped": M}`.

**The window math is the new logic** (RESEARCH §"Stay one ahead" cron, lines ~271-293):
`window = COUNT(FileRecord.state IN {PUSHING, PUSHED})`; `slots = cloud_max_in_flight - window`;
if `slots <= 0` return zero; `SELECT … WHERE state == AWAITING_CLOUD ORDER BY created_at ASC LIMIT
slots`; per file set `state = PUSHING` then enqueue `push_file`. Count the window from
`FileState` (committed truth, D-08), NOT the ledger/saq_jobs.

**Module docstring discipline to mirror** (`release_awaiting_cloud.py:1-26`): note CONTROL-ONLY
(needs `ctx["async_session"]` + `ctx["task_router"]`), register ONLY in `controller.py` (never the
agent worker — `tests/test_task_split.py`), and FastAPI-free imports.

**No-op + dedup contract** (`release_awaiting_cloud.py:80-89`): `job is None` (deterministic-key
dedup) counts as `skipped`; otherwise `staged`. The `push_file:<id>` key collapses a double-tick.

---

### `src/phaze/tasks/_shared/deterministic_key.py` (MODIFY — add `push_file` key builder)

**Analog:** the `_KEY_BUILDERS` dict (lines 74-83, same file).

**Exact edit** (`deterministic_key.py:74-83`): add one entry to the file-keyed builders:
```python
"push_file": lambda k: str(k["file_id"]),
```
NOTE the comment at line 70 ("Exactly 8 entries") must update to 9. Per RESEARCH §3, three
totality guards fail until updated together: this builder, `pipeline_counters.PIPELINE_FUNCTIONS`,
and `enqueue_router.AGENT_TASKS` (see those files below), plus the recovery classification.

---

### `src/phaze/models/file.py` (MODIFY — add `PUSHING`, `PUSHED`)

**Analog:** `AWAITING_CLOUD` (lines 40-43) and `ANALYSIS_FAILED` (lines 37-39), same `FileState`
StrEnum.

**Exact pattern** (`file.py:40-43`): add two members beside `AWAITING_CLOUD`, with the same
"code-only StrEnum over String(30) → no migration" comment:
```python
PUSHING = "pushing"   # rsync in progress to compute scratch (D-08)
PUSHED = "pushed"     # on compute scratch, awaiting/within analysis (D-08)
```
`state` is `String(30)` (`file.py:66`) — "pushing"/"pushed" fit, NO Alembic migration (the
`ANALYSIS_FAILED`/`AWAITING_CLOUD` precedent). The `ix_files_state` index (`file.py:78`) already
covers state-count queries.

---

### `src/phaze/schemas/agent_tasks.py` (MODIFY — extend `ProcessFilePayload`; add `PushFilePayload`)

**Analog:** `ProcessFilePayload` + the Phase-44 `fine_cap`/`coarse_cap` optional-field additions
(lines 28-43).

**Extend `ProcessFilePayload`** (`agent_tasks.py:38-43`): add two optional fields after the
cap fields, mirroring the "default None preserves the bulk producer under extra='forbid'" comment:
```python
expected_sha256: str | None = None   # control pins from FileRecord.sha256_hash (D-11)
scratch_path: str | None = None      # compute reads this ephemeral copy instead of original_path
```
`scratch_path is not None` IS the compute-read/ephemeral signal — no separate boolean flag.

**New `PushFilePayload`** (copy the `ProcessFilePayload` class header, `agent_tasks.py:28-37`):
`model_config = ConfigDict(extra="forbid")`; fields `file_id: uuid.UUID`, `original_path: str`
(the media-mount source), `file_type: str`, `agent_id: str`. The deterministic-key builder reads
`k["file_id"]`, so `file_id` must be present.

---

### `src/phaze/tasks/functions.py` (MODIFY — `process_file` scratch read/verify/cleanup)

**Analog:** the `process_file` body itself (lines 139-220).

**Read-path swap** (`functions.py:159-168`): change `payload.original_path` to
`read_path = payload.scratch_path or payload.original_path` and pass `read_path` to
`run_in_process_pool`. The analyzer is path-agnostic (RESEARCH-verified `analysis.py`).

**sha256 verify off-loop** — reuse `compute_sha256` exactly as `scan.py:268` does:
```python
actual = await asyncio.to_thread(compute_sha256, Path(payload.scratch_path))
if actual != payload.expected_sha256:
    Path(payload.scratch_path).unlink(missing_ok=True)
    await api.report_push_mismatch(payload.file_id)   # control re-drives push (D-12)
    return {"file_id": str(payload.file_id), "status": "push_mismatch"}
```
`compute_sha256` (`services/hashing.py:10`) is pure stdlib (hashlib + pathlib) → import-safe in the
Postgres-free worker. Gate the verify block on `payload.scratch_path and payload.expected_sha256`.

**`finally` cleanup (D-13)**: wrap the existing try/except (`functions.py:159-219`) so cleanup runs
on success, `TimeoutError` (`:169`), `ProcessExpired` (`:175`), generic `except` (`:179`), and the
mismatch path:
```python
finally:
    if payload.scratch_path:
        Path(payload.scratch_path).unlink(missing_ok=True)
```

---

### `src/phaze/tasks/agent_worker.py` (MODIFY — register `push_file` + compute-only janitor)

**Analog:** `startup()` (lines 75-163) + the `functions[]` list (lines 232-239), same file.

**Register the task** (`agent_worker.py:65, 232-239`): add `from phaze.tasks.push import push_file`
and append `push_file` to `settings["functions"]`.

**Startup janitor (D-14)** — model on the off-loop startup step at `agent_worker.py:120`
(`await asyncio.to_thread(ensure_models_present, Path(cfg.models_path))`). Add, gated on
`cfg.kind == "compute"` AND a configured scratch dir (the fileserver runs the SAME module and must
NOT sweep):
```python
if cfg.kind == "compute" and cfg.cloud_scratch_dir:
    await asyncio.to_thread(_sweep_scratch, Path(cfg.cloud_scratch_dir))
```
This is the agent-side analog of the controller's startup reconciliation (`controller.py:143-155`
`backfill_ledger_from_saq_jobs` + `recover_orphaned_work`). The `kind` field already exists
(`config.py:415-419`).

**Boundary guard:** `push.py` and the verify path must stay Postgres-free — run
`uv run pytest tests/test_task_split.py` after touching `push.py`/`functions.py`/`agent_worker.py`.

---

### `src/phaze/services/enqueue_router.py` (MODIFY — `AGENT_TASKS |= {"push_file"}`)

**Analog:** the `AGENT_TASKS` frozenset (lines 60-69).

**Exact edit** (`enqueue_router.py:60-69`): add `"push_file"` to the file-touching agent task set
(it reads the media mount → routes to the per-agent fileserver queue). `routing_for_function`
(`scheduling_ledger.py:45-58`) then derives `"agent"` automatically, and `resolve_queue_for_task`
(`:128-159`) routes it. `select_active_agent(session, kind="fileserver")` (`:93-125`) already
exists for picking the push initiator.

---

### `src/phaze/services/pipeline_counters.py` (MODIFY — `PIPELINE_FUNCTIONS += ("push_file",)`)

**Analog:** the `PIPELINE_FUNCTIONS` tuple (lines 33-42).

**Exact edit** (`pipeline_counters.py:33-42`): append `"push_file"` (update the "8 pipeline
functions" comment at line 29 to 9). The drift-guard test
(`tests/test_deterministic_key.py`) enforces this stays in sync with `_KEY_BUILDERS`.

---

### `src/phaze/tasks/reenqueue.py` (MODIFY — classify `push_file`; fileserver re-drive partition)

**Analog:** `_DOMAIN_COMPLETED_STAGES` (lines 107-113) + the `held_agent_rows` AWAITING_CLOUD
partition (lines 304-332), same file.

**Domain-completed predicate (D-10)** (`reenqueue.py:107-113` + `is_domain_completed` at
`:194-217`): add `"push_file"` to `_DOMAIN_COMPLETED_STAGES`, "done when `FileRecord.state` ∈
{PUSHED, ANALYZED, ANALYSIS_FAILED}" (the file advanced past pushing). A `push_file` row whose file
is still `PUSHING`/`AWAITING_CLOUD`/`DISCOVERED` is orphaned → re-drive. Mirror the `_ANALYZE_DONE`
done-set build in `_build_done_sets` (`:150-156`) and the `process_file` branch in
`is_domain_completed` (`:212-213`).

**Fileserver-routed re-drive** — copy the `held_agent_rows` pattern (`reenqueue.py:312-332`),
which already partitions AWAITING_CLOUD `process_file` rows and routes them to
`select_active_agent(session, kind="compute")`. A re-driven `push_file` is the mirror: partition
`push_file` rows out and route to `select_active_agent(session, kind="fileserver")` (it reads the
media mount). With no fileserver online, skip with a WARNING (do not raise), exactly as
`:324-328` / `:338-342`.

**`process_file` "done" unchanged** (D-10): keep `{ANALYZED, ANALYSIS_FAILED}` at
`_select_done_analyze_ids` (`:165-167`) — PUSHED is NOT added (a PUSHED file still needs analysis).

---

### `src/phaze/tasks/controller.py` (MODIFY — register the staging cron)

**Analog:** `CronJob(release_awaiting_cloud, cron="*/5 * * * *")` (line 232) + the import (line 41)
+ the functions-list registration (line 212).

**Exact pattern** (`controller.py:41, 205-233`): import `stage_cloud_window`, add it to
`settings["functions"]`, and register `CronJob(stage_cloud_window, cron="*/5 * * * *")` in
`cron_jobs`. Per RESEARCH §"State of the Art", this REPLACES the `release_awaiting_cloud` cron (the
"drain ALL → process_file" behavior is deprecated). Keep the Phase-42 "DO NOT re-add a general
auto-advance cron" comment intact (`:220-231`) — the new cron is scoped only to the bounded
cloud-window top-up.

---

### `src/phaze/routers/pipeline.py` (MODIFY — routing seam + two count cards)

**Analog:** `_route_discovered_by_duration` (lines 252-339) for the seam; the
`awaiting_cloud_count` surfacing (lines 477-502, 533-553) for the cards.

**Routing seam reshape (RESEARCH §Critical Finding 2, strongly preferred single-entry):** in the
long-file branch (`pipeline.py:306-313`), drop the `compute_agent is not None → cloud_files`
direct-enqueue path entirely — **always** `file.state = FileState.AWAITING_CLOUD` for a long file
(held regardless of compute availability; the staging cron stages it). Remove the
`cloud_files`/`compute_q`/`compute_agent` cloud-enqueue at `:294-299, 302, 328-331`. This is the
load-bearing change: it removes the only direct-to-compute path so the ≤N window cannot be bypassed
(Pitfall 1). The fileserver/local path (`:314-315, 324-327`) is unchanged.

**Two new count cards (D-09)** — copy the `awaiting_cloud_count` surfacing exactly:
initial-load read (`pipeline.py:477-502`) and the 5s-poll re-push (`:533-553`). Add
`pushing_count` ("Staged (pushing)") and `analyzing_cloud_count` ("Analyzing (cloud)") to both the
dashboard context dict and the stats-poll context, reading the new service helpers below.

---

### `src/phaze/routers/agent_push.py` (NEW — internal-API callbacks) — or extend `agent_analysis.py`

**Analog:** `src/phaze/routers/agent_analysis.py` — `put_analysis` (lines 94-199) and
`report_analysis_failed` (lines 202-234). RESEARCH §Critical Finding 1 + Open-Q2 recommend two
endpoints (mirroring the existing `put_analysis` / `report_analysis_failed` split).

**Router + auth shape to copy** (`agent_analysis.py:54, 94-100, 202-208`):
```python
router = APIRouter(prefix="/api/internal/agent/push", tags=["agent-internal"])

@router.post("/{file_id}/pushed", status_code=status.HTTP_200_OK, response_model=...)
async def report_pushed(
    file_id: uuid.UUID,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ...:
```

**The "pushed" handler — one transaction does state + ledger-clear + enqueue** (copy the
state-update + `clear_ledger_entry` idiom at `agent_analysis.py:189, 196, 221-226`):
```python
await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.PUSHED))
await clear_ledger_entry(session, f"push_file:{file_id}")
# Control plane reads sha256_hash from ORM here (D-11) and enqueues process_file on the compute queue:
file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
compute_agent = await select_active_agent(session, kind="compute")
compute_queue = request.app.state.task_router.queue_for(compute_agent.id)
await enqueue_process_file(compute_queue, file, compute_agent.id, models_path,
                           expected_sha256=file.sha256_hash, scratch_path=...)
await session.commit()
```
AUTH-01 discipline: `file_id` from the PATH only, `agent` from the token dep, never the body
(`agent_analysis.py:106-108, 194-196`).

**The "push-mismatch / re-drive" handler** mirrors `report_analysis_failed` (`:202-234`):
increment the attempt counter (RESEARCH Pitfall 4: store `push_attempt` in the `push_file` ledger
payload JSONB), and either re-enqueue `push_file` keeping the PUSHING slot (Open-Q1 recommendation)
or, past `push_max_attempts`, set `FileState.ANALYSIS_FAILED` + `clear_ledger_entry` in one
transaction (exactly `:221-226`).

---

### `src/phaze/services/analysis_enqueue.py` (MODIFY — extend `enqueue_process_file`)

**Analog:** `enqueue_process_file` + the Phase-44 `fine_cap`/`coarse_cap` keyword-only additions
(lines 43-101).

**Exact pattern** (`analysis_enqueue.py:43-51, 66-77`): add keyword-only
`expected_sha256: str | None = None`, `scratch_path: str | None = None` to the signature
(mirroring `fine_cap`/`coarse_cap` at `:49-50`) and thread them into the `ProcessFilePayload(...)`
construction (`:66-77`). The deterministic key (`process_file_job_key`, `:32-40`) and the
`timeout=7200`/`retries=2` policy (`:94-99`) are unchanged. The bulk local producer
(`pipeline.py:248-249` → `_enqueue_analysis_jobs`) passes neither new field, so short-file
behavior is byte-identical.

---

### `src/phaze/services/pipeline.py` (MODIFY — window + card count helpers)

**Analog:** `get_awaiting_cloud_count` (lines 805-816) + `_safe_count` (lines 272-289) +
`get_files_by_state` (lines 725-737).

**Two card counts (D-09)** — copy `get_awaiting_cloud_count` verbatim, swapping the state
(`pipeline.py:805-816`):
```python
async def get_pushing_count(session: AsyncSession) -> int:
    return await _safe_count(
        session,
        select(func.count(FileRecord.id)).where(FileRecord.state == FileState.PUSHING),
        node="pushing",
    )
```
and the same for `PUSHED` (`get_pushed_count`, node="analyzing_cloud"). `_safe_count` owns the
never-500 degrade-to-0 + rollback discipline (`:281-289`) the hot 5s poll requires.

**Window-count helper for the cron** — a single COUNT over `state IN {PUSHING, PUSHED}` (the
committed-truth window per D-08; do NOT count from the ledger). Same `select(func.count(...))`
shape as above with `FileRecord.state.in_([FileState.PUSHING, FileState.PUSHED])`. The cron's
candidate SELECT reuses the `get_files_by_state(session, FileState.AWAITING_CLOUD)` membership
(`:725-737`) plus `ORDER BY created_at ASC LIMIT slots`.

---

### `src/phaze/config.py` (MODIFY — window + push/SSH/scratch knobs + `_FILE` secrets)

**Analog:** `cloud_route_threshold_sec` on `ControlSettings` (lines 365-371); the agent analysis
knobs + `SECRET_FILE_FIELDS` on `AgentSettings` (lines 389, 485-503); the `_FILE`-secret machinery
(lines 79-145).

**Control-side knobs** — copy the `cloud_route_threshold_sec` Field pattern (`config.py:365-371`)
onto `ControlSettings`:
```python
cloud_max_in_flight: int = Field(default=2, gt=0, lt=100,
    validation_alias=AliasChoices("PHAZE_CLOUD_MAX_IN_FLIGHT", "cloud_max_in_flight"),
    description="Max cloud files staged-or-in-flight (PUSHING+PUSHED); the load-bearing ≤N window (Phase 50, D-03).")
push_max_attempts: int = Field(default=3, gt=0, lt=20,
    validation_alias=AliasChoices("PHAZE_PUSH_MAX_ATTEMPTS", "push_max_attempts"), description="...")
```

**Agent-side push knobs** — copy the `Field` + `AliasChoices("PHAZE_*", "...")` pattern (the
`watcher_*`/`analysis_*` fields, `config.py:433-503`) onto `AgentSettings`: `push_ssh_host`,
`push_ssh_user`, `cloud_scratch_dir`, `push_scratch_dir`, `push_timeout_sec`, `push_connect_timeout_sec`.

**`_FILE` secrets (D-05/D-07)** — `push_ssh_key` and `push_known_hosts` are file-mounted secrets.
Add them to `AgentSettings.SECRET_FILE_FIELDS` (copy the override at `config.py:389`:
`BaseSettings.SECRET_FILE_FIELDS | {"agent_token"}` → add the two new names). The
`_resolve_secret_files` before-validator (`config.py:82-145`) then auto-resolves their `<VAR>_FILE`
siblings — no new resolution code. NEVER log their values (D-13 token-preview discipline,
`agent_worker.py:87-97`).

**Config names are discretion** (RESEARCH A4) — these are recommendations; convention-match to
`cloud_route_threshold_sec`. Bounded `Field(gt=…, lt=…)` so an out-of-range value fails fast at
startup (the `cloud_route_threshold_sec` `gt=0, lt=86400` precedent, `:367-368`).

---

### `src/phaze/services/agent_client.py` (MODIFY — `report_pushed` / `report_push_mismatch`)

**Analog:** `report_analysis_failed` (lines 279-294), same file.

**Exact pattern** (`agent_client.py:279-294`): one method per endpoint, routing through the
`_request` tenacity funnel (`:168-217`), path-only `file_id`, lazy response-schema import:
```python
async def report_pushed(self, file_id: uuid.UUID, payload: PushedPayload) -> PushedResponse:
    from phaze.schemas.agent_push import PushedResponse  # noqa: PLC0415
    response = await self._request("POST", f"/api/internal/agent/push/{file_id}/pushed",
                                   json=payload.model_dump(mode="json"))
    return PushedResponse.model_validate(response.json())
```
`_request` already maps 4xx→no-retry / 5xx+network→retry (`:194-213`) — inherited for free. The
boundary stays httpx-only (no DB import) as the `report_metadata_failed`/`report_fingerprint_failed`
docstrings note (`:296-327`).

---

### `src/phaze/templates/pipeline/partials/*_card.html` (NEW ×2 — count cards)

**Analog:** `awaiting_cloud_card.html` (whole file, 27 lines).

**Exact pattern** (`awaiting_cloud_card.html:17-27`): clone the `<section id="…-card" …
{% if oob %}hx-swap-oob="true"{% endif %}>` structure for `#staged-pushing-card` and
`#analyzing-cloud-card`, rendering `{{ pushing_count }}` / `{{ analyzing_cloud_count }}`. Wire each
into `dashboard.html` (clone the include at `dashboard.html:25`:
`{% include "pipeline/partials/awaiting_cloud_card.html" %}`) AND `stats_bar.html` for the OOB
re-push (clone `stats_bar.html:78`:
`{% with oob = True %}{% include "pipeline/partials/awaiting_cloud_card.html" %}{% endwith %}`).
Same id on both renders is the OOB contract.

## Shared Patterns

### Postgres-free agent boundary (load-bearing)
**Source:** `tasks/functions.py:1-9` docstring; enforced by `tests/test_task_split.py`.
**Apply to:** `push.py`, the `process_file` verify path, `agent_worker.py`.
Agent tasks MUST NOT import `phaze.database`, `phaze.models.*`, `phaze.tasks.session`, or
`sqlalchemy.ext.asyncio`. They talk to control only via `PhazeAgentClient` (HTTP). `compute_sha256`
(`services/hashing.py`) is import-safe (pure stdlib). This is why push-success goes through a
control-side callback (`agent_push.py`), not an agent-side enqueue (RESEARCH §Critical Finding 1).

### Deterministic-key dedup at the single chokepoint
**Source:** `tasks/_shared/deterministic_key.py:74-103` (`_KEY_BUILDERS` + `apply_deterministic_key`).
**Apply to:** `push_file` (add the key builder); the staging cron, the recovery re-drive, and the
push-mismatch re-enqueue all rely on `push_file:<file_id>` collapsing a repeat enqueue to a no-op
(`job is None` → skipped). This is the backstop that makes the ≤N window double-tick-safe.

### Scheduling-ledger write/clear (never hand-rolled)
**Source:** WRITE hook `deterministic_key.py:117-151`; CLEAR in the control-side callback
`agent_analysis.py:196, 225` via `clear_ledger_entry`.
**Apply to:** `push_file`'s ledger row is written automatically by the `before_enqueue` hook (the
cron enqueues control-side → `ledger_sessionmaker` present). It is CLEARED in the "pushed" /
"mismatch-cap" callbacks (`agent_push.py`), in the SAME transaction as the state write — the agent
worker cannot clear its own row (no `ledger_sessionmaker` agent-side).

### `_safe_count` degrade-to-0 for every dashboard count
**Source:** `services/pipeline.py:272-289`.
**Apply to:** `get_pushing_count`, `get_pushed_count`, and the window-count helper. The hot 5s
`/pipeline/stats` poll must never 500 — `_safe_count` rolls back the aborted transaction and
returns 0 on any DB error.

### Kind-scoped agent selection
**Source:** `enqueue_router.py:93-125` (`select_active_agent(session, kind=)`).
**Apply to:** the staging cron gate (`kind="compute"` for the window gate), the `push_file` route
(`kind="fileserver"` — the push initiator), and the recovery fileserver re-drive partition. No new
selection code — the Phase-49 `kind` filter already exists.

### `_FILE`-secret convention for SSH credentials
**Source:** `config.py:79-145` (`SECRET_FILE_FIELDS` + `_resolve_secret_files`), override pattern
at `config.py:389`.
**Apply to:** `push_ssh_key`, `push_known_hosts`. Add the names to
`AgentSettings.SECRET_FILE_FIELDS`; resolution is automatic. Never log values (D-13).

## No Analog Found

| File / surface | Role | Data Flow | Reason |
|----------------|------|-----------|--------|
| `push.py` rsync `create_subprocess_exec` core (~40 lines: argv builder + exit-code mapping) | task transport | file-I/O | No `subprocess`/`rsync`/`ssh` precedent anywhere in `src/`. Use RESEARCH §"rsync-over-SSH from asyncio" (50-RESEARCH.md ~197-254) as the source-of-truth excerpt: list argv (no shell), `--partial-dir`+atomic rename (no `--inplace`), pinned `known_hosts`, exit-code table, two-timeout-layer (rsync `--timeout` < SAQ job timeout). The agent-task ENVELOPE around it has an exact analog (`functions.py`); only the transport call is novel. |

## Metadata

**Analog search scope:** `src/phaze/tasks/`, `src/phaze/services/`, `src/phaze/routers/`,
`src/phaze/models/`, `src/phaze/schemas/`, `src/phaze/config.py`,
`src/phaze/templates/pipeline/partials/`.
**Files scanned (read):** functions.py, agent_worker.py, file.py, schemas/agent_tasks.py,
analysis_enqueue.py, enqueue_router.py, reenqueue.py, agent_analysis.py, config.py,
routers/pipeline.py, services/pipeline.py, agent_client.py, scheduling_ledger.py (service+model),
release_awaiting_cloud.py, deterministic_key.py, controller.py, pipeline_counters.py,
agent_task_router.py, hashing.py, awaiting_cloud_card.html, dashboard.html, stats_bar.html.
**Pattern extraction date:** 2026-06-25
