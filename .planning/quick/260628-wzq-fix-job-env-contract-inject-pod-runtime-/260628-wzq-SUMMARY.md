---
phase: quick-260628-wzq
plan: 01
subsystem: control-plane / kube-staging
tags: [k8s, kueue, job-manifest, config, env-contract, docs]
requires: []
provides:
  - ControlSettings.kube_env_configmap_name / kube_env_secret_name
  - build_job_manifest emits PHAZE_JOB_FILE_ID + configMapRef/secretRef envFrom
  - operator runbook §6 Agent-env ConfigMap
affects:
  - src/phaze/config.py
  - src/phaze/services/kube_staging.py
key-files:
  created: []
  modified:
    - src/phaze/config.py
    - src/phaze/services/kube_staging.py
    - tests/test_services/test_kube_staging.py
    - docs/k8s-burst.md
decisions:
  - "envFrom sources the static-per-deployment agent env (ConfigMap + Secret); PHAZE_JOB_FILE_ID is code-injected per-Job because it cannot come from a static object"
  - "kube_env_secret_name defaults to the existing phaze-agent-token bearer-token Secret — no new Secret object required"
  - "New Internal-CA section renumbered §6→§7; agent-env ConfigMap inserted as §6 to keep it adjacent to the bearer-token Secret (§5)"
metrics:
  duration: ~12 min
  completed: 2026-06-28
---

# Quick Task 260628-wzq: Fix JOB-ENV-CONTRACT (inject pod runtime env) Summary

Closed the v6.0 milestone-audit critical blocker where `build_job_manifest` injected ONLY
`PHAZE_AGENT_CA_FILE`, so every admitted Kueue pod hit `job_runner.run()` with no
`PHAZE_JOB_FILE_ID` (and no agent role/url/token) and `sys.exit(EXIT_CONFIG)=20` before any
analysis — dead-lettering every file via `ANALYSIS_FAILED`.

## What changed

- **Two new `ControlSettings` knobs** (`config.py`): `kube_env_configmap_name` (default
  `phaze-agent-env`) and `kube_env_secret_name` (default `phaze-agent-token`), mirroring
  `kube_ca_secret_name` exactly (plain `str`, default, `AliasChoices`, description). Not added to
  `SECRET_FILE_FIELDS`; `_enforce_kube_config_when_k8s` untouched; defaults keep all existing
  instantiations valid.
- **`build_job_manifest` env contract** (`kube_staging.py`): appended
  `{"name": "PHAZE_JOB_FILE_ID", "value": str(file_id)}` to the analyze container `env` (keeping
  the existing `PHAZE_AGENT_CA_FILE` entry) and added an `envFrom` block
  `[{"configMapRef": {"name": cfg.kube_env_configmap_name}}, {"secretRef": {"name": cfg.kube_env_secret_name}}]`.
  All pre-existing fields (suspend, CA volume/mount, requests-only resources, labels,
  fail-loud-on-missing) unchanged.
- **Regression test** (`test_kube_staging.py`): `test_build_job_manifest_injects_env_contract`
  asserts the file_id env entry, both envFrom refs (tracked off `stub_cfg` knobs), and the
  retained CA entry — the test that would have caught the bug. Extended `_StubCfg.defaults` with
  the two new knobs.
- **Operator runbook** (`docs/k8s-burst.md`): new §6 "Agent-env ConfigMap" with `kubectl` +
  declarative YAML, the secretRef reuse of the existing bearer-token Secret, the per-Job
  `PHAZE_JOB_FILE_ID` injection note, and the name-override env vars. Internal-CA section
  renumbered to §7; deploy ordering + smoke test updated. No internal planning IDs in the new prose.

## TDD

Task 2 followed RED → GREEN: the new test failed first (`PHAZE_JOB_FILE_ID` absent from the
single-entry env list), then passed after the manifest edit.

## Deviations from Plan

Minor additive doc touch-ups beyond the strict task scope (Rule 2 — runbook correctness): added
the agent-env ConfigMap + internal-CA Secret to the deploy-ordering step 3 and the smoke-test
"Manifests apply clean" line so the runbook stays internally consistent after the new section.

## Tests / Evidence

- `uv run pytest tests/test_services/test_kube_staging.py -q` → **26 passed**
- `uv run pytest tests/test_config/ tests/test_config_role_split.py tests/test_config_worker.py -q` → **127 passed**
- `uv run mypy src/phaze/services/kube_staging.py src/phaze/config.py` → **Success: no issues found in 2 source files**
- All pre-commit hooks passed on every commit (no `--no-verify`).

## Commits

- `99bc940` feat(quick-260628-wzq): add kube_env_configmap_name + kube_env_secret_name ControlSettings knobs
- `62e9cc3` fix(quick-260628-wzq): inject PHAZE_JOB_FILE_ID + envFrom into build_job_manifest
- `5f43aa7` docs(quick-260628-wzq): document agent-env ConfigMap + envFrom secretRef in k8s runbook

## Self-Check: PASSED

- Files modified exist: config.py, kube_staging.py, test_kube_staging.py, docs/k8s-burst.md ✓
- Commits present in git log: 99bc940, 62e9cc3, 5f43aa7 ✓
- Targeted tests + mypy green (evidence above) ✓
