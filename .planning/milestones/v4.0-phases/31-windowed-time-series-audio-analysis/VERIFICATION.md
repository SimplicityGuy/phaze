---
phase: 31-windowed-time-series-audio-analysis
verified: 2026-06-10T00:00:00Z
status: human_needed
score: 14/14
overrides_applied: 0
human_verification:
  - test: "Load the review list in a browser; confirm the BPM sparkline SVG renders inline in each proposal row and the Timeline button is present."
    expected: "Each row shows a small BPM sparkline (or a dashed baseline for files with no windows) and a Timeline expand button."
    why_human: "Server-rendered SVG/HTMX visual appearance cannot be asserted by grep or a headless test."
  - test: "Click the Timeline expand button on a proposal that has analysis_window rows; confirm the multi-lane fragment loads (BPM polyline + Key/Mood/Style ribbon lanes)."
    expected: "The hidden timeline row un-hides, showing a BPM polyline SVG and proportional colored ribbon lanes. For files with no windows the empty-state message appears."
    why_human: "HTMX lazy-load + DOM reveal is a runtime browser interaction, not testable without a running server."
  - test: "Deploy v4.0.10 to the homelab, trigger Run analysis on the 11,428-file archive, and monitor a multi-hour Coachella set through completion."
    expected: "File completes without OnsetDetectionGlobal overflow or OOM; analysis_window rows appear in Postgres; aggregate fields on analysis row are populated; wall time is under 4h."
    why_human: "Requires real archive files, live GHCR image, and a running homelab environment."
---

# Phase 31: Windowed Time-Series Audio Analysis — Verification Report

**Phase Goal:** Rewrite `analyze_file` to stream-decode each file once and analyze it per-window — fixing the `RhythmExtractor2013` `OnsetDetectionGlobal` buffer-overflow crash and the latent whole-file OOM on multi-hour sets — producing a two-tier time-series (fine: BPM+key every 30s; coarse: mood/style/danceability every 3min). Persist windows in `analysis_window`; keep aggregates on the existing `analysis` row; extend `AnalysisWritePayload` with a `windows` list; make `put_analysis` idempotent; add a review-UI BPM sparkline that HTMX-expands to a multi-lane SVG/CSS timeline.
**Verified:** 2026-06-10
**Status:** human_needed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `analyze_file` decodes per-window with EasyLoader — no whole-file MonoLoader | VERIFIED | `es.EasyLoader(filename=file_path, sampleRate=..., startTime=start, endTime=end)()` at `services/analysis.py:416` (fine pass) and `:440` (coarse pass). The only "MonoLoader" token in the file is a docstring at line 356 explicitly noting EasyLoader does NOT decode the full signal, unlike MonoLoader. No `es.MonoLoader(...)` call exists. |
| 2 | Two-tier time-series produced: fine (BPM+key, 30s) and coarse (mood/style/danceability, 180s) | VERIFIED | `FineWindow`/`CoarseWindow` dataclasses at `analysis.py:246,269`; `_analyze_fine_windows` at `:411`; `_analyze_coarse_windows` at `:435`; return dict includes `"windows"` key at `:521`. |
| 3 | Representative aggregates (median BPM, modal key, dominant mood/style) kept on the `analysis` row | VERIFIED | `aggregate_bpm` (median, excludes confidence==0.0) at `:294`; `aggregate_key` (duration-weighted) at `:312`; `aggregate_dominant` (time-weighted) at `:321`; `aggregate_danceability` (mean) at `:331`. All four called in `analyze_file` at `:515–520`. |
| 4 | Window sizes configurable via AgentSettings (PHAZE_ANALYSIS_* env vars, defaults 30/180/15s) | VERIFIED | `analysis_fine_window_sec` (default 30), `analysis_coarse_window_sec` (default 180), `analysis_fine_min_sec` (default 15) at `config.py:388–400`, each with `AliasChoices("PHAZE_ANALYSIS_...", ...)`. |
| 5 | Per-window failure isolation: a failing window is logged+skipped, never fails the whole file | VERIFIED | `except Exception:  # per-window failure isolation: skip, never fail the file` at `analysis.py:429` (fine pass) and `:453` (coarse pass). Covered by `test_analyze_file_failure_isolation` + `test_analyze_file_coarse_failure_isolation`. |
| 6 | Asymmetric trailing-window policy: fine drops sub-min_sec trailing (except window 0); coarse has no floor | VERIFIED | `_iter_windows(drop_short_trailing=True)` for fine at `:414`; `_iter_windows(drop_short_trailing=False)` for coarse at `:438`. Policy logic at `:377`: "if `drop_short_trailing and (end - start) < min_sec and idx > 0`". |
| 7 | `analysis_window` table with CASCADE FK to files + partial/composite indexes | VERIFIED | `AnalysisWindow(TimestampMixin, Base)` at `models/analysis.py:27`; `ForeignKey("files.id", ondelete="CASCADE")` at `:46`; NOT unique (1:many, `index=True`). `AnalysisResult.file_id` left unchanged (unique=True, no ondelete). |
| 8 | Migration 018 is additive (no ALTER on existing tables), single alembic head | VERIFIED | `revision="018"`, `down_revision="017"` at `alembic/versions/018_add_analysis_window_table.py:36–37`. `grep -ci "alter"` = 0. Partial indexes at `:71–72` (`postgresql_where=sa.text("tier = 'fine'")`). |
| 9 | `AnalysisWindowPayload` + bounded `windows` field on `AnalysisWritePayload` | VERIFIED | `class AnalysisWindowPayload` at `schemas/agent_analysis.py:22`; `tier: Literal["fine", "coarse"]` at `:34`; `window_index/start_sec ge` guards at `:35–36`; `windows: list[AnalysisWindowPayload] \| None = Field(default=None, max_length=50000)` at `:63`. |
| 10 | `put_analysis` idempotently replaces a file's windows (delete-by-file_id + bulk insert, same transaction) | VERIFIED | `routers/agent_analysis.py:147–153`: `if body.windows is not None:` → `delete(AnalysisWindow).where(AnalysisWindow.file_id == file_id)` → `pg_insert(AnalysisWindow).values([{"id": uuid.uuid4(), "file_id": file_id, **w.model_dump()} for w in body.windows])`. PATH file_id only. |
| 11 | `process_file` forwards windows from `analyze_file` dict — import boundary (D-25) intact | VERIFIED | `tasks/functions.py:139`: `windows = [AnalysisWindowPayload(**w) for w in analysis.get("windows", [])]`. Module docstring + `grep` confirm no `phaze.database`/`phaze.models`/`sqlalchemy` import. `test_task_split.py` 6 passed. |
| 12 | `process_file` enqueued with bounded timeout (14400) + retries=2 (no 4x churn) | VERIFIED | `routers/pipeline.py:76–81`: `timeout=14400`, `retries=2` with inline comment explaining the `retries==1 -> 4` hook-clobber gotcha. Amended from plan's `timeout=0` per orchestrator amendment for restart resilience; intentional and documented in 31-05-SUMMARY. |
| 13 | Review-UI BPM sparkline + HTMX expand control in each proposal row | VERIFIED | `proposal_row.html:59`: `<svg viewBox="0 0 80 24">` sparkline; `:68`: `hx-get="/proposals/{{ proposal.id }}/timeline"`; `:102`: `<tr id="timeline-{{ proposal.id }}" class="hidden">`. |
| 14 | Timeline fragment: BPM polyline + key/mood/style ribbons, no charting lib, XSS-safe labels | VERIFIED | `analysis_timeline.html:11`: `<polyline points="{{ bpm_points }}">`. `grep -c "\| safe"` = 0. Ribbon labels at `:27` use `{{ ribbon.label }}` (standard autoescaping). `test_timeline_escapes_label_xss` asserts `&lt;script&gt;` rendering. 49-line template (> min_lines=25). |

**Score:** 14/14 truths verified

---

### Spike Evidence

**Locked decode strategy: EasyLoader-primary** — confirmed in `31-01-SUMMARY.md` Decode Decision Log.

Real file used: `Cosmic Gate - 2007-10-18 - Amsterdam Dance Event.mp3` (VBR MP3, 1.49h). Run environment: GHCR image `ghcr.io/simplicityguy/phaze:v4.0.9`, homelab agent, `phaze_models` volume.

| Validation | Threshold | Measured | Verdict |
|------------|-----------|----------|---------|
| (a) per-window decode | all windows decode, BPM returned | 178/178, failures=0 | PASS |
| (b) RhythmExtractor2013 on 30s buffer | no OnsetDetectionGlobal overflow | no overflow | PASS |
| (A1) seek cost vs window_index | roughly constant (non-quadratic) | first/last decode 5.162s/5.084s — no upward trend | PASS → EasyLoader-primary |
| (c) bounded RSS (fine pass) | flat, < ~1.5 GB | 254.2 / 270.7 / 270.7 MB (first/last/peak) | PASS |

Accepted deviation: file was 1.49h instead of ≥2h target. Rationale in 31-01-SUMMARY: "trends are conclusive across 178 windows and extrapolate linearly."

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/analysis.py` | Per-window analyze_file + EasyLoader | VERIFIED | 522 lines, EasyLoader at lines 416/440, aggregate helpers at 294–334, "windows" return at 521 |
| `src/phaze/models/analysis.py` | AnalysisWindow model with CASCADE FK | VERIFIED | AnalysisWindow at line 27, CASCADE FK at line 46 |
| `alembic/versions/018_add_analysis_window_table.py` | Additive migration + indexes | VERIFIED | revision="018", down_revision="017", 0 ALTERs, partial indexes at lines 71–72 |
| `src/phaze/schemas/agent_analysis.py` | AnalysisWindowPayload + windows field | VERIFIED | AnalysisWindowPayload at line 22, bounded windows field at line 63 |
| `src/phaze/routers/agent_analysis.py` | put_analysis idempotent child replace | VERIFIED | delete+insert guarded by `body.windows is not None` at lines 147–153 |
| `src/phaze/tasks/functions.py` | windows payload build from plain dicts | VERIFIED | AnalysisWindowPayload(**w) comprehension at line 139; import boundary clean |
| `src/phaze/routers/pipeline.py` | process_file enqueue with timeout=14400 + retries=2 | VERIFIED | Lines 76–81 |
| `src/phaze/routers/proposals.py` | HTMX timeline endpoint querying AnalysisWindow | VERIFIED | `GET /{proposal_id}/timeline` at line 252; `AnalysisWindow.file_id == file_id` at line 269 |
| `src/phaze/templates/proposals/partials/analysis_timeline.html` | Multi-lane SVG/CSS timeline (no charting lib) | VERIFIED | 49 lines, polyline at line 11, 0x `| safe`, ribbon labels autoescaped |
| `src/phaze/templates/proposals/partials/proposal_row.html` | BPM sparkline + HTMX expand control | VERIFIED | `<svg>` at line 59, `hx-get=.../timeline` at line 68, hidden `<tr>` at line 102 |
| `src/phaze/config.py` | analysis_fine/coarse/min_sec AgentSettings | VERIFIED | Lines 388–400, AliasChoices with PHAZE_ANALYSIS_* |
| `tests/test_services/test_analysis_long_file.py` | Bounded-memory + crash-guard integration tests | VERIFIED | `test_long_file_bounded` (mocked 2h-scale loop + RSS) + `test_real_decode_short_no_overflow` (real EasyLoader) |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `analyze_file` return | `"windows"` key | `{**aggregates, "windows": [...]}` plain dict | VERIFIED | `analysis.py:521`: `"windows": windows` |
| `analyze_file` coarse loop | `_classifier_cache` / `_predict_single` | `_run_model_sets` calls `_get_classifier`/`_predict_single` | VERIFIED | `analysis.py:447–456` via `_run_model_sets` |
| `process_file` | `AnalysisWritePayload.windows` | `[AnalysisWindowPayload(**w) for w in analysis.get("windows", [])]` | VERIFIED | `functions.py:139` |
| `AnalysisWritePayload.windows` | `AnalysisWindow` rows | `delete(AnalysisWindow).where(...) + pg_insert` in same txn | VERIFIED | `agent_analysis.py:147–153` |
| `proposal_row.html` expand control | timeline endpoint | `hx-get="/proposals/{{ proposal.id }}/timeline"` → `hx-target="#timeline-{{ proposal.id }}"` | VERIFIED | `proposal_row.html:68` |
| timeline endpoint | `analysis_window` rows | `select(AnalysisWindow).where(AnalysisWindow.file_id == file_id)` | VERIFIED | `proposals.py:269` |
| `alembic 018` | migration 017 | `down_revision = "017"` (bare number) | VERIFIED | `018_add_analysis_window_table.py:37` |

---

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `proposals.py` timeline endpoint | `windows` (list[AnalysisWindow]) | `select(AnalysisWindow).where(file_id==...).order_by(tier, window_index)` DB query | Yes — live DB query, not static | FLOWING |
| `proposals.py` list endpoint | `sparklines` (dict[str, polyline_pts]) | `_build_sparklines`: batch `IN (...)` query on `AnalysisWindow` | Yes — live batch DB query | FLOWING |
| `put_analysis` | `body.windows` (list[AnalysisWindowPayload]) | Wire payload from agent → parsed by pydantic → bulk insert into DB | Yes — insert path is real | FLOWING |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| 162 phase-related tests pass | `uv run pytest tests/test_services/test_analysis.py tests/test_services/test_analysis_long_file.py tests/test_models/test_analysis_window.py tests/test_migrations/test_migration_018.py tests/test_schemas/test_agent_analysis.py tests/test_routers/test_agent_analysis.py tests/test_routers/test_proposals.py tests/test_tasks/test_functions.py tests/test_task_split.py tests/test_routers/test_pipeline.py tests/test_config/test_agent_settings_windows.py -q` | 162 passed in 20.25s | PASS |
| Import boundary gate | `uv run pytest tests/test_task_split.py -q` | 6 passed in 0.92s | PASS |
| No whole-file MonoLoader calls | `grep "es\.MonoLoader\(" src/phaze/services/analysis.py` | No output | PASS |
| EasyLoader present in analysis service | `grep "EasyLoader" src/phaze/services/analysis.py` | Lines 412, 416, 440, 485 | PASS |
| No `\| safe` in timeline template | `grep -c "\| safe" src/phaze/templates/proposals/partials/analysis_timeline.html` | 0 | PASS |
| Migration 018 no ALTER | `grep -ci "alter" alembic/versions/018_add_analysis_window_table.py` | 0 | PASS |
| timeout=14400 in pipeline | `grep -n "timeout=14400" src/phaze/routers/pipeline.py` | Line 76 | PASS |
| retries=2 in pipeline | `grep -n "retries=2" src/phaze/routers/pipeline.py` | Line 81 | PASS |

---

### Requirements Coverage

| Requirement | Description | Status | Evidence |
|-------------|-------------|--------|----------|
| ANL-01 | BPM/key/mood/style detection extended to time-series; cross-archive queryability | SATISFIED | Two-tier windows; partial indexes on bpm/danceability; aggregate row preserved; end-to-end wire verified |

---

### Anti-Patterns Found

No blockers found. Scan of all phase-modified source files returned no TBD, FIXME, XXX, TODO, or placeholder markers. No stub patterns found. Bare `except Exception:` at `analysis.py:429,453` is intentional per-window failure isolation (documented with comment; BLE001 noqa absent because BLE is not in the project's ruff rule set — correct behavior).

The spike script (`scripts/spike_windowed_analysis.py`) is present but untracked (not committed), per the plan's requirement to leave it as a throwaway.

---

### Notable Deviation

**timeout=14400 (not timeout=0):** Plan 05 originally specified `timeout=0` (unbounded). During execution the orchestrator issued an amendment changing this to `timeout=14400` (4 hours) for worker-restart resilience. The 31-01 spike measured a 1.49h file at ~51min wall, so a ~3h Coachella set runs well under 4h. The amendment is documented in 31-05-SUMMARY under "Deviations from Plan" and the verification focus explicitly notes it as intentional. This is a strict improvement over the original plan, not a gap.

---

### Human Verification Required

### 1. Review-UI sparkline renders correctly

**Test:** Deploy the branch, navigate to the review list (the Proposals UI), and inspect at least one row.
**Expected:** Each row shows a compact inline SVG BPM sparkline (or a dashed flat baseline for files with no analysis windows). A "Timeline" button appears in the Actions cell.
**Why human:** Server-rendered SVG appearance and layout cannot be validated by unit tests or grep.

### 2. HTMX timeline expand loads the multi-lane fragment

**Test:** On the review list, click the Timeline button for a proposal whose file has `analysis_window` rows.
**Expected:** The hidden sibling `<tr>` un-hides, showing a BPM `<polyline>` SVG and proportional Key/Mood/Style colored ribbon lanes with escaped labels. For files with no windows, the "No analysis windows for this file." empty-state message appears.
**Why human:** HTMX lazy-load + DOM reveal requires a running browser and server. The unit tests verify the endpoint returns the correct HTML; the HTMX wiring is live-only.

### 3. Homelab end-to-end: v4.0.10 deployed + Re-run analysis on the archive

**Test:** Build and push GHCR image `v4.0.10`, redeploy on the homelab, trigger "Run analysis" on the 11,428-file archive (including at least one multi-hour Coachella set), and monitor Postgres for `analysis_window` row population.
**Expected:** Files complete without `OnsetDetectionGlobal` overflow or OOM kill; `analysis_window` rows appear (both fine and coarse tiers); aggregate fields on `analysis` row are non-null; wall time for a 2–3h set is under 4h.
**Why human:** Requires real archive files, live GHCR image, and the homelab environment. The automated `test_real_decode_short_no_overflow` proves the essentia path on a short real buffer; the full multi-hour production run is beyond CI scope.

---

### Gaps Summary

No automated gaps found. The phase goal is fully delivered and verified in the codebase across all 14 observable truths. Three human verification items remain — two are visual/HTMX runtime checks and one is the homelab redeploy that ships v4.0.10 and confirms the live crash fix. None of these indicate a coding defect.

---

_Verified: 2026-06-10_
_Verifier: Claude (gsd-verifier)_
