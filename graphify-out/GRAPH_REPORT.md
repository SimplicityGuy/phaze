# Graph Report - .  (2026-04-28)

## Corpus Check
- 195 files · ~88,605 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2574 nodes · 7649 edges · 70 communities detected
- Extraction: 39% EXTRACTED · 61% INFERRED · 0% AMBIGUOUS · INFERRED: 4677 edges (avg confidence: 0.56)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_CUE Sheet Management|CUE Sheet Management]]
- [[_COMMUNITY_Approvals & Directory Tree|Approvals & Directory Tree]]
- [[_COMMUNITY_Settings & Fingerprint Adapters|Settings & Fingerprint Adapters]]
- [[_COMMUNITY_Models & Test Fixtures|Models & Test Fixtures]]
- [[_COMMUNITY_File Discovery & Ingestion|File Discovery & Ingestion]]
- [[_COMMUNITY_CUE Generation Service|CUE Generation Service]]
- [[_COMMUNITY_Proposal Generation (LLM)|Proposal Generation (LLM)]]
- [[_COMMUNITY_Companion & Duplicate Files|Companion & Duplicate Files]]
- [[_COMMUNITY_Tag Extraction (Mutagen)|Tag Extraction (Mutagen)]]
- [[_COMMUNITY_Tracklist Matcher Tests|Tracklist Matcher Tests]]
- [[_COMMUNITY_Discogs Approval Tests|Discogs Approval Tests]]
- [[_COMMUNITY_Unified Search UI|Unified Search UI]]
- [[_COMMUNITY_API Endpoint Catalog|API Endpoint Catalog]]
- [[_COMMUNITY_Discogs Client & Matching|Discogs Client & Matching]]
- [[_COMMUNITY_Tag Comparison & Edits|Tag Comparison & Edits]]
- [[_COMMUNITY_Audio Analysis (Essentia)|Audio Analysis (Essentia)]]
- [[_COMMUNITY_Duplicate Resolution|Duplicate Resolution]]
- [[_COMMUNITY_Audfprint Service|Audfprint Service]]
- [[_COMMUNITY_Graph Report Meta-Nodes|Graph Report Meta-Nodes]]
- [[_COMMUNITY_Brand Design System|Brand Design System]]
- [[_COMMUNITY_Scan API|Scan API]]
- [[_COMMUNITY_SAQ Task Processing|SAQ Task Processing]]
- [[_COMMUNITY_DiscogsLink Model Tests|DiscogsLink Model Tests]]
- [[_COMMUNITY_CUE List UI Tests|CUE List UI Tests]]
- [[_COMMUNITY_Dashboard & Enqueue Tests|Dashboard & Enqueue Tests]]
- [[_COMMUNITY_Favicon & Brand Assets|Favicon & Brand Assets]]
- [[_COMMUNITY_Fingerprint File Tests|Fingerprint File Tests]]
- [[_COMMUNITY_Search Filter Tests|Search Filter Tests]]
- [[_COMMUNITY_Duplicate Resolve Tests|Duplicate Resolve Tests]]
- [[_COMMUNITY_SAQ Worker Settings|SAQ Worker Settings]]
- [[_COMMUNITY_Design Tokens (ColorType)|Design Tokens (Color/Type)]]
- [[_COMMUNITY_Shared Task Session Pattern|Shared Task Session Pattern]]
- [[_COMMUNITY_CICD Workflow|CI/CD Workflow]]
- [[_COMMUNITY_Migration 009 (Search Vectors)|Migration 009 (Search Vectors)]]
- [[_COMMUNITY_Migration 011 (Tag Write Log)|Migration 011 (Tag Write Log)]]
- [[_COMMUNITY_Migration 003 (Companions)|Migration 003 (Companions)]]
- [[_COMMUNITY_Migration 001 (Initial Schema)|Migration 001 (Initial Schema)]]
- [[_COMMUNITY_Migration 010 (Discogs Links)|Migration 010 (Discogs Links)]]
- [[_COMMUNITY_Migration 004 (Execution Log)|Migration 004 (Execution Log)]]
- [[_COMMUNITY_Migration 005 (Metadata Columns)|Migration 005 (Metadata Columns)]]
- [[_COMMUNITY_Migration 007 (Fingerprint Results)|Migration 007 (Fingerprint Results)]]
- [[_COMMUNITY_Migration 002 (Scan Batches)|Migration 002 (Scan Batches)]]
- [[_COMMUNITY_Migration 006 (Tracklist Tables)|Migration 006 (Tracklist Tables)]]
- [[_COMMUNITY_Migration 008 (Tracklist Status)|Migration 008 (Tracklist Status)]]
- [[_COMMUNITY_Tech Stack Concepts|Tech Stack Concepts]]
- [[_COMMUNITY_Health Endpoint Tests|Health Endpoint Tests]]
- [[_COMMUNITY_Database Session Factory|Database Session Factory]]
- [[_COMMUNITY_Health Check Endpoint|Health Check Endpoint]]
- [[_COMMUNITY_Dev Setup & Tooling|Dev Setup & Tooling]]
- [[_COMMUNITY_Deprecated Task Session|Deprecated Task Session]]
- [[_COMMUNITY_Brand Voice & Tone|Brand Voice & Tone]]
- [[_COMMUNITY_Tracklists API|Tracklists API]]
- [[_COMMUNITY_Package Init 1|Package Init 1]]
- [[_COMMUNITY_Package Init 2|Package Init 2]]
- [[_COMMUNITY_Package Init 3|Package Init 3]]
- [[_COMMUNITY_Package Init 4|Package Init 4]]
- [[_COMMUNITY_Package Init 5|Package Init 5]]
- [[_COMMUNITY_Package Init 6|Package Init 6]]
- [[_COMMUNITY_Package Init 7|Package Init 7]]
- [[_COMMUNITY_Package Init 8|Package Init 8]]
- [[_COMMUNITY_Package Init 9|Package Init 9]]
- [[_COMMUNITY_External References|External References]]
- [[_COMMUNITY_GSD Workflow|GSD Workflow]]
- [[_COMMUNITY_Health Endpoint Group|Health Endpoint Group]]
- [[_COMMUNITY_Tags Endpoint Group|Tags Endpoint Group]]
- [[_COMMUNITY_CUE Endpoint Group|CUE Endpoint Group]]
- [[_COMMUNITY_Search Endpoint Group|Search Endpoint Group]]
- [[_COMMUNITY_Companion Endpoint Group|Companion Endpoint Group]]
- [[_COMMUNITY_Logo Set Hyperedge|Logo Set Hyperedge]]
- [[_COMMUNITY_Banner Set Hyperedge|Banner Set Hyperedge]]

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
- `design/assets/favicon-512.png (Phaze brand favicon @512px, master raster)` --semantically_similar_to--> `src/phaze/static/favicon-512.png (runtime favicon @512px, master raster)`  [INFERRED] [semantically similar]
  design/assets/favicon-512.png → src/phaze/static/favicon-512.png
- `Technology Stack summary` --conceptually_related_to--> `Core Technologies stack table`  [INFERRED]
  README.md → CLAUDE.md
- `LLM settings` --references--> `litellm unified LLM client`  [INFERRED]
  docs/configuration.md → CLAUDE.md
- `Create a minimal SAQ context dict with async_session factory and orchestrator.` --uses--> `FileState`  [INFERRED]
  /Users/Robert/Code/public/phaze/tests/test_tasks/test_fingerprint.py → /Users/Robert/Code/public/phaze/src/phaze/models/file.py
- `Create a mock FileRecord.` --uses--> `FileState`  [INFERRED]
  /Users/Robert/Code/public/phaze/tests/test_tasks/test_fingerprint.py → /Users/Robert/Code/public/phaze/src/phaze/models/file.py

## Hyperedges (group relationships)
- **File processing pipeline (metadata->analysis->proposal->execution)** —  [EXTRACTED 1.00]
- **Dual-engine fingerprinting subsystem** —  [EXTRACTED 1.00]
- **Phaze tracklist domain model triad (Tracklist, TracklistVersion, TracklistTrack)** —  [INFERRED 0.85]

## Communities

### Community 0 - "CUE Sheet Management"
Cohesion: 0.02
Nodes (345): AnalysisResult, Base, CUE sheet management UI router -- generation, batch generation, and CUE manageme, Check filesystem for existing CUE files and return the version number.      Retu, Build CueTrackData list from a tracklist version's tracks + Discogs links., Load tracklist joined with file record., Render the CUE management page or HTMX partial., Generate a CUE file for a specific tracklist. (+337 more)

### Community 1 - "Approvals & Directory Tree"
Cohesion: 0.02
Nodes (248): Mixin providing created_at and updated_at timestamp columns., TimestampMixin, build_tree(), _count_files(), detect_collisions(), get_collision_ids(), Collision detection and directory tree builder for approved proposals., Node in a directory tree of approved proposals. (+240 more)

### Community 2 - "Settings & Fingerprint Adapters"
Cohesion: 0.02
Nodes (164): BaseSettings, Pydantic settings configuration for Phaze., Application settings loaded from environment variables and .env file., Settings, AudfprintAdapter, CombinedMatch, FingerprintEngine, FingerprintOrchestrator (+156 more)

### Community 3 - "Models & Test Fixtures"
Cohesion: 0.02
Nodes (135): Audio analysis results for a file (1:1 with files)., Base, DeclarativeBase with naming conventions and timestamp mixin., Base class for all SQLAlchemy models with naming conventions., async_engine(), client(), pytest_collection_modifyitems(), Shared test fixtures for Phaze test suite. (+127 more)

### Community 4 - "File Discovery & Ingestion"
Cohesion: 0.03
Nodes (146): FileCategory, Constants for file discovery and ingestion., Categories for classifying discovered files., bulk_upsert_files(), classify_file(), discover_and_hash_files(), normalize_path(), Ingestion service: directory scanning, hashing, classification, and bulk upsert. (+138 more)

### Community 5 - "CUE Generation Service"
Cohesion: 0.04
Nodes (59): _build_cue_tracks(), generate_batch(), generate_cue(), CueTrackData, generate_cue_content(), next_cue_path(), parse_timestamp_string(), CUE sheet generation service.  Generates CUE sheet content from tracklist data w (+51 more)

### Community 6 - "Proposal Generation (LLM)"
Cohesion: 0.05
Nodes (80): BaseModel, BatchProposalResponse, build_file_context(), check_rate_limit(), _clamp_confidence(), clean_companion_content(), FileProposalResponse, generate_proposals() (+72 more)

### Community 7 - "Companion & Duplicate Files"
Cohesion: 0.06
Nodes (84): associate_companions(), AssociateResponse, DuplicateFile, DuplicateGroup, DuplicateGroupsResponse, list_duplicates(), Companion association service: links companion files to media files in the same, A single file within a duplicate group. (+76 more)

### Community 8 - "Tag Extraction (Mutagen)"
Cohesion: 0.04
Nodes (63): extract_tags(), ExtractedTags, extract_file_metadata(), _first_str(), _parse_track(), _parse_year(), Tag extraction service using mutagen for audio metadata., Serialize all tags to a JSON-safe dict.      Skips binary values (cover art / AP (+55 more)

### Community 9 - "Tracklist Matcher Tests"
Cohesion: 0.05
Nodes (50): _make_ctx(), _make_file_record(), _make_scraped_tracklist(), _make_search_result(), Tests for TracklistMatcher service -- pure logic, no mocking needed., Tests for parse_live_set_filename()., Tests for compute_match_confidence()., Tests for auto-link threshold and should_auto_link(). (+42 more)

### Community 10 - "Discogs Approval Tests"
Cohesion: 0.06
Nodes (64): _make_file(), _make_tracklist(), _make_version_with_tracks(), test_accept_discogs_link(), test_accept_discogs_link_not_found(), test_approve_tracklist(), test_approve_tracklist_has_candidates(), test_approve_tracklist_no_candidates_no_bulk_button() (+56 more)

### Community 11 - "Unified Search UI"
Cohesion: 0.07
Nodes (56): get_summary_counts(), Unified search UI router -- serves the cross-entity search page., Render the search page, or an HTMX results fragment., search_page(), create_test_discogs_link(), create_test_file(), create_test_tracklist(), test_discogs_artist_filter() (+48 more)

### Community 12 - "API Endpoint Catalog"
Cohesion: 0.04
Nodes (65): Duplicates endpoints, Execution endpoints (SSE progress), Pipeline endpoints, Preview endpoint (directory tree), Proposals endpoints, Scan API (/api/v1/scan), audfprint API endpoints, audfprint architecture notes (+57 more)

### Community 13 - "Discogs Client & Matching"
Cohesion: 0.06
Nodes (43): match_tracklist_to_discogs(), compute_discogs_confidence(), DiscogsographyClient, match_track_to_discogs(), _parse_artist_from_name(), Discogsography API adapter and fuzzy matching for Discogs release linking., HTTP client adapter for the discogsography service.      Follows the same patter, Search Discogs releases via discogsography /api/search endpoint.          Return (+35 more)

### Community 14 - "Tag Comparison & Edits"
Cohesion: 0.07
Nodes (31): compute_proposed_tags(), parse_filename(), _build_comparison(), compare_tags(), _count_changes(), _determine_file_status(), edit_tag_field(), _get_accepted_discogs_link() (+23 more)

### Community 15 - "Audio Analysis (Essentia)"
Cohesion: 0.07
Nodes (54): analyze_file(), derive_mood(), derive_style(), _get_classifier(), _get_labels(), _make_standard_set(), ModelConfig, ModelSetConfig (+46 more)

### Community 16 - "Duplicate Resolution"
Cohesion: 0.09
Nodes (44): count_duplicate_groups(), find_duplicate_groups(), find_duplicate_groups_with_metadata(), get_duplicate_stats(), resolve_group(), score_group(), tag_completeness(), undo_resolve() (+36 more)

### Community 17 - "Audfprint Service"
Cohesion: 0.08
Nodes (36): _ensure_database(), health(), HealthResponse, ingest(), IngestRequest, IngestResponse, _parse_matches(), query() (+28 more)

### Community 18 - "Graph Report Meta-Nodes"
Cohesion: 0.08
Nodes (34): AnalysisResult (136 edges), audfprint_service (fingerprint engine), Community 0 (391 nodes, CUE sheet management), Community 1 (408 nodes, AnalysisResult/Base models), Community 15 (30 nodes, design system showcase), Community 2 (238 nodes, collision detection), Community 21 (20 nodes, favicons design assets), Community 3 (127 nodes, Settings/Fingerprint) (+26 more)

### Community 19 - "Brand Design System"
Cohesion: 0.13
Nodes (30): Banner dark PNG render (horizontal lockup, black bg, cyan mark), Banner light PNG render (horizontal lockup, off-white bg, teal mark), Background dark #0a0c12, Background light #eef0f5 / #f8f9fc, Primary accent cyan #1abbdb (dark mode), Primary accent teal #008caf (light mode), Design system showcase: Phi Wave Final Mark, palette, typography, usage rules, Favicon 128 (rounded dark square, phi-wave mark) (+22 more)

### Community 20 - "Scan API"
Cohesion: 0.17
Nodes (25): get_scan_status(), Pydantic schemas for scan API endpoints., Request body for triggering a file scan., Response returned after starting a scan., Response for scan status queries., Trigger a file discovery scan.      Accepts an optional path override; defaults, Get the status of a scan batch by its ID., scan_live_set() (+17 more)

### Community 21 - "SAQ Task Processing"
Cohesion: 0.15
Nodes (24): process_file(), _make_ctx(), _make_file_record(), _make_session_factory(), Tests for task functions., process_file upserts AnalysisResult with bpm, mood, style, musical_key, features, process_file updates FileRecord.state to FileState.ANALYZED after successful ana, process_file raises exception when analyze_file fails (SAQ handles retry). (+16 more)

### Community 22 - "DiscogsLink Model Tests"
Cohesion: 0.08
Nodes (14): Tests for DiscogsLink model., DiscogsLink can be created with all required fields., status column server_default is 'candidate'., DiscogsLink has correct __tablename__., DiscogsLink has index on track_id., DiscogsLink has index on status., DiscogsLink has index on discogs_release_id., Optional fields (discogs_artist, discogs_title, discogs_label, discogs_year) acc (+6 more)

### Community 23 - "CUE List UI Tests"
Cohesion: 0.17
Nodes (21): _create_approved_tracklist_with_file(), test_batch_generate_with_write_failure_continues(), test_cue_list_empty_state(), test_cue_list_fingerprint_first(), test_cue_list_full_page(), test_cue_list_htmx_partial(), test_cue_list_pagination(), test_cue_list_shows_generated_count_after_generation() (+13 more)

### Community 24 - "Dashboard & Enqueue Tests"
Cohesion: 0.13
Nodes (21): _make_file(), _make_file_with_convergence(), test_analyze_enqueues_discovered(), test_analyze_no_files(), test_dashboard_includes_settings_batch_size(), test_dashboard_page(), test_enqueue_analysis_background(), test_enqueue_proposals_background() (+13 more)

### Community 25 - "Favicon & Brand Assets"
Cohesion: 0.11
Nodes (20): design/assets/favicon-128.png (Phaze brand favicon @128px), design/assets/favicon-16.png (Phaze brand favicon @16px), design/assets/favicon-256.png (Phaze brand favicon @256px), design/assets/favicon-32.png (Phaze brand favicon @32px), design/assets/favicon-48.png (Phaze brand favicon @48px), design/assets/favicon-512.png (Phaze brand favicon @512px, master raster), design/assets/favicon-64.png (Phaze brand favicon @64px), src/phaze/static/apple-touch-icon.png (iOS home-screen icon, phaze brand mark) (+12 more)

### Community 26 - "Fingerprint File Tests"
Cohesion: 0.19
Nodes (18): fingerprint_file(), Fingerprint a single file through all registered engines.      Per D-17: Both en, _make_ctx(), _make_file_record(), _make_ingest_result(), fingerprint_file with non-existent file_id returns not_found., fingerprint_file is idempotent -- running twice updates existing FingerprintResu, Create a minimal SAQ context dict with async_session factory and orchestrator. (+10 more)

### Community 27 - "Search Filter Tests"
Cohesion: 0.2
Nodes (18): create_searchable_discogs_link(), create_searchable_file(), create_searchable_tracklist(), test_search_artist_filter(), test_search_bpm_filter(), test_search_discogs_purple_pill(), test_search_file_state_filter(), test_search_filter_panel_collapsed() (+10 more)

### Community 28 - "Duplicate Resolve Tests"
Cohesion: 0.29
Nodes (12): _make_file(), _make_metadata(), test_bulk_resolve(), test_bulk_undo(), test_compare_endpoint(), test_empty_state(), test_list_duplicates_htmx_returns_partial(), test_list_duplicates_returns_html() (+4 more)

### Community 29 - "SAQ Worker Settings"
Cohesion: 0.2
Nodes (9): Tests for SAQ worker settings configuration., settings["concurrency"] equals app_settings.worker_max_jobs., settings["startup"] is the startup function., settings["shutdown"] is the shutdown function., settings["functions"] contains process_file., test_worker_concurrency_matches_settings(), test_worker_functions_contains_process_file(), test_worker_shutdown_is_shutdown() (+1 more)

### Community 30 - "Design Tokens (Color/Type)"
Cohesion: 0.22
Nodes (9): Color System (tokens, surfaces, status), Component Patterns (badges, buttons, cards, inputs), Border radius scale, Spacing scale (4px base unit), Typography (Jura, Inter, mono), Color-as-timbre philosophy, Golden ratio and musical intervals composition, Interference pattern form logic (+1 more)

### Community 31 - "Shared Task Session Pattern"
Cohesion: 0.25
Nodes (7): Tests for the shared task session pattern (INFRA-01)., Verify startup hook signature expects to populate ctx with async_session., Verify shutdown hook signature accepts ctx for engine disposal., session.py no longer exports get_task_session., test_session_module_deprecated(), test_worker_shutdown_disposes_engine(), test_worker_startup_creates_engine_in_ctx()

### Community 32 - "CI/CD Workflow"
Cohesion: 0.29
Nodes (7): CI pattern (reusable workflows, discogsography-style), Testing (85% coverage, Codecov), Workflow: worktree per feature, PR per feature, discogsography reference project, Discogs settings, GitHub workflows (ci, code-quality, tests, security), Acknowledgment of discogsography reference

### Community 33 - "Migration 009 (Search Vectors)"
Cohesion: 0.33
Nodes (5): downgrade(), Add search_vector GENERATED columns and GIN indexes for full-text search.  Revis, Add tsvector columns, GIN indexes, and pg_trgm trigram indexes., Drop all search indexes, search_vector columns, and pg_trgm extension., upgrade()

### Community 34 - "Migration 011 (Tag Write Log)"
Cohesion: 0.33
Nodes (5): downgrade(), Add tag_write_log table for tag write audit trail.  Revision ID: 011 Revises: 01, Create tag_write_log table with indexes., Drop tag_write_log table., upgrade()

### Community 35 - "Migration 003 (Companions)"
Cohesion: 0.33
Nodes (5): downgrade(), Add file_companions join table.  Revision ID: 003 Revises: 002 Create Date: 2026, Create file_companions table with FKs, unique constraint, and indexes., Drop indexes and file_companions table., upgrade()

### Community 36 - "Migration 001 (Initial Schema)"
Cohesion: 0.33
Nodes (5): downgrade(), Initial schema - all 5 tables.  Revision ID: 001 Revises: Create Date: 2026-03-2, Drop all 5 tables in reverse dependency order., Create all 5 tables: files, metadata, analysis, proposals, execution_log., upgrade()

### Community 37 - "Migration 010 (Discogs Links)"
Cohesion: 0.33
Nodes (5): downgrade(), Add discogs_links table for Discogs release candidate matching.  Revision ID: 01, Create discogs_links table with indexes including GIN FTS index., Drop discogs_links table., upgrade()

### Community 38 - "Migration 004 (Execution Log)"
Cohesion: 0.33
Nodes (5): downgrade(), Add indexes to execution_log table.  Revision ID: 004 Revises: 003 Create Date:, Add indexes to execution_log table (table already created in migration 001)., Drop indexes from execution_log table., upgrade()

### Community 39 - "Migration 005 (Metadata Columns)"
Cohesion: 0.33
Nodes (5): downgrade(), Add track_number, duration, bitrate columns to metadata table.  Revision ID: 005, Add track_number, duration, bitrate columns to metadata table., Remove track_number, duration, bitrate columns from metadata table., upgrade()

### Community 40 - "Migration 007 (Fingerprint Results)"
Cohesion: 0.33
Nodes (5): downgrade(), Add fingerprint_results table.  Revision ID: 007 Revises: 006 Create Date: 2026-, Create fingerprint_results table., Drop fingerprint_results table., upgrade()

### Community 41 - "Migration 002 (Scan Batches)"
Cohesion: 0.33
Nodes (5): downgrade(), Add scan_batches table and unique path index.  Revision ID: 002 Revises: 001 Cre, Create scan_batches table, add unique index on files.original_path, add FK from, Drop FK, unique index, and scan_batches table., upgrade()

### Community 42 - "Migration 006 (Tracklist Tables)"
Cohesion: 0.33
Nodes (5): downgrade(), Add tracklists, tracklist_versions, and tracklist_tracks tables.  Revision ID: 0, Create tracklists, tracklist_versions, and tracklist_tracks tables., Drop tracklist_tracks, tracklist_versions, and tracklists tables., upgrade()

### Community 43 - "Migration 008 (Tracklist Status)"
Cohesion: 0.33
Nodes (5): downgrade(), Add source, status columns to tracklists and confidence to tracklist_tracks.  Re, Add source and status to tracklists, confidence to tracklist_tracks., Remove source, status, confidence columns and indexes., upgrade()

### Community 44 - "Tech Stack Concepts"
Cohesion: 0.4
Nodes (5): concept_essentia (audio analysis), concept_litellm (LLM unified client), concept_mutagen (audio metadata), Hyperedge: File processing pipeline (metadata->analysis->proposal->execution), Hyperedge: Phaze recommended technology stack

### Community 45 - "Health Endpoint Tests"
Cohesion: 0.5
Nodes (3): Tests for the health check endpoint., Health endpoint should return 200 with status ok., test_health_endpoint_returns_ok()

### Community 46 - "Database Session Factory"
Cohesion: 0.5
Nodes (3): get_session(), Async SQLAlchemy engine and session factory., Yield an async database session.

### Community 47 - "Health Check Endpoint"
Cohesion: 0.5
Nodes (3): health_check(), Health check endpoint., Check API and database connectivity.

### Community 48 - "Dev Setup & Tooling"
Cohesion: 0.5
Nodes (4): Development setup (Python 3.13, uv, pre-commit), Mypy strict configuration, Pre-commit hooks (frozen SHAs), Ruff configuration

### Community 49 - "Deprecated Task Session"
Cohesion: 1.0
Nodes (1): Task session module -- DEPRECATED.  Task functions now use the shared engine poo

### Community 50 - "Brand Voice & Tone"
Cohesion: 1.0
Nodes (2): Voice & Tone, Resonant Precision design movement

### Community 51 - "Tracklists API"
Cohesion: 1.0
Nodes (2): Tracklists endpoints, 1001Tracklists integration

### Community 52 - "Package Init 1"
Cohesion: 1.0
Nodes (0):

### Community 53 - "Package Init 2"
Cohesion: 1.0
Nodes (0):

### Community 54 - "Package Init 3"
Cohesion: 1.0
Nodes (0):

### Community 55 - "Package Init 4"
Cohesion: 1.0
Nodes (0):

### Community 56 - "Package Init 5"
Cohesion: 1.0
Nodes (0):

### Community 57 - "Package Init 6"
Cohesion: 1.0
Nodes (0):

### Community 58 - "Package Init 7"
Cohesion: 1.0
Nodes (0):

### Community 59 - "Package Init 8"
Cohesion: 1.0
Nodes (0):

### Community 60 - "Package Init 9"
Cohesion: 1.0
Nodes (0):

### Community 61 - "External References"
Cohesion: 1.0
Nodes (1): Sources (external references)

### Community 62 - "GSD Workflow"
Cohesion: 1.0
Nodes (1): GSD Workflow Enforcement

### Community 63 - "Health Endpoint Group"
Cohesion: 1.0
Nodes (1): Health endpoint

### Community 64 - "Tags Endpoint Group"
Cohesion: 1.0
Nodes (1): Tags endpoints

### Community 65 - "CUE Endpoint Group"
Cohesion: 1.0
Nodes (1): CUE sheets endpoints

### Community 66 - "Search Endpoint Group"
Cohesion: 1.0
Nodes (1): Search endpoint

### Community 67 - "Companion Endpoint Group"
Cohesion: 1.0
Nodes (1): Companion files endpoints

### Community 68 - "Logo Set Hyperedge"
Cohesion: 1.0
Nodes (1): Hyperedge: Logo set (icon and square lockups)

### Community 69 - "Banner Set Hyperedge"
Cohesion: 1.0
Nodes (1): Hyperedge: Banner set (1200x400 PNG renders)

## Knowledge Gaps
- **272 isolated node(s):** `Phase 4 gap-filling tests: SAQ queue lifespan and docker-compose worker command.`, `FastAPI lifespan creates a SAQ queue on app.state during startup.`, `FastAPI lifespan disconnects the SAQ queue when the application shuts down.`, `Worker startup fails fast if models directory does not exist.`, `Worker startup fails fast if models directory has no .pb files.` (+267 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Deprecated Task Session`** (2 nodes): `Task session module -- DEPRECATED.  Task functions now use the shared engine poo`, `session.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Brand Voice & Tone`** (2 nodes): `Voice & Tone`, `Resonant Precision design movement`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Tracklists API`** (2 nodes): `Tracklists endpoints`, `1001Tracklists integration`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init 1`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init 2`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init 3`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init 4`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init 5`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init 6`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init 7`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init 8`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init 9`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `External References`** (1 nodes): `Sources (external references)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `GSD Workflow`** (1 nodes): `GSD Workflow Enforcement`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Health Endpoint Group`** (1 nodes): `Health endpoint`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Tags Endpoint Group`** (1 nodes): `Tags endpoints`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `CUE Endpoint Group`** (1 nodes): `CUE sheets endpoints`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Search Endpoint Group`** (1 nodes): `Search endpoint`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Companion Endpoint Group`** (1 nodes): `Companion files endpoints`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Logo Set Hyperedge`** (1 nodes): `Hyperedge: Logo set (icon and square lockups)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Banner Set Hyperedge`** (1 nodes): `Hyperedge: Banner set (1200x400 PNG renders)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `FileRecord` connect `CUE Sheet Management` to `Approvals & Directory Tree`, `Settings & Fingerprint Adapters`, `Models & Test Fixtures`, `File Discovery & Ingestion`, `Proposal Generation (LLM)`, `Companion & Duplicate Files`, `Discogs Approval Tests`, `Unified Search UI`, `Duplicate Resolution`, `Scan API`, `CUE List UI Tests`, `Dashboard & Enqueue Tests`, `Fingerprint File Tests`, `Search Filter Tests`, `Duplicate Resolve Tests`?**
  _High betweenness centrality (0.232) - this node is a cross-community bridge._
- **Why does `FileState` connect `CUE Sheet Management` to `Approvals & Directory Tree`, `Settings & Fingerprint Adapters`, `Models & Test Fixtures`, `File Discovery & Ingestion`, `Proposal Generation (LLM)`, `Companion & Duplicate Files`, `Tag Extraction (Mutagen)`, `Fingerprint File Tests`?**
  _High betweenness centrality (0.210) - this node is a cross-community bridge._
- **Why does `TracklistTrack` connect `CUE Sheet Management` to `Approvals & Directory Tree`, `Models & Test Fixtures`, `File Discovery & Ingestion`, `Tracklist Matcher Tests`, `Discogs Approval Tests`, `Unified Search UI`, `Discogs Client & Matching`, `Scan API`, `CUE List UI Tests`, `Search Filter Tests`?**
  _High betweenness centrality (0.054) - this node is a cross-community bridge._
- **Are the 523 inferred relationships involving `FileRecord` (e.g. with `Tests for execution service - copy-verify-delete with audit logging.` and `GET /audit/ returns 200 with HTML containing Audit Log heading.`) actually correct?**
  _`FileRecord` has 523 INFERRED edges - model-reasoned connections that need verification._
- **Are the 523 inferred relationships involving `FileState` (e.g. with `Tests for fingerprint service layer: Protocol, adapters, orchestrator, progress.` and `Create a minimal SAQ context dict with async_session factory and orchestrator.`) actually correct?**
  _`FileState` has 523 INFERRED edges - model-reasoned connections that need verification._
- **Are the 257 inferred relationships involving `TracklistTrack` (e.g. with `Tests for Tracklist, TracklistVersion, and TracklistTrack models.` and `Tests for the scan API endpoints.`) actually correct?**
  _`TracklistTrack` has 257 INFERRED edges - model-reasoned connections that need verification._
- **Are the 254 inferred relationships involving `Tracklist` (e.g. with `Tests for Tracklist, TracklistVersion, and TracklistTrack models.` and `Tests for the scan API endpoints.`) actually correct?**
  _`Tracklist` has 254 INFERRED edges - model-reasoned connections that need verification._
