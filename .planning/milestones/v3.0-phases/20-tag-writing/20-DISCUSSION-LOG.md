# Phase 20: Tag Writing - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-03
**Phase:** 20-tag-writing
**Areas discussed:** Tag source & proposal logic, Review page UX, Write verification & error handling, Audit trail design

---

## Tag Source & Proposal Logic

### Where should corrected tags come from?

| Option | Description | Selected |
|--------|-------------|----------|
| FileMetadata + Discogs | Start from FileMetadata, enrich with DiscogsLink data. | |
| AI-generated proposals | Use litellm to generate corrected tags. | |
| Manual editing only | User types corrected values manually. | |
| FileMetadata + Filename + 1001tracklists | (User's choice via Other) | ✓ |

**User's choice:** FileMetadata + filename parsing + 1001tracklists data
**Notes:** Discogs data doesn't apply to file-level tags — DiscogsLinks are per-track within a tracklist, the file is the full live set. Discogs is for CUE sheets (Phase 21).

### Should Discogs data be included as a tag source?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, include Discogs | Pull genre, label, year from accepted DiscogsLinks. | |
| No, skip Discogs | Only FileMetadata + filename + 1001tracklists. | ✓ |

**User's choice:** No — Discogs is per-track, not per-file
**Notes:** One live set/concert maps to many DiscogsLinks. Can't use per-track data for file-level tags.

### How should proposed tags be computed?

| Option | Description | Selected |
|--------|-------------|----------|
| Priority cascade | Tracklist > FileMetadata > filename. Each field independent. | ✓ |
| User picks per field | Show all sources, let user choose per field. | |
| You decide | Claude picks. | |

**User's choice:** Priority cascade
**Notes:** Deterministic and auditable.

---

## Review Page UX

### How should the tag review page be structured?

| Option | Description | Selected |
|--------|-------------|----------|
| Dedicated tag review page | New '/tags' nav tab with file table and expand. | ✓ |
| Inline on file detail | Embedded on each file's detail view. | |
| Batch review table | One big table with all files and fields as columns. | |

**User's choice:** Dedicated tag review page
**Notes:** Similar to proposals page.

### How should proposed vs current tags be displayed?

| Option | Description | Selected |
|--------|-------------|----------|
| Two-column table | Field / Current / Proposed. Changed highlighted. | ✓ |
| Diff-style view | Only changed fields with red/green. | |
| You decide | Claude picks. | |

**User's choice:** Two-column table
**Notes:** Matches existing dense table aesthetic.

### Which tag fields should be shown?

| Option | Description | Selected |
|--------|-------------|----------|
| Core music fields | Artist, title, album, year, genre, track number. | ✓ |
| All extracted fields | Everything including duration, bitrate, raw_tags. | |
| You decide | Claude picks based on mutagen capabilities. | |

**User's choice:** Core music fields
**Notes:** Duration/bitrate are read-only properties.

### Should users be able to edit proposed tags before approving?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, inline editing | Proposed cells editable, HTMX inline edit pattern. | ✓ |
| No, approve as-is | Read-only proposed values. Reject and re-propose if wrong. | |
| You decide | Claude picks. | |

**User's choice:** Yes, inline editing
**Notes:** Same HTMX inline edit pattern as tracklist tracks.

---

## Write Verification & Error Handling

### How should verify-after-write work?

| Option | Description | Selected |
|--------|-------------|----------|
| Re-read and compare all fields | Re-open with mutagen, compare each field. | ✓ |
| Hash-based verification | Compare file hash before/after, then re-read. | |
| You decide | Claude picks. | |

**User's choice:** Re-read and compare all fields

### What happens on verification discrepancy?

| Option | Description | Selected |
|--------|-------------|----------|
| Flag and continue | Log discrepancy, mark as 'discrepancy' status. | ✓ |
| Rollback the write | Attempt to restore original tags. | |
| Block and require review | Stop and show discrepancy immediately. | |

**User's choice:** Flag and continue
**Notes:** Most discrepancies are cosmetic (encoding normalization).

### Synchronous or background?

| Option | Description | Selected |
|--------|-------------|----------|
| Synchronous per-file | Write inline on approve. Fast (~100ms). Immediate feedback. | ✓ |
| SAQ background task | Queue writes as background jobs. | |
| You decide | Claude picks. | |

**User's choice:** Synchronous per-file

---

## Audit Trail Design

### What granularity for TagWriteLog entries?

| Option | Description | Selected |
|--------|-------------|----------|
| Per-file snapshot | One entry per write with before/after JSONB. | ✓ |
| Per-field entries | Separate entry for each changed field. | |
| You decide | Claude picks. | |

**User's choice:** Per-file snapshot
**Notes:** Matches ExecutionLog pattern from Phase 8.

### Should the audit log track who/what triggered the write?

| Option | Description | Selected |
|--------|-------------|----------|
| Source field only | Record data source (tracklist, metadata, manual_edit). | ✓ |
| Full attribution | Track user ID, session, IP, data source. | |
| You decide | Claude picks. | |

**User's choice:** Source field only
**Notes:** Single-user app doesn't need user attribution.

---

## Claude's Discretion

- TagWriteLog model schema details
- Tag write service implementation (mutagen write API per format)
- Proposed tag computation service (cascade merge logic)
- HTMX partial structure for tag review page
- Filename parsing strategy
- Nav tab ordering

## Deferred Ideas

None — discussion stayed within phase scope.
