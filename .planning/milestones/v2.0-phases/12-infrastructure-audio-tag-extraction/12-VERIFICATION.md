---
phase: 12-infrastructure-audio-tag-extraction
verified: 2026-03-31T07:30:00Z
status: passed
score: 10/10 must-haves verified
re_verification: false
---

# Phase 12: Infrastructure & Audio Tag Extraction Verification Report

**Phase Goal:** Every music file has its audio tags extracted and stored in PostgreSQL, with richer metadata feeding all downstream features
**Verified:** 2026-03-31T07:30:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| #  | Truth | Status | Evidence |
|----|-------|--------|----------|
| 1  | Worker tasks use shared async engine pool from ctx dict instead of creating one per invocation | VERIFIED | `worker.py` creates `task_engine` in `startup()` with `pool_size=10, max_overflow=5`; all 3 existing task functions use `ctx["async_session"]()` |
| 2  | FileMetadata model has track_number, duration, and bitrate columns | VERIFIED | `models/metadata.py` lines 24-26: `track_number: Mapped[int | None]`, `duration: Mapped[float | None]`, `bitrate: Mapped[int | None]` |
| 3  | Pipeline dashboard includes METADATA_EXTRACTED state in PIPELINE_STAGES | VERIFIED | `services/pipeline.py` lines 17-24: `FileState.METADATA_EXTRACTED` is second entry in `PIPELINE_STAGES` list |
| 4  | mutagen reads ID3/Vorbis/MP4/FLAC/OPUS tags from music and video files | VERIFIED | `services/metadata.py` dispatches on `isinstance(audio.tags, ID3)` / `isinstance(audio, MP4)` / else Vorbis; handles all formats |
| 5  | Extracted tags populate FileMetadata with artist, title, album, year, genre, track_number, duration, bitrate | VERIFIED | `tasks/metadata_extraction.py` lines 59-67: all 9 fields assigned from `ExtractedTags` to `FileMetadata` row |
| 6  | Full raw tag dump stored in FileMetadata.raw_tags JSONB column | VERIFIED | `services/metadata.py` `_serialize_tags()` serializes all non-binary tags; assigned to `metadata.raw_tags` in task |
| 7  | Files with no tags get an empty FileMetadata row with all null fields and empty raw_tags dict | VERIFIED | `extract_tags()` returns `ExtractedTags()` with all None fields when `audio.tags is None`; task creates row unconditionally |
| 8  | User can trigger tag extraction via manual API endpoint | VERIFIED | `routers/pipeline.py` line 218: `POST /api/v1/extract-metadata` queries all music/video files and enqueues jobs |
| 9  | Tag extraction jobs enqueued automatically for new files during scan | VERIFIED | `services/ingestion.py` line 177: `arq_pool.enqueue_job("extract_file_metadata", ...)` after `bulk_upsert_files`; `routers/scan.py` line 49-50 passes `arq_pool` to `run_scan` |
| 10 | LLM proposal context includes extracted tag data | VERIFIED | `services/proposal.py` `build_file_context()` returns `"tags": tags_dict` with artist, title, album, year, genre, raw_tags; `tasks/proposal.py` queries `FileMetadata` for each file and passes it |

**Score:** 10/10 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/tasks/worker.py` | Shared engine creation in startup, disposal in shutdown | VERIFIED | `create_async_engine` in `startup()`, `task_engine.dispose()` in `shutdown()` |
| `src/phaze/tasks/session.py` | Deprecated module, no `get_task_session` | VERIFIED | Module contains only docstring explaining migration; no function definitions |
| `src/phaze/models/metadata.py` | FileMetadata with track_number, duration, bitrate | VERIFIED | All 3 columns present with correct SQLAlchemy types (Integer, Float, Integer) |
| `alembic/versions/005_add_metadata_columns.py` | Migration adding 3 columns to metadata table | VERIFIED | `op.add_column("metadata", ...)` called 3 times for track_number, duration, bitrate |
| `src/phaze/services/pipeline.py` | PIPELINE_STAGES with METADATA_EXTRACTED | VERIFIED | `FileState.METADATA_EXTRACTED` in stages list between DISCOVERED and ANALYZED |
| `src/phaze/services/metadata.py` | `extract_tags()` pure function using mutagen | VERIFIED | 243 lines; `ExtractedTags` dataclass, `extract_tags()`, `_VORBIS_MAP`, `_ID3_MAP`, `_MP4_MAP`, all helpers |
| `src/phaze/tasks/metadata_extraction.py` | `extract_file_metadata` arq task function | VERIFIED | Full arq task: fetches file, skips companions, calls `extract_tags()`, upserts `FileMetadata`, transitions state |
| `src/phaze/services/ingestion.py` | `run_scan` auto-enqueues `extract_file_metadata` for new files | VERIFIED | `arq_pool` parameter accepted; enqueue loop after `bulk_upsert_files` filters music/video files |
| `tests/test_services/test_metadata.py` | Unit tests for tag extraction across formats | VERIFIED | 383 lines, 27 test functions covering ID3/Vorbis/MP4, helpers, edge cases |
| `tests/test_tasks/test_metadata_extraction.py` | Unit tests for arq task function | VERIFIED | 173 lines, 6 test functions including auto-enqueue test |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `tasks/worker.py` | `tasks/functions.py` | `ctx["async_session"]` sessionmaker | WIRED | `functions.py` line 28: `async with ctx["async_session"]() as session:` |
| `tasks/worker.py` | shutdown hook | `await engine.dispose()` | WIRED | `worker.py` line 63-64: `task_engine = ctx.get("task_engine"); await task_engine.dispose()` |
| `tasks/metadata_extraction.py` | `services/metadata.py` | `from phaze.services.metadata import extract_tags` | WIRED | `metadata_extraction.py` line 15: import confirmed; `extract_tags()` called at line 48 |
| `tasks/metadata_extraction.py` | `ctx["async_session"]` | shared engine pool from Plan 01 | WIRED | `metadata_extraction.py` line 32: `async with ctx["async_session"]() as session:` |
| `tasks/worker.py` | `tasks/metadata_extraction.py` | registered in WorkerSettings.functions | WIRED | `worker.py` line 73: `[process_file, generate_proposals, execute_approved_batch, extract_file_metadata]` |
| `services/ingestion.py` | arq pool | `enqueue_job("extract_file_metadata", file_id)` | WIRED | `ingestion.py` line 177: confirmed by grep; `scan.py` lines 49-50 pass `arq_pool` |
| `services/proposal.py` | `models/metadata.py` | FileMetadata type hint in `build_file_context` | WIRED | `proposal.py` TYPE_CHECKING import; `metadata: FileMetadata | None = None` parameter used |
| `tasks/proposal.py` | `models/metadata.py` | queries FileMetadata for each file | WIRED | `proposal.py` line 54: `select(FileMetadata).where(FileMetadata.file_id == uid)` |
| `routers/pipeline.py` | convergence gate query | `exists()` subqueries for both FileMetadata and AnalysisResult | WIRED | `pipeline.py` lines 93-94: `exists(select(FileMetadata.id)...)` and `exists(select(AnalysisResult.id)...)` both present in trigger_proposals and trigger_proposals_ui |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `tasks/metadata_extraction.py` | `tags` (ExtractedTags) | `extract_tags(file_record.current_path)` via mutagen | Yes — reads actual file headers | FLOWING |
| `tasks/metadata_extraction.py` | `metadata` (FileMetadata) | upsert from `tags.*` fields | Yes — all 9 fields assigned | FLOWING |
| `services/proposal.py` build_file_context | `tags_dict` | `metadata.artist/title/album/year/genre/raw_tags` | Yes — 6 of 9 fields forwarded | FLOWING (see note) |

**Note on tags_dict field coverage:** `build_file_context` includes 6 fields (artist, title, album, year, genre, raw_tags) but omits track_number, duration, and bitrate from the LLM context. The FileMetadata model stores all 9 fields. The Plan 03 Summary documents this as a deliberate adaptation — the 3 omitted fields are less relevant to filename proposals. TAGS-02 and TAGS-04 require storing these fields in FileMetadata (satisfied), while TAGS-05 requires "tag data" in LLM context (satisfied by 6 fields). This is an implementation choice, not a functional gap, since raw_tags also contains all tag data.

### Behavioral Spot-Checks

Step 7b: SKIPPED (no runnable entry points — requires live PostgreSQL and Redis which are not available in this context).

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| INFRA-01 | 12-01 | Task session uses a shared async engine pool instead of creating a new engine per invocation | SATISFIED | `worker.py` startup creates shared engine, all 3 task functions migrated to `ctx["async_session"]()` pattern; `get_task_session` removed from codebase |
| INFRA-02 | 12-01, 12-03 | FileRecord state machine expanded with METADATA_EXTRACTED and FINGERPRINTED states, all consumers updated | SATISFIED | `models/file.py` has both `METADATA_EXTRACTED` and `FINGERPRINTED` in `FileState`; `PIPELINE_STAGES` includes `METADATA_EXTRACTED`; convergence gate in pipeline router handles both states |
| TAGS-01 | 12-02 | User can trigger tag extraction that reads ID3/Vorbis/MP4/FLAC/OPUS tags from all music files | SATISFIED | `POST /api/v1/extract-metadata` enqueues jobs for all music/video files; `extract_tags()` handles all 5 format families |
| TAGS-02 | 12-02 | Extracted tags populate FileMetadata with artist, title, album, year, genre, track number | SATISFIED | `metadata_extraction.py` assigns all 6 named fields plus track_number, duration, bitrate from `ExtractedTags` to `FileMetadata` |
| TAGS-03 | 12-02 | Full raw tag dump stored in FileMetadata.raw_tags JSONB column | SATISFIED | `_serialize_tags()` produces JSON-safe dict; assigned to `metadata.raw_tags`; binary values (APIC frames) excluded |
| TAGS-04 | 12-02 | Duration and bitrate extracted from audio file info and stored in FileMetadata | SATISFIED | `extract_tags()` reads `audio.info.length` and `audio.info.bitrate`; task assigns `tags.duration` and `tags.bitrate` to metadata row |
| TAGS-05 | 12-03 | LLM proposal context includes extracted tag data for richer filename/path proposals | SATISFIED | `build_file_context()` includes `"tags"` key with artist/title/album/year/genre/raw_tags; `generate_proposals` queries and passes `FileMetadata` |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| None detected | — | — | — | — |

Scanned modified files for TODO/FIXME/placeholder patterns, empty implementations, and hardcoded empty data. No anti-patterns found.

**Specific checks:**
- `get_task_session` grep across all `src/` returns no matches — full removal confirmed
- `session.py` contains only a deprecation docstring, no active code
- `metadata_extraction.py` has no `return null` or stub patterns; all code paths commit real data
- `services/metadata.py` has no hardcoded stubs — handles real mutagen objects with graceful fallback to empty `ExtractedTags` (correct behavior, not a stub)

### Human Verification Required

#### 1. Tag Extraction Against Real Audio Files

**Test:** Run `extract_tags()` against a real MP3, OGG/FLAC, and M4A file from the music collection.
**Expected:** Returns `ExtractedTags` with correct artist/title/album/year populated; `raw_tags` contains the actual tag key/value dump; duration and bitrate match file properties.
**Why human:** Tests use mocked mutagen objects; real-file behavior depends on actual tag encoding, encoding edge cases, and file system access.

#### 2. End-to-End Scan Auto-Enqueue

**Test:** Trigger a scan via `POST /api/v1/scan` on a directory containing MP3 files; observe Redis queue.
**Expected:** After scan completes, `extract_file_metadata` jobs appear in Redis for each music/video file discovered; FileMetadata rows are created after workers process them; file states transition to `METADATA_EXTRACTED`.
**Why human:** Requires live PostgreSQL + Redis + running arq worker; not testable with static grep analysis.

#### 3. Convergence Gate Behavior

**Test:** With a file in `ANALYZED` state (AnalysisResult exists, FileMetadata does NOT), trigger `POST /api/v1/proposals/generate`. Then add a FileMetadata row for that file and re-trigger.
**Expected:** First trigger returns 0 enqueued files. Second trigger returns 1+ files enqueued.
**Why human:** Requires live database with actual row state manipulation across two tables.

#### 4. LLM Context Tag Data in Proposals

**Test:** With a file that has both AnalysisResult and FileMetadata populated, trigger proposal generation and inspect `context_used` on the resulting `RenameProposal`.
**Expected:** `context_used.input_context.tags` contains artist/title/album/year/genre/raw_tags from the file's metadata.
**Why human:** Requires live LLM call or mock with actual DB state; verifies the 6-field tags_dict reaches the stored proposal context.

## Gaps Summary

No blocking gaps found. All 7 requirement IDs are satisfied. All 10 observable truths are verified with implementation evidence. The one design deviation noted (tags_dict omitting track_number, duration, bitrate from the LLM context) is documented in the Plan 03 Summary as a deliberate choice — the requirement text ("includes extracted tag data") is met by the 6 included fields. These 3 fields are stored in FileMetadata (TAGS-02 and TAGS-04 satisfied) and accessible via raw_tags if the LLM needs them. No corrective action required.

---

_Verified: 2026-03-31T07:30:00Z_
_Verifier: Claude (gsd-verifier)_
