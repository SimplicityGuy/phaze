---
quick_id: 260706-q90
title: Add optional models PVC mount to Kueue Job pod (models_pvc_name)
date: 2026-07-07
status: complete
---

# Quick Task 260706-q90 — Summary

## What shipped

An optional, backward-compatible per-Kueue-backend knob that mounts an
operator-provisioned models PVC read-only into every analyze pod, so the Kueue
burst path can run with an empty-`/models` image fixed by pre-provisioned storage
instead of a fat weights image or a runtime download.

## Commits (on `SimplicityGuy/phase-77`, off `main`)

| Commit | Change |
|--------|--------|
| `fca323bc` | `feat(config)`: add `KubeConfig.models_pvc_name: str \| None = None` (plain K8s object name, no SecretStr, no `*_file`) + TOML round-trip/default test |
| `2bf4778b` | `feat(kube)`: `build_job_manifest` mounts the PVC read-only at `/models` when set (second volume, separate from `/certs` CA mount); unset ⇒ byte-identical manifest + set/unset manifest tests |
| `478a3131` | `docs`: Dockerfile.job comment + configuration.md field row/prose + k8s-burst.md "Models provisioning" subsection |
| `767ec34b` | `chore(release)`: version bump `2026.7.2` → `2026.7.3` (pyproject + uv.lock) |

## Design decisions

- **`models_pvc_name` is a plain object name, not a secret** — matches the sibling
  `ca_secret_name` / `env_configmap_name` / `env_secret_name` fields. phaze creates
  no PV/PVC; it references the claim by name only (same posture as the LocalQueue /
  Secret / ConfigMap it references by name).
- **Second, separate volume** — the `models` PVC volume/mount is entirely additive
  and independent of the `phaze-ca` Secret mount at `/certs`. KDEPLOY-06 (runtime-
  mounted, never-baked CA) is preserved untouched.
- **Unset ⇒ byte-identical manifest** — regression-guarded by
  `test_build_job_manifest_omits_models_volume_when_unset` (asserts ONLY the
  `phaze-ca` volume/mount), so existing deploys are unaffected.
- **Invariant documented**: `/models` mountPath MUST equal the agent-env ConfigMap's
  `PHAZE_MODELS_DIR` — stated in the `build_job_manifest` docstring, configuration.md,
  and k8s-burst.md.
- **Round-trip test location**: added to `tests/analyze/services/test_backends.py`
  (as requested) as a real TOML→`resolve_backends` round-trip; the sibling direct-
  construction field test already lives in `test_backend_registry.py`.

## Verification

- `uv run pytest tests/analyze/services/test_kube_staging.py tests/analyze/services/test_backends.py tests/shared/config/ tests/shared/tasks/test_controller_startup_localqueue.py` → **172 passed** (test DB env set: 5433/6380).
- ruff check + ruff format --check + mypy → clean (also enforced by pre-commit on each commit).
- `just docs-drift` + `tests/shared/core/test_docs_beui03.py` → green.
- **HARD requirements verified**: branch diff (excl. `.planning/`) adds **no** cert/key/kubeconfig/SA-token material, no `-----BEGIN … KEY-----`, no baked secrets. The new PVC carries only essentia weights. `Dockerfile.job` stays weights-free (comment-only change).

## Release / next step (NOT yet done — requires user)

Publishing the api + job + sidecar images happens by pushing the bare CalVer tag
**`2026.7.3`** — but only **after** this branch merges to `main` (CI publishes on
`tags: ["[0-9]+.[0-9]+.[0-9]+"]` → docker-publish; per [[project_release_procedure]]).

1. Open a PR for `SimplicityGuy/phase-77` → `main`; get the `aggregate-results` gate green; merge.
2. On the merged `main` commit: `git tag -a 2026.7.3 -m "…" && git push origin 2026.7.3` → triggers the GHCR publish.

The tag push is an irreversible outward-facing publish, so it is left for explicit user go-ahead after merge.
