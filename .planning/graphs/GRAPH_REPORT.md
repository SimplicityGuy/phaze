# Graph Report - /Users/Robert/Code/public/phaze  (2026-04-17)

## Corpus Check
- 141 files · ~84,976 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2387 nodes · 7413 edges · 52 communities detected
- Extraction: 38% EXTRACTED · 62% INFERRED · 0% AMBIGUOUS · INFERRED: 4591 edges (avg confidence: 0.55)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]

## God Nodes (most connected - your core abstractions)
1. `FileRecord` - 527 edges
2. `FileState` - 525 edges
3. `TracklistTrack` - 261 edges
4. `Tracklist` - 258 edges
5. `TracklistVersion` - 230 edges
6. `DiscogsLink` - 211 edges
7. `FileMetadata` - 200 edges
8. `AnalysisResult` - 136 edges
9. `RenameProposal` - 117 edges
10. `ProposalStatus` - 116 edges

## Surprising Connections (you probably didn't know these)
- `Create a minimal SAQ context dict with async_session factory and orchestrator.` --uses--> `FileState`  [INFERRED]
  /Users/Robert/Code/public/phaze/tests/test_tasks/test_fingerprint.py → /Users/Robert/Code/public/phaze/src/phaze/models/file.py
- `Create a mock FileRecord.` --uses--> `FileState`  [INFERRED]
  /Users/Robert/Code/public/phaze/tests/test_tasks/test_fingerprint.py → /Users/Robert/Code/public/phaze/src/phaze/models/file.py
- `Create a mock IngestResult.` --uses--> `FileState`  [INFERRED]
  /Users/Robert/Code/public/phaze/tests/test_tasks/test_fingerprint.py → /Users/Robert/Code/public/phaze/src/phaze/models/file.py
- `fingerprint_file with both engines succeeding transitions file to FINGERPRINTED.` --uses--> `FileState`  [INFERRED]
  /Users/Robert/Code/public/phaze/tests/test_tasks/test_fingerprint.py → /Users/Robert/Code/public/phaze/src/phaze/models/file.py
- `fingerprint_file with one engine failing does NOT transition to FINGERPRINTED.` --uses--> `FileState`  [INFERRED]
  /Users/Robert/Code/public/phaze/tests/test_tasks/test_fingerprint.py → /Users/Robert/Code/public/phaze/src/phaze/models/file.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.02
Nodes (383): AnalysisResult, Base, CUE sheet management UI router -- generation, batch generation, and CUE manageme, Check filesystem for existing CUE files and return the version number.      Retu, Build CueTrackData list from a tracklist version's tracks + Discogs links., Load tracklist joined with file record., Render the CUE management page or HTMX partial., Generate a CUE file for a specific tracklist. (+375 more)

### Community 1 - "Community 1"
Cohesion: 0.02
Nodes (239): build_tree(), _count_files(), detect_collisions(), get_collision_ids(), Collision detection and directory tree builder for approved proposals., Node in a directory tree of approved proposals., Find approved proposals that would collide at the same destination.      Returns, Return set of string UUIDs for proposals that participate in collisions.      Us (+231 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (188): Audio analysis service: model registry, essentia analysis, mood/style derivation, Audio analysis results for a file (1:1 with files)., Base, DeclarativeBase with naming conventions and timestamp mixin., Base class for all SQLAlchemy models with naming conventions., Mixin providing created_at and updated_at timestamp columns., TimestampMixin, async_engine() (+180 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (128): BaseSettings, Pydantic settings configuration for Phaze., Application settings loaded from environment variables and .env file., Settings, create_app(), Create and configure the FastAPI application., BatchProposalResponse, build_file_context() (+120 more)

### Community 4 - "Community 4"
Cohesion: 0.04
Nodes (86): AudfprintAdapter, CombinedMatch, FingerprintEngine, FingerprintOrchestrator, FingerprintResult, get_fingerprint_progress(), IngestResult, PanakoAdapter (+78 more)

### Community 5 - "Community 5"
Cohesion: 0.03
Nodes (114): get_summary_counts(), Unified search UI router -- serves the cross-entity search page., Render the search page, or an HTMX results fragment., search_page(), create_test_discogs_link(), create_test_file(), create_test_tracklist(), test_discogs_artist_filter() (+106 more)

### Community 6 - "Community 6"
Cohesion: 0.04
Nodes (59): _build_cue_tracks(), generate_batch(), generate_cue(), CueTrackData, generate_cue_content(), next_cue_path(), parse_timestamp_string(), CUE sheet generation service.  Generates CUE sheet content from tracklist data w (+51 more)

### Community 7 - "Community 7"
Cohesion: 0.06
Nodes (82): associate_companions(), AssociateResponse, DuplicateFile, DuplicateGroup, DuplicateGroupsResponse, list_duplicates(), Companion association service: links companion files to media files in the same, A single file within a duplicate group. (+74 more)

### Community 8 - "Community 8"
Cohesion: 0.05
Nodes (63): extract_tags(), ExtractedTags, extract_file_metadata(), _first_str(), _parse_track(), _parse_year(), Serialize all tags to a JSON-safe dict.      Skips binary values (cover art / AP, Extract audio tags from a file using mutagen.      Returns an ExtractedTags data (+55 more)

### Community 9 - "Community 9"
Cohesion: 0.06
Nodes (64): _make_file(), _make_tracklist(), _make_version_with_tracks(), test_accept_discogs_link(), test_accept_discogs_link_not_found(), test_approve_tracklist(), test_approve_tracklist_has_candidates(), test_approve_tracklist_no_candidates_no_bulk_button() (+56 more)

### Community 10 - "Community 10"
Cohesion: 0.06
Nodes (43): match_tracklist_to_discogs(), compute_discogs_confidence(), DiscogsographyClient, match_track_to_discogs(), _parse_artist_from_name(), Discogsography API adapter and fuzzy matching for Discogs release linking., HTTP client adapter for the discogsography service.      Follows the same patter, Search Discogs releases via discogsography /api/search endpoint.          Return (+35 more)

### Community 11 - "Community 11"
Cohesion: 0.07
Nodes (31): compute_proposed_tags(), parse_filename(), _build_comparison(), compare_tags(), _count_changes(), _determine_file_status(), edit_tag_field(), _get_accepted_discogs_link() (+23 more)

### Community 12 - "Community 12"
Cohesion: 0.06
Nodes (40): execute_tag_write(), verify_write(), _write_id3(), _write_mp4(), write_tags(), _write_vorbis(), _make_mp3(), mp3_file() (+32 more)

### Community 13 - "Community 13"
Cohesion: 0.07
Nodes (53): analyze_file(), derive_mood(), derive_style(), _get_classifier(), _get_labels(), _make_standard_set(), ModelConfig, ModelSetConfig (+45 more)

### Community 14 - "Community 14"
Cohesion: 0.06
Nodes (48): _ensure_database(), health(), HealthResponse, ingest(), IngestRequest, IngestResponse, _parse_matches(), query() (+40 more)

### Community 15 - "Community 15"
Cohesion: 0.09
Nodes (44): count_duplicate_groups(), find_duplicate_groups(), find_duplicate_groups_with_metadata(), get_duplicate_stats(), resolve_group(), score_group(), tag_completeness(), undo_resolve() (+36 more)

### Community 16 - "Community 16"
Cohesion: 0.07
Nodes (40): process_file(), create_process_pool(), ProcessPoolExecutor lifecycle and helper for CPU-bound work., Create a ProcessPoolExecutor sized from settings (D-04)., Run a CPU-bound function in the worker's process pool.      Uses asyncio.get_run, run_in_process_pool(), _make_ctx(), _make_file_record() (+32 more)

### Community 17 - "Community 17"
Cohesion: 0.11
Nodes (25): dashboard(), _enqueue_analysis_jobs(), _enqueue_extraction_jobs(), _enqueue_fingerprint_jobs(), _enqueue_proposal_jobs(), get_files_by_state(), get_pipeline_stats(), pipeline_stats_partial() (+17 more)

### Community 18 - "Community 18"
Cohesion: 0.08
Nodes (14): Tests for DiscogsLink model., DiscogsLink can be created with all required fields., status column server_default is 'candidate'., DiscogsLink has correct __tablename__., DiscogsLink has index on track_id., DiscogsLink has index on status., DiscogsLink has index on discogs_release_id., Optional fields (discogs_artist, discogs_title, discogs_label, discogs_year) acc (+6 more)

### Community 19 - "Community 19"
Cohesion: 0.17
Nodes (21): _create_approved_tracklist_with_file(), test_batch_generate_with_write_failure_continues(), test_cue_list_empty_state(), test_cue_list_fingerprint_first(), test_cue_list_full_page(), test_cue_list_htmx_partial(), test_cue_list_pagination(), test_cue_list_shows_generated_count_after_generation() (+13 more)

### Community 20 - "Community 20"
Cohesion: 0.19
Nodes (17): fingerprint_file(), _make_ctx(), _make_file_record(), _make_ingest_result(), fingerprint_file with non-existent file_id returns not_found., fingerprint_file is idempotent -- running twice updates existing FingerprintResu, Create a minimal SAQ context dict with async_session factory and orchestrator., fingerprint_file propagates exceptions (SAQ handles retry with backoff). (+9 more)

### Community 21 - "Community 21"
Cohesion: 0.2
Nodes (18): create_searchable_discogs_link(), create_searchable_file(), create_searchable_tracklist(), test_search_artist_filter(), test_search_bpm_filter(), test_search_discogs_purple_pill(), test_search_file_state_filter(), test_search_filter_panel_collapsed() (+10 more)

### Community 22 - "Community 22"
Cohesion: 0.12
Nodes (16): lifespan(), FastAPI application factory with lifespan management., Manage application lifespan: verify DB, create SAQ queue on startup; dispose on, Phase 4 gap-filling tests: SAQ queue lifespan and docker-compose worker command., Worker startup fails fast if models directory has no .pb files., Worker startup succeeds when models directory has .pb files., docker-compose.yml worker service command is 'uv run saq phaze.tasks.worker.sett, FastAPI lifespan creates a SAQ queue on app.state during startup. (+8 more)

### Community 23 - "Community 23"
Cohesion: 0.4
Nodes (14): scan_live_set(), _make_ctx(), _make_file_record(), _make_metadata(), test_scan_live_set_creates_tracklist_with_fingerprint_source(), test_scan_live_set_creates_version_with_number_1(), test_scan_live_set_external_id_format(), test_scan_live_set_invalid_track_id_skipped() (+6 more)

### Community 24 - "Community 24"
Cohesion: 0.26
Nodes (13): _create_executed_file(), test_compare_tags(), test_inline_edit_invalid_field(), test_inline_edit_returns_input(), test_inline_edit_save(), test_list_tags_empty_state(), test_list_tags_full_page(), test_list_tags_htmx_partial() (+5 more)

### Community 25 - "Community 25"
Cohesion: 0.29
Nodes (12): _make_file(), _make_metadata(), test_bulk_resolve(), test_bulk_undo(), test_compare_endpoint(), test_empty_state(), test_list_duplicates_htmx_returns_partial(), test_list_duplicates_returns_html() (+4 more)

### Community 26 - "Community 26"
Cohesion: 0.2
Nodes (9): Tests for SAQ worker settings configuration., settings["concurrency"] equals app_settings.worker_max_jobs., settings["startup"] is the startup function., settings["shutdown"] is the shutdown function., settings["functions"] contains process_file., test_worker_concurrency_matches_settings(), test_worker_functions_contains_process_file(), test_worker_shutdown_is_shutdown() (+1 more)

### Community 27 - "Community 27"
Cohesion: 0.25
Nodes (7): Tests for the shared task session pattern (INFRA-01)., Verify startup hook signature expects to populate ctx with async_session., Verify shutdown hook signature accepts ctx for engine disposal., session.py no longer exports get_task_session., test_session_module_deprecated(), test_worker_shutdown_disposes_engine(), test_worker_startup_creates_engine_in_ctx()

### Community 28 - "Community 28"
Cohesion: 0.33
Nodes (5): downgrade(), Add search_vector GENERATED columns and GIN indexes for full-text search.  Revis, Add tsvector columns, GIN indexes, and pg_trgm trigram indexes., Drop all search indexes, search_vector columns, and pg_trgm extension., upgrade()

### Community 29 - "Community 29"
Cohesion: 0.33
Nodes (5): downgrade(), Add tag_write_log table for tag write audit trail.  Revision ID: 011 Revises: 01, Create tag_write_log table with indexes., Drop tag_write_log table., upgrade()

### Community 30 - "Community 30"
Cohesion: 0.33
Nodes (5): downgrade(), Add file_companions join table.  Revision ID: 003 Revises: 002 Create Date: 2026, Create file_companions table with FKs, unique constraint, and indexes., Drop indexes and file_companions table., upgrade()

### Community 31 - "Community 31"
Cohesion: 0.33
Nodes (5): downgrade(), Initial schema - all 5 tables.  Revision ID: 001 Revises: Create Date: 2026-03-2, Drop all 5 tables in reverse dependency order., Create all 5 tables: files, metadata, analysis, proposals, execution_log., upgrade()

### Community 32 - "Community 32"
Cohesion: 0.33
Nodes (5): downgrade(), Add discogs_links table for Discogs release candidate matching.  Revision ID: 01, Create discogs_links table with indexes including GIN FTS index., Drop discogs_links table., upgrade()

### Community 33 - "Community 33"
Cohesion: 0.33
Nodes (5): downgrade(), Add indexes to execution_log table.  Revision ID: 004 Revises: 003 Create Date:, Add indexes to execution_log table (table already created in migration 001)., Drop indexes from execution_log table., upgrade()

### Community 34 - "Community 34"
Cohesion: 0.33
Nodes (5): downgrade(), Add track_number, duration, bitrate columns to metadata table.  Revision ID: 005, Add track_number, duration, bitrate columns to metadata table., Remove track_number, duration, bitrate columns from metadata table., upgrade()

### Community 35 - "Community 35"
Cohesion: 0.33
Nodes (5): downgrade(), Add fingerprint_results table.  Revision ID: 007 Revises: 006 Create Date: 2026-, Create fingerprint_results table., Drop fingerprint_results table., upgrade()

### Community 36 - "Community 36"
Cohesion: 0.33
Nodes (5): downgrade(), Add scan_batches table and unique path index.  Revision ID: 002 Revises: 001 Cre, Create scan_batches table, add unique index on files.original_path, add FK from, Drop FK, unique index, and scan_batches table., upgrade()

### Community 37 - "Community 37"
Cohesion: 0.33
Nodes (5): downgrade(), Add tracklists, tracklist_versions, and tracklist_tracks tables.  Revision ID: 0, Create tracklists, tracklist_versions, and tracklist_tracks tables., Drop tracklist_tracks, tracklist_versions, and tracklists tables., upgrade()

### Community 38 - "Community 38"
Cohesion: 0.33
Nodes (5): downgrade(), Add source, status columns to tracklists and confidence to tracklist_tracks.  Re, Add source and status to tracklists, confidence to tracklist_tracks., Remove source, status, confidence columns and indexes., upgrade()

### Community 39 - "Community 39"
Cohesion: 0.5
Nodes (3): Tests for the health check endpoint., Health endpoint should return 200 with status ok., test_health_endpoint_returns_ok()

### Community 40 - "Community 40"
Cohesion: 0.5
Nodes (3): get_session(), Async SQLAlchemy engine and session factory., Yield an async database session.

### Community 41 - "Community 41"
Cohesion: 0.5
Nodes (3): health_check(), Health check endpoint., Check API and database connectivity.

### Community 42 - "Community 42"
Cohesion: 1.0
Nodes (1): Task session module -- DEPRECATED.  Task functions now use the shared engine poo

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (0):

### Community 44 - "Community 44"
Cohesion: 1.0
Nodes (0):

### Community 45 - "Community 45"
Cohesion: 1.0
Nodes (0):

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (0):

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (0):

### Community 48 - "Community 48"
Cohesion: 1.0
Nodes (0):

### Community 49 - "Community 49"
Cohesion: 1.0
Nodes (0):

### Community 50 - "Community 50"
Cohesion: 1.0
Nodes (0):

### Community 51 - "Community 51"
Cohesion: 1.0
Nodes (0):

## Knowledge Gaps
- **208 isolated node(s):** `Phase 4 gap-filling tests: SAQ queue lifespan and docker-compose worker command.`, `FastAPI lifespan creates a SAQ queue on app.state during startup.`, `FastAPI lifespan disconnects the SAQ queue when the application shuts down.`, `Worker startup fails fast if models directory does not exist.`, `Worker startup fails fast if models directory has no .pb files.` (+203 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 42`** (2 nodes): `Task session module -- DEPRECATED.  Task functions now use the shared engine poo`, `session.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 44`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `FileRecord` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 7`, `Community 9`, `Community 14`, `Community 15`, `Community 17`, `Community 19`, `Community 21`, `Community 24`, `Community 25`?**
  _High betweenness centrality (0.284) - this node is a cross-community bridge._
- **Why does `FileState` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 7`, `Community 8`, `Community 12`, `Community 17`, `Community 20`?**
  _High betweenness centrality (0.256) - this node is a cross-community bridge._
- **Why does `TracklistTrack` connect `Community 0` to `Community 1`, `Community 2`, `Community 5`, `Community 9`, `Community 10`, `Community 14`, `Community 19`, `Community 21`, `Community 23`?**
  _High betweenness centrality (0.073) - this node is a cross-community bridge._
- **Are the 523 inferred relationships involving `FileRecord` (e.g. with `Integration tests for the preview route -- directory tree preview page.` and `Create an approved proposal with its associated file record.`) actually correct?**
  _`FileRecord` has 523 INFERRED edges - model-reasoned connections that need verification._
- **Are the 523 inferred relationships involving `FileState` (e.g. with `Tests for fingerprint service layer: Protocol, adapters, orchestrator, progress.` and `Create a minimal SAQ context dict with async_session factory and orchestrator.`) actually correct?**
  _`FileState` has 523 INFERRED edges - model-reasoned connections that need verification._
- **Are the 257 inferred relationships involving `TracklistTrack` (e.g. with `Tests for the scan API endpoints.` and `Create a minimal SAQ context dict with async_session factory and orchestrator.`) actually correct?**
  _`TracklistTrack` has 257 INFERRED edges - model-reasoned connections that need verification._
- **Are the 254 inferred relationships involving `Tracklist` (e.g. with `Tests for the scan API endpoints.` and `Create a minimal SAQ context dict with async_session factory and orchestrator.`) actually correct?**
  _`Tracklist` has 254 INFERRED edges - model-reasoned connections that need verification._
