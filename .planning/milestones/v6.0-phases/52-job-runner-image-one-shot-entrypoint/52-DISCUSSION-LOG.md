# Phase 52: Job-runner image & one-shot entrypoint - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-27
**Phase:** 52-job-runner-image-one-shot-entrypoint
**Areas discussed:** Exit-code contract, In-pod resilience, Pod log format, GHCR build & publish

---

## Exit-code contract

| Option | Description | Selected |
|--------|-------------|----------|
| Distinct per failure class | 0=success, 10=download, 11=integrity, 12=analysis, 13=callback; most debuggable from pod status + Workload events | ✓ |
| Two-bucket | 0=success, 1=any failure (cause in logs); simplest honest contract | |
| You decide | Let the planner pick granularity | |

**User's choice:** Distinct per failure class
**Notes:** Unattended Jobs benefit from at-a-glance cause from pod status without reading logs. Exact integers are Claude's discretion; granularity is locked.

---

## In-pod resilience

| Option | Description | Selected |
|--------|-------------|----------|
| Retry callback only | Fail-fast on download/integrity/analysis; bounded retries on the final callback POST so a network blip doesn't waste a completed multi-hour analysis | ✓ |
| Pure fail-fast | Any error → immediate non-zero exit; control plane re-drives whole Job | |
| Retry download + callback | Bounded retries on presign GET, download, and callback; fail-fast only on integrity/analysis | |

**User's choice:** Retry callback only
**Notes:** Stays within KSUBMIT-05 ("control plane solely owns retry") — the in-pod retry is a delivery retry of an already-produced result, not a re-attempt of the work unit.

---

## Pod log format

| Option | Description | Selected |
|--------|-------------|----------|
| Structured JSON | One JSON object per step with file_id, timing, outcome; greppable/parseable | ✓ |
| Match app logger | Reuse phaze's existing logging config verbatim | (effectively this — see notes) |
| Plain human logs | Simple readable lines; easiest to eyeball, less machine-parseable | |

**User's choice:** Structured JSON
**Notes:** During discussion, confirmed the app already has `src/phaze/logging_config.py` (`configure_logging()`) — a structlog pipeline that renders JSON when stdout isn't a TTY (the pod case) and is import-safe (stdlib + structlog only, no Postgres). The "structured JSON" choice resolves to reusing that function, which also satisfies the "match app logger" intent — cluster logs look identical to homelab logs.

---

## GHCR build & publish

| Option | Description | Selected |
|--------|-------------|----------|
| Same release-tag workflow | Add the Job image as another target in the existing GHCR publish workflow, tagged off the same annotated v-tag push | ✓ |
| Dedicated workflow | Separate workflow + independent tag, rev'able without a full phaze release | |
| You decide | Let research pick the cleanest integration | |

**User's choice:** Same release-tag workflow
**Notes:** Confirmed `.github/workflows/docker-publish.yml` already builds a matrix of x86 targets (api/audfprint/panako); the Job image becomes another matrix entry. Versions stay in lockstep; matches the project's annotated-v-tag-push release procedure.

---

## Claude's Discretion

- Exact entrypoint module structure / file layout and how much v5.0 analysis-agent code to factor into a shared helper.
- Exact exit-code integers (granularity locked, numbers flexible).
- Callback retry count / backoff tuning.

## Deferred Ideas

None — discussion stayed within phase scope. (Out of scope for v6.0 and already tracked in REQUIREMENTS.md: multi-arch Job image / KJOB-06, ConfigMap-mounted CA rotation / KDEPLOY-06, GPU/Coral acceleration.)
