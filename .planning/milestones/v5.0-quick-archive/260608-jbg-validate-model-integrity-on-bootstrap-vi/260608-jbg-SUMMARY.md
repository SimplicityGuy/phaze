---
phase: 260608-jbg
plan: 01
subsystem: agent-model-bootstrap
tags: [resilience, integrity-validation, download, retry, timeout]
requires:
  - PR #91 (branch fix/model-bootstrap-transient-retry): _TIMEOUT, _MAX_ATTEMPTS, _TRANSIENT_ERRORS, _RetryableDownloadError, atomic os.replace
provides:
  - _with_retries (single shared bounded-retry/backoff/timeout helper for HEAD + GET)
  - _head_content_length / _try_head_size (bounded HEAD Content-Length probe + graceful degrade)
  - _ensure_present (per-file validate-or-download decision, size-based)
  - ensure_models_present without a glob-count gate (always size-validates via download_to)
affects:
  - phaze.tasks.agent_worker.startup (calls ensure_models_present on bootstrap)
tech-stack:
  added: []
  patterns:
    - "Every HTTP request (HEAD + GET) flows through _with_retries with explicit _TIMEOUT — no un-timeouted socket"
    - "On-disk byte size validated against server HEAD Content-Length; mismatch => remove + re-download"
    - "Completeness = all canonical files present AND size-valid (not a glob count)"
key-files:
  created: []
  modified:
    - src/phaze/scripts/download_models.py
    - src/phaze/tasks/_shared/model_bootstrap.py
    - tests/test_scripts/test_download_models.py
    - tests/test_services/test_model_bootstrap.py
decisions:
  - "PEP 695 type-parameter syntax for _with_retries[T] (satisfies ruff UP047 at py313 target; mypy strict clean) instead of a module-level TypeVar"
  - "_download_one cleans up its .part in an inner attempt fn on ANY exception (including fail-fast 4xx) then re-raises, so _with_retries decides retry-vs-propagate"
metrics:
  duration: ~25m
  completed: 2026-06-08
  tasks: 2
  files: 4
  commits: 2
---

# Phase 260608-jbg Plan 01: Validate model integrity on bootstrap Summary

Extends PR #91 so EVERY startup request (the new HEAD size probe and the GET) shares one bounded-retry/timeout helper, and replaces the truncation-blind glob-count gate with a per-file on-disk-size-vs-HEAD-Content-Length validation that removes and re-downloads corrupt files.

## What was built

**Task 1 — `download_models.py` (commit d2b817c):**
- `_with_retries[T](label, fn)`: the SINGLE retry/backoff/jitter/timeout-recovery implementation. Both `_head_content_length` (HEAD) and `_download_one` (GET) run through it. Transient (`_TRANSIENT_ERRORS`) + `_RetryableDownloadError` (5xx / truncated) are retried with bounded exponential backoff + jitter; any other exception (notably `httpx.HTTPStatusError` for 4xx) propagates immediately (fail-fast). After `_MAX_ATTEMPTS` it raises the per-label `RuntimeError` chained from the cause.
- `_head_content_length(url)`: HEAD with `_TIMEOUT`, 4xx fail-fast / 5xx retry, returns `int(Content-Length)` or `None` when the header is absent.
- `_try_head_size(url, dest)`: degrades a retry-exhausted (`RuntimeError`) or 4xx (`httpx.HTTPError`) HEAD to `None` with a WARNING — a HEAD failure never crashes or wedges the worker.
- `_ensure_present(url, dest)`: the LOCKED validate-or-download decision — keep a size-valid present file (no GET), remove + re-download a size-mismatched (truncated) one, keep an unvalidatable present file (WARNING), and GET a missing file.
- `_download_one`: stripped of its blind `if dest.exists(): return`; body is now an inner attempt fn that cleans its `.part` on any exception then re-raises, driven by `_with_retries`. Atomic `os.replace`, in-stream truncation check, 4xx fail-fast / 5xx retry, and the `# noqa: PTH105` / `# noqa: S311  # nosec B311` suppressions are byte-preserved.
- `download_to`: routes every `.pb`/`.json` (CLASSIFIER_MODELS then GENRE_MODELS, ordering + URL construction unchanged) through `_ensure_present`.

**Task 2 — `model_bootstrap.py` (commit b86babd):**
- Removed the `glob("*.pb")` count short-circuit and the partial/empty branch split — a truncated file can satisfy a count. `download_to(models_dir)` is now invoked unconditionally inside the preserved `RuntimeError(f"Model download failed: {exc}") from exc` wrap; the `__cause__` chaining is intact.
- Replaced the stale `~150MB, takes 2-5min` INFO text with the reality: ~3.1 GB across `_EXPECTED_MODEL_COUNT` (34) files, multi-GB / many-minutes (longer on a slow link) — so a legitimate transfer is not mistaken for a hang. `_EXPECTED_MODEL_COUNT` now feeds only that message.
- IMPORT-BOUNDARY preserved (stdlib + `phaze.scripts.download_models` only — no new imports; `test_task_split.py` green).

## Tests

All via `respx` (head + get) with monkeypatched `download_models.time.sleep` — zero real network, zero real sleep.
- (a) HEAD timeout on every attempt → bounded retry, HEAD route `call_count == _MAX_ATTEMPTS`; `_try_head_size` degrades to `None`.
- (b) present-but-truncated file → removed + re-fetched to full size, no `.part`.
- (c) transient GET errors then 200 → atomic success, sleeps the expected number of times.
- (d) fully valid size-matched set → `download_to` issues only HEADs, ZERO GETs, files untouched; valid single-file keep issues no GET.
- Plus: HEAD 5xx-retry / 4xx-fail-fast, HEAD no-Content-Length → `None`, unobtainable-size keep-present / download-missing paths.
- Adjusted PR #91 tests: `test_download_one_skips_when_dest_exists` removed (skip is now size-based); HEAD routes added to the fetch test; the idempotent test became case (d). Bootstrap tests rewritten to assert `download_to` is always called once (empty/partial/full) + the corrected estimate; `test_download_to_creates_pb_and_json_pairs` repointed at `_ensure_present`; the dead `_download_one`-idempotent test removed. `test_ensure_models_present_download_failure` (RuntimeError + `__cause__`) kept green.

## Verification

- Task 1 verify: `tests/test_scripts/test_download_models.py` 21 passed; ruff + mypy clean.
- Task 2 verify: `tests/test_services/test_model_bootstrap.py` + `tests/test_task_split.py` 12 passed; ruff + mypy clean.
- Changed-module coverage: `download_models.py` 100%, `model_bootstrap.py` 100% (33 tests).
- Repo-wide: `uv run ruff check .` → all checks passed; `uv run ruff format --check .` → 261 files already formatted; `uv run mypy .` → no issues in 135 source files. Per-commit pre-commit hooks (incl. mypy, bandit) passed on both commits.

## Deviations from Plan

**1. [Rule 3 - Blocking] PEP 695 type-parameter syntax for `_with_retries`.**
- **Found during:** Task 1 (ruff after first implementation).
- **Issue:** The module-level `TypeVar` + `Callable` form tripped `UP047` (wants type parameters) and `TC003` (move `Callable` into a `TYPE_CHECKING` block). Both block the lint gate.
- **Fix:** Switched to `def _with_retries[T](...)` (PEP 695; runtime is 3.14) and moved `Callable` into a `TYPE_CHECKING` block. The plan explicitly authorized "pick whatever typing form passes ruff (target py313) and mypy strict" — here PEP 695 is the form that passes cleanly. CLAUDE.md's py313-pin caveat concerns `TC`/`UP037` annotation rewrites that break Pydantic/SQLAlchemy/FastAPI runtime annotation resolution; this plain helper resolves no runtime annotations, so the caveat does not apply.
- **Files modified:** src/phaze/scripts/download_models.py
- **Commit:** d2b817c

## Known Stubs

None.

## Threat Flags

None — the changes implement the plan's `<threat_model>` mitigations (T-jbg-01 DoS via shared `_with_retries`/`_TIMEOUT`; T-jbg-02 truncation via `_ensure_present` size check) and introduce no new trust-boundary surface.

## Self-Check: PASSED

- src/phaze/scripts/download_models.py — FOUND (modified, committed d2b817c)
- src/phaze/tasks/_shared/model_bootstrap.py — FOUND (modified, committed b86babd)
- tests/test_scripts/test_download_models.py — FOUND (committed d2b817c)
- tests/test_services/test_model_bootstrap.py — FOUND (committed b86babd)
- Commit d2b817c — FOUND in git log
- Commit b86babd — FOUND in git log
