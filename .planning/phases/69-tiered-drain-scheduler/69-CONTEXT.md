# Phase 69: Tiered Drain Scheduler - Context

**Gathered:** 2026-07-04
**Status:** Ready for planning

<domain>
## Phase Boundary

Turn the single-backend drain (`stage_cloud_window`, today a per-file `backend.dispatch()` over exactly one resolved non-local backend) into a **tiered multi-backend scheduler**: each `AWAITING_CLOUD` file is dispatched to the *available* backend with the lowest `rank` whose `in_flight_count() < cap`, evaluated **per candidate file** so a full top-rank backend spills to the next rank rather than blocking the tick. The global `cloud_max_in_flight` window becomes a **per-backend `cap`** enforced by count-and-claim under the existing `pg_advisory_xact_lock`. A backend going offline or a job failing mid-flight returns the file to `AWAITING_CLOUD` for re-dispatch against *current* availability, with a black-hole/attempt guard preventing infinite thrash. This is **the first behavior-changing phase** — the moment more than one backend can run at once (SCHED-01..05).

**Out of scope (deferred):** N concurrent Kueue clusters (Phase 70), per-cluster S3 buckets (Phase 70), token-hack removal (Phase 70), N-lane UI + config/docs (Phase 71). No instance provisioning, no dollar-cost model, no new provider SDKs (milestone non-goals).
</domain>

<decisions>
## Implementation Decisions

### Local spillover timing (staleness guard) — SCHED-01
- **D-01:** Long files do **NOT** spill to slow local (rank 99) immediately when higher-ranked backends are full — this **diverges from the design's default** (§4.3 "keep it simple, no staleness logic"). A **staleness guard** applies: a file must sit in `AWAITING_CLOUD` beyond a wait threshold before local becomes an eligible dispatch target. Rationale: a momentary cloud backlog blip should not dump long files onto slow local (local is slowest exactly for the long files cloud burst exists to offload).
- **D-02:** The staleness threshold is **operator-configurable with a shipped sensible default** — a new config knob (suggested name `cloud_spill_to_local_after_seconds`, suggested default ~15 min; exact name/default at plan-time). Tunable in config, no logic redeploy. NOT a fixed constant, NOT duration-derived.
- **D-03:** The staleness wait applies **only when higher-ranked backends are FULL (busy)**. If every cloud/Kueue backend is genuinely **OFFLINE**, the file spills to local **immediately** (still subject to local's `cap`) — waiting is pointless during an outage. So the guard gates the *full→local* path, not the *offline→local* path.

### Black-hole / hard-fail policy — SCHED-03
- **D-04:** **Local is the guaranteed safety net.** Bounded **per-backend** dispatch attempts (reuse the existing `cloud_submit_max_attempts` config, applied per backend) stop cloud/Kueue thrash; once a file exhausts its cloud/Kueue attempts it falls to local, which performs full (slow) analysis. A file goes `ANALYSIS_FAILED` **only** when local itself fails **or** a global total-attempt ceiling is hit — a genuinely-processable file never hard-fails just because cloud was flaky/down. (The exact global ceiling + how per-backend counters interact with local is a plan-time/research mechanic — see open questions.)

### Global concurrency ceiling — SCHED-02
- **D-05:** **Purely per-backend caps.** The **sum** of per-backend `cap`s is the only total ceiling — no separate global concurrency knob. The old global `cloud_max_in_flight` setting is **retired** (its role is fully subsumed by per-backend `cap`, matching design §4.3.3). One source of truth.

### Failed-file re-dispatch target — SCHED-03/04
- **D-06:** **Stateless re-rank.** When a job fails mid-flight on backend X, the next tick re-picks the lowest-rank-available backend normally — it *may* re-pick X, but the per-backend attempt cap (D-04) bounds that and then spills to the next rank. **No per-file failure memory** (no "last-failed backend_id" carried on the file). Keeps the scheduler stateless, consistent with SCHED-04's stateless tie-break ethos.

### Already locked by ROADMAP/REQUIREMENTS (not re-discussed — do NOT re-open)
- **SCHED-04 tie-break:** equal-`rank` backends tie-broken deterministically + statelessly by **lowest current utilization `in_flight/cap`, then stable `id`**. No weighted/proportional fair-share.
- **SCHED-05 single recovery owner:** `reconcile_cloud_jobs` + the recovery ledger become `backend_id`-aware; the existing AST over-enqueue guard is extended so compute-backed cloud files gain no second recovery path (no replay of the 44.5k-job over-enqueue incident class).
- **cap source:** per-backend `cap` comes from the Phase-67 `backends` registry entries (already carry `rank` + `cap`).
- **offline detection:** the Phase-68 `Backend.is_available()` probe (compute→heartbeat GATE-1; Kueue→cluster probe, no compute dependency; local→always up).
- **in-flight counting:** the Phase-68 uniform `Backend.in_flight_count()` over `{UPLOADING,UPLOADED,SUBMITTED,RUNNING}`.

### Claude's Discretion
- Exact new config field name(s) + default value for the staleness threshold (D-02).
- Whether the "waited-since" staleness signal reads an existing timestamp or needs a new one (see open questions) — planner/researcher chooses the least-invasive source.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone design + roadmap
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` §4.3 (tiered drain), §4.4 (per-backend in-flight registry), §4.5 (failure/spillover), §6 (non-goals) — the scheduler design; **note D-01 deliberately overrides §4.3's "no staleness logic" default position**.
- `.planning/ROADMAP.md` → "Phase 69: Tiered Drain Scheduler" — goal + 5 success criteria (the black-hole/attempt-bound + single-recovery-owner criteria are stricter than the design doc; the success criteria win).
- `.planning/REQUIREMENTS.md` → SCHED-01..05.

### Prior-phase decisions this phase builds on
- `.planning/phases/68-backend-protocol-3-implementations/68-CONTEXT.md` — D-02/D-10 (uniform `in_flight_count` + in-flight status set), D-08 (compute writes+terminalizes a live `cloud_job`), D-09 (retained value accessors through Phase 70).
- `.planning/phases/67-backend-registry-config-model/67-CONTEXT.md` — the `backends` registry (rank/cap per entry), zero-config implicit all-local registry, `cloud_enabled`.

### Incident history to NOT replay (SCHED-05)
- The 44.5k-job over-enqueue incident (double recovery owner) and the Phase-30 default-queue misrouting incident (empty-backends wedge) — the recovery/ledger guard must stay single-owner-per-kind.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/phaze/tasks/release_awaiting_cloud.py` — `stage_cloud_window` drain; already loops candidates calling `backend.dispatch()`/`is_available()`/`.cap` for ONE resolved backend under `_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY = 5_000_504` (`pg_advisory_xact_lock`). Phase 69 generalizes the single-backend selection into per-file rank-first-eligible selection across `resolve_backends()`.
- `src/phaze/services/backends.py` — `Backend` protocol + `resolve_backends()` + per-backend `in_flight_count()`/`is_available()`/`.cap`/`.rank` (Phase 68). The scheduler's building blocks already exist; this phase adds the selection policy over them.
- `src/phaze/services/pipeline.py` — `get_cloud_window_count()` (retire/replace with per-backend counts) + `get_cloud_staging_candidates(session, limit)` (FIFO oldest-first candidate claim).
- `src/phaze/config.py:593` — `cloud_submit_max_attempts` (reuse per-backend for D-04); the global `cloud_max_in_flight` field is the one to retire (D-05).
- `src/phaze/tasks/reenqueue.py` — `SchedulingLedger`-based recovery + `is_domain_completed`/`_natural_id` AST over-enqueue guard (extend `backend_id`-aware for SCHED-05).
- `KueueBackend.reconcile` in `services/backends.py` (Phase 68, currently the only reconcile body) — generalize/make `backend_id`-aware for SCHED-05.

### Established Patterns
- Count-and-claim under `pg_advisory_xact_lock` in one transaction (the drain's existing overshoot guard) — per-backend `cap` enforcement rides this same lock (SCHED-02); **the drain↔reconcile lock-ordering is the load-bearing correctness change flagged for research.**
- "Cron never raises" no-op discipline (Phase 68 T-68-05/10 + the WR-02 clean-hold fix) — all new selection/spill/black-hole paths must degrade to holds, never raise out of the tick.

### Integration Points
- The scheduler stays inside `stage_cloud_window`; duration gating (`_route_discovered_by_duration`) and result-return (`put_analysis` by `file_id`) are untouched (design §5).
</code_context>

<specifics>
## Specific Ideas

- Robert explicitly wants the staleness guard (D-01) even though the design defaulted to "no staleness logic" — long files must not fall onto slow local for a mere momentary cloud-full blip, but an actual outage should release them at once (D-03).
- Prefer reuse over new knobs where possible: per-backend attempt bound reuses `cloud_submit_max_attempts` (D-04); the only genuinely-new config is the staleness threshold (D-02).
</specifics>

<deferred>
## Deferred Ideas

- **Duration-scaled staleness threshold** (longer files wait longer before local) — considered for D-02, deferred as over-engineering; revisit only if the flat threshold proves too blunt.
- **Per-file "avoid last-failed backend" memory** — considered for D-06, deferred (stateless re-rank chosen); revisit if a single flaky backend proves to starve specific files in practice.
- **Keep-a-global-master-ceiling** — considered for D-05, rejected in favor of purely per-backend caps; revisit only if operators need a cross-backend cost cap.

## Resolved Questions (answered at plan-time — see 69-RESEARCH.md, implemented + verified)
- **Staleness "waited-since" signal source (D-01/D-02):** RESOLVED — use `FileRecord.updated_at` with **zero migration**; no writer touches a parked `AWAITING_CLOUD` row until the drain flips it to `PUSHING`, so `updated_at` equals the entry-to-`AWAITING_CLOUD` timestamp and re-stamps on each fail-back.
- **Drain↔reconcile lock scope (SCHED-02):** RESOLVED — keep the single existing advisory-lock key `5_000_504`; the drain snapshots each backend's `in_flight_count()` once per tick under the lock and decrements locally as it claims. Reconcile only ever *decrements* in-flight (never claims a slot) and shares the same key per-row, so overlapping ticks provably never overshoot a cap. No per-backend lock keys needed.
- **Black-hole counters (D-04, SCHED-03):** RESOLVED — reuse the persistent `cloud_job.attempts` counter as the anti-thrash bound; a file whose attempts reach the cap becomes cloud/Kueue-ineligible (filtered from the eligible set), deterministically routing it to local. `ANALYSIS_FAILED` only when local itself fails or the global total-attempt ceiling is hit. Preserves D-06 statelessness (exclusion derives from a counter, not remembered backend IDs).
</deferred>

---

*Phase: 69-tiered-drain-scheduler*
*Context gathered: 2026-07-04*
