# Phase 17: Live Set Matching & Tracklist Review - Context

**Gathered:** 2026-04-01
**Status:** Ready for planning

<domain>
## Phase Boundary

Scan live set recordings against the fingerprint database to identify tracks with timestamps and confidence scores, display proposed tracklists in the admin UI with per-track review (approve/reject/edit individual track identifications), and integrate with the existing Tracklist model from Phase 15. Covers FPRINT-03 and FPRINT-04.

</domain>

<decisions>
## Implementation Decisions

### Scan Workflow
- **D-01:** Batch scan page — dedicated "Scan" tab on the existing Tracklists page alongside Matched/Unmatched/All. Users select files to scan in batch. Keeps tracklist management consolidated.
- **D-02:** Fingerprint service handles segmentation internally. The `/query` endpoint segments the audio and returns matches with timestamps. Main app receives structured results — no audio processing in the main app.
- **D-03:** Scanning is async via arq task. User triggers scan, task runs in background, results appear when done.

### Match Result Model
- **D-04:** Reuse the existing Tracklist model from Phase 15. Fingerprint scan creates a Tracklist + TracklistVersion + TracklistTracks, marked with `source='fingerprint'` instead of `source='1001tracklists'`. Same data model, same review flow.
- **D-05:** Per-track confidence scores. Nullable Float `confidence` column on TracklistTrack. NULL for scraped tracks (100% by definition), 0-100 for fingerprint matches.
- **D-06:** Source field on Tracklist model — string column with values like `'1001tracklists'` or `'fingerprint'`. Filter tabs and cards can distinguish source. Alembic migration to add column.

### Review UI & Actions
- **D-07:** Source badge on tracklist cards — shows '1001Tracklists' or 'Fingerprint' badge. Existing filter tabs work with source filter. Cards expand to show per-track confidence for fingerprint-sourced tracklists.
- **D-08:** Proposed → Approved/Rejected status flow. Fingerprint tracklists start as 'proposed'. User reviews and approves/rejects the whole tracklist. Individual tracks can be edited before approval. Consistent with proposal pattern.
- **D-09:** Color-coded confidence badges per track — reuse Phase 15 UI-SPEC confidence color tiers: green (90%+), yellow (70-89%), red (<70%). Badge next to each track in expanded view.
- **D-10:** "Reject All Low Confidence" bulk action — button to remove all tracks below a configurable confidence threshold (e.g., <50%). Quick cleanup for noisy scan results.

### Track Editing
- **D-11:** Editable fields: artist name, track title, timestamp, and delete track. Covers correcting fingerprint match errors and removing false positives.
- **D-12:** Inline editing via HTMX. Click a track field to edit inline. Save on blur or enter. Fast for corrections, consistent with minimal-click philosophy.

### Claude's Discretion
- arq task structure for scan job (follow Phase 16 `fingerprint_file` pattern)
- Tracklist card expansion for fingerprint-sourced tracklists (HTMX partial structure)
- Batch scan file selection UI (checkboxes, select-all, filter by file type)
- Confidence threshold default for bulk reject (50% suggested, tunable)
- Status field implementation on Tracklist model (proposed/approved/rejected enum or string)
- Inline edit HTMX partial pattern (edit mode toggle, save endpoint)
- Alembic migration for new columns (source, confidence, status)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — FPRINT-03, FPRINT-04 acceptance criteria

### Existing Code (MUST READ)
- `src/phaze/models/tracklist.py` — Tracklist, TracklistVersion, TracklistTrack models (add source, confidence, status columns)
- `src/phaze/services/fingerprint.py` — FingerprintOrchestrator with `query()` method via Protocol adapters (returns matches)
- `src/phaze/routers/tracklists.py` — Existing tracklists router with filter tabs, card layout, HTMX partials
- `src/phaze/templates/tracklists/` — All existing templates (list, cards, track detail, filter tabs, toast)
- `src/phaze/tasks/fingerprint.py` — Existing `fingerprint_file` task pattern (reference for scan task)
- `src/phaze/tasks/worker.py` — WorkerSettings, task registration

### UI Design References
- `.planning/phases/15-1001tracklists-integration/15-UI-SPEC.md` — Confidence color tiers, card layout, spacing, typography (reuse for consistency)

### Prior Phase Context
- `.planning/phases/15-1001tracklists-integration/15-CONTEXT.md` — Tracklist model decisions (D-01 through D-23), card layout, filter tabs, actions
- `.planning/phases/16-fingerprint-service-batch-ingestion/16-CONTEXT.md` — Fingerprint service architecture, Protocol adapters, scoring (D-01 through D-18)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- Tracklist model with TracklistVersion and TracklistTrack — extend with source/confidence/status columns
- Tracklists router with filter tabs, HTMX card expand/collapse — add Scan tab and fingerprint-specific views
- FingerprintOrchestrator.query() — queries both engines, returns weighted matches with timestamps
- Confidence color tiers from Phase 15 UI-SPEC (green/yellow/red)
- arq task patterns from `fingerprint_file` and `search_tracklist`

### Established Patterns
- HTMX for dynamic updates, Alpine.js for client-side state
- Jinja2 partials in `templates/{feature}/partials/`
- Card-per-tracklist layout with inline expand
- 10-second undo toast pattern
- arq tasks with retry/backoff

### Integration Points
- New `source` and `status` columns on Tracklist model (Alembic migration)
- New `confidence` column on TracklistTrack model
- New arq task `scan_live_set` in `tasks/`
- New scan-related endpoints on tracklists router
- New templates for scan tab, fingerprint track detail with inline editing
- Worker registration for scan task

</code_context>

<specifics>
## Specific Ideas

- Batch scan UI on the Tracklists page as a new tab — not a separate page
- Fingerprint-generated tracklists share the exact same Tracklist model and card UI as 1001tracklists ones, just with `source='fingerprint'` and per-track confidence
- Inline editing for quick corrections — no modal or separate page
- "Reject All Low Confidence" button for efficient cleanup of noisy scan results

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 17-live-set-matching-tracklist-review*
*Context gathered: 2026-04-01*
