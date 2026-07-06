---
phase: 73-per-agent-dispatch-liveness-scratch-failure-isolation
plan: 02
subsystem: cloud-compute-push-transport
tags: [push-pipeline, rsync, per-agent-dispatch, failure-isolation, D-04]
requires:
  - PushFilePayload.dest_host / dest_scratch_dir / dest_ssh_user (Plan 73-01)
  - ComputeAgentBackend.dispatch destination stamp (Plan 73-01, record-don't-rederive)
provides:
  - payload-driven _build_rsync_argv (per-file remote_dest from dest_*)
  - reduced _require_push_config required set (secrets + fallback user only)
affects:
  - Plan 03 (/pushed + /mismatch callbacks resolve scratch via resolve_compute_backend; the
    fileserver transport now honors the payload-carried destination)
tech-stack:
  added: []
  patterns:
    - record-don't-rederive on the transport side (read the stamped destination, never re-derive)
    - None-fallback to a single-global config value preserves ≤1-compute byte-identical behavior
key-files:
  created: []
  modified:
    - src/phaze/tasks/push.py
    - tests/analyze/core/test_push_pipeline.py
decisions:
  - "_payload() test helper now carries dest_host/dest_scratch_dir matching _fake_cfg so the
    three existing argv-invariant tests stay byte-identical while exercising the per-file dest path"
  - "the only remaining cfg.push_ssh_host/cfg.cloud_scratch_dir mention in push.py is inside a
    docstring documenting the retirement — no code path reads them as the remote target (D-04)"
metrics:
  tasks: 2
  source-files-modified: 1
  test-files-modified: 1
  completed: 2026-07-05
---

# Phase 73 Plan 02: Payload-Driven Fileserver Rsync Destination Summary

The fileserver leg of MCOMP-03: the rsync push destination is now resolved per file from the
payload (`dest_host` / `dest_scratch_dir` / `dest_ssh_user`) instead of the single-global
`cfg.push_ssh_*` / `cfg.cloud_scratch_dir`. N compute agents each receive their files at their OWN
host/scratch. This is D-04 in practice — the fileserver's single push-destination env is retired,
with no ≤1-compute fallback path, while the compute agent's OWN local janitor dir is left untouched.

## What Was Built

**Task 1 — `_build_rsync_argv` reads the destination from the payload (D-04 remote-target retire).**
The remote_dest interpolation changed from `{cfg.push_ssh_user}@{cfg.push_ssh_host}:{cfg.cloud_scratch_dir}/…`
to `{payload.dest_ssh_user or cfg.push_ssh_user}@{payload.dest_host}:{payload.dest_scratch_dir}/…`.
Two payloads with distinct `dest_host`/`dest_scratch_dir` now produce two distinct remote destinations.
`dest_ssh_user=None` falls back to `cfg.push_ssh_user` (≤1-compute byte-identical); a set
`dest_ssh_user` is used verbatim. The entire ssh element (`StrictHostKeyChecking=yes`,
`BatchMode=yes`, pinned `UserKnownHostsFile`), `--partial-dir=.rsync-partial`, `--timeout`, the `--`
argv terminator, and the `payload.original_path` source are byte-identical — only the destination
source changed. The remote name remains `<scratch_dir>/<file_id>.<file_type>` (server UUID, never the
untrusted filename), so the argv-injection / path-traversal surface is unchanged.

**Task 2 — reduced `_require_push_config` required set (D-04) — secrets kept.** The `missing` tuple
dropped from `("push_ssh_host", "push_ssh_user", "cloud_scratch_dir", "push_ssh_key", "push_known_hosts")`
to `("push_ssh_user", "push_ssh_key", "push_known_hosts")`. `push_ssh_host` + `cloud_scratch_dir` are
now payload-carried (retired from the required set); `push_ssh_key` + `push_known_hosts` (secret
material, D-03) and `push_ssh_user` (the `dest_ssh_user=None` fallback source) stay required. The
WR-03 timeout-layering fail-fast (`outer_guard >= PUSH_FILE_SAQ_TIMEOUT_SEC`) is untouched. Per
Landmine 2 / D-04 the `AgentSettings.cloud_scratch_dir` field in `config.py` was NOT deleted — the
compute agent's OWN local janitor (`agent_worker.py`) still reads it locally.

## Deviations from Plan

None — plan executed exactly as written. The one supporting change (adding `dest_host`/`dest_scratch_dir`
to the `_payload()` test helper, matching `_fake_cfg`) is a test-fixture update inherent to switching
the destination source, keeping the three pre-existing argv-invariant tests byte-identical rather than a
behavioral deviation.

## Threat Model Coverage

| Threat ID | Disposition | Realized |
|-----------|-------------|----------|
| T-73-04 (payload.dest_* smuggling an rsync flag / shell metachar) | mitigate | `--` argv terminator + list-argv `create_subprocess_exec` (no shell) + Plan-01 pydantic dest validators; remote name stays `<scratch_dir>/<file_id>.<file_type>` (server UUID). New test asserts the `--`/source/dest ordering survives. |
| T-73-05 (SSH key / known_hosts leak via the retired-config change) | mitigate | D-03: `push_ssh_key` + `push_known_hosts` stay in the required set; still materialized to 0600 temp files + shredded in `finally`; `dest_*` remain non-secret. |
| T-73-06 (silent scratch-dir skew if cloud_scratch_dir were deleted → compute janitor AttributeError) | mitigate | Landmine 2 / D-04: `AgentSettings.cloud_scratch_dir` field kept (verified present in config.py); only the fileserver's remote-target read is retired. |
| T-73-SC (dependency installs) | accept | zero new dependencies; `pyproject` untouched. |

No new security surface beyond the plan's threat register. No threat flags.

## Verification

- Plan verification: `uv run pytest tests/analyze/core/test_push_pipeline.py -q` → **41 passed**.
- Acceptance greps: `payload.dest_host`/`payload.dest_scratch_dir` present in `_build_rsync_argv`;
  neither `cfg.push_ssh_host` nor `cfg.cloud_scratch_dir` appears in the `_require_push_config`
  `missing` tuple (only a docstring mention remains in push.py); `push_ssh_key` still in the required
  tuple; `AgentSettings.cloud_scratch_dir` field still present in `src/phaze/config.py:843`.
- `uv run ruff check` + `uv run mypy src/phaze/tasks/push.py` → clean.
- Regression (serial, per test-env note): `test_agent_push.py`, `test_agent_tasks.py`,
  `test_routing_seam.py`, `test_task_split.py` → **64 passed**; `test_dispatch_snapshot.py`,
  `test_staging_cron.py`, `test_backends.py`, `test_submit_cloud_job.py` → **81 passed**. No
  downstream payload/dispatch caller regressed.

## Known Stubs

None — every field read is real and wired (the dispatch producer stamps `dest_*` in Plan 01).

## Self-Check: PASSED

- Modified source files exist on disk: `src/phaze/tasks/push.py`, `tests/analyze/core/test_push_pipeline.py`.
- All four task commits (2 RED + 2 GREEN) exist in git history.
- Key symbols present: `payload.dest_host`, `payload.dest_scratch_dir`, reduced `missing` tuple.
