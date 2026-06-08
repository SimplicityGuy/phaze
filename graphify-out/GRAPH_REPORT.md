# Graph Report - src + design  (2026-06-07)

## Corpus Check
- 259 files · ~92,502 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1418 nodes · 3662 edges · 51 communities detected
- Extraction: 49% EXTRACTED · 51% INFERRED · 0% AMBIGUOUS · INFERRED: 1873 edges (avg confidence: 0.58)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Distributed Agent Data Layer|Distributed Agent Data Layer]]
- [[_COMMUNITY_Tracklist & Search Frontend|Tracklist & Search Frontend]]
- [[_COMMUNITY_Agent API Client|Agent API Client]]
- [[_COMMUNITY_Admin Agents UI|Admin Agents UI]]
- [[_COMMUNITY_CUE Sheet Generation|CUE Sheet Generation]]
- [[_COMMUNITY_Proposal & Duplicate Review UI|Proposal & Duplicate Review UI]]
- [[_COMMUNITY_Duplicate Detection|Duplicate Detection]]
- [[_COMMUNITY_Discogs Matching & Controller|Discogs Matching & Controller]]
- [[_COMMUNITY_Execution Audit Log|Execution Audit Log]]
- [[_COMMUNITY_Collision & Tree Builder|Collision & Tree Builder]]
- [[_COMMUNITY_Tag Proposal & Comparison|Tag Proposal & Comparison]]
- [[_COMMUNITY_File Watcher Debouncer|File Watcher Debouncer]]
- [[_COMMUNITY_Agent Task Routing|Agent Task Routing]]
- [[_COMMUNITY_Brand Asset Renders|Brand Asset Renders]]
- [[_COMMUNITY_Tag Extraction & Writing|Tag Extraction & Writing]]
- [[_COMMUNITY_Settings & Config|Settings & Config]]
- [[_COMMUNITY_Fingerprint & Process Jobs|Fingerprint & Process Jobs]]
- [[_COMMUNITY_Essentia Audio Analysis|Essentia Audio Analysis]]
- [[_COMMUNITY_Agent Auth & Registration|Agent Auth & Registration]]
- [[_COMMUNITY_Favicon Assets|Favicon Assets]]
- [[_COMMUNITY_TLS Cert Bootstrap|TLS Cert Bootstrap]]
- [[_COMMUNITY_File Execution (copy-verify-delete)|File Execution (copy-verify-delete)]]
- [[_COMMUNITY_Exec Batch Progress|Exec Batch Progress]]
- [[_COMMUNITY_Scan API Schemas|Scan API Schemas]]
- [[_COMMUNITY_Model Weight Download|Model Weight Download]]
- [[_COMMUNITY_Design System Tokens|Design System Tokens]]
- [[_COMMUNITY_Naming Format Rules|Naming Format Rules]]
- [[_COMMUNITY_Agent Heartbeat|Agent Heartbeat]]
- [[_COMMUNITY_Health Check|Health Check]]
- [[_COMMUNITY_Time Humanizer|Time Humanizer]]
- [[_COMMUNITY_Analysis Write Endpoint|Analysis Write Endpoint]]
- [[_COMMUNITY_Agent Watcher Docs|Agent Watcher Docs]]
- [[_COMMUNITY_Brand Voice & Tone|Brand Voice & Tone]]
- [[_COMMUNITY__FILE Secret Resolution|_FILE Secret Resolution]]
- [[_COMMUNITY_Sidecar Locality Rule|Sidecar Locality Rule]]
- [[_COMMUNITY_Scan Roots Parsing|Scan Roots Parsing]]
- [[_COMMUNITY_HTTPS Enforcement Rule|HTTPS Enforcement Rule]]
- [[_COMMUNITY_Redis Password Rule|Redis Password Rule]]
- [[_COMMUNITY_Package Init (alembic)|Package Init (alembic)]]
- [[_COMMUNITY_Package Init (services)|Package Init (services)]]
- [[_COMMUNITY_Moved-State Validation|Moved-State Validation]]
- [[_COMMUNITY_Failed-Step Invariant|Failed-Step Invariant]]
- [[_COMMUNITY_Package Init (schemas)|Package Init (schemas)]]
- [[_COMMUNITY_Package Init (routers)|Package Init (routers)]]
- [[_COMMUNITY_Confidence Clamp|Confidence Clamp]]
- [[_COMMUNITY_Pagination Total Pages|Pagination: Total Pages]]
- [[_COMMUNITY_Pagination Has Prev|Pagination: Has Prev]]
- [[_COMMUNITY_Pagination Has Next|Pagination: Has Next]]
- [[_COMMUNITY_Pagination First Index|Pagination: First Index]]
- [[_COMMUNITY_Pagination Last Index|Pagination: Last Index]]
- [[_COMMUNITY_Brand File Manifest|Brand File Manifest]]

## God Nodes (most connected - your core abstractions)
1. `get()` - 100 edges
2. `Agent` - 92 edges
3. `FileRecord` - 77 edges
4. `PhazeAgentClient` - 67 edges
5. `FileState` - 60 edges
6. `FingerprintResult` - 50 edges
7. `ScanBatch` - 45 edges
8. `FileUpsertChunk` - 44 edges
9. `ExecBatchProgressPayload` - 43 edges
10. `ExecutionLogCreate` - 42 edges

## Surprising Connections (you probably didn't know these)
- `row_detail()` --references--> `GET /proposals/{id}/detail`  [INFERRED]
  /Users/Robert/Code/public/phaze/src/phaze/routers/proposals.py → src/phaze/templates/proposals/partials/proposal_row.html
- `Phaze Design System Showcase` --semantically_similar_to--> `base.html (layout shell)`  [INFERRED] [semantically similar]
  design/showcase.html → src/phaze/templates/base.html
- `row_detail()` --conceptually_related_to--> `Proposal (AI rename/move proposal)`  [INFERRED]
  /Users/Robert/Code/public/phaze/src/phaze/routers/proposals.py → src/phaze/templates/proposals/partials/proposal_row.html
- `src/phaze/static/favicon-512.png (runtime favicon @512px, master raster)` --semantically_similar_to--> `design/assets/favicon-512.png (Phaze brand favicon @512px, master raster)`  [INFERRED] [semantically similar]
  src/phaze/static/favicon-512.png → design/assets/favicon-512.png
- `Pydantic schemas for scan API endpoints.` --uses--> `FileCategory`  [INFERRED]
  /Users/Robert/Code/public/phaze/src/phaze/schemas/scan.py → src/phaze/constants.py

## Hyperedges (group relationships)
- **Tracklist browsing/listing UI flow** — tracklists_list, stats_header, filter_tabs, tracklist_card, tracklists_pagination [INFERRED 0.80]
- **Fingerprint scan UI flow** — scan_tab, scan_progress, fingerprint_track_detail [INFERRED 0.78]
- **Discogs matching/linking UI flow** — discogs_match_button, discogs_bulk_link, track_detail [INFERRED 0.75]
- **Scan-trigger flow (pipeline partials)** — pipeline_trigger_scan_card_partial, pipeline_scan_path_picker_partial, pipeline_scan_submit_error_partial, pipeline_scan_progress_card_partial, pipeline_recent_scans_table_partial, pipeline_scan_status_pill_partial [INFERRED 0.80]
- **Cue browsing/generation flow** — cue_list_page, cue_list_partial, cue_row_partial, cue_status_partial, cue_toast_partial [INFERRED 0.80]
- **Pipeline dashboard composition** — pipeline_dashboard_page, pipeline_stage_cards_partial, pipeline_stats_bar_partial, pipeline_trigger_scan_card_partial, pipeline_recent_scans_table_partial [INFERRED 0.80]
- **Tag review inline-edit flow (list+row+comparison+display+edit)** — tags_list, tag_list, tag_row, tag_comparison, tags_inline_display, tags_inline_edit [INFERRED 0.80]
- **Search flow (page+form+results content/table/row+counts)** — search_page, search_form, search_results_content, search_results_table, search_results_row, search_summary_counts [INFERRED 0.80]
- **Execution audit log flow (log+filter+table+pagination+collision)** — execution_audit_log, execution_filter_tabs, execution_audit_table, execution_pagination, execution_collision_block [INFERRED 0.70]
- **Execution monitoring SSE flow (progress card + dispatch summary + counters + per-agent table)** — execution_progress, execution_dispatch_summary_inline, execution_progress_row_inline, execution_agents_table [INFERRED 0.80]
- **Audit log filtered view (content + filter tabs + table + rows)** — execution_audit_content, execution_filter_tabs, execution_audit_table, execution_audit_row [INFERRED 0.85]
- **Duplicate resolution flow (list + group card + comparison + resolve/bulk response + toast)** — duplicates_group_card, duplicates_comparison_table, duplicates_resolve_response, duplicates_toast [INFERRED 0.80]
- **Proposal approval flow (list -> row -> approve -> execute -> undo)** — proposals_list, proposals_proposal_row, proposals_approve_response, proposals_execute_button, proposals_undo_response [INFERRED 0.80]
- **Proposal browse/filter/search/paginate flow** — proposals_filter_tabs, proposals_search_box, proposals_proposal_table, proposals_pagination [INFERRED 0.78]
- **Bulk approve/reject action flow** — proposals_bulk_actions, proposals_proposal_content, proposals_stats_bar [INFERRED 0.75]

## Communities

### Community 0 - "Distributed Agent Data Layer"
Cohesion: 0.03
Nodes (179): Agent, Convert ``dict[str, float]`` to ``"k=v,k=v,k=v"`` summary, top-3 by score, max 5, Idempotently upsert AnalysisResult for a file. Natural key: ``analysis.file_id``, ensure_dev_agent(), _hash_token(), SHA-256 hex digest of the full wire token (prefix included).      Mirrors :func:, Seed a dev agent on a fresh ``agents`` table; no-op otherwise.      Returns the, Pydantic v2 schemas for /api/internal/agent/files (phase-25 file-upsert endpoint (+171 more)

### Community 1 - "Tracklist & Search Frontend"
Cohesion: 0.03
Nodes (152): create_tracklist(), shutdown(), match_tracklist_to_discogs(), SAQ task for matching tracklist tracks to Discogs releases., Match all eligible tracks in a tracklist to Discogs releases.      For each trac, Unified search UI router -- serves the cross-entity search page., Render the search page, or an HTMX results fragment., search_page() (+144 more)

### Community 2 - "Agent API Client"
Cohesion: 0.07
Nodes (132): AnalysisWritePayload, AnalysisWriteResponse, Pydantic schemas for PUT /api/internal/agent/analysis/{file_id} (Phase 26 D-26)., Audio analysis upsert body. All optional -- partial-PUT preserves unset fields., Minimal echo response confirming the upsert (D-26 success body)., construct_agent_client(), Dev-agent seeding for the api lifespan (Phase 27 UAT Gap 3).  Migration 012 (``0, Build a :class:`PhazeAgentClient` from :class:`AgentSettings`.      The SecretSt (+124 more)

### Community 3 - "Admin Agents UI"
Cohesion: 0.03
Nodes (96): _is_htmx(), _load_agents(), page(), GET /admin/agents (full page) + GET /admin/agents/_table (HTMX partial) — Phase, Return the agents_table partial UNCONDITIONALLY.      This is the HTMX poll targ, Return True if the request has the HTMX-set ``HX-Request: true`` header.      Ma, Load every Agent, attach transient ``_status``, sort per UI-SPEC LOCKED.      Re, Render either the full ``admin/agents.html`` page or the partial.      HX-Reques (+88 more)

### Community 4 - "CUE Sheet Generation"
Cohesion: 0.03
Nodes (92): _build_cue_tracks(), generate_batch(), generate_cue(), CueTrackData, generate_cue_content(), next_cue_path(), parse_timestamp_string(), CUE sheet generation service.  Generates CUE sheet content from tracklist data w (+84 more)

### Community 5 - "Proposal & Duplicate Review UI"
Cohesion: 0.04
Nodes (78): base.html (layout shell), Approve proposal action, Bulk approve/reject action, Duplicate group (sha256), Execute/rename (copy-verify-delete), Preview directory tree node, Proposal (AI rename/move proposal), Reject proposal action (+70 more)

### Community 6 - "Duplicate Detection"
Cohesion: 0.06
Nodes (48): associate_companions(), AssociateResponse, DuplicateFile, DuplicateGroup, DuplicateGroupsResponse, list_duplicates(), Companion association service: links companion files to media files in the same, A single file within a duplicate group. (+40 more)

### Community 7 - "Discogs Matching & Controller"
Cohesion: 0.06
Nodes (37): SAQ controller settings -- entry point for ``saq phaze.tasks.controller.settings, Initialize shared resources for fileless tasks (SAQ startup hook).      Does NOT, Clean up shared resources (SAQ shutdown hook)., startup(), compute_discogs_confidence(), DiscogsographyClient, match_track_to_discogs(), _parse_artist_from_name() (+29 more)

### Community 8 - "Execution Audit Log"
Cohesion: 0.07
Nodes (40): Agent (distributed worker), agents_table.html (HTMX self-refreshing agents table), Audit log (execution operations), base.html (page shell, extended), Audit log entry, execution/partials/audit_content.html, audit_log(), execution/partials/audit_row.html (+32 more)

### Community 9 - "Collision & Tree Builder"
Cohesion: 0.07
Nodes (31): build_tree(), _count_files(), detect_collisions(), get_collision_ids(), Collision detection and directory tree builder for approved proposals., Node in a directory tree of approved proposals., Find approved proposals that would collide at the same destination.      Returns, Return set of string UUIDs for proposals that participate in collisions.      Us (+23 more)

### Community 10 - "Tag Proposal & Comparison"
Cohesion: 0.1
Nodes (33): start(), compute_proposed_tags(), parse_filename(), Tag proposal service - computes merged tags from multiple sources.  Priority cas, Extract artist, title, and year from a filename.      Supports patterns:       -, Compute proposed tags by merging sources with priority cascade.      Priority (p, _build_comparison(), compare_tags() (+25 more)

### Community 11 - "File Watcher Debouncer"
Cohesion: 0.09
Nodes (23): whoami_with_retry(), Debouncer, _PendingEntry, Asyncio-owned debouncer for the always-on watcher (Phase 27 D-01, D-02).  State, Per-path state captured at first touch and refreshed on each subsequent touch., Coalesce a stream of filesystem events into one post-per-settled-path.      Back, Record a file-change event for ``path``.          - First touch:        inserts, Emit settled paths and evict stuck paths in a single pass.          Returns ``(r (+15 more)

### Community 12 - "Agent Task Routing"
Cohesion: 0.07
Nodes (24): upsert_files(), AgentTaskRouter, Controller-side per-agent SAQ enqueuer (Phase 26 D-19..D-21).  Replaces the inli, Disconnect every cached Queue and clear the cache. Idempotent., Lazily-cached per-agent Queue enqueuer.      Usage:         router = AgentTaskRo, Return the cached Queue for ``agent_id``, constructing on first access., Enqueue ``task_name`` with ``payload.model_dump()`` kwargs onto agent's queue., Enqueue using ``file_record.agent_id`` (Phase 24 FK to agents.id). (+16 more)

### Community 13 - "Brand Asset Renders"
Cohesion: 0.13
Nodes (30): Banner dark PNG render (horizontal lockup, black bg, cyan mark), Banner light PNG render (horizontal lockup, off-white bg, teal mark), Background dark #0a0c12, Background light #eef0f5 / #f8f9fc, Primary accent cyan #1abbdb (dark mode), Primary accent teal #008caf (light mode), Design system showcase: Phi Wave Final Mark, palette, typography, usage rules, Favicon 128 (rounded dark square, phi-wave mark) (+22 more)

### Community 14 - "Tag Extraction & Writing"
Cohesion: 0.1
Nodes (26): extract_tags(), ExtractedTags, _first_str(), _parse_track(), _parse_year(), Tag extraction service using mutagen for audio metadata., Serialize all tags to a JSON-safe dict.      Skips binary values (cover art / AP, Extract audio tags from a file using mutagen.      Returns an ExtractedTags data (+18 more)

### Community 15 - "Settings & Config"
Cohesion: 0.08
Nodes (21): BaseSettings, _build_default_settings(), ControlSettings, _direct_env_names(), get_settings(), Pydantic settings configuration for Phaze.  Phase 26 D-14: settings split into a, Return the env-var names a field accepts directly: its ``validation_alias``, Application-server role: LLM proposal generation, Discogs matching, fileless tas (+13 more)

### Community 16 - "Fingerprint & Process Jobs"
Cohesion: 0.11
Nodes (20): FingerprintFilePayload, ProcessFilePayload, Typed SAQ-job payload models for file-bound tasks (Phase 26 D-22..D-24).  Every, SAQ job: CPU-bound essentia analysis of a single audio file., SAQ job: submit a file to audfprint + panako sidecars., fingerprint_file(), Fingerprint a file through both engines; PUT per-engine result via HTTP., _features_to_mood_dict() (+12 more)

### Community 17 - "Essentia Audio Analysis"
Cohesion: 0.12
Nodes (21): analyze_file(), derive_mood(), derive_style(), _get_classifier(), _get_labels(), _make_standard_set(), ModelConfig, ModelSetConfig (+13 more)

### Community 18 - "Agent Auth & Registration"
Cohesion: 0.13
Nodes (19): get_authenticated_agent(), hash_token(), Bearer-token authentication dependency for /api/internal/agent/* routes.  NOT a, Return `sha256(token).hex()` of the entire wire token (prefix included).      Pe, Resolve the calling agent from the bearer token.      Raises:         HTTPExcept, add_agent(), _build_parser(), derive_queue_name() (+11 more)

### Community 19 - "Favicon Assets"
Cohesion: 0.11
Nodes (20): design/assets/favicon-128.png (Phaze brand favicon @128px), design/assets/favicon-16.png (Phaze brand favicon @16px), design/assets/favicon-256.png (Phaze brand favicon @256px), design/assets/favicon-32.png (Phaze brand favicon @32px), design/assets/favicon-48.png (Phaze brand favicon @48px), design/assets/favicon-512.png (Phaze brand favicon @512px, master raster), design/assets/favicon-64.png (Phaze brand favicon @64px), src/phaze/static/apple-touch-icon.png (iOS home-screen icon, phaze brand mark) (+12 more)

### Community 20 - "TLS Cert Bootstrap"
Cohesion: 0.17
Nodes (13): ensure_certs_present(), _generate_ca(), _generate_leaf(), _parse_san_entries(), Pre-uvicorn cert bootstrap (Phase 29 D-02).  Generates a self-signed CA + leaf c, Generate a CA-signed leaf cert (ECDSA P-256, 2-year validity)., Idempotent CA + leaf bootstrap.      Generates a fresh CA + leaf pair ONLY if th, Parse comma-separated SAN list: DNSName for hostnames, IPAddress for IPs.      E (+5 more)

### Community 21 - "File Execution (copy-verify-delete)"
Cohesion: 0.18
Nodes (11): complete_operation(), execute_single_file(), get_approved_proposals(), log_operation(), Execute a single file rename via copy-verify-delete.      This is the core safet, Create a write-ahead ExecutionLog entry with IN_PROGRESS status.      The entry, Update an ExecutionLog entry to COMPLETED or FAILED.      Args:         session:, Get all approved proposals with eagerly loaded file relationships.      Args: (+3 more)

### Community 22 - "Exec Batch Progress"
Cohesion: 0.2
Nodes (9): _compute_increments(), _get_promote_status_script(), _get_redis(), post_exec_batch_progress(), Pydantic schemas for POST /api/internal/agent/exec-batches/{batch_id}/progress (, D-07 counter update rules. Returns the HINCRBY dict for this progress event., Per-proposal terminal-state event handler (D-05, D-07, D-15, D-17).      Returns, Return the cached status-promotion script, registering it on first call. (+1 more)

### Community 23 - "Scan API Schemas"
Cohesion: 0.2
Nodes (8): get_scan_status(), Request body for triggering a file scan., Response returned after starting a scan., Response for scan status queries., Get the status of a scan batch by its ID., ScanRequest, ScanResponse, ScanStatusResponse

### Community 24 - "Model Weight Download"
Cohesion: 0.22
Nodes (8): _download_one(), download_to(), Python helper that fetches the essentia weight files (Phase 29 D-21).  The same, Download ``url`` to ``dest`` using an atomic ``.part`` rename.      Idempotent:, Download all classifier + genre weight files into ``target_dir``.      Idempoten, ensure_models_present(), Auto-download essentia weights when /models is empty (Phase 29 D-21).  IMPORT-BO, Skip if all expected .pb files exist; else download. Raises RuntimeError on fail

### Community 25 - "Design System Tokens"
Cohesion: 0.22
Nodes (9): Color System (tokens, surfaces, status), Component Patterns (badges, buttons, cards, inputs), Border radius scale, Spacing scale (4px base unit), Typography (Jura, Inter, mono), Color-as-timbre philosophy, Golden ratio and musical intervals composition, Interference pattern form logic (+1 more)

### Community 26 - "Naming Format Rules"
Cohesion: 0.33
Nodes (7): Album track naming format, Confidence scoring rubric, Date format rules (YYYY.MM.DD with x placeholders), Directory path 3-step decision tree, Live set naming format, Structured metadata fields to extract, Music file naming LLM prompt

### Community 27 - "Agent Heartbeat"
Cohesion: 0.4
Nodes (3): post_heartbeat(), Pydantic schema for POST /api/internal/agent/heartbeat (phase-25 D-17, D-19)., Update agents.last_seen_at and last_status. Returns 204 No Content (D-19).

### Community 28 - "Health Check"
Cohesion: 0.5
Nodes (3): health_check(), Health check endpoint., Check API and database connectivity.

### Community 29 - "Time Humanizer"
Cohesion: 0.5
Nodes (3): Relative-time formatter: '23s ago', '4m ago', '2h ago', '3d ago'.  UI-SPEC §Rela, Return a glanceable 'N{s,m,h,d} ago' label for ``dt`` (or 'never' / 'just now')., relative_time()

### Community 30 - "Analysis Write Endpoint"
Cohesion: 1.0
Nodes (2): put_analysis(), _summarize_dict_to_string()

### Community 31 - "Agent Watcher Docs"
Cohesion: 1.0
Nodes (3): phaze.agent_watcher README, Agent watcher (file-server file watcher), POST /api/internal/agent/files

### Community 32 - "Brand Voice & Tone"
Cohesion: 1.0
Nodes (2): Voice & Tone, Resonant Precision design movement

### Community 33 - "_FILE Secret Resolution"
Cohesion: 1.0
Nodes (1): Resolve `<VAR>_FILE` secrets before any required-field / production guard.

### Community 34 - "Sidecar Locality Rule"
Cohesion: 1.0
Nodes (1): Phase 28 D-12 / TASK-04: fingerprint sidecars MUST be local to the file server.

### Community 35 - "Scan Roots Parsing"
Cohesion: 1.0
Nodes (1): Comma-split `PHAZE_AGENT_SCAN_ROOTS` env input into a list[str].          pydant

### Community 36 - "HTTPS Enforcement Rule"
Cohesion: 1.0
Nodes (1): Phase 29 CR-01: production refuses non-HTTPS agent_api_url.          Agent → app

### Community 37 - "Redis Password Rule"
Cohesion: 1.0
Nodes (1): D-06: production refuses passwordless redis_url.          Phase 29 AUTH-03 pairs

### Community 38 - "Package Init (alembic)"
Cohesion: 1.0
Nodes (0):

### Community 39 - "Package Init (services)"
Cohesion: 1.0
Nodes (0):

### Community 40 - "Moved-State Validation"
Cohesion: 1.0
Nodes (1): Per CONTEXT.md discretion: current_path is required when file_state=='moved'.

### Community 41 - "Failed-Step Invariant"
Cohesion: 1.0
Nodes (1): D-06 invariant: failed_at_step is required iff terminal_step == 'failed'.

### Community 42 - "Package Init (schemas)"
Cohesion: 1.0
Nodes (0):

### Community 43 - "Package Init (routers)"
Cohesion: 1.0
Nodes (0):

### Community 44 - "Confidence Clamp"
Cohesion: 1.0
Nodes (1): Clamp a confidence value to the 0.0-1.0 range.

### Community 45 - "Pagination: Total Pages"
Cohesion: 1.0
Nodes (1): Total number of pages.

### Community 46 - "Pagination: Has Prev"
Cohesion: 1.0
Nodes (1): Whether a previous page exists.

### Community 47 - "Pagination: Has Next"
Cohesion: 1.0
Nodes (1): Whether a next page exists.

### Community 48 - "Pagination: First Index"
Cohesion: 1.0
Nodes (1): 1-based index of the first item on this page.

### Community 49 - "Pagination: Last Index"
Cohesion: 1.0
Nodes (1): 1-based index of the last item on this page.

### Community 50 - "Brand File Manifest"
Cohesion: 1.0
Nodes (1): File Manifest (logos, banners, favicons)

## Ambiguous Edges - Review These
- `page()` → `table_partial()`  [AMBIGUOUS]
  src/phaze/templates/admin/agents.html · relation: rationale_for
- `pipeline scan_progress_card partial` → `Scan (ScanBatch) domain concept`  [AMBIGUOUS]
  src/phaze/templates/pipeline/partials/scan_progress_card.html · relation: rationale_for

## Knowledge Gaps
- **336 isolated node(s):** `Pydantic settings configuration for Phaze.  Phase 26 D-14: settings split into a`, `Return the env-var names a field accepts directly: its ``validation_alias```, `Build the case-insensitive name->value map used to resolve `_FILE` secrets.`, `v4.0 role selector. Controller = application server (fileless tasks); Agent = fi`, `Fields shared by both roles. Every existing call site `settings.<field>` resolve` (+331 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Brand Voice & Tone`** (2 nodes): `Voice & Tone`, `Resonant Precision design movement`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `_FILE Secret Resolution`** (1 nodes): `Resolve `<VAR>_FILE` secrets before any required-field / production guard.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Sidecar Locality Rule`** (1 nodes): `Phase 28 D-12 / TASK-04: fingerprint sidecars MUST be local to the file server.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Scan Roots Parsing`** (1 nodes): `Comma-split `PHAZE_AGENT_SCAN_ROOTS` env input into a list[str].          pydant`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `HTTPS Enforcement Rule`** (1 nodes): `Phase 29 CR-01: production refuses non-HTTPS agent_api_url.          Agent → app`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Redis Password Rule`** (1 nodes): `D-06: production refuses passwordless redis_url.          Phase 29 AUTH-03 pairs`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init (alembic)`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init (services)`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Moved-State Validation`** (1 nodes): `Per CONTEXT.md discretion: current_path is required when file_state=='moved'.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Failed-Step Invariant`** (1 nodes): `D-06 invariant: failed_at_step is required iff terminal_step == 'failed'.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init (schemas)`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init (routers)`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Confidence Clamp`** (1 nodes): `Clamp a confidence value to the 0.0-1.0 range.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Pagination: Total Pages`** (1 nodes): `Total number of pages.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Pagination: Has Prev`** (1 nodes): `Whether a previous page exists.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Pagination: Has Next`** (1 nodes): `Whether a next page exists.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Pagination: First Index`** (1 nodes): `1-based index of the first item on this page.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Pagination: Last Index`** (1 nodes): `1-based index of the last item on this page.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Brand File Manifest`** (1 nodes): `File Manifest (logos, banners, favicons)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `page()` and `table_partial()`?**
  _Edge tagged AMBIGUOUS (relation: rationale_for) - confidence is low._
- **What is the exact relationship between `pipeline scan_progress_card partial` and `Scan (ScanBatch) domain concept`?**
  _Edge tagged AMBIGUOUS (relation: rationale_for) - confidence is low._
- **Why does `get()` connect `Tracklist & Search Frontend` to `Distributed Agent Data Layer`, `Agent API Client`, `Admin Agents UI`, `CUE Sheet Generation`, `Proposal & Duplicate Review UI`, `Duplicate Detection`, `Discogs Matching & Controller`, `Execution Audit Log`, `Collision & Tree Builder`, `Tag Proposal & Comparison`, `File Watcher Debouncer`, `Agent Task Routing`, `Tag Extraction & Writing`, `Settings & Config`, `Fingerprint & Process Jobs`, `Essentia Audio Analysis`, `TLS Cert Bootstrap`, `Analysis Write Endpoint`?**
  _High betweenness centrality (0.481) - this node is a cross-community bridge._
- **Why does `Agent` connect `Distributed Agent Data Layer` to `Agent API Client`, `Admin Agents UI`, `Execution Audit Log`, `Collision & Tree Builder`, `Agent Auth & Registration`, `Exec Batch Progress`, `Agent Heartbeat`?**
  _High betweenness centrality (0.111) - this node is a cross-community bridge._
- **Why does `add()` connect `Tracklist & Search Frontend` to `Distributed Agent Data Layer`, `Duplicate Detection`, `Discogs Matching & Controller`, `Tag Extraction & Writing`, `Agent Auth & Registration`, `File Execution (copy-verify-delete)`?**
  _High betweenness centrality (0.073) - this node is a cross-community bridge._
- **Are the 70 inferred relationships involving `get()` (e.g. with `_resolution_env()` and `_resolve_secret_files()`) actually correct?**
  _`get()` has 70 INFERRED edges - model-reasoned connections that need verification._
- **Are the 88 inferred relationships involving `Agent` (e.g. with `Pydantic schemas for POST /api/internal/agent/tracklists (Phase 26 D-27).  Per D` and `Pull the Redis client from ``app.state`` (wired by Plan 26-12 main.py lifespan).`) actually correct?**
  _`Agent` has 88 INFERRED edges - model-reasoned connections that need verification._
