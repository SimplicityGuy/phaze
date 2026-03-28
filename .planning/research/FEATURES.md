# Feature Landscape

**Domain:** Music file management and AI-powered organization
**Researched:** 2026-03-27

## Table Stakes

Features users expect. Missing = product feels incomplete.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| File ingestion with hash dedup | Cannot manage what you cannot see. Every tool (beets, Picard, AudioRanger) starts with scanning and cataloging files. sha256 dedup prevents wasted processing. | Medium | Must handle ~200K files across mp3, m4a, ogg, and video formats. Batch/parallel processing essential. |
| Metadata extraction (ID3, Vorbis, MP4 tags) | Files already have some tags. Must capture before proposing changes. All tools (Mp3tag, beets, Picard) read existing embedded tags first. | Low | mutagen handles all formats. Extract artist, title, album, track, year, genre, duration. Handle missing/corrupt tags gracefully. |
| Audio fingerprinting and identification | Chromaprint/AcoustID is industry standard for identifying tracks even without metadata. Picard, beets, AudioRanger all use it. | Medium | pyacoustid + chromaprint C library. Lookup against AcoustID web service. Rate-limited API (3 req/sec); plan for batch pacing. |
| BPM detection | Core analysis feature per project requirements. Already prototyped. Standard feature in DJ-oriented tools. | Medium | librosa beat_track(). ~5-10 seconds per file. Must parallelize across workers. |
| AI filename proposals | Core value proposition. LLM examines metadata + analysis and proposes standardized name. No existing tool does this with LLMs. | Medium-High | Structured output from LLM. Template-based naming format (TBD). Batch prompting to reduce API calls and cost. |
| AI path proposals | Core value proposition. LLM proposes folder structure based on genre/mood/artist/event. | Medium | Depends on filename proposals. May use same LLM call. |
| Approval workflow UI | Human-in-the-loop is a project constraint. Every serious tool provides preview before execution. | High | Paginated list of proposals, approve/reject per-file, bulk actions, filtering. HTMX for interactivity. |
| Safe file rename/move execution | Must never lose or corrupt files. This is an irreplaceable collection. | Medium | Copy-verify-delete protocol. Atomic moves where possible. Collision detection. Append-only audit log. |
| Video stream metadata extraction | Concert videos are explicitly in scope. Must extract duration, resolution, codec info. | Low | ffprobe subprocess call. No audio analysis needed for video files initially. |
| Progress tracking / job status | 200K files = hours of processing. User needs visibility into pipeline progress. | Medium | Job state in PostgreSQL. UI polling via HTMX or SSE. Per-batch and per-file status. |
| PostgreSQL metadata store | At 200K files with complex metadata and relationships, a real RDBMS is needed. | Low | Schema: files, metadata, analysis, proposals, execution_log. JSONB for flexible tag storage. |
| Docker Compose deployment | Standard practice for self-hosted tools on home servers. | Low | Python API + workers + Postgres + Redis in Docker Compose. Well-understood pattern. |

## Differentiators

Features that set product apart. Not expected but add significant value.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Mood/style classification | Goes beyond basic tags. AI-inferred mood (energetic, chill, dark) and style labels from audio features. Beets has limited plugins for this; Phaze does it natively. | High | Requires existing prototype integration. Spectral features from librosa fed to classifier or LLM. |
| Acoustic duplicate detection | Finds duplicates even across different encodings (mp3 vs m4a of same track). No mainstream collection tool does fingerprint-based dedup. | Medium | Chromaprint fingerprint comparison. Group by fingerprint similarity. Present groups for human resolution. |
| Batch approval with smart grouping | Group proposals by artist/album/event for faster review instead of 200K individual decisions. Novel workflow. | Medium | UI grouping logic. "Approve all from this album" bulk action. Confidence-based sorting (review lowest confidence first). |
| Concert/event auto-detection | Auto-identify concert recordings from festival streams based on filename patterns, duration, metadata. No existing tool handles this. | Low-Medium | Pattern matching + LLM classification. "Coachella 2024 Day 2 Stream" -> event=Coachella, year=2024. |
| Undo/rollback for executed moves | Full audit trail and reverse capability. AudioRanger has limited undo; beets has none. | Medium | Transaction log in PostgreSQL. Reverse-move capability. |
| MusicBrainz metadata enrichment | Map fingerprints to canonical artist/album/track data from the open music database. Fills gaps in poorly tagged files. | Medium | musicbrainzngs library. Rate-limited API (1 req/sec). Provides canonical identifiers for future cross-service linking. |

## Anti-Features

Features to explicitly NOT build in v1.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| Music player / streaming | Completely different product. Plex, Jellyfin, Navidrome already do this. Scope explosion. | Organize files into a structure that existing players can ingest. |
| Full-text search UI | Deferred per PROJECT.md. Organization must come first. Search needs rich, clean metadata. | Expose PostgreSQL data via API for future search frontend. |
| Multi-user auth | Single user on private network. Auth adds complexity for zero benefit. | Optional basic auth or IP allowlist if paranoid. |
| Real-time file watching | Daemon complexity, filesystem event handling, race conditions. For a batch organization project, manual trigger is fine. | CLI command or UI button to re-scan on demand. |
| Tag writing / metadata embedding | Modifying original files is destructive. Risk of corruption at 200K scale. The rename/move IS the organization. | Store all metadata in Postgres. Tag writing can be a separate opt-in feature later. |
| Streaming service integration | API rate limits, authentication complexity, scope creep. Not needed for file organization. | MusicBrainz IDs link to external services. Streaming lookup is a future add-on. |
| Natural language querying | Deferred per PROJECT.md. Requires rich metadata to be useful. | Defer until post-v1. NLQ over organized Postgres DB is straightforward later. |
| 1001tracklists integration | Deferred per PROJECT.md. Scraping is fragile. Existing code available for future use. | Defer to post-v1. |
| Discogsography cross-linking | Deferred per PROJECT.md. Requires core organization to be complete. API is accessible. | Defer to post-v1. Use MusicBrainz IDs as bridge identifiers. |
| Auto-approve mode | Violates human-in-the-loop constraint. Dangerous for irreplaceable collection. | Always require explicit approval. Provide efficient bulk-approve UI. |

## Feature Dependencies

```
File Ingestion (hash + path + original name)
  |
  +---> Metadata Extraction (read existing tags with mutagen)
  |       |
  |       +---> MusicBrainz Enrichment --requires--> Audio Fingerprinting (pyacoustid)
  |       |
  |       +---> BPM Detection (librosa) [independent, parallelizable]
  |       |
  |       +---> Mood/Style Classification (librosa features) [independent]
  |
  +---> Exact Duplicate Detection (sha256 comparison)
  |
  +---> Acoustic Duplicate Detection --requires--> Audio Fingerprinting

AI Filename/Path Proposals --requires--> Metadata Extraction + Analysis results
  |
  +---> Approval Workflow UI --requires--> Proposals in database
          |
          +---> Safe File Execution --requires--> Approved proposals
                  |
                  +---> Audit Trail + Rollback
```

Video streams follow a shorter path:
```
File Ingestion (hash + path)
  +---> Video Metadata Extraction (ffprobe)
        +---> AI Filename/Path Proposal
              +---> Approval -> Execution
```

### Dependency Notes

- **AI proposals need metadata enrichment:** LLM needs rich context (artist, album, track, year, genre) to propose good filenames. Running AI on files with zero metadata produces garbage.
- **Music analysis is independent:** BPM/key/mood extraction can run in parallel with metadata enrichment. No dependency between them.
- **Fingerprinting enables two things:** AcoustID lookup (for MusicBrainz enrichment) and acoustic dedup. Both require fingerprints but are otherwise independent.
- **Execute requires approval:** Core safety contract. Nothing moves without human sign-off.

## MVP Recommendation

Prioritize in this order:

1. **File ingestion + hash dedup** -- Foundation. Everything depends on this.
2. **Metadata extraction** -- Low effort, immediate value, informs all downstream features.
3. **Database schema + migration setup** -- Structural foundation for everything.
4. **Task queue infrastructure (arq + Redis)** -- Required for parallel processing at scale.
5. **Audio fingerprinting** -- Enables MusicBrainz enrichment and acoustic dedup.
6. **BPM detection** -- Integrates existing prototypes, proves worker pipeline.
7. **AI filename/path proposals** -- Core differentiator. Can start with metadata-only proposals.
8. **Approval workflow UI** -- Gates all file operations.
9. **Safe file execution** -- Final step in the pipeline.

Defer to post-MVP:
- **Mood/style classification**: Can run analysis retroactively. Depends on prototype maturity.
- **MusicBrainz enrichment**: Valuable but rate-limited (1 req/sec = 55+ hours for 200K files). Run as background enrichment.
- **Acoustic fingerprint dedup**: sha256 catches exact dupes first. Acoustic dedup is a refinement.
- **Concert/event detection**: Nice-to-have. Can be added as LLM classification task later.
- **Undo/rollback**: Important but can be a fast-follow if transaction logging is in the initial schema.

## Competitor Feature Analysis

| Feature | beets | MusicBrainz Picard | AudioRanger | Mp3tag | Phaze |
|---------|-------|-------------------|-------------|--------|-------|
| Metadata from tags | Yes (auto) | Yes (auto) | Yes (auto) | Yes (manual+batch) | Yes (auto, in Postgres) |
| Audio fingerprinting | Plugin (chroma) | Core | Core | No | Core (AcoustID) |
| MusicBrainz lookup | Core | Core | Yes | Yes (web source) | Planned (post-MVP) |
| Rename by template | Yes (configurable) | Yes (configurable) | Yes (configurable) | Yes (format strings) | AI-proposed (beyond templates) |
| Folder organization | Yes (path formats) | Yes (file naming) | Yes (patterns) | No (rename only) | AI-proposed paths + approval |
| Duplicate detection | Plugin (duplicates) | No | No | No | SHA256 + fingerprint similarity |
| GUI | CLI only | Desktop GUI | Desktop GUI | Desktop GUI | Web UI (remote-accessible) |
| Approval workflow | Auto-apply | Manual per-file | Manual per-file | Manual per-file | Batch approval web queue |
| Video support | No | No | No | No | Yes (concert video streams) |
| BPM/key/mood analysis | Plugins (limited) | No | No | No | librosa (deep analysis) |
| Undo/rollback | No | No | Multi-level undo | No | Full audit trail + rollback |
| Scale (100K+ files) | Good (SQLite) | Poor (loads into RAM) | Moderate | Good (batch) | Designed for 200K (Postgres + workers) |

## Sources

- [beets - the music geek's media organizer](https://beets.io/) -- competitor reference
- [MusicBrainz Picard](https://picard.musicbrainz.org/) -- competitor reference
- [AudioRanger](https://www.audioranger.com/en/) -- competitor reference
- [Chromaprint/AcoustID](https://acoustid.org/chromaprint) -- fingerprinting ecosystem
- [librosa documentation](https://librosa.org/) -- audio analysis capabilities
- Project requirements from `.planning/PROJECT.md`

---
*Feature landscape for: Music file management and AI-powered organization*
*Researched: 2026-03-27*
