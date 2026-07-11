# Phase 88: Lane / Agent Drill-In - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-11
**Phase:** 88-Lane / Agent Drill-In
**Areas discussed:** Surface + poll-survival, Agent-activity grouping, Lane-detail depth, Cross-surface consistency + a11y

---

## Surface + poll-survival (DRILL-01/03)

### Where the detail renders / how it survives the 5s poll

| Option | Description | Selected |
|--------|-------------|----------|
| Sibling pane + URL param | Detail in a right/adjacent pane that is a SEPARATE swap target OUTSIDE the polled list; role=button trigger + hx-get + hx-push-url so `?lane=`/`?agent=` re-renders highlight and reload restores. Both DRILL-03 escape hatches. | ✓ |
| Modal / dialog | Centered modal (cmdk_modal precedent), focus-trap, Esc; outside polled region but overlays/hides the list. | |
| Dedicated full page | Navigate to standalone page; survives poll trivially but loses side-by-side context, full nav round-trip. | |

**User's choice:** Sibling pane + URL param.
**Notes:** Chose the pattern using BOTH DRILL-03 mechanisms — pane outside the polled region AND URL param for highlight/reload restore.

### Does the open detail live-refresh?

| Option | Description | Selected |
|--------|-------------|----------|
| Live-refresh (own tick) | Open pane self-polls every 5s (own hx-trigger), bounded per-lane/agent; stops on dismiss. | ✓ |
| Snapshot on click | Point-in-time snapshot, refresh only on reopen/explicit refresh. | |

**User's choice:** Live-refresh (own tick).
**Notes:** Keeps in-flight/queue/liveness current while watching; consistent with console cadence.

---

## Agent-activity grouping (DRILL-02)

### Form of the stage-status grouping

| Option | Description | Selected |
|--------|-------------|----------|
| 6-stage matrix counts | Per-agent counts at each stage × bucket via aggregate GROUP BY on stage_status_case, filtered to agent_id. | ✓ |
| Expandable file lists | Each bucket expands to a paginated list of owned files. | |
| 4-bucket summary only | Single not_started/in_flight/done/failed roll-up across all stages. | |

**User's choice:** 6-stage matrix counts.
**Notes:** Must be a SQL aggregate (agent may own a large share of 200K), not row materialization. Expandable lists deferred as later enhancement.

### Composition of the agent pane

| Option | Description | Selected |
|--------|-------------|----------|
| Stacked sections | Liveness header on top → stage grouping → per-lane queue depths → recent scan batches. | ✓ |
| Tabbed sections | Liveness always visible; Files/Queues/Scans as Alpine tabs. | |
| You decide | Leave composition to planning. | |

**User's choice:** Stacked sections.
**Notes:** Liveness always on top; stage grouping is primary content.

---

## Lane-detail depth (DRILL-01)

### Same fields for all lane kinds, or adaptive?

| Option | Description | Selected |
|--------|-------------|----------|
| Kind-adaptive fields | Common fields for all; quota-wait/Inadmissible for kueue only; short/long-set caption for local/compute. | ✓ |
| Uniform field set | Identical grid; kueue-only fields as n/a/0 for others. | |

**User's choice:** Kind-adaptive fields.
**Notes:** No fabricated zero rows; preserve the card layer's kind disambiguation.

### What bounds "recent completions"?

| Option | Description | Selected |
|--------|-------------|----------|
| Last N (e.g. 20) | Fixed small count, newest-first; bounded regardless of throughput. | ✓ |
| Time window (e.g. 15m) | All completions in trailing window; unbounded under a burst. | |
| You decide | Leave N vs window to planning. | |

**User's choice:** Last N (newest-first). Exact N left to planning.

---

## Cross-surface consistency + a11y (DRILL-03)

### How much do lane vs agent drill-ins share?

| Option | Description | Selected |
|--------|-------------|----------|
| Shared pane shell | One reusable `_detail_pane.html` (container, ?param wiring, keyboard model); lane/agent bodies as content. | ✓ |
| Page-native each | Built natively to each host page; poll-survival + keyboard contract implemented twice. | |

**User's choice:** Shared pane shell.
**Notes:** One place to keep the DRILL-03 poll-survival + a11y contract correct across both hosts.

### Keyboard interaction + dismiss model

| Option | Description | Selected |
|--------|-------------|----------|
| role=button + Esc dismiss | role=button/tabindex/Enter/Space/focus ring; Close control + Esc dismiss that clears ?param and restores focus to originating card/row. | ✓ |
| role=button, close-only | Meets literal DRILL-03 checklist; no Esc, no focus-return. | |
| You decide | Leave keystrokes/focus-restoration to planning. | |

**User's choice:** role=button + Esc dismiss.
**Notes:** Full WCAG dismiss + focus-return loop, exceeds DRILL-03's literal minimum.

---

## Claude's Discretion

- Exact N for lane recent completions; precise ordering/labels within the agent stacked sections.
- Physical placement of the sibling pane per host page and its responsive collapse.
- Whether per-lane queue depths in the agent pane reuse an existing lane snapshot read or a scoped variant.
- Plan/PR decomposition (shared shell + a11y/poll-survival; lane endpoint+body; agent endpoint+aggregate+pane).

## Deferred Ideas

- Expandable per-bucket file lists in the agent pane — later enhancement.
- Legacy `legacy-application-server` sentinel retire → Phase 89.
- `files.state` column drop + `FileState` enum deletion + remaining `.state=` writers → Phase 90.
