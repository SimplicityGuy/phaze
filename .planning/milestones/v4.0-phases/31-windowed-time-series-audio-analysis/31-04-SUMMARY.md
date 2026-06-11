---
phase: 31-windowed-time-series-audio-analysis
plan: 04
subsystem: analysis
tags: [essentia, easyloader, windowed-analysis, bpm, key, mood, style, danceability, bounded-memory, agent-worker]

# Dependency graph
requires:
  - phase: 31-01
    provides: "Locked decode strategy (EasyLoader-primary) + real-file RSS/seek/inference numbers"
provides:
  - "Rewritten analyze_file: segmented per-window decode (no whole-file MonoLoader) -> fixes OnsetDetectionGlobal overflow + whole-file OOM"
  - "Two-tier time-series output: fine (BPM/key, 30s) + coarse (mood/style/danceability, 180s) windows"
  - "Four pure-Python aggregate reducers (median BPM, duration-weighted key, time-weighted mood/style, mean danceability)"
  - "FineWindow/CoarseWindow containers with as_payload_dict() ready for AnalysisWindowPayload(**w)"
  - "Three window-config AgentSettings (analysis_fine_window_sec=30, analysis_coarse_window_sec=180, analysis_fine_min_sec=15)"
affects: [31-02-schema-model, 31-03-router-persistence, 31-05-job-config-wiring, 31-06-review-ui-timeline]

# Tech tracking
tech-stack:
  added: []  # zero new runtime dependencies (RESEARCH Package Legitimacy Audit)
  patterns:
    - "Segmented bounded decode loop: es.EasyLoader(startTime,endTime,sampleRate) per window, buffer GC'd between windows"
    - "Per-window failure isolation: try/except Exception + log.warning + continue (never fail the file)"
    - "Asymmetric trailing-window policy: fine drops sub-min_sec trailing (except window 0); coarse has no floor"
    - "Duration probe via es.MetadataReader (no PCM materialized) instead of MonoLoader"

key-files:
  created:
    - tests/test_config/test_agent_settings_windows.py
    - tests/test_services/test_analysis_long_file.py
  modified:
    - src/phaze/services/analysis.py
    - src/phaze/config.py
    - tests/test_services/test_analysis.py

key-decisions:
  - "EasyLoader-primary segmented decode (locked by 31-01 spike); hybrid decode+Resample rejected"
  - "analyze_file gains optional keyword window-size params (defaults mirror AgentSettings); preserves existing 2-arg callers; agent-worker->params wiring deferred to 31-05 (functions.py not in this plan's scope)"
  - "Aggregate danceability continues to funnel into features JSONB (no new analysis column); per-window danceability lives on the window dict (Pitfall 6 / Open Q2)"
  - "Representative analysis-row features = longest-duration coarse window's features (keeps existing features-JSONB structure populated for downstream consumers)"
  - "# noqa: BLE001 omitted: BLE is not in this project's ruff rule set, so the marker is flagged RUF100 (unused). Bare 'except Exception' + log is clean here"
  - "Real >=2h essentia decode is ~35min (not CI-feasible); the bounded-memory proof is split into a mocked-2h-scale loop test + a real-decode short-buffer crash guard"

patterns-established:
  - "Window loop primitive _iter_windows(total_sec, win_sec, min_sec, drop_short_trailing) shared by both tiers"
  - "Aggregate reducers operate on FineWindow/CoarseWindow lists, pure-Python, unit-tested without essentia"

requirements-completed: [ANL-01]

# Metrics
duration: ~75min
completed: 2026-06-10
---

# Phase 31 Plan 04: Segmented Per-Window analyze_file Summary

**Rewrote `analyze_file` from two whole-file `MonoLoader` decodes to EasyLoader-primary segmented per-window analysis — fixing the `OnsetDetectionGlobal` overflow crash and the whole-file OOM while upgrading every characteristic to a two-tier time-series with representative aggregates.**

## Performance

- **Duration:** ~75 min
- **Started:** 2026-06-10T16:20Z (approx)
- **Completed:** 2026-06-10T17:35Z (approx)
- **Tasks:** 2
- **Files modified:** 5 (2 created, 3 modified)

## Accomplishments
- `analyze_file` now decodes one short window at a time via `es.EasyLoader(startTime, endTime, sampleRate)` — no essentia algorithm ever sees more than one 30s/180s window, so the crash and OOM are eliminated by construction.
- Two-tier time-series: fine windows (BPM + key at 44.1kHz, 30s) and coarse windows (34 TF model sets at 16kHz, 180s, reusing `_classifier_cache` inference-only).
- Four pure-Python aggregate reducers keep the existing `analysis` row populated (median BPM excluding confidence==0.0, duration-weighted modal key, time-weighted dominant mood/style, mean danceability).
- Asymmetric trailing-window policy: fine drops sub-`analysis_fine_min_sec` trailing windows (except window 0); coarse keeps every window with audio (no floor).
- Per-window failure isolation: a window that raises is logged and skipped, never failing the whole file.
- Return shape is `{**aggregates, "windows": [...fine + coarse dicts...]}`, each dict ready for `AnalysisWindowPayload(**w)`.
- Three window-config `AgentSettings` fields with `PHAZE_ANALYSIS_*` aliases.
- Automated bounded-memory proof: a mocked-decode 2h-scale loop test (RSS does not scale with length) plus a real-decode short-buffer crash guard (real EasyLoader+RhythmExtractor2013+KeyExtractor, no overflow).

## Task Commits

Each task was committed atomically (per-task pre-commit hooks all passed — ruff, ruff-format, bandit, mypy):

1. **Task 1: Window-config AgentSettings + pure-Python aggregate helpers + window dataclasses** - `c81f897` (feat)
2. **Task 2: Rewrite analyze_file to per-window decode + asymmetric trailing policy + synthetic-2h bounded-memory test** - `b501d79` (feat)
3. **Task 2 coverage: coarse failure isolation + derive_danceability None** - `cf9436c` (test)

## Files Created/Modified
- `src/phaze/config.py` - Added `analysis_fine_window_sec` (30), `analysis_coarse_window_sec` (180), `analysis_fine_min_sec` (15) to `AgentSettings` with `AliasChoices`.
- `src/phaze/services/analysis.py` - Rewrote `analyze_file` body (segmented decode); added `FineWindow`/`CoarseWindow` dataclasses, `aggregate_bpm`/`aggregate_key`/`aggregate_dominant`/`aggregate_danceability`, `derive_danceability`, `_probe_duration_sec`, `_iter_windows`, `_run_model_sets`, `_analyze_fine_windows`, `_analyze_coarse_windows`, `_representative_features`. Kept all existing helpers/caches (`MODEL_SETS`, `_classifier_cache`, `_get_classifier`, `_predict_single`, `derive_mood`, `derive_style`, `_suppress_essentia_logging`).
- `tests/test_services/test_analysis.py` - Added aggregate-reducer tests (no essentia mock), window-boundary, asymmetric-trailing, fine + coarse failure-isolation, return-shape tests; updated the essentia mock (EasyLoader + MetadataReader + scalar confidence) and the corrupt-file test (now a fatal duration-probe failure).
- `tests/test_config/test_agent_settings_windows.py` - New: defaults, kwarg override, and `PHAZE_ANALYSIS_*` env-binding tests for the three window-config fields.
- `tests/test_services/test_analysis_long_file.py` - New (`integration`): `test_long_file_bounded` (2h-scale non-accumulating loop + bounded RSS) and `test_real_decode_short_no_overflow` (real essentia decode, no `OnsetDetectionGlobal` overflow).

## Decisions Made
- **EasyLoader-primary** segmented decode, per the 31-01 locked decision (hybrid decode+Resample rejected — it would reintroduce a whole-file resident buffer).
- **Optional keyword window-size params** on `analyze_file` (defaults mirror the AgentSettings defaults). Existing 2-arg call sites are unchanged; wiring the agent worker to pass `settings.analysis_*` into these params is a `tasks/functions.py` change deferred to Plan 31-05 (out of this plan's file scope).
- **Aggregate danceability** continues to funnel into the `features` JSONB (no new `analysis` column); per-window danceability lives on `analysis_window.danceability` via the window dict (Pitfall 6 / Open Q2).
- **Representative `features`** for the aggregate row = the longest-duration coarse window's features, keeping the existing all-model-sets + genre structure populated for downstream consumers (`_features_to_mood_dict`, etc.).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `# noqa: BLE001` marker removed (project lint config mismatch)**
- **Found during:** Task 2 (analyze_file rewrite)
- **Issue:** RESEARCH/PATTERNS prescribe `except Exception:  # noqa: BLE001` for per-window isolation, but `BLE` is not in this project's ruff rule set, so the marker is flagged `RUF100` (unused noqa) — which would fail the mandatory pre-commit hooks.
- **Fix:** Kept the broad `except Exception:` with an explanatory plain comment and `log.warning(..., exc_info=True)`; removed the unused `# noqa: BLE001`. Behavior (isolation) is unchanged and verified by the fine + coarse failure-isolation tests.
- **Files modified:** src/phaze/services/analysis.py
- **Verification:** `ruff check` clean; `test_analyze_file_failure_isolation` + `test_analyze_file_coarse_failure_isolation` pass.
- **Committed in:** b501d79 / cf9436c
- **Note:** This makes the plan's `grep -nE "# noqa: BLE001"` acceptance check inapplicable in this repo; the equivalent behavioral guarantee is covered by tests instead.

**2. [Rule 1 - Bug] Integration test redesigned — real >=2h decode is not CI-feasible**
- **Found during:** Task 2 (synthetic-2h integration test)
- **Issue:** The first cut ran a *real* essentia decode over a synthetic >=2h file. Measured cost is ~0.3s wall per second-of-audio (a 600s file took 183s), so a real 2h decode is ~35 min — it overran every tool/background timeout and produced no result. VALIDATION.md L88 already records that a real multi-hour fixture is "unavailable in CI fixtures" (the spike's job).
- **Fix:** Split into two fast, honest `integration` tests: (a) `test_long_file_bounded` mocks `EasyLoader` to return a realistic ~5MB buffer per window and runs the loop over a 7210s duration, asserting ~240 fine windows AND a bounded short->long peak-RSS increment (<400MB) — proving the loop never accumulates at 2h scale; (b) `test_real_decode_short_no_overflow` runs the *real* EasyLoader+RhythmExtractor2013+KeyExtractor path on a real ~90s synthetic WAV, asserting no `OnsetDetectionGlobal` overflow. Together they prove a >=2h file (only ever fed 30s/180s buffers by a non-accumulating loop) cannot crash or OOM.
- **Files modified:** tests/test_services/test_analysis_long_file.py
- **Verification:** Both pass in ~4.9s; `test_real_decode_short_no_overflow` exercises real essentia on real 30s buffers.
- **Committed in:** b501d79

**3. [Rule 1 - Bug] Corrupt-file test updated for the new decode path**
- **Found during:** Task 2
- **Issue:** The existing `test_analyze_file_raises_on_corrupt_file` mocked `MonoLoader` to raise — but the rewrite removed `MonoLoader`. Under the new path a per-window decode failure is *isolated* (skipped), not fatal.
- **Fix:** Repointed the test to the fatal stage — `es.MetadataReader` (the `_probe_duration_sec` whole-file probe) raising propagates, which is the correct "unreadable file" semantics.
- **Files modified:** tests/test_services/test_analysis.py
- **Verification:** Test passes (`pytest.raises(RuntimeError)`).
- **Committed in:** b501d79

---

**Total deviations:** 3 auto-fixed (1 blocking lint-config mismatch, 2 bug fixes for test fidelity).
**Impact on plan:** All necessary for pre-commit passing and CI feasibility. Core behavior (segmented decode, two-tier windows, aggregates, isolation, asymmetric trailing) matches the plan exactly. No scope creep; `tasks/functions.py` wiring correctly left to Plan 31-05.

## Issues Encountered
- essentia real-decode throughput (~0.3s/audio-second) made a literal real-2h integration test infeasible; resolved by the two-part test design above (see Deviation 2).
- `Counter[str]` (int-valued) could not accumulate float window durations under mypy strict; switched the duration-weighted reducers to a plain `dict[str, float]` + `_max_by_duration` helper (stable on ties).

## Threat Flags
None — no new network endpoints, auth paths, or trust-boundary surface. The plan's threat register (T-31-04-01/02 DoS mitigations) is satisfied: segmented decode holds one window buffer per pass (validated by `test_long_file_bounded`) and per-window try/except prevents dead-lettering (validated by the isolation tests).

## User Setup Required
None - no external service configuration required. New `PHAZE_ANALYSIS_*` env vars have safe defaults (30/180/15).

## Next Phase Readiness
- `analyze_file` returns `{**aggregates, "windows": [...]}` with each window dict shaped for `AnalysisWindowPayload(**w)` — ready for Plan 31-02 (schema/model) and 31-03 (router persistence).
- Plan 31-05 must wire `tasks/functions.py::process_file` to pass `settings.analysis_*` into the new `analyze_file` keyword params and build the windows payload, and set `process_file` `timeout=0`/`retries=2` (RESEARCH Open Q1).
- No blockers.

## Self-Check: PASSED

---
*Phase: 31-windowed-time-series-audio-analysis*
*Completed: 2026-06-10*
