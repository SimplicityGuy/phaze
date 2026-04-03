# Phase 20: Tag Writing - Research

**Researched:** 2026-04-03
**Domain:** Audio metadata writing (mutagen), review UI (HTMX/Jinja2), audit logging (PostgreSQL/SQLAlchemy)
**Confidence:** HIGH

## Summary

Phase 20 adds the ability to write corrected metadata tags to destination file copies. The entire stack is already in the project: mutagen (read+write), FastAPI+HTMX+Jinja2 (UI), SQLAlchemy+Alembic (models/migrations), and PostgreSQL JSONB (audit snapshots). No new dependencies are needed.

The core workflow is: compute proposed tags from tracklist data + FileMetadata + filename parsing (priority cascade per D-02), display them side-by-side with current tags on a `/tags` review page, let the user inline-edit and approve, write tags synchronously with mutagen, verify by re-reading, and log everything in an append-only TagWriteLog table.

**Primary recommendation:** Follow existing patterns exactly -- ExecutionLog for audit model, tracklists router for HTMX inline editing, metadata service for mutagen format-specific handling. The only novel code is the write-side of mutagen (reverse of existing read maps) and the proposed tag computation service (cascade merge).

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Tag sources are FileMetadata (existing tags from file), filename parsing, and 1001tracklists data (artist, event from Tracklist model). NOT Discogs -- DiscogsLinks are per-track within a tracklist, not per-file. Discogs data is for CUE sheets (Phase 21).
- **D-02:** Priority cascade for merging: tracklist data wins over FileMetadata wins over filename parsing. Each field resolved independently.
- **D-03:** Only EXECUTED files (with destination copies) are eligible for tag writing.
- **D-04:** Dedicated '/tags' page as a nav tab. Shows files with pending tag proposals in a table. Click to expand and see proposed vs current tags side-by-side.
- **D-05:** Two-column table layout: Field | Current | Proposed. Changed fields highlighted (bold or colored). Empty fields show '--'.
- **D-06:** Core music fields only: artist, title, album, year, genre, track number. Duration/bitrate are read-only, not writable.
- **D-07:** Proposed column cells are editable inline -- user can tweak values before approving. Same HTMX inline edit pattern as tracklist tracks.
- **D-08:** Verify-after-write: re-open file with mutagen and compare each written field against what was sent. Flag mismatches.
- **D-09:** Discrepancies flagged and logged (which field, expected vs actual) with 'discrepancy' status in TagWriteLog. Don't block user -- cosmetic discrepancies (encoding normalization) are common.
- **D-10:** Tag writes run synchronously per-file when user approves. Single file write is fast (~100ms). No SAQ background task needed. Immediate feedback.
- **D-11:** Per-file snapshot granularity. One TagWriteLog entry per file write with before_tags and after_tags as JSONB snapshots.
- **D-12:** Source field tracks which data source was used (tracklist, metadata, manual_edit). No user attribution needed -- single-user app.
- **D-13:** Append-only table -- no updates or deletes. Matches ExecutionLog pattern from Phase 8.

### Claude's Discretion
- TagWriteLog model schema details (columns, indexes, relationships)
- Tag write service implementation (mutagen write API per format)
- Proposed tag computation service (cascade merge logic)
- HTMX partial structure for tag review page
- Filename parsing strategy for extracting metadata
- Nav tab ordering (where '/tags' appears relative to other tabs)

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| TAGW-01 | User can write corrected tags to destination file copies (never originals) with format-aware encoding (ID3/Vorbis/MP4) | Mutagen write API patterns documented per format; existing `_ID3_MAP`, `_VORBIS_MAP`, `_MP4_MAP` reversed for writing; FileState.EXECUTED gate ensures only destination copies |
| TAGW-02 | Tag writes are verified by re-reading the file after write, with discrepancies flagged | Verify-after-write pattern: `mutagen.File()` re-read + field comparison; discrepancy status in TagWriteLog |
| TAGW-03 | All tag writes logged in append-only TagWriteLog audit table | ExecutionLog pattern replicated; JSONB before/after snapshots; append-only (no UPDATE/DELETE) |
| TAGW-04 | Tag review page shows proposed vs current tags side-by-side before user approves the write | HTMX partial pattern from tracklists; two-column comparison layout; inline edit on proposed values |
</phase_requirements>

## Standard Stack

### Core (already installed -- zero new dependencies)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| mutagen | >=1.47.0 | Audio tag read+write | Already used for extraction. Same library writes tags. Supports ID3, Vorbis, MP4, FLAC, OGG, OPUS. |
| FastAPI | >=0.135.2 | Router for /tags page | Existing web framework. New router follows proposals/tracklists pattern. |
| SQLAlchemy | >=2.0.48 | TagWriteLog model + queries | Existing ORM. New model follows ExecutionLog append-only pattern. |
| Alembic | >=1.18.4 | Migration for tag_write_log table | Existing migration tool. Migration 011. |
| Jinja2 | >=3.1 | Tag review templates | Existing template engine. |
| HTMX | 2.x (CDN) | Inline editing, approve/reject actions | Existing. Reuse tracklist inline edit pattern. |

### No New Dependencies
This phase requires zero new pip packages. Everything needed is already in `pyproject.toml`.

## Architecture Patterns

### New Files
```
src/phaze/
  models/
    tag_write_log.py        # TagWriteLog audit model (append-only, JSONB snapshots)
  services/
    tag_writer.py            # Write tags to file + verify (mutagen write API)
    tag_proposal.py          # Compute proposed tags (cascade merge from sources)
  routers/
    tags.py                  # /tags page endpoints (list, detail, approve, inline edit)
  templates/
    tags/
      list.html              # Full page: tag review list (extends base.html)
      partials/
        tag_list.html        # HTMX partial: file table with pending tag proposals
        tag_comparison.html  # HTMX partial: side-by-side Field|Current|Proposed
        inline_edit.html     # HTMX partial: editable proposed field input
        inline_display.html  # HTMX partial: display-mode proposed field
alembic/versions/
  011_add_tag_write_log.py   # Migration: create tag_write_log table
tests/
  test_services/
    test_tag_writer.py       # Tag write + verify tests (use temp files)
    test_tag_proposal.py     # Cascade merge logic tests
  test_routers/
    test_tags.py             # /tags endpoint integration tests
  test_models/
    test_tag_write_log.py    # Model creation + append-only behavior
```

### Pattern 1: TagWriteLog Model (follows ExecutionLog)
**What:** Append-only audit table with JSONB before/after snapshots
**When to use:** Every tag write operation creates one row

```python
# Source: existing ExecutionLog pattern in src/phaze/models/execution.py
import enum
import uuid
from datetime import datetime
from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from phaze.models.base import Base, TimestampMixin

class TagWriteStatus(enum.StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    DISCREPANCY = "discrepancy"

class TagWriteLog(TimestampMixin, Base):
    __tablename__ = "tag_write_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)
    before_tags: Mapped[dict] = mapped_column(JSONB, nullable=False)
    after_tags: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)  # tracklist, metadata, manual_edit
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    discrepancies: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {field: {expected, actual}}
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    written_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

### Pattern 2: Mutagen Write API (per-format)
**What:** Reverse the existing read maps to write tags
**When to use:** When user approves a tag write

```python
# ID3 (MP3): use frame classes with encoding=3 (UTF-8)
from mutagen.id3 import ID3, TPE1, TIT2, TALB, TDRC, TCON, TRCK
audio = mutagen.File(path)
audio.tags.add(TPE1(encoding=3, text=[artist]))
audio.tags.add(TIT2(encoding=3, text=[title]))
audio.tags.add(TALB(encoding=3, text=[album]))
audio.tags.add(TDRC(encoding=3, text=[str(year)]))
audio.tags.add(TCON(encoding=3, text=[genre]))
audio.tags.add(TRCK(encoding=3, text=[str(track_number)]))
audio.save()

# Vorbis (OGG, FLAC, OPUS): direct key assignment
audio = mutagen.File(path)
audio["artist"] = [artist]
audio["title"] = [title]
audio["album"] = [album]
audio["date"] = [str(year)]
audio["genre"] = [genre]
audio["tracknumber"] = [str(track_number)]
audio.save()

# MP4 (M4A): atom key assignment
audio = mutagen.File(path)
audio["\xa9ART"] = [artist]
audio["\xa9nam"] = [title]
audio["\xa9alb"] = [album]
audio["\xa9day"] = [str(year)]
audio["\xa9gen"] = [genre]
audio["trkn"] = [(track_number, 0)]  # tuple format: (track, total)
audio.save()
```

### Pattern 3: Cascade Merge for Proposed Tags
**What:** Merge tag values from multiple sources with priority
**When to use:** Computing what to propose for each file

Priority (D-02): tracklist > FileMetadata > filename parsing. Each field resolved independently.

```python
# Per-field resolution
def compute_proposed_tags(
    file_metadata: FileMetadata | None,
    tracklist: Tracklist | None,
    filename: str,
) -> dict[str, str | int | None]:
    parsed = parse_filename(filename)
    fields = {}
    for field in ("artist", "title", "album", "year", "genre", "track_number"):
        # Lowest priority first, higher overwrites
        val = getattr(parsed, field, None)
        if file_metadata and getattr(file_metadata, field, None) is not None:
            val = getattr(file_metadata, field)
        if tracklist:
            # artist -> artist, event -> album (D-specifics)
            if field == "artist" and tracklist.artist:
                val = tracklist.artist
            elif field == "album" and tracklist.event:
                val = tracklist.event
        if val is not None:
            fields[field] = val
    return fields
```

### Pattern 4: HTMX Inline Edit (from tracklists)
**What:** Click-to-edit on proposed tag values
**When to use:** Tag comparison view

The existing pattern in `tracklists/partials/inline_edit_field.html` and `inline_display_field.html` provides the exact interaction model. Replicate for tag fields:
- GET `/tags/{file_id}/edit/{field}` returns input element
- PUT `/tags/{file_id}/edit/{field}` saves value, returns display span
- `hx-trigger="blur, keyup[keyCode==13]"` for save on blur or Enter

### Anti-Patterns to Avoid
- **Writing to original files:** Gate on `FileState.EXECUTED` and use `current_path` (which points to destination copy after execution). Never touch `original_path`.
- **Async background write:** D-10 says synchronous. Single file write is ~100ms. Don't add SAQ complexity.
- **Updating TagWriteLog rows:** Append-only. New write = new row. Never UPDATE or DELETE.
- **Writing duration/bitrate:** D-06 says these are read-only. Only write the 6 core fields.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Tag format detection | Custom format sniffing | `mutagen.File(path)` then `isinstance()` checks | mutagen auto-detects format. Existing `extract_tags` already does this. |
| Tag encoding | Manual encoding handling | `encoding=3` (UTF-8) for ID3 frames | mutagen handles encoding internally. encoding=3 = UTF-8, the modern standard. |
| JSONB snapshots | Custom serialization | `_serialize_tags()` from existing metadata service | Already handles binary exclusion, list flattening, and type coercion. |
| Migration boilerplate | Autogenerate | Manual migration following pattern of `010_add_discogs_links.py` | Project uses manual migrations with explicit `op.create_table()`. |

## Common Pitfalls

### Pitfall 1: ID3 Tags Require Frame Objects, Not Dict Assignment
**What goes wrong:** Trying `audio.tags["TPE1"] = "Artist"` silently fails or raises TypeError.
**Why it happens:** ID3 tags use frame objects, not plain strings. Unlike Vorbis/MP4, you must use `audio.tags.add(TPE1(encoding=3, text=["value"]))`.
**How to avoid:** Use the frame class constructors (TPE1, TIT2, TALB, TDRC, TCON, TRCK) with `encoding=3` and `text=[value]`.
**Warning signs:** Tags appear blank after write, or mutagen raises TypeError.

### Pitfall 2: MP4 Track Number Is a Tuple
**What goes wrong:** Setting `audio["trkn"] = [5]` fails or writes garbage.
**Why it happens:** MP4 track number format is `[(track_number, total_tracks)]` as a list of tuples.
**How to avoid:** Use `audio["trkn"] = [(track_number, 0)]` where 0 means "total unknown".
**Warning signs:** Verification re-read returns unexpected track_number value.

### Pitfall 3: Verify-After-Write Encoding Normalization
**What goes wrong:** Written "Beyonce" reads back as "Beyonce" but comparison fails due to Unicode normalization differences (NFC vs NFD).
**Why it happens:** Some formats normalize Unicode on write. Especially common with accented characters.
**How to avoid:** Normalize both expected and actual to NFC before comparison. Use `unicodedata.normalize("NFC", value)`. Log discrepancies as warnings, not errors (D-09).
**Warning signs:** Discrepancy status on files with accented characters or non-ASCII text.

### Pitfall 4: Missing Tags Object on New Files
**What goes wrong:** `audio.tags` is None on a file that has never had tags written.
**Why it happens:** Some formats don't create a tags container until you explicitly add tags.
**How to avoid:** Call `audio.add_tags()` if `audio.tags is None` before writing. For ID3, use `audio.add_tags()` which creates an empty ID3 header.
**Warning signs:** AttributeError on `audio.tags.add()` for files with no existing tags.

### Pitfall 5: Writing to Original Instead of Destination
**What goes wrong:** Tags written to the source file, corrupting the original.
**Why it happens:** Using `original_path` instead of `current_path` on the FileRecord.
**How to avoid:** Gate on `FileState.EXECUTED` and use `current_path`. After execution, `current_path` points to the destination copy.
**Warning signs:** Original files modified on disk.

## Code Examples

### Tag Writer Service Pattern
```python
# Source: reverse of existing services/metadata.py extract_tags()
import mutagen
from mutagen.id3 import ID3, TPE1, TIT2, TALB, TDRC, TCON, TRCK
from mutagen.mp4 import MP4

# Reverse maps: field_name -> format-specific key
_WRITE_ID3_MAP = {
    "artist": TPE1,
    "title": TIT2,
    "album": TALB,
    "year": TDRC,
    "genre": TCON,
    "track_number": TRCK,
}

_WRITE_VORBIS_MAP = {
    "artist": "artist",
    "title": "title",
    "album": "album",
    "year": "date",
    "genre": "genre",
    "track_number": "tracknumber",
}

_WRITE_MP4_MAP = {
    "artist": "\xa9ART",
    "title": "\xa9nam",
    "album": "\xa9alb",
    "year": "\xa9day",
    "genre": "\xa9gen",
    "track_number": "trkn",
}

def write_tags(file_path: str, tags: dict[str, str | int | None]) -> None:
    """Write normalized tags to a file using format-appropriate encoding."""
    audio = mutagen.File(file_path)
    if audio is None:
        raise ValueError(f"Cannot open file: {file_path}")

    if audio.tags is None:
        audio.add_tags()

    if isinstance(audio.tags, ID3):
        for field, value in tags.items():
            if value is None:
                continue
            frame_cls = _WRITE_ID3_MAP.get(field)
            if frame_cls:
                audio.tags.add(frame_cls(encoding=3, text=[str(value)]))
    elif isinstance(audio, MP4):
        for field, value in tags.items():
            if value is None:
                continue
            mp4_key = _WRITE_MP4_MAP.get(field)
            if mp4_key:
                if field == "track_number":
                    audio[mp4_key] = [(int(value), 0)]
                else:
                    audio[mp4_key] = [str(value)]
    else:
        # Vorbis (OGG, FLAC, OPUS)
        for field, value in tags.items():
            if value is None:
                continue
            vorbis_key = _WRITE_VORBIS_MAP.get(field)
            if vorbis_key:
                audio[vorbis_key] = [str(value)]

    audio.save()
```

### Verification Pattern
```python
from phaze.services.metadata import extract_tags

def verify_write(file_path: str, expected: dict[str, str | int | None]) -> dict[str, dict]:
    """Re-read file after write and compare fields. Returns discrepancy dict."""
    import unicodedata
    actual = extract_tags(file_path)
    discrepancies = {}
    for field, expected_val in expected.items():
        if expected_val is None:
            continue
        actual_val = getattr(actual, field, None)
        # Normalize for comparison
        exp_str = unicodedata.normalize("NFC", str(expected_val))
        act_str = unicodedata.normalize("NFC", str(actual_val)) if actual_val is not None else None
        if exp_str != act_str:
            discrepancies[field] = {"expected": exp_str, "actual": act_str}
    return discrepancies
```

### Router Endpoint Pattern
```python
# Source: existing tracklists.py and proposals.py router patterns
@router.get("/", response_class=HTMLResponse)
async def list_tags(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=10, le=100),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render tag review page or HTMX partial."""
    # Query EXECUTED files that have pending proposed tags
    ...
    context = {"request": request, "files": files, "current_page": "tags", ...}
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request=request, name="tags/partials/tag_list.html", context=context)
    return templates.TemplateResponse(request=request, name="tags/list.html", context=context)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| ID3v1 tags | ID3v2.4 with UTF-8 encoding | Long established | Always use encoding=3 for ID3 frames |
| Separate read/write libs | mutagen handles both | Stable | No additional dependency needed |
| Sync-only mutagen | Still sync-only | N/A | Acceptable per D-10 (~100ms per file). Run in endpoint handler. |

## Open Questions

1. **Filename parsing strategy**
   - What we know: D-02 lists filename parsing as lowest-priority tag source. Common patterns include "Artist - Title.mp3", "Artist - Event - Title.mp3"
   - What's unclear: Exact regex patterns needed for the user's file naming conventions
   - Recommendation: Start with common patterns (split on " - ", extract year from parentheses). Make it extensible. User can tweak proposed values via inline edit anyway.

2. **Tracklist-to-file tag mapping**
   - What we know: D-specifics say artist -> artist tag, event -> album tag
   - What's unclear: Whether tracklist date should map to year tag
   - Recommendation: Map tracklist.date.year to year tag as lowest-priority fallback (only if no year from other sources). The year field is in the core 6.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/test_services/test_tag_writer.py tests/test_services/test_tag_proposal.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| TAGW-01 | Write tags to MP3/M4A/OGG/OPUS/FLAC destination copies | unit | `uv run pytest tests/test_services/test_tag_writer.py -x` | Wave 0 |
| TAGW-01 | Gate on FileState.EXECUTED (reject non-executed) | unit+integration | `uv run pytest tests/test_services/test_tag_writer.py::test_rejects_non_executed -x` | Wave 0 |
| TAGW-02 | Verify-after-write re-reads file, flags discrepancies | unit | `uv run pytest tests/test_services/test_tag_writer.py::test_verify_after_write -x` | Wave 0 |
| TAGW-03 | TagWriteLog created on every write with JSONB snapshots | unit+integration | `uv run pytest tests/test_models/test_tag_write_log.py -x` | Wave 0 |
| TAGW-04 | Review page shows proposed vs current side-by-side | integration | `uv run pytest tests/test_routers/test_tags.py -x` | Wave 0 |
| TAGW-04 | Inline edit on proposed fields | integration | `uv run pytest tests/test_routers/test_tags.py::test_inline_edit -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_services/test_tag_writer.py tests/test_services/test_tag_proposal.py tests/test_routers/test_tags.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/test_tag_writer.py` -- covers TAGW-01, TAGW-02 (write + verify per format)
- [ ] `tests/test_services/test_tag_proposal.py` -- covers cascade merge logic
- [ ] `tests/test_routers/test_tags.py` -- covers TAGW-04 (review page, inline edit, approve)
- [ ] `tests/test_models/test_tag_write_log.py` -- covers TAGW-03 (model creation, JSONB snapshots)
- [ ] Test audio fixtures: small valid MP3, M4A, OGG, OPUS, FLAC files for write tests (use mutagen to generate in conftest or fixtures/)

## Sources

### Primary (HIGH confidence)
- `src/phaze/services/metadata.py` -- existing mutagen read patterns, format maps, ExtractedTags dataclass
- `src/phaze/models/execution.py` -- ExecutionLog append-only audit pattern
- `src/phaze/models/file.py` -- FileState enum, FileRecord.current_path for destination
- `src/phaze/routers/tracklists.py` -- HTMX inline edit pattern (GET edit, PUT save)
- `src/phaze/templates/tracklists/partials/inline_edit_field.html` -- inline edit HTML template
- `src/phaze/templates/base.html` -- nav bar structure for adding Tags tab
- `alembic/versions/010_add_discogs_links.py` -- migration pattern (manual, with revision chain)
- mutagen installed package -- verified write imports: `mutagen.id3.{TPE1,TIT2,TALB,TDRC,TCON,TRCK}`, `mutagen.mp4.MP4`, `mutagen.oggvorbis.OggVorbis`, `mutagen.flac.FLAC`, `mutagen.oggopus.OggOpus`

### Secondary (MEDIUM confidence)
- [mutagen documentation](https://mutagen.readthedocs.io/en/latest/) -- write API patterns
- [mutagen PyPI](https://pypi.org/project/mutagen/) -- version 1.47.0

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all libraries already installed and used in production
- Architecture: HIGH -- all patterns directly replicated from existing codebase (ExecutionLog, tracklists router, metadata service)
- Pitfalls: HIGH -- format-specific quirks verified against mutagen source and existing read code
- Tag writing: HIGH -- verified mutagen write imports work in project's Python environment

**Research date:** 2026-04-03
**Valid until:** 2026-05-03 (stable domain, no moving parts)

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively**, `uv run` prefix for all commands
- **Pre-commit hooks must pass** before commits (frozen SHAs)
- **85% code coverage** minimum
- **Type hints on all functions** (mypy strict mode, excluding tests)
- **150-char line length**, double quotes, PEP 8
- **Alembic migrations** for schema changes (manual, sequential revision IDs)
- **HTMX + Jinja2** for UI (no SPA, no JS frameworks)
- **Never push directly to main** -- PR per feature
- **Register new model** in `src/phaze/models/__init__.py` for Alembic autogenerate
- **Register new router** in `src/phaze/main.py` `create_app()`
