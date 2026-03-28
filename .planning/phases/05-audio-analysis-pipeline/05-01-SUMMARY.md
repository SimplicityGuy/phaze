---
phase: 05-audio-analysis-pipeline
plan: 01
subsystem: infra
tags: [essentia-tensorflow, ml-models, docker, audio-analysis]

# Dependency graph
requires:
  - phase: 04-task-queue-worker-infrastructure
    provides: worker infrastructure, arq task queue, process pool
provides:
  - essentia-tensorflow dependency in pyproject.toml
  - model download script for 68 essentia ML model files
  - Dockerfile with model download layer
  - models_path configuration setting
affects: [05-02, audio-analysis-service, worker]

# Tech tracking
tech-stack:
  added: [essentia-tensorflow, numpy]
  patterns: [model download at Docker build time, flat model directory]

key-files:
  created:
    - scripts/download_models.sh
    - .dockerignore
  modified:
    - pyproject.toml
    - src/phaze/config.py
    - tests/test_config_worker.py
    - .env.example
    - Dockerfile
    - docker-compose.yml
    - .gitignore
    - justfile

key-decisions:
  - "Flat model directory structure (no subdirs) matching prototype pattern"
  - "Models baked into Docker image at build time (no runtime volume needed)"
  - "Added .dockerignore to prevent local models dir from being copied into image"

patterns-established:
  - "Model download script: scripts/download_models.sh with skip-existing and retry logic"
  - "Docker layer ordering: deps -> models -> source for optimal caching"

requirements-completed: [ANL-01, ANL-02]

# Metrics
duration: 5min
completed: 2026-03-28
---

# Phase 5 Plan 1: Essentia-TensorFlow Dependency and Model Infrastructure Summary

**essentia-tensorflow dependency with 68-file model download script baked into Docker image, plus models_path config**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-28T18:48:51Z
- **Completed:** 2026-03-28T18:54:08Z
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments
- Added essentia-tensorflow and numpy as project dependencies, installable via uv sync
- Created download script covering all 34 .pb model files and 34 .json metadata files from essentia.upf.edu
- Updated Dockerfile with curl install and model download cache layer
- Added models_path config setting with /models default, tested and wired into docker-compose

## Task Commits

Each task was committed atomically:

1. **Task 1: Add essentia-tensorflow dependency and models_path config** - `49d6195` (feat)
2. **Task 2: Create model download script and update Dockerfile and docker-compose** - `8a89eca` (feat)

## Files Created/Modified
- `pyproject.toml` - Added essentia-tensorflow and numpy dependencies
- `src/phaze/config.py` - Added models_path setting with /models default
- `tests/test_config_worker.py` - Added test_models_path_default test case
- `.env.example` - Added MODELS_PATH documentation
- `scripts/download_models.sh` - Downloads 68 model files from essentia.upf.edu
- `Dockerfile` - Added curl install, model download layer between deps and source
- `docker-compose.yml` - Added MODELS_PATH=/models to worker environment
- `.gitignore` - Added models/ exclusion
- `.dockerignore` - Created to exclude local models, .git, prototype, etc.
- `justfile` - Added download-models command

## Decisions Made
- Flat model directory structure (no subdirectories) to match prototype code pattern where models are loaded by filename only
- Models baked into Docker image at build time rather than using runtime volumes -- simpler and ensures models are always available
- Created .dockerignore to prevent accidentally copying local model files into Docker context
- Docker layer ordering: dependencies -> models -> source code for optimal build caching

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Added .dockerignore file**
- **Found during:** Task 2
- **Issue:** Plan mentioned adding models to .dockerignore if it exists, but .dockerignore did not exist. Without it, Docker context includes .git, prototype, .planning, and other unnecessary files.
- **Fix:** Created .dockerignore with comprehensive exclusions
- **Files modified:** .dockerignore
- **Verification:** File created and committed
- **Committed in:** 8a89eca (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 missing critical)
**Impact on plan:** Essential for Docker build efficiency. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- essentia-tensorflow installed and importable
- Model download script ready for Docker build or local use via `just download-models`
- models_path config wired through Settings -> docker-compose
- Ready for 05-02 to implement the audio analysis service using these models

---
*Phase: 05-audio-analysis-pipeline*
*Completed: 2026-03-28*
