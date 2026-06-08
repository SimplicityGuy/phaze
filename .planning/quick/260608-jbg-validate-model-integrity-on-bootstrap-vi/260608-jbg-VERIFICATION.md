---
phase: 260608-jbg
verified: 2026-06-08T00:00:00Z
status: passed
score: 6/6 must-haves verified
overrides_applied: 0
re_verification: false
---

# Quick Task 260608-jbg: Validate Model Integrity on Bootstrap — Verification Report

**Task Goal:** On every bootstrap, validate each essentia model file's on-disk byte size against the server's HEAD Content-Length; re-download missing/truncated/corrupt files; ensure NO request (HEAD or GET) can wedge the worker; remove the count-only completeness gate; correct the stale ~150MB/2-5min estimate to ~3.1GB/34 files.
**Branch:** fix/model-bootstrap-transient-retry (extends PR #91)
**Verified:** 2026-06-08
**Status:** PASSED
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| #  | Truth | Status | Evidence |
|----|-------|--------|----------|
| 1  | Every HTTP request (HEAD and GET) is issued with an explicit timeout and bounded retry; no request can block the worker indefinitely | VERIFIED | `httpx.head(..., timeout=_TIMEOUT)` at line 173; `httpx.stream("GET", ..., timeout=_TIMEOUT)` at line 254; both routed through `_with_retries` at lines 184 and 279 — only 2 httpx call sites exist in the file, both with timeout |
| 2  | On startup each model file's on-disk byte size is validated against the server's HEAD Content-Length | VERIFIED | `_ensure_present` calls `_try_head_size` (HEAD probe) per file; `download_to` calls `_ensure_present` for every .pb/.json; `ensure_models_present` unconditionally calls `download_to` |
| 3  | A present-but-truncated file (on-disk size != Content-Length) is removed and re-downloaded to the full size | VERIFIED | `_ensure_present` lines 220-226: `if actual != expected:` → `dest.unlink()` → `_download_one(url, dest)`; test `test_ensure_present_redownloads_truncated_file` (case b) proves it end-to-end |
| 4  | A fully valid on-disk set triggers only HEAD requests (no GET) and returns without an operator restart | VERIFIED | `_ensure_present` lines 221-223: `if actual == expected: return` — early return, no GET; test `test_download_to_valid_set_issues_only_heads_no_get` (case d) asserts `all(route.call_count == 0 for route in get_routes)` |
| 5  | ensure_models_present no longer short-circuits on a glob count; it always validates via download_to | VERIFIED | No `glob` call exists anywhere in `model_bootstrap.py`; `ensure_models_present` calls `download_to(models_dir)` unconditionally inside its try/except wrap (line 75) |
| 6  | The operator-facing log reflects ~3.1 GB across 34 files (multi-GB, many minutes) not the stale ~150MB/2-5min estimate | VERIFIED | `model_bootstrap.py` lines 68-70: `"~3.1 GB across %d files... multi-GB and can take many minutes"`; grep for "150MB" and "2-5min" returns no matches |

**Score:** 6/6 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/scripts/download_models.py` | `_with_retries`, `_head_content_length`, `_ensure_present`, `_download_one` without blind skip | VERIFIED | All four symbols present and substantive; `_download_one` starts at line 249 with `dest.parent.mkdir(...)` — no `if dest.exists(): return`; both `dest.exists()` occurrences are in `_ensure_present` (lines 215, 220), not `_download_one` |
| `src/phaze/tasks/_shared/model_bootstrap.py` | No count gate; always calls `download_to`; corrected size estimate | VERIFIED | No `glob`, no count comparison; `download_to(models_dir)` at line 75; "3.1 GB" and "many minutes" in log |
| `tests/test_scripts/test_download_models.py` | Cases a/b/c/d + respx + monkeypatched sleep | VERIFIED | All four required test cases present; `_patch_sleep` monkeypatches `download_models.time.sleep`; respx intercepts HEAD and GET |
| `tests/test_services/test_model_bootstrap.py` | No count gate tests; RuntimeError wrap / `__cause__` kept green | VERIFIED | Three tests assert `download_to` is called once for empty/partial/full dirs; `test_ensure_models_present_download_failure` asserts `RuntimeError("Model download failed")` and `excinfo.value.__cause__ is underlying` |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `download_to` | `_ensure_present` | Routes every .pb/.json call | VERIFIED | Lines 294-298 call `_ensure_present(...)` for each model in CLASSIFIER_MODELS then GENRE_MODELS |
| `_head_content_length` and `_download_one` | `_with_retries` | Single shared retry/backoff/timeout | VERIFIED | `_head_content_length` returns `_with_retries(f"HEAD {url}", _attempt)` at line 184; `_download_one` calls `_with_retries(f"download {dest.name}", _attempt)` at line 279 |
| `ensure_models_present` | `download_to` | Unconditional call; no count gate | VERIFIED | No glob/count gate; `download_to(models_dir)` at line 75 is the only path |

---

### Behavioral Spot-Checks (Test Suite)

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full targeted test suite | `uv run pytest tests/test_scripts/test_download_models.py tests/test_services/test_model_bootstrap.py tests/test_task_split.py -q` | 33 passed in 1.95s | PASS |
| Case (a): HEAD times out every attempt → bounded by `_MAX_ATTEMPTS` | `test_head_timeout_retries_bounded_then_raises` — asserts `route.call_count == _MAX_ATTEMPTS` | Passes | PASS |
| Case (b): truncated on-disk file removed + re-downloaded to full size | `test_ensure_present_redownloads_truncated_file` — asserts final size == full payload, no .part | Passes | PASS |
| Case (c): transient GET errors then 200 → atomic success, no .part, N-1 sleeps | `test_download_one_retries_transient_then_succeeds` — asserts dest exists, payload correct, no .part, `len(sleeps) == 2` | Passes | PASS |
| Case (d): fully valid size-matched set → ZERO GETs, files untouched | `test_download_to_valid_set_issues_only_heads_no_get` — asserts `all(route.call_count == 0 for route in get_routes)` | Passes | PASS |
| Import boundary: model_bootstrap is Postgres-free | `test_model_bootstrap_stays_postgres_free` (in test_task_split.py) | Passes | PASS |

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| None | — | — | — | — |

No TBD/FIXME/XXX/TODO markers found in modified files. No stub implementations. No hardcoded empty returns. No unresolved debt.

---

### Specific Plan Checks (from verification instructions)

**`_with_retries` is the SINGLE retry/backoff/timeout impl; BOTH HEAD and GET call it:**
Confirmed. Only two `_with_retries(` call sites exist: line 184 (`_head_content_length`) and line 279 (`_download_one`).

**Every httpx request uses `timeout=_TIMEOUT` — no httpx call without timeout:**
Only two httpx request calls in the file: `httpx.head(url, follow_redirects=True, timeout=_TIMEOUT)` (line 173) and `httpx.stream("GET", url, follow_redirects=True, timeout=_TIMEOUT)` (line 254). Zero un-timeouted calls.

**`_download_one` has NO blind `dest.exists(): return`:**
Confirmed. `_download_one` begins at line 249 with `dest.parent.mkdir(...)`. The two `dest.exists()` checks in the file (lines 215, 220) are inside `_ensure_present`, not `_download_one`.

**`_download_one` cleans `.part` on every failed attempt:**
Confirmed. The `except Exception:` block at lines 272-277 calls `tmp.unlink(missing_ok=True)` before re-raising, covering transient, 5xx, truncated, and 4xx failures.

**`download_to` routes through `_ensure_present`; CLASSIFIER_MODELS/GENRE_MODELS order unchanged:**
Confirmed. Lines 292-298: CLASSIFIER_MODELS loop first, then GENRE_MODELS loop; each calls `_ensure_present(...)` for both `.pb` and `.json`.

**`ensure_models_present` RuntimeError wrap + `__cause__` preserved:**
Confirmed. Lines 76-78: `raise RuntimeError(msg) from exc`. Test `test_ensure_models_present_download_failure` asserts `excinfo.value.__cause__ is underlying`.

**IMPORT-BOUNDARY intact (no new non-stdlib imports):**
Confirmed. Imports in `model_bootstrap.py`: `logging` (stdlib), `typing.TYPE_CHECKING` (stdlib), `pathlib.Path` (stdlib, TYPE_CHECKING only), `phaze.scripts.download_models` (phaze package). No new non-stdlib imports added.

**Message says ~3.1GB/34 files and does NOT contain "150MB" or "2-5min":**
Confirmed. Log at lines 68-70 contains "~3.1 GB across %d files" and "multi-GB and can take many minutes". Grep for "150MB" and "2-5min" returns no results.

---

### Human Verification Required

None. All behaviors are verified programmatically via the test suite with respx mock transport and monkeypatched sleep.

---

## Summary

All 6 must-have truths are VERIFIED against the actual code. The implementation correctly:

- Extracts `_with_retries[T]` as the single bounded-retry helper shared by HEAD (`_head_content_length`) and GET (`_download_one`), with every httpx call carrying `timeout=_TIMEOUT`
- Adds `_try_head_size` as a graceful degradation wrapper so HEAD failures never crash or wedge
- Implements `_ensure_present` with the four LOCKED decision paths (valid-keep, truncated-remove-redownload, unobtainable-keep-warn, missing-download)
- Strips `_download_one`'s blind `dest.exists(): return` skip; `.part` is cleaned on every failed attempt
- Routes `download_to` entirely through `_ensure_present`
- Removes the glob-count short-circuit from `ensure_models_present`, always calling `download_to`
- Corrects the stale ~150MB/2-5min estimate to ~3.1 GB / 34 files / multi-GB / many-minutes
- All 33 tests pass with zero real network I/O or sleep; IMPORT-BOUNDARY enforced by test_task_split.py

---

_Verified: 2026-06-08_
_Verifier: Claude (gsd-verifier)_
