# Phase 88: Lane / Agent Drill-In - Context

**Gathered:** 2026-07-11
**Status:** Ready for planning

<domain>
## Phase Boundary

Add two **clickable drill-in detail views** over the derived `stage_status` layer (Phases 78/82):

- **DRILL-01** — a backend-**lane** card (rendered by `_lane_card.html` in the analyze workspace)
  becomes clickable → **new** `GET /pipeline/lanes/{backend_id}` showing that lane's queues / in-flight /
  waiting / quota / recent completions.
- **DRILL-02** — an **agent** row (rendered by `admin/partials/agents_table.html`) becomes clickable →
  **new** `GET /admin/agents/{agent_id}/_activity` showing the agent's **owned files grouped by derived
  `stage_status`**, recent scan batches, per-lane queue depths, and liveness.
- **DRILL-03** — both drill-ins **survive the 5s poll swap** and are **keyboard-accessible**.

Requirements DRILL-01/02/03 (`.planning/REQUIREMENTS.md:89-91`) lock the endpoints and WHAT each returns.
This discussion decided **how they render, survive the poll, and behave** — not what to build.

**In scope:**
- Make lane cards and agent rows clickable drill-in triggers (they currently live *inside* their page's
  `hx-swap="outerHTML"` 5s poll region).
- The two new read-only detail endpoints and their rendered pane bodies.
- The DRILL-03 poll-survival mechanism + the keyboard/a11y model.
- A shared detail-pane shell reused across both surfaces.

**Out of scope:**
- Any change to the derived-status **derivation contract** (`done_clause`/`failed_clause`/`inflight_clause`/
  `stage_status_case`/`eligible_clause`) — owned by Phases 78/82, consumed here read-only.
- Any change to lane snapshot production (`get_backend_lane_snapshot`) or agent classification
  (`classify_compute_lanes`) beyond reading them for the detail bodies.
- The Phase-87 per-**file** stage matrix / right pane, and the force-skip / priority / retry surfaces
  (Phase 87). This phase is the **lane/agent** drill-in twin, not the file surface.
- `files.state` column drop / `FileState` enum deletion — Phase 90. Legacy sentinel retire — Phase 89.
- Any change to routing *policy*, approval semantics, or the tag/CUE bulk builders.

</domain>

<decisions>
## Implementation Decisions

### Carried forward (from Phase 87 — do not re-litigate)
- **D-00a: `stage_status_case(stage)` is the single derivation** (`services/stage_status.py`). The agent
  detail's stage grouping reads it — no second status-derivation path. (Phase 82 D-04 / Phase 87 D-00a.)
- **D-00b: Never render a whole-corpus scan per poll.** Every derived read is bounded and
  `_safe_count`/SAVEPOINT-degrade-safe (degrade to 0/None, never 500 the poll). Applies to both detail
  endpoints AND their live-refresh ticks. (PERF-01 / Phase 87 D-00c.)

### Surface & poll-survival (DRILL-01/03)
- **D-01: Detail renders in a SIBLING pane that is a separate swap target OUTSIDE the polled list region.**
  The lane cards (`#analyze-lanes` grid) and agent rows (`#agents-table-section`) keep OOB-swapping on
  their existing 5s poll; the detail pane is a distinct element the poll does not touch, so it is never
  clobbered. (Rejected: modal/dialog — overlays and hides the list, heavier for a glance-and-dismiss
  operator flow; dedicated full page — loses side-by-side list context, full nav round-trip per drill-in.)
- **D-02: Selection carried via a URL param** — `?lane={backend_id}` / `?agent={agent_id}` set with
  `hx-push-url` on the trigger. This satisfies **both** DRILL-03 escape hatches at once: (a) the pane lives
  outside the polled region, AND (b) the param lets the polled list re-render the **selected-card
  highlight** after each swap and lets a **page reload restore the open detail**. Both DRILL-03 mechanisms
  are used together, not either/or.
- **D-03: The open detail live-refreshes on its OWN bounded tick** (its own `hx-trigger="every 5s"` scoped
  to that lane/agent), so in-flight counts / queue depths / liveness stay current while the operator
  watches — consistent with the console's live cadence. The tick is a single bounded per-lane/per-agent
  read (D-00b) and **stops when the detail is dismissed** (no orphan loop). (Rejected: snapshot-on-click —
  numbers go stale while open, forces re-click.)

### Agent-activity grouping (DRILL-02)
- **D-04: Owned files grouped as a per-agent 6-stage matrix of COUNTS.** Reuse the Phase-87 stage model —
  6 stages (Meta / FP / Analyze / Propose / Approve / Execute) × the `stage_status_case` buckets
  (done / in-flight / not-started / failed) — as **aggregate counts filtered to `agent_id`**. This MUST be
  a SQL `GROUP BY` aggregate over the `stage_status_case` expression (bounded, one indexed aggregate
  query), **NOT** row-by-row materialization of the agent's files — an agent may own a large share of the
  200K corpus, so counts scale, materialized lists do not (D-00b). (Rejected: expandable per-bucket file
  lists — richer but adds paginated per-bucket queries, defer as a possible later enhancement; single
  4-bucket roll-up — loses the per-stage "where is this agent's work stuck?" insight DRILL-02 asks for.)
- **D-05: Agent pane = stacked vertical sections.** Order: **liveness header always on top** (last-seen /
  status badge — reuse `_kind_badge.html` + the existing heartbeat/last-seen pattern) → the D-04 stage
  grouping (primary content) → per-lane queue depths → recent scan batches (last N). Everything visible on
  open, scroll for depth; matches the existing admin/agents table idiom. (Rejected: tabbed sections — adds
  Alpine tab state and hides content behind clicks, more than an operator glance needs.)

### Lane-detail depth (DRILL-01)
- **D-06: Kind-adaptive field set.** Common fields for every lane (rank, in-flight/cap, availability,
  recent completions); kind-specific fields only where they actually exist — **quota-waiting +
  Inadmissible for kueue only** (already in the snapshot dict), the short/long-set caption for
  local/compute. No fabricated zero/"n/a" quota rows on non-kueue lanes — preserve the card layer's
  kind-disambiguation. Mirrors the `_lane_card.html` lane-dict contract. (Rejected: uniform field set with
  n/a fillers — muddies kind disambiguation.)
- **D-07: Recent completions bounded by last-N (newest-first).** A fixed small count (planning picks the
  exact N, e.g. ~20) rather than a time window — predictable render cost under any throughput, honors
  D-00b regardless of a burst. (Rejected: trailing time window — unbounded row count under a burst, would
  need its own cap anyway.)

### Cross-surface consistency & a11y (DRILL-03)
- **D-08: One shared detail-pane shell.** A single reusable pane partial (proposed `_detail_pane.html`)
  owns the swap-target container, the `?param` / `hx-push-url` wiring, open/dismiss behavior, and the
  keyboard/a11y contract; the lane body and agent body are parameterized content slots (e.g.
  `_lane_detail.html` + `_agent_activity.html`). One place to keep the DRILL-03 poll-survival + a11y
  contract correct across both the pipeline page and the admin page. (Rejected: page-native each —
  implements + must keep the contract correct twice, drift risk.)
- **D-09: Full keyboard loop — `role=button` triggers + Esc dismiss + focus restoration.** Cards/rows get
  `role=button`, `tabindex=0`, Enter/Space activation, and a visible focus ring (reuse existing
  `focus-visible` tokens). The open pane is focusable, has a **visible Close control AND Esc-to-dismiss**,
  and **dismissing clears the `?param` and returns focus to the originating card/row**. Exceeds DRILL-03's
  literal minimum (role=button / Enter/Space / focus ring) with a complete WCAG dismiss + focus-return
  loop. (Rejected: close-only without Esc/focus-return — meets the literal checklist but weaker keyboard
  dismiss.)

### Claude's Discretion
- **Exact N for lane recent completions** (D-07) and the precise ordering/labels within the agent stacked
  sections (D-05) — planning's call, constrained by bounded/derived reads.
- **Where the sibling pane physically sits** per host page (right column vs below the grid) and its
  responsive collapse on narrow screens — constrained by D-01 (separate swap target, outside polled region).
- **Whether per-lane queue depths in the agent pane reuse an existing lane snapshot read** or a scoped
  per-agent variant — planning picks, constrained by D-00b.
- **Plan/PR decomposition** — natural seams: (a) the shared `_detail_pane.html` shell + trigger a11y
  wiring + poll-survival URL-param mechanism (D-01/D-02/D-08/D-09); (b) the lane-detail endpoint + body
  (DRILL-01, D-06/D-07); (c) the agent-activity endpoint + per-agent aggregate stage grouping + pane
  (DRILL-02, D-04/D-05). Small blast-radius per PR is the milestone's standing rule.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone contract & requirements
- `.planning/REQUIREMENTS.md:89-91` — DRILL-01/02/03 full text (endpoints, what each returns, poll-survival
  + `role=button`/Enter/Space/focus-ring a11y); PERF-01 (no whole-corpus poll) and the anti-feature table.
- `.planning/ROADMAP.md` §"Phase 88" — goal + 3 success criteria; depends on Phase 87, Phase 78.
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` — §5 (YAGNI / derive-don't-store), §8 (constraints:
  90% cov, per-bucket test isolation).

### Sibling phase contracts (locked — do not re-litigate)
- `.planning/phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-CONTEXT.md` — the
  file-surface twin; D-00c (never a whole-corpus scan per poll), `_safe_count`/SAVEPOINT degrade pattern,
  the `stage_status_case` pill matrix + `_stage_matrix.html` tokens the D-04 agent grouping reuses.
- `.planning/phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-CONTEXT.md` — the
  single-source predicate module; the DERIV-04 SQL⇔Python equivalence discipline (the agent grouping reads
  the same `stage_status_case`, does not fork it).
- `.planning/phases/82-counts-pending-set-cutover/82-CONTEXT.md` — `stage_status_case` as the single
  4-bucket CASE; `get_stage_progress` four-bucket aggregate pattern the per-agent count aggregate mirrors.

### Source of truth in code
- `src/phaze/services/stage_status.py` — `done_clause`/`failed_clause`/`inflight_clause`/`stage_status_case`
  (the agent grouping aggregates over these, filtered to `agent_id`; read-only).
- `src/phaze/services/backends.py:756` — `get_backend_lane_snapshot()`: the per-lane dict contract
  (`id/kind/rank/cap/in_flight/available/quota_wait/inadmissible`) the D-06 lane detail reads; kueue-only
  fields live here.
- `src/phaze/services/pipeline.py:204` (group-by-`agent_id` precedent), `:302/:350` `get_stage_progress`
  (four-bucket `GROUP BY status` aggregate — the shape the per-agent D-04 aggregate mirrors).
- `src/phaze/routers/admin_agents.py` — `page` (`GET ""`) + `table_partial` (`GET /_table`, the 5s poll
  target); `_load_agents` + `classify_compute_lanes`. The new `GET /admin/agents/{agent_id}/_activity`
  lands here.
- `src/phaze/routers/pipeline.py` — hosts the analyze workspace render; the new `GET /pipeline/lanes/{backend_id}`
  lands here (or a sibling pipeline router).
- `src/phaze/templates/pipeline/partials/_lane_card.html` — the lane card to make clickable (DRILL-01
  trigger); lane-dict contract documented in its header comment.
- `src/phaze/templates/pipeline/partials/analyze_workspace.html` — the `#analyze-lanes` grid host (the
  polled region the pane must sit OUTSIDE of).
- `src/phaze/templates/admin/partials/agents_table.html` — `#agents-table-section` (the `outerHTML` 5s
  swap root; agent rows are the DRILL-02 triggers, pane must sit outside this section).
- `src/phaze/templates/admin/partials/_kind_badge.html` — reuse for the agent-pane liveness header (D-05).
- `src/phaze/templates/shell/partials/cmdk_modal.html` — existing focus/keyboard-dialog precedent to
  reference for the D-09 a11y model (even though D-01 chose a pane over a modal).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`get_backend_lane_snapshot()`** (`backends.py:756`) — already produces the per-lane dict the lane
  detail (D-06) reads; kueue-only `quota_wait`/`inadmissible` already present, so kind-adaptive fields are
  a read, not new derivation.
- **`stage_status_case` + `_stage_matrix.html`** (Phase 87) — the per-agent stage grouping (D-04) reuses
  the same buckets/tokens as an aggregate, avoiding a second matrix definition.
- **`get_stage_progress`** (`pipeline.py:302/:350`) — the `GROUP BY status` four-bucket aggregate is the
  template for the per-agent-per-stage aggregate.
- **`_kind_badge.html` + `_load_agents`/last-seen** (`admin_agents.py`) — liveness header for D-05.
- **`cmdk_modal.html`** — focus/keyboard precedent for the D-09 dismiss/focus model.

### Established Patterns
- **outerHTML 5s self-poll** — both `#agents-table-section` and `#analyze-lanes` re-emit their own
  `hx-trigger="every 5s"` and swap as a unit. The detail pane must be a **separate** swap target so the
  poll never clobbers it (D-01), and the poll endpoints must read the `?param` to re-render the selected
  highlight (D-02).
- **`_safe_count` / `begin_nested()` SAVEPOINT degrade** — every derived read on the poll degrades to
  0/None rather than 500. Both detail endpoints and their live-refresh ticks inherit this (D-00b).
- **Server-rendered scalars + Jinja autoescape** — lane/agent detail values are server-rendered scalars;
  operator-declared ids/kinds stay autoescaped (T-71-05 precedent).

### Integration Points
- The two triggers live *inside* polled swap regions (`_lane_card.html` in `#analyze-lanes`;
  agent rows in `#agents-table-section`) — making them clickable must not break the OOB swap; the pane
  must render outside these regions.
- The lane detail lives on the pipeline/analyze page; the agent detail on the admin/agents page — the
  shared `_detail_pane.html` shell (D-08) is the one abstraction spanning both hosts.
- Per-lane queue depths appear in BOTH the lane detail (DRILL-01) and the agent detail (DRILL-02) — a
  chance to share one queue-depth read.

</code_context>

<specifics>
## Specific Ideas

- **Poll-survival mock (D-01/D-02):**
  ```
  ┌─ lanes/agents (polls 5s) ─┐ ┌─ #detail-pane ───┐
  │ [card] [card*selected]    │ │ Lane: k8s-a      │
  │ [row ] [row ]  ...        │ │ in-flight 3/8    │
  │  ↑ hx-get→#detail-pane    │ │ waiting · quota  │
  │  ↑ hx-push-url ?lane=k8s  │ │ recent ✓✓✓       │
  └───────────────────────────┘ └──────────────────┘
    reload w/ ?lane= re-opens · poll keeps highlight
  ```
- **Agent stage-grouping mock (D-04):**
  ```
  Agent nox · owns 4,201 files
           done  ● infl  — not  ✗ fail
  Meta     4,201    0      0     0
  FP       4,180    9     12     0
  Analyze  3,900   40    257     4
  Propose  3,010    0  1,191     0  ...
  ```
- **Agent pane order (D-05):** liveness header (`█ alive · seen 3s ago`) → stage matrix → queue depths
  (`analyze 12 · io 3`) → recent scans (`batch#88 ✓ …`).
- **A11y model (D-09):** `card: role=button tabindex=0` Enter/Space → open, `:focus-visible` ring;
  `pane: [✕ Close]` Esc → dismiss → clears `?param`, focus returns to originating card/row.

</specifics>

<deferred>
## Deferred Ideas

- **Expandable per-bucket file lists in the agent pane** — clicking a stage-bucket count to expand into a
  paginated list of those files (jump from "4 failed analyze" straight to the files). Considered under D-04;
  deferred as a possible later enhancement to keep this phase's per-agent reads to bounded aggregates.
- **Legacy `legacy-application-server` sentinel retire** → **Phase 89** (LEGACY-01..03).
- **`files.state` column drop + `FileState` enum deletion + remaining `.state=` writers** → **Phase 90**.

None else — discussion stayed within phase scope.

</deferred>

---

*Phase: 88-Lane / Agent Drill-In*
*Context gathered: 2026-07-11*
