---
quick_id: 260609-oar
slug: populate-scanbatch-total-files-via-pre-c
status: complete
---

# Quick Task Summary: Pre-count pass to populate ScanBatch.total_files during RUNNING

## What changed

`scan_directory` now performs a lightweight **pre-count pass** immediately after the
`scan_root.is_dir()` guard and before the hashing walk. The pre-count walks the tree once
with `os.walk(followlinks=False, ...)`, counts only files whose `_classify()` is in
`_EXTRACTABLE` (MUSIC/VIDEO) — **no `stat`, no hashing** — then PATCHes
`ScanBatchPatch(total_files=<precount>)` up front. This gives the Recent Scans
"processed / total" widget a real denominator during a RUNNING scan instead of rendering
"—" until completion.

The hashed `total` remains the source of truth: the per-chunk `processed_files` PATCHes and
the terminal `total_files=total` PATCH are unchanged, so any drift between the pre-count and
the hashing walk self-corrects at completion.

### Design decisions

- **Separate error list for the pre-count.** Pre-count walk errors are collected in a
  dedicated local list (`precount_walk_errors`) and are NOT merged into the hashing walk's
  `walk_errors`. The zero-access failure check (`total == 0 and walk_errors`) and its
  error-count message therefore stay driven solely by the authoritative hashing walk, so a
  permission failure is counted exactly once. An `onerror` callback is still passed to the
  pre-count walk so a read failure is logged, not silently swallowed. (Documented in a code
  comment.)
- **Pre-count PATCH is best-effort.** The `total_files` pre-count PATCH is wrapped in a
  `try/except AgentApiServerError` that logs a warning and continues. The pre-count is UX-only;
  if the controller is down it should not abort the scan — the authoritative hashing walk's
  per-chunk/terminal PATCHes drive the real controller-5xx failure handling.
- Added `logger.info("scan precount", batch_id=..., total=<precount>)`.
- No new imports of `phaze.database`, `phaze.models.*`, `sqlalchemy`, or
  `phaze.services.ingestion` — `tests/test_task_split.py` import invariant stays green.

## Files changed

- `src/phaze/tasks/scan.py` — added the pre-count pass + best-effort `total_files` PATCH.
- `tests/test_tasks/test_scan_directory.py` — new test asserting the `total_files` pre-count
  PATCH is sent before the first `upsert_files`; updated the per-chunk progress test (4 → 5
  PATCHes, asserts first PATCH carries the precount); updated the controller-5xx abort test
  (now 2 PATCHes, asserts the precount PATCH value).

## Verification

- **scan tests:** `uv run pytest tests/test_tasks/test_scan_directory.py tests/test_task_split.py`
  → `27 passed`.
- **scan module coverage:** `90.83%` (the only uncovered lines are the unrelated
  `scan_live_set` function, covered by other suites). The new pre-count lines are fully covered.
- **Full suite:** `1465 passed`. The `9 failed, 42 errors` are all
  `redis.exceptions.ConnectionError` (no Redis on localhost:6379 in this sandbox) in
  `test_agent_task_router` / `test_execution_dispatch` / `test_agent_tracklists` —
  pre-existing, environment-only, unrelated to this change. These pass in CI where Redis runs.
- **Lint:** `uv run ruff check` clean. **Types:** `uv run mypy` clean.
- **Pre-commit:** `pre-commit run --all-files` — all hooks Passed.

## Zero-access / partial-access semantics preserved

- `test_scan_directory_root_unreadable_fails` (zero-access) stays green: pre-count counts 0,
  PATCHes `total_files=0`, then the hashing walk's `total == 0 and walk_errors` still fires the
  terminal `failed` PATCH.
- `test_scan_directory_partial_access_still_completes` stays green: scan completes with the
  single summarizing warning.
