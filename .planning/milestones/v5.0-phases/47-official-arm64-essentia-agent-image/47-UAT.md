---
status: complete
phase: 47-official-arm64-essentia-agent-image
source: [47-01-SUMMARY.md, 47-02-SUMMARY.md, 47-03-SUMMARY.md, 47-04-SUMMARY.md]
started: 2026-06-24
updated: 2026-06-24
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test — native arm64 image build + boot
expected: Dockerfile.agent-arm64 builds from scratch on native arm64; import-smoke (phaze.tasks.agent_worker + essentia.standard) exits 0. Proves CLOUDIMG-01 boot/import locally.
result: pass
note: |
  Found 2 blockers on first live attempt (both fixed inline, commit ff37352, then re-verified):
  (a) BUILD blocker — `uv export --frozen` emitted cp314-only hashes from uv.lock;
      install onto the image's cp313 interpreter failed with a psycopg-binary Hash
      mismatch. Fix: `--no-hashes`. Image now builds: 1.26 GB linux/arm64.
  (b) BOOT blocker — CI import-smoke imported phaze.tasks.agent_worker without
      PHAZE_AGENT_QUEUE, which raises at module import (Phase 26 D-16); CI import-smoke
      would have failed every run. Fix: pass throwaway -e PHAZE_AGENT_QUEUE.
  Re-verified: `docker run … import phaze.tasks.agent_worker; import essentia.standard` → IMPORT-SMOKE OK.

### 2. Real-audio analyze_file / fix #4 (dual-OpenMP) does not segfault
expected: Inside the built image, dump_analysis.py runs the REAL analyze_file over reference.wav with OMP_NUM_THREADS=1 baked in, emitting comparable JSON without SIGSEGV. First real-audio proof of spike fix #4.
result: pass
note: |
  DUMP_EXIT=0, no segfault. Output is numerically correct against the synthetic clip:
  bpm=120.0 (clip is 120 BPM — exact), musical_key="C major" (clip is a C-major triad — exact),
  mood=party, style=Electronic/Techno, danceability=0.783, plus 12 model feature outputs.
  (Required bumping the local colima VM 2 GiB → 8 GiB; TF model load OOM-killed at 2 GiB.
  Local-only constraint — CI ubuntu-24.04-arm runners have ~16 GB.)

### 3. Numeric parity comparator over real output
expected: compare_analysis.py compares real analyze_file output with bpm+key exact and scores within epsilon, treating None/missing as failures (no silent pass).
result: pass
note: |
  Self-compare of the real arm64 dump → "PARITY OK" exit 0. Perturbed bpm (120→125) →
  "PARITY FAIL (1 mismatch)" exit 1. The gate genuinely gates on real analyze_file output.

### 4. Parity comparator unit suite
expected: `uv run pytest tests/test_parity/` — bpm/key exact, scores epsilon, None-vs-number + missing-key are failures. 14 tests green.
result: pass

### 5. hadolint clean on Dockerfile.agent-arm64
expected: `hadolint Dockerfile.agent-arm64` reports no findings (reproducible, pinned, non-root build).
result: pass

### 6. Documentation accuracy
expected: docs/arm64-agent-image.md documents the 3.13 pin, the 4 fixes, and the build + parity commands.
result: pass

### 7. Operator just recipes
expected: `just --list` shows image-build-arm64, image-push-arm64, parity-check, parity-dump, parity-golden-regen with self-documenting descriptions.
result: pass

### 8. CI wiring (tags, gating, OCI labels)
expected: docker-publish.yml has build-arm64 (ubuntu-24.04-arm), parity-golden-x86, and parity-guard (needs both, compare ordered before gated push); -arm64 tag strategy + OCI labels. Actual CI execution proven on first arm64 runner.
result: pass

## Summary

total: 8
passed: 8
issues: 0
pending: 0
skipped: 0

resolved_during_uat: 2  # both blockers fixed inline + re-verified (commit ff37352)

## Gaps

[none open] — 2 blockers (build hash-mismatch, import-smoke missing env) were found
by live UAT, fixed inline, and re-verified on this native arm64 host. No gap-closure
plan needed.
