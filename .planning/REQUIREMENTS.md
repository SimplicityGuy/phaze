# Requirements: Phaze — Milestone 2026.7.7 Console & Cloud-Burst Hardening

**Defined:** 2026-07-14
**Core Value:** Get 200K messy music and concert files properly named, organized, deduplicated, with rich metadata in Postgres — human-in-the-loop approval so nothing moves without review. Files stay on file-server agents; decisions stay on the application server.

**Milestone thesis:** A hardening pass over the shipped 2026.7.5 system — fix the DAG-console UI correctness bugs, make the multi-Kueue compute lanes surface truthfully (and investigate a possible functional cloud-drain stall), make cloud-analysis pods observable, and pay down the Alembic migration-chain debt. **Zero new dependencies, no new product features.** All requirements are operator-facing (the single admin user).

## Milestone Requirements

### CONSOLE — DAG Console Correctness

- [ ] **CONSOLE-01**: Operator sees each stage's real derived status (done / in-flight / failed / not-started / skipped) in the file detail slide-in's *Stage Eligibility* pills, consistent with the Files-matrix row for the same file (today the pills are status-blind — a row showing Meta=done / Analyze=in-flight renders identical plain pills).
- [ ] **CONSOLE-02**: Operator sees the left-rail stage badges reflect actual work — the Analyze badge shows the true in-flight/pending count, never `0` while files are in flight (observed `0` while 2,183 analyze jobs were in flight).
- [ ] **CONSOLE-03**: Operator can dismiss the detail pop-out with its X / close control — both the Agents detail panel and the Analyze-lane detail panel fully close, rather than only removing the X icon (HTMX-swap / Alpine-global-scope trap).
- [x] **CONSOLE-04**: Operator can open the Analyze workspace without the browser severely slowing or hanging. (Phase 95 / epic phaze-zqvh, PR #264 — browser-verified at 200K corpus: ~4.1s open, ~80MB heap, flat 31-minute soak, no-jank interactions at the 13K-row working set; see 95-VERIFICATION.md)

### COMPUTE — Multi-Kueue Compute Surfacing (systemic)

- [ ] **COMPUTE-01**: Operator sees each Kueue cluster (vox, xenolab) as a live, per-cluster ephemeral identity on the Agents page while it runs workloads — derived from in-flight Kueue jobs, ACTIVE not perpetually `NEVER`/dead — and the single generic "k8s burst" lane is reconciled with these per-cluster identities (a cluster is never shown twice: once dead as an agent row, once as a generic active burst lane).
- [ ] **COMPUTE-02**: Operator sees the header agent count include every active compute lane (not `Agents · 1` while multiple compute lanes are actively running).
- [ ] **COMPUTE-03**: Operator sees each file's lane labeled with its real backend/cluster (derived from `backend_id`); the stale `☁ A1` label never appears when no A1 backend exists (only Kueue vox/xenolab + local are configured).

### DRAIN — Cloud-Drain Hold (functional investigation)

- [ ] **DRAIN-01**: The cloud-drain dispatch path does not falsely gate on a heartbeat-liveness signal that compute agents never emit — the "Awaiting cloud" backlog dispatches to available Kueue clusters and does not stall while compute is actively analyzing (verified by measured, non-zero dispatch throughput and a decreasing backlog). *(Scoped as investigate-then-fix: if the investigation proves the hold is purely a display artifact, this collapses into DRAIN-02.)*
- [ ] **DRAIN-02**: The Cloud Routing card message reflects real routing state — it does not read "held — no compute agent online" while compute agents are actively analyzing files.

### OBS — Analysis-Pod Observability (#249)

- [ ] **OBS-01**: Analysis pods no longer emit sustained progress-POST `ConnectTimeout` warning spam during analysis — progress posts use a short connect-timeout + zero retries and the progress-path transport-error log is demoted to debug; a regression guard asserts the short-timeout/no-retry client on the progress path.
- [ ] **OBS-02**: Operator can read a human-friendly frame for each job in the pod console — readable filename, source path/origin (fileserver, original path), target cluster / `backend_id` / staging bucket, duration and size — alongside the existing structured JSON logs.
- [ ] **OBS-03**: The admin-UI live analysis progress bar advances mid-analysis — essentia analysis runs in a subprocess so the pod's asyncio event loop is no longer GIL-starved — and the console progress lines and the UI progress bar share one source.

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
| CONSOLE-01 | Phase 93 | Pending |
| CONSOLE-02 | Phase 93 | Pending |
| CONSOLE-03 | Phase 94 | Pending |
| CONSOLE-04 | Phase 95 | Complete (verified — 95-VERIFICATION passed) |
| COMPUTE-01 | Phase 96 | Pending |
| COMPUTE-02 | Phase 97 | Pending |
| COMPUTE-03 | Phase 97 | Pending |
| DRAIN-01 | Phase 98 | Pending |
| DRAIN-02 | Phase 98 | Pending |
| OBS-01 | Phase 99 | Pending |
| OBS-02 | Phase 100 | Pending |
| OBS-03 | Phase 101 | Pending |
| MIG-01 | Phase 102 | Pending |
| MIG-02 | Phase 102 | Pending |
| MIG-03 | Phase 102 | Pending |

**Coverage:**
- Milestone requirements: 15 total
- Mapped to phases: 15 (10 phases, 93-102) ✓
- Unmapped: 0 ✓
- Duplicates: 0 (every requirement maps to exactly one phase)

---
*Requirements defined: 2026-07-14*
*Last updated: 2026-07-14 after roadmap creation — traceability populated, phases 93-102 (milestone 2026.7.7)*
