# Phase 71: Deployment, Config, Docs & N-Lane UI - Context

**Gathered:** 2026-07-04
**Status:** Ready for planning

<domain>
## Phase Boundary

Presentation + ops close-out for the 2026.7.1 Multi-Cloud Backends milestone — the milestone's
**last** phase, a **frontend + docs** phase. Delivers three operator-facing surfaces over the
now-proven scheduler and multi-Kueue routing (phases 67–70):

1. **BEUI-01 — N-lane UI**: generalize v7.0 Phase 58's fixed 3 lane cards (local/A1/k8s) into
   **N registry-derived per-backend lanes**, each showing available/offline, in-flight/cap, and
   rank, with the Kueue **quota-wait-vs-Inadmissible distinction preserved and attributed per lane
   by `backend_id`**. Read-only, riding the existing `/pipeline/stats` 5s poll — **no second poll
   loop** (WORK-05/R-2).
2. **BEUI-02 — master toggle**: a live one-click revert-all-routing-to-local control for incident
   response (the `backends`-era equivalent of today's `cloud_target=local` no-op gate).
3. **BEUI-03 — docs**: a new operator runbook + `configuration.md` coverage of the `backends:`
   schema, per-backend `_FILE` secrets, and the (now-trivial) `cloud_target`→`backends` deprecation.

**No routing-algorithm / cap / reconcile behavior change** — routing, per-backend caps, and
per-cluster reconcile already shipped in 67–70. The ONE sanctioned new routing-path control is the
BEUI-02 force-local gate (that IS the requirement). The per-lane data the UI renders is *conceptually*
computed by 69–70 but only as the drain tick's **transient in-tick `BackendSlot`** — this phase adds
its own **read-only** path to that data (D-01).

**Out of scope (deferred):** new concrete cloud providers + the compute `agent_ref` resolution fix
(PROV-01, Future Requirements — deferred from Phase 70 D-05); instance provisioning; dollar-cost
model; weighted fair-share (milestone non-goals).

</domain>

<decisions>
## Implementation Decisions

### Lane data plumbing — BEUI-01 (how per-backend state reaches the template)
- **D-01 (new read-only snapshot service):** Add a dedicated `get_backend_lane_snapshot(session)`
  service that reads the Phase-67 `backends` registry, COUNTs `cloud_job` per `backend_id` (the same
  D-02 in-flight substrate `select_backend` uses), and returns a **list of lane dicts** — one per
  registry backend. Pure read, degrade-safe (returns `[]` on any DB error, matching the existing
  never-500 `_safe_count` idiom), **zero coupling to the advisory-locked drain tick**. Chosen over
  reusing the drain's transient `BackendSlot` (that snapshot is built for dispatch decisions, exists
  only mid-tick, and would couple the UI to the locked scheduler path).
- **D-02 (live `is_available()` probe per poll, bounded + isolated):** Each lane's available/offline
  status comes from a **live `is_available()` probe on every poll** (freshest signal — operator
  preference over cached signals). Guardrails are load-bearing: probes are fired **concurrently**
  (`asyncio.gather`), each wrapped in **its own short timeout (~1–2s) + try/except** (extends Phase 70
  D-07 per-backend isolation). A timing-out/raising backend renders **`offline` for that poll** while
  every other lane is unaffected; the whole fan-out is bounded to ~one timeout so a hung cluster can
  **never stall the shared `/pipeline/stats` poll**. Planner must confirm the exact timeout value +
  that the local backend's cheap `is_available()` isn't penalized.
- **D-03 (per-lane admission attribution):** The snapshot service ALSO returns **per-`backend_id`**
  admission state (cloud_phase counts / inadmissible / localqueue-unreachable, GROUP BY `backend_id`
  — now possible because Phase 70 made reconcile per-`backend_id`), so **each lane card carries its
  own quota-wait-vs-Inadmissible attribution** (the explicit BEUI-01 requirement). One read path
  feeds both the lane grid and the per-lane state.
- **D-04 (join into BOTH context builders):** The lane list must be seeded **identically** in
  `build_dashboard_context()` (full-page load) AND `pipeline_stats_partial()` (the 5s OOB re-push),
  matching the existing seeded-identically discipline for the cloud counts — so the lanes refresh on
  the single existing poll with no second loop and no new backend endpoint for reads.

### N-lane card layout — BEUI-01
- **D-05 (responsive auto-fit wrapping grid):** Replace the fixed `grid grid-cols-3` in
  `analyze_workspace.html` with a **responsive wrapping grid** (e.g. `sm:grid-cols-2 lg:grid-cols-3`
  auto-fit) — 1 lane fills a sensible width, N lanes wrap to rows. **Card size/rhythm unchanged**
  from Phase 58 (`_lane_card.html` stays the per-card unit, now `{% for %}`-looped instead of
  included 3× by hand). Exact Tailwind classes are planner/UI-phase discretion within the C3
  two-weight contract.
- **D-06 (order by rank ascending):** Lane cards render **lowest-rank first** (most-preferred/cheapest
  capacity first; local rank-99 naturally lands last), **tie-broken by `id`** for stable order.
  Mirrors the scheduler's own preference order so the grid reads top-to-bottom as "what gets used
  first." Each card shows **rank** (new vs the Phase-58 card, which had no rank) alongside
  in-flight/cap + the available/offline word-label (never hue-only — WCAG 1.4.1, per the existing
  `_lane_card.html` contract).
- **D-07 (6 global cloud-state cards → Claude's discretion, lean keep-as-roll-up):** The 6 existing
  global cards (admission/inadmissible/localqueue/awaiting/pushing/analyzing) are **planner/UI-phase
  discretion**. Recommended lean: **keep them as an overall cross-lane roll-up below the lane grid**
  (lowest risk — no restyle, WORK-03 alert cards + their OOB swap ids untouched, preserves the
  cross-lane total at a glance) now that per-lane detail lives on the cards (D-03). Folding them
  entirely into lanes + deleting the globals is the higher-risk alternative and is NOT the default.

### Master toggle — BEUI-02
- **D-08 (live one-click runtime toggle):** A **live UI toggle that immediately forces all routing to
  local** — no redeploy, reversible — the true "incident response" behavior BEUI-02 names. This adds
  **one force-local gate read** to the routing path; that read IS the requirement (the routing
  algorithm, ranks, and caps are otherwise unchanged). Chosen over a config-only "edit registry
  all-local & restart" gate (incident response can't wait on a redeploy).
- **D-09 (persist in a DB control row):** The force-local override is persisted in a **small DB
  control row**, mirroring the existing **per-stage pause/priority control-table pattern**
  (`services/pipeline.py` `_DEFAULT_CONTROLS` / the Phase 37/38 control table). Survives restarts,
  single source of truth that both the router and the drain tick already have a DB session for,
  testable. Read in `select_backend`/the router the same way `paused` is read. Chosen over a Redis
  flag (would add a Redis dependency to the routing decision; more ephemeral than the DB control the
  scheduler already consults).
- **D-10 (global header status strip):** The toggle lives in the **persistent header status strip**
  — always visible on every page so it's one click from anywhere mid-incident, matching its global
  cross-stage scope. Writing it is a **new thin write endpoint** (the sanctioned Review-&-Apply-era
  thin-endpoint pattern). Planner decides confirm-vs-instant UX; note it's reversible (a reversible
  control needs no confirm per the R-4 copy rule) — reverting to local is the safe direction, so
  instant-on is defensible.

### Docs & deprecation — BEUI-03
- **D-11 (new runbook + update configuration.md):** Add a **new `docs/runbook.md`** (operator ops:
  the master toggle / incident revert procedure, how to read the N lanes, spillover behavior,
  per-backend `_FILE` secrets) AND update **`docs/configuration.md`** with the `backends:` schema +
  `_FILE` secrets + a **short** deprecation note. `docs/cloud-burst.md` / `docs/k8s-burst.md` get
  pointers to the unified `backends` model. The runbook earns its place on the toggle + per-lane
  reading + spillover content **independent of migration**.
- **D-12 (`cloud_target` = docs-only note, no migration guide):** Per operator: **no one ever
  deployed `cloud_target=a1|k8s` live — there is no config to migrate** (matches the ~zero-live-rows
  note carried from Phases 68/70). So the requirement's "migration path" collapses to: document
  `backends:` as the way and show the trivial **1:1 `cloud_target`→`backends` equivalence** for
  anyone reading old configs. **No migration runbook, no removal date.**
  **FACTUAL CORRECTION (Phase-71 research A1, 2026-07-04):** the discuss-phase premise that a
  `cloud_target=a1|k8s` **back-compat shim still exists** was WRONG — **Phase 67 already removed
  `cloud_target` entirely, with no shim** (`docs/configuration.md:123`), and Pydantic
  `extra="ignore"` **silently drops** a stale `PHAZE_CLOUD_TARGET` env var. There is nothing to
  "keep working." So the docs must say `cloud_target` was **removed in Phase 67 — use `backends:`**
  (not "deprecated but still works").
- **D-13 (docs-only — startup deprecation log DROPPED):** Superseded by the A1 correction + operator
  decision (2026-07-04). The originally-planned "one-line startup deprecation log when `cloud_target`
  is set" is **DROPPED**: there is no shim to warn about and the env var is already silently ignored,
  so a log would require brand-new env-var-detector code for a variable nobody ever set — rejected as
  over-engineering. **BEUI-03 deprecation coverage is DOCS-ONLY**: `configuration.md` states
  `cloud_target` is removed + shows the 1:1 `backends:` equivalence. **No new runtime code for
  deprecation.**

### Claude's Discretion (planner / UI-phase decides)
- Fate of the 6 global cloud-state cards (D-07) — lean keep-as-roll-up, but planner/UI-phase may fold.
- Exact responsive grid Tailwind classes (D-05) and card rank presentation within the C3 two-weight contract.
- Exact `is_available()` probe timeout value + concurrency mechanics (D-02); whether the local backend probe is short-circuited (it holds no network dependency).
- Master toggle confirm-vs-instant UX (D-10) — reversible control, instant-on defensible.
- Exact DB control-row shape/name for the force-local override (D-09) — follow the pause/priority control-table schema.
- `docs/runbook.md` vs `configuration.md` exact section split (D-11) once the planner sees the existing docs' structure/cross-refs.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & roadmap (authoritative scope)
- `.planning/REQUIREMENTS.md` §BEUI (BEUI-01, BEUI-02, BEUI-03) — the three deployment/config/docs/UI
  requirements this phase closes; §Out of Scope + §Future Requirements (PROV-01) for the deferred
  compute-provider / `agent_ref` work.
- `.planning/ROADMAP.md` → "Phase 71: Deployment, Config, Docs & N-Lane UI" — goal + success criteria
  (N lanes riding the existing `/pipeline/stats` poll, master revert toggle, runbook/config/migration
  docs) + the 2026.7.1 execution discipline (**PR on a worktree branch, NEVER a direct commit to
  `main`**; presentation/ops only).

### Design spine
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` (PR #182) — the `backends`
  registry / `Backend` protocol / tiered scheduler design this UI surfaces. **NOTE:** its §6/§7
  "one shared bucket" framing is superseded by REG-05 (not relevant to this UI/docs phase, but don't
  reintroduce the stale framing in docs).

### Prior-phase context this phase builds on
- `.planning/phases/70-multi-kueue-n-clusters/70-CONTEXT.md` — Phase 70 made reconcile
  **per-`backend_id`** (the basis for D-03 per-lane admission attribution) + D-07 per-backend
  try/except isolation (the basis for D-02's bounded probes).
- `.planning/phases/69-tiered-drain-scheduler/69-CONTEXT.md` — the `BackendSlot` snapshot +
  `select_backend` rank-first dispatch the lane UI mirrors (rank ordering D-06) and where the
  force-local gate (D-08/D-09) reads.
- `.planning/phases/67-backend-registry-config-model/67-CONTEXT.md` — the `backends` registry submodel
  (`id`/`kind`/`rank`/`cap`) + `cloud_enabled` config gate + `_FILE` secrets the docs (D-11) cover.

### Existing code the phase edits (see Code Context below for detail)
- `src/phaze/templates/pipeline/partials/analyze_workspace.html` — the fixed 3-card grid → N-loop (D-05).
- `src/phaze/templates/pipeline/partials/_lane_card.html` — the reusable per-lane card (now looped).
- `src/phaze/routers/pipeline.py` — `build_dashboard_context()` + `pipeline_stats_partial()` (D-04 seed).
- `src/phaze/services/backends.py` / `backend_selection.py` — where `get_backend_lane_snapshot` lands + the force-local read.
- `src/phaze/services/pipeline.py` — the per-stage pause/priority control table pattern to mirror for D-09.
- `docs/configuration.md` (update) + `docs/runbook.md` (new); `docs/cloud-burst.md`, `docs/k8s-burst.md` (pointers).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `_lane_card.html` (Phase 58): the reusable execution-lane summary card — title row (emoji + LANE ·
  NODE), mono capacity numeral, h-1.5 capacity bar, sub-label; already handles online/offline/not-
  configured word-labels (WCAG 1.4.1). BEUI-01 loops this over N lanes instead of including it 3× by
  hand. It's pure presentation (no `hx-trigger`/`setInterval` — single-poll discipline).
- The per-stage **pause/priority DB control table** (`services/pipeline.py` `_DEFAULT_CONTROLS`,
  `get_stage_controls` overlay pattern): the proven runtime-mutable control read by the router — the
  direct model for the D-09 force-local control row.
- The never-500 `_safe_count` / SAVEPOINT degrade idiom used by every cloud-state count read
  (`get_awaiting_cloud_count`, `get_cloud_phase_counts`, `get_inadmissible_count`, etc.) — the
  degrade contract `get_backend_lane_snapshot` (D-01) must follow (`[]` on error).
- `Backend.is_available()` / `in_flight_count()` (services/backends.py `_BaseBackend`) — the per-
  backend probe + `cloud_job` COUNT the snapshot service reuses (D-01/D-02).

### Established Patterns
- **Single-poll discipline (WORK-05/R-2):** the whole chrome refreshes on ONE `/pipeline/stats` 5s
  poll; `build_dashboard_context()` and `pipeline_stats_partial()` seed context **identically** so
  OOB swaps land. New lane data must ride this (D-04), NOT a second loop.
- **`$store.pipeline` keys vs OOB card swaps (Pitfall 2):** local/A1 capacity binds to Alpine store
  keys; k8s counts arrive as OOB card swaps (not store keys). The N-lane redesign must decide per
  datum which mechanism carries it — planner/UI-phase concern.
- **C3 "Evolved phaze" + two-weight type contract:** Jura headings, blue accent, dark `phaze-bg` +
  light toggle; capacity numerals are Inter 500 (NOT semibold). Preserve.
- **`cloud_lane_kind` is transitional:** `resolved_non_local_kind(settings)` returns a single legacy-
  shaped kind ("local"/"compute"/"kueue") and CANNOT represent N same-kind backends (e.g. 2 Kueue
  clusters). BEUI-01 **replaces** it with the D-01 per-lane list. Its `# TRANSITIONAL — Phase 71`
  comment marks the removal site.

### Integration Points
- `get_backend_lane_snapshot()` (new) → seeded in both context builders in `routers/pipeline.py` →
  consumed by the looped `_lane_card.html` in `analyze_workspace.html`.
- Force-local control row (new) → written by a new thin endpoint (header toggle) → read in
  `select_backend`/router where `cloud_enabled` / pause controls are already consulted.
- BEUI-03 deprecation (D-12/D-13) → **docs-only**, NO code integration point. `cloud_target` was
  already fully removed in Phase 67 (`config.py`); `configuration.md` documents that removal + the
  1:1 `backends:` equivalence. (Original "startup deprecation log" integration point DROPPED — see
  D-13 correction.)

</code_context>

<specifics>
## Specific Ideas

- Operator was explicit: **nobody used k8s or cloud burst yet — there's no config to migrate.** Keep
  the migration docs trivial (1:1 equivalence for completeness only); do NOT write a heavyweight
  migration runbook (D-12).
- The N-lane grid should read top-to-bottom as the scheduler's own preference order (rank ascending,
  D-06) — so an operator glancing at the grid sees "what gets used first."
- Freshness over caching for lane availability (D-02) — operator chose live probes despite the
  latency tradeoff, accepting the bounded-timeout/isolation mitigation.

</specifics>

<deferred>
## Deferred Ideas

- **PROV-01** (new concrete cloud providers + the `ComputeAgentBackend` `agent_ref → Agent.id`
  resolution fix) — Future Requirements, deferred from Phase 70 D-05. The `agent_ref` field exists on
  config but is unused by the service impl; only bites once a 2nd compute provider coexists.
- **Folding the 6 global cloud-state cards fully into per-lane cards** (removing the global roll-up) —
  considered under D-07, deferred as higher-risk; default keeps them as a roll-up. A future UI polish
  pass could revisit once N-lane usage is observed.
- **`cloud_target` removal** — already DONE in Phase 67 (no shim exists; research A1). Nothing to
  remove this phase; BEUI-03 just documents the removal (D-12/D-13, docs-only).

</deferred>

---

*Phase: 71-deployment-config-docs-n-lane-ui*
*Context gathered: 2026-07-04*
