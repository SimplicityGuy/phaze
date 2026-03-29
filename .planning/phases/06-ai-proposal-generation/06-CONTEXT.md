# Phase 6: AI Proposal Generation - Context

**Gathered:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Use an LLM to propose new filenames for music files. Send file metadata, analysis results, and companion file content to the LLM, receive structured filename proposals, store as immutable records in PostgreSQL. Filename proposals only (no directory path proposals in v1). Runs as automated batch pipeline through arq workers.

</domain>

<decisions>
## Implementation Decisions

### Naming Format
- **D-01:** LLM decides the best naming format per file based on available metadata (adaptive, not a rigid template). The prompt includes naming rules but the LLM picks the most informative style per file.
- **D-02:** **Live sets/performances:** `{Artist} - Live @ {Venue|Event} {day/stage if available} {YYYY.MM.DD}.{ext}`
- **D-03:** **Album tracks:** `{Artist} - {Track #} - {Track Title}.{ext}` inside a directory named `{Album Name}`
- **D-04:** **Date format:** Always `YYYY.MM.DD` with `x` for unknown parts. Examples: `2013.05.xx` (month only), `2005.xx.xx` (year only), `xxxx.03.14` (month/day only, rare).
- **D-05:** Always preserve the original file extension. Never change or normalize extensions.
- **D-06:** Ignore the scene-era 255 char dirname+filename limit. Modern Linux ext4/btrfs supports 255 bytes per filename component and 4096 bytes for full path.
- **D-07:** For files with very little metadata (no analysis, no companions, just a filename), generate a low-confidence proposal and flag it for manual review in the admin UI.

### Prompt Design
- **D-08:** Send all available context per file: original filename, original path, analysis results (BPM, mood, style, key, features), and companion file content (NFO text, cue sheets, m3u playlists).
- **D-09:** LLM returns structured JSON via Pydantic structured output: `proposed_filename`, `confidence` (0-1), extracted metadata (`artist`, `event_name`, `venue`, `date`, `source_type`, `stage`, `day_number`, etc.), and `reasoning`.
- **D-10:** No few-shot examples in the prompt. Naming rules only. This is a fully automated agentic pipeline — no interactive prompting.
- **D-11:** Prompt template stored as a **markdown file on disk** (not baked into code). Loaded at runtime, easy to edit without code changes, version-controlled in git.

### Metadata Extraction & Storage
- **D-12:** LLM extracts structured metadata alongside filename proposals: event details (name, year, day, stage), artist info (normalized name, guests, b2b partners), source type (SBD/FM/AUD/WEB etc.), and venue/location.
- **D-13:** All extracted metadata stored in the existing `RenameProposal.context_used` JSONB column. No new columns or tables needed for v1.

### Batch Strategy
- **D-14:** Fixed-size batches (files grouped in chunks regardless of directory origin). Most of the collection is unorganized, so directory-based grouping provides little contextual benefit.
- **D-15:** Claude's discretion on optimal batch size — balance token usage per file against model context window limits.
- **D-16:** Batch processing runs through the **arq worker pool** — one arq job per batch. Leverages existing retry/backoff infrastructure from Phase 4.

### LLM Provider
- **D-17:** Use **litellm** (pinned `>=1.82.6,<1.82.7` per CLAUDE.md) for unified LLM access. Target Claude (Anthropic) and OpenAI — user wants to experiment with both.
- **D-18:** Model name and API keys configured via **environment variables** (e.g., `LLM_MODEL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). litellm handles provider routing based on model name.
- **D-19:** **Configurable rate limiting** on LLM calls (max requests per minute via env var) to prevent runaway costs.

### Scope
- **D-20:** **Filename proposals only** in v1. Directory path proposals (AIP-03) deferred to v2. The extracted metadata (event name, type, venue, year) stored in `context_used` JSONB will enable directory routing later without re-querying the LLM.

### Claude's Discretion
- Optimal batch size based on token budget analysis
- Rate limiting implementation (Redis-based counter, in-memory, or arq job scheduling)
- How to read companion file content (full text vs summary/truncation for large NFO files)
- Pydantic response model field names and structure for the structured JSON output
- Where to store the prompt markdown file (e.g., `prompts/`, `config/`, or `src/phaze/prompts/`)
- Whether to add `litellm` to pyproject.toml dependencies or treat as system dependency

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, code quality rules, litellm version pinning
- `.planning/PROJECT.md` — Project vision, constraints, naming format TBD (now resolved)
- `.planning/REQUIREMENTS.md` — AIP-01 (LLM filename proposals), AIP-02 (immutable proposal records)

### Existing Code
- `src/phaze/models/proposal.py` — RenameProposal model (proposed_filename, proposed_path, confidence, context_used JSONB, reason, status)
- `src/phaze/models/file.py` — FileRecord model (original_path, original_filename, current_path, file_type, state with PROPOSAL_GENERATED)
- `src/phaze/models/analysis.py` — AnalysisResult model (bpm, musical_key, mood, style, features JSONB)
- `src/phaze/models/file_companion.py` — FileCompanion join table (companion_id, media_id)
- `src/phaze/tasks/functions.py` — process_file pattern (arq job, session management, retry with backoff)
- `src/phaze/tasks/pool.py` — run_in_process_pool helper
- `src/phaze/tasks/worker.py` — WorkerSettings with on_startup/on_shutdown hooks
- `src/phaze/config.py` — Settings with pydantic-settings

### Naming Reference
- `prototype/naming/mp3rules4.1.txt` — Official MP3 Release Rules 4.1 (scene naming conventions, source tags, dirname format). Use as context for understanding incoming filenames, NOT as the target naming format.
- `prototype/naming/dirs.json` — Current directory structure (performances/{artists,festivals,concerts,events,radioshows,clubs,...}). Reference for understanding collection organization.

### Prior Phase Context
- `.planning/phases/04-task-queue-worker-infrastructure/04-CONTEXT.md` — Worker decisions (arq patterns, retry, process pool)
- `.planning/phases/05-audio-analysis-pipeline/05-CONTEXT.md` — Analysis decisions (essentia, model registry, features JSONB)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `RenameProposal` model — already has all needed columns (proposed_filename, confidence, context_used JSONB, status)
- `FileState.PROPOSAL_GENERATED` — state transition already defined in the file state machine
- arq worker infrastructure — retry with backoff, process pool, session management patterns
- `_get_session()` pattern in `tasks/functions.py` — reusable for proposal generation jobs

### Established Patterns
- arq job functions in `tasks/functions.py` with try/finally session management
- `run_in_process_pool` for CPU-bound work (LLM calls are I/O-bound so may not need this)
- SQLAlchemy 2.0 async queries with upsert pattern
- pydantic-settings for configuration with env var overrides

### Integration Points
- New `src/phaze/services/proposal.py` for LLM interaction and proposal generation logic
- New arq job function `generate_proposals` in `tasks/functions.py` (or separate file)
- New prompt template file (markdown) loaded by the proposal service
- Add litellm dependency to pyproject.toml
- Add LLM config fields to Settings (model name, API keys, rate limit, batch size)
- Query FileRecord + AnalysisResult + FileCompanion to build LLM context per file

</code_context>

<specifics>
## Specific Ideas

- The user's collection is primarily DJ sets and live recordings, not studio albums. Naming should prioritize the live set format.
- Scene-style filenames (e.g., `999999999-Live_At_Boiler_Room_Paris_Possession-WEB-2019-XTC_iNT`) contain rich parseable info — artist, event, source, year, group.
- NFO files from scene releases often contain detailed event info, tracklists, and radio station names.
- The directory structure under `performances/` has categories: artists, festivals, concerts, events, radioshows, clubs, bootlegs, raid party, misc.
- Directory naming conventions for future path proposals (v2): `performances/artists/{Artist Name}/`, `performances/festivals/{Festival Name} {Year}/`, `performances/concerts/{Concert Name} {Year}/`, `performances/radioshows/{Radioshow Name}/`, `performances/raid party/{Date}/`.

</specifics>

<deferred>
## Deferred Ideas

- **AIP-03 (Directory path proposals):** LLM proposes destination folder paths based on extracted metadata. Deferred to v2. The metadata extracted in Phase 6 (event name, type, venue, year) stored in `context_used` JSONB will make this straightforward without re-querying the LLM.
- **Few-shot prompt tuning:** If rules-only prompting produces inconsistent results across 200K files, add curated before/after examples to the prompt template.

</deferred>

---

*Phase: 06-ai-proposal-generation*
*Context gathered: 2026-03-28*
