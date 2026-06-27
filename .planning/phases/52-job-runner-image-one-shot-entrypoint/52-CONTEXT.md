# Phase 52: Job-runner image & one-shot entrypoint - Context

**Gathered:** 2026-06-27
**Status:** Ready for planning

<domain>
## Phase Boundary

Build the **x86 Kueue Job-runner image** (published to GHCR, `FROM` the existing x86
essentia base image, **zero new pip dependencies**) plus a **one-shot pod entrypoint**
that executes the v6.0 burst-analysis flow for a single long file and exits:

> request a fresh presigned download URL from the control plane → download the file →
> sha256-verify against `FileRecord` → windowed/streaming analyze → POST the result to
> `/api/internal/agent/*` (reconciled by `file_id`) → exit.

Locked by KJOB-01..05 (see REQUIREMENTS.md):
- x86-only image, `FROM` the existing essentia base, zero new pip deps (KJOB-01)
- the 6-step one-shot flow above (KJOB-02)
- windowed/streaming analysis only — **no** whole-file `MonoLoader` decode, so a
  multi-hour set never OOMs under a hard pod memory limit (KJOB-03)
- honest exit-code contract — never reports success on a failed analysis (KJOB-04)
- internal CA **baked into the image** for the HTTPS callback; no `verify=False`
  anywhere (KJOB-05)

This phase is independently unit-testable without a live cluster (the entrypoint is
exercised against a fake control plane / fixtures; no kube API here — that arrives in
Phase 54). Discussion below clarifies HOW to implement the locked flow.
</domain>

<decisions>
## Implementation Decisions

### Exit-code contract (KJOB-04)
- **D-01:** Use **distinct exit codes per failure class**, not a two-bucket
  success/fail. Suggested mapping (planner may adjust the exact integers):
  `0` = success, `10` = download/presign failure, `11` = integrity/sha256 mismatch,
  `12` = analysis failure, `13` = callback POST failure. Rationale: Jobs are
  unattended; a per-class exit code makes the failure cause legible from pod status +
  Workload events without reading logs first. Must stay consistent with KJOB-04 —
  exit code is non-zero on any download/integrity/analysis/callback failure, and the
  entrypoint never exits `0` on a failed analysis.

### In-pod resilience (interacts with KSUBMIT-05)
- **D-02:** **Retry the final callback POST only.** Fail-fast (immediate non-zero
  exit) on presign/download, integrity, and analysis errors — the control plane
  re-drives the whole Job. But the callback POST gets **bounded short retries**
  (e.g. ~3× exponential backoff) because by that point a completed multi-hour
  analysis result is in hand and a transient network blip should not throw it away.
  This stays within KSUBMIT-05 ("control plane solely owns retry", Job
  `backoffLimit`/Kueue requeue neutralized): the in-pod retry is a *delivery* retry
  of an already-produced result, not a re-attempt of the work unit.

### Logging / observability
- **D-03:** Emit **structured JSON to stdout**, one event per pipeline step
  (presign, download, verify, analyze, callback) carrying at minimum `file_id`,
  step outcome, and timing. Implement by reusing the app's existing structlog
  pipeline — call `phaze.logging_config.configure_logging()` in the entrypoint.
  It already renders JSON when stdout is not a TTY (the pod case) and is
  deliberately import-safe (stdlib + structlog only, no Postgres/`phaze.config`
  import), so it runs cleanly in the Postgres-less pod. `kubectl logs` is the only
  debugging surface for unattended Jobs, so logs must be greppable/parseable.

### GHCR build & publish (KJOB-01)
- **D-04:** Build & tag the Job-runner image in the **same release-tag workflow** as
  the existing images — add it as another target in `.github/workflows/docker-publish.yml`
  alongside the `api`/`audfprint`/`panako` matrix entries, tagged off the same
  annotated `v`-tag push. One release procedure; the Job image version stays in
  lockstep with the rest of phaze. (Consistent with the project's release procedure:
  annotated v-tag push triggers GHCR publish.)

### Claude's Discretion
- Exact entrypoint module structure / file layout (new module vs. thin shim), and how
  much v5.0 analysis-agent code to factor into a shared helper vs. inline — planner +
  researcher decide based on the existing `analysis.py` windowed API and the
  `entrypoint.py` precedent.
- Exact exit-code integers (the *granularity* — per-class — is locked; the specific
  numbers are flexible).
- Callback retry count/backoff tuning.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase scope & requirements
- `.planning/ROADMAP.md` — Phase 52 line + the v6.0 Kubernetes Burst Analysis intro
  (the image → legs → pipeline → routing → deploy spine; Phase 52 is the image leg).
- `.planning/REQUIREMENTS.md` §"Job-runner image & one-shot entrypoint (KJOB)" —
  KJOB-01..05 are the locked requirements for this phase; also see the v6.0
  out-of-scope and explicit-non-goals lists (no GPU/Coral, no multi-arch, no
  ConfigMap CA in v6.0 — KJOB-06/KDEPLOY-06 are deferred).
- `.planning/PROJECT.md` §"Key Decisions" — CPU-only cluster rationale; internal-CA
  and pre-uvicorn-entrypoint-shim precedents.

### Image build (FROM target + precedent)
- `Dockerfile` — the **x86 essentia base** this image builds `FROM`
  (`python:3.14-slim` + essentia native runtime libs; `USER phaze`). Confirm whether
  to base off its `base` stage or the published `api` image.
- `Dockerfile.agent-arm64` — v5.0 analysis-agent image precedent (build structure,
  CA handling, agent runtime layout). Note: that one is arm64 source-built; Phase 52
  is x86 and reuses the existing x86 essentia wheel — **no source build**.
- `.github/workflows/docker-publish.yml` — existing GHCR publish workflow; the Job
  image becomes a new matrix target here (D-04). Note the `image_suffix`/bare-repo
  URL convention (Phase 29 D-15) and the cache/metadata steps.

### Entrypoint flow building blocks
- `src/phaze/logging_config.py` — `configure_logging()`; reuse for D-03 structured
  JSON logging (import-safe, Postgres-less).
- `src/phaze/services/analysis.py` — windowed/streaming analysis API
  (`_iter_windows`, `_analyze_fine_windows`, `_analyze_coarse_windows`); the KJOB-03
  no-whole-file-decode path the entrypoint must call.
- `src/phaze/entrypoint.py` — v4.0 pre-uvicorn cert-bootstrap shim; precedent for
  PID-1 / signal-clean process startup in a container entrypoint.
- `src/phaze/routers/agent_analysis.py` + `src/phaze/schemas/agent_analysis.py` —
  the `/api/internal/agent/*` callback target and result schema (reconciled by
  `file_id`); the POST payload the entrypoint produces must match this.

No new external specs introduced during discussion — decisions above fully capture
this phase's gray areas.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `phaze.logging_config.configure_logging()` — drop-in structured-JSON logging for
  the pod; already TTY-aware and Postgres-import-free.
- `phaze.services.analysis` windowed API — the streaming analysis path that satisfies
  KJOB-03; the entrypoint orchestrates these rather than reimplementing analysis.
- `src/phaze/entrypoint.py` — model for a clean container entrypoint (bootstrap then
  exec); the one-shot can follow the same signal/PID-1 discipline.
- `Dockerfile.agent-arm64` — image-layout precedent (CA baking, agent user, runtime
  deps), adapted from arm64-source-build to x86-wheel-reuse.

### Established Patterns
- **HTTP-only agent boundary (v4.0):** the pod has zero Postgres access; everything
  goes through `/api/internal/agent/*` with a per-agent bearer token. The one-shot
  must honor this — no DB imports in the entrypoint path (mirror the
  `test_agent_worker_does_not_import_phaze_database` boundary).
- **`_FILE`-convention secrets (v4.0.1):** control-plane URL, bearer token, and
  file_id reach the pod via env / file-based secrets, not request bodies.
- **Self-signed internal CA (v4.0):** baked into the image for HTTPS callback
  trust (KJOB-05); ConfigMap-mounted rotation is explicitly deferred (KDEPLOY-06).
- **Reconcile-by-`file_id`:** the callback is keyed on `file_id`, not on any pod- or
  Job-scoped identity.

### Integration Points
- GHCR: new build target in `.github/workflows/docker-publish.yml`.
- Callback: POST to `/api/internal/agent/*` matching `schemas/agent_analysis.py`.
- This phase produces only the image + entrypoint; the S3 staging leg (Phase 53),
  kube submit/watch (Phase 54), and routing seam (Phase 55) wire it into the live
  pipeline. Phase 52 stands alone and is unit-testable against a fake control plane.
</code_context>

<specifics>
## Specific Ideas

- Exit codes should be at-a-glance meaningful from `kubectl get pods` / Workload
  events, not buried in logs (drove D-01).
- The callback result is the expensive artifact (hours of CPU) — protect its delivery
  with retry even though the work itself is fail-fast (drove D-02).
- Cluster logs should look like homelab logs — same structlog JSON pipeline (drove D-03).
</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope. (Explicitly out of scope for v6.0 and
already tracked in REQUIREMENTS.md: multi-arch Job image / KJOB-06, ConfigMap-mounted
CA rotation / KDEPLOY-06, GPU/Coral acceleration.)

</deferred>

---

*Phase: 52-job-runner-image-one-shot-entrypoint*
*Context gathered: 2026-06-27*
