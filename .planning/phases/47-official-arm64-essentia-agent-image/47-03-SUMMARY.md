---
phase: 47-official-arm64-essentia-agent-image
plan: 03
subsystem: testing
tags: [parity, essentia, numpy, wave, comparator, ci, arm64, cloudimg]

# Dependency graph
requires:
  - phase: 31-windowed-analysis
    provides: "analyze_file two-tier time-series return dict (bpm/musical_key/mood/style/danceability/features) — the parity schema"
provides:
  - "compare_analysis.compare() — bpm/key-exact + model-score-epsilon comparator over the analyze_file dict (CLI exits non-zero on mismatch)"
  - "dump_analysis.py — shared CLI that runs analyze_file and emits the parity-projected JSON (same tool on x86 golden + arm64 actual)"
  - "scripts/parity/reference.wav — deterministic, byte-reproducible, non-degenerate parity reference clip"
  - "generate_reference.py — RNG-free arithmetic generator that regenerates reference.wav byte-identically"
  - "just parity-golden-regen — operator recipe to regenerate golden-x86.json from the x86 image"
affects: [47-04, parity-guard, docker-publish]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Parity comparator: exact for bpm/key/mood/style, math.isclose(abs_tol) for danceability + recursively-flattened features scores"
    - "Anti-silent-pass: None-vs-number and missing/renamed score keys are FAILURES via .get(), never KeyError (T-47-07)"
    - "Shared single-source dump tool runs inside both images for a byte-identical comparison schema"
    - "Deterministic synthetic audio fixture (no RNG, arithmetic construction) committed for byte-reproducible regen"

key-files:
  created:
    - scripts/parity/compare_analysis.py
    - scripts/parity/dump_analysis.py
    - scripts/parity/generate_reference.py
    - scripts/parity/reference.wav
    - tests/test_parity/__init__.py
    - tests/test_parity/test_compare_analysis.py
  modified:
    - justfile
    - pyproject.toml

key-decisions:
  - "Comparator keys on the REAL analyze_file return dict (features), NOT the RESEARCH example's non-existent model_scores key"
  - "dump_analysis emits NESTED features (the exact shape the comparator unit tests exercise); the comparator is the single flatten authority"
  - "reference.wav is 8 kHz mono 16-bit / 30 s (~480 KB) to stay under the 500 KB check-added-large-files limit; analyze_file resamples internally so source rate is parity-neutral"

patterns-established:
  - "Parity tolerance contract encoded once in compare() and unit-tested without essentia/models"
  - "Synthetic fixture generators are arithmetic + RNG-free for byte-reproducibility"

requirements-completed: [CLOUDIMG-03]

# Metrics
duration: ~20min
completed: 2026-06-24
---

# Phase 47 Plan 03: arm64↔x86 Numeric-Parity Toolkit Summary

**Pure-Python parity comparator (bpm/key exact, model scores within epsilon), a shared analyze_file-dump CLI, and a deterministic byte-reproducible synthetic reference clip — the toolkit plan 47-04's CI uses to gate the arm64 essentia image against an x86 golden.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-06-24
- **Completed:** 2026-06-24
- **Tasks:** 2 (Task 1 via TDD: RED → GREEN)
- **Files modified:** 8 (6 created, 2 modified)

## Accomplishments
- `compare_analysis.compare(golden, actual, *, atol)` — returns a list of human-readable failure strings (`[]` == parity); bpm/musical_key/mood/style exact, danceability + every flattened `features` model score via `math.isclose(abs_tol=atol)`; non-zero-exit CLI for the CI parity guard.
- Anti-silent-pass guarantee (T-47-07): None-vs-number and missing/renamed score keys are failures, never a silent pass and never a `KeyError` — covered by 14 unit tests with no essentia/models.
- `dump_analysis.py` — the single shared tool that runs the real `analyze_file` and projects to the parity key set (`bpm`, `musical_key`, `mood`, `style`, `danceability`, `features`), dropping the variable `windows`/coverage-count keys; `faulthandler` enabled for native-segfault diagnostics.
- `generate_reference.py` + committed `reference.wav` — an arithmetic, RNG-free C-major triad (with harmonics) modulated by a 120 BPM exponential-decay envelope; regenerates byte-identically (sha256 stable across two runs).
- `just parity-golden-regen` — operator path to regenerate `golden-x86.json` from the x86 api image via the shared dump tool (CI in 47-04 remains the authoritative golden producer).

## Reference clip facts (per <output>)
- **sha256:** `d6786a1d3373ca3840aabb62a232a98e86d9bf803b04181723f240061dd96581`
- **Size:** 480044 bytes (under the 500 KB large-file pre-commit limit)
- **Construction:** 30 s, 8 kHz, mono, 16-bit PCM. Harmonic content = C-major triad (C4 261.63 / E4 329.63 / G4 392.00 Hz) with 3 harmonics each (max partial 1176 Hz, below the 4 kHz Nyquist), weights `0.6^(h-1)`. Rhythmic pulse = per-beat exponential-decay amplitude envelope `exp(-6·phase)` retriggered at 120 BPM. Normalized to 0.9 peak before int16 quantization. Fully synthetic, no RNG, license-clean.
- **Parity key set projected by `dump_analysis.py`:** `bpm`, `musical_key`, `mood`, `style`, `danceability`, `features` (nested model-score map). Dropped: `windows`, `fine_windows_analyzed`, `fine_windows_total`, `coarse_windows_analyzed`, `coarse_windows_total`, `sampled`.

## Task Commits

1. **Task 1 (RED): failing comparator tests** - `593ce47` (test)
2. **Task 1 (GREEN): bpm/key-exact + score-epsilon comparator** - `92e2aa6` (feat)
3. **Task 2: shared dump CLI + deterministic reference clip + recipe** - `6c83e31` (feat)

_Task 1 was TDD (test → feat); no refactor commit needed._

## Files Created/Modified
- `scripts/parity/compare_analysis.py` - `compare()` + non-zero-exit CLI; the CLOUDIMG-03 tolerance contract.
- `scripts/parity/dump_analysis.py` - shared `analyze_file` → parity-JSON CLI (x86 golden + arm64 actual).
- `scripts/parity/generate_reference.py` - deterministic arithmetic reference-clip generator.
- `scripts/parity/reference.wav` - committed deterministic parity fixture.
- `tests/test_parity/__init__.py` - parity test package marker.
- `tests/test_parity/test_compare_analysis.py` - 14 comparator unit tests (pass + fail + anti-silent-pass cases).
- `justfile` - `parity-golden-regen` recipe (operator golden regen path).
- `pyproject.toml` - added `"scripts/parity/**" = ["T201"]` ruff per-file-ignore (CLI scripts print).

## Decisions Made
- Comparator keys on the REAL `analyze_file` return dict via `features`, explicitly NOT the RESEARCH "comparison helper (shape)" example's non-existent `model_scores` key (would `KeyError`). Model scores are reached through a recursive numeric flatten of `features`.
- `dump_analysis.py` emits NESTED `features` (the exact shape the comparator unit tests exercise). The comparator is the single flatten authority and is idempotent over flat maps, so there is one source of truth for flattening and zero cross-script imports.
- `reference.wav` is 8 kHz mono 16-bit / 30 s (~480 KB) to fit under the `check-added-large-files` 500 KB limit while keeping the planned ~30 s duration; `analyze_file` resamples internally to its 44.1 kHz / 16 kHz passes, so the lower source rate is parity-neutral (identical on both architectures).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Reference-clip size vs the 500 KB large-file pre-commit limit**
- **Found during:** Task 2 (reference clip generation)
- **Issue:** The plan suggested ~30-45 s; a 30-45 s 44.1 kHz 16-bit mono WAV is 2.6-4.0 MB, which the `check-added-large-files` hook (default 500 KB) would reject, blocking the required commit of `reference.wav`.
- **Fix:** Generated the clip at 8 kHz mono 16-bit / 30 s (~480 KB). All partials stay below the 4 kHz Nyquist; `analyze_file` resamples internally, so parity is unaffected.
- **Files modified:** scripts/parity/generate_reference.py
- **Verification:** `stat` reports 480044 bytes (< 512000); commit `6c83e31` passed all pre-commit hooks including `check-added-large-files`.
- **Committed in:** `6c83e31` (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Necessary to land the committed fixture within repo constraints. No scope creep; parity behavior unchanged.

## Issues Encountered
- A `[MEDIUM] uv-missing-dependency-cooldown` semgrep finding fired at `pyproject.toml:176` while editing the ruff per-file-ignores. It is a pre-existing condition in the unrelated `[tool.uv]` block (my edit was at ~line 104), so it was logged to `.planning/phases/47-official-arm64-essentia-agent-image/deferred-items.md` per the scope-boundary rule and NOT fixed here.

## User Setup Required
None - no external service configuration required. (The `parity-golden-regen` recipe requires a local Docker + the x86 api image, but that is an operator convenience, not a setup prerequisite for this plan.)

## Next Phase Readiness
- The comparator, shared dump contract, and committed reference clip are ready for plan 47-04's CI parity jobs (x86 golden + arm64 actual run the same `dump_analysis.py`, then shell out to `compare_analysis.py`).
- Open follow-up for 47-04: tune `atol` empirically from observed x86↔arm64 deltas (currently a conservative `1e-4` placeholder), and produce the authoritative `golden-x86.json` in CI.

---
*Phase: 47-official-arm64-essentia-agent-image*
*Completed: 2026-06-24*
