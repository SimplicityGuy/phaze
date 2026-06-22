---
phase: quick-260622-i0w
plan: 01
subsystem: pipeline-admin-ui
tags: [reconciliation, dedup, dag-canvas, degrade-safe, htmx]
requires:
  - "ScanBatch (scan_batches.total_files, status, created_at)"
  - "FileRecord (composite unique key agent_id, original_path)"
  - "_safe_count degrade pattern (services/pipeline.py)"
provides:
  - "deduped_count pure helper"
  - "get_scanned_total / get_global_reconciliation / get_agent_reconciliations"
  - "Discovery DAG-node 'scanned · deduped' subtitle"
  - "Recent Scans per-agent '→ N unique · M deduped' annotation"
affects:
  - "src/phaze/services/pipeline.py"
  - "src/phaze/routers/pipeline.py"
  - "src/phaze/routers/pipeline_scans.py"
  - "src/phaze/templates/pipeline/partials/dag_canvas.html"
  - "src/phaze/templates/pipeline/partials/recent_scans_table.html"
tech-stack:
  added: []
  patterns:
    - "Window-function latest-per-agent (row_number() OVER PARTITION BY agent_id ORDER BY created_at DESC)"
    - "None-as-hide sentinel distinct from real 0"
    - "Service-owns-degrade router wiring (no router try/except)"
key-files:
  created: []
  modified:
    - "src/phaze/services/pipeline.py"
    - "src/phaze/routers/pipeline.py"
    - "src/phaze/routers/pipeline_scans.py"
    - "src/phaze/templates/pipeline/partials/dag_canvas.html"
    - "src/phaze/templates/pipeline/partials/recent_scans_table.html"
    - "tests/test_services/test_pipeline.py"
    - "tests/test_dag_canvas_render.py"
    - "docs/architecture.md"
decisions:
  - "scanned uses each agent's LATEST completed batch only (re-scan never double-counts)"
  - "None scanned hides the whole reconciliation line (distinct from a real 0)"
  - "Server-render only — not threaded through the JS/$store 5s poll path"
metrics:
  duration: "~35 min"
  completed: "2026-06-22"
  tasks: 2
  files_modified: 8
  tests_total: 2013
  coverage: "97.45%"
---

# Quick 260622-i0w: Scanned / Deduped / Unique Reconciliation Summary

Turn the apparent Discovery-count vs agent-scan-total bug into a self-explaining
reconciliation: a degrade-safe data layer plus two muted UI annotations (Discovery DAG node +
Recent Scans FILES cell) that surface dedup/normalization collisions as expected behavior, not
lost work.

## What was built

**Task 1 — degrade-safe data layer (`services/pipeline.py`)** — commit `9d84093`
- `deduped_count(scanned, unique)` pure helper: None passthrough + `max(0, scanned - unique)` clamp.
- `get_scanned_total` — SUM of each agent's LATEST completed `ScanBatch.total_files` via
  `row_number()` window (re-scan-safe). Returns `None` (the hide sentinel) on empty DB or any error.
- `get_global_reconciliation` — `{"scanned", "deduped"}`; `deduped = max(0, scanned − COUNT(all files))`,
  hidden state `{None, None}` when scanned unavailable.
- `get_agent_reconciliations` — per-agent `{scanned, unique, deduped}` map, degrades to `{}`.
- All readers mirror the existing `_safe_count` / `get_stage_controls` discipline (log → guarded
  rollback → sentinel); none raise into the 5s poll.

**Task 2 — router + template wiring + render tests** — commit `8d805bd`
- `dashboard()` passes `reconcile_scanned` / `reconcile_deduped` from `get_global_reconciliation`
  (server-render only; NOT in `pipeline_stats_partial`, per the LOCKED render-path decision).
- `build_recent_scans()` attaches per-batch `_reconciliation` from one `get_agent_reconciliations`
  call — shared helper keeps `dashboard()` and `delete_scan()` in lockstep.
- Discovery DAG node renders a `N scanned · M deduped` subtitle gated `reconcile_deduped > 0`.
- Recent Scans FILES cell appends `→ N unique · M deduped` gated `batch._reconciliation.deduped > 0`.
- Three render tests cover the present / deduped==0-hidden / None-hidden states.

**Docs** — commit `cb542a1`
- `docs/architecture.md`: one-paragraph + table-row mention of the new reconciliation helpers.

## Tests

- 15 new service tests (arithmetic, clamp, latest-only re-scan, cross-agent SUM, non-completed
  exclusion, empty-DB → None, degrade-to-None / degrade-to-{}, global happy/hidden/clamp, per-agent
  dedup).
- 3 new render tests (subtitle present at deduped>0, absent at 0, absent at None).
- Full suite: **2013 passed**, coverage **97.45%** (requirement ≥85%).
- `pre-commit run --all-files`: all hooks **Passed** (ruff, ruff-format, bandit, mypy, etc.).

## Deviations from Plan

None of Rules 1–4 triggered. Two minor in-scope adjustments while making tests pass:
- **[Test harness]** Re-scan ordering tests seed `created_at` with **naive** datetimes — the test-DB
  `create_all` schema makes `created_at` `TIMESTAMP WITHOUT TIME ZONE`, so a tz-aware value raised
  an asyncpg `DataError`. Matches the existing tz-naive seeding constraint in the test DB.
- **[Lint]** Replaced U+2212 MINUS SIGN with ASCII hyphen in new docstrings/comments to satisfy
  ruff `RUF002`/`RUF003`.

Both are test/lint hygiene, not behavior changes.

## Known Stubs

None. Both annotations are wired to live DB-backed reconciliation data and hide cleanly when there
is nothing to show.

## Self-Check: PASSED

- `src/phaze/services/pipeline.py` — FOUND (deduped_count, get_scanned_total,
  get_global_reconciliation, get_agent_reconciliations present)
- `src/phaze/templates/pipeline/partials/dag_canvas.html` — FOUND (reconcile subtitle)
- `src/phaze/templates/pipeline/partials/recent_scans_table.html` — FOUND (per-agent annotation)
- Commits FOUND: `9d84093` (feat data layer), `8d805bd` (feat wiring + render tests),
  `cb542a1` (docs).
