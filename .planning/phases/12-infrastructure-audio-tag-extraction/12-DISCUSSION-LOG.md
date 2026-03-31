# Phase 12: Infrastructure & Audio Tag Extraction - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-30
**Phase:** 12-infrastructure-audio-tag-extraction
**Areas discussed:** Pipeline flow & ordering, Backfill strategy, Tag-to-LLM integration, Extraction trigger & scope

---

## Pipeline Flow & Ordering

| Option | Description | Selected |
|--------|-------------|----------|
| Before analysis | DISCOVERED -> METADATA_EXTRACTED -> ANALYZED. Tags first, then essentia. | |
| After analysis | DISCOVERED -> ANALYZED -> METADATA_EXTRACTED. Keeps v1 flow intact. | |
| Independent / parallel | Both run independently from DISCOVERED, converge before proposals. | |

**User's initial response:** "would the tags be used in analysis? i didn't think they would be..."

**Clarification:** Tags are not used by essentia audio analysis (which works on raw waveform). Tag extraction and audio analysis are fully independent operations.

| Option | Description | Selected |
|--------|-------------|----------|
| Sequential: tags first | DISCOVERED -> METADATA_EXTRACTED -> ANALYZED. Simple linear pipeline. | |
| Parallel from DISCOVERED | Both run independently, converge at proposal gate. Faster throughput. | ✓ |
| You decide | Claude picks best fit. | |

**User's choice:** Parallel from DISCOVERED

| Option | Description | Selected |
|--------|-------------|----------|
| Dual state check | Query for files where BOTH FileMetadata and AnalysisResult exist. No new state. | ✓ |
| Composite state on FileRecord | Add READY_FOR_PROPOSAL state set when both complete. | |
| You decide | Claude picks cleanest approach. | |

**User's choice:** Dual state check (recommended)

---

## Backfill Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Queue all files | Enqueue tag extraction for all ~200K files. Simple, complete, idempotent. | ✓ |
| Only files without metadata | Skip files that already have FileMetadata rows. | |
| New files only, backfill later | Only new files; separate backfill job on-demand. | |

**User's choice:** Queue all files

| Option | Description | Selected |
|--------|-------------|----------|
| No, tags for new proposals only | Existing proposals stay as-is. Future proposals use tag data. | ✓ |
| Yes, regenerate all proposals | Re-run proposal generation for all files with tag context. | |
| Optional re-propose trigger | Selective re-propose via API endpoint. | |

**User's choice:** No, tags for new proposals only

---

## Tag-to-LLM Integration

| Option | Description | Selected |
|--------|-------------|----------|
| Flat fields in context | Add tag fields as top-level fields alongside analysis data. | |
| Nested under 'tags' key | Group tag data under a 'tags' object. Visually distinct from analysis. | ✓ |
| You decide | Claude picks best structure. | |

**User's choice:** Nested under 'tags' key

| Option | Description | Selected |
|--------|-------------|----------|
| Curated fields only | Send artist, title, album, year, genre, track_number, duration, bitrate. | |
| Include raw tags | Full raw tag dump alongside curated fields. Richer context. | ✓ |
| Curated + selected raw extras | Curated fields plus whitelisted raw tags. | |

**User's choice:** Include raw tags

| Option | Description | Selected |
|--------|-------------|----------|
| Update prompt with guidance | Add section to naming.md about tag data priority. | |
| Just include in context | Add tag data to context dict without prompt changes. | ✓ |
| You decide | Claude picks based on current prompt structure. | |

**User's choice:** Just include in context
**Notes:** "Tags may sometimes contain nonsense (like origination of the file or extra data that is irrelevant). So letting the LLM decide is better."

---

## Extraction Trigger & Scope

| Option | Description | Selected |
|--------|-------------|----------|
| Auto after scan | Tag extraction enqueued automatically during scan. | |
| Separate API endpoint | Manual trigger from pipeline dashboard. | |
| Both auto + manual | Auto-enqueue during scan plus manual endpoint for backfill. | ✓ |

**User's choice:** Both auto + manual

| Option | Description | Selected |
|--------|-------------|----------|
| Empty metadata row | Create FileMetadata with all nulls. File transitions to METADATA_EXTRACTED. | ✓ |
| Skip, no metadata row | Don't create row if no tags. File stays in DISCOVERED for tag purposes. | |
| You decide | Claude picks cleanest approach for convergence gate. | |

**User's choice:** Empty metadata row

| Option | Description | Selected |
|--------|-------------|----------|
| Music files only | Only extract from music files (mp3, m4a, ogg, opus, flac, wav). | |
| Music + video files | Also extract from video files (mp4, mkv, avi). Concert streams may have tags. | ✓ |
| You decide | Claude determines based on extension map. | |

**User's choice:** Music + video files

---

## Claude's Discretion

- Shared async engine pool implementation details
- FileMetadata model column additions (track_number, duration, bitrate)
- Alembic migration structure
- mutagen integration details (format detection, error handling)
- Manual extraction API endpoint design
- arq task function structure
- Pipeline dashboard updates

## Deferred Ideas

None — discussion stayed within phase scope.
