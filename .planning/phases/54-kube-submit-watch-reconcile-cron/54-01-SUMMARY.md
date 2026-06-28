---
phase: 54-kube-submit-watch-reconcile-cron
plan: 01
subsystem: config
tags: [config, kube, kueue, dependency, supply-chain, secrets]
requires:
  - "ControlSettings + SECRET_FILE_FIELDS / _resolve_secret_files (Phase 26/v4.0.1)"
  - "Phase 53 S3 staging config surface (analog shape)"
provides:
  - "kr8s control-plane dependency"
  - "ControlSettings.cloud_submit_max_attempts (D-08)"
  - "ControlSettings kube client surface (kube_api_url/namespace/local_queue/job_image/cpu+memory_request/workload_api_version)"
  - "ControlSettings kube_kubeconfig/kube_sa_token SecretStr creds via SECRET_FILE_FIELDS"
affects:
  - "Phase 54 submit seam, submit task, reconcile cron (read these knobs)"
  - "Phase 55/56 fail-fast kube-config-when-cloud-enabled validator (KDEPLOY-02)"
tech-stack:
  added:
    - "kr8s>=0.20.15 (Kubernetes client, BSD-3-Clause, pure-Python)"
  patterns:
    - "Optional config fields mirroring s3_* shape (default None, AliasChoices dual form)"
    - "Bounded int knob mirroring push_max_attempts (gt=0, lt=20)"
    - "_FILE-resolved SecretStr creds via SECRET_FILE_FIELDS membership (no new resolver code)"
key-files:
  created:
    - "tests/test_config/test_kube_settings.py"
  modified:
    - "pyproject.toml"
    - "uv.lock"
    - "src/phaze/config.py"
    - "docs/configuration.md"
decisions:
  - "kr8s placed as a control-plane dependency only (kube creds never reach agent/pod, mirroring aioboto3)"
  - "cloud_submit_max_attempts is a DISTINCT retry budget from push_max_attempts (D-08), not an alias"
  - "kube_* fields kept optional with NO cloud_burst_enabled coupling — that fail-fast validator is Phase 55/56 (KDEPLOY-02); coupling here would break Phase 53 cloud-on/no-kube deploys"
metrics:
  duration: "~9 min"
  completed: "2026-06-28"
  tasks: 2
  files: 5
requirements: [KSUBMIT-01, KSUBMIT-05]
---

# Phase 54 Plan 01: Kube Submit/Reconcile Config Foundation Summary

Installed the legitimacy-gated kr8s control-plane Kubernetes client and added the full kube
config surface to `ControlSettings` — the `cloud_submit_max_attempts` retry budget (D-08) plus an
optional kube client surface and `_FILE`-resolved `SecretStr` credentials — all optional in Phase
54 so existing Phase 53 cloud-on/no-kube deploys keep working.

## What Was Built

- **kr8s dependency** (`pyproject.toml`/`uv.lock`): `kr8s>=0.20.15` added as a control-plane
  dependency, alphabetically sorted between `httpx` and `litellm`, with a comment noting it is
  control-plane-only (kube creds never reach the agent/pod, mirroring aioboto3) and was verified
  legitimate before install (T-54-SC).
- **`cloud_submit_max_attempts: int`** (`config.py`): mirrors `push_max_attempts` exactly
  (default=3, gt=0, lt=20, `AliasChoices("PHAZE_CLOUD_SUBMIT_MAX_ATTEMPTS","cloud_submit_max_attempts")`)
  — a distinct retry budget for the kube submit leg per D-08.
- **Kube client surface** (`config.py`, all `default=None` except the apiVersion default):
  `kube_api_url`, `kube_namespace`, `kube_local_queue`, `kube_job_image`, `kube_job_cpu_request`,
  `kube_job_memory_request` (all `str | None`), and `kube_workload_api_version: str` defaulting to
  `"kueue.x-k8s.io/v1beta1"`. No model validator couples them to `cloud_burst_enabled`.
- **Kube credentials** (`config.py`): `kube_kubeconfig: SecretStr | None` and
  `kube_sa_token: SecretStr | None`, with both added to `ControlSettings.SECRET_FILE_FIELDS` so the
  existing `_resolve_secret_files` before-validator auto-resolves their `<VAR>_FILE` siblings — no
  new resolver code.
- **Tests** (`tests/test_config/test_kube_settings.py`): 14 tests covering the default budget,
  out-of-range rejection (0 and 20), distinctness from `push_max_attempts`, kube_* defaults + env
  binding, the no-coupling-to-cloud_burst guarantee, SecretStr `_FILE` resolution + repr masking,
  and the T-54-01 control-plane-only invariant (AgentSettings has no kube_* fields).
- **Docs** (`docs/configuration.md`): new "Kube submit/reconcile settings (Phase 54, v6.0)" table
  with a row per field, noting they are optional in Phase 54 and fail-fast validation arrives in
  Phase 56.

## Tasks Completed

| Task | Name | Commit | Files |
| ---- | ---- | ------ | ----- |
| 1 | kr8s package-legitimacy gate (blocking) | n/a (verification-only) | (none) |
| 2 | Install kr8s + add kube config surface to ControlSettings | 01ff159 | pyproject.toml, uv.lock, src/phaze/config.py, tests/test_config/test_kube_settings.py, docs/configuration.md |

### Task 1 — checkpoint resolution

Task 1 was a `checkpoint:human-verify` blocking-human supply-chain legitimacy gate for kr8s. The
operator pre-approved it before this executor ran: kr8s 0.20.15 was verified on PyPI (released
2026-01-16, `requires_python >=3.9`, repo github.com/kr8s-org/kr8s, BSD-3-Clause, maintainer Jacob
Tomlinson, pure-Python `py3-none-any` wheels, 100+ releases) and the operator typed "approved".
Recorded as satisfied; the executor proceeded directly to Task 2.

## Deviations from Plan

**1. [Plan hygiene] Re-positioned kr8s relative to the litellm comment block.**
- **Found during:** Task 2
- **Issue:** `uv add kr8s` inserted the dependency alphabetically but landed it *after* the
  multi-line litellm supply-chain comment, separating that comment from the `litellm` line it
  documents.
- **Fix:** Moved `kr8s>=0.20.15` above the litellm comment block and gave kr8s its own explanatory
  comment, so each comment stays attached to its dependency. Ordering remains alphabetical
  (httpx < kr8s < litellm).
- **Files modified:** pyproject.toml
- **Commit:** 01ff159

No other deviations — the plan executed as written.

## Verification

- `uv run pytest tests/test_config/test_kube_settings.py -x` — 14 passed.
- `uv run python -c "import kr8s"` — exits 0 (kr8s 0.20.15).
- `uv run ruff check src/phaze/config.py tests/test_config/test_kube_settings.py` — All checks passed.
- `uv run mypy src/phaze/config.py` — Success: no issues found.
- All pre-commit hooks passed on commit (ruff, ruff-format, bandit, mypy).

### Acceptance criteria

- `cloud_submit_max_attempts` Field has `gt=0, lt=20, default=3`. ✓
- 5 kube client-surface field definitions present (`kube_api_url`/`kube_namespace`/`kube_local_queue`/`kube_job_image`/`kube_workload_api_version`). ✓
- `kube_kubeconfig` appears both as a SecretStr field AND inside SECRET_FILE_FIELDS. ✓
- `kr8s` in pyproject.toml; `import kr8s` exits 0. ✓
- No model validator references `cloud_target` or couples kube_* to `cloud_burst_enabled`. ✓ (grep for `cloud_target` returns 0)
- ruff + mypy pass for touched files. ✓

## Known Stubs

None — all fields are wired into the real `ControlSettings` validation machinery. The kube_* fields
are intentionally optional (not stubs): the submit seam, submit task, and reconcile cron in later
Phase 54 plans read them, and the fail-fast validator coupling them to `cloud_burst_enabled` is
deliberately deferred to Phase 55/56 (KDEPLOY-02) per D-08 / the plan's no-coupling directive.

## Self-Check: PASSED

- FOUND: src/phaze/config.py
- FOUND: tests/test_config/test_kube_settings.py
- FOUND: pyproject.toml
- FOUND: docs/configuration.md
- FOUND commit: 01ff159
