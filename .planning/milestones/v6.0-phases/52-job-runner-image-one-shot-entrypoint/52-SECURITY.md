---
phase: 52
slug: job-runner-image-one-shot-entrypoint
status: secured
threats_total: 11
threats_closed: 11
threats_open: 0
register_authored_at_plan_time: true
asvs_level: 1
block_on: critical
created: 2026-06-27
---

# SECURITY — Phase 52: Job-runner image & one-shot entrypoint

**Audit date:** 2026-06-27
**Disposition basis:** threat register authored at plan time (`register_authored_at_plan_time: true`) — each declared mitigation verified against implemented code, not documentation.
**Result:** SECURED — 11/11 threats closed.
**Block-on:** critical → no blockers found.

## Threat Verification

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| T-52-01 | Spoofing/Info (TLS MITM on callback) | mitigate | CLOSED | `construct_agent_client(cfg)` at `src/phaze/job_runner.py:163` → `agent_bootstrap.py:65-70` passes `verify=cfg.agent_ca_file` (baked CA). `grep -Ec "verify\s*=\s*False" job_runner.py` = 0. CA-existence guard rejects empty/missing cert (`agent_bootstrap.py:61-64`). Test `test_ca_verify_threads_baked_ca` asserts `verify == ca_file` and `is not False`. |
| T-52-02 | Elevation (pod reaches Postgres) | mitigate | CLOSED | Import-boundary banner `job_runner.py:20-23`; subprocess test `test_job_runner_does_not_import_phaze_database` (`tests/test_task_split.py:356-396`) bans the exact tuple `("phaze.database","phaze.tasks.session","sqlalchemy.ext.asyncio")`. Deferred essentia import seam `_load_analyze_file` keeps load Postgres-free. |
| T-52-03 | Tampering (corrupt download analyzed) | mitigate | CLOSED | `compute_sha256` off-loop at `job_runner.py:190`; `actual_sha256 != expected_sha256` → `sys.exit(EXIT_INTEGRITY=11)` at `:191-193`, BEFORE analyze (`:198`). Temp unlinked in `finally` (`:229-230`). Test `integrity_mismatch → 11`. |
| T-52-03a | Tampering (integrity hash provenance) | mitigate | CLOSED | `PresignDownloadResponse.expected_sha256: str` required field with `model_config = ConfigDict(extra="forbid")` (`src/phaze/schemas/agent_analysis.py:121-124`). A response missing the hash fails validation rather than silently disabling verify. |
| T-52-04 | Info disclosure (bearer token leak) | mitigate | CLOSED | Token header-only via httpx default headers, never stored as attribute (`agent_client.py:124-126,156-161`). Download uses a FRESH bearer-less client `_download_to` (`job_runner.py:91-104`); happy-path test asserts `"authorization" not in` bucket request headers. Token-not-in-logs tests: `test_bearer_token_absent_from_warning_logs_on_500` + `test_request_download_url_token_absent_from_warning_logs_on_500`. |
| T-52-05 | Spoofing (presigned-URL replay after expiry) | accept (server deferred) | CLOSED | CLIENT portion verified: a FRESH presign is minted per pod via `client.request_download_url(file_id)` at `job_runner.py:170` (no URL caching/reuse). Server-side short-TTL minting + replay defense is the Phase 53 contract (KSTAGE-03). See Accepted Risks log below. |
| T-52-06 | Tampering/Supply chain (stale `:latest` base) | mitigate | CLOSED | `Dockerfile.job:17-18` `ARG BASE_IMAGE` + `FROM ${BASE_IMAGE}`; `grep -c ":latest" Dockerfile.job` = 0. Workflow job `build-job-runner` is `needs: build-and-push` (`docker-publish.yml:560`) and passes `BASE_IMAGE=${{ fromJSON(steps.base-meta.outputs.json).tags[0] }}` (`:652`). Guards `test_build_job_runner_needs_build_and_push`, `test_dockerfile_job_does_not_pin_latest_base`. |
| T-52-07 | DoS (multi-hour set OOM) | mitigate | CLOSED | Windowed path only: `grep -c "MonoLoader" job_runner.py` = 0; `analyze_file(..., fine_cap, coarse_cap)` at `:201`. Temp streamed in 64 KiB chunks (`_download_to`) to tempdir and unlinked in `finally` (`:229-230`). `AnalysisWritePayload.windows` capped `max_length=50000` (`agent_analysis.py:72`). |
| T-52-08 | Tampering (SIGTERM → false success) | accept/mitigate | CLOSED | No signal handler installed (`grep "import signal\|signal.signal" job_runner.py` = none). Default Python SIGTERM → 143 stays honestly non-zero; the single `sys.exit(0)` is reached only on full success (`:224`). See Accepted Risks log. |
| T-52-09 | Tampering (image drifts out of release lockstep) | accept | CLOSED | `build-job-runner` runs in the SAME workflow off the SAME annotated v-tag set (`docker-publish.yml:594-607`, identical tag block to api/parity jobs), gated `needs: build-and-push`. See Accepted Risks log. |
| T-52-SC | Tampering/Supply chain (new installs in image) | mitigate | CLOSED | `grep -Ec "pip install|uv add|uv pip install" Dockerfile.job` = 0; image adds only a CA `COPY` + `CMD`. All three plans declare `tech-stack.added: []`. Guard `test_dockerfile_job_zero_new_deps_and_targets_job_runner`. |

## Accepted Risks Log

- **T-52-05 — Presigned-URL replay after expiry (server-side):** Accepted/deferred to Phase 53 (KSTAGE-03). Phase 52 owns only the client, which mints a fresh presign per pod start and never caches or reuses a URL. Short-TTL minting and replay rejection are the control-plane presign-endpoint server contract, which does not exist in this phase. Client-side fresh-presign behavior is verified in code (`job_runner.py:170`). Residual risk bounded by the future server TTL.
- **T-52-08 — SIGTERM trapped into a false success exit:** Accepted. The runner intentionally installs no SIGTERM trap; Python's default SIGTERM → 143 is a truthful non-zero exit, and the only `sys.exit(0)` is on the full-success terminal path. Eviction re-drive is owned by the control plane (KSUBMIT-05). Verified: no `signal` import/handler in `job_runner.py`.
- **T-52-09 — Job image drifts out of release lockstep:** Accepted. The Job image is built in the same `docker-publish.yml` workflow, off the same annotated v-tag set, in a `needs: build-and-push`-gated job, so it moves in lockstep with the api image (D-04). Verified in workflow YAML.

## Unregistered Flags

None. No summary declares a `## Threat Flags` section, and all three plans declare `tech-stack.added: []` (zero new dependencies / no new attack surface). Every threat in the register maps to implemented code.

## Adjacent Finding — WR-01 (non-security, informational)

A prior code review flagged that `_build_payload(result)` is evaluated INSIDE the callback `try` block (`job_runner.py:217`), so an analysis-output error (e.g. a malformed `result["windows"]` entry failing `AnalysisWindowPayload(**w)`, or a mood/style conversion error) would be caught by the callback handler and coded `EXIT_CALLBACK=13` instead of `EXIT_ANALYSIS=12`.

**Assessment: diagnostics/correctness, NOT a security threat.** It does not breach any declared mitigation:
- The security property of T-52-08 ("a failure is never mistaken for success") HOLDS — a payload-build error still exits NON-ZERO (13), so Kueue/control-plane reads it as a failure, never as success. No false-success path exists.
- T-52-03 integrity is unaffected: the sha256 gate runs and exits 11 before `_build_payload` is ever reached.
- The only harm is exit-code mis-attribution (analysis-output error labelled "callback" rather than "analysis"), which affects observability and any exit-code-keyed retry routing. Control-plane re-drive (KSUBMIT-05) treats ALL non-zero exits as failure, so the mis-attribution does not change the re-drive decision.

Recommendation (correctness, out of audit scope — implementation is read-only here): hoist `payload = _build_payload(result)` above the callback `try` and fold its failure into the analysis branch (`EXIT_ANALYSIS`). Tracked as a diagnostics-accuracy improvement, not a security blocker for shipping Phase 52.
