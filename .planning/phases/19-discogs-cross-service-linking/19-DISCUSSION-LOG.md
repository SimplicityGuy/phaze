# Phase 19: Discogs Cross-Service Linking - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-02
**Phase:** 19-discogs-cross-service-linking
**Areas discussed:** Match triggering & scope, Candidate review UX, Cross-system query design, Bulk-link behavior

---

## Match Triggering & Scope

### How should Discogs matching be triggered?

| Option | Description | Selected |
|--------|-------------|----------|
| Per-tracklist button | A 'Match to Discogs' button on the tracklist detail page. User picks which tracklist to match. Fits the human-in-the-loop pattern. | ✓ |
| Batch across all unlinked | A pipeline-style action that queues matching for all tracklists with unlinked tracks. Runs via SAQ worker. | |
| Both | Per-tracklist button for on-demand + a batch endpoint for initial data processing. | |

**User's choice:** Per-tracklist button
**Notes:** Matches existing fingerprint scan pattern — one action per tracklist.

### Which tracks should be eligible for Discogs matching?

| Option | Description | Selected |
|--------|-------------|----------|
| Artist + title required | Only match tracks that have both artist and title filled in. | ✓ |
| Title-only fallback | Also attempt matching tracks with only a title (no artist). Broader but noisier. | |
| All tracks attempted | Try every track regardless of data completeness. Maximum coverage but many false positives. | |

**User's choice:** Artist + title required
**Notes:** None — clean data requirement avoids noise.

### Should matching run synchronously or via SAQ background task?

| Option | Description | Selected |
|--------|-------------|----------|
| SAQ background task | Queue a SAQ job per tracklist. Keeps UI responsive. Matches fingerprint scan pattern. | ✓ |
| Synchronous inline | Match all tracks in a single request-response cycle. Simpler but blocks UI. | |
| You decide | Claude picks based on existing patterns. | |

**User's choice:** SAQ background task
**Notes:** Consistent with fingerprint scan flow.

---

## Candidate Review UX

### Where should Discogs match candidates appear?

| Option | Description | Selected |
|--------|-------------|----------|
| Inline on tracklist page | Expand each track row to show candidate Discogs matches below it. | ✓ |
| Dedicated linking page | Separate '/discogs-links' page showing all pending candidates. | |
| Both views | Inline per-track + dedicated overview page. | |

**User's choice:** Inline on tracklist page
**Notes:** Keeps context — user sees track and candidates together.

### What info should each Discogs candidate row show?

| Option | Description | Selected |
|--------|-------------|----------|
| Artist, title, label, year, confidence | Core Discogs release info plus fuzzy match confidence score. | ✓ |
| Full release details | Above plus genre, format, catalog number, tracklist count. | |
| You decide | Claude picks based on what discogsography returns. | |

**User's choice:** Artist, title, label, year, confidence
**Notes:** Compact enough for inline display.

### How many candidate matches should be stored per track?

| Option | Description | Selected |
|--------|-------------|----------|
| Top 3 | Store the 3 highest-confidence matches per track. | ✓ |
| Top 5 | More options for ambiguous tracks. | |
| All above threshold | Store every match above a minimum confidence score. | |

**User's choice:** Top 3
**Notes:** Short list, can re-match for more.

### What actions on each candidate?

| Option | Description | Selected |
|--------|-------------|----------|
| Accept / Dismiss | Accept links track to release, auto-dismisses others. Dismiss removes candidate. | ✓ |
| Accept / Reject / Ignore | Three states with explicit rejection tracking. | |
| You decide | Claude picks based on existing approve/reject patterns. | |

**User's choice:** Accept / Dismiss
**Notes:** Mirrors existing approve/reject pattern. One accepted link per track.

---

## Cross-System Query Design

### How should 'find all sets containing track X' be accessed?

| Option | Description | Selected |
|--------|-------------|----------|
| Extend existing search page | Add 'Discogs releases' entity type to Phase 18 search. | ✓ |
| Dedicated cross-reference page | New page for cross-system queries. | |
| Click-through from track row | Contextual 'Find in other sets' per track. | |

**User's choice:** Extend existing search page
**Notes:** Reuses established search patterns from Phase 18.

### Should results query discogsography live or only stored links?

| Option | Description | Selected |
|--------|-------------|----------|
| Stored links only | Query DiscogsLink table. Fast, no external dependency during search. | ✓ |
| Live query to discogsography | Hit discogsography /api/search in real-time. Shows unlinked data too. | |
| Stored + live fallback | Stored first, offer 'Search Discogs' button for live results. | |

**User's choice:** Stored links only
**Notes:** Consistent with human-in-the-loop model — only shows accepted links.

### How should Discogs results appear in the search page?

| Option | Description | Selected |
|--------|-------------|----------|
| Purple pill for Discogs releases | New pill color in unified results table. Same dense row format. | ✓ |
| Nested under track results | Sub-rows under matching track/tracklist results. | |
| You decide | Claude picks based on existing search UI patterns. | |

**User's choice:** Purple pill for Discogs releases
**Notes:** Extends blue (files), green (tracklists) pattern.

---

## Bulk-Link Behavior

### What should 'bulk-link all tracks' do?

| Option | Description | Selected |
|--------|-------------|----------|
| Accept top match per track | One-click accepts highest-confidence candidate for every track. | ✓ |
| Accept above threshold | Accept candidates above a user-set confidence threshold. | |
| Confirm-all review | Show summary before committing the batch. | |

**User's choice:** Accept top match per track
**Notes:** Quick one-click action for when matches look good.

### Should bulk-link require matches to exist first?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, match first | Two-step: match -> review (optional) -> bulk-link. | ✓ |
| Match + link in one action | Single action triggers matching AND accepts top results. | |
| You decide | Claude picks based on human-in-the-loop constraint. | |

**User's choice:** Yes, match first
**Notes:** Clear separation: match is discovery, bulk-link is approval.

---

## Claude's Discretion

- DiscogsLink model schema details (columns, indexes, relationships)
- Fuzzy matching strategy (rapidfuzz algorithm choice, scoring normalization)
- discogsography API adapter implementation (retry logic, timeout handling)
- SAQ task structure (job naming, progress reporting)
- HTMX partial structure for inline candidate display
- Search integration implementation details (FTS config for Discogs data)

## Deferred Ideas

None — discussion stayed within phase scope.
