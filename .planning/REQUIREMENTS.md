# Requirements: Phaze — Milestone 2026.7.7 Console & Cloud-Burst Hardening

> **Historical planning snapshot (2026-07-29).** Checkboxes and traceability below record this
> milestone's planning state; they are not today's requirements backlog. See
> [`.planning/README.md`](README.md).

**Defined:** 2026-07-14
**Core Value:** Get 200K messy music and concert files properly named, organized, deduplicated, with rich metadata in Postgres — human-in-the-loop approval so nothing moves without review. Files stay on file-server agents; decisions stay on the application server.

**Milestone thesis:** A hardening pass over the shipped 2026.7.5 system — fix the DAG-console UI correctness bugs, make the multi-Kueue compute lanes surface truthfully (and investigate a possible functional cloud-drain stall), make cloud-analysis pods observable, and pay down the Alembic migration-chain debt. **Zero new dependencies, no new product features.** All requirements are operator-facing (the single admin user).

## Milestone Requirements

### CONSOLE — DAG Console Correctness

- [x] **CONSOLE-01**: Operator sees each stage's real derived status (done / in-flight / failed / not-started / skipped) in the file detail slide-in's *Stage Eligibility* pills, consistent with the Files-matrix row for the same file (today the pills are status-blind — a row showing Meta=done / Analyze=in-flight renders identical plain pills). (Phase 93 / epic phaze-nawk.1, PR #260 — record slide-in renders the shared `_stage_pill.html` token off `stage_status_case`, the same derivation the Files matrix uses; browser-UAT verified phaze-nawk.4. Not a fix-round 1-2 outcome — see phaze-pw7v.10)
- [x] **CONSOLE-02**: Operator sees the left-rail stage badges reflect actual work — the Analyze badge shows the true in-flight/pending count, never `0` while files are in flight (observed `0` while 2,183 analyze jobs were in flight). (Phase 93 / epic phaze-nawk.2, PR #260 — the rail `<aside>` was missing its Alpine `x-data` root, so every badge binding was inert and stuck at the server-rendered `0`; fixed in commit d09b398; browser-UAT confirmed the badge now tracks in-flight count. Not a fix-round 1-2 outcome — see phaze-pw7v.10)
- [x] **CONSOLE-03**: Operator can dismiss the detail pop-out with its X / close control — both the Agents detail panel and the Analyze-lane detail panel fully close, rather than only removing the X icon (HTMX-swap / Alpine-global-scope trap). (Phase 94 / epic phaze-nawk.3, PR #260 — `_detail_pane.html`'s `hide()` clears the swapped body and reaches Alpine state via `Alpine.$data(this)` instead of a bare global reference; verified closed on both surfaces and survives the 5s poll-swap; browser-UAT verified phaze-nawk.4. Not a fix-round 1-2 outcome — see phaze-pw7v.10)
- [x] **CONSOLE-04**: Operator can open the Analyze workspace without the browser severely slowing or hanging. (Phase 95 / epic phaze-zqvh, PR #264 — browser-verified at 200K corpus: ~4.1s open, ~80MB heap, flat 31-minute soak, no-jank interactions at the 13K-row working set; see 95-VERIFICATION.md)

### COMPUTE — Multi-Kueue Compute Surfacing (systemic)

- [x] **COMPUTE-01**: Operator sees each Kueue cluster (vox, xenolab) as a live, per-cluster ephemeral identity on the Agents page while it runs workloads — derived from in-flight Kueue jobs, ACTIVE not perpetually `NEVER`/dead — and the single generic "k8s burst" lane is reconciled with these per-cluster identities (a cluster is never shown twice: once dead as an agent row, once as a generic active burst lane). (Phase 96 / epic phaze-zlv.1-.3, PR #258 — `derive_compute_lane_identities` (`services/agent_liveness.py`) derives each cluster's live identity from in-flight workloads; registry-shadowed never-heartbeating rows suppressed; epic verification gate phaze-zlv.6 passed. Not a fix-round 1-2 outcome — see phaze-pw7v.10)
- [x] **COMPUTE-02**: Operator sees the header agent count include every active compute lane (not `Agents · 1` while multiple compute lanes are actively running). (Phase 97 / epic phaze-zlv.4, PR #258 — header `Agents · N` sums `$store.pipeline.agentOnline + computeLanesActive`. Not a fix-round 1-2 outcome — see phaze-pw7v.10)
- [x] **COMPUTE-03**: Operator sees each file's lane labeled with its real backend/cluster (derived from `backend_id`); the stale `☁ A1` label never appears when no A1 backend exists (only Kueue vox/xenolab + local are configured). (Phase 97 / epic phaze-zlv.5, PR #258 — per-file lane badge derives from `CloudJob.backend_id` via `f.lane_kind` (commit c7736a2), replacing the retired `cloud_phase`-based a1/k8s heuristic. Not a fix-round 1-2 outcome — see phaze-pw7v.10)

### DRAIN — Cloud-Drain Hold (functional investigation)

- [x] **DRAIN-01**: The cloud-drain dispatch path does not falsely gate on a heartbeat-liveness signal that compute agents never emit — the "Awaiting cloud" backlog dispatches to available Kueue clusters and does not stall while compute is actively analyzing (verified by measured, non-zero dispatch throughput and a decreasing backlog). *(Scoped as investigate-then-fix: if the investigation proves the hold is purely a display artifact, this collapses into DRAIN-02.)* (Phase 98 / epic phaze-qtk.1, PR #257 — investigated and recorded COSMETIC/refuted: `KueueBackend.is_available` has no agent/heartbeat dependency, the fileserver gate is ever-checked-in not heartbeat-based, and capacity math explains the observed backlog; collapses into DRAIN-02 per the scoped contract above. Not a fix-round 1-2 outcome — see phaze-pw7v.10)
- [x] **DRAIN-02**: The Cloud Routing card message reflects real routing state — it does not read "held — no compute agent online" while compute agents are actively analyzing files. (Phase 98 / epic phaze-qtk.2 + phaze-613, PR #257 — `derive_cloud_hold_reason` (`services/backends.py`) derives the caption from the real per-lane snapshot, replacing the hardcoded literal; the drain's hung-probe gap also bounded with `asyncio.wait_for`. Not a fix-round 1-2 outcome — see phaze-pw7v.10)

### OBS — Analysis-Pod Observability (#249)

- [x] **OBS-01**: Analysis pods no longer emit sustained progress-POST `ConnectTimeout` warning spam during analysis — progress posts use a short connect-timeout + zero retries and the progress-path transport-error log is demoted to debug; a regression guard asserts the short-timeout/no-retry client on the progress path. (Phase 99 / PR #252 + regression-guard PR #253, backfilled as epic phaze-ph99 — `services/agent_client.py` progress POSTs use a short connect-timeout with zero retries, the transport-error log demoted to debug, a regression guard pins the client, and a dropped POST still never fails the analysis job. Not a fix-round 1-2 outcome — see phaze-pw7v.10)
- [x] **OBS-02**: Operator can read a human-friendly frame for each job in the pod console — readable filename, source path/origin (fileserver, original path), target cluster / `backend_id` / staging bucket, duration and size — alongside the existing structured JSON logs. (Phase 100 / epic phaze-sfbx, PR #259 — `job_runner.py` emits a human-readable startup banner + step lines beside the existing structured JSON fields; essentia's own stdout bracket-framed. Not a fix-round 1-2 outcome — see phaze-pw7v.10)
- [x] **OBS-03**: The admin-UI live analysis progress bar advances mid-analysis — essentia analysis runs in a subprocess so the pod's asyncio event loop is no longer GIL-starved — and the console progress lines and the UI progress bar share one source. (Phase 101, already recorded complete in ROADMAP.md 2026-07-16 — epic phaze-bo3p (epic + 5 issues): analysis runs via `phaze.analysis_child` over a JSONL protocol driven by `services/analysis_exec.py`, shared by both the pod and SAQ-worker lanes; `services/analysis.py` untouched (byte-identity preserved). This checkbox was simply never synced from the ROADMAP tick — not a fix-round 1-2 outcome, and out of phase-range 93-100 for this cross-walk; see phaze-pw7v.10)

### MIG — Alembic Migration-Chain Flatten

- [ ] **MIG-01**: The Alembic chain `001`–`039` is collapsed into a single baseline migration reusing revision id `039` (`down_revision=None`), embedding the full schema DDL (`pg_dump -s`) + seed rows (`pipeline_stage_control`, `route_control`); production (at `039`) is a no-op on the next `upgrade head`, and ephemeral CI/test DBs build cleanly from the baseline.
- [ ] **MIG-02**: A schema-fidelity merge gate proves the baseline is equivalent to the pre-flatten chain output — empty schema diff, byte-identical seed rows, empty `--autogenerate` diff, and a clean upgrade-from-empty + `downgrade base` round-trip; production `alembic_version == '039'` is re-confirmed via the read-only PG probe immediately before merge.
- [ ] **MIG-03**: The ~22 per-migration test files are replaced by one baseline invariant test preserving the durable invariants (the `033` `analysis_completed_at` XOR/NAND check, seed rows present, partial indexes, search-vector/GIN, enums, expected tables/columns), with the 90% coverage gate preserved.

## v2 / Deferred Requirements

Not in this milestone's roadmap.

### Performance

- **DENORM-01**: Denormalized per-file stage-status bitmap column to bring the `/pipeline/stats` 200K-corpus poll under the ~1s soft budget. Deferred from 2026.7.5 (Phase 82/92) as under-budget after the `asyncio.gather` fan-out; revisit only if CONSOLE-04 traces the Analyze slowdown to the stats poll rather than client-side rendering. **Phase 95 revisit (phaze-zqvh.1/.4)**: CONSOLE-04's baseline traced the SEVERE Analyze-open slowdown to the client-side unbounded per-file table, NOT the stats poll (see 95-BASELINE.md) — but the poll itself re-measured p50 1099.5ms/1147.1ms (two 200K runs), over budget. phaze-zqvh.4 fanned out `pipeline_stats_partial`'s remaining ~12 serial awaits via bounded `asyncio.gather` (mirroring the Phase 92 `get_stage_progress` pattern, `src/phaze/routers/pipeline.py:761-830`); post-fix p50 dropped to **1100.3ms** (p95 1167.7ms) — a ~50-90ms improvement, but still marginally OVER the ~1s budget. Root cause of the remainder: `get_stage_progress` itself (~850-900ms, already Phase-92-parallelized — a genuine DB floor, not serialization overhead) plus `_build_dag_context`'s OWN ~10 still-serial awaits (`get_stage_controls`, `get_search_busy_count`, `get_scan_busy_count`, `count_active_agents` x2, `derive_compute_lane_identities`, `get_scrape_busy_count`, `get_match_busy_count`, `get_stage_busy_counts`, `_read_pipeline_counters`) — out of this bead's named scope. **Decision: DENORM-01 stays deferred.** The remaining ~100ms overshoot is a serialization-overhead problem (mechanical, same `asyncio.gather` idiom applies), not evidence a denormalized bitmap column is needed — recommend a fast-follow bead fanning out `_build_dag_context`'s internals before reconsidering DENORM-01. See `95-STATS-BUDGET.md` for the full measurement + recommendation.

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Any new product feature | This is a hardening/bug-fix milestone — correctness and observability only |
| New runtime/library dependencies | Hard constraint: zero new dependencies |
| `analysis_completed_at` backfill | Already shipped as migration `036` (Phase 80), live in prod at `039`; the pending todo was stale and retired |
| Schema changes | The Alembic flatten is byte-identical to the `039` chain output — no DDL change |
| A1 backend support / re-adding an A1 lane | No A1 backend exists; COMPUTE-03 removes the stale label, it does not add A1 |
| Drain-scheduler routing-policy redesign | DRAIN is a targeted liveness-gate fix, not a rank/cap/routing rework |

## Traceability

Which phases cover which requirements. Populated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| CONSOLE-01 | Phase 93 | Complete (epic phaze-nawk.1, PR #260 — browser-UAT verified phaze-nawk.4) |
| CONSOLE-02 | Phase 93 | Complete (epic phaze-nawk.2, PR #260 — browser-UAT verified phaze-nawk.4) |
| CONSOLE-03 | Phase 94 | Complete (epic phaze-nawk.3, PR #260 — browser-UAT verified phaze-nawk.4) |
| CONSOLE-04 | Phase 95 | Complete (verified — 95-VERIFICATION passed) |
| COMPUTE-01 | Phase 96 | Complete (epic phaze-zlv.1-.3, PR #258 — epic verification gate phaze-zlv.6 passed) |
| COMPUTE-02 | Phase 97 | Complete (epic phaze-zlv.4, PR #258) |
| COMPUTE-03 | Phase 97 | Complete (epic phaze-zlv.5, PR #258) |
| DRAIN-01 | Phase 98 | Complete (epic phaze-qtk.1, PR #257 — investigated, recorded cosmetic/refuted, collapses into DRAIN-02) |
| DRAIN-02 | Phase 98 | Complete (epic phaze-qtk.2 + phaze-613, PR #257) |
| OBS-01 | Phase 99 | Complete (PR #252 + #253, backfilled as epic phaze-ph99) |
| OBS-02 | Phase 100 | Complete (epic phaze-sfbx, PR #259) |
| OBS-03 | Phase 101 | Complete (epic phaze-bo3p — already recorded complete in ROADMAP.md 2026-07-16; this table simply hadn't been synced) |
| MIG-01 | Phase 102 | Pending (out of scope for this cross-walk — phaze-pw7v.10 covers CONSOLE/COMPUTE/DRAIN/OBS only) |
| MIG-02 | Phase 102 | Pending (out of scope for this cross-walk) |
| MIG-03 | Phase 102 | Pending (out of scope for this cross-walk) |

**Coverage:**
- Milestone requirements: 15 total
- Mapped to phases: 15 (10 phases, 93-102) ✓
- Unmapped: 0 ✓
- Duplicates: 0 (every requirement maps to exactly one phase)

---
*Requirements defined: 2026-07-14*
*Last updated: 2026-07-29 (phaze-pw7v.10) — cross-walked CONSOLE-01/02/03, COMPUTE-01/02/03, DRAIN-01/02, OBS-01/02/03 against merged code and re-ticked all 12 as Complete with bead/PR evidence; OBS-03 synced from ROADMAP.md's already-complete Phase 101. None of these were delivered by fix rounds 1-2 (PRs #349-360, which fixed unrelated v7-shell/stats-counter/cloud-burst-race bugs) — each was already shipped 2026-07-14/16 via a dedicated Phase-93..100 delivery epic (phaze-nawk, phaze-zlv, phaze-qtk, phaze-ph99, phaze-sfbx) that simply never had its ROADMAP/REQUIREMENTS checkbox synced back. MIG-01/02/03 left Pending — out of this cross-walk's scope.*
