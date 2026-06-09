---
phase: quick-260608-u8g
plan: 01
subsystem: agent-model-bootstrap
tags: [essentia, bootstrap, async, startup, resilience]
requires:
  - phaze.scripts.download_models
  - phaze.tasks._shared.model_bootstrap
  - phaze.tasks.agent_worker
provides:
  - "Local size-manifest validation for essentia weights (zero-network healthy path)"
  - "Non-blocking agent startup models check via asyncio.to_thread"
affects:
  - "Agent worker boot path; scan_directory SAQ job no longer starved by model validation"
tech-stack:
  added: []
  patterns:
    - "Baked-in size MANIFEST derived programmatically from model tuples (drift-proof)"
    - "Pure os.stat local validation; network touched only to repair miss/mismatch"
    - "asyncio.to_thread to keep sync repair-path I/O off the async event loop"
key-files:
  created: []
  modified:
    - src/phaze/scripts/download_models.py
    - src/phaze/tasks/_shared/model_bootstrap.py
    - src/phaze/tasks/agent_worker.py
    - tests/test_scripts/test_download_models.py
    - tests/test_services/test_model_bootstrap.py
decisions:
  - "MANIFEST is built from CLASSIFIER_MODELS/GENRE_MODELS at import time so it cannot drift from the model list (T-u8g-04)"
  - "Healthy boot is pure os.stat with zero network; repair GET still validates streamed bytes against server Content-Length (T-u8g-01)"
  - "Deleted the three steady-state HEAD helpers (_head_content_length, _try_head_size, _ensure_present) - no external consumers"
  - "asyncio.to_thread wraps the sync ensure_models_present so the rare repair path cannot freeze the startup event loop (T-u8g-03)"
metrics:
  duration: "~12 min"
  completed: "2026-06-09"
  tasks: 3
  files: 5
---

# Phase quick-260608-u8g Plan 01: Model Bootstrap Local Validation Summary

Reworked the essentia model bootstrap from "always HEAD-validate all 68 weight files against essentia.upf.edu on every worker boot" to **local-validation-only** against a baked-in byte-size manifest; the network is touched only to repair a missing or wrong-size file, and the rare repair path is moved off the async startup event loop via `asyncio.to_thread`.

## What Was Built

- **Baked-in size `MANIFEST`** (`download_models.py`): `dict[str, int]` built programmatically by `_build_manifest()` from `CLASSIFIER_MODELS` + `GENRE_MODELS`, covering exactly 68 files. `.pb` sizes resolve via `_expected_pb_size()` (musicnn-common / vggish / discogs-effnet / the lone `voice_instrumental-musicnn-msd-1` outlier); `.json` sizes come from the `_JSON_SIZES` literal (authoritative server `Content-Length` at model-pin time).
- **`_ensure_present_local(url, dest, expected_size)`**: present + correct size -> return with zero network (pure `os.stat`); missing -> INFO + `_download_one`; present-but-wrong-size -> WARN naming on-disk vs manifest size + `_download_one` re-fetch.
- **HEAD-free `download_to`**: walks both model families, calls `_ensure_present_local` with `MANIFEST[...]`. A fully valid directory issues ZERO HTTP requests.
- **Deleted** `_head_content_length`, `_try_head_size`, `_ensure_present`. Kept `_with_retries`, `_download_one`, retry constants, and the atomic `.part` repair semantics unchanged.
- **`model_bootstrap.py`** docstrings updated to the 260608-u8g local-validation contract; behavior unchanged (`download_to` still called unconditionally; failures still wrapped in `RuntimeError`).
- **`agent_worker.startup`**: `ensure_models_present(...)` -> `await asyncio.to_thread(ensure_models_present, ...)`, plus `import asyncio` (stdlib - import-boundary invariant unaffected).

## Tasks & Commits

| Task | Name | Commit | Type |
| ---- | ---- | ------ | ---- |
| 1 | Baked-in size manifest + local-validation download path + tests | `9867d2f` | feat |
| 2 | model_bootstrap contract docs for local validation | `4785885` | docs |
| 3 | Non-blocking startup models check via asyncio.to_thread | `458d4d5` | fix |

## Verification

### `uv run ruff format .` / `uv run ruff check .`
```
263 files left unchanged
All checks passed!
```

### `uv run mypy .`
```
Success: no issues found in 136 source files
```

### Behavioral checks
```
=== 1. MANIFEST == 68 ===
OK 68 files
=== 2. asyncio.to_thread present ===
107:    await asyncio.to_thread(ensure_models_present, Path(cfg.models_path))
=== 3. HEAD helpers gone ===
NONE FOUND (good)
=== 4. download_to steady-state has no httpx.head ===
no httpx.head anywhere (good)
```

### Targeted tests + coverage (changed modules)
```
tests/test_scripts/test_download_models.py + test_model_bootstrap.py
+ test_agent_startup_banner.py + test_phase04_gaps.py + test_task_split.py  ->  37 passed

Name                                         Stmts   Miss   Cover
src/phaze/scripts/download_models.py            99      0 100.00%
src/phaze/tasks/_shared/model_bootstrap.py      15      0 100.00%
src/phaze/tasks/agent_worker.py                 61      2  96.72%  (lines 171-172: pre-existing module-import queue-name guard)
TOTAL                                          175      2  98.86%   (gate 85%)
```

### `pre-commit run --all-files`
All 24 hooks passed (large-files, toml/yaml/json, EOF, trailing-ws, ruff, ruff-format, bandit, jsonschema, actionlint, yamllint, shellcheck, shfmt, mypy).

### Full suite
`uv run pytest --cov` -> **1390 passed**, 7 failed, 39 errors. **All 46 non-passing tests are in `tests/test_routers/`, `tests/test_services/test_agent_task_router.py`, and `tests/test_execution_dispatch.py` and fail with `redis.exceptions.ConnectionError` (no Redis on localhost:6379 in this sandbox).** They are infrastructure-dependent integration tests, pre-existing, and unrelated to this change (model bootstrap requires no Redis/Postgres). Every test touching the modified modules passes.

## Deviations from Plan

None - plan executed exactly as written. The three steady-state HEAD helpers were deleted as specified, the manifest is programmatically derived, and all `_download_one` repair tests were retained.

## Threat Model Coverage

- **T-u8g-01** (tampering, repaired file): repair GET still streams to `.part` and validates byte count vs server `Content-Length` before atomic `os.replace` - unchanged `_download_one`.
- **T-u8g-03** (DoS, flaky remote freezing startup): healthy path is pure `os.stat`; repair path runs under `asyncio.to_thread` so it cannot block the event loop or starve `scan_directory`.
- **T-u8g-04** (manifest drift): `MANIFEST` built programmatically from the model tuples; `test_manifest_covers_exactly_68_files` asserts exactly 68 entries.

## Known Stubs

None.

## Self-Check: PASSED
- Files: all 5 modified files present on disk.
- Commits: `9867d2f`, `4785885`, `458d4d5` all present in `git log`.
