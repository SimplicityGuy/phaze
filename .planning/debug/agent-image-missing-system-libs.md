---
slug: agent-image-missing-system-libs
status: open
trigger: After v4.0.8 payload fix, process_file jobs reach analysis code but fail at `import essentia` with `ImportError: libatomic.so.1: cannot open shared object file`. Agent image lacks all audio system libs.
created: 2026-06-10
updated: 2026-06-10
---

# Debug Session: agent-image-missing-system-libs

## Symptoms

- **Expected:** With v4.0.8 deployed (payload fix), `process_file` jobs validate, the agent worker runs essentia, writes `analysis` rows, and files leave `discovered`.
- **Actual:** Every `process_file` job fails at `src/phaze/services/analysis.py:17` `import essentia` → `from . import _essentia` with `ImportError: libatomic.so.1: cannot open shared object file: No such file or directory`. Jobs retry 4× and dead-letter. Files stay `discovered`; `analysis` has 0 rows.
- **Latent history:** Masked by two upstream bugs. First the default-queue misrouting (Phase 30) meant jobs never reached a consumer. Then the v4.0.8 payload bug failed jobs at `ProcessFilePayload` validation before the analysis import. Now both are fixed, jobs reach `import essentia` for the first time and expose that essentia's native extension was never loadable.

## Live evidence (2026-06-10)

- nox `phaze-agent-worker` on `ghcr.io/simplicityguy/phaze:v4.0.8`. **0** `ProcessFilePayload` validation errors — payload fix confirmed working; jobs carry full payload (`file_id, original_path, file_type, agent_id, models_path`).
- Triggered `POST /api/v1/analyze` → `{"enqueued":5000,...}`. Jobs routed correctly to `phaze-agent-nox`, processed, and ALL failed on the essentia import.
- DB unchanged after trigger: `public.files` 5000 rows all `discovered`; `public.analysis` 0 rows.
- In-container probe of `phaze-agent-worker`: `ffmpeg MISSING, ffprobe MISSING, fpcalc MISSING, libatomic.so.1 MISSING, libsndfile.so.1 MISSING, libchromaprint.so.1 MISSING`.

## Root Cause

The main `Dockerfile` (shared by `phaze-api`, `phaze-worker`, `phaze-agent-worker` — all `ghcr.io/simplicityguy/phaze`) is `FROM python:3.14-slim` and runs **no `apt-get install`** at all. It installs only Python deps via `uv sync`. None of the audio pipeline's required system packages are present:
- `libatomic1` → `libatomic.so.1` (essentia-tensorflow native `_essentia` ext) — current hard blocker at import
- `ffmpeg` → `ffmpeg`/`ffprobe` (audio decode, video stream metadata)
- `libchromaprint-tools` → `fpcalc` + `libchromaprint.so.1` (pyacoustid fingerprinting)
- `libsndfile1` → `libsndfile.so.1` (audio file IO)

This contradicts the documented stack ("apt-get install -y ffmpeg chromaprint-tools" in Dockerfile) — that layer was never actually added.

## Fix (proposed, not yet applied)

Add a runtime system-deps layer to `Dockerfile` before the non-root user, e.g.:
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        libatomic1 ffmpeg libsndfile1 libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*
```
Then release v4.0.9 and redeploy nox + lux. Even once essentia imports, ffmpeg/fpcalc would be the next failures downstream, so install all four together.

## Verification plan

- Rebuild image; in-container `ldconfig -p` shows libatomic/libsndfile/libchromaprint present; `command -v ffmpeg ffprobe fpcalc` all resolve.
- `python -c "import essentia"` succeeds in the venv.
- Re-trigger `/api/v1/analyze`; agent worker logs show jobs completing (no ImportError); `public.analysis` row count climbs; files leave `discovered`.

## Eliminated

- Payload shape (v4.0.8) — ELIMINATED: 0 validation errors, full payload present.
- Queue misrouting (Phase 30) — ELIMINATED: jobs reach `phaze-agent-nox` and execute.
- Deploy/version skew — ELIMINATED: both hosts on v4.0.8.
