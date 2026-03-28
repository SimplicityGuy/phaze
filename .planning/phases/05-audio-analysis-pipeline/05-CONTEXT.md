# Phase 5: Audio Analysis Pipeline - Context

**Gathered:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Implement audio analysis for music files: BPM detection, mood classification, style/genre classification. Uses essentia (not librosa) with TensorFlow pre-trained models from the existing prototypes. Analysis runs through the arq worker pool (Phase 4) via ProcessPoolExecutor. Results stored in the existing AnalysisResult model.

</domain>

<decisions>
## Implementation Decisions

### Analysis Library
- **D-01:** Use **essentia** for all analysis — BPM, mood, style, genre. No librosa. This deviates from CLAUDE.md's recommendation but aligns with the existing prototype code which is entirely essentia-based. Essentia can handle BPM via its `RhythmExtractor2013` algorithm.

### Model Scope
- **D-02:** Run all 33 models from the prototype (11 model sets x 3 models each): mood_acoustic, mood_electronic, mood_aggressive, mood_relaxed, mood_happy, mood_sad, mood_party, danceability, gender, tonality, voice_instrumental, plus discogs-effnet genre classification. Full prototype coverage — no subset.

### Result Storage
- **D-03:** Store the top-level summary in AnalysisResult columns (bpm, mood, style) and all raw model predictions in the `features` JSONB column. This preserves all 33-model outputs for later use while keeping the most useful fields queryable as typed columns.

### Model File Management
- **D-04:** Claude's discretion on how to manage the ~33 .pb model files and their JSON metadata. Options include: Docker volume mount, download at build time, or bundled in image. The models are publicly available from essentia.upf.edu.

### Worker Integration
- **D-05:** Analysis logic runs through the existing `process_file` function in `tasks/functions.py`, using `run_in_process_pool` for CPU-bound essentia work. One job per file (Phase 4 D-02).

### Claude's Discretion
- How to derive the single `mood` and `style` string values from the multi-model outputs (e.g., highest-confidence label across mood models, highest-confidence genre from discogs)
- Essentia installation approach in Docker (pip vs system packages vs C++ compilation)
- Whether to add essentia to pyproject.toml or treat as a system dependency
- Model download script or Dockerfile RUN step
- How to handle files that essentia can't process (unsupported format, corrupt audio)
- Whether to add `musical_key` detection via essentia's `KeyExtractor`

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, code quality rules (NOTE: librosa recommendation overridden by D-01)
- `.planning/PROJECT.md` — Project vision, constraints
- `.planning/REQUIREMENTS.md` — ANL-01 (BPM detection), ANL-02 (mood/style classification)

### Existing Code
- `src/phaze/models/analysis.py` — AnalysisResult model (bpm, musical_key, mood, style, fingerprint, features JSONB)
- `src/phaze/tasks/functions.py` — process_file skeleton (Phase 4), ready for analysis logic
- `src/phaze/tasks/pool.py` — ProcessPoolExecutor helper for CPU-bound work
- `src/phaze/tasks/worker.py` — WorkerSettings with on_startup/on_shutdown hooks
- `src/phaze/config.py` — Settings with worker_* fields

### Prototype Code (MUST READ)
- `prototype/code/bpm-genre.py` — Essentia discogs-effnet genre classification per minute
- `prototype/code/characteristics.py` — Essentia mood/style classification with 11 ModelSets (33 models total)
- `prototype/code/genre.py` — Essentia TensorflowPredictMusiCNN genre extraction
- `prototype/code/models.txt` — URLs for all 33 TensorFlow model files (.pb + .json)

### Prior Phase Context
- `.planning/phases/04-task-queue-worker-infrastructure/04-CONTEXT.md` — Worker decisions (D-01 through D-04)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `AnalysisResult` model — already has all needed columns (bpm, mood, style, features JSONB)
- `process_file` skeleton — ready for analysis logic injection
- `run_in_process_pool` — async wrapper for CPU-bound essentia work
- ProcessPoolExecutor lifecycle in worker startup/shutdown hooks
- Prototype `Predictor` class and `ModelSet`/`Model` dataclasses — can be adapted directly

### Established Patterns
- arq job functions in `tasks/functions.py`
- Retry with exponential backoff via `arq.Retry`
- Async session access via `get_session` dependency
- SQLAlchemy 2.0 async queries

### Integration Points
- Fill in `process_file` body with real analysis logic
- Add essentia dependency (pyproject.toml or Dockerfile)
- Download/mount model files in Docker
- New `src/phaze/services/analysis.py` for analysis business logic
- New API endpoint for triggering analysis jobs (optional, could reuse scan pattern)

</code_context>

<specifics>
## Specific Ideas

- Prototype `characteristics.py` has a clean `Predictor` context manager pattern — adapt this for the service layer
- 11 model sets with 3 model variants each (MusiCNN MSD, MusiCNN MTT, VGGish) — average predictions across variants for robustness
- BPM detection via essentia's `RhythmExtractor2013` — well-established algorithm
- Model files are ~5-10MB each, ~33 files total = ~200-300MB — significant Docker image consideration

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 05-audio-analysis-pipeline*
*Context gathered: 2026-03-28*
