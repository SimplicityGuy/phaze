# 56-06 Summary — Runtime CA Secret mount (reverse KJOB-05, implement KDEPLOY-06)

**Status:** Complete. **Branch:** `gsd/phase-56-deployment-runbook-config-docs`.

## Problem

`build-job-runner` in `.github/workflows/docker-publish.yml` hard-failed on **every `main` push**
at the "Materialize the internal CA cert" step (`exit 1`) because the `PHAZE_INTERNAL_CA_CERT` repo
secret was unset. Root cause is architectural, not a missing secret: the internal CA is generated
**per-deployment at runtime** by `cert_bootstrap` and is unique per operator, so there is no
canonical CA to bake — and baking a private CA into a **public GHCR image** is wrong. The original
KJOB-05 "bake the CA into the image" decision was the mistake.

## Resolution (user-chosen: runtime CA mount)

Reversed KJOB-05; implemented the previously-deferred KDEPLOY-06 as a **Secret** mount.

| # | Change | File(s) |
|---|--------|---------|
| 1 | RED test: manifest must carry the CA volume/mount/env | `tests/test_services/test_kube_staging.py` |
| 2 | GREEN: `kube_ca_secret_name` knob + manifest `volumes`/`volumeMounts` (ro `/certs`) + `PHAZE_AGENT_CA_FILE` env | `src/phaze/config.py`, `src/phaze/services/kube_staging.py` |
| 3 | Strip the bake → **fixes the build error** | `Dockerfile.job`, `.github/workflows/docker-publish.yml` |
| 4 | Operator CA-Secret runbook (§6 YAML + kubectl + rotation), knob doc, deployment rotation note | `docs/k8s-burst.md`, `docs/configuration.md`, `docs/deployment.md` |
| 5 | Decision flip recorded | `.planning/REQUIREMENTS.md` |
| 6 | Corrected stale "baked CA" comments → "mounted at runtime" | `src/phaze/job_runner.py`, `tests/conftest.py`, `tests/test_job_runner.py` |

## How the CA reaches the pod now

Operator creates a `core/v1` Secret `phaze-internal-ca` (key `phaze-ca.crt`, **public cert only —
never the CA key**). `build_job_manifest` mounts it read-only at `/certs`; the container sets
`PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt`. `construct_agent_client` verifies the control-plane TLS
chain against it; the `st_size == 0` guard still fails loud on an empty CA — `verify=False` appears
nowhere. CA rotation = Secret update + re-submit, **no image rebuild**.

## Verification

- `tests/test_services/test_kube_staging.py` (25) + `test_job_runner.py` + CA-consumer
  `test_agent_client_tls.py`/`test_agent_client.py` + config tests: **all pass**.
- `ruff`, `ruff format`, `mypy`, `bandit`, `actionlint`, hadolint (Dockerfile), yamllint: **pass**.
- No residual `PHAZE_INTERNAL_CA_CERT` / `COPY phaze-ca.crt` references remain (comments only).
- Full-suite DB-dependent failures (837 errors) are **environmental** — no local Postgres on
  `localhost:5432` (confirmed `Errno 61` connection-refused); CI provisions the DB. None are caused
  by this slice (no DB code touched).

## Decisions

- **KJOB-05** — superseded (CA no longer baked).
- **KDEPLOY-06** — implemented now (Secret, not ConfigMap, since it carries cert material).
- **KDEPLOY-01** — the CA Secret is an operator-created object phaze does NOT author; runbook ships
  copy-paste YAML for it.

## Commits

`docs(56-06): plan…` → `feat(56-06): mount internal CA…` → `fix(56-06): stop baking…` →
`docs(56-06): document…Secret` → `docs(56-06): record KJOB-05 superseded…` →
`docs(56-06): correct stale 'baked CA' wording`.
