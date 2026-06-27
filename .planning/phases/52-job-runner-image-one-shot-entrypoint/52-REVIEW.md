---
phase: 52-job-runner-image-one-shot-entrypoint
reviewed: 2026-06-27T00:00:00Z
depth: standard
files_reviewed: 12
files_reviewed_list:
  - .github/workflows/docker-publish.yml
  - Dockerfile.job
  - src/phaze/job_runner.py
  - src/phaze/schemas/agent_analysis.py
  - src/phaze/services/agent_client.py
  - src/phaze/services/analysis_wire.py
  - src/phaze/tasks/functions.py
  - tests/conftest.py
  - tests/test_deployment/test_job_image.py
  - tests/test_job_runner.py
  - tests/test_services/test_agent_client.py
  - tests/test_task_split.py
findings:
  critical: 0
  warning: 5
  warning_resolved: 5
  warning_open: 0
  info: 2
  info_resolved: 2
  info_open: 0
  total: 7
status: resolved
resolved:
  - WR-01
  - WR-02
  - WR-03
  - WR-04
  - WR-05
  - IN-01
  - IN-02
resolved_at: 2026-06-27T00:00:00Z
notes: >-
  All 5 warnings fixed in commits 616ed42 (WR-01), 3050f54 (WR-02),
  ea3d5ed (WR-04), 6af1704 (WR-03), ebf2b3c (WR-05). IN-01 fixed in
  commit 8c66585 (docstrings reworded to match converter behavior, no
  runtime change). IN-02 fixed in commit f904e01 (expected_sha256 pinned
  to ^[0-9a-f]{64}$ + .strip().lower() integrity compare, schema rejection
  test added).
---

# Phase 52: Code Review Report

**Reviewed:** 2026-06-27
**Depth:** standard
**Files Reviewed:** 12
**Status:** issues_found

## Summary

Reviewed the Phase 52 one-shot Kubernetes Job-runner: `job_runner.py` (presign →
download → sha256-verify → analyze → callback PUT → distinct exit codes), the
Postgres-free `analysis_wire` converters, the `request_download_url` presign client
method + `PresignDownloadResponse` schema, and `Dockerfile.job` + the CI publish job.

The core flow is sound. Confirmed: the bearer token is never attached to the
self-authenticating download (T-52-04 holds — verified by code and test); TLS verify
is always threaded from the baked CA and `verify=False` appears nowhere (KJOB-05);
the import boundary is enforced (no `phaze.database`/`sqlalchemy.ext.asyncio`); the
mood/style converters were relocated to `analysis_wire` verbatim with no behavioral
drift (diffed against the pre-phase `functions.py`); the temp file is cleaned on every
exit path; `sys.exit` (SystemExit/BaseException) is not swallowed by the `except
Exception` step handlers; and `CancelledError`/`KeyboardInterrupt` are not caught.

No BLOCKER-class defects (no data loss, security hole, or happy-path crash). The
findings are correctness-of-diagnostics and deployment-robustness issues: exit-code
misclassification of analysis-output errors, exit-code overloading for config
failures, a CI CA-secret guard gap, an inaccurate exit-code contract comment, and a
mutable base tag on default-branch builds.

## Warnings

### WR-01: `_build_payload(result)` runs inside the callback try-block — analysis-output errors are mis-coded as EXIT_CALLBACK (13)

**Status:** RESOLVED (commit 616ed42) — payload build hoisted into the analyze step with an `isinstance(result, dict)` guard; `_build_payload` now iterates `(result.get("windows") or [])`; matrix test extended with non-dict-result and bad-window-key scenarios asserting exit 12.

**File:** `src/phaze/job_runner.py:215-220` (and `107-133`)
**Issue:** Payload construction is evaluated as the argument *inside* the callback
`try`:
```python
try:
    await client.put_analysis(file_id, _build_payload(result))
except Exception:
    log.exception("job_runner_callback_failed", ...)
    sys.exit(EXIT_CALLBACK)
```
`_build_payload` builds `AnalysisWindowPayload(**w)` (which has `extra="forbid"`) and
reads `result.get("windows", [])` with no `isinstance` guard — unlike the SAQ
`process_file` path, which guards `if isinstance(analysis, dict) else []` and
`isinstance(features, dict)` (functions.py:186-193). If `analyze_file` returns a
malformed result (non-dict, `windows` present-but-`None`, or a window dict carrying an
unexpected key), the exception is raised during payload build, caught here, logged as
`job_runner_callback_failed`, and the pod exits **13 (callback failure)** when the true
cause is bad analysis output → should be **12 (EXIT_ANALYSIS)**. Kueue reads the wrong
failure class straight from pod status (defeating the KJOB-04 distinct-exit-code
contract). `result.get("windows", [])` returning `None` also raises `TypeError` on
`for w in None`.
**Fix:** Build the payload in the analyze step (or its own guarded step) so build
errors map to `EXIT_ANALYSIS`, and mirror the `process_file` dict-guards:
```python
# inside the analyze try, after result is produced:
if not isinstance(result, dict):
    log.error("job_runner_bad_result", file_id=fid, step="analyze")
    sys.exit(EXIT_ANALYSIS)
payload = _build_payload(result)   # raises here -> EXIT_ANALYSIS, not EXIT_CALLBACK
...
# callback step:
try:
    await client.put_analysis(file_id, payload)
except Exception:
    ...
    sys.exit(EXIT_CALLBACK)
```
And in `_build_payload`, guard the iterable: `for w in (result.get("windows") or [])`.

### WR-02: EXIT_DOWNLOAD (10) is overloaded for configuration/precondition failures

**Status:** RESOLVED (commit 3050f54) — added distinct `EXIT_CONFIG = 20`; wrong-role, missing `PHAZE_JOB_FILE_ID`, and invalid-UUID now exit 20; contract docstring updated; parametrized precondition test added.

**File:** `src/phaze/job_runner.py:143-155`
**Issue:** Wrong-role, missing `PHAZE_JOB_FILE_ID`, and an invalid-UUID `file_id` all
`sys.exit(EXIT_DOWNLOAD)`. The documented contract (job_runner.py:13-18) defines `10`
as "presign request OR download failure (fail-fast, no retry — D-02)". A permanent
misconfiguration is therefore reported to Kueue/Workload as a transient download
failure, which a controller may treat as retry-worthy and re-drive a Job that can never
succeed (env/role never changes between attempts). It also makes the exit-code class
ambiguous for operators reading pod status.
**Fix:** Reserve `10` for the presign/download steps and route precondition failures to
a distinct code (e.g. a new `EXIT_CONFIG`/`EXIT_PRECONDITION`), or let them surface as
a bare non-zero (uncaught) so they are visibly not a download failure. At minimum,
document in the exit-code contract that `10` doubles as the startup/precondition code.

### WR-03: CI CA materialization writes a 1-byte file for an empty secret, passing the `st_size == 0` guard

**Status:** RESOLVED (commit 6af1704) — CA-materialization step now errors out (`exit 1`) when `PHAZE_INTERNAL_CA_CERT` is empty/unset and validates the PEM parses via `openssl x509 -noout` before the build.

**File:** `.github/workflows/docker-publish.yml:625-635`
**Issue:** The step runs `printf '%s\n' "${CA_CERT}" > phaze-ca.crt`. If
`secrets.PHAZE_INTERNAL_CA_CERT` is unset or empty, this writes a single newline (1
byte), not an empty file. `Dockerfile.job` then `COPY`s it successfully and the image
publishes green. The step comment claims "construct_agent_client raises at runtime if
the baked cert is empty, so a misconfigured secret surfaces fast" — but
`construct_agent_client` (agent_bootstrap.py:62) only rejects `st_size == 0`. A 1-byte
newline passes the guard, so the misconfiguration does **not** surface fast; it ships a
junk CA that fails only at the first TLS handshake inside a live pod (an unparseable
PEM may instead fail at httpx SSLContext build — still a deploy-time-only failure, not
the claimed build-time gate).
**Fix:** Fail the CI step when the secret is empty, e.g.:
```bash
if [ -z "${CA_CERT}" ]; then echo "::error::PHAZE_INTERNAL_CA_CERT is empty"; exit 1; fi
printf '%s\n' "${CA_CERT}" > phaze-ca.crt
```
Optionally validate it parses (`openssl x509 -in phaze-ca.crt -noout`) before the build.

### WR-04: Exit-code contract documents `12 = ... inner timeout`, but no timeout is implemented

**Status:** RESOLVED (contract docstring corrected alongside commit 3050f54; analyze-step clarification in commit ea3d5ed) — chose the safe documentation fix (no behavior change): removed "inner timeout" language and documented that wall-clock bounding is delegated to the Kueue/Job deadline (SIGTERM → 143). No asyncio timeout wrapper added (avoided scope creep).

**File:** `src/phaze/job_runner.py:13-18` and `196-204`
**Issue:** The module docstring maps exit `12` to "windowed analysis raised / OOM /
**inner timeout** (fail-fast — D-02)". The implementation calls `analyze_file(...)`
**synchronously** with no pebble pool, no `asyncio.wait_for`, and no timeout argument —
only `except Exception` maps to `EXIT_ANALYSIS`. A genuinely hung analysis therefore
never produces exit 12; it blocks the event loop until an external Kubernetes mechanism
(`activeDeadlineSeconds`) sends SIGTERM, which Python turns into exit 143 — a different,
undocumented code. The "inner timeout" language is inherited from the SAQ
`process_file` path (which *does* use a killable pebble timeout) and is inaccurate here.
**Fix:** Either implement a bound (e.g. wrap the sync call in
`asyncio.wait_for(asyncio.to_thread(analyze_file, ...), timeout=cfg.analysis_inner_timeout_sec)`
and map `TimeoutError` → `EXIT_ANALYSIS`), or remove "inner timeout" from the exit-code
contract and document that wall-clock bounding is delegated to the Kueue/Job deadline
(SIGTERM → 143). Note: running `analyze_file` directly on the loop is acceptable for a
single-file one-shot, but `to_thread` would also restore SIGTERM responsiveness.

### WR-05: `BASE_IMAGE` resolves to the mutable `:latest` tag on default-branch builds

**Status:** RESOLVED (commit ebf2b3c) — added a shell resolution step that deterministically prefers the first non-`:latest` (semver/ref) tag and falls back to the freshly-pushed `:latest` only on default-branch builds where no version tag exists (keeps those builds working). Mirrors the parity-golden-x86 pattern.

**File:** `.github/workflows/docker-publish.yml:651-652`
**Issue:** `BASE_IMAGE=${{ fromJSON(steps.base-meta.outputs.json).tags[0] }}` takes the
first resolved tag. The tag config lists `type=raw,value=latest,enable={{is_default_branch}}`
first, so on a default-branch (main) push `tags[0]` is `:latest` — a mutable, moving
tag. This mildly contradicts the in-file T-52-06 rationale ("the exact release tag,
never a stale shared moving tag"). On a release/tag push `tags[0]` is the immutable
semver version (correct). Within a single workflow run `:latest` was just freshly
pushed by `build-and-push`, so the practical risk is low and this matches the existing
`parity-golden-x86` `head -n1` pattern — but the "never a moving tag" guarantee only
truly holds for release builds.
**Fix:** Prefer the immutable tag deterministically, e.g. select the semver/`ref` tag
from `steps.base-meta.outputs.json` rather than index 0, or gate the Job-image publish
to tag events only so `BASE_IMAGE` is always a version tag. If the current behavior is
intended, soften the comment to state that on default-branch builds the base is the
freshly-pushed `:latest`.

## Info

### IN-01: `functions.py` docstring claims "top-10 genre predictions" but the converter slices nothing

**Status:** RESOLVED (commit 8c66585) — reworded the module docstrings in both `tasks/functions.py` and `services/analysis_wire.py` to "the genre predictions returned by the discogs effnet model"; chose the docstring fix over adding a `[:10]` slice so `job_runner` wire output does not diverge from the verbatim-relocated `process_file` converters. No runtime change.

**File:** `src/phaze/tasks/functions.py:14-16` (also `analysis_wire.py:17-18`)
**Issue:** The module docstring says `style` "takes the top-10 genre predictions", but
`_features_to_style_dict` iterates **all** `features["genre"]["predictions"]` entries
with no `[:10]` slice (confirmed grep — no slice in either module). If the upstream
`analyze_file` already caps predictions to 10 the wire result is bounded, but the
docstring overstates a guarantee this code does not enforce. Pre-existing (relocated
verbatim), surfaced because both files are in scope this phase.
**Fix:** Either slice the top-N in the converter, or reword the docstrings to "the
genre predictions returned by the model" to match behavior.

### IN-02: `PresignDownloadResponse.expected_sha256` has no format validation and the integrity compare is case/whitespace sensitive

**Status:** RESOLVED (commit f904e01) — constrained the field to `Field(pattern=r"^[0-9a-f]{64}$")` (kept required, `extra="forbid"`) so format skew fails at the wire boundary, and normalized both sides with `.strip().lower()` before the integrity compare in `job_runner.py` (defensive; `compute_sha256` already returns lowercase). Added a parametrized schema test rejecting malformed digests (wrong length / uppercase / non-hex / empty / leading whitespace) plus a valid-digest case. Existing fixtures already used 64-char lowercase hex, so no stubs changed; exit-11 mismatch and exit-0 happy-path tests still pass.

**File:** `src/phaze/schemas/agent_analysis.py:123-124`, `src/phaze/job_runner.py:191`
**Issue:** `expected_sha256: str` accepts any string (including `""`), and the check is
`actual_sha256 != expected_sha256`. `compute_sha256` returns a lowercase hex digest
(hashing.py:25), and `FileRecord.sha256_hash` is stored lowercase, so today they match —
but the schema does not enforce a 64-char lowercase-hex shape, so a server-side change to
uppercase/prefixed digests would silently fail every download as an integrity mismatch
(exit 11) with no diagnostic distinguishing "corrupt bytes" from "format skew".
**Fix:** Constrain the field (`Field(pattern=r"^[0-9a-f]{64}$")` or `min_length=64,
max_length=64`) and/or normalize both sides with `.strip().lower()` before comparison.

---

_Reviewed: 2026-06-27_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
