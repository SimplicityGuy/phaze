---
phase: 260608-jbg
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/scripts/download_models.py
  - src/phaze/tasks/_shared/model_bootstrap.py
  - tests/test_scripts/test_download_models.py
  - tests/test_services/test_model_bootstrap.py
autonomous: true
requirements:
  - QT-260608-jbg   # LOCKED CONTEXT.md decisions: HEAD size validation, shared bounded-retry, drop count gate, fix stale estimate

must_haves:
  truths:
    - "Every HTTP request (HEAD and GET) is issued with an explicit timeout and bounded retry; no request can block the worker indefinitely"
    - "On startup each model file's on-disk byte size is validated against the server's HEAD Content-Length"
    - "A present-but-truncated file (on-disk size != Content-Length) is removed and re-downloaded to the full size"
    - "A fully valid on-disk set triggers only HEAD requests (no GET) and returns without an operator restart"
    - "ensure_models_present no longer short-circuits on a glob count; it always validates via download_to"
    - "The operator-facing log reflects ~3.1 GB across 34 files (multi-GB, many minutes) not the stale ~150MB/2-5min estimate"
  artifacts:
    - path: src/phaze/scripts/download_models.py
      provides: "_with_retries shared helper, _head_content_length, _ensure_present, _download_one without blind dest.exists() skip"
      contains: "_with_retries"
    - path: src/phaze/tasks/_shared/model_bootstrap.py
      provides: "ensure_models_present always calls download_to (count gate removed) + corrected size estimate"
    - path: tests/test_scripts/test_download_models.py
      provides: "size-validation tests (a/b/c/d) using respx.head + respx.get, monkeypatched time.sleep"
    - path: tests/test_services/test_model_bootstrap.py
      provides: "adjusted bootstrap tests (no count gate) + RuntimeError-wrap/__cause__ kept green"
  key_links:
    - from: src/phaze/scripts/download_models.py
      to: "_ensure_present"
      via: "download_to calls _ensure_present per (url, dest)"
      pattern: "_ensure_present\\("
    - from: "_head_content_length and _download_one"
      to: "_with_retries"
      via: "single shared retry/backoff/timeout implementation used by both HEAD and GET"
      pattern: "_with_retries\\("
    - from: src/phaze/tasks/_shared/model_bootstrap.py
      to: "download_to"
      via: "ensure_models_present always invokes download_to (no _EXPECTED_MODEL_COUNT >= gate)"
      pattern: "download_to\\(models_dir\\)"
---

<objective>
Validate essentia model integrity on every worker bootstrap via a per-file HEAD Content-Length size check, re-downloading corrupt/truncated files, while guaranteeing no request can wedge the worker. Extends PR #91 (branch `fix/model-bootstrap-transient-retry`) — same branch, NO new PR.

Purpose: A live first-deploy surfaced two failures worse than the original crash-loop — (1) a no-timeout request that hung the worker 16+ min with `restart: unless-stopped` never firing, and (2) a count-only completeness check that blessed a truncated `.pb` as "present". This plan extends PR #91's GET-only resilience to ALL requests (including the new HEAD validation) and replaces the glob-count gate with size validation.

Output: HEAD-based size validation sharing PR #91's timeout + bounded-retry machinery; `_download_one` stripped of its blind existence skip; `ensure_models_present` with the count short-circuit removed and the stale `~150MB/2-5min` estimate corrected to ~3.1 GB across 34 files; full test coverage of the hang, truncation, transient-recovery, and valid-set-no-GET paths.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/quick/260608-jbg-validate-model-integrity-on-bootstrap-vi/260608-jbg-CONTEXT.md
@CLAUDE.md
@src/phaze/scripts/download_models.py
@src/phaze/tasks/_shared/model_bootstrap.py
@tests/test_scripts/test_download_models.py
@tests/test_services/test_model_bootstrap.py

<interfaces>
<!-- Current contracts the executor builds on. Extracted from the codebase — no exploration needed. -->

From src/phaze/scripts/download_models.py (current, post-PR #91):
- `_TIMEOUT: httpx.Timeout` (connect=15, read=60, write=60, pool=15) — the ONLY allowed per-request timeout.
- `_MAX_ATTEMPTS = 5`, `_BACKOFF_BASE_SECONDS = 1.0`, `_BACKOFF_MAX_SECONDS = 30.0`, `_JITTER_SECONDS = 1.0`.
- `_TRANSIENT_ERRORS: tuple` — httpx.TransportError, ssl.SSLError, socket.timeout, TimeoutError, urllib.error.URLError, ConnectionError. (httpx.HTTPStatusError deliberately NOT in it → 4xx fails fast.)
- `class _RetryableDownloadError(Exception)` — sentinel for 5xx + truncated-read (application-level retryable).
- `CLASSIFIER_MODELS: tuple[str, ...]` (33) and `GENRE_MODELS: tuple[str, ...]` (1) — byte-for-byte order MUST be preserved.
- `_download_one(url: str, dest: Path) -> None` — currently: blind `if dest.exists(): return`; then inline retry loop over `httpx.stream("GET", ...)`, in-stream Content-Length truncation check, `os.replace(tmp, dest)  # noqa: PTH105`, jitter line `# noqa: S311  # nosec B311`.
- `download_to(target_dir: Path) -> None` — iterates CLASSIFIER_MODELS then GENRE_MODELS, calling `_download_one` for `.pb` and `.json` each, under bases `_CLASSIFIER_BASE` / `_GENRE_BASE`.

From src/phaze/tasks/_shared/model_bootstrap.py (current):
- IMPORT-BOUNDARY: Postgres-free; imports stdlib + `phaze.scripts.download_models` only (verified by tests/test_task_split.py::test_model_bootstrap_stays_postgres_free).
- `_EXPECTED_MODEL_COUNT = len(CLASSIFIER_MODELS) + len(GENRE_MODELS)` (== 34).
- `ensure_models_present(models_dir: Path) -> None` — count-only gate (`len(glob("*.pb")) >= _EXPECTED_MODEL_COUNT` early return), partial-WARNING branch, empty-INFO branch with the stale `~150MB, takes 2-5min` text, then `download_to` wrapped so any Exception → `RuntimeError(f"Model download failed: {exc}") from exc`.

respx note: `respx.head(url)` mocks HEAD, `respx.get(url)` mocks GET; both intercept httpx transport. `side_effect=[...]` sequences per-attempt responses/exceptions. Patch `download_models.time.sleep` to a no-op counter — zero real network, zero real sleep.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Shared bounded-retry helper + HEAD size validation + validate-or-download in download_models.py</name>
  <files>src/phaze/scripts/download_models.py, tests/test_scripts/test_download_models.py</files>
  <behavior>
    Drive these with respx (head + get) and a monkeypatched `download_models.time.sleep` (no real network/sleep):
    - Hang/timeout (case a): HEAD raises ConnectTimeout/ReadTimeout on every attempt → `_head_content_length` retries with bounded backoff and the HEAD route `call_count == _MAX_ATTEMPTS` (never unbounded). Same bound already proven for GET by the kept `test_download_one_raises_after_exhausting_attempts`.
    - Valid-keep (part of d): present file whose on-disk size == HEAD Content-Length → `_ensure_present` keeps it and issues NO GET (get route `call_count == 0`).
    - Truncated re-download (case b): pre-write a SHORT file (e.g. 5 bytes), HEAD Content-Length == full payload length, GET serves the full payload → `_ensure_present` unlinks the stale file and re-downloads; final on-disk size == full length; no `.part` remains.
    - Unobtainable size, present file: HEAD 200 with NO Content-Length header (or HEAD failing after retries) AND file present → keep it, emit a WARNING, issue NO GET.
    - Unobtainable size, missing file: HEAD without Content-Length AND file absent → fall through to GET, which downloads.
    - Transient recovery (case c): file missing, HEAD 200 with Content-Length, GET ConnectError×2 then 200 → file lands atomically, no `.part`, sleeps twice. (The existing `_download_one`-level transient/5xx/truncated tests stay green and also cover c.)
    - Valid-set no-GET (case d): a fully pre-populated dir where every HEAD reports Content-Length == the on-disk sentinel size → `download_to` issues only HEADs, ZERO GETs, files untouched.
  </behavior>
  <action>
    Refactor the inline retry loop out of `_download_one` into a shared generic helper `_with_retries(label: str, fn: Callable[[], T]) -> T` that: loops `1..=_MAX_ATTEMPTS`, calls `fn()`, returns its value on success; on `(*_TRANSIENT_ERRORS, _RetryableDownloadError)` cleans up nothing itself but sleeps `min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)) + random.uniform(0, _JITTER_SECONDS)` (keep the `# noqa: S311  # nosec B311` suppression) and retries; after the final attempt raises `RuntimeError(f"Failed to {label} after {_MAX_ATTEMPTS} attempts: {exc}") from exc` and logs the same per-attempt WARNING as today. Any other exception (notably httpx.HTTPStatusError from a 4xx) propagates immediately — preserving 4xx fail-fast. Add `from collections.abc import Callable` and a module TypeVar; pick whatever typing form passes ruff (target py313) and mypy strict — do not force PEP 695 if it triggers UP rewrites.

    Add `_head_content_length(url: str) -> int | None`: define an inner attempt that issues `httpx.head(url, follow_redirects=True, timeout=_TIMEOUT)`, calls `response.raise_for_status()` for 4xx (fail-fast, not retried) and raises `_RetryableDownloadError` for status >= 500 (retried), then returns `int(response.headers["Content-Length"])` or `None` when the header is absent; run it through `_with_retries(f"HEAD {dest_or_url_label}", ...)`. Add a thin wrapper `_try_head_size(url: str, dest: Path) -> int | None` that calls `_head_content_length` and, on `(RuntimeError, httpx.HTTPError)`, logs a WARNING naming `dest.name` and returns `None` — so a HEAD that exhausts retries or 4xxs degrades gracefully to the "unobtainable" path instead of wedging or crashing.

    Add `_ensure_present(url: str, dest: Path) -> None` implementing the LOCKED validate-or-download decision (CONTEXT.md decisions block): compute `expected = _try_head_size(url, dest)`. If `expected is None` (unobtainable): when `dest.exists()` log a WARNING ("cannot validate {dest.name}, keeping existing file") and return; otherwise call `_download_one(url, dest)`. If `expected` is known: when `dest.exists()` and `dest.stat().st_size == expected` return (valid, no GET); when `dest.exists()` but size mismatches, log a WARNING with on-disk vs expected sizes, `dest.unlink()` the stale file, then `_download_one(url, dest)`; when `dest` is missing, `_download_one(url, dest)`.

    Strip the blind `if dest.exists(): return` from `_download_one` — the skip decision now lives in `_ensure_present` (size-based). `_download_one` becomes "download this file atomically": keep parent `mkdir`, the `<dest>.part` stream, the in-stream Content-Length truncation check (raises `_RetryableDownloadError`), the atomic `os.replace(tmp, dest)  # noqa: PTH105`, and `.part` cleanup. Implement its body as an inner attempt fn that, on ANY exception, `tmp.unlink(missing_ok=True)` before re-raising, then drive it via `_with_retries(f"download {dest.name}", ...)` so the per-file named RuntimeError and 4xx fail-fast behavior are byte-identical to PR #91. Route `download_to` through `_ensure_present` (not `_download_one`) for every `.pb`/`.json` across CLASSIFIER_MODELS then GENRE_MODELS — preserve the exact ordering and URL construction. Update the `_download_one`/`download_to` docstrings to drop the "skips files that already exist" idempotency claim and describe size-validation.

    Adjust the existing PR #91 tests in tests/test_scripts/test_download_models.py: rewrite `test_download_one_skips_when_dest_exists` into a `_ensure_present` valid-keep test (HEAD size == on-disk size → no GET), since `_download_one` no longer skips; add HEAD routes to `test_download_to_fetches_classifier_and_genre_urls` (each file now HEADs then GETs); rewrite `test_download_to_is_idempotent_on_already_populated_dir` into case (d) — register `respx.head` returning Content-Length equal to the 1-byte sentinel size and assert ZERO GET calls. Keep the transient/5xx/exhaustion/truncated `_download_one` tests as-is (they call `_download_one` directly and remain valid). Add the new case (a)/(b)/unobtainable tests from the behavior block.
  </action>
  <verify>
    <automated>uv run pytest tests/test_scripts/test_download_models.py -x && uv run ruff check src/phaze/scripts/download_models.py tests/test_scripts/test_download_models.py && uv run mypy src/phaze/scripts/download_models.py</automated>
  </verify>
  <done>`_with_retries` is the single retry implementation used by both `_head_content_length` and `_download_one`; `_download_one` has no `dest.exists()` skip; `download_to` calls `_ensure_present` per file with ordering preserved; tests a/b/c/d pass; adjusted PR #91 tests pass; ruff + mypy clean.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Remove count gate from ensure_models_present, correct the size estimate, adjust bootstrap tests</name>
  <files>src/phaze/tasks/_shared/model_bootstrap.py, tests/test_services/test_model_bootstrap.py</files>
  <behavior>
    - Empty/any dir → `ensure_models_present` ALWAYS calls `download_to(models_dir)` once (no glob-count short-circuit); the startup log states ~3.1 GB across 34 files / multi-GB / many-minutes and does NOT contain "150MB" or "2-5min".
    - A fully valid pre-populated set → `ensure_models_present` returns without error and still calls `download_to` exactly once (download_to is responsible for the per-file HEAD validation and issues no GET when sizes match — proven in Task 1's case d).
    - Download failure → `RuntimeError` matching "Model download failed" with `excinfo.value.__cause__ is underlying` (this existing test MUST stay green).
  </behavior>
  <action>
    In `ensure_models_present`: remove the `pb_files = list(models_dir.glob("*.pb"))` count gate, the `len(pb_files) >= _EXPECTED_MODEL_COUNT` early return, and the partial/empty branch split — a truncated file can satisfy the count, so completeness is now "all canonical files present AND size-valid", enforced entirely by `download_to`'s per-file `_ensure_present` validation. Always call `download_to(models_dir)` inside the existing try/except that wraps any Exception into `RuntimeError(f"Model download failed: {exc}") from exc` (keep this wrap and the `__cause__` chaining intact). Replace the stale `~150MB, takes 2-5min on first start` INFO text with a single pre-validation INFO line stating reality: ~3.1 GB across {_EXPECTED_MODEL_COUNT} files, a fresh download is multi-GB and can take many minutes (longer on a slow link) so operators don't mistake a legitimate transfer for a hang. Keep `_EXPECTED_MODEL_COUNT` (now used only for that message, not as a gate) and the IMPORT-BOUNDARY intact (stdlib + phaze.scripts.download_models only — add no new imports). Update the docstring to describe always-validate (size-based via download_to) instead of the count-equals-expected contract.

    Adjust tests/test_services/test_model_bootstrap.py: rewrite `test_ensure_models_present_populated_no_op` so it asserts `download_to` IS called once for a populated set (the no-GET-on-valid-set assertion now lives in Task 1's download_models case d, not here) and the new size-estimate log appears; fold/rewrite `test_ensure_models_present_partial_triggers_redownload` to assert the count gate is gone (a partial dir still calls `download_to` once — no special partial branch); update `test_ensure_models_present_empty_dir_downloads` log assertion to the new estimate wording (drop "downloading essentia weights"/"150MB" expectations as needed). Keep `test_ensure_models_present_download_failure` (RuntimeError + __cause__) green. In the SAME file, fix the two download_models-internal tests that the Task 1 refactor breaks: remove or repurpose `test_download_one_is_idempotent_when_dest_exists` (since `_download_one` no longer skips on existence), and change `test_download_to_creates_pb_and_json_pairs` to monkeypatch `_ensure_present` (not `_download_one`, which `download_to` no longer calls directly). Leave `test_download_models_classifier_count_matches_bash` untouched.
  </action>
  <verify>
    <automated>uv run pytest tests/test_services/test_model_bootstrap.py tests/test_task_split.py -x && uv run ruff check src/phaze/tasks/_shared/model_bootstrap.py tests/test_services/test_model_bootstrap.py && uv run mypy src/phaze/tasks/_shared/model_bootstrap.py</automated>
  </verify>
  <done>`ensure_models_present` has no glob/count gate and always calls `download_to`; the stale 150MB/2-5min estimate is replaced; RuntimeError-wrap + __cause__ test stays green; IMPORT-BOUNDARY test (test_task_split.py) passes; the two download_models-internal tests in this file are fixed for the Task 1 refactor; ruff + mypy clean.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| worker → essentia.upf.edu | The worker fetches multi-GB weight files over the network at startup; the remote server and the transport are untrusted (stalled TLS, truncated transfers, transient 5xx). |
| /models volume → analysis runtime | Files on disk are consumed by essentia at analysis time; a corrupt/truncated `.pb` produces a silently broken model. |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-jbg-01 | Denial of Service | every HTTP request in `download_models.py` (HEAD + GET) | mitigate | All requests issued via `_with_retries` with explicit `_TIMEOUT`; no un-timeouted socket can wedge the worker. Test (a) asserts the HEAD route call_count is bounded by `_MAX_ATTEMPTS`. |
| T-jbg-02 | Tampering | on-disk `.pb`/`.json` files vs server Content-Length | mitigate | `_ensure_present` validates on-disk byte size against HEAD Content-Length; size mismatch → stale file removed + re-downloaded. Test (b) proves a truncated file is replaced. |
| T-jbg-03 | Tampering | same-size bit-flip corruption | accept | Out of scope per LOCKED CONTEXT.md (no SHA-256 — would re-hash ~3.1 GB every container start). Size check catches the observed truncation class only. |
| T-jbg-04 | Spoofing | server with no/forged Content-Length on HEAD | accept | Unobtainable Content-Length degrades to keep-present-file (WARNING) / GET-missing-file per LOCKED decision; no integrity signal exists to validate against (no published checksums for essentia weights). |

No package-manager installs in this plan → no supply-chain (T-*-SC) checkpoint required.
</threat_model>

<verification>
Full-suite + quality gates after both tasks (matches CLAUDE.md: uv-only, ruff line-length 150, mypy strict, 85% coverage, pre-commit must pass):

```
uv run pytest --cov --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy .
pre-commit run --all-files
```

Behavioral checks (all via respx + monkeypatched sleep — zero real network/sleep):
- (a) HEAD timeout on every attempt → bounded retry, HEAD call_count == `_MAX_ATTEMPTS`, never unbounded.
- (b) present-but-truncated file (on-disk size != HEAD Content-Length) → removed + re-fetched; final size == full.
- (c) transient GET errors on first N attempts → eventual success, atomic (no `.part`, dest appears only on full success).
- (d) complete + size-valid set → `download_to` issues only HEADs (zero GET), returns; `ensure_models_present` succeeds with no operator action.
</verification>

<success_criteria>
- HEAD and GET share one bounded-retry/timeout implementation (`_with_retries`); no request lacks `_TIMEOUT`.
- `_download_one`'s blind `if dest.exists(): return` is gone; skip is size-based in `_ensure_present`.
- `ensure_models_present` has no count short-circuit; always validates via `download_to`; RuntimeError-wrap + `__cause__` preserved; IMPORT-BOUNDARY intact.
- Stale `~150MB/2-5min` estimate replaced with ~3.1 GB / 34 files / multi-GB reality.
- CLASSIFIER_MODELS/GENRE_MODELS ordering, atomic `os.replace`, 4xx fail-fast / 5xx retry, `# noqa: PTH105` and `# noqa: S311 # nosec B311` suppressions all preserved.
- All four required test cases pass; adjusted PR #91 / count-gate tests pass; ≥85% coverage; ruff + mypy + pre-commit clean.
- No new PR — changes land on branch `fix/model-bootstrap-transient-retry`.
</success_criteria>

<output>
Create `.planning/quick/260608-jbg-validate-model-integrity-on-bootstrap-vi/260608-jbg-SUMMARY.md` when done.
</output>
