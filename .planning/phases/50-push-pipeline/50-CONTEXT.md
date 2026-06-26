# Phase 50: Push pipeline - Context

**Gathered:** 2026-06-25
**Status:** Ready for planning

<domain>
## Phase Boundary

Make a cloud-routed long file physically reach the compute agent's local scratch disk and get analyzed, then cleaned up. A **file-server agent pushes** a cloud-routed file to the **compute agent's scratch directory** over rsync/SSH-over-Tailscale (file-server initiates; compute agent only receives into scratch). The compute agent **verifies sha256** against the `FileRecord` after transfer before analyzing, **deletes its scratch copy** after analysis (success or terminal failure), and the **control plane keeps the pipeline "one ahead"** — at most a configurable number of cloud files staged-or-in-flight (default 2 = one analyzing + one staged), driven by the Phase 45/49 scheduling ledger, with idempotent re-drive of failed/interrupted work and **no orphaned scratch files and no double-enqueues**.

Requirements: CLOUDPIPE-01..05. **Depends on Phase 49** (duration routing must place files on the cloud queue first) and Phase 48 (`kind="compute"` agent), Phase 45 (scheduling ledger), Phase 47 (arm64 essentia image).

**Out of scope:** object storage / presigned-URL staging (v5.0 chose rsync push, NO object storage); the cloud-agent compose/deploy + Tailscale ACL + config docs (Phase 51, CLOUDDEPLOY-*); cost/throughput-aware routing (CLOUDROUTE-05).

</domain>

<decisions>
## Implementation Decisions

### Control loop & pipeline shape (CLOUDPIPE-01, -05)
- **D-01:** A cloud file flows through **two SAQ stages**: a new **`push_file`** task on the **file-server agent's** queue (rsyncs the file to compute scratch) that, on success, enqueues **`process_file`** on the **compute agent's** queue (which now reads the scratch copy). Each stage is a ledger-tracked, deterministic-keyed job — clean separation, idempotent per-stage re-drive. Mirrors the existing per-stage agent-task model.
- **D-02:** **"Stay one ahead" is driven by a single controller cron** (modeled on Phase 49's `release_awaiting_cloud` `*/5`). The cron counts current staged + in-flight cloud files (from state/ledger) and enqueues `push_file` for the next eligible file(s) until the window (≤N, default 2) is full. **Single driver, recovery-only-compatible** (respects the Phase-42 "no general auto-advance cron" principle — this cron is scoped only to topping-up the bounded cloud window and is gated on a compute agent being online). No completion-chaining hooks.
- **D-03:** **Window size N is a config knob, default 2** (= one analyzing + one staged). Follow the established `*_threshold`/`*_sec` settings convention (e.g. `cloud_max_in_flight: int` default 2). The bound must **never be exceeded** — it is the load-bearing invariant that prevents scratch-disk blowup (e.g. "Run analysis" on the 144-file backlog must not push 144 files at once).
- **D-04:** **Compute agent offline mid-window → hold & resume.** In-flight ledger rows stay; the file remains in its cloud state. When a compute agent is seen again, ledger-driven recovery re-drives push/analyze. **Never fall back to local analysis** (long files time out locally — Phase 49's load-bearing safety invariant). Scratch on a dead agent is reconciled by that agent's startup janitor (D-14) when it returns.

### Push transport & target wiring (CLOUDPIPE-02)
- **D-05:** **Static config on the file-server** supplies the push target and SSH identity: `push_ssh_host` (compute agent's Tailscale name), `push_ssh_user`, `push_ssh_key` (via `_FILE` secret), `push_scratch_dir`. Simplest for the single-A1 milestone; matches CLOUDDEPLOY-02's "push SSH target" knob. (Dynamic-from-heartbeat multi-agent discovery deferred.)
- **D-06:** Push runs **rsync over SSH** via an asyncio subprocess (first rsync/SSH usage in the codebase — no precedent), using **`--partial-dir` (or temp-name) + atomic rename** so the compute agent never sees a half-written file at the final scratch path. rsync's own checksum guards the wire; app-level sha256 verify still runs compute-side before analysis (defense in depth). Resumable. Exact remaining flags (`--inplace`, `--timeout`, compression) are Claude's discretion within the atomicity + integrity goal.
- **D-07:** **Pinned known_hosts (strict).** The compute agent's host key is operator-provisioned into a known_hosts file (mounted via `_FILE`-style secret); ssh uses `StrictHostKeyChecking=yes`. Tailscale already authenticates the network path; one-time setup belongs in the Phase 51 CLOUDDEPLOY-03 runbook.

### In-flight state model & observability (CLOUDPIPE-01)
- **D-08:** Add two **new `FileState` members: `PUSHING`** (rsync in progress) and **`PUSHED`** (on compute scratch, awaiting/within analysis). `FileState` is a code-only StrEnum over `String(30)` → **no migration** (Phase 49 `AWAITING_CLOUD` precedent). Explicit states make the dashboard honest and let the cron count the window directly from state.
- **D-09:** **Two new dashboard count cards** — "Staged (pushing)" and "Analyzing (cloud)" — reusing the Phase 49 `_safe_count` + count-card pattern, alongside the existing "Awaiting cloud" card. Makes the "one ahead" window visible at a glance. Click-through to per-file lists deferred (consistent with Phase 49).
- **D-10:** `PUSHING`/`PUSHED` must be wired into the recovery/reenqueue **domain-completed predicate** correctly: they are **not terminal/done** (the file still needs analysis), so they remain eligible for re-drive. `process_file` "done" stays `{ANALYZED, ANALYSIS_FAILED}`.

### Integrity verification & scratch cleanup (CLOUDPIPE-03, -04, -05)
- **D-11:** **Expected sha256 travels in the `ProcessFilePayload`.** The control plane already builds `process_file`'s payload and has `FileRecord.sha256_hash`; include `expected_sha256` and the scratch path. No extra internal-API round-trip; the value is pinned at enqueue time. (The compute agent has no direct ORM — it reaches Postgres for the queue and the API on :8000, but reading FileRecord fields via payload avoids an internal-API fetch.) The compute `process_file` reads the scratch copy (ephemeral) instead of `original_path` — payload must carry a flag/scratch path distinguishing the compute-scratch read from the file-server's local-mount read.
- **D-12:** On **sha256 mismatch** (corrupt/incomplete transfer): the compute agent **fails the job cleanly and deletes the bad scratch file**; the control plane **re-drives the push** (`push_file` again) up to a **configurable max attempts** (default ~3). After the cap, mark **`ANALYSIS_FAILED`** so it surfaces instead of looping forever. (Re-push attempt-counter storage — ledger payload vs FileRecord column vs SAQ retries — is Claude's discretion.)
- **D-13:** **Scratch cleanup in `process_file`'s `finally` block** (success OR terminal failure), bounding compute disk to the in-flight set.
- **D-14:** **Compute-agent startup janitor** sweeps scratch files left by a killed/interrupted worker (the "no orphaned scratch files" criterion). Safe because the window is small and any still-needed file is re-pushed on demand by the cron. This is the reconciliation path for D-04 (agent that died mid-window) and a hard-killed worker that skipped its `finally`.

### Claude's Discretion
- **Routing seam (Phase 49 integration):** how Phase 50 replaces Phase 49's "enqueue `process_file` directly to the compute queue when a compute agent is online" path — Claude picks between funneling **all** cloud-routed long files through a cloud-pending state + the staging cron (single entry, simplest invariant) vs a fast-path that enqueues `push_file` immediately when the window has room (lower first-file latency, two paths sharing one bounded-enqueue guard). **Hard constraint: the ≤N window is never exceeded** — a direct-to-compute enqueue that bypasses the push step or the window bound is a bug.
- Re-push attempt-counter storage location; eligibility ordering for which file the cron stages next (e.g. FIFO by discovery / oldest cloud-pending first); exact rsync flags beyond the atomicity + integrity requirements; config knob names/defaults (convention match to `cloud_route_threshold_sec`).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase scope & requirements
- `.planning/ROADMAP.md` §"Phase 50: Push pipeline" — goal, 5 success criteria, dependency on Phase 49.
- `.planning/REQUIREMENTS.md` — CLOUDPIPE-01..05 (in scope); CLOUDDEPLOY-* (Phase 51, out of scope); CLOUDROUTE-05 (deferred).
- `.planning/phases/49-duration-routing-backfill/49-CONTEXT.md` — the routing/ledger decisions this phase builds on (`AWAITING_CLOUD`, kind-filtered selection, release cron, deterministic-key dedup, "never analyze a long file locally" invariant).

### Two-stage flow: routing, queues, ledger (primary change surface)
- `src/phaze/services/enqueue_router.py` — `resolve_queue_for_task`, `select_active_agent(session, kind=)` (Phase 49 D-13), `RoutedQueue`, `CONTROLLER_TASKS`/`AGENT_TASKS` (register `push_file`).
- `src/phaze/services/agent_task_router.py` — `AgentTaskRouter.queue_for(agent_id)`, `enqueue_for_agent(...)` (per-agent queue, deterministic key) — how `push_file` reaches the file-server and `process_file` reaches the compute agent.
- `src/phaze/services/scheduling_ledger.py` — `routing_for_function`, `upsert_ledger_entry`, `insert_ledger_if_absent`, `clear_ledger_entry`.
- `src/phaze/models/scheduling_ledger.py` — `SchedulingLedger` row shape (key/function/routing/payload/timeout/retries).
- `src/phaze/tasks/_shared/deterministic_key.py` — `_KEY_BUILDERS` (add a `push_file:<file_id>` builder; keeps re-drive idempotent at the `before_enqueue` chokepoint).
- `src/phaze/tasks/reenqueue.py` — `recover_orphaned_work`, `_DOMAIN_COMPLETED_STAGES` (classify `PUSHING`/`PUSHED` as pending).
- `src/phaze/tasks/release_awaiting_cloud.py` — the Phase 49 cron pattern to model the new staging/top-up cron on (`*/5`, gated on online compute agent).

### Analyze task & payload
- `src/phaze/tasks/functions.py` — `process_file` (reads `payload.original_path`; extend to read the ephemeral scratch path + verify `expected_sha256`; add scratch cleanup `finally`).
- `src/phaze/services/analysis.py` — `analyze_file(...)` windowed analysis (Phase 31); already path-agnostic — reads whatever path it's given.
- The `ProcessFilePayload` schema (in the tasks/payload module) — add `expected_sha256`, scratch-path, and ephemeral flag.
- `src/phaze/tasks/agent_worker.py` — agent SAQ worker startup (register `push_file`; wire the compute-agent startup scratch janitor here).

### State model & dashboard
- `src/phaze/models/file.py` — `FileState` StrEnum (add `PUSHING`, `PUSHED`); `FileRecord` (`sha256_hash`, `original_path`, `current_path`, `state String(30)`, `agent_id`).
- `src/phaze/models/metadata.py` — `FileMetadata.duration` (Float, nullable, seconds).
- `src/phaze/models/agent.py` — `Agent.kind`, `last_seen_at`, `revoked_at`, `last_status`.
- `src/phaze/routers/pipeline.py` — `trigger_analysis` / `_enqueue_analysis_jobs` (the routing seam to reshape), dashboard count surfacing (add the two new cards).
- `src/phaze/services/pipeline.py` — `get_files_by_state`, `_safe_count`, count helpers.

### Internal agent API (token auth)
- `src/phaze/routers/agent_analysis.py` — `PUT /api/internal/agent/analysis/{file_id}` (existing result PUT + ledger clear); reference for any new internal endpoint if needed.

### Config
- `src/phaze/config.py` — `ControlSettings` (`cloud_route_threshold_sec` ~L365 as the template for `cloud_max_in_flight`); `AgentSettings` (`kind`, agent-side analysis knobs); the `_FILE`-secret convention (`SECRET_FILE_FIELDS`, `_resolve_secret_files`) for `push_ssh_key` / known_hosts; add `push_ssh_host`/`push_ssh_user`/`push_scratch_dir`/`push_max_attempts`/`compute scratch_path`.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `release_awaiting_cloud` cron (Phase 49): the exact template for the new staging/top-up cron — `*/5`, gated on `select_active_agent(kind="compute")`, returns `{count, skipped}`.
- `select_active_agent(session, kind=)`: select the file-server (`kind="fileserver"`) to run `push_file` and the compute agent (`kind="compute"`) as the push target — no rewrite, just the existing kind filter.
- `insert_ledger_if_absent` + deterministic-key dedup: idempotent per-stage re-drive with no double-enqueue (a double-click / double-cron-tick collapses to a no-op).
- `_safe_count` + count-card pattern: drop-in for the two new "Staged" / "Analyzing (cloud)" cards.
- `process_file` + windowed `analyze_file`: analysis is already path-agnostic, so reading from scratch needs only a payload path swap, not analyzer changes.
- pebble `run_in_process_pool` (Phase 43) + `analysis_inner_timeout_sec`: the compute agent's existing CPU-isolation + timeout machinery is unchanged.

### Established Patterns
- Control-side enqueue routes through a single chokepoint (`resolve_queue_for_task`) that never targets the consumer-less default queue — `push_file` must register and route the same way.
- `FileState` is a code-only StrEnum over `String(30)` → new states need no migration (precedent: `ANALYSIS_FAILED`, `AWAITING_CLOUD`).
- Recovery/reenqueue is ledger-scoped + domain-completed-predicate gated; any new pending state must be classified as pending, not done.
- Threshold/limit settings: `Field` + `PHAZE_*` `AliasChoices`; secrets via `<VAR>_FILE`.
- No subprocess/rsync/ssh precedent exists in `src/` — `push_file` introduces it (use `asyncio.create_subprocess_exec`, not shell, to avoid injection).

### Integration Points
- `trigger_analysis` / `_enqueue_analysis_jobs` (pipeline router) — the routing seam: cloud-routed long files must funnel into the bounded window rather than enqueue `process_file` directly.
- New staging/top-up cron (controller) — the single "stay one ahead" driver.
- New `push_file` agent task (file-server) — rsync subprocess + on-success enqueue of `process_file`.
- `process_file` (compute) — scratch read, sha256 verify, cleanup `finally`.
- Compute-agent worker startup — scratch janitor.
- `config.py` — push/SSH/scratch/window settings.
- Pipeline dashboard — two new count cards.

</code_context>

<specifics>
## Specific Ideas

- "File-server initiates; compute agent only receives into scratch" is a hard directional invariant (CLOUDPIPE-02) — the compute agent never reaches back to pull.
- The ≤N (default 2) staged-or-in-flight bound is the load-bearing safety property: it prevents the 144-file backfill from blowing up the compute scratch disk. Every enqueue path into the compute pipeline must respect it.
- Defense in depth on integrity: rsync wire checksum + atomic rename (no half-files) + app-level sha256 verify before analysis.
- v5.0 explicitly chose rsync push over object storage — the older "upload→object-storage→presigned-URL→reconcile" sketch (an earlier research memory) is NOT the architecture for this milestone.

</specifics>

<deferred>
## Deferred Ideas

- Dynamic compute-agent target discovery via heartbeat `last_status` (multi/rotating compute agents) — static config is sufficient for the single-A1 milestone (D-05).
- Cloud-agent compose, Tailscale ACL, least-privilege Postgres queue role, and config/runbook docs — Phase 51 (CLOUDDEPLOY-01..04).
- Click-through drill-down lists for the new cloud count cards — count-only for now (consistent with Phase 49).
- Cost/throughput-aware routing beyond the fixed duration threshold — CLOUDROUTE-05, out of scope this milestone.
- Round-robin / least-loaded dispatch among multiple compute agents — most-recently-seen kind-filtered selection is sufficient.

None of the above are blockers; discussion stayed within phase scope.

</deferred>

---

*Phase: 50-push-pipeline*
*Context gathered: 2026-06-25*
