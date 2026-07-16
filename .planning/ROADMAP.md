# Roadmap: Phaze

## Milestones

- ✅ **v1.0 MVP** — Phases 1-11 (shipped 2026-03-30)
- ✅ **v2.0 Metadata Enrichment & Tracklist Integration** — Phases 12-17 (shipped 2026-04-02)
- ✅ **v3.0 Cross-Service Intelligence & File Enrichment** — Phases 18-23 (shipped 2026-04-04)
- ✅ **v4.0 Distributed Agents** — Phases 24-29 (shipped 2026-05-17)
- ✅ **v5.0 Cloud Burst Analysis** — Phases 47-51 (shipped 2026-06-26)
- ✅ **v6.0 Kubernetes Burst Analysis** — Phases 52-56 (shipped 2026-06-29)
- ✅ **v7.0 UI Redesign (DAG-Centric Hybrid Console)** — Phases 57-62 (shipped 2026-07-02)
- ✅ **2026.7.0 Engineering Improvements** — Phases 63-66 (shipped 2026-07-03)
- ✅ **2026.7.1 Multi-Cloud Backends** — Phases 67-71 (shipped 2026-07-05)
- ✅ **2026.7.2 Multi-Compute Agents (N Cloud-Compute Backends)** — Phases 72-76 (shipped 2026-07-06)
- ✅ **2026.7.5 Parallel Enrich DAG (Retire Linear `FileState`)** — Phases 77-92 (shipped 2026-07-14)
- 🚧 **2026.7.7 Console & Cloud-Burst Hardening** — Phases 93-102 (in progress)

## Phases

### 🚧 2026.7.7 Console & Cloud-Burst Hardening (Active)

**Milestone Goal:** Harden the shipped 2026.7.5 DAG-console system — fix the console correctness bugs, make the multi-Kueue compute lanes surface truthfully (and fix a possible functional cloud-drain stall), make cloud-analysis pods observable, and pay down the Alembic migration-chain debt. **Zero new dependencies, no new product features** — all changes are over existing routers / services / Jinja templates + the cloud job-runner. Prod is at Alembic `039`.

Granularity is **fine** — small, single-seam phases with tight blast radius; **each phase ships as its own PR on a worktree branch, never direct to main.** Phase numbering **continues from 92 (starts at Phase 93)** — NOT reset. 15 requirements mapped 1:1 (0 orphans, 0 duplicates). The work is four independent clusters plus one shared root: the **multi-Kueue per-cluster liveness derivation (Phase 96)** feeds both the compute-lane surfacing (Phase 97) and the cloud-drain hold fix (Phase 98). The three OBS pod-observability fixes stay isolated (OBS-03's subprocess execution-model change is the riskiest, its own phase). The MIG flatten is fully self-contained engineering debt.

**Phases:**

- [ ] **Phase 93: Console Derived-Status Truthfulness** — the file detail slide-in Stage-Eligibility pills + the left-rail stage badges surface the real derived per-stage status/in-flight counts, reusing the same `stage_status` the Files matrix already renders (CONSOLE-01, CONSOLE-02)
- [ ] **Phase 94: Detail Pop-Out Close/Dismiss Fix** — the X/close control fully dismisses the detail pop-out on BOTH the Agents detail and the Analyze-lane detail (the HTMX-swap / Alpine-global-scope trap that today only removes the X icon); fix once, apply to both (CONSOLE-03)
- [ ] **Phase 95: Analyze-View Browser-Slowdown Investigation & Fix** — diagnose and fix the Analyze workspace severely slowing/hanging the browser (client-side render/poll cost vs the `/pipeline/stats` 200K poll); investigate-then-fix (CONSOLE-04)
- [ ] **Phase 96: Per-Cluster Kueue Liveness Derivation & Agents-Page Identity** — model each Kueue cluster (vox/xenolab) as a live per-cluster ephemeral identity derived from in-flight Kueue workloads (ACTIVE not `NEVER`), reconciling the single generic "k8s burst" lane; the shared derivation core (COMPUTE-01)
- [ ] **Phase 97: Compute-Lane Surfacing — Header Count & Per-File Lane Label** — the header agent count includes every active compute lane, and each file's lane is labeled with its real `backend_id`-derived cluster, killing the stale `☁ A1` label; consumes Phase 96's derivation (COMPUTE-02, COMPUTE-03)
- [ ] **Phase 98: Cloud-Drain Liveness-Gate Investigation & Fix** — diagnose whether the drain dispatch falsely gates on the heartbeat-liveness compute agents never emit (stranding the backlog), fix the gate if the stall is real, and make the Cloud Routing card message reflect real routing state; consumes Phase 96's corrected liveness signal (DRAIN-01, DRAIN-02)
- [ ] **Phase 99: Progress-POST Timeout & Log-Spam Quieting** — give progress POSTs a short connect-timeout + zero retries and demote the progress-path transport-error log to debug, killing the sustained `ConnectTimeout` spam; small and independent (OBS-01)
- [ ] **Phase 100: Human-Friendly Pod Console Logs** — a readable per-job startup banner (filename, source path/origin, target cluster / `backend_id` / bucket, duration, size) + human-phrased step lines, alongside the existing structured JSON logs; small and independent (OBS-02)
- [ ] **Phase 101: Subprocess Essentia Analysis & Live Progress Restoration** — run essentia analysis in a subprocess so the pod's asyncio loop is no longer GIL-starved, restoring the admin-UI live progress bar with console + UI progress sharing one source; the riskiest phase (changes the pod execution model), results byte-identical (OBS-03)
- [x] **Phase 102: Alembic Migration-Chain Flatten** — collapse the `001`–`039` chain into one baseline reusing revision `039` (`down_revision=None`, prod-at-039 no-op), embedding the `pg_dump -s` DDL + seed rows, proven by an empty-diff fidelity merge gate + the read-only prod-at-039 probe, replacing ~22 per-migration tests with one invariant test; self-contained engineering debt (MIG-01, MIG-02, MIG-03)

---

### Phase 93: Console Derived-Status Truthfulness
**Goal**: The console surfaces each file's real derived per-stage status and real in-flight counts, consistent with the Files matrix — no status-blind pills, no `0` badge while work is in flight.
**Depends on**: Nothing (reuses the shipped derived `stage_status`)
**Requirements**: CONSOLE-01, CONSOLE-02
**Success Criteria** (what must be TRUE):
  1. In the file detail slide-in, each stage's Stage-Eligibility pill shows the real derived status (done / in-flight / failed / not-started / skipped), matching that file's Files-matrix row.
  2. A file whose row shows Meta=done / Analyze=in-flight renders visibly distinct pills (not identical plain pills).
  3. The left-rail Analyze badge shows the true in-flight/pending count and never reads `0` while analyze jobs are in flight (the observed `0`-while-2,183-in-flight case).
  4. The detail pills and rail badges derive from the SAME `stage_status` the Files matrix already uses — one status source, no divergent second derivation.
**Plans**: TBD
**UI hint**: yes

### Phase 94: Detail Pop-Out Close/Dismiss Fix
**Goal**: The detail pop-out X/close control fully dismisses the panel on both surfaces, escaping the HTMX-swap / Alpine-global-scope trap that today only removes the X icon.
**Depends on**: Nothing
**Requirements**: CONSOLE-03
**Success Criteria** (what must be TRUE):
  1. Clicking X on the Agents detail panel fully closes the panel (not just removing the X icon).
  2. Clicking X on the Analyze-lane detail panel fully closes the panel.
  3. The close behavior survives an HTMX poll-swap — the panel does not reappear or leave a stuck partial after the next 5s poll.
  4. The close handler invokes the Alpine method through component scope (e.g. `Alpine.$data(this)`), not a global-scope reference, and a source-level guard covers the wiring so the global-scope trap can't silently return (browser UAT is the authoritative catch).
**Plans**: TBD
**UI hint**: yes

### Phase 95: Analyze-View Browser-Slowdown Investigation & Fix
**Goal**: The operator can open and use the Analyze workspace at 200K-corpus scale without the browser severely slowing or hanging.
**Depends on**: Nothing
**Requirements**: CONSOLE-04
**Success Criteria** (what must be TRUE):
  1. Opening the Analyze workspace at corpus scale does not severely slow or freeze the browser.
  2. The slowdown root cause is identified (client-side render/poll cost vs the `/pipeline/stats` poll) and recorded in the phase artifacts.
  3. If the root cause traces to the stats poll, the DENORM-01 deferral is revisited and the decision recorded; otherwise the client-side render/poll cost is bounded at the source.
  4. Existing Analyze workspace behavior (lane cards, per-file rows, the live windowed-progress signal) is preserved.
**Plans**: TBD
**UI hint**: yes

### Phase 96: Per-Cluster Kueue Liveness Derivation & Agents-Page Identity
**Goal**: Each Kueue cluster surfaces as a live per-cluster ephemeral identity derived from in-flight workloads, and the single generic "k8s burst" lane is reconciled with those identities.
**Depends on**: Nothing (the shared derivation core)
**Requirements**: COMPUTE-01
**Success Criteria** (what must be TRUE):
  1. On the Agents page, each Kueue cluster (vox, xenolab) renders ACTIVE while it is running workloads — not perpetually `NEVER` / dead.
  2. The per-cluster identity is derived from in-flight Kueue jobs / `backend_id`, not from a heartbeat the pods never emit.
  3. A cluster is never shown twice (once dead as an agent row, once as a generic active burst lane) — the generic "k8s burst" lane is reconciled with the per-cluster identities.
  4. Existing routing/dispatch behavior is unchanged — rank/cap tiering and per-cluster failure isolation are untouched; this is a surfacing/liveness derivation, not a scheduler change.
**Plans**: TBD
**UI hint**: yes

### Phase 97: Compute-Lane Surfacing — Header Count & Per-File Lane Label
**Goal**: The header agent count and every per-file lane label reflect the real active compute clusters, killing the stale `☁ A1` label.
**Depends on**: Phase 96
**Requirements**: COMPUTE-02, COMPUTE-03
**Success Criteria** (what must be TRUE):
  1. The header agent count includes every active compute lane (not `Agents · 1` while multiple compute lanes are actively running).
  2. Each file's lane is labeled with its real backend/cluster, derived from `backend_id`.
  3. The stale `☁ A1` label never appears when no A1 backend is configured (only Kueue vox/xenolab + local) — and no A1 lane is re-added.
  4. The header count and lane labels consume Phase 96's derived liveness / `backend_id` (single source), not a re-derived signal.
**Plans**: TBD
**UI hint**: yes

### Phase 98: Cloud-Drain Liveness-Gate Investigation & Fix
**Goal**: The cloud-drain dispatch path and its status message reflect real routing state; the "Awaiting cloud" backlog is not falsely stranded while compute is actively analyzing.
**Depends on**: Phase 96
**Requirements**: DRAIN-01, DRAIN-02
**Success Criteria** (what must be TRUE):
  1. The investigation determines whether the drain dispatch falsely gates on a heartbeat-liveness signal compute agents never emit, and the finding is recorded (if purely cosmetic, DRAIN-01 collapses into DRAIN-02).
  2. If the stall is real, the "Awaiting cloud" backlog dispatches to available Kueue clusters — verified by measured, non-zero dispatch throughput and a decreasing backlog.
  3. The Cloud Routing card never reads "held — no compute agent online" while compute agents are actively analyzing files.
  4. The rank/cap routing policy and per-cluster failure isolation are unchanged — a targeted liveness-gate fix, not a routing/scheduler redesign.
**Plans**: TBD
**UI hint**: yes

### Phase 99: Progress-POST Timeout & Log-Spam Quieting
**Goal**: Analysis pods stop emitting sustained progress-POST `ConnectTimeout` spam during analysis.
**Depends on**: Nothing
**Requirements**: OBS-01
**Success Criteria** (what must be TRUE):
  1. Progress POSTs use a short connect-timeout with zero retries — not the 30s × 3-retry budget that piles up into spam.
  2. Pods no longer emit sustained `ConnectTimeout` warning spam during analysis; the progress-path transport-error log is demoted to debug.
  3. A regression guard asserts the progress path uses the short-timeout / no-retry client, so the 30s×3 spam budget can't silently return.
  4. A dropped progress POST still never fails the analysis job (the best-effort contract is preserved).
**Plans**: TBD

### Phase 100: Human-Friendly Pod Console Logs
**Goal**: The operator can read a human-friendly frame for each job in the pod console, alongside the existing structured JSON logs.
**Depends on**: Nothing (sequenced after Phase 99 — same pod-logging surface)
**Requirements**: OBS-02
**Success Criteria** (what must be TRUE):
  1. Each job logs a readable startup banner — human-readable filename, `file_id`, source path/origin (fileserver, original path), target cluster / `backend_id` / staging bucket, duration and size.
  2. Step lines are phrased for humans (e.g. downloaded N MB in Xs, verified sha256) while the structured `event`/`step`/`elapsed_ms` JSON fields are preserved for machine parsing.
  3. essentia's own stdout banners are framed/routed so they do not read as the app's own logs.
**Plans**: TBD

### Phase 101: Subprocess Essentia Analysis & Live Progress Restoration
**Goal**: The admin-UI live progress bar advances mid-analysis, driven by the same source as the console progress lines, by running essentia analysis in a subprocess so the event loop is no longer GIL-starved.
**Depends on**: Phase 100
**Requirements**: OBS-03
**Success Criteria** (what must be TRUE):
  1. essentia analysis runs in a subprocess so the pod's asyncio event loop is no longer GIL-starved during analysis.
  2. The admin-UI live analysis progress bar advances mid-analysis (not a 0→100% jump at completion).
  3. The console progress lines and the UI progress bar are driven by one shared source (the fine-window counts).
  4. Analysis results are byte-identical to the pre-change windowed output (BPM/key/window counts unchanged) — progress remains best-effort.
**Plans**: TBD

### Phase 102: Alembic Migration-Chain Flatten
**Goal**: The `001`–`039` Alembic chain is collapsed into a single baseline reusing revision `039` — a no-op on prod, a clean build on ephemeral CI/test DBs, byte-identical to the pre-flatten chain output.
**Depends on**: Nothing (self-contained engineering debt)
**Requirements**: MIG-01, MIG-02, MIG-03
**Success Criteria** (what must be TRUE):
  1. A single baseline migration (`revision="039"`, `down_revision=None`) embeds the full `pg_dump -s` DDL + seed rows (`pipeline_stage_control`, `route_control`); prod (at `039`) is a no-op on the next `upgrade head`, and ephemeral CI/test DBs build cleanly from the baseline.
  2. A merge gate proves fidelity — empty schema diff vs the pre-flatten chain, byte-identical seed rows, empty `--autogenerate` diff, and a clean upgrade-from-empty + `downgrade base` round-trip.
  3. Prod `alembic_version == '039'` is re-confirmed via the read-only PG probe (`ssh datum@lux.lan`, `BEGIN TRANSACTION READ ONLY`) immediately before merge; if not at `039`, the merge holds.
  4. The ~22 per-migration test files are replaced by one baseline invariant test (the `033` `analysis_completed_at` XOR/NAND check, seed rows present, partial indexes, search-vector/GIN, enums, expected tables/columns) with the 90% coverage gate preserved.
**Plans**: TBD

<details>
<summary>✅ 2026.7.5 Parallel Enrich DAG (Retire Linear FileState) — Phases 77-92 · SHIPPED 2026-07-14</summary>

Retire the linear `FileState` enum and derive per-file, per-stage status (`not_started` / `in_flight` / `done` / `failed`) from the output tables that already exist, so metadata / fingerprint / analyze become genuinely per-file parallel (every `discovered` file lights up in all three enrich tabs, workable in any order). This is a **live-corpus data-model migration** touching ~23 source files: additive `032` → a standing shadow-compare gate → readers-before-writers cutover, seam by seam → the destructive migration (number assigned at plan time). **Small blast-radius per phase (one shippable PR per seam)** is a hard requirement. Phase numbering **continues from 76**. 42 requirements mapped 1:1, 0 orphans, 0 duplicates. Zero new dependencies. Design contract: `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md`; research: `.planning/research/SUMMARY.md`.

- [x] **Phase 77: Additive Schema & Rescan-Wipe Fix (migration `032`)** — additive-only `032` creates the failure markers, dedup marker, cloud-routing sidecar rows, and partial indexes (mirrored into the ORM), backfilled from `files.state` **without touching `files.state`**; plus the independently-shippable rescan progress-wipe fix (MIG-01, MIG-03, PERF-01) (completed 2026-07-08)
- [x] **Phase 78: Derivation Layer, Eligibility & Anti-Drift Test Harness** — the single-source predicate module (`enums/stage.py` DB-free + `services/stage_status.py`), `stage_status()` / `eligible()`, SAVEPOINT-wrapped in-flight detection, and the SQL⇔Python equivalence test; carries the **D-01 open decision** (written decision record required at plan-time) (DERIV-01..05, ELIG-01..04, INFLIGHT-01..03) (completed 2026-07-08)
- [x] **Phase 79: Shadow-Compare Gate (live corpus)** — a committed, re-runnable implication check between legacy `files.state` and the derived representation; must pass before any reader cutover and before the destructive migration (number assigned at plan time) (MIG-02) (completed 2026-07-08)
- [x] **Phase 80: Recovery / Re-enqueue Cutover** — `reenqueue.py` + `reconcile_cloud_jobs.py` derive done/in-flight from `stage_status`/sidecars with no `FileRecord.state` read; deliberately **before** the pending-set/counts readers (double-negation dependency) (READ-03) (completed 2026-07-10)
- [x] **Phase 81: Per-Stage Failure Persistence & Retry Paths** — durable failure markers for analyze + metadata (`report_metadata_failed` records instead of nothing) + reused fingerprint failure; a metadata retry path so a failure is never a permanent dead-end (FAIL-01..04) (completed 2026-07-09)
- [x] **Phase 82: Counts & Pending-Set Cutover** — the three enrich pending sets + `get_pipeline_stats` derived from `stage_status`; the cross-stage deadlock dissolves; four-bucket per-stage counts; the 200K-scale poll latency measured (READ-01, READ-02, PERF-02) (completed 2026-07-10)
- [x] **Phase 83: Cloud-Routing Sidecar Cutover** — cloud routing (`AWAITING_CLOUD`/`PUSHING`/`PUSHED`/`LOCAL_ANALYZING`) via the `cloud_job` sidecar / derived `in_flight(analyze)`, one atomic consistency domain, CAS-guard collapse (closes the missing `/upload-failed` guard) (SIDECAR-01) (completed 2026-07-09)
- [x] **Phase 84: Dedup & Fingerprint-Progress Cutover** — `services/dedup.py` + `get_fingerprint_progress` derive from the dedup marker / output tables; resolve/undo preserved (READ-04, SIDECAR-02) (completed 2026-07-10)
- [x] **Phase 85: EXECUTED-Gate Revival** — the dead `state == EXECUTED` gates revived against the real apply-outcome (`applied(f)` predicate); turns tag/CUE writing on for the first time — **own PR, live-UAT-worthy, not bundled** (READ-05) (completed 2026-07-10)
- [x] **Phase 86: Proposals Cutover** — `proposals.status` becomes the sole authority; the redundant `FileRecord.state` cascade (`_TERMINAL_FILE_STATES`) deleted, dissolving the `store_proposals` MOVED-regression bug (SIDECAR-03) (completed 2026-07-11)
- [x] **Phase 87: Operator UI — Stage Matrix, Failure Retry, Eligibility Trace & Priority** — per-file derived stage matrix (paginated), per-stage failure visibility + retry, the "why not eligible?" trace, force-done/skip, orphaned-work count, and the restored per-stage priority stepper (UI-01..05, PRIO-01) (completed 2026-07-11)
- [x] **Phase 88: Lane / Agent Drill-In** — clickable lane-detail + agent-detail views (the agent-activity view groups owned files by derived `stage_status`), poll-swap-surviving + keyboard-accessible (DRILL-01..03) (completed 2026-07-11)
- [x] **Phase 89: Legacy Scan-Path Deletion & Sentinel Reattribution** — delete the orphaned legacy scan path (removes two `FileState` writers), reattribute historical `legacy-application-server`-owned rows to a real fileserver agent, then drop the `agent_id` default + delete the sentinel row (RESTRICT-FK-ordered) (LEGACY-01..03) (completed 2026-07-11)
- [x] **Phase 90: Destructive Migration & Writer Removal** — gated last (shadow-compare green + cloud-push lanes drained): drop `ix_files_state`, drop `files.state`, delete the `FileState` enum, remove the remaining `.state=` writers (MIG-04) (completed 2026-07-13)
- [x] **Phase 91: Milestone-Close Hygiene** — milestone-close hardening shipped outside the formal GSD phase pipeline, documented retroactively for numbering continuity: HYG-01 bounded the orphan-count hot poll via an O(1) module cache + lifespan refresh (PR #239); HYG-02/03/04 = coverage uplift to 100% on four files, a vulture dead-code sweep (zero dead src), a `FileState` docs scrub, plus a queue-activity connect-before-count fix (PR #241). No GSD plans/requirements (post-42-req hygiene). (completed 2026-07-13)
- [x] **Phase 92: Milestone-Close Tech-Debt Cleanup** — the tech-debt items surfaced by the 2026.7.5 milestone audit: the PERF-02 follow-up (parallelize the three `get_stage_progress` bucket-count reads via `asyncio.gather`), fix the non-hermetic test flakes 83-01/83-03 for per-bucket CI isolation, and cosmetic doc fixes (stale `agent_files.py` DISCOVERED-stamp comment + duplicated `backends.py` `KueueBackend.reconcile` comment block). DENORM-01 stays v2-deferred; P85 WR-01..04 stays accepted-deferred (requirements assigned at discuss-phase) (completed 2026-07-14)
</details>

<details>
<summary>✅ 2026.7.2 Multi-Compute Agents (N Cloud-Compute Backends) (Phases 72-76) — SHIPPED 2026-07-06</summary>

Finished the 2026.7.1 registry's deliberate compute-side descope: **N cloud-compute agents** now dispatch / route / reconcile / fail-isolate simultaneously, exactly as N Kueue clusters do — the direct compute-side twin of Phase 70's multi-Kueue work. Retired the `≤1-compute` fail-fasts (`config.active_compute_scratch_dir`, `services/backends.resolved_non_local_kind`) and generalized them for a `local + N-Kueue + N-compute` registry. **Parity only** — no new routing semantics, no provisioning, **zero new dependencies** (a pure application-code extension of the existing `Backend` protocol + push/rsync pipeline). Requirements mapped 1:1: MCOMP-01→72 · MCOMP-02..06→73 · MCOMP-07→74, plus appended sweeps HYG-01..05→75 (engineering hygiene) and HARD-01..03→76 (compute/push hardening) (15 mapped, 0 orphans). Shipped as PRs #209/#210/#211/#213/#214. Audit PASSED (15/15 reqs, 5/5 phases, 6/6 integration seams, E2E flow complete); four low-severity/cosmetic review items closed at close-out by quick `260706-odc`. Deferred to v2: PROV-01 (N-compute-aware orphan recovery) + PROV-02/03. Full detail archived in `milestones/2026.7.2-ROADMAP.md`; requirements in `milestones/2026.7.2-REQUIREMENTS.md`; audit in `milestones/2026.7.2-MILESTONE-AUDIT.md`.

- [x] **Phase 72: Per-Entry Compute Binding & Fail-Fast Retirement** — declare N `compute` backends in `backends.toml`, each bound to a specific registered compute Agent, all accepted at boot; retire + generalize the `≤1-compute` fail-fasts for a `local + N-Kueue + N-compute` registry; behavior-preserving groundwork (MCOMP-01) (completed 2026-07-05)
- [x] **Phase 73: Per-Agent Dispatch, Liveness, Scratch & Failure Isolation** — per-agent liveness probe, per-agent push/scratch destination + `/pushed` callback, rank/cap load-spread across N compute agents (free arm64 preferred, spill to paid x86), per-backend failure isolation + in-flight/terminalization scoping; the behavior core (MCOMP-02..06) (completed 2026-07-05)
- [x] **Phase 74: Docs, Runbook & N-Lane Compute UI Verification** — operator runbook for adding a 2nd+ compute agent + mixed arm64/x86 rank/cap cost-tiering; verified the Phase-71 BEUI N-lane UI already renders each compute agent as its own lane (MCOMP-07) (completed 2026-07-06)
- [x] **Phase 75: Engineering Hygiene — Guard Hardening, Tech-Debt & Stale-Tracking Cleanup** — hardened the docs-drift guard for the between-milestones state, dropped stale compose comments, added the force-local duration-router gate test, reconciled stale 2026.7.0 tracking (HYG-01..05) (completed 2026-07-06)
- [x] **Phase 76: Compute/Push Hardening** — serialized the N-compute liveness probes (HARD-01, structural session-safety, closes WR-01); atomic `push_attempt` ledger RMW via `pg_advisory_xact_lock` (HARD-02, closes AR-73-02/WR-04); `agent_id` pattern/max-length validation at the scan-status + agent-roots HTTP boundary (HARD-03, closes AR-30-03). Closed the milestone. No new dependencies (completed 2026-07-06)

</details>

<details>
<summary>✅ 2026.7.1 Multi-Cloud Backends (Phases 67-71) — SHIPPED 2026-07-05</summary>

Generalized the single `cloud_target` selector (`local`/`a1`/`k8s`) into a declarative, cost-tiered `backends.toml` registry that drains long, locally-timing-out audio files across **local + N Kueue clusters + N cloud-compute agents simultaneously** — ranked by `rank`, bounded by per-backend `cap`, preferring free/owned capacity and spilling to paid only under load. Static routing, **no provisioning**, **zero new dependencies** — a pure application-code refactor over the v6.0 cloud-burst system. REG-05 + revised MKUE-02/04 superseded the design's "one shared bucket" decision per operator direction. Requirements mapped 1:1: REG→67 · BACK→68 · SCHED→69 · MKUE→70 · BEUI→71 (21 mapped, 0 orphans). Shipped as PRs #201/#202/#203/#204/#206. Audit PASSED (21/21 reqs, 5/5 flows). Full detail archived in `milestones/2026.7.1-ROADMAP.md`; requirements in `milestones/2026.7.1-REQUIREMENTS.md`; audit in `milestones/2026.7.1-MILESTONE-AUDIT.md`.

- [x] **Phase 67: Backend Registry & Config Model** — declarative `backends.toml` registry + per-kind validators + S3 staging-bucket registry (public/shared vs cluster-specific) + removal of `cloud_target` and flat `s3_*`/`kube_*`/`compute_*` fields with no back-compat shim; zero-config implicit-local default (REG-01..05) (completed 2026-07-04)
- [x] **Phase 68: Backend Protocol + 3 Implementations** — one `Backend` protocol (`is_available`/`in_flight_count`/`dispatch`/`reconcile`) with Local/ComputeAgent/Kueue bodies + `cloud_job.backend_id` migration 029; behavior-preserving, byte-identical characterization gate (BACK-01..04) (completed 2026-07-04)
- [x] **Phase 69: Tiered Drain Scheduler** — rank-first eligible dispatch per file, per-backend `cap`, spill-when-full, offline→next re-dispatch with black-hole guard, stateless tie-break, single recovery owner, `FileState.LOCAL_ANALYZING` (CR-01); first behavior-changing phase (SCHED-01..05) (completed 2026-07-04)
- [x] **Phase 70: Multi-Kueue (N Clusters)** — N distinct kr8s clients, deterministic per-file `pick_bucket`, `cloud_job.staging_bucket` migration 030, per-cluster failure isolation, concurrency-safe clean-before-flip cross-bucket cleanup (MKUE-01..04) (completed 2026-07-04)
- [x] **Phase 71: Deployment, Config, Docs & N-Lane UI** — N registry-derived read-only lanes on the existing 5s poll + persisted no-redeploy master force-local toggle gating drain/routers/backfill + operator runbook/config docs (BEUI-01..03) (completed 2026-07-05)

</details>

<details>
<summary>✅ 2026.7.0 Engineering Improvements (Phases 63-66) — SHIPPED 2026-07-03</summary>

Cleanup / engineering-debt paydown: faster parallel CI, code-change-gated builds, CalVer release versioning, a per-module coverage uplift, a docs-drift guard, and small UI/dead-code cleanup. **No product-behavior change, no backend behavior change.** Phase numbering continues from v7.0 (Phase 62 was the last integer; 57.1 was a decimal insert). This milestone *adopts* CalVer — the last `vN.M`-numbered planning cycle, its release the first CalVer tag (`2026.7.0`). Shipped as PRs #193/#194/#197/#198/#199. Full detail archived in `milestones/2026.7.0-ROADMAP.md`; requirements in `milestones/2026.7.0-REQUIREMENTS.md`.

- [x] **Phase 63: Parallel CI & Code-Change Gating** — partition the ~1,750-test suite into workflow-step buckets, fan out across parallel jobs, combine per-shard coverage into one Codecov upload, and skip heavy jobs on doc-only changes (skip-with-success) (CI-01..04) (completed 2026-07-02)
- [x] **Phase 64: Per-Module Coverage Uplift & Gate Raise** — raise the worst-offender modules to a per-module coverage floor with behavior-asserting tests and lift the enforced gate above today's 90.38%, wired into CI (COV-01, COV-02) (completed 2026-07-03)
- [x] **Phase 65: CalVer Adoption** — replace `vN.M` with `YYYY.MM.REVISION` (first tag `2026.7.0`) across the release procedure, version badges, image tags, and the milestone↔version mapping, historical record intact (VER-01..04) (completed 2026-07-03)
- [x] **Phase 66: Docs-Drift Gate & Dead-Code Sweep** — a CI gate cross-checking REQUIREMENTS.md traceability against passed phases + re-link the `/saq` monitor in the shell + delete vestigial dead code (DOCS-01, CLEAN-01, CLEAN-02) (completed 2026-07-03); guard-robustness follow-up hardened in PR #199

</details>

<details>
<summary>✅ v6.0 Kubernetes Burst Analysis (Phases 52-56) — SHIPPED 2026-06-29</summary>

K8s became a **third** analysis-routing target alongside local and the v5.0 OCI A1: long sets that can't finish locally run as ephemeral, quota-scheduled **Kueue batch Jobs** on a remote x64 cluster — the v5.0 control-plane choreography with the execution unit changed to a one-shot per-file Job. Full detail archived in `milestones/v6.0-ROADMAP.md`; audit in `milestones/v6.0-MILESTONE-AUDIT.md`.

- [x] **Phase 52: Job-runner image & one-shot entrypoint** — x86 GHCR image FROM the existing essentia base (zero new pip deps); one-shot presign-download → sha256-verify → windowed analyze → POST result → exit; honest exit codes; runtime-mounted internal CA (KJOB-01..05) (completed 2026-06-27)
- [x] **Phase 53: S3 object-staging leg** — control-plane presign (aioboto3, sole S3 importer) + file-server agent httpx-PUT upload + pod presigned GET; `file_id`-scoped keys; cleanup on every outcome; `cloud_job` sidecar migration (KSTAGE-01..05) (completed 2026-06-28)
- [x] **Phase 54: Kube submit/watch + reconcile cron** — suspended per-file Kueue Job (kr8s); fast submit + `*/5` reconcile cron; out-of-band callback authoritative; no ledger-seed; Inadmissible-vs-Pending (KSUBMIT-01..06) (completed 2026-06-28)
- [x] **Phase 55: Routing, state & ledger integration** — `cloud_target` selector (replaces `cloud_burst_enabled` bool) + `stage_cloud_window` K8s branch + `enqueue_router` additions + AST guard (the one live-seam edit) (KROUTE-01..06) (completed 2026-06-28)
- [x] **Phase 56: Deployment, runbook, config & docs** — Kueue admin runbook + least-privilege RBAC + `_FILE` secrets + transport-agnostic endpoints + LocalQueue startup probe + ephemeral-identity Agents-UI note + master toggle (KDEPLOY-01..06) (completed 2026-06-29)

**Post-audit fix (quick 260628-wzq):** JOB-ENV-CONTRACT — `build_job_manifest` now injects `PHAZE_JOB_FILE_ID` + `envFrom` (the pod's runtime env), closing the manifest→pod seam every admitted pod needs.

**Deferred (deployment-gated):** live K8s + real-S3 E2E (UAT 53/54/55) — re-run FIRST after the live rollout.

</details>

<details>
<summary>✅ v7.0 UI Redesign — DAG-Centric Hybrid Console (Phases 57-62) — SHIPPED 2026-07-02</summary>

Replaced the MVP tab-sprawl UI with a **DAG-centric hybrid console**: the pipeline DAG is the home + navigation spine (three-column shell — rail nav · stage workspace · per-file record slide-in), local/A1/k8s are first-class Analyze lanes, and every human approval unifies behind one before→after diff/approve gate. IA/template rewrite over existing routers/services — **no backend behavior change** (one scoped exception: Phase 57.1's incremental analyze-progress signal). Full detail archived in `milestones/v7.0-ROADMAP.md`; audit in `milestones/v7.0-MILESTONE-AUDIT.md`.

- [x] **Phase 57: Shell & DAG rail** — three-column shell, `GET /` (Analyze default) + `/s/<stage>` HTMX nav, ⌘K + header status strip, brand/theme preserved, 8 legacy routes redirect into the shell, seeded dead-template guard (SHELL-01..05) (completed 2026-06-30)
- [x] **Phase 57.1: Incremental window persistence & live analyze progress signal** — mid-flight `fine_windows_analyzed` counter (idempotent under Phase 32 re-enqueue) + `analysis_completed_at` discriminator gating partial rows out of proposals; scoped backend exception (PROG-01..03) (completed 2026-06-30)
- [x] **Phase 58: Enrich + Analyze workspaces** — Discover/Metadata/Fingerprint/Analyze views; 3 Analyze lane cards (local/A1/k8s) with Kueue quota-wait/Inadmissible; single `/pipeline/stats` poll fanout (WORK-01..05) (completed 2026-06-30)
- [x] **Phase 59: Identify workspaces** — Track-ID surfacing existing audfprint+Panako + rapidfuzz signals; Tracklist Search→Scrape→Match 3-step (IDENT-01..02) (completed 2026-07-01)
- [x] **Phase 60: Review & Apply** — unified before→after diff + Approve/Edit/Skip + server-predicate bulk-approve; Dedupe keeper-select; Cue preview; audit + reversible (REVIEW-01..05) (completed 2026-07-01)
- [x] **Phase 61: Full record + ⌘K + Agents** — per-file record slide-in, ⌘K palette (`@alpinejs/focus`), Agents page w/ ephemeral k8s identity, first-run empty state (RECORD-01..04) (completed 2026-07-02)
- [x] **Phase 62: Polish & cutover** — a11y baseline, narrow-width icon rail, docs/README refresh, CUT-02 dead-code cutover (20 legacy templates deleted, empty guard allowlist) (CUT-01..04) (completed 2026-07-02)

**Milestone audit:** PASSED — 28/28 requirements, 7/7 phases verified, 0 broken flows (`milestones/v7.0-MILESTONE-AUDIT.md`).
**Deferred (deployment-gated):** 57.1 UAT tests 8-9 (real multi-hour kill-9 on local/A1; live Kueue k8s progress) — confirm at the next homelab/cluster rollout.

</details>

<details>
<summary>v1.0 MVP (Phases 1-11) -- SHIPPED 2026-03-30</summary>

- [x] Phase 1: Infrastructure & Project Setup (3/3 plans) -- completed 2026-03-27
- [x] Phase 2: File Discovery & Ingestion (3/3 plans) -- completed 2026-03-27
- [x] Phase 3: Companion Files & Deduplication (2/2 plans) -- completed 2026-03-27
- [x] Phase 4: Task Queue & Worker Infrastructure (2/2 plans) -- completed 2026-03-27
- [x] Phase 5: Audio Analysis Pipeline (2/2 plans) -- completed 2026-03-28
- [x] Phase 6: AI Proposal Generation (2/2 plans) -- completed 2026-03-28
- [x] Phase 7: Approval Workflow UI (3/3 plans) -- completed 2026-03-29
- [x] Phase 8: Safe File Execution & Audit (2/2 plans) -- completed 2026-03-29
- [x] Phase 9: Pipeline Orchestration (1/1 plan) -- completed 2026-03-30
- [x] Phase 10: CI Config & Bug Fixes (1/1 plan) -- completed 2026-03-30
- [x] Phase 11: Polish & Cleanup (3/3 plans) -- completed 2026-03-30

Full details: `.planning/milestones/v1.0-ROADMAP.md`

</details>

<details>
<summary>v2.0 Metadata Enrichment & Tracklist Integration (Phases 12-17) -- SHIPPED 2026-04-02</summary>

- [x] Phase 12: Infrastructure & Audio Tag Extraction (3/3 plans) -- completed 2026-03-31
- [x] Phase 13: AI Destination Paths (3/3 plans) -- completed 2026-03-31
- [x] Phase 14: Duplicate Resolution UI (2/2 plans) -- completed 2026-04-01
- [x] Phase 15: 1001Tracklists Integration (2/2 plans) -- completed 2026-04-01
- [x] Phase 16: Fingerprint Service & Batch Ingestion (3/3 plans) -- completed 2026-04-01
- [x] Phase 17: Live Set Matching & Tracklist Review (3/3 plans) -- completed 2026-04-02

Full details: `.planning/milestones/v2.0-ROADMAP.md`

</details>

<details>
<summary>v3.0 Cross-Service Intelligence & File Enrichment (Phases 18-23) -- SHIPPED 2026-04-04</summary>

- [x] Phase 18: Unified Search (2/2 plans) -- completed 2026-04-03
- [x] Phase 19: Discogs Cross-Service Linking (3/3 plans) -- completed 2026-04-03
- [x] Phase 20: Tag Writing (3/3 plans) -- completed 2026-04-03
- [x] Phase 21: CUE Sheet Generation (3/3 plans) -- completed 2026-04-03
- [x] Phase 22: Tracklist Integration Fixes (1/1 plan) -- completed 2026-04-04
- [x] Phase 23: v3.0 Polish & Wiring Fixes (1/1 plan) -- completed 2026-04-04

Full details: `.planning/milestones/v3.0-ROADMAP.md`

</details>

<details>
<summary>v4.0 Distributed Agents (Phases 24-29) -- SHIPPED 2026-05-17</summary>

- [x] Phase 24: Schema Foundation & Agent Registry (5/5 plans) -- completed 2026-05-11
- [x] Phase 25: Internal Agent HTTP API & Bearer Auth (8/8 plans) -- completed 2026-05-12
- [x] Phase 26: Task Code Reorg & HTTP-Backed Agent Worker (13/13 plans) -- completed 2026-05-12
- [x] Phase 27: Watcher Service & User-Initiated Scan (7/7 plans) -- completed 2026-05-14
- [x] Phase 28: Distributed Execution Dispatch (6/6 plans) -- completed 2026-05-15
- [x] Phase 29: Deployment Hardening & Agents Admin (8/8 plans) -- completed 2026-05-17

Full details: `.planning/milestones/v4.0-ROADMAP.md`

</details>

<details>
<summary>✅ v5.0 Cloud Burst Analysis (Phases 47-51) — SHIPPED 2026-06-26</summary>

Analyze long-duration audio (≥90 min) on a free OCI Ampere A1 (arm64) "compute agent" reached over Tailscale, instead of locally — clearing the long-set backlog that exceeds the local analysis timeout. Full detail archived in `milestones/v5.0-ROADMAP.md`.

- [x] **Phase 47: Official arm64 essentia agent image** — build essentia from source on a native arm64 CI runner, publish to GHCR with a parity guard (completed 2026-06-24)
- [x] **Phase 48: Compute-agent type** — register a media-less `kind="compute"` agent that drains its queue + PUTs results, surfaced on the Agents page (completed 2026-06-25)
- [x] **Phase 49: Duration routing & backfill** — route ≥90min files to an online compute agent (else "awaiting cloud"), backfill the 144 timed-out long files via the Phase 45 ledger (completed 2026-06-25)
- [x] **Phase 50: Push pipeline** — rsync-over-Tailscale "stay one ahead" push to the compute agent's scratch dir, sha256-verify, ephemeral cleanup, idempotent re-drive (completed 2026-06-26)
- [x] **Phase 51: Deployment, config & docs** — cloud-agent compose + Tailscale, all config knobs (`_FILE` secrets), OCI A1 / Tailscale-ACL runbook, master enable toggle (completed 2026-06-26)

Deployment-gated verification deferred to the live OCI A1 rollout (see STATE.md Deferred Items).

</details>

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Infrastructure & Project Setup | v1.0 | 3/3 | Complete | 2026-03-27 |
| 2. File Discovery & Ingestion | v1.0 | 3/3 | Complete | 2026-03-27 |
| 3. Companion Files & Deduplication | v1.0 | 2/2 | Complete | 2026-03-27 |
| 4. Task Queue & Worker Infrastructure | v1.0 | 2/2 | Complete | 2026-03-27 |
| 5. Audio Analysis Pipeline | v1.0 | 2/2 | Complete | 2026-03-28 |
| 6. AI Proposal Generation | v1.0 | 2/2 | Complete | 2026-03-28 |
| 7. Approval Workflow UI | v1.0 | 3/3 | Complete | 2026-03-29 |
| 8. Safe File Execution & Audit | v1.0 | 2/2 | Complete | 2026-03-29 |
| 9. Pipeline Orchestration | v1.0 | 1/1 | Complete | 2026-03-30 |
| 10. CI Config & Bug Fixes | v1.0 | 1/1 | Complete | 2026-03-30 |
| 11. Polish & Cleanup | v1.0 | 3/3 | Complete | 2026-03-30 |
| 12. Infrastructure & Audio Tag Extraction | v2.0 | 3/3 | Complete | 2026-03-31 |
| 13. AI Destination Paths | v2.0 | 3/3 | Complete | 2026-03-31 |
| 14. Duplicate Resolution UI | v2.0 | 2/2 | Complete | 2026-04-01 |
| 15. 1001Tracklists Integration | v2.0 | 2/2 | Complete | 2026-04-01 |
| 16. Fingerprint Service & Batch Ingestion | v2.0 | 3/3 | Complete | 2026-04-01 |
| 17. Live Set Matching & Tracklist Review | v2.0 | 3/3 | Complete | 2026-04-02 |
| 18. Unified Search | v3.0 | 2/2 | Complete | 2026-04-03 |
| 19. Discogs Cross-Service Linking | v3.0 | 3/3 | Complete | 2026-04-03 |
| 20. Tag Writing | v3.0 | 3/3 | Complete | 2026-04-03 |
| 21. CUE Sheet Generation | v3.0 | 3/3 | Complete | 2026-04-03 |
| 22. Tracklist Integration Fixes | v3.0 | 1/1 | Complete | 2026-04-04 |
| 23. v3.0 Polish & Wiring Fixes | v3.0 | 1/1 | Complete | 2026-04-04 |
| 24. Schema Foundation & Agent Registry | v4.0 | 5/5 | Complete | 2026-05-11 |
| 25. Internal Agent HTTP API & Bearer Auth | v4.0 | 8/8 | Complete | 2026-05-12 |
| 26. Task Code Reorg & HTTP-Backed Agent Worker | v4.0 | 13/13 | Complete | 2026-05-12 |
| 27. Watcher Service & User-Initiated Scan | v4.0 | 7/7 | Complete | 2026-05-14 |
| 28. Distributed Execution Dispatch | v4.0 | 6/6 | Complete | 2026-05-15 |
| 29. Deployment Hardening & Agents Admin | v4.0 | 8/8 | Complete | 2026-05-17 |
| 30. Fix control-plane SAQ queue misrouting | v4.0 | 5/5 | Complete   | 2026-06-10 |
| 31. Windowed Time-Series Audio Analysis | v4.0 | 6/6 | Complete   | 2026-06-11 |
| 32. Pipeline Reboot Resilience & Re-enqueue | v4.0 | 4/4 | Complete   | 2026-06-11 |
| 33. SAQ Monitoring UI (mounted in phaze-api) | v4.0 | 4/4 | Complete   | 2026-06-11 |
| 34. Pipeline Queue-Depth Status & Double-Enqueue Guard | v4.0 | 5/5 | Complete | 2026-06-10 |
| 35. Pipeline Determinism, Idempotency & Per-Job-Type Observability | v4.0 | 5/5 | Complete | 2026-06-12 |
| 36. Pipeline Queue Backend Migration (Redis → Postgres SAQ) | v4.0 | — | Complete | 2026-06-12 |
| 37. Per-Stage Pause and Priority Control Plane | v4.0 | 4/4 | Complete | 2026-06-12 |
| 38. Pipeline DAG Pause/Priority UI and Rescan Button Removal | v4.0 | 3/3 | Complete | 2026-06-13 |
| 39. Tracklist Search DAG Node | v4.0 | 1/1 | Executed | — |
| 40. Tracklist Fingerprint-Scan DAG Node | v4.0 | 1/1 | Executed | — |
| 41. Scrape and Match DAG Triggers | v4.0 | 1/1 | Executed | — |
| 42. Recovery-Only Pipeline Automation | v4.0 | 2/2 | Executed | — |
| 43. Analyze Throughput Fix | v4.0 | 4/4 | Complete | 2026-06-17 |
| 44. Analyze Observability UI | v4.0 | 4/4 | Complete | 2026-06-18 |
| 45. Scheduling Ledger for Orphan Recovery | v4.0 | 6/6 | Complete    | 2026-06-19 |
| 46. Heartbeat Starvation Fix | v4.0 | 1/1 | Complete | 2026-06-23 |
| 47. Official arm64 essentia agent image | v5.0 | 4/4 | Complete    | 2026-06-24 |
| 48. Compute-agent type | v5.0 | 3/3 | Complete   | 2026-06-25 |
| 49. Duration routing & backfill | v5.0 | 4/4 | Complete    | 2026-06-25 |
| 50. Push pipeline | v5.0 | 8/8 | Complete    | 2026-06-26 |
| 51. Deployment, config & docs | v5.0 | 4/4 | Complete   | 2026-06-26 |
| 52. Job-runner image & one-shot entrypoint | v6.0 | 3/3 | Complete    | 2026-06-27 |
| 53. S3 object-staging leg | v6.0 | 5/5 | Complete    | 2026-06-28 |
| 54. Kube submit/watch + reconcile cron | v6.0 | 6/6 | Complete    | 2026-06-28 |
| 55. Routing, state & ledger integration | v6.0 | 6/6 | Complete    | 2026-06-28 |
| 56. Deployment, runbook, config & docs | v6.0 | 7/7 | Complete    | 2026-06-29 |
| 57. Shell & DAG rail | v7.0 | 4/4 | Complete    | 2026-06-30 |
| 57.1. Incremental window persistence & live analyze progress signal | v7.0 | 4/4 | Complete    | 2026-06-30 |
| 58. Enrich + Analyze workspaces | v7.0 | 4/4 | Complete    | 2026-06-30 |
| 59. Identify workspaces | v7.0 | 3/3 | Complete    | 2026-07-01 |
| 60. Review & Apply | v7.0 | 4/4 | Complete   | 2026-07-01 |
| 61. Full record + ⌘K + Agents | v7.0 | 5/5 | Complete    | 2026-07-02 |
| 62. Polish & cutover | v7.0 | 4/4 | Complete    | 2026-07-02 |
| 63. Parallel CI & Code-Change Gating | 2026.7.0 | 4/4 | Complete    | 2026-07-02 |
| 64. Per-Module Coverage Uplift & Gate Raise | 2026.7.0 | 4/4 | Complete    | 2026-07-03 |
| 65. CalVer Adoption | 2026.7.0 | 2/2 | Complete   | 2026-07-03 |
| 66. Docs-Drift Gate & Dead-Code Sweep | 2026.7.0 | 3/3 | Complete    | 2026-07-03 |
| 67. Backend Registry & Config Model | 2026.7.1 | 6/6 | Complete    | 2026-07-04 |
| 68. Backend Protocol + 3 Implementations | 2026.7.1 | 5/5 | Complete    | 2026-07-04 |
| 69. Tiered Drain Scheduler | 2026.7.1 | 5/5 | Complete    | 2026-07-04 |
| 70. Multi-Kueue (N Clusters) | 2026.7.1 | 5/5 | Complete    | 2026-07-04 |
| 71. Deployment, Config, Docs & N-Lane UI | 2026.7.1 | 5/5 | Complete    | 2026-07-05 |
| 72. Per-Entry Compute Binding & Fail-Fast Retirement | 2026.7.2 | 4/4 | Complete    | 2026-07-05 |
| 73. Per-Agent Dispatch, Liveness, Scratch & Failure Isolation | 2026.7.2 | 4/4 | Complete    | 2026-07-05 |
| 74. Docs, Runbook & N-Lane Compute UI Verification | 2026.7.2 | 4/4 | Complete    | 2026-07-06 |
| 75. Engineering Hygiene — Guard Hardening, Tech-Debt & Stale-Tracking Cleanup | 2026.7.2 | 2/2 | Complete    | 2026-07-06 |
| 76. Compute/Push Hardening | 2026.7.2 | 3/3 | Complete    | 2026-07-06 |
| 77. Additive Schema & Rescan-Wipe Fix (migration 032) | 2026.7.5 | 3/3 | Complete    | 2026-07-08 |
| 78. Derivation Layer, Eligibility & Anti-Drift Test Harness | 2026.7.5 | 2/2 | Complete    | 2026-07-08 |
| 79. Shadow-Compare Gate (live corpus) | 2026.7.5 | 2/2 | Complete    | 2026-07-08 |
| 80. Recovery / Re-enqueue Cutover | 2026.7.5 | 5/5 | Complete    | 2026-07-10 |
| 81. Per-Stage Failure Persistence & Retry Paths | 2026.7.5 | 6/6 | Complete    | 2026-07-09 |
| 82. Counts & Pending-Set Cutover | 2026.7.5 | 4/4 | Complete    | 2026-07-10 |
| 83. Cloud-Routing Sidecar Cutover | 2026.7.5 | 7/7 | Complete    | 2026-07-09 |
| 84. Dedup & Fingerprint-Progress Cutover | 2026.7.5 | 6/6 | Complete    | 2026-07-10 |
| 85. EXECUTED-Gate Revival | 2026.7.5 | 4/4 | Complete    | 2026-07-10 |
| 86. Proposals Cutover | 2026.7.5 | 5/5 | Complete    | 2026-07-11 |
| 87. Operator UI — Stage Matrix, Failure Retry, Eligibility Trace & Priority | 2026.7.5 | 9/8 | Complete    | 2026-07-11 |
| 88. Lane / Agent Drill-In | 2026.7.5 | 3/3 | Complete    | 2026-07-11 |
| 89. Legacy Scan-Path Deletion & Sentinel Reattribution | 2026.7.5 | 2/2 | Complete    | 2026-07-11 |
| 90. Destructive Migration & Writer Removal | 2026.7.5 | 4/4 | Complete    | 2026-07-13 |
| 91. Milestone-Close Hygiene | 2026.7.5 | — | Complete    | 2026-07-13 |
| 92. Milestone-Close Tech-Debt Cleanup | 2026.7.5 | 5/5 | Complete    | 2026-07-14 |
| 93. Console Derived-Status Truthfulness | 2026.7.7 | 0/TBD | Not started | - |
| 94. Detail Pop-Out Close/Dismiss Fix | 2026.7.7 | 0/TBD | Not started | - |
| 95. Analyze-View Browser-Slowdown Investigation & Fix | 2026.7.7 | 0/TBD | Not started | - |
| 96. Per-Cluster Kueue Liveness Derivation & Agents-Page Identity | 2026.7.7 | 0/TBD | Not started | - |
| 97. Compute-Lane Surfacing — Header Count & Per-File Lane Label | 2026.7.7 | 0/TBD | Not started | - |
| 98. Cloud-Drain Liveness-Gate Investigation & Fix | 2026.7.7 | 0/TBD | Not started | - |
| 99. Progress-POST Timeout & Log-Spam Quieting | 2026.7.7 | 0/TBD | Not started | - |
| 100. Human-Friendly Pod Console Logs | 2026.7.7 | 0/TBD | Not started | - |
| 101. Subprocess Essentia Analysis & Live Progress Restoration | 2026.7.7 | 0/TBD | Not started | - |
| 102. Alembic Migration-Chain Flatten | 2026.7.7 | 0/TBD | Not started | - |

_Phase-by-phase detail archived to `milestones/2026.7.5-ROADMAP.md`._

## Backlog (unscheduled — no phase number yet)

- **⚠ MILESTONE-CLOSE GATE (2026.7.5) — Bound the hot-poll cost of `get_stage_orphan_counts` (Phase 87 code-review WR-02).** _[Surfaced 2026-07-11 by the Phase 87 code review; MUST be resolved before `/gsd:complete-milestone 2026.7.5`.]_ `services/pipeline.py get_stage_orphan_counts` reuses recovery's full machinery on EVERY 5s `/pipeline/stats` tick: `get_ledger_rows` over the entire `scheduling_ledger` (~44.5K rows in the 2026-06-18 incident) + `_build_done_sets` + live-key + in-flight-cloud reads. It is SAVEPOINT degrade-safe (→ zeros on error), so it never 500s, but on a large ledger it can BLOCK — not just degrade — the hot poll. Steady-state the ledger is small (cleared on completion), so the badge is usually cheap; the risk is the post-incident large-ledger window. Fix options to weigh at planning: a cheaper bounded count query (per-stage `COUNT` of ledger-minus-done rather than materializing full sets), a short-TTL memoization across the poll fan-out, or capping the ledger scan. The orphan badge must stay "definitionally what recovery would re-enqueue" (no drift from `recover_orphaned_work`). Presentation stays; this is a read-path performance bound only.
- **Distributed cloud analysis (burst the backlog).** _[SCHEDULED as v5.0 Cloud Burst Analysis, Phases 47-51 — narrowed to rsync-over-Tailscale to a free arm64 OCI A1 (essentia built from source), no object storage. See Phase Details (v5.0).]_ Offload long-file analysis to cloud x86 workers via the existing agent model: stage file to object storage → cloud worker pulls (presigned GET) → analyzes → PUTs result; **reconcile by `file_id`** (already end-to-end), sha256 for download integrity. Only new pieces: optional `source_url`+`sha256` on `ProcessFilePayload` + a "stager". essentia is **x86-only** (no aarch64 wheel; source build infeasible). Best near-free path = **GCP $300/90-day trial, x86 e2 spot, GCS same-region** (≈$0 out of pocket); min-cost paid = OCI E5 preemptible (~$100, free egress). **Gate: only pursue if nox throughput is still insufficient after the Phase 43 redeploy + re-measure** — bounding may make this moot. Full design: memory `reference-essentia-arm64-cloud-burst` + `project-analyze-4h-timeout-incident`.
- **Partition the test suite for parallel CI.** _[SCHEDULED as 2026.7.0 Engineering Improvements, Phase 63 (CI-01/02/03). See Phase Details (2026.7.0).]_ Split the ~1750-test pytest suite into independently-runnable buckets so CI fans them out across parallel jobs instead of one serial run. Partition by **pipeline workflow-step** (discovery, metadata, fingerprint, analyze, identify/tracklist, review/apply, agents/distributed) plus a **generic/shared** bucket (schema, config, helpers, routing). Open questions to resolve at planning: marker-based selection (`@pytest.mark.<step>`) vs directory layout vs `pytest-xdist` sharding vs a CI job matrix; how to keep coverage aggregation correct across shards (combine `.coverage` files → single Codecov upload) and preserve the 85% gate; real-Postgres integration tests likely need their own bucket. Goal: cut wall-clock CI time without losing the single coverage report.
- **Adopt CalVer ([calver.org](https://calver.org/)) for release versioning.** _[SCHEDULED as 2026.7.0 Engineering Improvements, Phase 65 (VER-01..04). See Phase Details (2026.7.0).]_ Replace the current milestone-aligned `vN.M` scheme (now at v7.0) with a calendar-based version. Decide the exact scheme at planning (e.g. `YYYY.MM.MICRO` or `YY.MM.MICRO`) and how it coexists with the milestone narrative (milestones become named, versions become dated). Update: the release procedure (pyproject `version` + `uv.lock` bump → annotated tag PUSH → GHCR publish — see memory `project-release-procedure`), README/version badges (one-line badge style), the milestone↔version mapping in ROADMAP/MILESTONES, and any image tags / compose references. Note the prior cadence shipped many `v4.0.x` patch releases — pick a MICRO convention that supports same-month patches.
- **CI builds only when code changes.** _[SCHEDULED as 2026.7.0 Engineering Improvements, Phase 63 (CI-04). See Phase Details (2026.7.0).]_ Stop running the full build/test/security CI on docs- and planning-only changes (e.g. `.planning/**`, `*.md`) so commits like these backlog/requirements edits don't trigger the whole pipeline. Decide the mechanism at planning: workflow `paths`/`paths-ignore` filters vs a changed-files detection job that gates downstream jobs (the latter avoids the "required check never runs → PR can't merge" branch-protection trap that bare `paths-ignore` causes). Must keep the required status checks satisfiable on doc-only PRs (skip-with-success, not skip-absent). Pairs with the "partition test suite for parallel CI" item.
- **Re-add an in-UI link to the `/saq` SAQ monitor.** _[SCHEDULED as 2026.7.0 Engineering Improvements, Phase 66 (CLEAN-01). See Phase Details (2026.7.0).]_ _[Surfaced by the v7.0 milestone audit (`v7.0-MILESTONE-AUDIT.md`) — target the next cleanup / "engineering basics" milestone.]_ The SAQ task-queue dashboard is still mounted at `/saq` (`main.py`) and reachable by direct URL, but the v7.0 cutover (Phase 62/CUT-02) deleted the only in-UI link when it removed `dashboard.html`. Nothing is broken — the monitor works, it's just unlinked. Add a discreet link back into the shell; the natural home is the Agents / Compute page (RECORD-03 already surfaces agent state) rather than the DAG rail. Presentation-only; no backend change.
- **Harden the docs-drift guard for the between-milestones state.** _[SATISFIED by PR #207 (`ec80a53a`, 2026-07-05); Phase 75 (HYG-01) records it as already-satisfied — no new code/test. See Phase Details (2026.7.2).]_ _[Surfaced at the 2026.7.0 milestone close, 2026-07-03.]_ The Phase-66 traceability guard (`tests/shared/core/test_requirements_traceability.py`) reads `.planning/REQUIREMENTS.md` with no existence check in its 4 active-milestone tests, so the standard milestone-close `git rm REQUIREMENTS.md` would raise `FileNotFoundError` and fail the required code-quality check. For the 2026.7.0 close we kept REQUIREMENTS.md in place (guard verified green, all 13 reqs `[x]`→passed phases) instead of deleting it. Follow-up: make the active-milestone tests `pytest.skip` (or fail-clean) when REQUIREMENTS.md is absent, add a regression test for the archived/no-active-milestone state, then the close can `git rm` the file again. Small, self-contained; a natural quick task or a Phase-66-style guard-robustness follow-up.
- **Restore the per-stage job-priority UI control.** _[Surfaced 2026-07-07 during the post-deploy review; deferred by the user to a future milestone.]_ Job priority is **live end-to-end in the backend** — `PipelineStageControl.priority` (SMALLINT, LOWER dequeues sooner, default 50), the SAQ `before_enqueue` stamp in `tasks/_shared/stage_control.py`, the live backlog-reorder SQL in `services/stage_control.py`, and the endpoint `POST /pipeline/stages/{stage}/priority` — but the **UI control that posted to it was removed** in the v7.0 DAG-console cutover (the DAG-canvas priority steppers are gone; only passive Alpine store seeds `metadata/analyze/fingerprintPriority` remain in `base.html`/`shell.html`/`_workspace_poll_seeds.html`). Net: the setter endpoint is orphaned — operators cannot change priority from the UI. Re-wire a priority stepper (▲ higher = decrement number, ▼ lower = increment) per agent stage into the current shell, POSTing to the existing endpoint. Also consider surfacing the (also-existing) pause/resume controls if that endpoint is likewise orphaned. Presentation + wiring only; the backend is already there. Was "C5" in the 2026-07-07 fix batch.
- **Retire the `legacy-application-server` sentinel — go fully agent-based.** _[Surfaced + scoped 2026-07-08 post-deploy; user deferred the WHOLE thing to a future milestone. See memory `project_legacy_sentinel_retirement`.]_ The `legacy-application-server` Agent row is the vestigial agentless-era sentinel: the `default=` for `file.agent_id`/`scan_batch.agent_id` (FK `ondelete=RESTRICT`), seeded already-revoked by migration 012. **The FK ownership model STAYS** (`agent_id` = which fileserver owns the file). The live operator scan flow (`POST /pipeline/scans` → distributed `scan_directory` → `agent_files.py:110` stamps `agent.id` from auth) already attributes rows to the **real** agent (nox), not legacy — so new data is already agent-based. The only remaining legacy WRITE path is the orphaned `POST /api/v1/scan` (`routers/scan.py:71`) → `run_scan`/`discover_and_hash_files` (`services/ingestion.py:79,157`), which no shipped UI hits. Milestone work: (a) delete `routers/scan.py` `/api/v1/scan` + `run_scan` + `discover_and_hash_files` so nothing new is attributed to legacy; (b) data-migration to reattribute historical legacy-owned files/scan_batches to a real `kind=fileserver` agent (nox); (c) drop the column `default=`, then delete the sentinel row (RESTRICT FK requires reattribution first). App-server local files should be owned by a real deployed fileserver agent (nox already is one via `phaze agents add`).
- **Clickable backend-lane cards + agent rows → filtered activity drill-in.** _[Surfaced 2026-07-08 post-deploy; user chose to fold into the next milestone. Companion to the (a)/(c) agent-UI fixes shipped as quick tasks 260707-s44/ser/sq3.]_ Today the backend-lane cards (`_lane_card.html`: KUEUE·vox, LOCAL·local) and the AGENTS·heartbeating rows (`admin/partials/agents_table.html`) are pure presentation with no interactivity, and no server endpoint filters activity by lane or agent — so the drill-in the operator expects doesn't exist. Build: (1) lane-detail endpoint/drawer (`GET /pipeline/lanes/{backend_id}`) — that lane's queues / in-flight / waiting / quota / recent completions; (2) agent-detail endpoint/drawer (`GET /admin/agents/{agent_id}/_activity`) — owned files by state, recent scan batches, per-lane queue depths, liveness; (3) UI wiring — `hx-get` on cards/rows into a slide-over drawer (Alpine, v7.0 hybrid-console style) with `cursor-pointer`/`role=button`/Enter-Space a11y + selected highlight; (4) **CRITICAL polling interaction** — the panel self-polls every 5s via `outerHTML` swap, so the drawer must live OUTSIDE the polled section (or carry its state through the poll) or it gets clobbered (same OOB lesson as the 260707-sq3 Summary task); (5) selection via URL param so it survives poll swaps + is shareable; (6) tests + reconcile rail/card structural guards. Spec decisions to pin: drawer vs dedicated page (recommend drawer); what "activity" means per lane vs per agent; how `LOCAL·local` maps now that scans are agent-attributed.
