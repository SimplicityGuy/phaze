---
phase: 48-compute-agent-type
plan: 02
subsystem: cli-config
tags: [cli, argparse, config, pydantic-settings, compute-agent, kind, agents-add]
requires:
  - "agents.kind String(16) column + ck_agents_kind_enum CHECK (Plan 01)"
  - "Agent(...) constructor accepting kind= (Plan 01)"
provides:
  - "`phaze agents add --kind {fileserver,compute}` flag (default fileserver)"
  - "--scan-roots now optional; enforced (non-empty + absolute) only for fileserver"
  - "add_agent(...) threads kind into Agent(kind=...)"
  - "AgentSettings.kind: Literal[fileserver,compute] bound to PHAZE_AGENT_KIND"
  - "Empty-scan-roots startup gate relaxed for kind == 'compute'"
affects:
  - src/phaze/cli/__init__.py
  - src/phaze/config.py
  - tests/test_cli/test_agents_add.py
  - tests/test_config/test_agent_settings_kind.py
tech-stack:
  added: []
  patterns:
    - "3-layer enum defense: argparse choices= (CLI) + Literal (config) + DB CHECK (Plan 01)"
    - "AliasChoices(PHAZE_AGENT_KIND, kind) mirrors the existing agent_env field shape"
    - "conditional required-field gate inside a mode=after model_validator"
key-files:
  created:
    - tests/test_config/test_agent_settings_kind.py
  modified:
    - src/phaze/cli/__init__.py
    - src/phaze/config.py
    - tests/test_cli/test_agents_add.py
decisions:
  - "scan-roots requirement enforced in main() (kind=='fileserver' branch) AND in the config validator — the CLI gate keeps argparse UX friendly, the config gate protects worker startup"
  - "kind threaded as a plain `str` param on add_agent with default 'fileserver' (back-compat); the Literal/choices enums sit at the CLI and config edges"
  - "compute keeps agent_api_url + agent_token unconditional — only scan_roots is relaxed"
metrics:
  duration: ~6min
  tasks: 2
  files: 4
  tests_added: 9
  completed: 2026-06-25
requirements: [CLOUDAGENT-01]
---

# Phase 48 Plan 02: Compute-agent registration + config gate Summary

Wired the operator-facing and worker-startup relaxations that let a media-less `compute` (cloud) agent exist (CLOUDAGENT-01): a `--kind {fileserver,compute}` CLI flag that makes `--scan-roots` optional for compute, and an `AgentSettings.kind` Literal field that relaxes the empty-scan-roots startup gate so a compute worker boots with no roots. Together with Plan 01's DB CHECK these form the 3-layer enum defense (argparse choices → config Literal → `ck_agents_kind_enum`).

## What Was Built

**Task 1 — CLI `--kind` flag + conditional scan-roots (TDD):**
- `src/phaze/cli/__init__.py`:
  - Added `add.add_argument("--kind", choices=("fileserver","compute"), default="fileserver", ...)` — the outer enum-defense layer (rejects bad kinds before any session opens).
  - `--scan-roots` is now `required=False, default=""`.
  - `add_agent(...)` and `_run_add(...)` gained a `kind: str = "fileserver"` param; `add_agent` passes `kind=kind` into the `Agent(...)` constructor.
  - `main()` threads `args.kind` through and enforces the non-empty/absolute scan-roots rule (via the existing `validate_scan_roots`) **only when `kind == "fileserver"`** — a fileserver with no roots fails with a friendly message before any DB access; compute proceeds with `scan_roots=[]`.
  - D-13 preserved: the minted token is still `print()`-only and never reaches a logger.
- `tests/test_cli/test_agents_add.py` (+5 tests): `test_add_agent_compute_empty_roots`, `test_add_agent_defaults_fileserver`, `test_main_compute_no_scan_roots_succeeds`, `test_main_fileserver_without_scan_roots_fails`, `test_main_compute_token_not_logged` (asserts the token appears in stdout but not in `caplog.text`).

**Task 2 — `AgentSettings.kind` field + relaxed startup gate (TDD):**
- `src/phaze/config.py`:
  - Added `kind: Literal["fileserver","compute"] = Field(default="fileserver", validation_alias=AliasChoices("PHAZE_AGENT_KIND","kind"), ...)` — mirrors the existing `agent_env` field shape; reuses the already-imported `Literal`/`Field`/`AliasChoices`. This is the middle enum-defense layer.
  - `_enforce_required_agent_fields` (mode="after") now gates the scan_roots branch on `self.kind != "compute"`. `agent_api_url` and `agent_token` stay unconditional for every kind.
- `tests/test_config/test_agent_settings_kind.py` (new, +6 tests): kind default, compute-accepts-empty-roots, fileserver-still-requires-roots, compute-still-requires-api_url, compute-still-requires-token, PHAZE_AGENT_KIND env alias.

## Verification

- `uv run pytest tests/test_cli/test_agents_add.py tests/test_config/ -q` — **62 passed** (27 CLI + 35 config, against the ephemeral `just test-db` Postgres on port 5433). No config regression.
- `uv run ruff check` + `uv run mypy` on `src/phaze/cli/__init__.py` and `src/phaze/config.py` — clean.
- Manual sanity (migrated `phaze_test` DB): `uv run phaze agents add --kind compute --id smoke-compute --name "Smoke Compute"` printed a token + `queue: phaze-agent-smoke-compute`; the inserted row shows `kind = compute`, `scan_roots = []`. `--kind fileserver` with no `--scan-roots` exits rc=1 with `--scan-roots is required for --kind fileserver`.

## Threat Model Coverage

- **T-48-02 (Tampering — `--kind` + `AgentSettings.kind`)** mitigated: argparse `choices=("fileserver","compute")` rejects bad CLI values before a session opens (outer layer); `kind: Literal[...]` rejects bad config at settings construction (middle layer); Plan 01's `ck_agents_kind_enum` is the inner backstop.
- **T-48-03 (Information Disclosure — bearer token)** mitigated: the `--kind` change touches no token-handling line; the token stays `print()`-only. `test_main_compute_token_not_logged` asserts the token does not appear in `caplog.text`.
- **T-48-04 (EoP — relaxed scan-roots gate)** mitigated: only `scan_roots` is relaxed and only for `kind == "compute"`; `agent_api_url` + `agent_token` stay required for all kinds (tests `test_compute_still_requires_api_url` / `_token`), so a compute agent still authenticates with a bearer over HTTP.
- **T-48-SC (dependency install)**: no packages installed — only stdlib argparse + already-pinned pydantic-settings. No supply-chain surface.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing critical functionality] explicit empty-roots guard for fileserver in `main()`**
- **Found during:** Task 1
- **Issue:** With `--scan-roots` made optional (`default=""`), a `--kind fileserver` invocation with no roots would parse to an empty `scan_roots` list. `validate_scan_roots([])` is a no-op (the loop never runs), so the empty list would slip through and a fileserver agent would be created with no scan roots — defeating the whole reason fileservers exist.
- **Fix:** Added an explicit `if not scan_roots: raise ValueError("--scan-roots is required for --kind fileserver ...")` inside the `kind == "fileserver"` branch before calling `validate_scan_roots`. The config-layer validator (`_enforce_required_agent_fields`) is the redundant backstop at worker startup.
- **Files modified:** src/phaze/cli/__init__.py
- **Commit:** c8e9ff3

Note: the ruff-format pre-commit hook reordered imports in the new config test file on the first RED commit attempt, aborting that commit; re-staged and committed cleanly (4e8f952). No behavior change.

## Commits

- 2f7b995 — test(48-02): failing CLI --kind compute tests (RED)
- c8e9ff3 — feat(48-02): CLI --kind flag + conditional scan-roots (GREEN)
- 4e8f952 — test(48-02): failing AgentSettings.kind tests (RED)
- c39a0c0 — feat(48-02): AgentSettings.kind + relaxed scan-roots gate (GREEN)

## TDD Gate Compliance

Both tasks followed RED → GREEN. Each `test(...)` RED commit precedes its `feat(...)` GREEN commit in git history. No REFACTOR commit was needed (both implementations were minimal and clean).

## Known Stubs

None.

## Self-Check

- Files exist: src/phaze/cli/__init__.py, src/phaze/config.py, tests/test_cli/test_agents_add.py, tests/test_config/test_agent_settings_kind.py — verified below.
- Commits exist: 2f7b995, c8e9ff3, 4e8f952, c39a0c0 — verified below.

## Self-Check: PASSED

All 4 created/modified source+test files present; all 4 task commits present in git history.
