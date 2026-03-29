# Phase 5: Audio Analysis Pipeline - Context

**Gathered:** 2026-03-28
**Updated:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Implement audio analysis for music files: BPM detection, mood classification, style/genre classification, musical key detection. Uses essentia-tensorflow (not librosa) with 34 pre-trained models from the existing prototypes. Analysis runs through the arq worker pool (Phase 4) via ProcessPoolExecutor. Results stored in the existing AnalysisResult model.

</domain>

<decisions>
## Implementation Decisions

### Analysis Library & Model Scope
- **D-01:** Use essentia-tensorflow for all analysis — BPM, mood, style, genre, key. No librosa. This overrides CLAUDE.md recommendation to match existing prototype code which is entirely essentia-based.
- **D-02:** Run all 34 models (33 characteristic + 1 discogs-effnet genre). Full prototype coverage. BPM via RhythmExtractor2013, key via KeyExtractor with EDMA profile.

### Result Storage & Derivation
- **D-03:** Single mood/style summary columns for quick access. Mood derived by averaging positive-class confidence across 7 mood model variants. Style from top genre prediction in discogs-effnet.
- **D-04:** All raw predictions (all 34 models) stored in features JSONB column. Top-3 moods/styles available in JSONB — no need for additional columns since Phase 6 reads from features JSONB for proposal context.

### Model File Management
- **D-05:** **Runtime volume mount** — models NOT baked into Docker image. Download script (`just download-models`) populates a host directory, Docker Compose mounts it into the worker container. Smaller Docker image.
- **D-06:** Worker checks models exist on startup. Fails fast with clear error if models directory is missing or incomplete.
- **D-07:** Flat directory structure matching prototype pattern. 68 files (34 .pb + 34 .json) from essentia.upf.edu.

### Worker Integration
- **D-08:** Analysis runs through process_file in tasks/functions.py via run_in_process_pool. One job per file. 3 retries with job_try*5s backoff.
- **D-09:** Non-music files skipped (extension set check). Corrupt/unreadable files trigger retry then permanent failure.

### Claude's Discretion
- Mood/style derivation algorithm details
- Essentia installation approach in Docker
- Model download script implementation
- Error handling for corrupt audio files

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup (NOTE: librosa recommendation overridden by D-01)
- `.planning/REQUIREMENTS.md` — ANL-01 (BPM detection), ANL-02 (mood/style classification)

### Existing Code
- `src/phaze/models/analysis.py` — AnalysisResult model (bpm, musical_key, mood, style, features JSONB)
- `src/phaze/tasks/functions.py` — process_file skeleton
- `src/phaze/tasks/pool.py` — ProcessPoolExecutor helper
- `src/phaze/tasks/worker.py` — WorkerSettings with on_startup/on_shutdown hooks

### Prototype Code (MUST READ)
- `prototype/code/characteristics.py` — Essentia mood/style classification with 11 ModelSets
- `prototype/code/bpm-genre.py` — Essentia discogs-effnet genre classification
- `prototype/code/models.txt` — URLs for all model files

### Prior Phase Context
- `.planning/phases/04-task-queue-worker-infrastructure/04-CONTEXT.md` — Worker decisions

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- AnalysisResult model with all needed columns
- process_file skeleton ready for analysis logic
- run_in_process_pool async wrapper
- Prototype Predictor class and ModelSet dataclasses

### Established Patterns
- arq job functions, retry with backoff
- Async session access, SQLAlchemy 2.0 queries

### Integration Points
- New src/phaze/services/analysis.py for analysis business logic
- Fill in process_file body with real analysis
- Download script and Docker volume mount for models
- Models path configuration in Settings

</code_context>

<specifics>
## Specific Ideas

- Prototype characteristics.py has clean Predictor context manager — adapt for service layer
- 11 model sets with 3 variants each — average predictions across variants
- Model files ~5-10MB each, 68 files total = ~200-300MB
- **CHANGE FROM ORIGINAL:** Models via volume mount, not baked into image

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 05-audio-analysis-pipeline*
*Context gathered: 2026-03-28*
*Context updated: 2026-03-28*
