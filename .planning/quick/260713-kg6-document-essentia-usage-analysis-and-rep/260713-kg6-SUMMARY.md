---
quick_id: 260713-kg6
slug: document-essentia-usage-analysis-and-rep
date: 2026-07-13
status: complete
---

# Quick Task 260713-kg6 — Summary

## What was done

Authored `docs/essentia-analysis.md` — a decision-record documenting this session's
investigation into essentia usage and whether a less compute-intensive replacement
exists without losing features. Added an index row under the Reference section of
`docs/README.md`.

## Key content captured

- **Usage map** — the entire essentia *compute* surface is `services/analysis.py`
  (two-tier windowed FINE/COARSE passes); all other references are plumbing
  (weights download, PVC mount, deferred import).
- **Compute profile** — wall-clock is DSP + decode bound (`RhythmExtractor2013`
  multifeature dominates); the 34 TF models are a negligible slice. GPU/Coral don't
  help; horizontal CPU parallelism is the throughput lever.
- **Feature surface to preserve** — `bpm`, `musical_key`, `mood`, `style`,
  `danceability`, and the full `features` JSONB (incl. `gender`, `tonality`,
  `voice_instrumental`) fed verbatim to the LLM; plus the coverage contract. Noted
  the `aggregate_bpm` `confidence != 0.0` coupling.
- **Replacement landscape (web-researched)** — no lighter drop-in: librosa slower,
  madmom/MIRFLEX heavier, aubio faster-DSP-only (no classifiers, no confidence).
  Essentia is itself documented as speed/memory-optimized; the high-level
  classifiers are effectively the Essentia-models ecosystem.
- **Ranked recommendations** — #1 retune tempo method (degara/Percival/TempoCNN)
  with the confidence-filter caveat + parity validation; #2 prune classifier
  variants (footprint, not wall-clock); #3 decode-once = NOT worth it standalone
  (window sizes are load-bearing in opposite directions); #4 ONNX export (footprint/
  dependency, not wall-clock). Plus a "what NOT to do" (librosa swap, GPU/Coral).
- **Sources** — essentia docs (Context7-verified tempo facts), ISMIR/TISMIR papers,
  MIRFLEX, aubio, madmom.

## Scope

Analysis-only. No source-code behavior changes. Any implementation (e.g. the #1
tempo retune) is a separate GSD phase gated on parity validation via the existing
`scripts/parity/compare_analysis.py`.

## Files changed

- `docs/essentia-analysis.md` (new)
- `docs/README.md` (index row added)
