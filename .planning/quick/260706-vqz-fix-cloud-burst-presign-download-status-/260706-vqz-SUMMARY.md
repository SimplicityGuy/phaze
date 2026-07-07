---
phase: quick-260706-vqz
plan: 01
subsystem: cloud-burst / k8s presign-download
tags: [bugfix, cloud-burst, k8s, presign, WR-03]
requirements: [WR-03]
requires:
  - "phaze.models.cloud_job.CloudJobStatus"
provides:
  - "presign_download readiness guard keyed on a downloadable/staged status set {UPLOADED, SUBMITTED, RUNNING}"
affects:
  - "src/phaze/routers/agent_files.py"
tech-stack:
  added: []
  patterns:
    - "module-level frozenset[str] status-set guard with a bug-referencing comment"
key-files:
  created: []
  modified:
    - "src/phaze/routers/agent_files.py"
    - "tests/agents/routers/test_agent_presign_download.py"
decisions:
  - "Dedicated _PRESIGN_DOWNLOADABLE_STATUSES frozenset in the router, NOT services/backends.py IN_FLIGHT (that includes UPLOADING, which is not downloadable)."
metrics:
  duration: "~15 min"
  completed: "2026-07-07"
  tasks: 2
  files: 2
---

# Quick 260706-vqz: Fix Cloud-Burst Presign-Download Status Guard Summary

One-liner: Widened the `presign_download` readiness guard so a live k8s analyze pod (cloud_job
`SUBMITTED`/`RUNNING`) can actually fetch its staged bytes, unblocking the k8s cloud-burst path
end-to-end.

## What Changed

The presign-download readiness guard in `src/phaze/routers/agent_files.py` previously 409'd unless
`cloud_job.status == UPLOADED`. But `submit_cloud_job.py:117` advances the status to `SUBMITTED` at
Kueue Job creation, BEFORE the analyze pod ever runs and calls presign-download. A live pod therefore
always observes `SUBMITTED` (or later `RUNNING`) and could never fetch its bytes — cloud analysis
could never complete. Found in the first live k8s cloud-burst E2E, 2026-07-07 (image 2026.7.3).

### Task 1 — Widen the guard (commit `2cb54a07`)

- Added a module-level `_PRESIGN_DOWNLOADABLE_STATUSES: frozenset[str]` built from
  `{CloudJobStatus.UPLOADED.value, CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value}`
  with an explanatory comment referencing bug 260706-vqz and the `submit_cloud_job.py:117` timing.
- Changed the guard condition from `cloud_job_status != CloudJobStatus.UPLOADED.value` to
  `cloud_job_status not in _PRESIGN_DOWNLOADABLE_STATUSES`.
- Kept `cloud_job_row is None` as the first disjunct and preserved the informative 409 detail
  (`f"staged object not ready (cloud_job status={cloud_job_status!r})"`).
- Left untouched: the 404-on-unknown-file path, the bucket-resolvability 409, the server-sourced
  `expected_sha256` from `FileRecord`, and the AUTH-01 path-only `file_id` behavior.
- Deliberately did NOT reuse `services/backends.py` `IN_FLIGHT` (that set includes `UPLOADING`, which
  is not yet fully staged and so must NOT be downloadable).

### Task 2 — Boundary regression tests (commit `624e6480`)

Extended `tests/agents/routers/test_agent_presign_download.py`, reusing the existing `s3_env`,
`authenticated_client`, `session`, `seed_test_agent` fixtures and the `_seed_file` / `_seed_cloud_job`
helpers:

- `test_presign_download_staged_non_terminal_returns_url` (parametrized `SUBMITTED`, `RUNNING`) —
  200 with a presigned `download_url` containing the `file_id` and `expected_sha256 == _SHA`. This is
  the bug boundary.
- `test_presign_download_terminal_status_returns_409` (parametrized `SUCCEEDED`, `FAILED`) — 409 with
  `"not ready"` in the detail.
- Existing `UPLOADING → 409`, no-cloud_job `→ 409`, unknown-file `→ 404`, and unresolvable-bucket
  `→ 409` tests left unchanged and still green.

## Behavior Matrix (verified)

| cloud_job.status | Result |
| ---------------- | ------ |
| UPLOADED         | 200 (unchanged) |
| SUBMITTED        | 200 (**bug fix**) |
| RUNNING          | 200 (**bug fix**) |
| UPLOADING        | 409 |
| SUCCEEDED        | 409 |
| FAILED           | 409 |
| (no cloud_job)   | 409 |

## Verification (actual command output)

- `uv run ruff check src/phaze/routers/agent_files.py tests/agents/routers/test_agent_presign_download.py` → **All checks passed!**
- `uv run ruff format --check tests/agents/routers/test_agent_presign_download.py` → **1 file already formatted**
- `uv run mypy src/phaze/routers/agent_files.py` → **Success: no issues found in 1 source file**
- `uv run pytest tests/agents/routers/test_agent_presign_download.py` → **12 passed** (was 8; +4 parametrized cases)
- `uv run pytest --cov=phaze.routers.agent_files` across the presign + discovery agent_files suites →
  **27 passed, src/phaze/routers/agent_files.py 100.00%** (≥90% requirement met)
- `pre-commit run --all-files` → **all hooks Passed** (ruff, ruff-format, bandit, mypy, actionlint,
  yamllint, shellcheck, etc.). NEVER used `--no-verify`.

Note: these tests are DB-backed; ran against the ephemeral integration Postgres/Redis (`just test-db`,
ports 5433/6380).

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None.

## Self-Check: PASSED

- `src/phaze/routers/agent_files.py` — FOUND, contains `_PRESIGN_DOWNLOADABLE_STATUSES`.
- `tests/agents/routers/test_agent_presign_download.py` — FOUND, contains the new parametrized tests.
- Commit `2cb54a07` (Task 1) — FOUND in git log.
- Commit `624e6480` (Task 2) — FOUND in git log.
