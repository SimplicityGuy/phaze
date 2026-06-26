---
phase: quick-260609-pr5-delete-scans
plan: 01
subsystem: pipeline / scan-lifecycle
tags: [delete-scans, cascade, htmx, admin-ui, transactional]
requires:
  - PR4 scan activity indicator (last_progress_at heartbeat, stall reaper, elapsed helpers)
provides:
  - services/scan_deletion.py::delete_scan_cascade (ordered transactional FK cascade)
  - DELETE /pipeline/scans/{batch_id} HTMX endpoint with live/running 409 guards
  - routers/pipeline_scans.py::build_recent_scans shared helper
  - Recent Scans delete control (terminal rows only)
affects:
  - routers/pipeline.py::dashboard (refactored to use build_recent_scans; no behavior change)
tech-stack:
  added: []
  patterns:
    - "Set-based DELETE ... WHERE col IN (SELECT ...) with synchronize_session=False for bulk cascade (no identity-map load)"
    - "Caller-owns-transaction service (no commit inside) for atomic composition at the endpoint"
key-files:
  created:
    - src/phaze/services/scan_deletion.py
    - tests/test_services/test_scan_deletion.py
  modified:
    - src/phaze/routers/pipeline_scans.py
    - src/phaze/routers/pipeline.py
    - tests/test_routers/test_pipeline_scans.py
    - src/phaze/templates/pipeline/partials/recent_scans_table.html
    - docs/api.md
    - README.md
decisions:
  - "Application-level cascade (no migration): most FK columns have no ondelete rule; adding DB cascades would require a schema migration. Explicit ordered cascade is self-documenting and engine-independent."
  - "build_recent_scans looks up agent names across ALL agents (not just non-revoked) so a scan owned by a since-revoked agent still resolves to a human-readable name."
metrics:
  duration: ~40m
  completed: 2026-06-09
---

# Phase quick-260609 PR5: Delete Scans + Cascade Summary

DELETE control + ordered, set-based, transactional application-level cascade that removes a terminal `ScanBatch` and every row hanging off its files in one transaction, scoped strictly to that batch (zero collateral damage to other batches), exposed via an HTMX delete button on terminal Recent Scans rows.

## What shipped

- **`services/scan_deletion.py::delete_scan_cascade(session, batch_id) -> dict[str, int]`** — 13 ordered child->parent `DELETE ... WHERE col IN (SELECT ...)` statements, each scoped to `FileRecord.id WHERE batch_id == :bid`, run with `synchronize_session=False` (set-based, no identity-map load — handles tens of thousands of files). Does **not** commit (caller owns the transaction). Returns a per-table rows-deleted counts dict and emits a structlog INFO. Verified order: discogs_links -> tracklist_tracks -> tracklist_versions -> tracklists -> execution_log -> proposals -> fingerprint_results -> analysis -> metadata -> tag_write_log -> file_companions -> files -> scan_batches.
- **`DELETE /pipeline/scans/{batch_id}`** — 404 unknown; 409 `live` (watcher sentinel, never deletable); 409 `running` (only terminal scans deletable, server-side authoritative recheck); 200 + re-rendered Recent Scans table for terminal scans. Runs the cascade then commits atomically, logs `scan deleted` with counts.
- **`routers/pipeline_scans.py::build_recent_scans(session)`** — extracted the dashboard's last-10-non-LIVE query + transient-attr attachment (`_agent_name`, `_elapsed_seconds`, `_seconds_since_progress`, `_is_stalled`) into one shared helper. `pipeline.dashboard` refactored to call it (gap-14 lesson: no duplicated elapsed-seconds copy that could drift and crash the table).
- **`recent_scans_table.html`** — new Actions column; a red trash-icon delete button on `completed`/`failed` rows only (`hx-delete` + `hx-confirm` + `hx-target="#recent-scans"` + `hx-swap="outerHTML"` + `aria-label`); running rows render an empty cell; error-row colspan bumped 6 -> 7.
- **Docs** — `docs/api.md` DELETE row + cascade/409 note; README Recent Scans delete note.

## Critical correctness points (each test-backed, against real Postgres)

1. **No collateral deletion** — `test_cascade_does_not_touch_sibling_batch` seeds a SECOND full-graph batch and asserts exact surviving per-table counts after deleting the first.
2. **Cross-batch companions** — `test_cross_batch_companion_join_dies_but_other_file_survives`: a batch-A file linked via `file_companions` to a batch-B file. Deleting A removes only the join row; the batch-B file survives.
3. **4-level tracklist chain** — cascade follows `tracklists -> tracklist_versions -> tracklist_tracks -> discogs_links` (tracks hang off a VERSION, per the verified DAG).
4. **Nullable `tracklists.file_id`** — `test_null_file_id_tracklist_is_never_touched`: a scraped tracklist with `file_id=NULL` survives (scoping by `file_id IN F` never matches NULL).
5. **Delete policy** — `live` -> 409, `running` -> 409 (server-side recheck authoritative); template renders the delete control on terminal rows only.

## Tests

- `tests/test_services/test_scan_deletion.py` — 4 service tests (full-graph delete + counts dict, sibling intact, cross-batch companion, NULL-file_id tracklist). Real Postgres.
- `tests/test_routers/test_pipeline_scans.py` — added: completed delete (200 + row-gone + child file cascaded), failed deletable, 404 unknown, 409 live, 409 running, UI render (hx-delete on completed row, absent on running). Updated the failed-inline-error test colspan assertion 6 -> 7.

## Verification (run from the worktree)

1. **Service cascade test** — `uv run pytest tests/test_services/test_scan_deletion.py` -> `4 passed` (full-graph + second-batch-intact + cross-batch-companion + NULL-file_id, real Postgres).
2. **Endpoint tests** — completed->200, failed->200, unknown->404, live->409, running->409 -> all green.
3. **UI render test** — `test_recent_scans_table_delete_control_on_terminal_rows_only` -> green (hx-delete present on completed, absent on running; Actions header present).
4. **Full suite** — `uv run pytest` -> `1445 passed, 7 failed, 39 errors`. ALL 7 failures + 39 errors are in `test_agent_tracklists.py`, `test_agent_task_router.py`, `test_agent_exec_batches.py`, `test_execution_dispatch.py` and fail solely on `redis ConnectionError` (`Connect call failed ... :6379`) — Redis is not provisioned in this sandbox; identical on main, unrelated to this change.
5. **Lint/types** — `uv run ruff check .` -> All checks passed; `uv run ruff format --check .` -> 274 files already formatted; `uv run mypy .` -> Success: no issues found in 141 source files.
6. **Pre-commit** — `pre-commit run --all-files` -> all hooks pass (large-files, merge-conflicts, ruff, ruff-format, bandit, jsonschema, actionlint, yamllint, shellcheck, shfmt, mypy). Never used `--no-verify`.
7. **Migration** — none added. Application-level cascade as planned (the PR5 commit range touches no `alembic/` files). `alembic heads` is therefore unchanged from the PR4 base.

## Coverage

- `services/scan_deletion.py` — **100.00%**
- `routers/pipeline_scans.py` — **100.00%**
- `routers/pipeline.py` — 88.34% (uncovered 317-346, 354 are pre-existing fingerprint endpoints not touched here)
- Changed-module aggregate: 93.83% (>= 85% threshold).

## Deviations from Plan

**1. [Rule 2 — correctness] `build_recent_scans` resolves agent names across ALL agents, not only non-revoked.**
- **Found during:** Task 2 (extracting the dashboard query into a shared helper).
- **Issue:** The dashboard previously built its `agent_name_by_id` map from the non-revoked-agents dropdown query, so a Recent-Scans row owned by a since-revoked agent would display the raw `agent_id` instead of its name. Making the helper self-contained (signature `build_recent_scans(session)`) meant it needed its own name query.
- **Fix:** The helper issues `select(Agent.id, Agent.name)` over all agents for the name map. Strictly more correct (revoked agents still show a readable name) and keeps the dashboard's separate non-revoked dropdown query untouched. No dashboard test regressed.
- **Files modified:** `src/phaze/routers/pipeline_scans.py`, `src/phaze/routers/pipeline.py`.
- **Commit:** `0a12dfe`.

No other deviations — plan executed as written (3 tasks, the verified 13-step delete order, application-level cascade, no migration).

## Commits

- `6add7b1` feat(quick-260609-pr5-delete-scans): ordered transactional scan-deletion cascade
- `0a12dfe` feat(quick-260609-pr5-delete-scans): DELETE /pipeline/scans/{batch_id} + shared recent-scans helper
- `47aae93` feat(quick-260609-pr5-delete-scans): Recent Scans delete control + docs

## Self-Check: PASSED

- Files: `src/phaze/services/scan_deletion.py`, `tests/test_services/test_scan_deletion.py`, `src/phaze/routers/pipeline_scans.py`, `src/phaze/templates/pipeline/partials/recent_scans_table.html` — all FOUND.
- Commits `6add7b1`, `0a12dfe`, `47aae93` — all present in git log.
