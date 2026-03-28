# Phase 5: Audio Analysis Pipeline - Research

**Researched:** 2026-03-28
**Domain:** Audio analysis with essentia (BPM, mood, style classification via TensorFlow models)
**Confidence:** MEDIUM-HIGH

## Summary

Phase 5 fills in the `process_file` skeleton from Phase 4 with real audio analysis logic using essentia (not librosa -- per user decision D-01). The existing prototype code in `prototype/code/characteristics.py` and `prototype/code/bpm-genre.py` provides a complete reference implementation that needs to be adapted into the service layer pattern, wired through `run_in_process_pool`, and connected to the `AnalysisResult` model.

The critical technical consideration is the `essentia-tensorflow` package: it bundles TensorFlow inside the wheel (~291MB), requires 34 pre-trained model files (~200-300MB of `.pb` + `.json` files), and needs audio loaded at specific sample rates (16kHz for TF models, 44.1kHz for RhythmExtractor2013). The Docker image will grow significantly. Model files should be downloaded at build time or volume-mounted to avoid baking them into the image layer.

**Primary recommendation:** Add `essentia-tensorflow` as a project dependency in `pyproject.toml`. Create a model download script for Docker builds. Implement analysis as a synchronous function callable via `run_in_process_pool`. Store all 34-model outputs in the `features` JSONB column, derive `mood`/`style`/`bpm`/`musical_key` summary columns from the raw predictions.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Use **essentia** for all analysis -- BPM, mood, style, genre. No librosa. Deviates from CLAUDE.md recommendation but aligns with existing prototype code.
- **D-02:** Run all 33 models from the prototype (11 model sets x 3 models each) plus discogs-effnet genre classification. Full prototype coverage -- no subset.
- **D-03:** Store top-level summary in AnalysisResult columns (bpm, mood, style) and all raw model predictions in the `features` JSONB column.
- **D-04:** Claude's discretion on model file management (~34 .pb files + .json metadata).
- **D-05:** Analysis runs through existing `process_file` in `tasks/functions.py`, using `run_in_process_pool` for CPU-bound essentia work. One job per file.

### Claude's Discretion
- How to derive single `mood` and `style` strings from multi-model outputs
- Essentia installation approach in Docker (pip vs system packages)
- Whether to add essentia to pyproject.toml or treat as system dependency
- Model download script or Dockerfile RUN step
- How to handle files essentia can't process (unsupported format, corrupt audio)
- Whether to add `musical_key` detection via essentia's `KeyExtractor`

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ANL-01 | System detects BPM for music files using existing prototypes | essentia `RhythmExtractor2013` at 44.1kHz sample rate; prototype `bpm-genre.py` shows pattern; store in `AnalysisResult.bpm` |
| ANL-02 | System classifies mood and style for music files using existing prototypes | essentia TF models (MusiCNN, VGGish, EffnetDiscogs) at 16kHz; prototype `characteristics.py` has full 33-model Predictor class; store mood/style summaries + raw features JSONB |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| essentia-tensorflow | 2.1b6.dev1389 | Audio analysis + TF model inference | Bundles TensorFlow within wheel. Provides MonoLoader, RhythmExtractor2013, KeyExtractor, TensorflowPredictMusiCNN, TensorflowPredictVGGish, TensorflowPredictEffnetDiscogs. Python 3.13 wheels available. |
| numpy | (transitive) | Array operations for activation averaging | Pulled in by essentia-tensorflow. Used for `np.mean(activations, axis=0)` and `np.argsort`. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| six | (transitive) | Python 2/3 compat (essentia dep) | Pulled in automatically. No direct usage needed. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| essentia-tensorflow (291MB wheel) | essentia (14MB) + separate tensorflow | Smaller base wheel but more complex dependency management. The bundled approach is simpler and verified to work. |

**Installation:**
```bash
uv add essentia-tensorflow
```

**Version verification:** essentia-tensorflow 2.1b6.dev1389 confirmed on PyPI with cp313-manylinux_2_17_x86_64 wheel (291MB). Published 2025-07-24. Only dependencies are numpy and six.

**Docker note:** The essentia-tensorflow wheel bundles FFmpeg libraries for audio loading. No separate `apt-get install ffmpeg` needed for essentia's MonoLoader. However, FFmpeg/ffprobe may still be needed for other future phases.

## Architecture Patterns

### Recommended Project Structure
```
src/phaze/
  services/
    analysis.py          # Analysis service (business logic)
  tasks/
    functions.py         # process_file (updated with analysis calls)
    pool.py              # run_in_process_pool (existing)
    worker.py            # WorkerSettings (existing)
  config.py              # Settings (add models_path)
  models/
    analysis.py           # AnalysisResult (existing, no changes needed)
scripts/
  download_models.sh      # Model download script for Docker/dev
```

### Pattern 1: Synchronous Analysis Function for ProcessPoolExecutor
**What:** All essentia analysis logic in a single synchronous function that takes a file path and returns a results dict. This function runs inside `run_in_process_pool`.
**When to use:** Always -- essentia is CPU-bound and blocks the event loop.
**Example:**
```python
# Source: Adapted from prototype/code/characteristics.py pattern
def analyze_file(file_path: str, models_dir: str) -> dict:
    """Synchronous analysis -- runs in ProcessPoolExecutor.

    Returns dict with bpm, musical_key, mood, style, features.
    """
    import essentia.standard as es
    import numpy as np

    # BPM detection (44.1kHz required by RhythmExtractor2013)
    audio_44k = es.MonoLoader(filename=file_path, sampleRate=44100)()
    rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
    bpm, beats, beats_confidence, _, beats_intervals = rhythm_extractor(audio_44k)

    # Key detection
    key_extractor = es.KeyExtractor(profileType="edma")
    key, scale, strength = key_extractor(audio_44k)
    musical_key = f"{key} {scale}"

    # TF model predictions (16kHz required)
    audio_16k = es.MonoLoader(filename=file_path, sampleRate=16000)()
    # ... run all 34 models, collect predictions ...

    return {
        "bpm": float(bpm),
        "musical_key": musical_key,
        "mood": derive_mood(features),
        "style": derive_style(genre_features),
        "features": all_raw_predictions,
    }
```

### Pattern 2: Model Registry with Lazy Loading
**What:** Define all 34 models (11 mood/characteristic sets x 3 variants + 1 discogs-effnet) as a registry. Load model files lazily on first use. Models persist in memory within the ProcessPoolExecutor worker process.
**When to use:** Avoid reloading ~34 TF model graphs per file.
**Example:**
```python
# Source: Adapted from prototype/code/characteristics.py Model class
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class ModelConfig:
    name: str
    variant: str  # "musicnn_msd", "musicnn_mtt", "vggish"
    filename: str  # e.g., "mood_acoustic-musicnn-msd-2"
    classifier_type: str  # "musicnn", "vggish", "effnet_discogs"

@dataclass
class ModelSetConfig:
    name: str
    models: list[ModelConfig]

# Registry: all 11 characteristic model sets + discogs-effnet
MODEL_SETS: list[ModelSetConfig] = [
    ModelSetConfig("mood_acoustic", [...]),
    # ... all 11 sets ...
]
GENRE_MODEL = ModelConfig(
    "discogs_genre", "effnet", "discogs-effnet-bs64-1", "effnet_discogs"
)
```

### Pattern 3: Mood/Style Derivation from Multi-Model Outputs
**What:** Derive single `mood` and `style` strings from raw predictions.
**When to use:** Populating AnalysisResult.mood and AnalysisResult.style.
**Recommendation:**
- **mood:** For each mood model set (acoustic, electronic, aggressive, relaxed, happy, sad, party), average the positive-class prediction across the 3 variants (MusiCNN MSD, MusiCNN MTT, VGGish). Pick the mood with the highest averaged confidence. Format: `"happy"` (lowercase).
- **style:** From discogs-effnet, take `np.mean(activations, axis=0)`, find the top label. Format: `"Electronic---House"` becomes `"Electronic/House"` (replace `---` with `/`).
- **musical_key:** Include KeyExtractor with `profileType="edma"` (tuned for electronic/dance music -- appropriate for this collection). Format: `"C minor"`, `"A major"`.

### Anti-Patterns to Avoid
- **Loading models per-file:** Each TF model graph load takes seconds. Models must be loaded once and reused across files within the same process. Use module-level or class-level caching.
- **Loading audio twice unnecessarily:** The file must be loaded at 44.1kHz (for BPM/key) and 16kHz (for TF models). These are two separate loads but don't load more than twice.
- **Blocking the async event loop:** Never call essentia directly from an async context. Always go through `run_in_process_pool`.
- **Swallowing errors silently:** If essentia fails on a file (corrupt, unsupported format), raise an exception so arq retry logic can handle it. After max retries, the file should be marked as failed.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| BPM detection | Custom beat tracking | `es.RhythmExtractor2013(method="multifeature")` | Proven algorithm with multifeature voting. Handles tempo ambiguity (120 vs 60 BPM). |
| Key detection | Chroma-based key finder | `es.KeyExtractor(profileType="edma")` | Combines HPCP + key profiles. edma profile tuned for electronic music. |
| Audio loading/resampling | Manual ffmpeg subprocess | `es.MonoLoader(filename=..., sampleRate=...)` | Handles mp3, m4a, ogg, wav, flac. Auto-downmixes stereo. Auto-resamples. |
| TF model inference | Raw TensorFlow session | `es.TensorflowPredictMusiCNN`, `es.TensorflowPredictVGGish`, `es.TensorflowPredictEffnetDiscogs` | Correct input/output tensor names, batch handling, activation extraction all handled. |
| Model download | Manual curl commands | Scripted download with checksum verification | 68 files from essentia.upf.edu. Script ensures completeness and integrity. |

## Common Pitfalls

### Pitfall 1: Sample Rate Mismatch
**What goes wrong:** RhythmExtractor2013 produces garbage BPM values.
**Why it happens:** RhythmExtractor2013 requires exactly 44100Hz input. TF models require 16000Hz. Using the wrong sample rate for either produces wrong results silently (no error thrown).
**How to avoid:** Load audio twice: `MonoLoader(sampleRate=44100)` for BPM/key, `MonoLoader(sampleRate=16000)` for TF models. Document sample rate requirements in function docstrings.
**Warning signs:** BPM values that are exactly half or double expected, or wildly off.

### Pitfall 2: Model File Not Found at Runtime
**What goes wrong:** `RuntimeError` when TF model graph file doesn't exist at the expected path.
**Why it happens:** Model files are external assets not in the Python package. Docker builds may not have downloaded them, or the path configuration is wrong.
**How to avoid:** Add a `models_path` setting to `config.py`. Validate model directory existence at worker startup (in `on_startup` hook). Fail fast with clear error message listing missing files.
**Warning signs:** Worker starts but every job fails immediately.

### Pitfall 3: Memory Pressure from 34 Loaded Models
**What goes wrong:** OOM kills or extreme memory usage in worker processes.
**Why it happens:** Each TF model graph occupies memory. 34 models loaded simultaneously could consume several GB per process. With `worker_process_pool_size=4`, that's 4x the memory.
**How to avoid:** Load models lazily (on first use) within each process. Consider reducing `worker_process_pool_size` to 2 for the analysis worker. Monitor memory. The ProcessPoolExecutor workers are long-lived (created at startup, not per-job), so model loading happens once per worker process.
**Warning signs:** Docker container OOM-killed, processes consuming >2GB each.

### Pitfall 4: essentia TF Environment Variable Noise
**What goes wrong:** TensorFlow logging floods stdout with INFO/WARNING messages.
**Why it happens:** TF defaults to verbose logging. essentia also has its own logging.
**How to avoid:** Set `os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"` before importing essentia. Also set `essentia.log.infoActive = False` and `essentia.log.warningActive = False`. The prototype code already does this -- replicate the pattern.
**Warning signs:** Log files growing rapidly with TF/essentia noise.

### Pitfall 5: ProcessPoolExecutor Initialization Timing
**What goes wrong:** Models are re-loaded for every file because each `run_in_process_pool` call creates state from scratch.
**Why it happens:** ProcessPoolExecutor workers don't share state with the main process. Module-level globals in the child process are fresh.
**How to avoid:** Use a module-level model cache in the analysis module. Since ProcessPoolExecutor reuses worker processes, the first call loads models and subsequent calls find them already loaded. Alternatively, use an `initializer` function on the ProcessPoolExecutor, but the lazy-loading approach is simpler.
**Warning signs:** Analysis taking 30+ seconds per file (model load) vs 2-5 seconds (inference only).

### Pitfall 6: Corrupt or Unsupported Audio Files
**What goes wrong:** essentia throws exceptions on corrupt files, very short files, or unsupported codecs.
**Why it happens:** Real-world music collections contain broken files, DRM-protected files, or unusual formats.
**How to avoid:** Wrap the analysis function in try/except. On failure, let arq retry logic handle transient errors. After max retries, store a failed status. Consider checking `file_type == "music"` before enqueuing (video files shouldn't go through audio analysis).
**Warning signs:** Many jobs stuck in retry loops.

## Code Examples

### BPM and Key Detection
```python
# Source: essentia docs + prototype/code/bpm-genre.py
import essentia.standard as es

def detect_bpm_and_key(file_path: str) -> dict:
    """Detect BPM and musical key. Requires 44.1kHz audio."""
    audio = es.MonoLoader(filename=file_path, sampleRate=44100)()

    # BPM
    rhythm = es.RhythmExtractor2013(method="multifeature")
    bpm, beats, beats_confidence, _, beats_intervals = rhythm(audio)

    # Key (edma profile for electronic/dance music)
    key_ext = es.KeyExtractor(profileType="edma")
    key, scale, strength = key_ext(audio)

    return {
        "bpm": round(float(bpm), 1),
        "musical_key": f"{key} {scale}",
        "key_strength": float(strength),
        "beats_confidence": float(beats_confidence),
    }
```

### Mood/Style Classification with TF Models
```python
# Source: prototype/code/characteristics.py Predictor pattern
import essentia.standard as es
import numpy as np

def classify_with_model(audio_16k, model_path: str, classifier_type: str) -> np.ndarray:
    """Run a single TF model and return mean activations."""
    if classifier_type == "musicnn":
        predictor = es.TensorflowPredictMusiCNN(graphFilename=model_path)
    elif classifier_type == "vggish":
        predictor = es.TensorflowPredictVGGish(graphFilename=model_path)
    elif classifier_type == "effnet_discogs":
        predictor = es.TensorflowPredictEffnetDiscogs(graphFilename=model_path)
    else:
        raise ValueError(f"Unknown classifier type: {classifier_type}")

    activations = predictor(audio_16k)
    return np.mean(activations, axis=0)
```

### Discogs Genre Extraction
```python
# Source: prototype/code/bpm-genre.py
import json
import essentia.standard as es
import numpy as np

def extract_genre(audio_16k, model_dir: str, top_n: int = 5) -> dict:
    """Extract top-N genres from discogs-effnet model."""
    json_path = f"{model_dir}/discogs-effnet-bs64-1.json"
    model_path = f"{model_dir}/discogs-effnet-bs64-1.pb"

    with open(json_path) as f:
        metadata = json.load(f)

    labels = [l.replace("---", "/") for l in metadata["classes"]]
    model = es.TensorflowPredictEffnetDiscogs(graphFilename=model_path)
    activations = model(audio_16k)
    activations_mean = np.mean(activations, axis=0)
    top_idx = np.argsort(activations_mean)[::-1][:top_n]

    return {
        "top_genres": [
            {"label": labels[i], "confidence": float(activations_mean[i])}
            for i in top_idx
        ],
        "style": labels[top_idx[0]],  # Top genre as style summary
    }
```

### process_file Integration
```python
# Source: Existing tasks/functions.py pattern + pool.py
async def process_file(ctx: dict, file_id: str) -> dict:
    """Process a single file through the analysis pipeline."""
    # 1. Fetch file record from DB (need path, file_type)
    # 2. Skip non-music files
    # 3. Run CPU-bound analysis in process pool
    result = await run_in_process_pool(
        ctx, analyze_file, file_path, models_dir
    )
    # 4. Upsert AnalysisResult with bpm, mood, style, musical_key, features
    # 5. Update FileRecord.state to ANALYZED
    return {"file_id": file_id, "status": "analyzed"}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| librosa for BPM | essentia RhythmExtractor2013 | User decision D-01 | essentia handles BPM, mood, style -- single library for all analysis |
| Separate TensorFlow install | essentia-tensorflow bundles TF in wheel | essentia 2.1b6.dev1389 (2025-07-24) | No need to manage TF dependency separately |
| essentia C++ compilation | Pre-built pip wheels for Python 3.13 | essentia 2.1b6.dev1389 | Simple `pip install essentia-tensorflow` with manylinux wheels |

## Open Questions

1. **Model file storage strategy**
   - What we know: ~68 files (.pb + .json), ~200-300MB total, publicly available from essentia.upf.edu
   - What's unclear: Whether to bake into Docker image (larger image, self-contained) or volume-mount (smaller image, external dependency)
   - Recommendation: Download script in `scripts/download_models.sh` + Docker volume mount. Add a `models_path` setting defaulting to `/models`. Dockerfile RUN step downloads during build as fallback. This gives flexibility for both dev and prod.

2. **Memory budget per worker process**
   - What we know: 34 TF models loaded into memory, each several MB
   - What's unclear: Exact memory footprint per process with all models loaded
   - Recommendation: Start with `worker_process_pool_size=2` for analysis workers. Monitor memory. Can increase if headroom allows. Document memory requirements.

3. **File type filtering before analysis**
   - What we know: `FileRecord.file_type` exists with values like "music", "video", "companion"
   - What's unclear: Should `process_file` filter or should the enqueuing logic filter?
   - Recommendation: Both. Enqueuing should only enqueue music files. `process_file` should also check as a safety guard and skip non-music files without error.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.13 | Runtime | Yes | 3.13 | -- |
| uv | Package management | Yes | installed | -- |
| Docker | Deployment | Yes (project infra) | -- | -- |
| PostgreSQL | Data storage | Yes (docker-compose) | 18-alpine | -- |
| Redis | arq broker | Yes (docker-compose) | 8-alpine | -- |
| essentia-tensorflow | Audio analysis | No (not yet installed) | 2.1b6.dev1389 available | -- |
| essentia model files | TF model inference | No (not yet downloaded) | -- | Download script needed |

**Missing dependencies with no fallback:**
- `essentia-tensorflow` must be added to pyproject.toml
- Model files (~68 files) must be downloaded from essentia.upf.edu

**Missing dependencies with fallback:**
- None

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/test_services/test_analysis.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ANL-01 | BPM detected for music files via essentia | unit (mock essentia) | `uv run pytest tests/test_services/test_analysis.py::test_detect_bpm -x` | Wave 0 |
| ANL-01 | BPM stored in AnalysisResult.bpm | unit (DB) | `uv run pytest tests/test_services/test_analysis.py::test_bpm_stored -x` | Wave 0 |
| ANL-02 | Mood classified via 33 TF models | unit (mock essentia) | `uv run pytest tests/test_services/test_analysis.py::test_classify_mood -x` | Wave 0 |
| ANL-02 | Style classified via discogs-effnet | unit (mock essentia) | `uv run pytest tests/test_services/test_analysis.py::test_classify_style -x` | Wave 0 |
| ANL-02 | Results stored in AnalysisResult (mood, style, features) | unit (DB) | `uv run pytest tests/test_services/test_analysis.py::test_analysis_result_stored -x` | Wave 0 |
| ANL-01+02 | process_file runs analysis via process pool | unit (mock) | `uv run pytest tests/test_tasks/test_functions.py::test_process_file_analysis -x` | Wave 0 |
| ANL-01+02 | Failed analysis triggers arq retry | unit | `uv run pytest tests/test_tasks/test_functions.py::test_process_file_retry -x` | Existing (update) |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_services/test_analysis.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/test_analysis.py` -- covers ANL-01, ANL-02 (analysis service unit tests)
- [ ] Update `tests/test_tasks/test_functions.py` -- update process_file tests for real analysis logic
- [ ] Mock strategy for essentia: mock at the `analyze_file` boundary (the sync function passed to `run_in_process_pool`) so tests don't need actual model files or essentia installed

**Testing strategy note:** Since essentia-tensorflow is a 291MB dependency with native C++ extensions, unit tests should mock at the analysis function boundary. Integration tests that actually run essentia can be marked with `@pytest.mark.slow` and skipped in CI unless model files are available.

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively** -- essentia-tensorflow 2.1b6.dev1389 has cp313 wheels (verified)
- **uv only** -- `uv add essentia-tensorflow` to add dependency
- **Pre-commit hooks must pass** -- new code must pass ruff, mypy, bandit
- **85% code coverage minimum** -- mock essentia calls for unit test coverage
- **Every feature gets its own PR** -- Phase 5 work on its own branch
- **Type hints on all functions** -- analysis service functions need full type annotations
- **Mypy strict mode** (excluding tests and prototype/) -- essentia has no type stubs, will need `# type: ignore[import-untyped]`
- **Double quotes, 150 char line length** -- follow existing patterns
- **Ruff rules** -- the enabled rules (especially `S` for bandit, `TCH` for type checking imports) apply to new code

## Sources

### Primary (HIGH confidence)
- [essentia-tensorflow on PyPI](https://pypi.org/project/essentia-tensorflow/) -- version 2.1b6.dev1389 verified, Python 3.13 wheels confirmed, 291MB wheel with bundled TF
- [Essentia models documentation](https://essentia.upf.edu/models.html) -- 16kHz sample rate for all TF models confirmed
- [RhythmExtractor2013 reference](https://essentia.upf.edu/reference/std_RhythmExtractor2013.html) -- 44100Hz requirement confirmed, multifeature method
- [KeyExtractor reference](https://essentia.upf.edu/reference/std_KeyExtractor.html) -- edma profile for electronic music
- Prototype code: `prototype/code/characteristics.py`, `prototype/code/bpm-genre.py` -- working reference implementations

### Secondary (MEDIUM confidence)
- [Essentia installing docs](https://essentia.upf.edu/installing.html) -- general installation guidance
- [MonoLoader docs](https://essentia.upf.edu/reference/streaming_MonoLoader.html) -- FFmpeg-based loading, supports mp3/m4a/ogg/wav/flac
- [Essentia beat detection tutorial](https://essentia.upf.edu/tutorial_rhythm_beatdetection.html) -- BPM detection patterns

### Tertiary (LOW confidence)
- Wheel size data from PyPI JSON API (verified programmatically: 291,541,021 bytes for cp313 manylinux wheel)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- essentia-tensorflow verified on PyPI with Python 3.13 wheels, dependencies confirmed via dry-run
- Architecture: MEDIUM-HIGH -- patterns derived directly from working prototype code, integrated with established Phase 4 patterns
- Pitfalls: MEDIUM -- sample rate issues and model loading concerns are well-documented in essentia docs; memory budget is estimated

**Research date:** 2026-03-28
**Valid until:** 2026-04-28 (essentia releases infrequently; stack is stable)
