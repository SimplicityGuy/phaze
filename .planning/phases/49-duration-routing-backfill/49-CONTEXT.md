# Phase 49: Duration routing & backfill - Context

**Gathered:** 2026-06-25
**Status:** Ready for planning

<domain>
## Phase Boundary

Route analysis jobs by file duration. Long files (`metadata.duration` ≥ a configurable threshold, default 90 min) are enqueued to an online `kind="compute"` agent's queue; short files continue to analyze on the local file-server agent with unchanged behavior. When no compute agent is online, ≥threshold files are held in an explicit "awaiting cloud" state and are **never** silently analyzed locally (where they would time out). The operator can backfill the existing 144 `analysis_failed` long files to the cloud, scoped through the Phase 45 scheduling ledger so only previously-scheduled work is re-driven (no whole-backlog over-enqueue).

Requirements: CLOUDROUTE-01..04. This phase is duration-based routing only — cost/throughput-aware routing (CLOUDROUTE-05) and the rsync push pipeline (Phase 50) are out of scope.

</domain>

<decisions>
## Implementation Decisions

### "Awaiting cloud" state (held ≥threshold files when no compute agent online)
- **D-01:** Represent the held state as a **new `FileState.AWAITING_CLOUD`** value. `FileRecord.state` is `String(30)` and `FileState` is a code-only `StrEnum` (see `ANALYSIS_FAILED` precedent), so **no enum/DB migration is needed** — add the member and use it.
- **D-02:** A ≥threshold file with no online compute agent transitions to `AWAITING_CLOUD` instead of being enqueued locally. It must **never** be routed to a fileserver agent.
- **D-03:** Release of held files is **automatic via the existing controller reenqueue path** (Phase 32/45: runs on controller-worker startup + `CronJob(*/5)`). Extend that path to also scan `AWAITING_CLOUD` files and route them to a compute queue once a compute agent is online — ledger-scoped, deterministic-key dedup, same as the rest of recovery. ~5 min release latency is acceptable.
- **D-04:** `AWAITING_CLOUD` must be wired into the recovery/reenqueue **domain-completed predicate** correctly: it is NOT terminal/done (the file still needs analysis), so it must remain eligible for the cron re-drive. `process_file` "done" stays `{ANALYZED, ANALYSIS_FAILED}` — confirm `AWAITING_CLOUD` is treated as pending, not done.
- **D-05:** Surface held files as an **"Awaiting cloud" count card** on the pipeline dashboard, reusing the existing `_safe_count` + count-card pattern (alongside `straggler_count` / `analysis_failed_count`). Click-through to a per-file list is deferred.

### Unknown-duration routing & threshold config
- **D-06:** `metadata.duration` (mutagen tag duration, control-side) can be null at routing time. A file with **null/unknown duration routes local** (treated as short). Rationale: long concert sets — including the 144 failures — reliably carry tag durations, so null almost always means a normal short track. A rare long file with missing tags timing out locally is the pre-49 status quo, not a regression. (The analyze worker's own `_probe_duration_sec` is agent-side and not available at the control-side routing decision.)
- **D-07:** Routing threshold is a single global config knob following the established `straggler_threshold_sec` convention: `cloud_route_threshold_sec: int` default **5400** (= 90 min), alias `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC`. Compare against `metadata.duration` in seconds. (Claude's discretion — pure convention match.)

### Backfill workflow (the 144 timed-out long files)
- **D-08:** Trigger via a **"Backfill to cloud" button on the pipeline dashboard**, next to the existing Recover/trigger controls, returning a count-confirmed response partial.
- **D-09:** Backfill selects files where `state == ANALYSIS_FAILED AND duration >= cloud_route_threshold_sec`, **resets them to `DISCOVERED`**, seeds the scheduling ledger via `insert_ledger_if_absent`, and routes each through the **same new duration-aware router**: compute queue if a compute agent is online, else `AWAITING_CLOUD`. Fully reuses the Area-1/Area-4 routing path.
- **D-10:** The over-enqueue class (prior "Recover orphaned work" incident) is closed by (a) the explicit `ANALYSIS_FAILED ∧ duration≥threshold` filter — not a whole-backlog sweep — and (b) the deterministic-key dedup at the `before_enqueue` chokepoint, so a double-click collapses to a no-op.

### Routing & kind-aware agent selection
- **D-11:** Per-file routing replaces the current all-files-to-one-queue enqueue in `trigger_analysis` / `_enqueue_analysis_jobs`: each DISCOVERED file is routed individually by duration (short→local, long→compute/`AWAITING_CLOUD`).
- **D-12:** The "Run analysis" response reports the **split counts** (e.g. "Enqueued 50 local, 12 cloud, 5 awaiting cloud") so the operator sees how the corpus routed.
- **D-13:** Extend `select_active_agent` with a **kind filter**: long files select the most-recently-seen `kind='compute'` agent; short files select the most-recently-seen `kind='fileserver'` agent. Keeps the existing deterministic "most-recently-seen, non-revoked" rule, just scoped by kind. This prevents a compute agent (no media/ORM access) from winning short-file selection. Round-robin / least-loaded dispatch stays deferred (CLOUDROUTE-05 out of scope).

### Claude's Discretion
- D-07 threshold knob naming/default (convention match to `straggler_threshold_sec`).
- Exact wiring of the kind-filtered selection (new param vs. sibling helper) and where the per-file routing loop lives, provided the kind boundary in D-13 holds.
- Backfill response-partial copy and count formatting.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase scope & requirements
- `.planning/ROADMAP.md` §"Phase 49: Duration routing & backfill" — goal, success criteria, dependencies (Phase 48 compute agent, Phase 45 ledger).
- `.planning/REQUIREMENTS.md` — CLOUDROUTE-01..04 (in scope), CLOUDROUTE-05 (deferred, out of scope).

### Routing & queue plumbing (the primary change surface)
- `src/phaze/services/enqueue_router.py` — `resolve_queue_for_task`, `select_active_agent` (extend with kind filter), `NoActiveAgentError`, `AGENT_TASKS`/`CONTROLLER_TASKS`.
- `src/phaze/services/agent_task_router.py` — `AgentTaskRouter.queue_for(agent_id)` per-agent queue resolution.
- `src/phaze/routers/pipeline.py` — `trigger_analysis` + `_enqueue_analysis_jobs` (all-to-one-queue today; becomes per-file), dashboard count surfacing (`analysis_failed_count`, `straggler_count`).
- `src/phaze/services/pipeline.py` — `get_files_by_state`, count helpers, `_safe_count`, `FileMetadata` join patterns.

### State model
- `src/phaze/models/file.py` — `FileState` StrEnum (add `AWAITING_CLOUD`), `FileRecord` (`state String(30)`).
- `src/phaze/models/metadata.py` — `FileMetadata.duration` (`Float`, nullable, seconds).
- `src/phaze/models/agent.py` — `Agent.kind` (`fileserver`/`compute`, Phase 48), `last_seen_at`, `revoked_at`.

### Ledger / recovery (backfill scoping + held-file release)
- `src/phaze/services/scheduling_ledger.py` — `insert_ledger_if_absent`, `upsert_ledger_entry`, `routing_for_function`.
- `src/phaze/tasks/reenqueue.py` — Phase 32/45 controller reenqueue (startup + cron */5); `_DOMAIN_COMPLETED_STAGES`, ledger-driven replay; extend to release `AWAITING_CLOUD`.
- `src/phaze/models/scheduling_ledger.py` — `SchedulingLedger` row shape.
- `src/phaze/tasks/_shared/deterministic_key.py` — `_KEY_BUILDERS` (deterministic-key dedup, totality test).

### Config
- `src/phaze/config.py` — settings conventions (`straggler_threshold_sec` at ~L350 as the template for `cloud_route_threshold_sec`; `AliasChoices`/`PHAZE_*`).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `select_active_agent` + `RoutedQueue` (enqueue_router): the deterministic agent-selection rule to extend with a kind filter rather than rewrite.
- `insert_ledger_if_absent` + deterministic-key dedup: the exact primitives the backfill and held-file release need for safe, idempotent re-drive (no whole-backlog over-enqueue).
- Pipeline dashboard `_safe_count` + count-card pattern: drop-in for the "Awaiting cloud" card.
- Controller reenqueue path (reenqueue.py): self-healing infra to extend for `AWAITING_CLOUD` release — no new trigger site needed.
- `Agent.kind` (Phase 48): the `compute`/`fileserver` discriminator routing keys off.

### Established Patterns
- Control-side enqueue routes through a single chokepoint (`resolve_queue_for_task`) that never targets the consumer-less default queue — per-file routing must keep that invariant.
- `FileState` is a code-only StrEnum over a `String(30)` column → new states need no migration (precedent: `ANALYSIS_FAILED`).
- Recovery/reenqueue is ledger-scoped + domain-completed-predicate gated; any new pending state must be classified correctly (pending vs. done).
- Threshold/timeout settings use `*_threshold_sec` / `*_sec` `Field` + `PHAZE_*` `AliasChoices`.

### Integration Points
- `trigger_analysis` / `_enqueue_analysis_jobs` — the per-file routing fork (short/long/awaiting) and the split-count response.
- `enqueue_router.select_active_agent` — kind-filtered selection.
- `reenqueue.py` — `AWAITING_CLOUD` release sweep.
- Pipeline dashboard template + router — new count card + "Backfill to cloud" button/partial.
- `config.py` — `cloud_route_threshold_sec`.

</code_context>

<specifics>
## Specific Ideas

- The 144 backfill targets are precisely `ANALYSIS_FAILED ∧ duration ≥ threshold` — not all `ANALYSIS_FAILED`.
- "Never silently analyze a long file locally" is the load-bearing safety invariant (Success Criterion 3 / CLOUDROUTE-02): when in doubt about routing, the only safe non-local fallback is `AWAITING_CLOUD`, and the only safe local fallback is for genuinely short/unknown-short files (D-06).

</specifics>

<deferred>
## Deferred Ideas

- Cost/throughput-aware routing beyond a fixed duration threshold — CLOUDROUTE-05, explicitly out of scope this milestone.
- Round-robin / least-loaded dispatch among multiple compute agents — deferred; most-recently-seen kind-filtered is sufficient for the single-A1 milestone.
- Click-through drill-down list for the "Awaiting cloud" count card — count-only for now.
- Backfill dry-run/preview count before enqueue — considered (option offered) but operator chose the plain button; the explicit filter + dedup already guard the over-enqueue class.

None of the above are blockers; discussion stayed within phase scope.

</deferred>

---

*Phase: 49-duration-routing-backfill*
*Context gathered: 2026-06-25*
