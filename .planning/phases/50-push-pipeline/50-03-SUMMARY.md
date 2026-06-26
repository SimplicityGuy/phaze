---
phase: 50-push-pipeline
plan: 03
subsystem: agent-push-pipeline
tags: [rsync, ssh, agent-task, scratch-janitor, postgres-free-boundary]
requires:
  - "50-01: PushFilePayload, AgentSettings push_* fields, schemas.agent_push (PushedResponse/PushMismatchResponse)"
provides:
  - "push_file agent task (rsync-over-SSH transfer + exit-code handling + on-success callback)"
  - "compute-only startup scratch janitor (_sweep_scratch / _maybe_sweep_scratch)"
  - "PhazeAgentClient.report_pushed / report_push_mismatch callback methods"
affects:
  - "50-04 (process_file scratch read/verify/cleanup — consumes report_push_mismatch)"
  - "50-05 (control-plane agent_push router — receives report_pushed/report_push_mismatch)"
  - "51 (operator provisioning of rsync/openssh + push_* secrets, real transfer verification)"
tech-stack:
  added: []
  patterns:
    - "asyncio.create_subprocess_exec with list argv (shell-free) for the rsync transport"
    - "file-mounted SecretStr materialized to 0600 temp files, shredded in finally"
    - "compute-only startup janitor gated on kind==compute + cloud_scratch_dir (off-loop via to_thread)"
key-files:
  created:
    - "src/phaze/tasks/push.py"
  modified:
    - "src/phaze/services/agent_client.py"
    - "src/phaze/tasks/agent_worker.py"
    - "tests/test_push_pipeline.py"
    - "tests/test_task_split.py"
decisions:
  - "Execution reordered Task 3 -> Task 1 -> Task 2 so push.py's mypy/pre-commit (it calls api.report_pushed) is satisfiable before commit (Rule 3 — blocking)."
  - "SSH key + known_hosts SecretStr hold file CONTENTS (the _FILE convention reads the file), so push_file materializes them to private 0600 temp files for ssh -i / UserKnownHostsFile, shredded in finally."
  - "Missing rsync/ssh binary maps to a clear terminal RuntimeError (no local-analysis fallback) rather than a callback, per the CLOUDROUTE-02 directional invariant."
  - "The compute-only gate is a named helper (_maybe_sweep_scratch) rather than an inline if, for direct unit-testability of the compute/fileserver split."
metrics:
  duration: ~30m
  completed: 2026-06-26
  tasks: 3
  files_created: 1
  files_modified: 4
---

# Phase 50 Plan 03: File-server Push Pipeline (push_file) Summary

**One-liner:** Implemented the novel `push_file` agent task — a shell-free, host-key-pinned
rsync-over-SSH transfer to the compute scratch dir with full exit-code handling and an on-success
control-side callback — plus the compute-only startup scratch janitor and the `report_pushed` /
`report_push_mismatch` client methods, all while keeping the agent worker Postgres-free.

## What was built

### Task 3 — `report_pushed` / `report_push_mismatch` client methods (`agent_client.py`)
Two HTTP callbacks routed through the existing `_request` tenacity funnel (5xx retries, 4xx
surfaces immediately), path-scoped (`file_id` on the URL only, AUTH-01), no body, lazy-importing
their response schemas from `phaze.schemas.agent_push`. ORM-free — no DB import added.

### Task 1 — `push_file` rsync task (`push.py`, NEW)
- `_build_rsync_argv` (pure, unit-testable): a Python list argv with `rsync` first;
  `--partial-dir=.rsync-partial` + `--timeout=<push_timeout_sec>`; a single `-e "ssh …"` element
  carrying `-i <key> -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<known_hosts> -o
  BatchMode=yes -o ConnectTimeout=<n>`; remote dest `<user>@<host>:<scratch_dir>/<file_id>.<ext>`
  (server UUID, never the untrusted filename). Omits `--inplace`, `-z`, `-c`, `-a`.
- `push_file`: validates `PushFilePayload`, materializes the file-mounted SSH key + known_hosts
  `SecretStr` to private 0600 temp files (shredded in `finally`), spawns via
  `asyncio.create_subprocess_exec`, wraps `communicate()` in `asyncio.wait_for` (outer bound =
  rsync timeout + 30s; TimeoutError → kill+wait+raise). rc==0 → `api.report_pushed(file_id)` and
  return `{"status":"pushed"}`; rc!=0 → `RuntimeError` with a bounded (500-char) stderr snippet
  (SAQ retry, `--partial` resumes); `FileNotFoundError` (rsync/ssh absent) → clear terminal
  `RuntimeError`, no callback, never a local fallback.
- Module docstring asserts the Postgres-free boundary; `test_task_split.py` extended with a
  subprocess import-boundary case for `phaze.tasks.push`.

### Task 2 — registration + compute-only janitor (`agent_worker.py`)
- `from phaze.tasks.push import push_file`; appended to `settings["functions"]`.
- `_sweep_scratch(scratch_dir)`: stdlib-only removal of every file + the `.rsync-partial` dir,
  tolerates a missing dir.
- `_maybe_sweep_scratch(cfg)`: gate on `kind == "compute"` AND `cloud_scratch_dir`; the fileserver
  (same module) is a no-op. Wired into `startup()` off the event loop (`asyncio.to_thread`) after
  the models check and before the dispatch loop.

## Verification

- `uv run pytest tests/test_push_pipeline.py tests/test_task_split.py -q` → 18 passed.
- `uv run pytest tests/test_services/test_agent_client.py -q` → 15 passed (no regression).
- `uv run ruff check .` → all checks passed. `uv run mypy .` → 0 issues in 167 source files.
- All pre-commit hooks (ruff, ruff-format, bandit, mypy) green on every task commit.
- Real rsync-over-Tailscale transfer is a Phase 51 manual verification (50-VALIDATION.md
  Manual-Only) — out of scope here; the argv builder, exit-code mapping, and janitor are covered
  by unit tests with the subprocess mocked.

## Threat model coverage

| Threat ID | Mitigation delivered |
|-----------|----------------------|
| T-50-injection | `create_subprocess_exec` with a fixed list argv (no shell); remote path is `<file_id>.<ext>` (server UUID). Test asserts the filename never reaches the dest. |
| T-50-spoof | `-o StrictHostKeyChecking=yes -o UserKnownHostsFile=<pinned> -o BatchMode=yes` in the argv (asserted). |
| T-50-scratch-dos | compute-only `_sweep_scratch` full-sweep at startup (gated; fileserver no-op). |
| T-50-hang | `--timeout` + ssh `ConnectTimeout` + outer `asyncio.wait_for`; `BatchMode=yes` blocks auth-prompt hangs. |
| T-50-secret-leak | key materialized to 0600 temp file then shredded; bounded stderr snippet; test asserts key contents absent from the error message. |
| T-50-no-fallback | `FileNotFoundError` → terminal `RuntimeError` naming the binary; never analyzes locally. |

## Deviations from Plan

**1. [Rule 3 — Blocking issue] Task execution reordered to 3 → 1 → 2.**
- **Found during:** Task 1 setup. `push.py` calls `api.report_pushed`, which mypy (and the
  pre-commit `uv run mypy .` hook) require to exist on `PhazeAgentClient` before `push.py` can be
  committed green.
- **Fix:** Executed Task 3 (client methods) first, then Task 1, then Task 2. Each task still has
  its own atomic commit and each commit is independently green.
- **Files modified:** none beyond the planned per-task files.
- **Commit:** 7ccb9a7 (Task 3) precedes 46bbe5a (Task 1).

**2. [Rule 2 — Missing critical functionality] SSH-secret materialization to temp files.**
- **Found during:** Task 1. The `push_ssh_key` / `push_known_hosts` fields are `SecretStr` holding
  the file CONTENTS (the `_FILE` convention reads the mounted file), but `ssh -i` /
  `UserKnownHostsFile` need filesystem PATHS.
- **Fix:** `push_file` writes the secret contents to a private 0600 temp dir for the transfer and
  shreds them in `finally`. Also added `_require_push_config` to fail fast with a clear terminal
  error when push config is incomplete (the fields are operator-provisioned in Phase 51).
- **Files modified:** `src/phaze/tasks/push.py`.
- **Commit:** 46bbe5a.

No architectural changes were required (no Rule 4 checkpoints). No authentication gates occurred.

## Known Stubs

None. The `push_*` config fields are intentionally operator-provisioned in Phase 51 (declared in
50-01); `push_file` fails fast with a clear terminal error if they are absent at runtime, which is
the intended Phase-50 behavior, not a stub.

## TDD Gate Compliance

Tasks 1 and 2 (`tdd="true"`) followed RED → GREEN:
- Task 1: `test(50-03)` c2f8b84 (RED — push module absent) → `feat(50-03)` 46bbe5a (GREEN).
- Task 2: `test(50-03)` b70425f (RED — `_sweep_scratch` absent) → `feat(50-03)` 6beb3c0 (GREEN).
No unexpected passes during RED; no REFACTOR commits were needed.

## Commits

| Task | Type | Hash | Description |
|------|------|------|-------------|
| 3 | feat | 7ccb9a7 | report_pushed / report_push_mismatch client methods |
| 1 | test | c2f8b84 | RED: push_file argv + exit-code tests |
| 1 | feat | 46bbe5a | GREEN: push_file rsync-over-SSH task |
| 2 | test | b70425f | RED: registration + compute-only janitor tests |
| 2 | feat | 6beb3c0 | GREEN: register push_file + _sweep_scratch janitor |

## Self-Check: PASSED
- `src/phaze/tasks/push.py` — FOUND
- commit 7ccb9a7, c2f8b84, 46bbe5a, b70425f, 6beb3c0 — FOUND
