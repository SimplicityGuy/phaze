# Quick Task 260608-jbg: Validate model integrity on bootstrap (HEAD size check + re-download) - Context

**Gathered:** 2026-06-08
**Status:** Ready for planning

<domain>
## Task Boundary

Follow-up to quick task 260608-i21 (PR #91, branch `fix/model-bootstrap-transient-retry`).
Extends the same branch/PR — do NOT open a new PR.

A live first-deploy on a fresh `/models` volume (~3.1 GB across 34 `.pb` files, incl. three
~288 MB vggish-audioset weights) surfaced two failure modes worse than the original crash-loop:

1. **Silent indefinite hang (highest priority).** A request with no connect/read timeout hit a
   stalled TLS connection; the worker sat in `download_to` for 16+ min, Status=running, no crash,
   no progress — so `restart: unless-stopped` never fired and there was NO recovery.
   (NOTE: PR #91 already added explicit `_TIMEOUT` + bounded retry to the GET in `_download_one`.
   This task must ensure EVERY request — including the new HEAD validation requests — also has the
   timeout + bounded-retry treatment, so no socket can ever wedge the worker forever.)
2. **Completeness check is count-only — no integrity validation.** `ensure_models_present` treats
   the dir as complete on `len(glob("*.pb")) >= _EXPECTED_MODEL_COUNT`. A correctly-named but
   truncated/corrupt `.pb` counts as "present" and ships a broken model. Also, `_download_one`'s
   blind `if dest.exists(): return` blesses any pre-existing file without checking its integrity.

Already shipped in PR #91 (do NOT redo): per-file bounded retry + backoff/jitter on the GET,
explicit `httpx.Timeout`, atomic `os.replace` from `<dest>.part`, in-stream Content-Length
truncation check, fail-fast 4xx / retry 5xx, per-file error after exhausting attempts.
</domain>

<decisions>
## Implementation Decisions (LOCKED — do not revisit)

### Integrity validation source: HEAD on startup
- There are NO published checksums/sizes for the essentia weights (verified: no manifest in
  `scripts/download-models.sh`). The authoritative size signal is the server's `Content-Length`.
- For each expected file, issue a `HEAD` request to its URL and read `Content-Length` as the
  expected size. The HEAD request MUST use the same explicit timeout + bounded-retry/backoff
  treatment as the GET (reuse the retry machinery — no un-timeouted request anywhere).
- Validation rule per file:
  - If on-disk file is missing → download.
  - If present and on-disk byte size == expected `Content-Length` → keep (valid), no GET.
  - If present and size mismatches → corrupt/truncated → remove the stale file and re-download.
  - If `Content-Length` is absent/unobtainable for a present file → keep it (cannot validate),
    log a WARNING; for a missing file, download with whatever the GET reports.
- Accepted trade-off: ~34 HEAD round-trips on EVERY worker startup (even when the set is complete).
  This is the explicit cost of always-validate; offline validation is NOT a requirement.
- No sidecar manifest file is written. No legacy one-time bulk re-download.

### Validation depth: size only
- Validate present files by comparing on-disk byte size to the expected `Content-Length`.
  Cheap `stat`, catches the observed truncation bug, fast startup.
- Do NOT compute or store SHA-256 (would re-hash ~3.1 GB every container start). Same-size
  bit-flips are out of scope.

### Recovery model
- Progress + recovery must happen WITHIN the process via timeouts + bounded retry + integrity
  re-download — NOT by relying on the Docker container-restart policy.
- The count-only short-circuit in `ensure_models_present` must be removed/replaced: completeness =
  "all 34 canonical files present AND size-valid", decided by the per-file validation in
  `download_to`, not by a glob count (which a truncated file can satisfy). A fully-downloaded,
  valid on-disk set must be recognized and return WITHOUT an operator restart.

### Stale user-facing estimate
- The "~150MB, takes 2-5min on first start" message in `model_bootstrap.py` is wrong. Update it to
  reflect reality: ~3.1 GB across 34 files (multi-GB, can take many minutes / longer on a slow link)
  so operators don't mistake a legitimate multi-GB download for a hang.
</decisions>

<specifics>
## Specific Ideas

- Files: `src/phaze/scripts/download_models.py` (HEAD validation + download orchestration),
  `src/phaze/tasks/_shared/model_bootstrap.py` (drop count-only gate; update message),
  `tests/test_scripts/test_download_models.py` (new tests).
- Reuse/refactor the existing retry loop so both HEAD and GET share timeout + backoff + the
  `_TRANSIENT_ERRORS`/`_RetryableDownloadError` handling. No request may be issued without `_TIMEOUT`.
- Preserve: CLASSIFIER_MODELS/GENRE_MODELS byte-for-byte ordering; model_bootstrap IMPORT-BOUNDARY
  (Postgres-free, stdlib + `phaze.scripts.download_models` only); 4xx fail-fast / 5xx retry; atomic
  `os.replace`; per-file named error after exhausting retries.
- Tests required (respx + monkeypatched `time.sleep`, zero real network/sleep):
  (a) a hanging/slow response → the request times out and RETRIES rather than blocking forever
      (simulate via `side_effect` raising `httpx.ReadTimeout`/`ConnectTimeout`, assert retry then
      success or bounded failure — never an unbounded block);
  (b) a present-but-truncated file (on-disk size != HEAD Content-Length) → it is removed and
      re-fetched, and the final file has the correct full size;
  (c) transient errors on the first N attempts → eventual success with atomic writes (no `.part`
      left, file only appears after full success);
  plus keep the existing PR #91 tests green (adjusting any that asserted the old blind
  `dest.exists()` skip, since the skip now depends on size validation).
</specifics>

<canonical_refs>
## Canonical References

- PR #91 / branch `fix/model-bootstrap-transient-retry` — the in-progress resilience fix this extends.
- Prior quick task: `.planning/quick/260608-i21-harden-agent-model-bootstrap-against-tra/`
- CLAUDE.md — uv-only, ruff line-length 150, mypy strict, 85% coverage, double quotes, type hints,
  pre-commit must pass (never `--no-verify`).
</canonical_refs>
