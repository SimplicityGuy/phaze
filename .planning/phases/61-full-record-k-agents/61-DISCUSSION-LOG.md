# Phase 61: Full record + ⌘K + Agents - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-01
**Phase:** 61-full-record-k-agents
**Areas discussed:** Record surface & liveness, ⌘K commands + keyboard, Empty-state directory input, Agents k8s presentation

---

## Record surface (RECORD-01)

| Option | Description | Selected |
|--------|-------------|----------|
| Wide slide-in only | Single wide slide-in over the shell; row-click + ⌘K open it; empty 350px aside unused | ✓ |
| Pane preview + slide-in | 350px aside shows a light preview; full record in a wider slide-in | |
| Fill the right pane | Record fills the existing 350px aside in place | |

**User's choice:** Wide slide-in only
**Notes:** Matches design §8 + prototype literally. The empty aside's removal rides Phase 62.

## Record liveness (RECORD-01)

| Option | Description | Selected |
|--------|-------------|----------|
| Snapshot on open | Point-in-time fragment; no self-update; re-open to refresh | |
| Counts/status ride the poll | Static body; a few OOB bits (lane badge/chips/approval count) update off the existing /pipeline/stats fanout | ✓ |

**User's choice:** Counts/status ride the poll
**Notes:** Mirrors Phase 60 counts-only OOB; never re-renders the operator's in-progress subtree. No new loop.

---

## ⌘K quick commands (RECORD-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Scan / trigger scan | Fires the existing scan trigger via enqueue_router | ✓ |
| Jump to a stage | Rail nav to any /s/<stage> workspace | ✓ |
| Jump to a review queue | Into a specific Phase 60 Review & Apply gate | ✓ |
| Open Agents | Jump to the Agents page | ✓ |

**User's choice:** All four commands
**Notes:** Search itself funnels through the existing search service + enqueue_router guards.

## ⌘K keyboard depth (RECORD-02, design §13 open Q)

| Option | Description | Selected |
|--------|-------------|----------|
| Full arrow-nav + grouping | Grouped (Files/Tracklists/Artists/Commands); ↑↓ across groups; Enter activates; Esc closes | ✓ |
| Search-first, minimal keys | Type-to-filter, Enter opens top result; commands mouse-driven | |

**User's choice:** Full arrow-nav + grouping
**Notes:** Resolves the §13 open question toward full command parity. x-trap for the focus-trap.

## ⌘K Artists facet source (RECORD-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Distinct-artist read query | Read-only SELECT DISTINCT over FileMetadata.artist / Tracklist.artist; Enter → files?artist=X | ✓ |
| No distinct group; artist-as-filter | No separate group; artist stays in the FTS concat + a filter affordance | |

**User's choice:** Distinct-artist read query
**Notes:** The one sanctioned additive backend touch — a read query, within the presentation-only rule. Search service has no artist result_type today.

---

## Empty-state directory input (RECORD-04)

| Option | Description | Selected |
|--------|-------------|----------|
| Free-text path field | Text input for an absolute path + agent selector | |
| Server-side directory browser | Clickable tree via a new filesystem-listing endpoint | |
| Agent roots only (no path input) | Reuse configured scan_roots + existing scan trigger; "Configure roots →" link | ✓ |

**User's choice:** Agent roots only (no path input)
**Notes:** Most presentation-only option — zero new input surface, no filesystem-listing attack surface on the archive host. Live progress rides the existing poll.

---

## Agents k8s presentation (RECORD-03)

| Option | Description | Selected |
|--------|-------------|----------|
| Separate 'Compute lanes' section | Heartbeating Agents section + a distinct ephemeral Compute/burst-lanes section for k8s | ✓ |
| One unified list | k8s as a row in the same list, status from CloudJob counts | |

**User's choice:** Separate 'Compute lanes' section
**Notes:** Visually honest about the ephemeral, Job-based nature — the KDEPLOY-04 intent (k8s isn't a persistent heartbeating agent).

## k8s liveness states (RECORD-03)

| Option | Description | Selected |
|--------|-------------|----------|
| Active / Idle (two states) | ACTIVE when ≥1 in-flight workload; IDLE otherwise; never DEAD | |
| Active / Idle / Waiting | Adds WAITING for submitted-but-inadmissible (Kueue quota-wait) vs running | ✓ |

**User's choice:** Active / Idle / Waiting
**Notes:** Reuses CloudJob.inadmissible + running distinction (Phase 58 quota-wait surfacing). Read query over CloudJob. Never DEAD.

## Claude's Discretion

- Exact record fragment route shape + OOB ids registered for the open record (must ride the single poll behind oob_counts).
- Whether Discogs-release results appear in ⌘K or the palette stays limited to Files/Tracklists/Artists + Commands.
- Empty-state placement (home/Analyze workspace vs dedicated fragment) + copy (locked by /gsd:ui-phase 61).
- Record "history" source: ExecutionLog + TagWriteLog directly vs /audit/ scoped to the file.
- Reuse vs restyle of legacy per-file partials into the record's sections (supersede-in-place; removal is CUT-02).

## Deferred Ideas

- Empty 350px right aside removal (shell.html:164) — Phase 62 (CUT-02).
- Full a11y depth for record + palette — Phase 62 (CUT-01).
- Narrow-width / responsive rail-collapse affecting slide-in + palette — Phase 62 (CUT-03).
- Free-text path field / server-side directory browser for empty state — rejected (attack surface).
- Per-artist entity pages / artist as a first-class result_type — beyond the D-05 filter facet; not requested.
