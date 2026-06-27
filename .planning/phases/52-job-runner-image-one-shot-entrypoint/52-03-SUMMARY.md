---
phase: 52-job-runner-image-one-shot-entrypoint
plan: 03
subsystem: deployment
tags: [docker, ci, kubernetes-job, image, ca-trust]
requires:
  - api image (Dockerfile) published to GHCR
provides:
  - Dockerfile.job (x86 one-shot Job-runner image)
  - build-job-runner workflow job (needs-gated, ghcr.io/<owner>/phaze/job)
  - static deployment guards for the Job image contract
affects:
  - .github/workflows/docker-publish.yml
tech-stack:
  added: []
  patterns:
    - "FROM api base via ARG BASE_IMAGE (zero new deps)"
    - "needs-gated CI job cloned from parity-golden-x86"
    - "baked internal CA (COPY) instead of mounted volume"
key-files:
  created:
    - Dockerfile.job
    - tests/test_deployment/test_job_image.py
  modified:
    - .github/workflows/docker-publish.yml
decisions:
  - "Job image tagged at ghcr.io/<owner>/phaze/job sub-path off the same v-tag set (D-04 lockstep)"
  - "BASE_IMAGE resolved via fromJSON(metadata-action json).tags[0] — the freshly-pushed api tag, never :latest (T-52-06)"
  - "CA cert materialized from PHAZE_INTERNAL_CA_CERT repo secret into the build context, baked to /etc/phaze/phaze-ca.crt (KJOB-05)"
  - "Job image build gated to non-PR events — FROM the api tag requires it to have been pushed first"
metrics:
  duration: "~12m"
  completed: 2026-06-27
  tasks: 2
  files: 3
requirements: [KJOB-01, KJOB-05]
---

# Phase 52 Plan 03: Job-runner image & release wiring Summary

Packaged the one-shot entrypoint into a publishable x86 `Dockerfile.job` (FROM the api base,
zero new deps, baked internal CA, `uv run python -m phaze.job_runner` CMD) and wired it into the
release pipeline via a `needs: build-and-push`-gated `build-job-runner` workflow job, proven by
four static deployment guards.

## What Was Built

### Task 1 — `Dockerfile.job` (commit b9dd2d4)
- `ARG BASE_IMAGE` + `FROM ${BASE_IMAGE}` — builds FROM the resolved freshly-pushed x86 api tag
  (which already carries Python 3.14 + essentia + native libs + the `phaze` package + uv). Zero
  `pip install`/`uv add`/`uv pip install` lines (KJOB-01).
- Switches to `USER root` only to `mkdir /etc/phaze` and `COPY phaze-ca.crt /etc/phaze/phaze-ca.crt`,
  sets `ENV PHAZE_AGENT_CA_FILE=/etc/phaze/phaze-ca.crt`, then drops back to the non-root `USER phaze`
  (uid/gid 1000 inherited from the base). CA is baked, not mounted (KJOB-05).
- `CMD ["uv", "run", "python", "-m", "phaze.job_runner"]` — `uv run` (not bare `python3 -m`) so the
  child exit code propagates through the container boundary (Pitfall 5).
- No essentia ML weights baked and no upf.edu download — provisioned externally at runtime (D-05).

### Task 2 — `build-job-runner` workflow job + static guards (commit a47bbb2)
- Added `build-job-runner` to `.github/workflows/docker-publish.yml`, cloning the `parity-golden-x86`
  `needs: build-and-push` shape (fixes Pitfall 1 — a sibling matrix row cannot order against the api push).
- Resolves the api base tag via `docker/metadata-action` (bare-repo URL, Phase 29 D-15) and passes
  `BASE_IMAGE=${{ fromJSON(steps.base-meta.outputs.json).tags[0] }}` to the build.
- Tags the Job image at `ghcr.io/<owner>/phaze/job` off the SAME annotated v-tag set as the api image
  (D-04 release lockstep), reusing the GHCR login + buildx + build-push step shapes (provenance + sbom).
- Materializes the internal CA from the `PHAZE_INTERNAL_CA_CERT` repo secret into the build context.
- `tests/test_deployment/test_job_image.py` — four PyYAML/grep static guards (no live docker build):
  (a) `build-job-runner` exists with `needs: build-and-push`; (b) the job builds `Dockerfile.job` and
  passes `BASE_IMAGE`; (c) `Dockerfile.job` has zero new deps and CMDs `phaze.job_runner`; (d)
  `Dockerfile.job` pins no `:latest` base and FROMs `BASE_IMAGE`.

## Verification

- `uv run pytest tests/test_deployment/ -k job -x` → 5 passed (4 new guards + 1 pre-existing match).
- `grep -Ec "pip install|uv add|uv pip install" Dockerfile.job` → 0.
- `pre-commit` clean on the workflow: actionlint, check-github-workflows (jsonschema), yamllint all Passed;
  hadolint Passed on Dockerfile.job; ruff + ruff-format + mypy + bandit Passed on the test.

## Threat Mitigations Applied

- **T-52-06** (stale base): FROM `ARG BASE_IMAGE` resolved to the freshly-pushed release tag;
  guard asserts `:latest` absent and FROM references BASE_IMAGE.
- **T-52-SC** (supply chain): zero new package installs; guard asserts no pip/uv add lines.
- **T-52-01** (TLS trust): internal CA baked + `PHAZE_AGENT_CA_FILE` set to the baked path; no `verify=False`.
- **T-52-09** (release drift): Job image built in the same workflow off the same annotated v-tag (D-04).

## Deviations from Plan

None — plan executed as written. The plan suggested a `/job` suffix OR a distinct tag for the Job
image; selected the `/job` sub-path (consistent with the audfprint/panako sidecar convention).

## Operator Setup Required (user_setup, KJOB-05)

The `build-job-runner` job expects a `PHAZE_INTERNAL_CA_CERT` GitHub Actions repo secret containing the
internal CA cert PEM. The job writes it to `phaze-ca.crt` in the build context so `Dockerfile.job` can
`COPY` and bake it. Without this secret the Job image build (non-PR only) bakes an empty cert, and
`construct_agent_client` will raise at runtime — surfacing the misconfiguration fast.

## Notes for Downstream Plans

- The Job image only becomes runnable once Plan 02's `src/phaze/job_runner.py` module ships — this plan
  has no source dependency on it (the CMD is a text string; guards are static).
- Phase 54 provisions the essentia models PVC mount at `/models` (D-05); this image bakes none.

## Self-Check: PASSED
- FOUND: Dockerfile.job
- FOUND: .github/workflows/docker-publish.yml
- FOUND: tests/test_deployment/test_job_image.py
- FOUND commit b9dd2d4 (Dockerfile.job)
- FOUND commit a47bbb2 (workflow + guards)
