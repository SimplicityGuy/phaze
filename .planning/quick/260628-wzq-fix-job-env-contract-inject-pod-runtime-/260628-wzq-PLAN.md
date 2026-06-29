---
phase: quick-260628-wzq
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/config.py
  - src/phaze/services/kube_staging.py
  - tests/test_services/test_kube_staging.py
  - docs/k8s-burst.md
autonomous: true
requirements: [JOB-ENV-CONTRACT, KJOB-02, KSUBMIT-01, KSTAGE-03, KROUTE-02]
must_haves:
  truths:
    - "An admitted Kueue pod receives PHAZE_JOB_FILE_ID and no longer exits EXIT_CONFIG=20 for a missing file id"
    - "An admitted Kueue pod sources PHAZE_ROLE / PHAZE_AGENT_API_URL / PHAZE_AGENT_TOKEN / PHAZE_MODELS_DIR from operator-created ConfigMap + Secret via envFrom"
    - "build_job_manifest keeps suspend, CA volume/mount, requests-only resources, labels, and fail-loud-on-missing-image/cpu/memory behavior intact"
    - "A manifest-env-contract test asserts the file_id env entry and the configMapRef/secretRef envFrom — the test that would have caught the bug"
    - "Existing ControlSettings instantiations keep working because the new knobs have defaults (no new required-when-k8s fields)"
  artifacts:
    - path: "src/phaze/config.py"
      provides: "kube_env_configmap_name and kube_env_secret_name ControlSettings knobs"
      contains: "kube_env_configmap_name"
    - path: "src/phaze/services/kube_staging.py"
      provides: "PHAZE_JOB_FILE_ID env entry + envFrom block in build_job_manifest"
      contains: "PHAZE_JOB_FILE_ID"
    - path: "tests/test_services/test_kube_staging.py"
      provides: "manifest-env-contract regression test"
      contains: "PHAZE_JOB_FILE_ID"
    - path: "docs/k8s-burst.md"
      provides: "operator runbook for the agent-env ConfigMap + envFrom secretRef"
      contains: "configMapRef"
  key_links:
    - from: "src/phaze/services/kube_staging.py"
      to: "src/phaze/config.py"
      via: "cfg.kube_env_configmap_name / cfg.kube_env_secret_name"
      pattern: "kube_env_(configmap|secret)_name"
    - from: "build_job_manifest container env"
      to: "src/phaze/job_runner.py PHAZE_JOB_FILE_ID read"
      via: "PHAZE_JOB_FILE_ID env entry value == str(file_id)"
      pattern: "PHAZE_JOB_FILE_ID"
---

<objective>
Fix JOB-ENV-CONTRACT (v6.0 milestone-audit critical blocker): `build_job_manifest` in
`src/phaze/services/kube_staging.py` injects ONLY `PHAZE_AGENT_CA_FILE` into the analyze
container, so every admitted Kueue pod hits `job_runner.run()`, finds no `PHAZE_JOB_FILE_ID`
(and no agent role/url/token), and `sys.exit(EXIT_CONFIG)` = 20 before any analysis.

This plan closes the manifest->pod env seam by (1) code-injecting the per-Job
`PHAZE_JOB_FILE_ID`, (2) adding an `envFrom` block that sources the static-per-deployment agent
env from an operator-created ConfigMap + Secret named by two new `ControlSettings` knobs, (3)
documenting those objects in the cluster-admin runbook, and (4) adding the manifest-env-contract
test that would have caught the bug.

Purpose: make the live K8s burst end-to-end actually run analysis instead of dead-lettering
every file via `ANALYSIS_FAILED`.
Output: 2 new config knobs, an env-contract-complete Job manifest, a regression test, and an
operator runbook section.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@./CLAUDE.md
@.planning/v6.0-MILESTONE-AUDIT.md

<interfaces>
<!-- Pinned facts the executor needs — no codebase exploration required. -->

CONSTRAINTS (CLAUDE.md): Python 3.14, `uv run` for every command (never bare pip/python/pytest/mypy),
double quotes, type hints on all functions, 150-col line length, mypy strict, ruff clean, >=85% coverage.

DESIGN DECISIONS (pinned — do NOT deviate):
- The two new knobs mirror `kube_ca_secret_name` EXACTLY: plain `str` (NOT Optional, NOT SecretStr),
  with a default value, an `AliasChoices(...)` validation alias, and a description. They are object
  NAMES, not secrets — do NOT add them to `SECRET_FILE_FIELDS` and do NOT use the `_FILE` convention.
- Defaults are chosen so existing ControlSettings instantiations keep working and
  `_enforce_kube_config_when_k8s` (config.py:648) needs NO new required fields. Do NOT touch that validator.
  - `kube_env_configmap_name` default: "phaze-agent-env"
  - `kube_env_secret_name`     default: "phaze-agent-token"  (reuses the EXISTING bearer-token Secret
    already documented in docs/k8s-burst.md §5, which already carries PHAZE_AGENT_TOKEN)

EXISTING ControlSettings field to mirror (config.py:569-573):
```
kube_ca_secret_name: str = Field(
    default="phaze-internal-ca",
    validation_alias=AliasChoices("PHAZE_KUBE_CA_SECRET_NAME", "kube_ca_secret_name"),
    description="...",
)
```

POD-SIDE CONTRACT the manifest must satisfy:
- job_runner.py:78  `_FILE_ID_ENV = "PHAZE_JOB_FILE_ID"`  (read at :159; sys.exit(20) if absent)
- job_runner.py:79  `_MODELS_DIR_ENV = "PHAZE_MODELS_DIR"`
- job_runner.py:155 requires `get_settings()` to return `AgentSettings` (needs PHAZE_ROLE=agent)
- AgentSettings requires PHAZE_AGENT_API_URL (config.py:703) and PHAZE_AGENT_TOKEN (config.py:707)
- ConfigMap carries: PHAZE_ROLE=agent, PHAZE_AGENT_API_URL, PHAZE_MODELS_DIR
- Secret carries:    PHAZE_AGENT_TOKEN  (already documented in §5)

CURRENT build_job_manifest container env (kube_staging.py:181-183) — KEEP this entry, ADD to it:
```
"env": [
    {"name": "PHAZE_AGENT_CA_FILE", "value": "/certs/phaze-ca.crt"},
],
```

TEST STUB to extend (test_kube_staging.py:44-60): `_StubCfg(SimpleNamespace)` `defaults` dict must
gain `kube_env_configmap_name` and `kube_env_secret_name` or build_job_manifest will AttributeError.
The ONLY config stub feeding build_job_manifest is this `_StubCfg`; real submit paths use real
ControlSettings (defaults apply).
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add kube_env_configmap_name + kube_env_secret_name ControlSettings knobs</name>
  <files>src/phaze/config.py</files>
  <action>
    In ControlSettings, immediately after the `kube_ca_secret_name` field (config.py:569-573),
    add two new fields that mirror it EXACTLY in shape (plain `str`, a default, an `AliasChoices`
    validation alias, a description):
      - `kube_env_configmap_name: str` default "phaze-agent-env",
        alias `AliasChoices("PHAZE_KUBE_ENV_CONFIGMAP_NAME", "kube_env_configmap_name")`.
        Description (operator-facing): names the operator-created core/v1 ConfigMap the suspended
        Job sources its static agent env from via envFrom (PHAZE_ROLE=agent, PHAZE_AGENT_API_URL,
        PHAZE_MODELS_DIR); phaze references it by name only and never authors it.
      - `kube_env_secret_name: str` default "phaze-agent-token",
        alias `AliasChoices("PHAZE_KUBE_ENV_SECRET_NAME", "kube_env_secret_name")`.
        Description (operator-facing): names the operator-created core/v1 Secret the suspended Job
        sources PHAZE_AGENT_TOKEN from via envFrom; defaults to the existing bearer-token Secret;
        phaze references it by name only and never authors it.
    Do NOT add these to SECRET_FILE_FIELDS, do NOT make them Optional/SecretStr, and do NOT modify
    `_enforce_kube_config_when_k8s` — defaults must keep all existing instantiations valid.
  </action>
  <verify>
    <automated>uv run python -c "from phaze.config import ControlSettings; c=ControlSettings(); assert c.kube_env_configmap_name=='phaze-agent-env'; assert c.kube_env_secret_name=='phaze-agent-token'; print('ok')"</automated>
  </verify>
  <done>ControlSettings exposes both knobs with the pinned defaults; default-construction still succeeds (no new required fields).</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Inject PHAZE_JOB_FILE_ID + envFrom into build_job_manifest, with regression test</name>
  <files>src/phaze/services/kube_staging.py, tests/test_services/test_kube_staging.py</files>
  <behavior>
    - The analyze container env list contains `{"name": "PHAZE_JOB_FILE_ID", "value": str(file_id)}`
      IN ADDITION TO the existing PHAZE_AGENT_CA_FILE entry.
    - The analyze container has an `envFrom` list with two entries:
      `{"configMapRef": {"name": cfg.kube_env_configmap_name}}` and
      `{"secretRef": {"name": cfg.kube_env_secret_name}}`.
    - All pre-existing manifest fields are unchanged: suspend True, parallelism/completions 1,
      backoffLimit 0, ttlSecondsAfterFinished == JOB_TTL_SECONDS, restartPolicy Never, the phaze-ca
      volume + read-only /certs mount, requests-only resources (no limits), the three labels, and the
      fail-loud on missing image/cpu/memory.
  </behavior>
  <action>
    In `build_job_manifest` (kube_staging.py), in the analyze container dict:
      1. Append `{"name": "PHAZE_JOB_FILE_ID", "value": str(file_id)}` to the existing `env` list
         (keep the PHAZE_AGENT_CA_FILE entry first; the per-Job file id CANNOT come from a static
         ConfigMap/Secret so it must be code-injected here).
      2. Add an `"envFrom"` key on the container with
         `[{"configMapRef": {"name": cfg.kube_env_configmap_name}}, {"secretRef": {"name": cfg.kube_env_secret_name}}]`
         (sources the static-per-deployment PHAZE_ROLE/PHAZE_AGENT_API_URL/PHAZE_MODELS_DIR and
         PHAZE_AGENT_TOKEN the pod entrypoint requires). Add a short comment explaining the per-Job
         vs static-env split, matching the existing CA comment style.
    Leave every other field of the manifest exactly as-is.

    In test_kube_staging.py:
      3. Extend `_StubCfg.defaults` (around line 48) with
         `"kube_env_configmap_name": "phaze-agent-env"` and
         `"kube_env_secret_name": "phaze-agent-token"`.
      4. Add `test_build_job_manifest_injects_env_contract(stub_cfg)` asserting: (a)
         `{"name": "PHAZE_JOB_FILE_ID", "value": str(fid)}` is in the analyze container `env`; (b)
         the container `envFrom` contains `{"configMapRef": {"name": "phaze-agent-env"}}` and
         `{"secretRef": {"name": "phaze-agent-token"}}`; (c) the existing PHAZE_AGENT_CA_FILE env
         entry is still present (regression guard for the additive change). Reference the configured
         names from `stub_cfg` so the assertion tracks the knobs, not a literal.
    Run the new test RED-first if practical (before editing the manifest) to confirm it catches the gap.
  </action>
  <verify>
    <automated>uv run pytest tests/test_services/test_kube_staging.py -x -q</automated>
  </verify>
  <done>The new manifest carries PHAZE_JOB_FILE_ID (== str(file_id)) and the configMapRef/secretRef envFrom; all existing test_build_job_manifest_* tests still pass.</done>
</task>

<task type="auto">
  <name>Task 3: Document the agent-env ConfigMap + envFrom secretRef in the cluster-admin runbook</name>
  <files>docs/k8s-burst.md</files>
  <action>
    Add a new runbook subsection (place it logically near §5 "Bearer-token Secret" / §6
    "Internal-CA Secret", e.g. a new "Agent-env ConfigMap" step) documenting the env contract the
    suspended Job now sources via envFrom. Include:
      - A `core/v1` ConfigMap (default name `phaze-agent-env`, namespace `phaze`) with `data:`
        PHAZE_ROLE: agent, PHAZE_AGENT_API_URL: <control-plane https URL>, PHAZE_MODELS_DIR:
        <in-image models path>. Provide both a `kubectl create configmap` example and the
        equivalent declarative YAML.
      - A note that PHAZE_AGENT_TOKEN is sourced via `envFrom.secretRef` from the EXISTING
        bearer-token Secret (default name `phaze-agent-token`, §5) — no new Secret needed.
      - A note that the suspended Job's analyze container declares
        `envFrom: [configMapRef(phaze-agent-env), secretRef(phaze-agent-token)]`, and that
        PHAZE_JOB_FILE_ID is injected PER-JOB by phaze at submit time (NOT operator-managed, NOT in
        the ConfigMap).
      - A note that the names are overridable via PHAZE_KUBE_ENV_CONFIGMAP_NAME /
        PHAZE_KUBE_ENV_SECRET_NAME on the control plane (mirrors the PHAZE_KUBE_CA_SECRET_NAME note in §6).
    Keep it operator-facing prose: do NOT leak internal planning IDs (D-NN, T-54-NN, KJOB/KSUBMIT
    codes) into the new text.
  </action>
  <verify>
    <automated>grep -q "phaze-agent-env" docs/k8s-burst.md && grep -q "configMapRef\|envFrom" docs/k8s-burst.md && grep -q "PHAZE_KUBE_ENV_CONFIGMAP_NAME" docs/k8s-burst.md && echo ok</automated>
  </verify>
  <done>Runbook documents the agent-env ConfigMap, the envFrom secretRef reuse of the bearer-token Secret, the per-Job PHAZE_JOB_FILE_ID injection, and the name-override env vars — with no internal planning IDs.</done>
</task>

</tasks>

<verification>
Run the full quality gate (CLAUDE.md — uv run only):
- `uv run ruff check .` and `uv run ruff format --check .` clean
- `uv run mypy .` clean (strict)
- `uv run pytest tests/test_services/test_kube_staging.py -q` green
- `uv run pytest --cov --cov-report=term-missing` full suite green, coverage >=85%
</verification>

<success_criteria>
- build_job_manifest emits PHAZE_JOB_FILE_ID (== str(file_id)) plus a configMapRef + secretRef envFrom, while keeping suspend/CA-mount/requests-only/labels/fail-loud intact.
- Two new ControlSettings knobs exist with defaults; no existing instantiation or k8s fail-fast validator changed.
- A manifest-env-contract test asserts the file_id entry and both envFrom refs.
- docs/k8s-burst.md documents the operator-created ConfigMap + the secretRef reuse, operator-facing, no internal IDs.
- ruff + mypy + full suite green, coverage >=85%.
</success_criteria>

<output>
Create `.planning/quick/260628-wzq-fix-job-env-contract-inject-pod-runtime-/260628-wzq-SUMMARY.md` when done.
</output>
