# Phase 17: Live Set Matching & Tracklist Review - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-01
**Phase:** 17-live-set-matching-tracklist-review
**Areas discussed:** Scan workflow, Match result model, Review UI & actions, Track editing

---

## Scan Workflow

### Trigger Mechanism

| Option | Description | Selected |
|--------|-------------|----------|
| Manual per-file | User clicks 'Scan for Tracks' on a file card. | |
| Auto after fingerprinting | Auto-scan live set files after fingerprinting. | |
| Batch scan page | Dedicated page where user selects files to scan in batch. | ✓ |

**User's choice:** Batch scan page
**Notes:** None

### Segmentation

| Option | Description | Selected |
|--------|-------------|----------|
| Fingerprint service handles it | /query segments internally, returns matches with timestamps. | ✓ |
| Main app pre-segments | Main app splits audio, queries each segment. | |
| You decide | Let Claude choose based on engine capabilities. | |

**User's choice:** Fingerprint service handles it
**Notes:** None

### Page Location

| Option | Description | Selected |
|--------|-------------|----------|
| Tab on Tracklists page | Add 'Scan' tab alongside existing filter tabs. | ✓ |
| Separate Scans page | New nav entry for scanning. | |
| Section on Pipeline page | Add to Pipeline dashboard. | |

**User's choice:** Tab on Tracklists page
**Notes:** None

### Result Storage

| Option | Description | Selected |
|--------|-------------|----------|
| Reuse Tracklist model | Create Tracklist + TracklistVersion + TracklistTracks with source='fingerprint'. | ✓ |
| New ProposedTracklist model | Separate model for scan-generated tracklists. | |
| Merge into existing tracklist | Merge fingerprint matches into existing scraped tracklist. | |

**User's choice:** Reuse Tracklist model
**Notes:** None

---

## Match Result Model

### Confidence Granularity

| Option | Description | Selected |
|--------|-------------|----------|
| Per-track confidence | Each TracklistTrack gets a confidence score. | ✓ |
| Tracklist-level only | Overall confidence on the tracklist. | |
| Both levels | Per-track plus aggregate tracklist. | |

**User's choice:** Per-track confidence
**Notes:** Essential for FPRINT-03 requirement.

### Source Tracking

| Option | Description | Selected |
|--------|-------------|----------|
| Source field on Tracklist | String column: '1001tracklists' or 'fingerprint'. | ✓ |
| Source on TracklistVersion only | Track source per version. | |
| Separate flag | Boolean 'is_fingerprint_generated'. | |

**User's choice:** Source field on Tracklist
**Notes:** None

### Track Schema Extension

| Option | Description | Selected |
|--------|-------------|----------|
| Add confidence column | Nullable Float on TracklistTrack. NULL for scraped, 0-100 for fingerprint. | ✓ |
| Separate match metadata table | New FingerprintMatch table. | |
| JSON field | JSONB 'match_metadata' column. | |

**User's choice:** Add confidence column
**Notes:** None

---

## Review UI & Actions

### Display

| Option | Description | Selected |
|--------|-------------|----------|
| Source badge on cards | Each card shows source badge. Filter tabs add source filter. | ✓ |
| Separate source filter tab | Add 'Fingerprint' and '1001Tracklists' filter tabs. | |
| Combined with color coding | Source indicated by card color/accent. | |

**User's choice:** Source badge on cards
**Notes:** None

### Status Flow

| Option | Description | Selected |
|--------|-------------|----------|
| Proposed → Approved/Rejected | Start as 'proposed'. User approves/rejects whole tracklist. | ✓ |
| Auto-approved, editable | Auto-accepted but editable. | |
| Per-track approval | Each track individually approved/rejected. | |

**User's choice:** Proposed → Approved/Rejected
**Notes:** Consistent with proposal pattern.

### Confidence Display

| Option | Description | Selected |
|--------|-------------|----------|
| Color-coded badges | Green (90%+), yellow (70-89%), red (<70%). | ✓ |
| Inline percentage only | Just the number. | |
| Sortable by confidence | Tracks sorted plus color badges. | |

**User's choice:** Color-coded badges
**Notes:** Reuse Phase 15 UI-SPEC confidence color tiers.

### Bulk Action

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, threshold-based | Remove all tracks below threshold (e.g., <50%). | ✓ |
| No bulk actions | Review each track individually. | |
| Accept All / Reject All only | Whole-tracklist bulk actions only. | |

**User's choice:** Yes, threshold-based
**Notes:** None

---

## Track Editing

### Editable Fields (multi-select)

| Option | Description | Selected |
|--------|-------------|----------|
| Artist name | Correct matched artist. | ✓ |
| Track title | Correct matched title. | ✓ |
| Timestamp | Adjust start/end time. | ✓ |
| Delete track | Remove false positive. | ✓ |

**User's choice:** All four fields
**Notes:** None

### Edit UX

| Option | Description | Selected |
|--------|-------------|----------|
| Inline editing | Click field to edit, save on blur/enter. HTMX. | ✓ |
| Edit modal | Click 'Edit' button for modal. | |
| Edit page | Dedicated page per track. | |

**User's choice:** Inline editing
**Notes:** Consistent with minimal-click philosophy.

---

## Claude's Discretion

- arq task structure for scan job
- Batch scan file selection UI
- Confidence threshold default for bulk reject
- Status field implementation
- Inline edit HTMX partial pattern
- Alembic migration details

## Deferred Ideas

None — discussion stayed within phase scope.
