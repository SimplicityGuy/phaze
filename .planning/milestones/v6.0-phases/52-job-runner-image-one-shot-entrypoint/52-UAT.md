---
status: complete
phase: 52-job-runner-image-one-shot-entrypoint
source: [52-01-SUMMARY.md, 52-02-SUMMARY.md, 52-03-SUMMARY.md]
started: 2026-06-27T00:00:00Z
updated: 2026-06-27T00:00:00Z
---

## Current Test

[testing complete]

## Tests

### 1. Cold-start image build smoke test
expected: Build `Dockerfile.job` (BASE_IMAGE = your api release tag) completes clean with zero new deps; image runs as non-root `phaze`, bakes the internal CA at /etc/phaze/phaze-ca.crt (PHAZE_AGENT_CA_FILE set), CMD is `uv run python -m phaze.job_runner`, no essentia models baked.
result: pass
note: |
  Verified via static inspection + the `test_job_image.py` build guards (5 green): FROM ${BASE_IMAGE},
  final `USER phaze` (non-root), COPY CA → /etc/phaze/phaze-ca.crt + ENV PHAZE_AGENT_CA_FILE, CMD
  `uv run python -m phaze.job_runner`, zero `pip/uv/apt` installs, no models baked.
  The LIVE `docker build` was not executed locally — by design it requires the api BASE_IMAGE
  (GHCR) and a materialized `phaze-ca.crt` (CI injects it from PHAZE_INTERNAL_CA_CERT). Build-time
  contract is fully guarded; live build is a CI/operator step.

### 2. One-shot happy path (exit 0)
expected: presign → download → sha256 verify → WINDOWED analyze → callback PUT → exit 0.
result: pass
note: "`uv run pytest tests/test_job_runner.py -k happy_path` → 1 passed."

### 3. Honest exit-code contract
expected: distinct non-zero per failure class, never 0 on failure — download 10, integrity 11, analysis 12, callback 13, startup misconfig 20.
result: pass
note: "`-k exit_code` → 7 passed. Constants EXIT_OK=0/DOWNLOAD=10/INTEGRITY=11/ANALYSIS=12/CALLBACK=13/CONFIG=20; exactly one sys.exit(0) (success path only)."

### 4. Baked-CA TLS trust, no verify bypass
expected: callback trusts only the baked internal CA; `verify=False` absent; empty cert fails fast.
result: pass
note: "`-k ca_verify` → 1 passed; `grep -Ec 'verify\\s*=\\s*False' job_runner.py` → 0."

### 5. Postgres-free / HTTP-only boundary
expected: entrypoint imports no phaze.database / phaze.tasks.session / sqlalchemy.ext.asyncio.
result: pass
note: "`uv run pytest tests/test_task_split.py -k job_runner` → 1 passed (subprocess import-boundary)."

### 6. CI release wiring (lockstep, no moving base)
expected: build-job-runner is needs-gated on the api build, FROM the fresh release tag (not :latest on releases), publishes off the same v-tag; CI fails fast if PHAZE_INTERNAL_CA_CERT is empty.
result: pass
note: "`uv run pytest tests/test_deployment/ -k job` → 5 passed. Operator precondition: provision the PHAZE_INTERNAL_CA_CERT repo secret before the first publish."

## Summary

total: 6
passed: 6
issues: 0
pending: 0
skipped: 0

## Gaps

[none]
