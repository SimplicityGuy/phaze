# Phase 59: Identify workspaces - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-30
**Phase:** 59-identify-workspaces
**Areas discussed:** Track-ID fingerprint signal, Track-ID workspace layout, Tracklist 3-step rendering, Per-set match progress

---

## Track-ID fingerprint signal

| Option | Description | Selected |
|--------|-------------|----------|
| Per-engine status badges | Two badges per row: audfprint + Panako [done/failed/pending] from FingerprintResult; no invented numbers | ✓ |
| Derived combined state | Collapse both engines into one 'fingerprinted' state per file | |
| Badges + error surfacing | Per-engine badges AND surface error_message on failed rows | |

**User's choice:** Per-engine status badges
**Notes:** Verified at discuss time that `FingerprintResult` stores only `engine` + `status` + `error_message` (no numeric score). Drove the D-02 reconciliation: IDENT-01's "match/score" wording resolves to per-engine match state + tracklist confidence.

---

## Track-ID workspace layout

| Option | Description | Selected |
|--------|-------------|----------|
| One combined per-file table | Single table, all in-stage files: file · audfprint · Panako · tracklist state · confidence (mirrors Phase 58 D-03) | ✓ |
| Two sub-sections | Separate 'Fingerprint identity' and 'Tracklist identity' sub-tables | |

**User's choice:** One combined per-file table

| Option | Description | Selected |
|--------|-------------|----------|
| Linked tracklist (fallback best) | Show linked/auto-linked match_confidence; fallback to highest candidate | ✓ |
| Best candidate always | Always show highest match_confidence regardless of link state | |
| You decide | Let planner pick from existing list_tracklists query | |

**User's choice:** Linked tracklist (fallback best)
**Notes:** Reflects the actual identity-decision state rather than implying an unlinked match.

---

## Tracklist 3-step rendering

| Option | Description | Selected |
|--------|-------------|----------|
| Three step cards | Sequential Search · Scrape · Match cards (Analyze lane-card pattern) with per-step counts | ✓ |
| Horizontal stepper | Single connected stepper bar with counts on each node (new component) | |

**User's choice:** Three step cards

| Option | Description | Selected |
|--------|-------------|----------|
| Per-step ALL buttons | SEARCH ALL / SCRAPE ALL / MATCH ALL wired to existing per-step endpoints verbatim | ✓ |
| Single 'run chain' button | One button kicking off the whole flow (needs backend orchestration) | |

**User's choice:** Per-step ALL buttons
**Notes:** "Triggerable from one surface" interpreted as all three triggers co-located on the one Tracklist workspace — no chain-orchestration endpoint (would break no-backend-change rule).

---

## Per-set match progress

| Option | Description | Selected |
|--------|-------------|----------|
| Track-level coverage | N/M tracks confident within the set's linked tracklist (TracklistTrack.confidence) | ✓ |
| Chain-step position | Where the set is in Search→Scrape→Match | |
| Both | Chain position AND track coverage per set | |

**User's choice:** Track-level coverage

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — per-set table below cards | Step cards (aggregate) on top, table of sets/files below with per-set progress | ✓ |
| Cards only | Just the 3 aggregate step cards, no per-set table | |

**User's choice:** Yes — per-set table below cards
**Notes:** Parallels Phase 58 Analyze (lane cards + file table); gives IDENT-02's "per-set match progress" a concrete home. Chain-step position not duplicated per-row since the step cards convey it in aggregate.

---

## Claude's Discretion

- Exact OOB id additions for the two new workspace fragments (must ride the single `/pipeline/stats` poll + `oob_counts` gate).
- Reuse-and-restyle of existing `tracklists/partials/` templates vs. fresh fragments.
- Empty-state + trigger-response wiring detail (to be locked by `/gsd:ui-phase 59`).
- Optional surfacing of failed-engine `error_message` on Track-ID badges.

## Deferred Ideas

- AcoustID + MusicBrainz identity backend → IDENT-03 (future milestone); the reason a fingerprint score can't be shown today.
- Single "run chain" trigger for Search→Scrape→Match (needs backend orchestration).
- Row-click → rich per-file record/pane → Phase 61 (RECORD-01); rows are inert-but-present here.
- Numeric fingerprint match scoring (depends on the deferred IDENT-03 backend).
