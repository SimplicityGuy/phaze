---
quick_id: 260609-oar
slug: populate-scanbatch-total-files-via-pre-c
status: planned
---

# Quick Task: Pre-count pass to populate ScanBatch.total_files during RUNNING

## Problem

The Recent Scans "N / Z" progress widget (`templates/pipeline/partials/recent_scans_table.html:64`)
renders `processed_files / total_files`. During a RUNNING scan, `total_files` stays at its
initial `0` (set at batch creation in `routers/pipeline_scans.py:358`) and is only written in the
terminal success PATCH (`tasks/scan.py:267`). So `{{ batch.total_files or '—' }}` renders "—" for
the entire scan and only fills in a real denominator at completion.

## Fix

In `src/phaze/tasks/scan.py::scan_directory`, add a **pre-count pass** before the hashing walk:

1. After the existing `scan_root.is_dir()` guard (line ~160) and before the hashing `os.walk`
   loop, perform a lightweight `os.walk(scan_root, followlinks=False, onerror=_on_walk_error)`
   that counts files whose `_classify(filename)` is in `_EXTRACTABLE` (MUSIC/VIDEO). **No stat,
   no hashing** — just count names. This is fast even on a large network mount.
2. PATCH `ScanBatchPatch(total_files=<precount>)` so the denominator is populated up front.
3. Keep the per-chunk PATCHes (`processed_files=total`) exactly as-is.
4. Keep the terminal success PATCH setting `total_files=total` (the real hashed total) — this
   self-corrects any drift if files appeared/disappeared between the pre-count and the hashing
   walk. The pre-count is an estimate-for-UX; the hashed `total` remains the source of truth at
   completion.

### Design notes / constraints

- Reuse the existing `_on_walk_error`/`walk_errors` machinery is NOT required for the pre-count —
  but the pre-count walk should pass the SAME `onerror=_on_walk_error` callback so a permission
  failure is still recorded (do not let the pre-count silently swallow walk errors). Make sure the
  zero-access-failure logic (line ~235, `total == 0 and walk_errors`) still behaves correctly:
  the pre-count appending to `walk_errors` is fine because the existing check is gated on
  `total == 0` from the hashing walk. If anything, double-counting walk_errors across both passes
  is acceptable (it only affects the error-count number in the failure message). To keep the
  failure message count clean, prefer counting pre-count walk errors into a SEPARATE local list
  that is NOT merged into `walk_errors`, OR reset `walk_errors` before the hashing walk. Choose
  whichever keeps the existing zero-access failure test green; document the choice in a comment.
- Do not add stat/hash to the pre-count — it must stay cheap.
- Module import invariant still holds: no new imports of `phaze.database`, `phaze.models.*`,
  `sqlalchemy`, or `phaze.services.ingestion` (enforced by `tests/test_task_split.py`).
- Add an operational log line: `logger.info("scan precount", batch_id=..., total=<precount>)`.

## Files

- `src/phaze/tasks/scan.py` — add pre-count pass + total_files PATCH.
- `tests/` — update/extend the `scan_directory` tests: the patch_scan_batch call sequence now
  begins with a `total_files=<precount>` PATCH. Add a test asserting `total_files` is PATCHed
  before the first `upsert_files` call. Keep the existing zero-access / partial-access /
  controller-5xx tests green.

## Verification

- `uv run pytest tests/ -k scan` passes.
- `uv run pytest` full suite passes, coverage ≥ 85%.
- `pre-commit run --all-files` clean (ruff, mypy, bandit, etc.).
- Manual reasoning: during RUNNING, Recent Scans shows `processed / precount` instead of
  `processed / —`.
