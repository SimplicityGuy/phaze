# Phase 21: CUE Sheet Generation - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md -- this log preserves the alternatives considered.

**Date:** 2026-04-03
**Phase:** 21-cue-sheet-generation
**Areas discussed:** Timestamp resolution, CUE generation trigger, Discogs REM enrichment, Output & delivery

---

## Timestamp Resolution

### Q1: Fingerprint timestamp format

| Option | Description | Selected |
|--------|-------------|----------|
| MM:SS | Minutes and seconds, like 1001tracklists uses | |
| Seconds (decimal) | Decimal seconds from fingerprint engine offset | |
| HH:MM:SS | Hours, minutes, seconds format | |
| You decide | Claude inspects fingerprint service response | |

**User's choice:** User redirected to CUE spec research first. Provided three CUE spec URLs (Wikipedia, fileformat.com x2) to determine supported timestamp formats. Research confirmed CUE uses MM:SS:FF at 75fps.
**Notes:** CUE spec research became a prerequisite before answering format questions. The spec-driven approach means whatever format fingerprint timestamps are stored in, they get converted to MM:SS:FF.

### Q2: Tracks missing timestamps

| Option | Description | Selected |
|--------|-------------|----------|
| Skip trackless entries | Omit tracks without timestamps from CUE | :heavy_check_mark: |
| Use equal spacing | Divide duration evenly among timestampless tracks | |
| Position 00:00:00 | Place all at start | |
| Block generation | Don't allow CUE if any track missing timestamp | |

**User's choice:** Skip trackless entries
**Notes:** None

### Q3: Timestamp source priority

| Option | Description | Selected |
|--------|-------------|----------|
| Fingerprint always wins (Recommended) | Measured from actual audio, more accurate | :heavy_check_mark: |
| User picks source per tracklist | Toggle on CUE generation UI | |
| You decide | Claude picks best approach | |

**User's choice:** Fingerprint always wins (Recommended)
**Notes:** None

---

## CUE Generation Trigger

### Q1: UI location for Generate CUE

| Option | Description | Selected |
|--------|-------------|----------|
| Button on tracklist detail (Recommended) | Natural home, per-tracklist action | |
| Dedicated CUE page | New /cue nav tab for batch operations | |
| Both places | Tracklist button + CUE management page | :heavy_check_mark: |

**User's choice:** Both places
**Notes:** None

### Q2: Tracklist eligibility

| Option | Description | Selected |
|--------|-------------|----------|
| Approved tracklists only (Recommended) | Only status='approved', consistent with human-in-the-loop | :heavy_check_mark: |
| Any tracklist with timestamps | Regardless of approval status | |
| Approved + linked to EXECUTED file | Strictest, requires destination copy | |

**User's choice:** Approved tracklists only (Recommended)
**Notes:** None

### Q3: Sync vs async generation

| Option | Description | Selected |
|--------|-------------|----------|
| Synchronous (Recommended) | Pure string formatting, instant feedback | :heavy_check_mark: |
| SAQ background task | Queue for batch generation | |
| You decide | Claude picks based on patterns | |

**User's choice:** Synchronous (Recommended)
**Notes:** None

---

## Discogs REM Enrichment

### Q1: REM scope (per-track vs disc-level)

| Option | Description | Selected |
|--------|-------------|----------|
| Per-track REMs (Recommended) | Each TRACK gets own REM from its DiscogsLink | :heavy_check_mark: |
| Disc-level REMs only | Single set from most common values | |
| Both levels | Disc-level for set + per-track for Discogs data | |

**User's choice:** Per-track REMs (Recommended)
**Notes:** None

### Q2: Tracks without DiscogsLink

| Option | Description | Selected |
|--------|-------------|----------|
| Omit REMs for unlinked tracks | Clean omission, no guessing | :heavy_check_mark: |
| REM COMMENT placeholder | Explicit "No Discogs link" | |
| You decide | Claude picks cleanest approach | |

**User's choice:** Omit REMs for unlinked tracks
**Notes:** None

---

## Output & Delivery

### Q1: Storage location

| Option | Description | Selected |
|--------|-------------|----------|
| Database only (download on demand) | Store as text in DB, download via button | |
| Filesystem next to destination file | Write .cue alongside destination audio file | :heavy_check_mark: |
| Both (Recommended) | DB + filesystem | |

**User's choice:** Filesystem next to destination file
**Notes:** None

### Q2: Re-generation behavior

| Option | Description | Selected |
|--------|-------------|----------|
| Overwrite silently (Recommended) | Replace existing, CUEs are generated artifacts | |
| Version suffix | e.g., file.v2.cue -- preserves history | :heavy_check_mark: |
| Prompt before overwrite | Confirmation dialog | |

**User's choice:** Version suffix
**Notes:** None

### Q3: FILE command path reference

| Option | Description | Selected |
|--------|-------------|----------|
| Filename only (Recommended) | Just the audio filename, CUE and audio co-located | :heavy_check_mark: |
| Relative path | More robust for different directories | |
| You decide | Claude picks per CUE conventions | |

**User's choice:** Filename only (Recommended)
**Notes:** None

---

## Claude's Discretion

- CUE generation service implementation (string building, frame conversion math)
- CUE management page layout and filtering options
- HTMX partial structure for inline CUE status on tracklist page
- Batch generation loop on CUE management page
- Audio file type mapping for FILE command
- Version number tracking strategy
- Nav tab ordering for /cue page

## Deferred Ideas

None -- discussion stayed within phase scope
