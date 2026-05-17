---
phase: 29-deployment-hardening-agents-admin
plan: 05
subsystem: deployment
tags: [phase-29, ops-03, d-21, models, bootstrap, agent-worker, v4.0]

# Dependency graph
requires:
  - phase: 27-watcher-service-user-initiated-scan
    provides: phaze.tasks._shared.agent_bootstrap (analog for _shared/ module shape + import-boundary banner)
  - phase: 26-task-code-reorg-http-backed-agent-worker
    provides: phaze.tasks.agent_worker.startup hook + D-25 subprocess import-boundary invariant pattern
provides:
  - "phaze.scripts.download_models — single-source-of-truth URL list (33 classifier + 1 genre model) + download_to() + python -m CLI"
  - "phaze.tasks._shared.model_bootstrap.ensure_models_present — Postgres-free idempotent .pb-glob + auto-download orchestration"
  - "agent_worker.startup wiring: ensure_models_present invoked AFTER /whoami (auth-fail-fast before download)"
  - "agent_watcher WARNING-7 documentation comment: worker-only download avoids .part-file race"
  - "tests/test_task_split.py::test_model_bootstrap_stays_postgres_free — BLOCKER-1 subprocess import-boundary case"
affects: [29-04 docker-compose-agent, 29-08 deployment-doc-and-justfile]

# Tech tracking
tech-stack:
  added: []  # zero new pip dependencies; httpx already in deps
  patterns:
    - "Atomic file-download via .part suffix + tmp.rename(dest) — crash-safe per-file idempotency (T-29-05-03)"
    - "Bash-shim-delegates-to-python-module: single source of truth for URL lists callable from both bash and Python"
    - "Import-boundary subprocess test parallel to agent_bootstrap pattern — covers each new _shared/ module"

key-files:
  created:
    - src/phaze/scripts/__init__.py
    - src/phaze/scripts/download_models.py
    - src/phaze/tasks/_shared/model_bootstrap.py
    - tests/test_services/test_model_bootstrap.py
  modified:
    - scripts/download-models.sh (rewritten 6-line shim; was 102-line bash)
    - src/phaze/tasks/agent_worker.py (drop in-place RuntimeError check; call ensure_models_present after whoami)
    - src/phaze/agent_watcher/__main__.py (add WARNING-7 documentation comment only)
    - tests/test_task_split.py (add test_model_bootstrap_stays_postgres_free)
    - tests/test_phase04_gaps.py (replace 2 OLD fail-fast tests with ordering + propagation tests)

key-decisions:
  - "URL list lives in Python (CLASSIFIER_MODELS tuple), not bash; bash shim execs `uv run python -m phaze.scripts.download_models`"
  - "Atomic .part rename pattern (POSIX-atomic per file) — crash mid-stream leaves only .part which the glob does NOT match"
  - "ensure_models_present is invoked AFTER whoami_with_retry (Step 3a) — auth fails fast in ~63s instead of after 5min download (RESEARCH <specifics> line 906)"
  - "WARNING-7 resolution: ONLY the worker calls ensure_models_present; the watcher documents this choice in a code comment but does NOT call it"
  - "BLOCKER-1: dedicated subprocess case test_model_bootstrap_stays_postgres_free added to tests/test_task_split.py — parallel structure to test_shared_bootstrap_stays_postgres_free"
  - "RuntimeError wrap of underlying download exception preserves __cause__ chain (test_ensure_models_present_download_failure asserts this) — container exits non-zero → restart: unless-stopped retries (T-29-05-02)"

patterns-established:
  - "Pattern: bash-shim-delegates-to-python — `exec uv run python -m phaze.scripts.<name> \"$@\"` makes the bash script a 6-line thin wrapper while keeping the operator-facing `just download-models` recipe operational"
  - "Pattern: import-boundary subprocess test per new _shared/ module — each new Postgres-free module under phaze.tasks._shared/ gets its own dedicated test_*_stays_postgres_free function so a future regression in one module is not masked by another"

requirements-completed: [OPS-03]

# Metrics
duration: ~25min
completed: 2026-05-16
---

# Phase 29 Plan 05: Models Setup — Auto-Download on Empty /models Summary

**Extracts the essentia model URL list (33 classifier + 1 genre = 68 files) from bash into a Python helper, wires a Postgres-free auto-download bootstrap into agent_worker.startup AFTER /whoami, and adds the BLOCKER-1 subprocess import-boundary test.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-05-16T21:35Z (approx)
- **Completed:** 2026-05-16T22:00Z (approx)
- **Tasks:** 2 (both auto, both TDD)
- **Files created:** 4 (scripts/__init__.py, scripts/download_models.py, _shared/model_bootstrap.py, tests/test_model_bootstrap.py)
- **Files modified:** 5 (download-models.sh, agent_worker.py, agent_watcher/__main__.py, test_task_split.py, test_phase04_gaps.py)
- **Tests added/modified:** 6 new + 2 replacing 2 old = 8 net new behaviour-locking tests

## Accomplishments

- **OPS-03 fully closed.** A fresh file-server host's first `just up-agent` now succeeds without manual pre-warming — the worker container auto-downloads ~150MB of essentia weights to its local `/models` volume on first start. `just download-models` still works for operators who prefer to pre-warm.
- **D-21 implemented with WARNING-7 race-avoidance.** Only the worker (`phaze.tasks.agent_worker.startup`) calls `ensure_models_present`; the watcher documents the intentional non-call so two parallel containers on a fresh /models volume cannot race on `.pb.part` files.
- **BLOCKER-1 resolved.** `tests/test_task_split.py::test_model_bootstrap_stays_postgres_free` is now a hard gate covering `phaze.tasks._shared.model_bootstrap`; a future regression that imports `phaze.database` or `sqlalchemy.ext.asyncio` into the model_bootstrap chain will trip CI even if the existing `test_shared_bootstrap_stays_postgres_free` (which covers agent_bootstrap.py only) stays green.
- **Bash → Python URL-list migration.** The 33 classifier paths + 1 genre model that previously lived in `scripts/download-models.sh` lines 16-55 now live in `src/phaze/scripts/download_models.py::CLASSIFIER_MODELS` + `GENRE_MODELS`. The bash script is a 6-line `exec uv run python -m phaze.scripts.download_models "$@"` shim. Single source of truth.
- **Auth-fail-fast ordering preserved.** `ensure_models_present` is invoked as Step 3a — AFTER `whoami_with_retry` succeeds — so a bad token / unreachable app server fails in ~60s instead of after a 5-minute 150MB download (RESEARCH `<specifics>` line 906).

## Task Commits

Each task was committed atomically:

1. **Task 1: phaze.scripts package with download_models.py + bash shim + test scaffold** — `6800931` (feat)
2. **Task 2: model_bootstrap shared module + agent_worker.startup rewire + watcher WARNING-7 comment + BLOCKER-1 subprocess test** — `4ccd283` (feat)

## Files Created/Modified

### Created

- `src/phaze/scripts/__init__.py` — Package marker for `python -m phaze.scripts.<name>` invocations.
- `src/phaze/scripts/download_models.py` — `download_to(target_dir)` public entry; `_download_one(url, dest)` with `.part`-atomic rename; `CLASSIFIER_MODELS` tuple (33 items) + `GENRE_MODELS` tuple (1 item); CLI entry at module bottom.
- `src/phaze/tasks/_shared/model_bootstrap.py` — `ensure_models_present(models_dir)` Postgres-free function with the IMPORT-BOUNDARY INVARIANT banner naming the new subprocess test. Imports stdlib + `phaze.scripts.download_models` only.
- `tests/test_services/test_model_bootstrap.py` — Six tests: three LOCKED `ensure_models_present` cases (empty→download, populated→no-op, network-fail→RuntimeError with `__cause__` chain) + three `download_to` / `_download_one` cases (count assertion, idempotency, .pb+.json pair generation).

### Modified

- `scripts/download-models.sh` — Replaced the 102-line bash script (custom `download_file` function, manual counter, two for-loops) with a 7-line shim: shebang + 4 comment lines + `set -euo pipefail` + `exec uv run python -m phaze.scripts.download_models "${1:-./models}"`. `exec` passes signals + exit code through cleanly.
- `src/phaze/tasks/agent_worker.py` — Added `from phaze.tasks._shared.model_bootstrap import ensure_models_present`. Deleted the in-place RuntimeError checks (old lines 88-97). Inserted `ensure_models_present(Path(cfg.models_path))` as Step 3a (after `_whoami_with_retry`, before the queue-mismatch guard).
- `src/phaze/agent_watcher/__main__.py` — Inserted a documentation-only comment block at the post-`whoami_with_retry` site explaining the WARNING-7 race-avoidance decision (worker owns the download; watcher intentionally does not). No code change.
- `tests/test_task_split.py` — Appended `test_model_bootstrap_stays_postgres_free` subprocess case mirroring the existing `test_shared_bootstrap_stays_postgres_free` structure (same env vars, same banned triple `{phaze.database, phaze.tasks.session, sqlalchemy.ext.asyncio}`, import target changed to `phaze.tasks._shared.model_bootstrap`).
- `tests/test_phase04_gaps.py` — Replaced two OLD fail-fast model-dir RuntimeError tests (`test_agent_startup_raises_if_models_dir_missing`, `test_agent_startup_raises_if_no_pb_files`) with two new tests matching the new auto-download semantics: `test_agent_startup_invokes_ensure_models_present_after_whoami` (ordering invariant) and `test_agent_startup_propagates_ensure_models_present_failure` (propagation invariant). The OLD error-message strings are no longer asserted anywhere in the test tree.

## Decisions Made

- **Idempotency check anchored on `*.pb` glob.** A populated dir with even one `.pb` file short-circuits; a dir with only `.part` files (from a crashed previous run) is treated as empty and retried. This makes recovery from a partial download trivial — re-run produces the same end state.
- **Top-level `Exception` catch in `ensure_models_present`.** The wrap is intentional: any failure from `download_to` (httpx error, OSError, etc.) becomes `RuntimeError("Model download failed: …")` with the original chained as `__cause__`. The test asserts the chain explicitly. This gives the SAQ event loop a single error class to surface and lets the container exit non-zero so `restart: unless-stopped` retries.
- **`httpx.stream` with `timeout=60` + 64KiB chunks.** Matches RESEARCH `<specifics>` line 890. Per-chunk timeout (not total-download) prevents an indefinite hang on a stalled network without making large files un-downloadable.
- **Watcher WARNING-7 resolution = documentation comment + zero code change.** The plan considered an explicit `# do not call ensure_models_present here` note vs. a flock-coordinated dual-call. The comment-only approach is the minimum-viable resolution and matches the actual dependency (the watcher cannot dispatch analysis jobs without a worker anyway), so worker-owns-download is operationally correct.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Ruff `TC003` + `RUF100` warnings on test imports**

- **Found during:** Task 1 verification (ruff check).
- **Issue:** `pathlib.Path` was imported at module-top in `tests/test_services/test_model_bootstrap.py` but used only in type hints; the `# noqa: ARG001` comment on the `boom` helper was flagged as targeting a non-enabled rule (the per-file-ignore in pyproject.toml already exempts tests/ from ARG001).
- **Fix:** Moved `Path` into a `TYPE_CHECKING` block; removed the unused `# noqa: ARG001` comment.
- **Files modified:** tests/test_services/test_model_bootstrap.py
- **Verification:** `uv run ruff check src/phaze/scripts/ tests/test_services/test_model_bootstrap.py` → All checks passed.
- **Committed in:** `6800931` (part of Task 1).

**2. [Rule 3 - Blocking] Ruff `I001` import order in `download_models.py`**

- **Found during:** Task 1 verification (ruff check).
- **Issue:** Initial draft had `import sys` then `from pathlib import Path` (alphabetical by full module name); ruff's isort variant prefers `from … import …` before `import …` when alphabetically ordered.
- **Fix:** `uv run ruff check --fix` auto-reorganized the imports. Confirmed mypy + tests still green.
- **Files modified:** src/phaze/scripts/download_models.py
- **Verification:** `uv run ruff check src/phaze/scripts/` → All checks passed.
- **Committed in:** `6800931` (part of Task 1).

**3. [Rule 1 - Bug] Pre-commit ruff-format auto-reformatted `tests/test_phase04_gaps.py`**

- **Found during:** Task 2 commit (pre-commit hook).
- **Issue:** New test docstrings + assertion messages exceeded 150-char line length; ruff-format wrapped them.
- **Fix:** Re-staged the auto-reformatted file and re-ran `git commit`. Second attempt passed.
- **Files modified:** tests/test_phase04_gaps.py (whitespace-only fixups)
- **Verification:** Pre-commit ran clean on second attempt.
- **Committed in:** `4ccd283` (part of Task 2).

## Deferred Issues

The following pre-existing test failure was discovered during Task 2 verification. It is **out of scope** for Plan 29-05 (does not touch any file this plan modifies, root cause traces to Plan 29-03's app-server compose hardening):

- **`tests/test_phase04_gaps.py::test_docker_compose_has_agent_worker_consuming_agent_queue`** — Asserts the root `docker-compose.yml` contains a service that runs `uv run saq phaze.tasks.agent_worker.settings` with `PHAZE_ROLE=agent`. Plan 29-03 removed the agent-worker block from root compose (app-server-only invariant); Plan 29-04 (parallel wave) creates `docker-compose.agent.yml` where the agent-worker now lives. This test must be updated by Plan 29-04 (or a follow-on plan) to scan both compose files. Logged in `.planning/phases/29-deployment-hardening-agents-admin/deferred-items.md`.

## Verification

### Task 1 acceptance

- ✅ `src/phaze/scripts/__init__.py` exists (28-char docstring)
- ✅ `src/phaze/scripts/download_models.py` exports `download_to`, `_download_one`, `CLASSIFIER_MODELS`, `GENRE_MODELS`
- ✅ `CLASSIFIER_MODELS` is a tuple of exactly 33 strings — `assert len(CLASSIFIER_MODELS) == 33` passes
- ✅ `GENRE_MODELS == ("discogs-effnet-bs64-1",)`
- ✅ `_download_one(url, dest)` is idempotent: existing `dest.exists()` returns without re-downloading (test_download_one_is_idempotent_when_dest_exists asserts httpx.stream is never called)
- ✅ `.part` suffix atomic rename pattern present in `_download_one` body
- ✅ `python -m phaze.scripts.download_models <dir>` works via CLI block at module bottom
- ✅ `scripts/download-models.sh` is exactly 7 lines (shebang + 4 comment lines + `set -euo pipefail` + `exec uv run python -m phaze.scripts.download_models "${1:-./models}"`) — matches the `<action>` block in 29-05-PLAN.md verbatim (shebang + usage + delegation note + 2 functional lines)
- ✅ `uv run mypy src/phaze/scripts/` → Success: no issues found in 2 source files
- ✅ `uv run ruff check src/phaze/scripts/ tests/test_services/test_model_bootstrap.py` → All checks passed

### Task 2 acceptance

- ✅ `src/phaze/tasks/_shared/model_bootstrap.py` exists with IMPORT-BOUNDARY INVARIANT banner naming `tests/test_task_split.py::test_model_bootstrap_stays_postgres_free`
- ✅ Module imports: `logging`, `pathlib` (TYPE_CHECKING), `phaze.scripts.download_models.download_to`. No `phaze.database`, no `sqlalchemy.ext.asyncio`, no `phaze.tasks.session`.
- ✅ `ensure_models_present` body matches RESEARCH lines 838-853 (glob `.pb`, log status, call `download_to` on empty, wrap exception in RuntimeError)
- ✅ `agent_worker.py::startup` no longer contains the in-place `RuntimeError("Models directory not found ...")` / `RuntimeError("No .pb model files ...")` checks
- ✅ `agent_worker.startup` calls `ensure_models_present(Path(cfg.models_path))` exactly once, AFTER `await _whoami_with_retry(client)` and BEFORE the queue-mismatch guard
- ✅ `agent_watcher/__main__.py::main` does NOT call `ensure_models_present`; the documentation comment is present at the post-whoami site
- ✅ `tests/test_task_split.py::test_model_bootstrap_stays_postgres_free` exists, mirrors `test_shared_bootstrap_stays_postgres_free` structure, imports `phaze.tasks._shared.model_bootstrap`, asserts banned-triple absence
- ✅ `uv run mypy src/phaze/tasks/_shared/model_bootstrap.py src/phaze/tasks/agent_worker.py src/phaze/agent_watcher/__main__.py` → Success
- ✅ All 6 tests in `test_model_bootstrap.py` pass
- ✅ All 4 subprocess tests in `test_task_split.py` pass (including new `test_model_bootstrap_stays_postgres_free`)
- ✅ Test sweep: `tests/test_tasks/test_agent_startup_banner.py tests/test_phase04_gaps.py tests/test_agent_watcher/test_main.py tests/test_services/test_model_bootstrap.py tests/test_task_split.py` → 41 passed, 1 deselected (pre-existing Plan 29-03 failure documented above)

### Threat-model mitigations delivered

| Threat ID | Mitigation Delivered |
|-----------|----------------------|
| T-29-05-01 (MITM during model download) | HTTPS-only URLs (essentia.upf.edu); httpx public CA chain verifies cert. Future SHA-256 manifest deferred per plan. |
| T-29-05-02 (network-failure DoS during boot) | RuntimeError wraps `download_to` failures → non-zero exit → restart: unless-stopped retries. test_ensure_models_present_download_failure asserts the wrap + __cause__ chain. |
| T-29-05-03 (half-downloaded .pb satisfies idempotency next time) | `.part` atomic rename pattern in `_download_one`. test_download_one_is_idempotent_when_dest_exists confirms a present `.pb` file short-circuits without touching the network. |
| T-29-05-04 (malicious essentia.upf.edu upload) | Accepted out-of-scope per plan (v4.0 single-user scope). |
| T-29-05-05 (5min boot looks like a hang) | INFO log line "downloading essentia weights (~150MB, takes 2-5min on first start)..." surfaces in `docker compose logs worker`. |
| T-29-05-06 (model_bootstrap drags in Postgres) | New test_model_bootstrap_stays_postgres_free subprocess case is a hard CI gate. |
| T-29-05-07 (worker+watcher race on /models) | WARNING-7 resolution: only worker calls ensure_models_present; watcher documents the non-call. |

## Self-Check: PASSED

**Files created — verified to exist:**

- ✅ `src/phaze/scripts/__init__.py` — FOUND
- ✅ `src/phaze/scripts/download_models.py` — FOUND
- ✅ `src/phaze/tasks/_shared/model_bootstrap.py` — FOUND
- ✅ `tests/test_services/test_model_bootstrap.py` — FOUND

**Files modified — verified `git log --follow` reachable:**

- ✅ `scripts/download-models.sh` — modified in `6800931`
- ✅ `src/phaze/tasks/agent_worker.py` — modified in `4ccd283`
- ✅ `src/phaze/agent_watcher/__main__.py` — modified in `4ccd283`
- ✅ `tests/test_task_split.py` — modified in `4ccd283`
- ✅ `tests/test_phase04_gaps.py` — modified in `4ccd283`

**Commits — verified `git log --all` reachable:**

- ✅ `6800931` — Task 1
- ✅ `4ccd283` — Task 2
