# Phase 17: Live Set Matching & Tracklist Review - Research

**Researched:** 2026-04-01
**Domain:** Audio fingerprint querying, tracklist model extension, HTMX inline editing, arq task patterns
**Confidence:** HIGH

## Summary

Phase 17 bridges the fingerprint service (Phase 16) to the tracklist UI (Phase 15). The core work is: (1) a new arq task that calls `FingerprintOrchestrator.combined_query()` on a live set file and converts matches into Tracklist/TracklistVersion/TracklistTrack rows, (2) three new columns on existing models (`source` on Tracklist, `confidence` on TracklistTrack, `status` on Tracklist), (3) a "Scan" tab on the tracklists page with batch file selection and async scanning, and (4) inline editing of fingerprint-sourced track rows via HTMX.

All infrastructure exists. The fingerprint service already has `combined_query()` returning `CombinedMatch` objects with `track_id`, `confidence`, and per-engine scores. The tracklists router already has filter tabs, card layout, expand/collapse, and HTMX partials. The main gaps are: `QueryMatch`/`CombinedMatch` lack timestamp fields (needed for live set segmentation results), inline editing is a new UI pattern not yet used in the project, and the `source`/`status`/`confidence` columns need an Alembic migration.

**Primary recommendation:** Extend the existing Tracklist model with `source` and `status` columns, extend TracklistTrack with `confidence`, create a `scan_live_set` arq task that calls `combined_query()` and persists results as tracklist rows, then build the scan tab and inline editing UI on top of the existing tracklist card/template structure.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- D-01: Batch scan page -- dedicated "Scan" tab on existing Tracklists page alongside Matched/Unmatched/All
- D-02: Fingerprint service handles segmentation internally. `/query` endpoint segments audio and returns matches with timestamps. No audio processing in main app.
- D-03: Scanning is async via arq task. User triggers scan, task runs in background, results appear when done.
- D-04: Reuse existing Tracklist model. Fingerprint scan creates Tracklist + TracklistVersion + TracklistTracks with `source='fingerprint'`.
- D-05: Per-track confidence scores. Nullable Float `confidence` column on TracklistTrack. NULL for scraped tracks, 0-100 for fingerprint matches.
- D-06: Source field on Tracklist model -- string column with values `'1001tracklists'` or `'fingerprint'`.
- D-07: Source badge on tracklist cards. Filter tabs work with source filter. Cards expand to show per-track confidence for fingerprint-sourced tracklists.
- D-08: Proposed -> Approved/Rejected status flow. Fingerprint tracklists start as 'proposed'. Individual tracks can be edited before approval.
- D-09: Color-coded confidence badges per track -- green (90%+), yellow (70-89%), red (<70%).
- D-10: "Reject All Low Confidence" bulk action -- button to remove all tracks below configurable confidence threshold.
- D-11: Editable fields: artist name, track title, timestamp, and delete track.
- D-12: Inline editing via HTMX. Click to edit, save on blur or enter.

### Claude's Discretion
- arq task structure for scan job (follow Phase 16 `fingerprint_file` pattern)
- Tracklist card expansion for fingerprint-sourced tracklists (HTMX partial structure)
- Batch scan file selection UI (checkboxes, select-all, filter by file type)
- Confidence threshold default for bulk reject (50% suggested, tunable)
- Status field implementation on Tracklist model (proposed/approved/rejected enum or string)
- Inline edit HTMX partial pattern (edit mode toggle, save endpoint)
- Alembic migration for new columns (source, confidence, status)

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| FPRINT-03 | User can scan a live set recording against the fingerprint DB to identify tracks with timestamps | Scan tab UI (D-01), `scan_live_set` arq task (D-03), `FingerprintOrchestrator.combined_query()` returns matches, persisted as Tracklist rows (D-04) |
| FPRINT-04 | Proposed tracklists from fingerprint matches displayed in admin UI for review and approval | Tracklist cards with source badge (D-07), per-track confidence (D-05, D-09), inline editing (D-11, D-12), approve/reject status flow (D-08), bulk reject low confidence (D-10) |
</phase_requirements>

## Standard Stack

No new libraries required. This phase uses only existing project dependencies.

### Core (already installed)
| Library | Version | Purpose | Phase 17 Usage |
|---------|---------|---------|----------------|
| FastAPI | >=0.135.2 | Web framework | New scan/edit endpoints on tracklists router |
| SQLAlchemy | >=2.0.48 | ORM | Model extensions (source, confidence, status columns) |
| Alembic | >=1.18.4 | Migrations | Migration 008 for new columns + backfill existing rows |
| arq | >=0.27.0 | Task queue | New `scan_live_set` task |
| HTMX | 2.x (CDN) | UI interactions | Scan tab, inline editing, approve/reject actions |
| Alpine.js | 3.x (CDN) | Client state | Batch selection checkboxes, edit mode toggling |
| Jinja2 | >=3.1 | Templates | New partials for scan tab, inline editing, confidence badges |

## Architecture Patterns

### Recommended Project Structure (new files)
```
src/phaze/
  models/
    tracklist.py           # MODIFY: add source, status to Tracklist; confidence to TracklistTrack
  routers/
    tracklists.py          # MODIFY: add scan tab, scan trigger, inline edit, approve/reject, bulk reject endpoints
  tasks/
    scan.py                # NEW: scan_live_set arq task
    worker.py              # MODIFY: register scan_live_set task
  services/
    fingerprint.py         # MODIFY: extend CombinedMatch/QueryMatch with timestamp field
  templates/tracklists/
    partials/
      scan_tab.html        # NEW: batch scan file selection UI
      scan_progress.html   # NEW: scan in-progress indicator
      fingerprint_track_detail.html  # NEW: track detail with confidence + inline edit
      inline_edit_field.html         # NEW: HTMX inline edit partial
      source_badge.html    # NEW: source badge (1001Tracklists / Fingerprint)
      confidence_badge.html # NEW: per-track confidence badge
      bulk_actions.html    # NEW: reject all low confidence button
alembic/versions/
  008_add_tracklist_source_status_confidence.py  # NEW
tests/
  test_routers/test_tracklists.py  # EXTEND
  test_tasks/test_scan.py          # NEW
  test_services/test_fingerprint.py # EXTEND (combined_query with timestamps)
```

### Pattern 1: Extending Existing Models with New Columns (Alembic)

**What:** Add `source` (String), `status` (String) to Tracklist; `confidence` (Float, nullable) to TracklistTrack.
**When to use:** When adding columns to existing tables that already have data.
**Key concern:** Backfill existing rows. All existing Tracklist rows must get `source='1001tracklists'` and `status='approved'` (they were already reviewed via scraping). Existing TracklistTrack rows keep `confidence=NULL` (scraped tracks have implicit 100% confidence).

```python
# alembic/versions/008_add_tracklist_source_status_confidence.py
from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    # Add source column with default for backfill
    op.add_column("tracklists", sa.Column("source", sa.String(30), nullable=False, server_default="1001tracklists"))
    # Add status column with default for backfill
    op.add_column("tracklists", sa.Column("status", sa.String(20), nullable=False, server_default="approved"))
    # Add confidence to tracks (nullable -- NULL means scraped/100%)
    op.add_column("tracklist_tracks", sa.Column("confidence", sa.Float(), nullable=True))
    # Add index on source for filtering
    op.create_index("ix_tracklists_source", "tracklists", ["source"])
    # Add index on status for filtering
    op.create_index("ix_tracklists_status", "tracklists", ["status"])

def downgrade() -> None:
    op.drop_index("ix_tracklists_status")
    op.drop_index("ix_tracklists_source")
    op.drop_column("tracklist_tracks", "confidence")
    op.drop_column("tracklists", "status")
    op.drop_column("tracklists", "source")
```

### Pattern 2: arq Scan Task (follow fingerprint_file pattern)

**What:** New `scan_live_set` task that calls `FingerprintOrchestrator.combined_query()` and persists results.
**Key design:** The fingerprint service `/query` endpoint handles audio segmentation internally (D-02). The main app just sends a file path and receives structured match results with timestamps.

```python
async def scan_live_set(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    """Scan a live set recording against the fingerprint DB.

    Per D-02: fingerprint service handles segmentation.
    Per D-04: results stored as Tracklist + TracklistVersion + TracklistTracks.
    """
    async with ctx["async_session"]() as session:
        result = await session.execute(
            select(FileRecord).where(FileRecord.id == uuid.UUID(file_id))
        )
        file_record = result.scalar_one_or_none()
        if file_record is None:
            return {"file_id": file_id, "status": "not_found"}

        orchestrator: FingerprintOrchestrator = ctx["fingerprint_orchestrator"]
        matches = await orchestrator.combined_query(file_record.current_path)

        if not matches:
            return {"file_id": file_id, "status": "no_matches"}

        # Create Tracklist with source='fingerprint', status='proposed'
        tracklist = Tracklist(
            external_id=f"fp-{file_record.id.hex[:12]}",
            source_url="",  # No external URL for fingerprint results
            file_id=file_record.id,
            source="fingerprint",
            status="proposed",
            artist=file_record.original_filename,  # Best we have
        )
        session.add(tracklist)

        version = TracklistVersion(
            tracklist_id=tracklist.id,
            version_number=1,
        )
        session.add(version)

        for i, match in enumerate(matches):
            track = TracklistTrack(
                version_id=version.id,
                position=i + 1,
                artist=match.resolved_artist,  # From fingerprint DB lookup
                title=match.resolved_title,
                timestamp=match.timestamp,
                confidence=match.confidence,
            )
            session.add(track)

        tracklist.latest_version_id = version.id
        await session.commit()

        return {"file_id": file_id, "status": "scanned", "tracklist_id": str(tracklist.id)}
```

### Pattern 3: HTMX Inline Editing

**What:** Click-to-edit fields that swap between display and edit mode.
**This is new to the project.** No existing inline edit pattern exists.

```html
<!-- Display mode (default) -->
<td class="py-2 pr-3 text-gray-900"
    hx-get="/tracklists/tracks/{{ track.id }}/edit/artist"
    hx-target="this"
    hx-swap="innerHTML"
    class="cursor-pointer hover:bg-blue-50">
    {{ track.artist or '-' }}
</td>

<!-- Edit mode (swapped in by HTMX) -->
<input type="text"
       name="artist"
       value="{{ track.artist }}"
       class="text-sm border border-blue-300 rounded px-2 py-1 w-full focus:outline-none focus:ring-1 focus:ring-blue-500"
       hx-put="/tracklists/tracks/{{ track.id }}/edit/artist"
       hx-target="closest td"
       hx-swap="innerHTML"
       hx-trigger="blur, keyup[keyCode==13]"
       autofocus>
```

**Endpoint pattern:**
```python
@router.get("/tracks/{track_id}/edit/{field}", response_class=HTMLResponse)
async def edit_track_field(track_id: uuid.UUID, field: str, ...) -> HTMLResponse:
    """Return an input field for inline editing."""
    # Returns the <input> partial

@router.put("/tracks/{track_id}/edit/{field}", response_class=HTMLResponse)
async def save_track_field(track_id: uuid.UUID, field: str, ...) -> HTMLResponse:
    """Save edited field, return display mode partial."""
    # Validates, saves, returns the display <td> content
```

### Pattern 4: Scan Tab Integration with Filter Tabs

**What:** Add a "Scan" tab alongside All/Matched/Unmatched.
**Key concern:** The scan tab shows a different view (file selection for scanning) rather than a filtered list of tracklists. Use Alpine.js to toggle between the tracklist list view and the scan view.

```html
<!-- Extended filter tabs -->
<button @click="activeTab = 'scan'"
        :class="activeTab === 'scan' ? 'active-tab-classes' : 'inactive-tab-classes'">
    Scan
</button>
```

When "Scan" tab is active, swap the main content area to show the batch file selection UI instead of the tracklist list.

### Anti-Patterns to Avoid
- **Duplicating tracklist model for fingerprint results:** Reuse the same Tracklist/TracklistVersion/TracklistTrack model. The `source` column distinguishes origin. Do NOT create separate FingerprintTracklist models.
- **Audio processing in the main app:** Per D-02, the fingerprint service handles segmentation. Do not import librosa or audio processing code into the main app for this phase.
- **Modal dialogs for track editing:** D-12 specifies inline editing. Do not build modal forms.
- **Polling for scan status:** Use HTMX polling (`hx-trigger="every 2s"`) on a status endpoint during scan, not WebSocket or SSE. Keeps it simple and consistent with existing arq patterns.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Inline editing | Custom JS edit framework | HTMX swap + Alpine.js toggle | Standard HTMX pattern; 10 lines of HTML vs. custom JS |
| Batch selection | Custom checkbox state manager | Alpine.js x-data with array tracking | Alpine's reactive arrays handle select-all/none natively |
| Confidence color tiers | Custom color function | Jinja2 macro or inline conditional | Already established in Phase 15 tracklist cards -- reuse the same pattern |
| Scan progress polling | Custom WebSocket/SSE | HTMX `hx-trigger="every 2s"` polling | arq tasks already store results; poll until done, then swap final content |

## Common Pitfalls

### Pitfall 1: QueryMatch/CombinedMatch Missing Timestamps
**What goes wrong:** The current `QueryMatch` and `CombinedMatch` dataclasses have only `track_id` and `confidence`. Live set scanning needs timestamp data (when each track appears in the recording).
**Why it happens:** Phase 16 designed these for single-file identification, not live set segmentation.
**How to avoid:** Extend `QueryMatch` with an optional `timestamp: str | None` field. The fingerprint service `/query` endpoint must return timestamps when querying a long-form recording. `CombinedMatch` also needs `timestamp` and resolved metadata (`artist`, `title`).
**Warning signs:** Tests pass but TracklistTrack rows have no timestamps.

### Pitfall 2: Backfill Migration for Existing Rows
**What goes wrong:** Adding `source` and `status` as NOT NULL columns without defaults breaks the migration on existing data.
**Why it happens:** Forgetting that the tracklists table already has rows from Phase 15.
**How to avoid:** Use `server_default` in the migration. Existing rows get `source='1001tracklists'` and `status='approved'`. After backfill, optionally remove the server_default if you want application-level control.
**Warning signs:** `IntegrityError` during migration.

### Pitfall 3: external_id Uniqueness for Fingerprint Tracklists
**What goes wrong:** The Tracklist model has `unique=True` on `external_id`. Fingerprint-generated tracklists need unique external IDs too.
**Why it happens:** `external_id` was designed for 1001tracklists URLs. Fingerprint results need a different ID scheme.
**How to avoid:** Use a deterministic ID format like `fp-{file_id_hex[:12]}` for fingerprint tracklists. If a file is re-scanned, either update the existing tracklist or generate a new version.
**Warning signs:** `UniqueViolation` on second scan of the same file.

### Pitfall 4: Inline Edit Race Conditions
**What goes wrong:** User clicks to edit a field, types, then clicks another field before blur fires. Two concurrent saves could conflict.
**Why it happens:** HTMX fires on blur, but clicking another element triggers blur on the first.
**How to avoid:** Each inline edit targets its own `<td>`, so concurrent saves to different fields on different tracks are fine (different DB rows). For the same track, SQLAlchemy's session handles sequential commits. Keep edit endpoints idempotent.
**Warning signs:** Stale data displayed after rapid edits.

### Pitfall 5: Filter Tab Interaction with Source
**What goes wrong:** Adding a "Scan" tab but not updating the filter logic to handle `source='fingerprint'` filtering alongside matched/unmatched.
**Why it happens:** The existing filter only considers `file_id IS NULL` (unmatched) vs not null (matched).
**How to avoid:** The Scan tab shows a different view entirely (file selection for scanning). For the existing tabs, optionally add source-based sub-filtering or a source badge that visually distinguishes fingerprint vs scraped tracklists without changing tab behavior.
**Warning signs:** Fingerprint tracklists mixed indistinguishably with scraped ones.

### Pitfall 6: Scan Tab is Not a Filter
**What goes wrong:** Trying to implement the Scan tab as another filter value sent to the same endpoint.
**Why it happens:** The other tabs (All/Matched/Unmatched) are filters. "Scan" is a different action.
**How to avoid:** Use Alpine.js to toggle visibility: when Scan tab is active, hide `#tracklists-list` and show `#scan-panel`. The scan panel is a separate HTMX-loaded partial with file selection UI.
**Warning signs:** The scan view shows an empty tracklist list instead of a file picker.

## Code Examples

### Existing Confidence Color Tier Pattern (reuse from tracklist_card.html)
```html
<!-- Source: src/phaze/templates/tracklists/partials/tracklist_card.html, lines 17-19 -->
<span class="text-sm {% if tracklist.match_confidence >= 90 %}text-green-600{% elif tracklist.match_confidence >= 70 %}text-yellow-600{% else %}text-red-600{% endif %}">
    {{ tracklist.match_confidence }}%
</span>
```

### Existing arq Task Pattern (reference from tasks/fingerprint.py)
```python
# Source: src/phaze/tasks/fingerprint.py
async def fingerprint_file(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    try:
        async with ctx["async_session"]() as session:
            # ... query file, call orchestrator, persist results, commit
            pass
    except Exception as exc:
        raise Retry(defer=ctx["job_try"] * 5) from exc
```

### Existing Filter Tab Pattern (extend for Scan tab)
```html
<!-- Source: src/phaze/templates/tracklists/partials/filter_tabs.html -->
<div x-data="{ activeTab: '{{ active_filter }}' }" class="flex gap-1 border-b border-gray-200 mb-4">
    <!-- Add Scan tab button here following the same pattern -->
</div>
```

### Existing Worker Registration Pattern
```python
# Source: src/phaze/tasks/worker.py, line 88-96
class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        process_file,
        # ... existing tasks ...
        fingerprint_file,
        # ADD: scan_live_set
    ]
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Separate models for different tracklist sources | Single Tracklist model with `source` column | Phase 17 design (D-04) | Avoids model proliferation, reuses all existing UI |
| Full-page edit forms | HTMX inline editing | Standard HTMX pattern | Less context switching, faster corrections |

## Open Questions

1. **CombinedMatch timestamp and metadata resolution**
   - What we know: `CombinedMatch` currently has `track_id` (string), `confidence` (float), `engines` (dict). For live set scanning, we need timestamp and resolved artist/title.
   - What's unclear: Does the fingerprint service `/query` endpoint already return timestamps and metadata, or do we need to look up `track_id` against the `files` table to resolve artist/title from FileMetadata?
   - Recommendation: Extend `QueryMatch` and `CombinedMatch` with `timestamp: str | None` and add a resolution step in the scan task that looks up `track_id` in the files table to get artist/title from FileMetadata. The fingerprint service returns `track_id` (which is a file_id from ingestion) and the main app resolves metadata.

2. **Re-scanning a file**
   - What we know: `external_id` must be unique. First scan generates `fp-{hex}`.
   - What's unclear: Should re-scanning create a new TracklistVersion (like re-scraping), or replace the existing tracklist entirely?
   - Recommendation: Follow the re-scrape pattern -- create a new TracklistVersion with incremented version_number, update `latest_version_id`. This preserves history of previous scans.

3. **Scan progress feedback**
   - What we know: Scanning is async via arq (D-03). User triggers and results appear when done.
   - What's unclear: How to show intermediate progress during a long scan.
   - Recommendation: After triggering scan, show a simple "Scanning..." indicator on the file row. Use HTMX polling (`hx-trigger="every 3s"`) on a status endpoint that checks arq job result. When complete, swap in the tracklist card. Keep it simple.

## Environment Availability

Step 2.6: SKIPPED (no external dependencies -- this phase is purely code/config/template changes within the existing application stack).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` ([tool.pytest.ini_options]) |
| Quick run command | `uv run pytest tests/test_tasks/test_scan.py tests/test_routers/test_tracklists.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| FPRINT-03 | scan_live_set task queries fingerprint DB and creates tracklist rows | unit | `uv run pytest tests/test_tasks/test_scan.py -x` | Wave 0 |
| FPRINT-03 | Scan tab UI renders file selection list | unit | `uv run pytest tests/test_routers/test_tracklists.py::test_scan_tab -x` | Wave 0 |
| FPRINT-03 | Scan trigger endpoint enqueues arq job | unit | `uv run pytest tests/test_routers/test_tracklists.py::test_trigger_scan -x` | Wave 0 |
| FPRINT-04 | Fingerprint tracklist cards show source badge and per-track confidence | unit | `uv run pytest tests/test_routers/test_tracklists.py::test_fingerprint_card -x` | Wave 0 |
| FPRINT-04 | Inline edit endpoints save and return updated partial | unit | `uv run pytest tests/test_routers/test_tracklists.py::test_inline_edit -x` | Wave 0 |
| FPRINT-04 | Approve/reject status transitions | unit | `uv run pytest tests/test_routers/test_tracklists.py::test_approve_reject -x` | Wave 0 |
| FPRINT-04 | Bulk reject low confidence removes tracks below threshold | unit | `uv run pytest tests/test_routers/test_tracklists.py::test_bulk_reject -x` | Wave 0 |
| FPRINT-03 | CombinedMatch includes timestamp field | unit | `uv run pytest tests/test_services/test_fingerprint.py -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_tasks/test_scan.py tests/test_routers/test_tracklists.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_tasks/test_scan.py` -- covers FPRINT-03 (scan task logic)
- [ ] New test cases in `tests/test_routers/test_tracklists.py` -- covers FPRINT-03, FPRINT-04 (scan tab, inline edit, approve/reject, bulk reject)
- [ ] New test cases in `tests/test_services/test_fingerprint.py` -- covers FPRINT-03 (CombinedMatch timestamp extension)

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively** -- all code must target 3.13
- **uv only** -- never use bare pip, python, pytest, or mypy. Always `uv run` prefix.
- **Pre-commit must pass** -- all hooks must pass before commits
- **85% code coverage** -- minimum required
- **Type hints on all functions** -- mypy strict mode (excluding tests)
- **150-char line length** -- ruff configured
- **Double quotes** -- ruff format enforced
- **Worktree branches** -- every feature gets its own worktree and PR
- **Frequent commits** -- commit during execution, not batched at end
- **Just commands** -- justfile as command runner, GitHub Actions delegate to just
- **README per service** -- keep updated
- **Frozen SHAs for pre-commit hooks**

## Sources

### Primary (HIGH confidence)
- `src/phaze/models/tracklist.py` -- current Tracklist/TracklistVersion/TracklistTrack model structure
- `src/phaze/services/fingerprint.py` -- FingerprintOrchestrator, CombinedMatch, QueryMatch dataclasses
- `src/phaze/routers/tracklists.py` -- existing router with filter tabs, HTMX partials, card layout
- `src/phaze/tasks/fingerprint.py` -- existing arq task pattern for fingerprint_file
- `src/phaze/tasks/worker.py` -- WorkerSettings registration pattern
- `src/phaze/templates/tracklists/` -- all existing templates (card, filter tabs, track detail)
- `.planning/phases/15-1001tracklists-integration/15-UI-SPEC.md` -- confidence color tiers, card layout specs
- `.planning/phases/17-live-set-matching-tracklist-review/17-CONTEXT.md` -- all locked decisions D-01 through D-12
- `alembic/versions/` -- existing migration numbering (007 is latest, next is 008)

### Secondary (MEDIUM confidence)
- HTMX inline editing pattern -- standard HTMX approach using `hx-trigger="blur"` and swap. Well-documented in HTMX docs but not yet used in this project.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new libraries, all existing dependencies
- Architecture: HIGH -- extending well-established patterns (arq tasks, HTMX partials, SQLAlchemy models)
- Pitfalls: HIGH -- identified from direct codebase inspection (missing timestamps, backfill needs, unique constraint)

**Research date:** 2026-04-01
**Valid until:** 2026-05-01 (stable -- all dependencies frozen, no external API changes expected)
