# Phase 5: Audio Analysis Pipeline - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-28
**Phase:** 05-audio-analysis-pipeline
**Areas discussed:** Analysis library choice, Model scope

---

## Analysis Library Choice

| Option | Description | Selected |
|--------|-------------|----------|
| Librosa only (Recommended for v1) | Librosa for BPM, skip essentia mood/style for now. Simpler. | |
| Both librosa + essentia | Librosa for BPM, essentia for mood/style per prototypes. | |
| Essentia for everything | All-in on essentia including BPM. Matches prototype approach. | ✓ |
| You decide | Let Claude pick. | |

**User's choice:** Essentia for everything
**Notes:** User wants to use existing prototype code directly. Deviates from CLAUDE.md librosa recommendation.

---

## Model Scope

| Option | Description | Selected |
|--------|-------------|----------|
| All 33 models (full prototype) | Run every model set from prototype: 11 sets x 3 models. | ✓ |
| Core subset (~12 models) | BPM + key moods + danceability + genre. Skip gender, tonality, voice_instrumental. | |
| You decide | Let Claude pick a reasonable subset. | |

**User's choice:** All 33 models (full prototype)
**Notes:** Full prototype coverage, no subset.

---

## Claude's Discretion

- Model file management strategy (Docker volume, build-time download, bundled)
- Result summarization (how to derive single mood/style from 33-model output)
- Essentia installation approach in Docker
- Error handling for unprocessable files
- Musical key detection inclusion
