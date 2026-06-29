---
phase: 52-job-runner-image-one-shot-entrypoint
plan: 02
subsystem: job-runner / one-shot-entrypoint
tags: [one-shot, exit-codes, kueue, tdd, KJOB-02, KJOB-03, KJOB-04, KJOB-05]
requires:
  - phaze.services.analysis_wire._features_to_mood_dict / _features_to_style_dict (Plan 52-01)
  - phaze.services.agent_client.PhazeAgentClient.request_download_url (Plan 52-01)
  - phaze.tasks._shared.agent_bootstrap.construct_agent_client (Phase 27/29)
  - phaze.services.analysis.analyze_file (windowed; Phase 31/43)
  - phaze.services.hashing.compute_sha256
  - phaze.logging_config.configure_logging
provides:
  - phaze.job_runner.run / main (one-shot orchestrator)
  - phaze.job_runner exit-code contract (EXIT_OK/EXIT_DOWNLOAD/EXIT_INTEGRITY/EXIT_ANALYSIS/EXIT_CALLBACK)
affects:
  - Phase 52 Plan 03 (Dockerfile.job CMD `uv run python -m phaze.job_runner`)
  - Phase 53 (server side of presign-download)
  - Phase 54 (models PVC provisioning — PHAZE_MODELS_DIR)
tech-stack:
  added: []
  patterns:
    - "one-shot pod: run flow then sys.exit(<distinct code>) instead of report-failure-then-return (the v5.0 process_file divergence, D-01)"
    - "fail-fast presign/download/integrity/analysis; only the callback PUT inherits the shared _request bounded retry (D-02)"
    - "deferred essentia import seam (_load_analyze_file) keeps module load Postgres-free AND essentia-free"
key-files:
  created:
    - src/phaze/job_runner.py
    - tests/test_job_runner.py
  modified:
    - tests/conftest.py
    - tests/test_task_split.py
decisions:
  - "Patched the analyze seam at phaze.job_runner._load_analyze_file (not phaze.services.analysis.analyze_file) so unit tests run without importing the platform-gated essentia wheel or its GB models; the no_monoloader source guard independently proves the windowed path is wired."
  - "Config-guard branches (cfg not AgentSettings / missing / invalid PHAZE_JOB_FILE_ID) map to EXIT_DOWNLOAD (10) rather than a new undocumented code, keeping the exit surface to the five documented constants."
  - "Embedded a static, valid self-signed test CA PEM in conftest so httpx(verify=<path>) builds its SSLContext under respx (respx intercepts below TLS, so the cert is never used in a handshake — it only has to parse)."
metrics:
  duration: ~25m
  completed: 2026-06-27
  tasks: 2
  files: 4
requirements: [KJOB-02, KJOB-03, KJOB-04, KJOB-05]
---

# Phase 52 Plan 02: One-Shot Job Runner & Exit-Code Contract Summary

Built `src/phaze/job_runner.py` — the fire-once burst orchestrator that runs presign → download → sha256-verify → windowed analyze → callback PUT for ONE file and translates each step's outcome into a distinct process exit code (0/10/11/12/13), the structural divergence from v5.0 `process_file` so Kueue/Workload reads failure from pod status.

## What Was Built

### Task 1 — Failing test suite + import-boundary case (RED)
- `tests/test_job_runner.py`: respx fake control plane + object store driving `run()`. Tests: `happy_path` (exit 0), parametrized `exit_code` matrix (presign/download → 10, sha mismatch → 11, analyze raises → 12, PUT fails → 13), `ca_verify` (client built with `verify=<baked CA>`, never `verify=False`), and a `no_monoloader` source guard.
- `tests/test_task_split.py`: cloned the subprocess import-boundary case as `test_job_runner_does_not_import_phaze_database` (bans `phaze.database` / `phaze.tasks.session` / `sqlalchemy.ext.asyncio`).
- `tests/conftest.py`: `job_env` fixture (agent env + baked-CA PEM + models dir; clears the `get_settings` lru_cache). Tests import `phaze.job_runner` lazily inside each body so collection succeeds while the module is unwritten — RED surfaced as `ModuleNotFoundError`.

### Task 2 — `job_runner.py` orchestrator + exit-code contract (GREEN)
- `run()` (async) + `main()` (configures logging first, then `asyncio.run(run())`). Exit constants `EXIT_OK=0`, `EXIT_DOWNLOAD=10`, `EXIT_INTEGRITY=11`, `EXIT_ANALYSIS=12`, `EXIT_CALLBACK=13`.
- Flow: presign via `client.request_download_url` (fail-fast → 10); stream the presigned GET to a temp file with a **fresh bearer-less** httpx client (T-52-04); `compute_sha256` off the event loop, mismatch → 11 + unlink; `analyze_file(tmp, models_dir, fine_cap, coarse_cap)` **directly** (deferred import, no pebble pool, no retry — fail-fast → 12); build `AnalysisWritePayload` via the `analysis_wire` converters + per-window list and `put_analysis` (final failure → 13). Success → `sys.exit(0)`.
- Client built via `construct_agent_client(cfg)` (`verify=cfg.agent_ca_file`); TLS verification never bypassed. Temp file unlinked + client pool released in `finally` (V12). `models_dir` from `PHAZE_MODELS_DIR` (default `cfg.models_path` `/models`, D-05). Structured JSON event per step carrying `file_id` + `step` + `elapsed_ms` (D-03); bearer never logged.

## TDD Gate Compliance
- RED gate: `d0e3e1f` — `test(52-02)` adds the failing suite + boundary case (`ModuleNotFoundError: phaze.job_runner`).
- GREEN gate: `45f9566` — `feat(52-02)` implements the module; the full `-k` selection passes.
- No REFACTOR commit — the module was clean on first green (only an acceptance-grep comment reword, folded into the GREEN commit).

## Verification
- `uv run pytest tests/test_job_runner.py tests/test_task_split.py -k "job_runner or happy_path or exit_code or ca_verify or no_monoloader"` — 9 passed.
- `uv run pytest tests/test_task_split.py -k job_runner` — import boundary GREEN.
- New-module coverage: `src/phaze/job_runner.py` 90.91% (≥85% gate). Uncovered: the real deferred-essentia import body and defensive config-guard branches.
- Broader sanity: `test_task_split.py` + `test_config_role_split.py` + `test_services/test_agent_client.py` + `test_job_runner.py` — 52 passed.
- `uv run ruff check` + `ruff format --check` + `uv run mypy src/phaze/job_runner.py` — clean.

## Acceptance Criteria
- `grep -c "def test_" tests/test_job_runner.py` = 4 (happy_path, exit_code matrix, ca_verify, no_monoloader).
- `grep -Ec "EXIT_DOWNLOAD|EXIT_INTEGRITY|EXIT_ANALYSIS|EXIT_CALLBACK"` = 12; `grep -c "sys.exit(0)"` = 1.
- `grep -c "MonoLoader"` = 0; `grep -c "analyze_file"` = 10; `grep -Ec "verify\s*=\s*False"` = 0.
- Boundary subprocess test GREEN (no `phaze.database` / `sqlalchemy.ext.asyncio` in `sys.modules`).

## Threat Model Disposition
- T-52-01 (TLS MITM on callback) — mitigated: client via `construct_agent_client` (`verify=cfg.agent_ca_file`); `verify=False` absent; asserted by `ca_verify` + source guard.
- T-52-02 (pod reaches Postgres) — mitigated: module banner + subprocess import-boundary test ban the ORM/async-DB modules.
- T-52-03 (corrupt download analyzed) — mitigated: `compute_sha256 == expected_sha256` before analyze; mismatch → exit 11 + unlink.
- T-52-04 (bearer leak) — mitigated: token header-only via `PhazeAgentClient`; the presigned download uses a fresh bearer-less client; a happy-path assertion confirms no `Authorization` header reaches the object store.
- T-52-07 (multi-hour OOM) — mitigated: windowed `analyze_file` only (no whole-file MonoLoader — source guard); temp streamed to tempdir + unlinked in `finally`.
- T-52-08 (SIGTERM→false-success) — accepted: no SIGTERM→0 trap; default Python SIGTERM→143 stays honestly non-zero.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] respx factory-router mismatch in the exit-code matrix tests**
- **Found during:** Task 2 GREEN run.
- **Issue:** `@respx.mock(assert_all_called=False)` (the factory form) binds a *separate* router, but the test bodies register routes via the global `respx.post/get/put` — so requests hit the active router with no matching routes (`AllMockedAssertionError`), making every matrix case fail at presign (exit 10).
- **Fix:** Switched the matrix + `ca_verify` tests to plain `@respx.mock` (global router). Each scenario already registers exactly the routes its flow exercises, so the default `assert_all_called` holds.
- **Files modified:** tests/test_job_runner.py
- **Commit:** 45f9566

**2. [Rule 3 - Blocking] Test CA PEM must parse for `httpx(verify=<path>)`**
- **Found during:** Task 2 GREEN run.
- **Issue:** `construct_agent_client` passes the CA path to `httpx.AsyncClient(verify=...)`, which eagerly builds an SSLContext from the file; the placeholder fake-PEM content raised `ssl.SSLError` at client construction (before respx could intercept).
- **Fix:** Embedded a static, valid self-signed test CA PEM in the `job_env` fixture. respx intercepts below TLS, so the cert is never used in a handshake — it only has to load.
- **Files modified:** tests/conftest.py
- **Commit:** 45f9566

**3. [Rule 3 - Blocking] Lint fixes on the RED test files (S108 / ARG005)**
- **Found during:** Task 1 commit (pre-commit ruff).
- **Issue:** A literal `/tmp` in the `job_env` fixture tripped `S108`; placeholder lambda args tripped `ARG005` (neither is in the `tests/**` ignore list).
- **Fix:** Used `str(tmp_path)` for scan roots and `*_a, **_k` for the throwaway lambda params.
- **Files modified:** tests/conftest.py, tests/test_job_runner.py
- **Commit:** d0e3e1f

### Notes
- The pre-existing `DeprecationWarning: verify=<str> is deprecated` originates in `construct_agent_client` (Phase 29), not this plan's code — out of scope (logged here, not fixed).

## Known Stubs
None — the orchestrator is fully wired to the merged Plan 52-01 contracts and the real windowed `analyze_file`. The presign *server* endpoint is intentionally Phase 53; the client side it consumes is real and merged.

## Commits
- `d0e3e1f` test(52-02): add failing job_runner suite + import-boundary case (RED)
- `45f9566` feat(52-02): implement one-shot job_runner orchestrator + exit-code contract (GREEN)

## Self-Check: PASSED
- FOUND: src/phaze/job_runner.py
- FOUND: tests/test_job_runner.py
- FOUND: commit d0e3e1f, 45f9566
