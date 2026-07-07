---
task: 260707-c9o
title: Add a y-axis (BPM) scale + labels to the "BPM (fine windows)" chart
type: quick
status: complete
date: 2026-07-07
commits:
  - 4bcd7adf  # feat: surface min/max BPM from _bpm_spark helper (Task 1)
  - 0bd4ed27  # feat: render max/min BPM scale labels on the fine-window chart (Task 2)
files_modified:
  - src/phaze/routers/proposals.py
  - src/phaze/routers/record.py
  - src/phaze/templates/proposals/partials/analysis_timeline.html
  - tests/review/routers/test_proposals.py
  - tests/shared/core/test_record_palette_agents.py
---

# Quick 260707-c9o: BPM y-axis scale + labels Summary

The "BPM (fine windows)" chart rendered as a bare stretched polyline with no y-axis and no
numeric labels. It now surfaces the min/max BPM (already computed and discarded inside the
helper) as HTML gutter labels beside the chart on BOTH surfaces (proposals timeline expand +
record slide-in), via a single shared-partial edit and both context builders passing the numbers.

## What changed

**Task 1 — helper refactor (TDD, commit `4bcd7adf`):**
- Renamed `_bpm_polyline_points` → `_bpm_spark`, now returning a `BpmSpark(points, lo, hi, count)`
  NamedTuple instead of a bare points string.
- The coordinate math is IDENTICAL (same rounding, same `span <= 0 → height/2` flat-line handling,
  same "higher BPM sits higher" mapping), so the rendered `points` string is byte-for-byte unchanged.
- `lo`/`hi` are numeric `float`s straight from `w.bpm` — never essentia strings — and are used ONLY
  as HTML label text; they are deliberately NOT written into any SVG geometry attribute, preserving
  the coordinate-numeric-only XSS hardening invariant.
- Updated all three call sites: the row sparkline dict takes `.points` (dict stays `dict[str, str]`,
  visually unchanged); the proposals and record timeline contexts each build `spark = _bpm_spark(...)`
  and pass `bpm_points`/`bpm_lo`/`bpm_hi`. Updated `record.py`'s import.
- RED/GREEN: 3 unit tests (normal range / flat line / empty) written failing, then made green.

**Task 2 — template labels + render assertions (commit `0bd4ed27`):**
- Edited only the BPM block of the shared `analysis_timeline.html` partial: wrapped the SVG in a
  flex row with a numeric gutter column OUTSIDE the `preserveAspectRatio="none"` SVG (text inside
  it would stretch). `lo != hi` renders max (top) + min (bottom); flat line (`lo == hi`) renders a
  single centered value; the empty `{% else %}` "No BPM data." branch is unchanged.
- Gutter styled with the existing `text-xs text-gray-400 dark:text-gray-500` tokens (+ `tabular-nums`)
  and an `aria-label="BPM range {lo} to {hi}"`. SVG `viewBox`/`preserveAspectRatio`/`polyline` are
  exactly as-is (numeric geometry unchanged).
- Because `record_body.html` `{% include %}`s this partial verbatim, the one edit covers both surfaces.
- Render assertions: proposals timeline asserts `120`/`128` + the aria-label; a new
  `test_record_renders_bpm_scale_labels` asserts `128`/`130` + aria-label over `GET /record/{id}`
  (proving record.py's context builder passes `bpm_lo`/`bpm_hi`).

## Deviations from Plan

**1. [Rule 3 - blocking] mypy `count`-vs-`tuple.count` field clash.**
- **Found during:** Task 1 mypy verify.
- **Issue:** The `BpmSpark.count` field shadows `tuple.count`, so mypy flagged
  `Incompatible types in assignment [assignment]`. Runtime is fine (the NamedTuple field wins;
  the unit test `result.count == 2` passed), but mypy must be clean (hard constraint).
- **Fix:** Added a targeted `# type: ignore[assignment]` on the `count: int` field line with an
  explanatory comment. Kept the field name `count` as the plan specified. `warn_unused_ignores`
  confirms the ignore is load-bearing.
- **Commit:** `4bcd7adf`

## Verification

- `uv run pytest tests/review/routers/test_proposals.py tests/shared/core/test_record_palette_agents.py`
  → **49 passed** (3 new helper unit tests + extended proposals render + new record render).
- `uv run ruff check .` → All checks passed.
- `uv run ruff format --check .` → 483 files already formatted.
- `uv run mypy .` → Success, no issues in 196 source files.
- Both per-task commits ran pre-commit hooks in full (no `--no-verify`).
- Ephemeral test Postgres/Redis (5433/6380) started for DB-backed render tests, torn down after.

## Self-Check: PASSED
- `src/phaze/routers/proposals.py` — FOUND (BpmSpark + _bpm_spark)
- `src/phaze/routers/record.py` — FOUND (import + context)
- `src/phaze/templates/proposals/partials/analysis_timeline.html` — FOUND (gutter)
- Commit `4bcd7adf` — FOUND
- Commit `0bd4ed27` — FOUND
