# Phase 55: Routing, state & ledger integration (the live seam) - Research

**Researched:** 2026-06-28
**Domain:** Live-seam code integration (Python 3.14 / FastAPI / SQLAlchemy async / SAQ) — wiring K8s as a third `cloud_target` branch into the existing v5.0 duration-router / `stage_cloud_window` / scheduling-ledger pipeline, plus a breaking config rename.
**Confidence:** HIGH (this is a codebase-tracing phase; every claim below is `[VERIFIED: codebase grep]` against the live source, with file:line)

## Summary

This phase is **pure code integration**, not library discovery. There are NO new external dependencies — the stack (FastAPI, SQLAlchemy async, asyncpg, SAQ, kr8s, aioboto3) is already in place from Phases 52–54. The whole job is wiring K8s in as **one new branch** at two coordinated fork points keyed on a single `cloud_target` config value, performing a breaking `cloud_burst_enabled → cloud_target` rename, adding one additive `cloud_job` column (`cloud_phase`) via an Alembic migration, and extending the static AST routing-guard test.

The two fork points (D-01) are **already physically distinct callbacks**, which makes the integration cleaner than "an `if` inside one function" implies: the a1 transport is rsync (`push_file` → `report_pushed`), the k8s transport is S3 (`s3_upload` → `report_uploaded`). The stage-side fork lives in `stage_cloud_window` (`tasks/release_awaiting_cloud.py`); the post-staging fork is simply that each transport's existing callback enqueues its own downstream task. The a1 callback `report_pushed` already enqueues `process_file`; the k8s callback `report_uploaded` currently enqueues **nothing** and **does not touch FileRecord state** — extending it is the core of D-01b.

**Primary recommendation:** Implement the `cloud_target` branch **inside `stage_cloud_window`** (not the duration router — the duration router only needs its boolean `cloud_enabled` gate re-sourced from `cloud_target != "local"`). Treat **three landmines as the load-bearing risks**: (1) `stage_file_to_s3` commits internally and would release the cron's `pg_advisory_xact_lock` mid-loop, silently breaking the ≤N window atomicity — extract a no-commit core; (2) the k8s path has **no persistent compute agent**, so `stage_cloud_window`'s GATE 1 (`select_active_agent(kind="compute")`) is wrong for k8s and must be skipped on that branch; (3) the k8s backfill must **NOT** seed a `process_file:<id>` ledger row (the CLOUDROUTE-02 hazard) the way the existing a1 backfill does at `pipeline.py:718-727`.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| `cloud_target` selection + fail-fast | Config (`ControlSettings`) | — | Control plane owns routing; a startup validator is the only safe place to fail on misconfig |
| Duration routing (hold long files in `AWAITING_CLOUD`) | API/Backend (`routers/pipeline.py`) | DB | Already target-agnostic; only its `cloud_enabled` boolean source changes |
| Stage-side fork (a1 rsync vs k8s S3) | Controller cron (`tasks/release_awaiting_cloud.py`) | DB + agent queue | The single ≤N in-flight gate; the fork keyed on `cloud_target` belongs here (D-01a) |
| Post-staging fork (process_file vs submit_cloud_job) | API callbacks (`routers/agent_push.py` a1 / `routers/agent_s3.py` k8s) | Controller queue | Each transport's own completion callback routes to its own downstream task (D-01b) |
| `cloud_phase` admission lifecycle | DB sidecar (`cloud_job`) + reconcile cron | UI | Kueue admission state is a cloud_job concern; FileRecord state machine stays unchanged (KROUTE-03) |
| Backfill to K8s | API action (`routers/pipeline.py`) | DB (ledger-scoped) | Operator-initiated, bounded, ledger-scoped — never a cron sweep (D-03) |
| Admission-state cards | UI (`templates/pipeline/partials/`) | Service read | Thin degrade-safe read over `cloud_phase`, mirroring `inadmissible_card.html` (D-04/KROUTE-06) |

## Package Legitimacy Audit

**Not applicable — this phase installs ZERO external packages.** All required libraries (`kr8s`, `aioboto3`, `sqlalchemy`, `saq`, `fastapi`, `alembic`, `structlog`) are already present and slop-audited in Phases 52–54. No `npm`/`pip`/`cargo` install occurs. `[VERIFIED: codebase grep — no new imports introduced by the planned changes]`

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01: K8s reuses the in-flight window as ONE branch at TWO coordinated fork points, keyed on `cloud_target`.**
  (a) In `stage_cloud_window` (`tasks/release_awaiting_cloud.py`): `a1` enqueues `push_file` (file-server rsync) as today; `k8s` enqueues the Phase 53 S3-staging path (`cloud_staging.stage_file_to_s3` → `s3_upload`) instead. Both flip `AWAITING_CLOUD → PUSHING` and reuse the existing window math (advisory lock, FIFO `FOR UPDATE SKIP LOCKED`, `window = COUNT(PUSHING|PUSHED)`, `slots = cloud_max_in_flight - window`).
  (b) In the post-staging callback: the `a1` `report_pushed` path enqueues `process_file` on the COMPUTE queue (as today); the `k8s` path enqueues `submit_cloud_job` (controller queue, Phase 54) once bytes are in S3 — the pod fetches via the Phase 53 presigned GET, so **K8s skips the file-server rsync entirely.** Reuses `PUSHING`/`PUSHED` (no new FileRecord state, KROUTE-03). Rejected: making `submit_cloud_job` do its own S3 staging.
- **D-02: HARD REPLACE — `cloud_burst_enabled` is REMOVED; `cloud_target: Literal["local","a1","k8s"]` (default `"local"`, `"local"` == cloud off) is the single source of truth.** No back-compat alias. Amends KROUTE-01's "under the existing `cloud_burst_enabled` master toggle" wording. The planner MUST in this phase: rewrite the `compute_scratch_dir` fail-fast validators to key off `cloud_target`; add the `cloud_target == "k8s"` fail-fast (requires `kube_api_url`/`kube_namespace`/`kube_local_queue`) — pulls the K8s portion of KDEPLOY-02 forward; migrate every operator-facing config surface (`.env.example`, `.env.example.agent`, `docker-compose*.yml`, the Phase 51 homelab runbook, `docs/`).
- **D-03: Backfill is an operator-initiated "Backfill to K8s" action on the pipeline dashboard, ledger-scoped.** Re-drives ONLY files that are `analysis_failed` AND `duration ≥ cloud_route_threshold_sec` AND carry a prior scheduling-ledger row. Bounded and explicit; NOT a cron sweep. Routes through `enqueue_router`, enforced by the KROUTE-04 AST guard.
- **D-04: `cloud_phase` is a small enum on `cloud_job`** (`queued_behind_quota` / `admitted` / `running` / `finished`) added via a new Alembic migration. The Phase 54 reconcile cron (`reconcile_cloud_jobs._reconcile_one`) is extended to write `cloud_phase` alongside `status`; `submit_cloud_job` seeds the initial value on the SUBMITTED row. KROUTE-06 dashboard admission-state cards ship in this phase — a thin read over `cloud_phase`, mirroring `get_inadmissible_count` + `inadmissible_card.html`.

### Claude's Discretion
- Exact enum storage form for `cloud_phase` (CHECK-constrained varchar vs StrEnum) — follow the `CloudJobStatus` precedent from Phase 54.
- Whether the `cloud_target` routing branch lives in the duration router entry point or inside `stage_cloud_window` — planner/researcher to resolve against the actual call graph. **(Resolved below: inside `stage_cloud_window`.)**

### Deferred Ideas (OUT OF SCOPE)
- KROUTE-01 wording amendment reconciliation — surface at `/gsd:audit-milestone`.
- Phase 56 (KDEPLOY) scope reduction (the k8s fail-fast validator portion of KDEPLOY-02 is absorbed here).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| KROUTE-01 | Single `cloud_target` selector (`Literal["local","a1","k8s"]`) chooses the active target | D-02 blast-radius map below; `config.py:405` field replacement + 3 validators |
| KROUTE-02 | K8s reuses duration router + AWAITING_CLOUD hold + `stage_cloud_window` ≤N window as a single new branch | D-01a fork in `release_awaiting_cloud.py:109`; window math at `pipeline.py:884` is target-agnostic |
| KROUTE-03 | K8s reuses `PUSHING`/`PUSHED` (no new FileRecord state); admission phase in `cloud_phase` on `cloud_job` | D-04 migration (mirror 026); `report_uploaded` extension flips PUSHING→PUSHED |
| KROUTE-04 | Static AST guard asserts every K8s enqueue site routes through `enqueue_router` | Extend `tests/test_no_default_queue_producers.py` (existing AST scanner of routers/services) |
| KROUTE-05 | ≥threshold backfill of timed-out long files to K8s, ledger-scoped exactly like v5.0 | D-03; existing `trigger_backfill_cloud` (`pipeline.py:657`) is the pattern, with two forks (ledger-scope filter + no process_file seed) |
| KROUTE-06 | Pipeline dashboard admission-state cards driven by `cloud_phase` (pulled in-scope by D-04) | Mirror `inadmissible_card.html` + `get_inadmissible_count` + OOB poll wiring |
</phase_requirements>

---

## The Live Call Graph (D-01 — trace with file:line)

### Today's a1 flow, end to end `[VERIFIED: codebase grep]`

1. **Duration routing decision** — `routers/pipeline.py:255` `_route_discovered_by_duration()`. For each `(file, duration)`: `is_long = cloud_enabled and duration is not None and duration >= threshold_sec` (`pipeline.py:312`). Long → `file.state = FileState.AWAITING_CLOUD` (`pipeline.py:316`), committed before backgrounding enqueues (`pipeline.py:326`). **No direct-to-compute path exists** (Phase 50 reshape, T-50-bypass). Callers: `trigger_analysis` (`pipeline.py:368`), `trigger_analysis_ui` (`pipeline.py:613`), `trigger_backfill_cloud` (`pipeline.py:705`). All three pass `settings.cloud_burst_enabled` as the `cloud_enabled` arg (`pipeline.py:373, 618, 710`).

2. **Stage-side window** — `tasks/release_awaiting_cloud.py:109` `stage_cloud_window(ctx)`, a `*/5` controller cron (`controller.py:242`). Sequence:
   - Master gate: `if not cfg.cloud_burst_enabled: return {...}` (`release_awaiting_cloud.py:125`).
   - `pg_advisory_xact_lock(_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY)` (`release_awaiting_cloud.py:134`) — serializes overlapping ticks (WR-04), released only at **transaction end**.
   - **GATE 1**: `select_active_agent(session, kind="compute")` — no compute → no-op (`release_awaiting_cloud.py:138`).
   - `window = get_cloud_window_count(session)` = `COUNT(state IN {PUSHING, PUSHED})` (`pipeline.py:884`); `slots = max_in_flight - window` (`release_awaiting_cloud.py:144`).
   - `candidates = get_cloud_staging_candidates(session, slots)` = oldest `AWAITING_CLOUD` `FOR UPDATE SKIP LOCKED` (`pipeline.py:898`).
   - **GATE 2**: `select_active_agent(session, kind="fileserver")` — no fileserver → hold (`release_awaiting_cloud.py:157`).
   - Per candidate: `file.state = FileState.PUSHING` then `_enqueue_push_file(push_queue, file, agent_id)` (`release_awaiting_cloud.py:167-168`), then **one** `await session.commit()` after the loop (`release_awaiting_cloud.py:173`).

3. **Post-staging callback** — `routers/agent_push.py:62` `report_pushed()`. One transaction: rowcount-guarded `UPDATE ... state=PUSHED WHERE state=PUSHING` (`agent_push.py:103-108`), `clear_ledger_entry(push_file:<id>)` (`agent_push.py:118`), `enqueue_process_file(...)` on the compute queue (`agent_push.py:122-129`). The file reaches **PUSHED** here, and stays in the ≤N window until the analysis result callback flips it to ANALYZED.

### The k8s flow — what exists vs. what this phase adds

| Step | a1 (exists) | k8s (this phase) |
|------|-------------|------------------|
| Hold long file | `AWAITING_CLOUD` (`pipeline.py:316`) | **Same** — duration router unchanged |
| Stage-side | `stage_cloud_window` → `push_file` (`release_awaiting_cloud.py:168`) | **NEW branch**: `stage_cloud_window` → `cloud_staging.stage_file_to_s3` (`services/cloud_staging.py:52`) which enqueues `s3_upload` |
| Flip to PUSHING | `release_awaiting_cloud.py:167` | **Same** — both branches flip `AWAITING_CLOUD → PUSHING` |
| Byte transport | fileserver rsync (`tasks/push.py`) | fileserver httpx multipart PUT to presigned S3 (`tasks/s3_upload.py`, Phase 53) |
| Upload-complete callback | `report_pushed` (`agent_push.py:62`) | `report_uploaded` (`agent_s3.py:58`) |
| Flip to PUSHED | `report_pushed` does it (`agent_push.py:106`) | **`report_uploaded` does NOT touch FileRecord today (`agent_s3.py:86-92` flips only cloud_job UPLOADING→UPLOADED)** — must add the FileRecord PUSHING→PUSHED flip |
| Downstream enqueue | `process_file` (`agent_push.py:122`) | **`report_uploaded` enqueues NOTHING today** — must add `submit_cloud_job` enqueue via `enqueue_router` |
| Analysis | compute agent drains `process_file` | one-shot pod runs analysis, POSTs `/api/internal/agent/analysis/{file_id}` (same callback, reconciled by `file_id`) |
| Leaves the window | analysis callback → ANALYZED | **Same** — the result callback flips PUSHED→ANALYZED; window drains identically |

### How the FileRecord reaches PUSHED on the k8s path (the precise insertion)

`report_uploaded` (`agent_s3.py:58`) currently:
- loads `cloud_job`, completes the multipart upload control-side (`agent_s3.py:81`), rowcount-guarded `UPDATE cloud_job ... UPLOADED` (`agent_s3.py:86-92`). **It never reads/writes `FileRecord`.**

The k8s integration must extend `report_uploaded` to, after the cloud_job flip, **also**:
1. Rowcount-guarded `UPDATE FileRecord SET state=PUSHED WHERE id=:fid AND state=PUSHING` (mirror `agent_push.py:103-108` for idempotency under a late/duplicate callback).
2. Enqueue `submit_cloud_job` on the **controller** queue via `enqueue_router.resolve_queue_for_task("submit_cloud_job", request.app.state, session)` (it is in `CONTROLLER_TASKS`, `enqueue_router.py:51`), with the deterministic `submit_cloud_job_key(file_id)` (`submit_cloud_job.py:44`).

**Two concrete shape problems the planner MUST handle:**
- `report_uploaded` has **no `request: Request` parameter** today (`agent_s3.py:59-64`) — it cannot reach `app.state` for the routed enqueue. Add `request: Request` (its sibling `report_upload_failed` already takes one, `agent_s3.py:108`). This keeps AUTH-01 intact (`file_id` stays on the path).
- The S3 callbacks (`agent_s3.py`) fire **only for k8s files** (a1 uses rsync). So no explicit `cloud_target == "k8s"` check is strictly required inside `report_uploaded` — but a defensive guard (only flip PUSHED + submit when `cloud_target == "k8s"`, else preserve today's cloud_job-only behavior) is recommended to avoid coupling correctness to the assumption that S3 staging is k8s-exclusive forever.

## Critical landmines (read before planning D-01a)

### Landmine 1 — `stage_file_to_s3` commits internally and releases the cron's advisory lock `[VERIFIED: codebase grep]`
`stage_cloud_window` holds `pg_advisory_xact_lock` across the **entire tick** (`release_awaiting_cloud.py:134`); the lock auto-releases at transaction end. The a1 loop flips state + enqueues, then commits **once** after the loop (`release_awaiting_cloud.py:173`). But `cloud_staging.stage_file_to_s3` ends with its **own** `await session.commit()` (`cloud_staging.py:119`). Calling it per-candidate inside the window loop would **commit mid-loop → release the advisory xact lock → release all `FOR UPDATE SKIP LOCKED` row locks → a concurrent tick could now over-stage past `cloud_max_in_flight`** (re-opening the exact T-50-scratch-dos class the lock exists to prevent). **Resolution:** extract a no-commit core from `stage_file_to_s3` (e.g. `_stage_file_to_s3(session, file, task_router)` that does the multipart init + cloud_job upsert + `s3_upload` enqueue but **defers** the commit), and have `stage_cloud_window` commit once after its loop for both branches. The existing public `stage_file_to_s3` keeps its commit for the `redrive_upload` caller (`cloud_staging.py:143`).

### Landmine 2 — GATE 1 (compute agent) is wrong for k8s `[VERIFIED: codebase grep]`
`stage_cloud_window` GATE 1 requires an online **compute** agent (`release_awaiting_cloud.py:138`) because the a1 model is "rsync to a persistent compute agent's scratch dir, which then drains `process_file`". **K8s has no persistent compute agent** — analysis runs in ephemeral Kueue pods; the cluster (not a heartbeating agent) is the consumer. On the k8s branch, GATE 1 must be **skipped** (or it would silently hold every k8s file forever, since no compute agent will ever be online in a k8s-only deploy). GATE 2 (fileserver agent) **stays** — the fileserver still owns the media mount and performs the S3 upload (`cloud_staging.py:73`). Tie-in: KDEPLOY-04 notes the cluster identity is an ephemeral Job-based identity, not a perpetually-DEAD heartbeating agent — consistent with skipping GATE 1.

### Landmine 3 — k8s backfill must NOT seed a `process_file` ledger row (CLOUDROUTE-02) `[VERIFIED: codebase grep]`
The existing a1 backfill `trigger_backfill_cloud` seeds a `process_file:<id>` scheduling-ledger row for every held file (`pipeline.py:714-727`, `insert_ledger_if_absent(... key=process_file_job_key(file.id) ...)`). For k8s, **KSUBMIT-06 / CLOUDROUTE-02 forbid this**: a `process_file:<id>` ledger row lets `recover_orphaned_work` replay the file onto a **local agent queue** — exactly the hazard Phase 54 designed around (the `cloud_job` row, not the ledger, is the k8s in-flight registry — `54-CONTEXT.md` D-02, `submit_cloud_job.py:15-18`). The k8s backfill branch must reset to `DISCOVERED` → let the duration router hold it in `AWAITING_CLOUD` → `stage_cloud_window` k8s branch picks it up, **with no ledger seed**.

### Landmine 4 — D-03 selection is ledger-*scoped*, but the current backfill is not `[VERIFIED: codebase grep]`
D-03 requires selecting only files that **carry a prior scheduling-ledger row** ("previously-scheduled work only"). The current candidate query `_backfill_candidates_stmt` filters only `ANALYSIS_FAILED ∧ duration ≥ threshold` (`pipeline.py:917-928`) — it does **not** require a ledger row. The k8s backfill needs an added `EXISTS (SELECT 1 FROM scheduling_ledger WHERE key = 'process_file:' || file.id)` (or equivalent join) so a never-scheduled failed file is not swept in — mirroring the v5.0 recover-over-enqueue fix that scoped recovery to previously-scheduled work. This is the property the KROUTE-04 "no whole-backlog enqueue" guard protects.

## D-02 — `cloud_burst_enabled` removal blast radius (every reference)

`[VERIFIED: codebase grep — grep -rn cloud_burst_enabled, excluding .planning/]`

### Production code (MUST change)
| File:line | Current | Replacement |
|-----------|---------|-------------|
| `config.py:405-409` | `cloud_burst_enabled: bool = Field(default=False, ...)` | `cloud_target: Literal["local","a1","k8s"] = Field(default="local", validation_alias=AliasChoices("PHAZE_CLOUD_TARGET","cloud_target"), ...)` (`Literal` already imported, `config.py:14`) |
| `config.py:599-615` `_enforce_s3_config_when_cloud_enabled` | `if self.cloud_burst_enabled: require s3_bucket + s3_endpoint_url` | **k8s-only**: S3 staging is the k8s byte path (a1 uses rsync, not S3). Key off `if self.cloud_target == "k8s"`. |
| `config.py:617-636` `_enforce_compute_scratch_dir_when_cloud_enabled` | `if self.cloud_burst_enabled and not self.compute_scratch_dir` | **a1-only**: `compute_scratch_dir` builds the `process_file` scratch path on the rsync→compute path. Key off `if self.cloud_target == "a1"`. |
| `config.py` (new validator) | — | **NEW** `_enforce_kube_config_when_k8s`: `if self.cloud_target == "k8s"` require `kube_api_url`, `kube_namespace`, `kube_local_queue` (D-02 pulls KDEPLOY-02's k8s portion forward). The fields exist optional today (`config.py:533-547`). |
| `tasks/release_awaiting_cloud.py:125` | `if not cfg.cloud_burst_enabled:` | `if cfg.cloud_target == "local":` |
| `routers/pipeline.py:373` | `settings.cloud_burst_enabled` (arg to router) | `settings.cloud_target != "local"` |
| `routers/pipeline.py:618` | `settings.cloud_burst_enabled` (arg) | `settings.cloud_target != "local"` |
| `routers/pipeline.py:682` | `if not settings.cloud_burst_enabled:` (backfill gate) | `if settings.cloud_target == "local":` |
| `routers/pipeline.py:710` | `settings.cloud_burst_enabled` (arg) | `settings.cloud_target != "local"` |
| `templates/pipeline/partials/backfill_response.html:15` | "Cloud burst is disabled (cloud_burst_enabled=false)…" | Update copy to `cloud_target=local` |

> **Subtlety the planner MUST get right:** the two existing validators are NOT both "cloud on" — they are per-target. `compute_scratch_dir` is the **a1** rsync-scratch concern; `s3_bucket`/`s3_endpoint_url` is the **k8s** staging concern. Splitting them by `cloud_target` (rather than a single `!= "local"` gate) is the correct, non-over-coupled rewrite. Verify with `uv run mypy .` — `Literal` comparisons are statically exhaustive, so a typo'd member fails type-check.

### Tests (MUST migrate — these will go red on the rename)
| File | What references it |
|------|--------------------|
| `tests/test_config/test_cloud_burst_toggle.py` | Entire file is the toggle's unit tests (default-False, env alias, fail-fast). Rewrite to `cloud_target` (default `"local"`, the per-target fail-fasts). |
| `tests/test_config/test_kube_settings.py:127,139` | Asserts kube_* optional / `cloud_burst_enabled is True`. Update to the new k8s fail-fast. |
| `tests/test_config/test_s3_settings.py:148,158,168,176,187` | S3 fail-fast cases keyed on `cloud_burst_enabled`. Re-key to `cloud_target=="k8s"`. |
| `tests/test_routers/test_pipeline.py:62-71,833-868` | Autouse fixture sets `cloud_burst_enabled=True`; backfill on/off cases. Re-key to `cloud_target`. |
| `tests/test_routing_seam.py:112-162` | Phase 51 gate tests (pass `cloud_enabled` bool to the router helper). The helper signature stays a bool, but the source `settings.cloud_target != "local"` — keep bool-arg tests, add a k8s case. |
| `tests/test_staging_cron.py:44-57,198-228` | `_StubCfg.cloud_burst_enabled` + `_patch_settings`. Re-key the stub to `cloud_target`; add a k8s-branch case. |

### Docs / operator surfaces (MUST migrate — "or cloud silently goes local on redeploy")
| File:line | Note |
|-----------|------|
| `docs/configuration.md:89,107,112,128` | The master-switch table row + the kube-config section + "Master toggle" subsection. Rewrite to `cloud_target`. |
| `docs/cloud-burst.md:285-300+` | "Toggle & runtime-state semantics" — `PHAZE_CLOUD_BURST_ENABLED` is **the** single-switch prose. This file IS the Phase 51 homelab runbook. Rewrite to `cloud_target` (local/a1/k8s) + add the k8s setup steps. |
| `.env.example` | `[VERIFIED]` currently contains **NO** cloud vars (164 lines, zero `CLOUD`/`KUBE` matches). D-02's "migrate `.env.example`" is really **ADD** `PHAZE_CLOUD_TARGET` (+ the kube/S3 `_FILE` vars) here. |
| `docker-compose*.yml` | `[VERIFIED]` no `cloud_burst`/`cloud_target` in `docker-compose.yml`/`.override.yml`/`.cloud-agent.yml`. Only `.cloud-agent.yml` carries cloud agent config. Adding `PHAZE_CLOUD_TARGET` to the control-plane service env is an **addition**, not a rename. |

**`.env.example.agent` does not exist** `[ASSUMED → VERIFIED: ls shows only .env.example]` — CONTEXT D-02 names it, but the repo has only `.env.example`, `docker-compose.agent.yml`, and `docker-compose.cloud-agent.yml`. `cloud_target` lives on `ControlSettings` (control plane), so the agent compose files do **not** need it. Flag this CONTEXT reference as resolved-to-absent; do not create a phantom file.

## D-04 — `cloud_phase` column + admission cards

### Migration shape (new `027`, mirror `026`) `[VERIFIED: codebase grep]`
`alembic/versions/026_add_cloud_job_kube_columns.py` is the exact template (additive, reversible, `cloud_job`-only, `[VERIFIED]` touches no `saq_jobs`). New `027_add_cloud_job_cloud_phase.py`:
- `revision = "027"`, `down_revision = "026"`.
- `upgrade()`: `op.add_column("cloud_job", sa.Column("cloud_phase", sa.String(20), nullable=True))` — nullable so existing in-flight rows backfill lazily; optionally a CHECK constraint `cloud_phase IN ('queued_behind_quota','admitted','running','finished')`. Note the existing `status` CHECK is named `status_enum` (`026:54-57`); use a distinct name like `cloud_phase_enum`.
- `downgrade()`: drop the CHECK (if added) then `op.drop_column`.
- Test: mirror `tests/test_migrations/test_migration_025_cloud_job.py` for upgrade/downgrade round-trip.

### Enum storage form (Discretion → recommend CHECK-constrained varchar, the `CloudJobStatus` precedent)
`CloudJobStatus` is a string-backed `enum.StrEnum` (`models/cloud_job.py:30`) with a DB `CheckConstraint` membership gate (`models/cloud_job.py:74-78`). **Recommendation:** add a parallel `class CloudPhase(enum.StrEnum)` with members `QUEUED_BEHIND_QUOTA="queued_behind_quota"`, `ADMITTED="admitted"`, `RUNNING="running"`, `FINISHED="finished"`, and a `Mapped[str | None]` `cloud_phase` column with its own `CheckConstraint`. String-backed means future members need only a CHECK swap, no Postgres enum-type migration (the documented design, `models/cloud_job.py:11-16,30-36`).

### Where `_reconcile_one` writes `cloud_phase` `[VERIFIED: codebase grep]`
`tasks/reconcile_cloud_jobs.py:188` `_reconcile_one` already maps Job/Workload conditions to outcomes. Add `cloud_phase` writes alongside the existing `status`/`inadmissible` writes:
- Healthy `Pending` (`reconcile_cloud_jobs.py:246`) → `cloud_phase = QUEUED_BEHIND_QUOTA`.
- `Admitted`/`QuotaReserved=True` → RUNNING branch (`reconcile_cloud_jobs.py:255-260`) → `cloud_phase = ADMITTED` then `RUNNING` (the code already advances `status` SUBMITTED→RUNNING here; co-write `cloud_phase`).
- `_record_success` (`reconcile_cloud_jobs.py:124`) → `cloud_phase = FINISHED`.
- Inadmissible (`reconcile_cloud_jobs.py:232`) stays a separate `inadmissible` flag (already exists) — `cloud_phase` is the admission *progression*, not the fault flag. Keep them orthogonal.

### Where `submit_cloud_job` seeds it `[VERIFIED: codebase grep]`
`tasks/submit_cloud_job.py:79-96` upserts the `cloud_job` row with `status=SUBMITTED`. Add `cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value` to the `pg_insert(...).values(...)` and to the `on_conflict_do_update set_` (a re-submit resets the admission progression). This is the initial seed D-04 requires.

### Dashboard cards read pattern (KROUTE-06) `[VERIFIED: codebase grep]`
Mirror the inadmissible card end to end:
- Service: add `get_cloud_phase_counts(session)` (or per-phase `_safe_count` reads) next to `get_inadmissible_count` (`services/pipeline.py:820`). Use the degrade-safe `_safe_count` helper (`pipeline.py:273`) — returns 0 on any DB error so the hot 5s poll never 500s.
- Template: new `templates/pipeline/partials/*_card.html` mirroring `inadmissible_card.html` (`[VERIFIED]` read in full): an empty `<section id="...">` carrier always emitted, `{% if oob %}hx-swap-oob="true"{% endif %}`, static autoescaped strings only (no operator free-text), body rendered only when count > 0.
- Router wiring: seed the counts in **both** `dashboard()` (`pipeline.py:507-525`) and `pipeline_stats_partial()` (`pipeline.py:565-587`) contexts, exactly like `inadmissible_count` is seeded in both (`pipeline.py:498,564`). The OOB swap on the 5s poll keeps the cards live.

## KROUTE-04 — the AST guard (extend the existing scanner) `[VERIFIED: codebase grep]`

`tests/test_no_default_queue_producers.py` is the existing static guard (`[VERIFIED]` read in full): an `ast.NodeVisitor` walks every `.py` under `src/phaze/routers` and `src/phaze/services` and fails on `*.state.queue` access or unnamed `Queue.from_url(...)`. It also has runtime assertions that every `CONTROLLER_TASKS`/`AGENT_TASKS` name routes correctly.

For KROUTE-04, extend this guard to cover the **new k8s enqueue site** (`submit_cloud_job` inside `report_uploaded`):
- **Positive routing assertion:** `submit_cloud_job` is already in `CONTROLLER_TASKS` (`enqueue_router.py:51`); the existing runtime test `test_every_controller_task_routes_to_controller_queue` already covers it. Confirm `report_uploaded`'s new enqueue uses `resolve_queue_for_task("submit_cloud_job", ...)` (not a raw `controller_queue.enqueue`).
- **Static assertion (the no-whole-backlog property):** add a check that the backfill candidate query is the **bounded, ledger-scoped** filter (`_backfill_candidates_stmt` + the new ledger-EXISTS predicate), not a bare `state == ANALYSIS_FAILED` sweep. The reconcile test already imports `ast` and asserts import boundaries (`test_reconcile_cloud_jobs.py:432-437`) — a similar AST assertion can verify the k8s enqueue sites resolve through `enqueue_router`.
- The complementary control-only/agent-free invariants for `submit_cloud_job` already exist (`test_task_split.py:190-234`).

## Architecture Patterns

### Data flow (k8s branch, the new path)

```
Discovered long file
   │  duration >= cloud_route_threshold_sec  AND  cloud_target != "local"
   ▼
_route_discovered_by_duration  ──►  FileState.AWAITING_CLOUD  (held; committed)   [pipeline.py:316]
   │
   │  stage_cloud_window  (*/5 cron, advisory-locked, ≤N window)                  [release_awaiting_cloud.py:109]
   │     branch on cfg.cloud_target:
   │        a1  ─► flip PUSHING ─► push_file (rsync)        ──► report_pushed   ─► process_file (compute q)
   ▼        k8s ─► flip PUSHING ─► stage_file_to_s3 (no-commit core) ─► s3_upload (fileserver httpx PUT)
FileState.PUSHING
   │
   │  fileserver agent PUTs bytes to presigned S3 multipart URLs                  [tasks/s3_upload.py]
   ▼
report_uploaded  ──►  complete multipart (control-side)  ──►  cloud_job UPLOADED  [agent_s3.py:58]
   │   (EXTEND): flip FileRecord PUSHING ─► PUSHED (rowcount-guarded)
   │            + enqueue submit_cloud_job via enqueue_router (controller q)
   ▼
FileState.PUSHED   +   cloud_job SUBMITTED (cloud_phase=queued_behind_quota)
   │
   │  submit_cloud_job: one suspended Kueue Job, idempotent                       [submit_cloud_job.py:55]
   │  reconcile_cloud_jobs (*/5): cloud_phase queued_behind_quota→admitted→running→finished   [reconcile_cloud_jobs.py:188]
   ▼
one-shot pod: presigned GET ─► sha256 verify ─► analyze ─► POST /api/internal/agent/analysis/{file_id}
   │   (authoritative result; reconciled by file_id; deletes S3 object inline — Phase 53 D-02)
   ▼
FileState.PUSHED ─► ANALYZED   (leaves the ≤N window; window drains identically to a1)
```

### Pattern: branch keyed on `cloud_target` inside `stage_cloud_window` (Discretion resolved)
**What:** Put the a1-vs-k8s fork in `stage_cloud_window`, NOT the duration router. **Why:** the duration router's only target-dependence is the boolean "is cloud on at all" (`is_long` gate) — it holds every cloud-routed file in `AWAITING_CLOUD` identically regardless of target. The transport choice (rsync vs S3) is a staging concern, and `stage_cloud_window` is already the single ≤N gate. Branching here keeps the window math, advisory lock, and FIFO claim shared across both targets (D-01's "ONE branch, not a parallel pipeline").

### Anti-patterns to avoid
- **Committing inside the `stage_cloud_window` candidate loop** (Landmine 1) — releases the advisory lock, breaks ≤N atomicity.
- **Requiring a compute agent for k8s** (Landmine 2) — wedges every k8s file in `AWAITING_CLOUD`.
- **Seeding a `process_file:<id>` ledger row for k8s backfill** (Landmine 3) — re-opens CLOUDROUTE-02.
- **A second parallel cron for k8s** — CONTEXT/REQUIREMENTS explicitly want ONE branch in the existing seam, not a parallel pipeline.
- **Raw `controller_queue.enqueue` in `report_uploaded`** — must route through `enqueue_router` (KROUTE-04).

## Don't Hand-Roll

| Problem | Don't build | Use instead | Why |
|---------|-------------|-------------|-----|
| K8s S3 staging | A new k8s upload task | `cloud_staging.stage_file_to_s3` (Phase 53) — extracted no-commit core | The leg is built + tested; D-01 reuses it |
| K8s submit | Inline kube POST in the callback | `submit_cloud_job` (Phase 54, `CONTROLLER_TASKS`) | Idempotent, control-only, already wired |
| Admission lifecycle | A new watch loop | Extend `reconcile_cloud_jobs._reconcile_one` | The reconcile cron already owns Job/Workload state |
| Degrade-safe card counts | Try/except in the router | `_safe_count` (`pipeline.py:273`) | Service-owns-degrade idiom; hot poll never 500s |
| Idempotent state flip | New guard logic | Rowcount-guarded `UPDATE ... WHERE state=PUSHING` (`agent_push.py:103`) | Proven idempotent-callback pattern |
| Routing safety | A new queue selector | `enqueue_router.resolve_queue_for_task` | The Phase 30 single seam; AST-guarded |
| Migration | A bespoke DDL script | Mirror `026` additive/reversible migration | Phase 53/54 precedent; `cloud_job`-scoped |

**Key insight:** Phases 52–54 deliberately built every k8s leg *standalone and unwired*. This phase is almost entirely **composition** of existing, tested seams — the new code is a handful of branch points, one column, one validator rewrite, and UI cards. Resist writing new machinery.

## Runtime State Inventory

> This is a config-rename + additive-column phase. Runtime-state audit:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `cloud_job` rows: the new `cloud_phase` column is **additive, nullable** — existing in-flight rows backfill lazily as the reconcile cron next touches them. No data migration of existing rows required. | Code edit (migration 027) only |
| Live service config | The operator env var `PHAZE_CLOUD_BURST_ENABLED` is **removed**; `PHAZE_CLOUD_TARGET` replaces it. **A redeploy that still sets the old var (or omits the new one) silently defaults to `cloud_target="local"` — cloud goes OFF.** This is the D-02 "silently goes local" risk. | Operator action at Phase 56 deploy: update env. Docs (`docs/cloud-burst.md`, `.env.example`) must make the rename loud. |
| OS-registered state | None — no Task Scheduler / launchd / systemd units reference the toggle. (`[VERIFIED]` grep found only code/docs/tests.) | None |
| Secrets / env vars | `PHAZE_CLOUD_BURST_ENABLED` → `PHAZE_CLOUD_TARGET` is a **non-secret** env rename. Kube/S3 `_FILE` secrets are unchanged (already exist from Phase 53/54, `config.py:533-579`). | Update env name; no secret value changes |
| Build artifacts | None — no compiled/installed artifact embeds the toggle name. | None |

**The canonical question — after every file is updated, what still has the old string?** Only the **operator's deployment env** (the homelab `.env` / compose env not in git). Phase 56 (KDEPLOY) owns the live redeploy; this phase makes the rename loud in `.env.example` + the runbook so the operator can't miss it.

## Common Pitfalls

### Pitfall 1: Wedged window — k8s files never leave PUSHING
**What goes wrong:** if `report_uploaded` is not extended to flip `FileRecord PUSHING → PUSHED`, k8s files sit in `PUSHING` forever. The ≤N window (`get_cloud_window_count` counts `{PUSHING, PUSHED}`, `pipeline.py:884`) never drains → after `cloud_max_in_flight` files the staging cron stages nothing and the whole k8s pipeline stalls silently.
**How to avoid:** the PUSHING→PUSHED flip in `report_uploaded` is load-bearing, not cosmetic. Test that a k8s file reaches PUSHED on upload-complete and that the window frees a slot.
**Warning signs:** `get_pushing_count` climbs to `cloud_max_in_flight` and `stage_cloud_window` logs `slots <= 0` every tick.

### Pitfall 2: Advisory-lock release via nested commit (Landmine 1 restated)
**Warning signs:** intermittent over-staging beyond `cloud_max_in_flight` under concurrent cron ticks; the `test_staging_cron.py` window-cap assertions flake.

### Pitfall 3: Breaking the live a1 path
**What goes wrong:** this is the ONE phase that edits the live v5.0 seam. A careless `cloud_burst_enabled → cloud_target` rewrite (e.g. collapsing the two per-target validators into a single `!= "local"` gate) changes a1 fail-fast semantics or the routing gate.
**How to avoid:** keep the validators per-target (a1→compute_scratch_dir, k8s→S3+kube). Keep every existing a1 test green after re-keying. The a1 path must behave **identically** to today when `cloud_target="a1"`.
**Warning signs:** `test_routing_seam.py` / `test_staging_cron.py` a1 regressions; a1 push files routing local.

### Pitfall 4: Over-enqueue recurrence (v4.0.6 / v5.0)
**What goes wrong:** the k8s backfill sweeps the whole `ANALYSIS_FAILED` backlog (not ledger-scoped) or seeds a `process_file` ledger row that `recover_orphaned_work` later replays onto a local queue.
**How to avoid:** Landmines 3 & 4 — ledger-scoped selection + no `process_file` seed for k8s; the KROUTE-04 AST guard locks it in.
**Warning signs:** a "Backfill to K8s" click enqueues far more than the failed-long set; k8s files appear on `phaze-agent-<id>` queues.

### Pitfall 5: `report_uploaded` can't reach `app.state`
**What goes wrong:** adding the `submit_cloud_job` enqueue without adding `request: Request` to `report_uploaded` → no way to reach `app.state.controller_queue` through the router.
**How to avoid:** add `request: Request` (sibling `report_upload_failed` already has it, `agent_s3.py:108`).

## Code Examples (verified patterns from this codebase)

### Idempotent rowcount-guarded state flip (the PUSHED flip to add in report_uploaded)
```python
# Source: src/phaze/routers/agent_push.py:103-117 (report_pushed) — mirror for the k8s PUSHED flip
res = cast(
    "CursorResult[Any]",
    await session.execute(
        update(FileRecord)
        .where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING)
        .values(state=FileState.PUSHED)
    ),
)
if res.rowcount == 0:
    await session.commit()  # already advanced: idempotent no-op, no re-enqueue
    return ...
```

### Routed controller enqueue (the submit_cloud_job enqueue to add)
```python
# Source: src/phaze/routers/pipeline.py:420 (trigger_proposals) — the routed-enqueue idiom
routed = await enqueue_router.resolve_queue_for_task("submit_cloud_job", request.app.state, session)
await routed.queue.enqueue("submit_cloud_job", key=submit_cloud_job_key(file_id), file_id=str(file_id))
```

### Degrade-safe card count (the cloud_phase card reads)
```python
# Source: src/phaze/services/pipeline.py:820-840 (get_inadmissible_count) — mirror per phase
return await _safe_count(
    session,
    select(func.count(CloudJob.id)).where(CloudJob.cloud_phase == CloudPhase.RUNNING.value),
    node="cloud_phase_running",
)
```

## State of the Art

| Old approach | Current approach | When changed | Impact |
|--------------|------------------|--------------|--------|
| `cloud_burst_enabled: bool` master toggle | `cloud_target: Literal["local","a1","k8s"]` single source of truth | This phase (D-02) | One setting selects off/a1/k8s; no layered toggle |
| a1 = only cloud target | a1 + k8s coexist, selected by `cloud_target` | This phase | K8s is additive, not a replacement (REQUIREMENTS out-of-scope) |
| `cloud_job` tracks staging + submit lifecycle | + `cloud_phase` admission progression | This phase (D-04) | Operator sees queued-behind-quota / admitted / running / finished |

**Deprecated/outdated by this phase:**
- `PHAZE_CLOUD_BURST_ENABLED` env var — removed, no alias. Operators must set `PHAZE_CLOUD_TARGET`.

## Validation Architecture

> `workflow.nyquist_validation` is `true` (`.planning/config.json`). Test infra: ephemeral Postgres on **5433** + Redis **6380** (justfile:5,9; `TEST_DATABASE_URL` default 5432 for in-process, 5433 for integration). Kube is faked via `tests/kube_fakes.py` (Layer-1 logic fakes, ZERO HTTP) + monkeypatched `kube_staging`. No live cluster needed.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| Quick run command | `uv run pytest tests/test_routing_seam.py tests/test_staging_cron.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (85% min, CLAUDE.md) |
| Kube fake | `tests/kube_fakes.py` (`PENDING`/`INADMISSIBLE`/`ADMITTED`/`EVICTED` workloads, `fake_job`) |
| Queue fakes | `tests/_queue_fakes.py` (`DedupFakeTaskRouter`, `DedupFakeQueue`, `seed_active_agent`) |

### Phase Requirements / Decisions → Test Map
| Req / D | Critical transition to assert | Test type | Command / file |
|---------|-------------------------------|-----------|----------------|
| KROUTE-01 / D-02 | `cloud_target="local"` → long file routes local (no AWAITING_CLOUD); `"a1"`/`"k8s"` → held | unit | `tests/test_routing_seam.py` (re-key from bool), `tests/test_config/test_cloud_target.py` (new, ❌ Wave 0) |
| KROUTE-01 / D-02 | per-target fail-fast: `"a1"` requires compute_scratch_dir; `"k8s"` requires s3_bucket+endpoint+kube_* | unit | `tests/test_config/test_s3_settings.py`, `test_kube_settings.py` (re-key) |
| KROUTE-02 / D-01a | `stage_cloud_window` k8s branch flips PUSHING + enqueues `s3_upload` (not `push_file`); GATE 1 skipped for k8s | unit | `tests/test_staging_cron.py` (add k8s case, ❌ Wave 0) |
| KROUTE-02 | ≤N window not over-staged under concurrent ticks WITH the k8s (nested-commit-free) branch | integration | `tests/test_staging_cron.py` advisory-lock case (5433 DB) |
| KROUTE-03 / D-01b | `report_uploaded` flips FileRecord PUSHING→PUSHED + enqueues `submit_cloud_job`; idempotent on duplicate callback | unit | `tests/test_routers/test_agent_s3.py` (extend, ❌ Wave 0) |
| KROUTE-03 | k8s file traverses AWAITING_CLOUD→PUSHING→PUSHED→ANALYZED, no new FileState | integration | new seam test (❌ Wave 0) |
| KROUTE-04 | every k8s enqueue site routes through `enqueue_router`; no whole-backlog enqueue | static (ast) | `tests/test_no_default_queue_producers.py` (extend) |
| KROUTE-05 / D-03 | backfill selects only `analysis_failed ∧ duration≥threshold ∧ has ledger row`; resets DISCOVERED; **no `process_file` seed for k8s** | unit | `tests/test_routers/test_pipeline.py` backfill cases (extend, ❌ Wave 0) |
| D-04 | migration 027 upgrade/downgrade round-trip | unit | `tests/test_migrations/test_migration_027_cloud_phase.py` (new, ❌ Wave 0) |
| D-04 | `submit_cloud_job` seeds `cloud_phase=queued_behind_quota`; `_reconcile_one` writes admitted/running/finished | unit | `tests/test_tasks/test_submit_cloud_job.py`, `test_reconcile_cloud_jobs.py` (extend) |
| KROUTE-06 | `get_cloud_phase_counts` degrade-safe (returns 0 on DB error); card renders empty when 0 | unit | `tests/test_services/test_pipeline*.py` + a card render test (❌ Wave 0) |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_routing_seam.py tests/test_staging_cron.py tests/test_no_default_queue_producers.py -x` (sub-30s).
- **Per wave merge:** `uv run pytest tests/test_config tests/test_routers tests/test_tasks tests/test_services tests/test_migrations`.
- **Phase gate:** `uv run pytest --cov` green (85%) + `uv run ruff check .` + `uv run mypy .` + `pre-commit run --all-files` before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/test_config/test_cloud_target.py` — replaces `test_cloud_burst_toggle.py`; default `"local"`, per-target fail-fasts (KROUTE-01/D-02).
- [ ] `tests/test_staging_cron.py` k8s-branch case — `cloud_target="k8s"` enqueues `s3_upload`, skips GATE 1, no nested-commit over-stage (KROUTE-02/D-01a).
- [ ] `tests/test_routers/test_agent_s3.py` k8s post-staging case — PUSHED flip + `submit_cloud_job` enqueue + idempotency (KROUTE-03/D-01b).
- [ ] `tests/test_routers/test_pipeline.py` backfill-to-k8s case — ledger-scoped selection, no process_file seed (KROUTE-05/D-03).
- [ ] `tests/test_migrations/test_migration_027_cloud_phase.py` — additive/reversible round-trip (D-04).
- [ ] A full-traversal seam test — AWAITING_CLOUD→PUSHING→PUSHED→ANALYZED for k8s (KROUTE-03).
- [ ] Re-key existing `cloud_burst_enabled` tests (`test_s3_settings.py`, `test_kube_settings.py`, `test_pipeline.py`, `test_routing_seam.py`, `test_staging_cron.py`) to `cloud_target`.

## Security Domain

> `security_enforcement` not set in config → treat as enabled. This phase adds no new external attack surface (no new endpoints exposed to untrusted clients; the extended `report_uploaded` stays token-authed with `file_id` on the path, AUTH-01).

### Applicable ASVS categories
| ASVS | Applies | Standard control (already in place) |
|------|---------|-------------------------------------|
| V4 Access Control | yes | `report_uploaded`/backfill stay token-authed (`get_authenticated_agent`); `file_id` on path, agent from token (AUTH-01, `agent_s3.py:24`) |
| V5 Input Validation | yes | Pydantic schemas `extra="forbid"`; `cloud_target` is a `Literal` (rejects invalid members at construction); `file_id` is `uuid.UUID` typed |
| V6 Cryptography | no | No new crypto; S3 presign + sha256 verify unchanged (Phase 53) |
| V9 Communications | yes | Pod→control callback over the baked internal CA (no `verify=False`, KJOB-05, Phase 52) — unchanged here |

### Known threat patterns for this stack
| Pattern | STRIDE | Mitigation (in place) |
|---------|--------|-----------------------|
| Whole-backlog over-enqueue (resource exhaustion) | DoS | Ledger-scoped bounded backfill + deterministic keys + KROUTE-04 AST guard (Landmines 3/4) |
| Misrouted enqueue to consumer-less default queue | DoS (silent loss) | `enqueue_router` single seam; static guard `test_no_default_queue_producers.py` |
| SQL injection via threshold/target | Tampering | Bound parameters / ORM only (`pipeline.py:917-928`); no f-string SQL (T-49-02) |
| Secret leak (kube/S3 creds) | Info disclosure | `_FILE` SecretStr, control-plane only, never logged (`config.py:568-579`); agent import-boundary guard (`test_task_split.py`) |

## Assumptions Log

| # | Claim | Section | Risk if wrong |
|---|-------|---------|---------------|
| A1 | `.env.example.agent` (named in CONTEXT D-02) does not exist; only `.env.example` does, and `cloud_target` (ControlSettings) doesn't belong on agent compose | D-02 blast radius | Low — verified by `ls`; planner should confirm no agent-side cloud_target is needed |
| A2 | The S3 callbacks (`agent_s3.py`) fire only for k8s files, so a `cloud_target` check in `report_uploaded` is defensive-only | D-01b | Low — a1 uses rsync (`push_file`), never S3; verified by call graph. A defensive guard is recommended regardless |
| A3 | `cloud_phase` CHECK-constrained varchar (vs StrEnum-native) is the right form | D-04 | Low — explicitly the `CloudJobStatus` precedent CONTEXT points to |
| A4 | GATE 1 (compute agent) should be fully skipped (not replaced by a kube-reachability probe) on the k8s branch in this phase | Landmine 2 | Medium — KDEPLOY-04's startup LocalQueue-reachability check is Phase 56; if the planner wants an in-phase k8s liveness gate, that's a scope call. Recommend: skip GATE 1 here, defer reachability to Phase 56 |

## Open Questions

1. **Should the k8s branch keep GATE 2 (fileserver) exactly as a1, or also tolerate a fileserver-less k8s deploy?**
   - What we know: the fileserver owns the media mount and performs the S3 upload (`cloud_staging.py:73`), so it IS required for the k8s byte path.
   - Recommendation: keep GATE 2 unchanged for both targets.

2. **Does the "Backfill to K8s" action reuse the existing `/pipeline/backfill-cloud` endpoint (branching on `cloud_target`) or add a distinct endpoint?**
   - What we know: the existing endpoint (`pipeline.py:657`) already does the reset+route; only the ledger-scope filter and the no-process_file-seed fork differ for k8s.
   - Recommendation: branch the existing endpoint on `cloud_target` (one surface, mirrors D-01's "one branch" philosophy) — but the planner may prefer a separate button/endpoint for operator clarity. Either satisfies D-03; flag for the planner.

3. **`cloud_phase` for a1 files:** a1 files also pass through PUSHING/PUSHED but have no Kueue admission phase. Should `cloud_phase` stay NULL for a1, or is it k8s-only?
   - Recommendation: `cloud_phase` is k8s-only (it's a Kueue admission concept); leave NULL for a1. The cards count only non-NULL phases.

## Environment Availability

> Mostly code/config changes. External dependencies are deploy-phase (Phase 56) concerns; this phase is testable entirely against fakes.

| Dependency | Required by | Available for THIS phase | Fallback |
|------------|-------------|--------------------------|----------|
| Live Kueue cluster | Runtime k8s analysis | ✗ (not needed) | `tests/kube_fakes.py` + monkeypatched `kube_staging` (Phase 54 precedent) |
| S3 backend | Runtime staging | ✗ (not needed) | moto/botocore stubber (Phase 53 precedent) |
| Postgres (test) | Integration tests | ✓ 5433 (`just test-db`) | — |
| Redis (test) | Integration tests | ✓ 6380 | — |
| `kr8s`, `aioboto3` | Code imports | ✓ installed (Phase 52-54) | — |

**No blocking missing dependencies** — every external system is faked for this phase's tests.

## Sources

### Primary (HIGH confidence) — live codebase `[VERIFIED: codebase grep]`
- `src/phaze/tasks/release_awaiting_cloud.py` — `stage_cloud_window` window math + advisory lock + gates (D-01a fork point)
- `src/phaze/routers/agent_push.py` — `report_pushed` (a1 post-staging, the PUSHED-flip template)
- `src/phaze/routers/agent_s3.py` — `report_uploaded` (k8s post-staging callback to extend) / `report_upload_failed`
- `src/phaze/services/cloud_staging.py` — `stage_file_to_s3` (internal commit = Landmine 1) / `redrive_upload`
- `src/phaze/tasks/submit_cloud_job.py` / `reconcile_cloud_jobs.py` — Phase 54 producer + reconcile (`_reconcile_one` for cloud_phase)
- `src/phaze/services/enqueue_router.py` — `CONTROLLER_TASKS`/`AGENT_TASKS`/`resolve_queue_for_task`
- `src/phaze/config.py` — `cloud_burst_enabled` field (405) + per-target validators (599/617) + kube fields (533-579)
- `src/phaze/routers/pipeline.py` — `_route_discovered_by_duration` (255), `trigger_backfill_cloud` (657), card wiring (498-587)
- `src/phaze/services/pipeline.py` — window/count helpers (`get_cloud_window_count` 884, `get_inadmissible_count` 820, `_safe_count` 273, backfill candidates 917)
- `src/phaze/models/cloud_job.py` — `CloudJob`/`CloudJobStatus` (cloud_phase precedent)
- `alembic/versions/025_*.py`, `026_*.py` — migration template for 027
- `tests/test_no_default_queue_producers.py`, `test_task_split.py`, `test_routing_seam.py`, `test_staging_cron.py`, `kube_fakes.py`, `test_tasks/test_reconcile_cloud_jobs.py` — the guard/validation patterns
- `templates/pipeline/partials/inadmissible_card.html`, `backfill_response.html` — card/copy patterns

### Secondary (project docs)
- `.planning/phases/55-.../55-CONTEXT.md` (D-01..D-04), `54-CONTEXT.md`, `53-CONTEXT.md`, `REQUIREMENTS.md` (KROUTE-01..06)
- `docs/configuration.md`, `docs/cloud-burst.md` (the operator surfaces to migrate)

## Metadata

**Confidence breakdown:**
- Call graph / fork points (D-01): HIGH — traced end to end with file:line; both fork points are physically distinct callbacks already.
- Config blast radius (D-02): HIGH — exhaustive grep; the per-target validator split is the one subtlety, flagged.
- Backfill / ledger scoping (D-03): HIGH for the hazard (CLOUDROUTE-02 forbids the process_file seed); MEDIUM on endpoint-vs-new-action (Open Q2, planner's call).
- cloud_phase migration + cards (D-04): HIGH — direct mirror of 026 + inadmissible_card.
- Pitfalls/landmines: HIGH — Landmines 1-3 are verified structural facts (internal commit, compute-gate, ledger seed), not speculation.

**Research date:** 2026-06-28
**Valid until:** ~2026-07-28 (stable; codebase-internal, no fast-moving external deps)
