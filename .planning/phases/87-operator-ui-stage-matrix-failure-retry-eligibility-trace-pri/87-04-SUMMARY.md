---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
plan: 04
subsystem: operator-ui
tags: [stage-matrix, five-bucket-pill, paginated-derivation, degrade-safe, ui-01, perf-01, deriv-read]
requires:
  - "Status.SKIPPED + skipped-threaded stage_status_case (Plan 02)"
  - "Phase-77 partial indexes ix_metadata_failed / ix_analysis_completed / ix_analysis_failed / ix_fprint_success"
provides:
  - "_stage_pill.html — the 5-bucket stage-status token (done/in_flight/not_started/failed/skipped), glyph+word+aria-label+dark: pair"
  - "_stage_matrix.html — the 6-pill row with the 7→6 remap (Appr=REVIEW, Exec=APPLY, tracklist omitted) + one-legend-per-surface"
  - "services/pipeline.py:get_files_page — bounded (LIMIT+1 sentinel, no whole-corpus COUNT), correlated per-page stage_status_case derivation, begin_nested SAVEPOINT degrade-safe"
  - "services/pipeline.py:_files_page_stmt — the extracted bounded Select (EXPLAIN-probeable)"
  - "GET /pipeline/files — paginated files table, stage/bucket params validated + plumbed for Plan 05"
  - "files_table_view.html — per-row 6-pill matrix, record slide-in binding, cursor Prev/Next"
affects:
  - "Plan 05 retires the raw-enum State cell in favour of these pills and wires the stage/bucket status-filter bar (templates-only — params already accepted)"
tech-stack:
  added: []
  patterns:
    - "5-bucket status pill extends scan_status_pill.html geometry; colour is never the sole channel (glyph shape + word + aria-label, WCAG 1.4.1)"
    - "paginated per-page derivation: correlated stage_status_case columns evaluate for the N page rows only (O(page_size), never O(corpus))"
    - "keyset-style LIMIT+1 sentinel for has_next — pagination with NO whole-corpus COUNT (T-87-11 DoS mitigation)"
    - "begin_nested() SAVEPOINT degrade → safe empty page (the saq_detail idiom, INFLIGHT-02/T-87-12)"
key-files:
  created:
    - src/phaze/templates/pipeline/partials/_stage_pill.html
    - src/phaze/templates/pipeline/partials/_stage_matrix.html
    - src/phaze/templates/pipeline/partials/files_table_view.html
    - tests/shared/test_stage_pill_render.py
    - tests/integration/test_files_page.py
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
decisions:
  - "files_table_view.html builds its OWN table instead of reusing _file_table.html: that partial's cell contract is text-only ({{ cell.text }}, autoescaped) and cannot host the _stage_pill COMPONENT markup each stage cell requires. Its structure (empty-state, thead/tbody, D-06 rows, row_file_ids record binding) is mirrored verbatim."
  - "Pagination is inline cursor-style Prev/Next (LIMIT+1 sentinel), NOT tracklists/partials/pagination.html: that template requires pagination.total, which forces a whole-corpus COUNT — forbidden by T-87-11 / behavior-7. The no-COUNT constraint (security) outranks the pattern-reuse suggestion."
  - "page_size clamps to 10..100 (min 10 matches tracklists' Query ge=10); a smaller request clamps UP to 10."
  - "stage/bucket params validated against Stage/Status allowlists in the router (unknown → unfiltered page, never a 422 into the poll); the service applies them as a pure ORM bound-param comparison (T-87-14)."
metrics:
  duration: ~50m
  completed: 2026-07-11
  tasks: 2
  files: 7
---

# Phase 87 Plan 04: Stage-matrix pill + paginated derived files table Summary

Built the operator's scannable "where's this file at?" overview: the five-bucket stage-status pill, the
six-pill matrix row (with the 7-stage→6-pill remap landmine encoded in one place), and a paginated
`GET /pipeline/files` whose per-row status is DERIVED via correlated `stage_status_case` columns —
never the raw `FileRecord.state` string, and never a whole-corpus scan/COUNT per poll. This is the
primary visual anchor of UI-01 and the scannable half of the D-02 dual home.

## What Was Built

- **`_stage_pill.html` (Task 1)**: the five-bucket token extending `scan_status_pill.html`'s pill
  geometry (`text-xs font-semibold px-2 py-0.5 rounded-full bg-{hue}-100 dark:bg-{hue}-950 …`). Five
  branches: `done` (green ✓), `in_flight` (blue ● + `animate-pulse`), `not_started` (muted gray —),
  `failed` (red ✗), `skipped` (violet ⊘ + `ring-1 ring-dashed ring-violet-400/60`). Colour is never the
  sole channel — every branch carries a distinct glyph shape, a human word, an `aria-label`, and a
  `dark:` pair (WCAG 1.4.1). The `skipped` token is deliberately unlike `done` (D-08 honesty — a
  force-skip can never read as genuine completion). An unknown/empty bucket falls back to `not_started`.
- **`_stage_matrix.html` (Task 1)**: the six-pill row in order Meta·FP·Analyze·Prop·Appr·Exec with the
  **7→6 remap LANDMINE** encoded in the loop — `Appr` reads `buckets.review`, `Exec` reads
  `buckets.apply`, and `tracklist` is never shown. `flex flex-wrap gap-2` (wraps narrow, D-01). A
  `legend`/`legend_only` param single-sources the one-per-surface legend (`✓ done · ● in-flight · —
  not-started · ✗ failed · ⊘ skipped`).
- **`get_files_page` + `_files_page_stmt` (Task 2, `services/pipeline.py`)**: builds
  `select(FileRecord, stage_status_case(METADATA), …, stage_status_case(APPLY)).order_by(FileRecord.id)`
  with an optional `stage_status_case(stage) == bucket` filter, `LIMIT page_size + 1` (the sentinel that
  yields `has_next` with **no COUNT**), run inside a `begin_nested()` SAVEPOINT try/except that degrades
  to a **safe empty page** on any error. Returns `FilesPage(rows, page, page_size, has_next)`; each
  `FilesPageRow` carries the ORM record + a `buckets` dict keyed by `Stage` value. `_files_page_stmt` is
  extracted so the EXPLAIN test can probe the exact statement.
- **`GET /pipeline/files` (Task 2, `routers/pipeline.py`)**: renders `files_table_view.html`. `stage`
  and `bucket` are validated against the `Stage` / `Status` allowlists (unknown → unfiltered) and
  plumbed straight through, so Plan 05's status-filter bar is templates-only. No router try/except — the
  degrade lives at the service layer.
- **`files_table_view.html` (Task 2)**: columns File · Type · Meta · FP · Analyze · Prop · Appr · Exec;
  the path cell is `font-mono text-xs truncate` with `title=full_path` (autoescaped, never `| safe` —
  T-87-13); each stage cell hosts one `_stage_pill`; rows bind to the record slide-in via
  `hx-get="/record/{file_id}"` (Phase-61 idiom); one legend per surface; inline cursor Prev/Next.
- **Tests**: `tests/shared/test_stage_pill_render.py` (10) — every bucket's glyph+word+aria-label+dark
  pair, in_flight pulse, skipped-unlike-done, unknown fallback, 6-pill count/order, the Appr↔review /
  Exec↔apply remap, tracklist omission, legend + legend_only. `tests/integration/test_files_page.py`
  (5) — bounded + no-COUNT emitted SQL (captured via a `before_cursor_execute` listener), last-page
  has_next False, per-row buckets match seeded markers, EXPLAIN names all four partial indexes, and the
  SAVEPOINT degrade to a safe empty page + session recovery.

## How to Verify

- `uv run pytest tests/shared/test_stage_pill_render.py -q` → 10 passed.
- With the test DB up (port 5433, DB `phaze_test`):
  `TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test PHAZE_QUEUE_URL=postgresql://phaze:phaze@localhost:5433/phaze_test uv run pytest tests/integration/test_files_page.py -q`
  → 5 passed.
- `uv run ruff check .` clean; `uv run mypy .` clean (both ran green via pre-commit on each task commit).
- `uv run pytest tests/integration tests/shared --co -q` → 1331 tests collect (no import regressions).

### EXPLAIN evidence (PERF-01 / behavior-7)

`EXPLAIN` of `_files_page_stmt(page=1, page_size=25)` with `enable_seqscan=off` on a seeded corpus:

```
Limit  (cost=0.14..2534.94 rows=13 width=752)
  ->  Index Scan using pk_files on files  (...)          # bounded outer scan on the PK index, no COUNT
        ...
        ->  Bitmap Index Scan on ix_metadata_failed      # correlated per-page marker probes
        ->  Bitmap Index Scan on ix_fprint_success
        ->  Bitmap Index Scan on ix_analysis_completed
        ->  Bitmap Index Scan on ix_analysis_failed
```

Top node is `Limit` (bounded, no `Aggregate`/`COUNT`); the outer scan rides `pk_files`; the four
Phase-77 partial indexes back the correlated `stage_status_case` probes. This is the T-87-11 DoS
mitigation: derivation cost is O(page_size), never O(corpus).

## Deviations from Plan

### Design deviations (both driven by the higher-priority no-COUNT / component-hosting constraints)

**1. [Rule 3 — Design] `files_table_view.html` builds its own table, not `_file_table.html`**
- **Found during:** Task 2.
- **Issue:** The plan says "reuses `_file_table.html`", but that partial's cell contract is text-only
  (`{{ cell.text }}`, autoescaped) and cannot host the `_stage_pill` **component** markup each stage
  cell requires (acceptance criterion: "each stage cell hosts one `_stage_pill`").
- **Fix:** `files_table_view.html` mirrors `_file_table.html`'s structure verbatim — empty-state,
  `thead/tbody` idiom, D-06 inert-but-present rows, the Phase-61 `row_file_ids` record slide-in binding,
  and the `x-data` Alpine-scope wrapper (CR-01) — while hosting a `_stage_pill` in each of the six stage
  cells. Path cells stay autoescaped (T-87-13).
- **Files:** `src/phaze/templates/pipeline/partials/files_table_view.html`. **Commit:** 5cd5c4ec.

**2. [Rule 3 — Design] Inline cursor Prev/Next instead of `tracklists/partials/pagination.html`**
- **Found during:** Task 2.
- **Issue:** The plan suggests reusing `tracklists/partials/pagination.html`, but that template requires
  `pagination.total`, which would force a whole-corpus `COUNT(*)` — exactly the anti-feature the T-87-11
  mitigation and behavior-7 test forbid ("no unbounded whole-corpus COUNT per poll").
- **Fix:** Used a keyset-style `LIMIT page_size + 1` sentinel to compute `has_next` with **no COUNT**,
  and rendered inline cursor Prev/Next controls (Page N + Previous/Next) in `files_table_view.html`. The
  no-COUNT security constraint outranks the pattern-reuse suggestion.
- **Files:** `services/pipeline.py`, `files_table_view.html`. **Commit:** 5cd5c4ec.

No auto-fixed bugs, no auth gates, no architectural (Rule 4) escalations. No out-of-scope issues found;
`deferred-items.md` not appended.

## Threat Register Coverage

- **T-87-11** (whole-corpus scan/COUNT per poll): mitigated — `_files_page_stmt` is `LIMIT`-bounded with
  a `+1` sentinel (no COUNT), and the six correlated `stage_status_case` columns evaluate for the page
  rows only. Asserted by `test_files_page_is_bounded_and_emits_no_count` (real emitted SQL captured; no
  `count(`, `LIMIT` present) + the EXPLAIN `Limit`/`Index Scan using pk_files` evidence above.
- **T-87-12** (poll-time 500 on DB hiccup): mitigated — `begin_nested()` SAVEPOINT try/except degrades
  to a safe empty page; a forced build error returns `rows=[]`/`has_next=False` and the outer session
  still serves a follow-up query. Asserted by `test_degrades_to_empty_page_on_error`.
- **T-87-13** (XSS via file path): mitigated — the path cell is rendered as autoescaped text with a
  `title` attribute, never `| safe`. `file.id` is a UUID. Verified in the template smoke-render (`&` →
  `&amp;`).
- **T-87-14** (injection via stage/bucket param): mitigated — router validates both against the
  `Stage` / `Status` allowlists; the service applies the filter as a pure ORM bound-param comparison
  (`stage_status_case(stage) == bucket`), never f-string SQL.

No new threat surface introduced beyond the plan's register (the endpoint is read-only; no new writes,
auth paths, or schema).

## Self-Check: PASSED

All 5 created files + both modified files present on disk; both task commits (08b356b1, 5cd5c4ec) found
in git history. (Verified below at write time.)
