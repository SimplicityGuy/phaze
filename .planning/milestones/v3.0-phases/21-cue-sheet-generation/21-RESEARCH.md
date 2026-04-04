# Phase 21: CUE Sheet Generation - Research

**Researched:** 2026-04-03
**Domain:** CUE sheet file format, string-based file generation, timestamp conversion
**Confidence:** HIGH

## Summary

CUE sheet generation is a well-defined problem with a stable specification (CDRWIN format, unchanged since the late 1990s). The phase requires no new dependencies -- it is pure string formatting plus filesystem write. The core technical challenges are: (1) correctly converting source timestamps (HH:MM:SS from 1001tracklists or seconds-as-string from fingerprint services) to CUE's MM:SS:FF format at 75 frames per second, (2) mapping audio file extensions to CUE FILE type keywords, and (3) writing UTF-8 with BOM encoding.

The existing codebase provides strong patterns to follow. The `tag_writer.py` service demonstrates synchronous file operations with verify-after-write, and the `tags.py` router demonstrates a dedicated management page with HTMX partials and stats. The tracklist card template shows how to add inline action buttons. The data model already has all required fields: `TracklistTrack.timestamp` for source times, `DiscogsLink` with accepted status for Discogs metadata enrichment, and `FileRecord.current_path` for determining the CUE output location.

**Primary recommendation:** Build a pure-Python `CueGenerator` service class that takes a tracklist's tracks (with optional DiscogsLink data) and produces a CUE file string. No third-party CUE libraries needed -- the format is simple enough that hand-rolling is correct here.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** CUE timestamps use MM:SS:FF format at 75 frames per second per the CUE sheet specification
- **D-02:** Fingerprint timestamps always take priority over 1001tracklists timestamps when both exist for the same track
- **D-03:** Tracks without any timestamp (no fingerprint offset, no 1001tracklists time) are omitted from the generated CUE file entirely
- **D-04:** "Generate CUE" button on the tracklist detail page (inline, alongside existing actions like Match to Discogs)
- **D-05:** Dedicated CUE management page (/cue nav tab) listing all tracklists with CUE generation status. Supports batch generation.
- **D-06:** Only tracklists with status='approved' are eligible for CUE generation
- **D-07:** CUE generation runs synchronously (pure string formatting + file write). No SAQ background task needed.
- **D-08:** REM comments are per-track, not disc-level. Each TRACK section gets REM GENRE, REM LABEL, REM YEAR from that track's accepted DiscogsLink.
- **D-09:** Tracks without an accepted DiscogsLink get no REM comments
- **D-10:** .cue files are written to the filesystem next to the destination audio file (same directory, same base name with .cue extension). Requires the tracklist's linked file to have state EXECUTED (current_path is the destination).
- **D-11:** FILE command in the CUE references the audio filename only (not a full or relative path)
- **D-12:** Re-generating a CUE file uses version suffix naming (e.g., file.v2.cue, file.v3.cue)
- **D-13:** CUE files use UTF-8 encoding with BOM (byte order mark)

### Claude's Discretion
- CUE generation service implementation (string building, frame conversion math)
- CUE management page layout and filtering options
- HTMX partial structure for inline CUE status on tracklist page
- Batch generation loop on CUE management page
- Audio file type mapping for FILE command (MP3, WAVE, AIFF, etc.)
- Version number tracking strategy (scan existing files or DB counter)
- Nav tab ordering for /cue page

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CUE-01 | System generates .cue companion files from tracklist data, preferring fingerprint timestamps with 1001tracklists fallback | CUE format spec researched, timestamp priority logic defined (D-02), omission rule for missing timestamps (D-03), FILE/TRACK/INDEX command syntax documented |
| CUE-02 | CUE files use correct 75fps frame conversion and UTF-8 with BOM encoding | Frame conversion formula documented (seconds to MM:SS:FF at 75fps), UTF-8 BOM byte sequence identified (EF BB BF / `\ufeff`), Python `codecs` approach verified |
| CUE-03 | CUE files include REM comments with Discogs metadata (genre, label, catalog number, year) | DiscogsLink model has discogs_label, discogs_year fields; REM syntax is `REM KEY value`; per-track placement after TRACK command, before INDEX |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively**, `uv run` prefix for all commands
- **Pre-commit hooks must pass** before commits (frozen SHAs)
- **85% minimum code coverage** required
- **Type hints on all functions** (mypy strict mode, excluding tests)
- **150-character line length**, double quotes, PEP 8
- **Ruff** for linting and formatting
- **Every feature gets its own git worktree and PR**
- **Never push directly to main**

## Standard Stack

### Core (No New Dependencies)

This phase requires zero new pip dependencies. Everything is pure Python stdlib + existing project dependencies.

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib (pathlib, codecs) | 3.13 | File path manipulation, UTF-8 BOM writing | Built-in, no dependency |
| FastAPI | existing | New /cue router endpoints | Already in project |
| SQLAlchemy | existing | Query tracklists, tracks, DiscogsLinks | Already in project |
| Jinja2 | existing | CUE management page templates | Already in project |
| HTMX | existing (CDN) | Dynamic CUE generation button, batch UI | Already in project |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Hand-rolled CUE generation | pycue / cueparser libraries | CUE format is trivially simple (string concatenation). Libraries add dependency for no benefit. Hand-rolling is correct here. |
| Filesystem version scanning | DB version counter column | DB counter requires migration. Scanning existing .cue files with glob is simpler and accurate. Recommend filesystem scan. |

## Architecture Patterns

### Recommended Project Structure

```
src/phaze/
  services/
    cue_generator.py          # CUE content generation + file writing
  routers/
    cue.py                    # CUE management page endpoints
  templates/
    cue/
      list.html               # Full CUE management page
      partials/
        cue_list.html          # HTMX partial for tracklist list with CUE status
        cue_status.html        # Per-tracklist CUE generation status badge
        toast.html             # Success/error toast after generation
```

### Pattern 1: CUE Generator Service

**What:** Pure function that takes structured data and returns a CUE file content string. Separate function handles file writing.
**When to use:** Always -- separation of content generation from I/O enables easy testing.

```python
# Service structure (Claude's discretion on implementation)
def generate_cue_content(
    audio_filename: str,
    audio_type: str,
    tracks: list[CueTrackData],
) -> str:
    """Generate CUE sheet content as a string."""
    ...

def write_cue_file(
    content: str,
    destination_dir: Path,
    base_name: str,
) -> Path:
    """Write CUE content to filesystem with version suffix if needed."""
    ...
```

### Pattern 2: Timestamp Conversion (75fps Frames)

**What:** Convert source timestamps to CUE MM:SS:FF format.
**When to use:** For every track's INDEX 01 command.

The CUE specification defines timestamps as `MM:SS:FF` where:
- MM = minutes (00-99)
- SS = seconds (00-59)
- FF = frames at 75 frames per second (00-74)

**Conversion from seconds (float or string):**
```python
def seconds_to_cue_timestamp(total_seconds: float) -> str:
    """Convert seconds to CUE MM:SS:FF format (75fps)."""
    total_frames = int(total_seconds * 75)
    frames = total_frames % 75
    total_secs = total_frames // 75
    minutes = total_secs // 60
    seconds = total_secs % 60
    return f"{minutes:02d}:{seconds:02d}:{frames:02d}"
```

**Conversion from HH:MM:SS or MM:SS string (1001tracklists format):**
```python
def parse_timestamp_string(ts: str) -> float:
    """Parse HH:MM:SS or MM:SS to total seconds."""
    parts = ts.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return float(ts)  # Assume raw seconds
```

### Pattern 3: FILE Type Mapping

**What:** Map audio file extensions to CUE FILE command type keywords.
**When to use:** For the FILE command at the top of the CUE sheet.

Per the CUE specification (CDRWIN format):
| Extension | CUE Type | Notes |
|-----------|----------|-------|
| .mp3 | MP3 | Standard |
| .wav | WAVE | Standard |
| .aiff, .aif | AIFF | Standard |
| .flac | WAVE | Convention: lossless formats use WAVE |
| .ogg | WAVE | Non-standard but widely accepted |
| .m4a | WAVE | Non-standard but commonly used |
| .opus | WAVE | Non-standard but commonly used |

```python
_FILE_TYPE_MAP: dict[str, str] = {
    "mp3": "MP3",
    "wav": "WAVE",
    "wave": "WAVE",
    "aiff": "AIFF",
    "aif": "AIFF",
    "flac": "WAVE",
    "ogg": "WAVE",
    "m4a": "WAVE",
    "opus": "WAVE",
}
```

### Pattern 4: CUE File Output Format

**What:** The exact CUE file structure to generate.
**When to use:** Reference for the string builder.

```
REM COMMENT "Generated by Phaze"
FILE "artist - event (2024).mp3" MP3
  TRACK 01 AUDIO
    REM GENRE "House"
    REM LABEL "Defected Records"
    REM YEAR "2023"
    TITLE "Track Title"
    PERFORMER "Track Artist"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Another Track"
    PERFORMER "Another Artist"
    INDEX 01 05:32:45
```

Key formatting rules:
- FILE command at top level (no indentation)
- TRACK, REM, TITLE, PERFORMER, INDEX indented with 2 spaces
- Track numbers are 2-digit zero-padded (01-99)
- String values in double quotes
- REM comments appear after TRACK, before TITLE/PERFORMER/INDEX
- First track typically starts at INDEX 01 00:00:00

### Pattern 5: Version Suffix Naming

**What:** Scan filesystem for existing .cue files to determine next version number.
**When to use:** When writing CUE file to determine filename.

```python
from pathlib import Path
import re

def next_cue_path(audio_path: Path) -> Path:
    """Determine the next CUE file path with version suffix if needed."""
    base = audio_path.stem
    parent = audio_path.parent
    base_cue = parent / f"{base}.cue"

    if not base_cue.exists():
        return base_cue

    # Scan for existing versions
    pattern = re.compile(rf"^{re.escape(base)}\.v(\d+)\.cue$")
    max_version = 1  # base_cue counts as v1
    for f in parent.iterdir():
        m = pattern.match(f.name)
        if m:
            max_version = max(max_version, int(m.group(1)))

    return parent / f"{base}.v{max_version + 1}.cue"
```

### Pattern 6: UTF-8 with BOM Writing

**What:** Write CUE content with UTF-8 BOM prefix.
**When to use:** All CUE file writes (D-13).

```python
def write_with_bom(path: Path, content: str) -> None:
    """Write string content as UTF-8 with BOM."""
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(content)
```

Python's `utf-8-sig` encoding automatically prepends the BOM (EF BB BF bytes) on write and strips it on read.

### Anti-Patterns to Avoid

- **Storing CUE content in the database:** CUE files are generated artifacts derived from existing data. No need to store them -- regeneration is instant.
- **Using SAQ tasks for generation:** D-07 explicitly says synchronous. CUE generation is pure string formatting + one file write -- under 1ms.
- **Overwriting existing CUE files:** D-12 requires version suffixes. Never overwrite.
- **Including full file paths in FILE command:** D-11 says filename only. CUE and audio are co-located.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| UTF-8 BOM encoding | Manual byte prefix | `encoding="utf-8-sig"` | Python stdlib handles BOM correctly |
| Filesystem path manipulation | String concatenation | `pathlib.Path` | Cross-platform, safe |

**Key insight:** CUE sheet generation itself IS the correct thing to hand-roll. The format is a trivial text format (a dozen string concatenations). No library is warranted.

## Common Pitfalls

### Pitfall 1: Frame Arithmetic Off-by-One
**What goes wrong:** Using `round()` instead of `int()` for frame calculation, or forgetting frames are 0-74 not 0-75.
**Why it happens:** Confusion between "75 frames per second" and frame index range.
**How to avoid:** Use `int(total_seconds * 75)` for total frames, then `total_frames % 75` for the frame component. Frames range 00-74.
**Warning signs:** Frame values of 75 appearing in output (impossible in valid CUE).

### Pitfall 2: Timestamp Format Ambiguity
**What goes wrong:** Treating a "3:45" timestamp as "3 minutes 45 seconds" when it might be "3 hours 45 minutes" from a long DJ set.
**Why it happens:** 1001tracklists timestamps for long sets can exceed 60 minutes.
**How to avoid:** Parse strictly: if 3 parts, treat as HH:MM:SS. If 2 parts, treat as MM:SS. If 1 part, treat as raw seconds. Document the convention.
**Warning signs:** Track timestamps exceeding the audio file duration.

### Pitfall 3: Missing Destination Path
**What goes wrong:** Trying to write a CUE file when the tracklist's linked file hasn't been executed (no destination path).
**Why it happens:** CUE generation requires `FileRecord.current_path` to be the destination (state=EXECUTED), but the tracklist might be linked to a file still in an earlier pipeline state.
**How to avoid:** Guard: check `file_record.state == FileState.EXECUTED` before allowing CUE generation. Return a clear error message for files not yet at destination.
**Warning signs:** CUE files appearing in source directories instead of destination directories.

### Pitfall 4: Special Characters in Filenames
**What goes wrong:** CUE FILE command has unescaped quotes in the filename.
**Why it happens:** Audio filenames may contain double quotes or other special characters.
**How to avoid:** Escape or strip double quotes from filenames when embedding in CUE FILE command. The CUE spec wraps filenames in double quotes, so internal quotes must be removed.
**Warning signs:** CUE files that fail to parse in media players.

### Pitfall 5: Tracklist Without Linked File
**What goes wrong:** Attempting CUE generation for an approved tracklist that has no linked file (file_id is NULL).
**Why it happens:** Tracklists can be approved without being linked to a file.
**How to avoid:** Check both `tracklist.status == 'approved'` AND `tracklist.file_id is not None` AND file state is EXECUTED.
**Warning signs:** NoneType errors when accessing file record properties.

## Code Examples

### Complete CUE Generation Flow

```python
# Verified pattern from existing tag_writer.py (synchronous service + async orchestrator)

# 1. Service: Pure CUE content generation (synchronous, testable)
def generate_cue_content(
    audio_filename: str,
    file_type: str,
    tracks: list[CueTrackData],
) -> str:
    lines: list[str] = []
    lines.append('REM COMMENT "Generated by Phaze"')
    cue_type = _FILE_TYPE_MAP.get(file_type.lower(), "WAVE")
    lines.append(f'FILE "{audio_filename}" {cue_type}')

    for i, track in enumerate(tracks, start=1):
        lines.append(f"  TRACK {i:02d} AUDIO")
        # REM comments from DiscogsLink (D-08, D-09)
        if track.genre:
            lines.append(f'    REM GENRE "{track.genre}"')
        if track.label:
            lines.append(f'    REM LABEL "{track.label}"')
        if track.year:
            lines.append(f'    REM YEAR "{track.year}"')
        if track.title:
            lines.append(f'    TITLE "{track.title}"')
        if track.artist:
            lines.append(f'    PERFORMER "{track.artist}"')
        lines.append(f"    INDEX 01 {track.cue_timestamp}")

    return "\n".join(lines) + "\n"
```

### Router Endpoint Pattern (from tags.py)

```python
# Synchronous generation, HTMX response with OOB toast
@router.post("/{tracklist_id}/generate", response_class=HTMLResponse)
async def generate_cue(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    # Load tracklist + file + tracks + discogs links
    # Validate: approved, has file, file is EXECUTED
    # Generate CUE content
    # Write to filesystem
    # Return updated card partial + toast
    ...
```

### HTMX Inline Button Pattern (from tracklist_card.html)

```html
<!-- Add alongside existing action buttons in tracklist_card.html -->
{% if tracklist.status == 'approved' and tracklist.file_id %}
<button hx-post="/cue/{{ tracklist.id }}/generate"
        hx-target="#tracklist-{{ tracklist.id }}"
        hx-swap="outerHTML"
        class="text-xs bg-green-600 hover:bg-green-700 text-white font-semibold px-3 py-1.5 rounded-md">
    Generate CUE
</button>
{% endif %}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| CUE sheets as CD-burning artifacts | CUE sheets as chapter markers for digital audio | ~2010+ | Modern players (foobar2000, VLC, Kodi) support CUE for track splitting within single-file albums |
| ANSI/ASCII encoding | UTF-8 with BOM | Widespread adoption ~2015+ | Required for non-Latin characters in artist/title fields |

**Deprecated/outdated:**
- CDRWIN-specific extensions (CDTEXTFILE, etc.) -- not needed for audio-only CUE files
- Binary/Motorola FILE types -- only relevant for disc image CUE files, not audio

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_services/test_cue_generator.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CUE-01 | Generate CUE from tracklist with fingerprint timestamp priority | unit | `uv run pytest tests/test_services/test_cue_generator.py::TestCueGeneration -x` | Wave 0 |
| CUE-01 | Fallback to 1001tracklists timestamp when no fingerprint | unit | `uv run pytest tests/test_services/test_cue_generator.py::TestTimestampPriority -x` | Wave 0 |
| CUE-01 | Omit tracks without any timestamp | unit | `uv run pytest tests/test_services/test_cue_generator.py::TestTrackOmission -x` | Wave 0 |
| CUE-02 | 75fps frame conversion correctness | unit | `uv run pytest tests/test_services/test_cue_generator.py::TestTimestampConversion -x` | Wave 0 |
| CUE-02 | UTF-8 BOM encoding in written file | unit | `uv run pytest tests/test_services/test_cue_generator.py::TestFileWriting -x` | Wave 0 |
| CUE-03 | REM comments from accepted DiscogsLink | unit | `uv run pytest tests/test_services/test_cue_generator.py::TestDiscogsRem -x` | Wave 0 |
| CUE-03 | No REM for tracks without accepted DiscogsLink | unit | `uv run pytest tests/test_services/test_cue_generator.py::TestDiscogsRem -x` | Wave 0 |
| CUE-01 | Generate CUE via /cue/{id}/generate endpoint | integration | `uv run pytest tests/test_routers/test_cue.py -x` | Wave 0 |
| CUE-01 | CUE management page lists tracklists with status | integration | `uv run pytest tests/test_routers/test_cue.py::TestCueListPage -x` | Wave 0 |
| D-12 | Version suffix naming on re-generation | unit | `uv run pytest tests/test_services/test_cue_generator.py::TestVersionSuffix -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_services/test_cue_generator.py tests/test_routers/test_cue.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/test_cue_generator.py` -- covers CUE-01, CUE-02, CUE-03, D-12
- [ ] `tests/test_routers/test_cue.py` -- covers CUE management page and generate endpoint

No framework install needed -- pytest + pytest-asyncio already configured.

## Sources

### Primary (HIGH confidence)
- [CUE sheet Wikipedia](https://en.wikipedia.org/wiki/Cue_sheet_(computing)) -- format overview, MM:SS:FF at 75fps, command syntax
- [Hydrogenaudio CUE wiki](https://wiki.hydrogenaudio.org/index.php?title=Cue_sheet) -- FILE type keywords: BINARY, MOTOROLA, AIFF, WAVE, MP3
- [FileFormat.com CUE](https://docs.fileformat.com/disc-and-media/cue/) -- command syntax reference
- Existing codebase: `tag_writer.py`, `tags.py`, `tracklist_card.html` -- service, router, and template patterns

### Secondary (MEDIUM confidence)
- WebSearch for FILE type keyword completeness -- verified against Hydrogenaudio wiki (authoritative source)
- Convention that non-standard formats (FLAC, OGG, M4A) use WAVE keyword -- widely accepted but not in original CDRWIN spec

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- zero new dependencies, all existing project libs
- Architecture: HIGH -- follows established patterns from Phase 20 (tag writing)
- CUE format spec: HIGH -- stable format, verified against multiple authoritative sources
- Timestamp conversion: HIGH -- straightforward arithmetic, well-defined spec
- Pitfalls: HIGH -- based on analysis of actual data model and existing code

**Research date:** 2026-04-03
**Valid until:** Indefinite -- CUE sheet format is frozen, project patterns are established
