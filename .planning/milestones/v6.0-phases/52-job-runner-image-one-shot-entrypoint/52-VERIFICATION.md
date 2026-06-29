---
phase: 52-job-runner-image-one-shot-entrypoint
verified: 2026-06-27T00:00:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
---

# Phase 52: Job-Runner Image & One-Shot Entrypoint Verification Report

**Phase Goal:** An x86 Kueue Job-runner image exists on GHCR with a one-shot entrypoint that pulls a file, analyzes it (windowed), POSTs the result, and exits with an honest exit-code contract — the execution unit everything else depends on, built and tested independently of any live cluster or bucket.
**Verified:** 2026-06-27
**Status:** PASSED
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| #  | Truth | Status | Evidence |
|----|-------|--------|----------|
| 1  | KJOB-01: Image built FROM api base via ARG BASE_IMAGE, zero new pip deps, one-shot CMD | VERIFIED | `Dockerfile.job` exists: `FROM ${BASE_IMAGE}`, no `pip install`/`uv add`/`uv pip install` lines, `CMD ["uv", "run", "python", "-m", "phaze.job_runner"]`; 4 static deployment guard tests pass |
| 2  | KJOB-01: build-job-runner workflow job is `needs: build-and-push`-gated (not a sibling matrix row) | VERIFIED | Line 557-560 of `docker-publish.yml`: `build-job-runner:` job with `needs: build-and-push`; `test_build_job_runner_needs_build_and_push` passes |
| 3  | KJOB-02: Entrypoint runs presign → download → sha256-verify → windowed analyze → PUT → exit | VERIFIED | `src/phaze/job_runner.py` `run()` implements all 5 steps; `test_happy_path_exits_zero` passes (exit 0); `request_download_url` in `agent_client.py` routes through `_request` funnel; `PresignDownloadResponse.expected_sha256` required field |
| 4  | KJOB-02: Shared `analysis_wire.py` module holds Postgres-free converters, imported by both SAQ path and job_runner | VERIFIED | `src/phaze/services/analysis_wire.py` exists; boundary grep returns 0 for `phaze.database|phaze.models|sqlalchemy`; `functions.py` line 31: `from phaze.services.analysis_wire import _features_to_mood_dict, _features_to_style_dict`; `job_runner.py` imports from `phaze.services.analysis_wire` |
| 5  | KJOB-03: Analysis goes only through windowed `analyze_file`; no whole-file MonoLoader | VERIFIED | `grep -c "MonoLoader" job_runner.py` = 0; `grep -c "analyze_file" job_runner.py` = 10; `test_no_monoloader_source_guard` passes; `_load_analyze_file()` deferred import confirmed |
| 6  | KJOB-04: Distinct non-zero exit codes per failure class; analysis never exits 0 | VERIFIED | EXIT_OK=0, EXIT_DOWNLOAD=10, EXIT_INTEGRITY=11, EXIT_ANALYSIS=12, EXIT_CALLBACK=13 defined; `grep -c "sys.exit(0)"` = 1 (success only); exit-code matrix test: 5 parametrized scenarios all produce correct distinct non-zero codes |
| 7  | KJOB-05: Internal CA baked into image; no TLS bypass anywhere in entrypoint path | VERIFIED | `Dockerfile.job` COPYs `phaze-ca.crt` to `/etc/phaze/phaze-ca.crt`; `ENV PHAZE_AGENT_CA_FILE=/etc/phaze/phaze-ca.crt`; `grep -Ec "verify\s*=\s*False" job_runner.py` = 0; `construct_agent_client(cfg)` wired with `verify=cfg.agent_ca_file`; `test_ca_verify_threads_baked_ca` passes |

**Score:** 5/5 requirements verified (7 observable truths all VERIFIED)

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/job_runner.py` | One-shot orchestrator + exit-code mapping + `main()` | VERIFIED | 242 lines; contains `def main`, `async def run`, EXIT_OK/DOWNLOAD/INTEGRITY/ANALYSIS/CALLBACK constants; mypy clean; ruff clean |
| `src/phaze/services/analysis_wire.py` | Postgres-free mood/style converters shared between SAQ and job_runner | VERIFIED | 89 lines; contains `_features_to_mood_dict`, `_features_to_style_dict`, `_MOOD_SET_NAMES`; no database/ORM imports |
| `src/phaze/services/agent_client.py` | `request_download_url` client method hitting presign endpoint | VERIFIED | `async def request_download_url` at line 282; routes through `self._request`; returns `(download_url, expected_sha256)` tuple |
| `src/phaze/schemas/agent_analysis.py` | `PresignDownloadResponse` with `expected_sha256` | VERIFIED | `class PresignDownloadResponse` at line 105; `extra="forbid"`, `download_url: str`, `expected_sha256: str` (required) |
| `Dockerfile.job` | x86 Job-runner image: FROM api base, COPY CA, CMD uv run python -m phaze.job_runner | VERIFIED | 41 lines; `FROM ${BASE_IMAGE}`; no `:latest`; `COPY phaze-ca.crt /etc/phaze/phaze-ca.crt`; `CMD ["uv", "run", "python", "-m", "phaze.job_runner"]`; no new deps |
| `.github/workflows/docker-publish.yml` | build-job-runner needs-gated job emitting the Job image tag | VERIFIED | `build-job-runner` job at line 557; `needs: build-and-push` at line 560; `file: Dockerfile.job`; passes `BASE_IMAGE` build-arg via `fromJSON(steps.base-meta.outputs.json).tags[0]` |
| `tests/test_job_runner.py` | respx happy path, exit-code matrix, ca_verify, no_monoloader | VERIFIED | 192 lines; `grep -c "def test_"` = 4 (happy_path, exit_code matrix [5 params], ca_verify, no_monoloader); all 9 test cases pass |
| `tests/test_task_split.py` | subprocess import-boundary case for phaze.job_runner | VERIFIED | `test_job_runner_does_not_import_phaze_database` at line 356; subprocess confirms no `phaze.database`/`phaze.tasks.session`/`sqlalchemy.ext.asyncio` in sys.modules after import; passes |
| `tests/test_deployment/test_job_image.py` | static guards: no pip install in Dockerfile.job, needs-gated workflow job, CMD target | VERIFIED | 4 static guards (a-d) all pass: build-job-runner exists with needs; Dockerfile.job + BASE_IMAGE; zero deps + CMD; no :latest |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/phaze/job_runner.py` | `phaze.services.analysis.analyze_file` | `_load_analyze_file()` deferred import; windowed analysis with `models_dir` | WIRED | Lines 79-88 (deferred import seam), line 198-201 (call with fine_cap/coarse_cap) |
| `src/phaze/job_runner.py` | `PhazeAgentClient(verify=cfg.agent_ca_file)` | `construct_agent_client(cfg)` at line 163 | WIRED | `verify=False` grep = 0; CA path threaded through via AgentSettings |
| `src/phaze/job_runner.py` | `phaze.services.analysis_wire` | `from phaze.services.analysis_wire import _features_to_mood_dict, _features_to_style_dict` | WIRED | Line 50; used in `_build_payload` at lines 117-118 |
| `src/phaze/tasks/functions.py` | `phaze.services.analysis_wire` | `from phaze.services.analysis_wire import` | WIRED | Line 31; `_features_to_mood_dict` definition no longer in functions.py (grep count = 0) |
| `src/phaze/services/agent_client.py` | `self._request` retry funnel | `request_download_url` routes POST through shared tenacity funnel | WIRED | Line 302-307; no bespoke retry loop; 5xx retried, 4xx fail-fast inherited from `_request` |
| `Dockerfile.job` | `ghcr.io/<owner>/phaze` api image | `ARG BASE_IMAGE` / `FROM ${BASE_IMAGE}` | WIRED | Line 17-18; CI resolves freshly-pushed api tag via `fromJSON(steps.base-meta.outputs.json).tags[0]` |
| `build-job-runner` workflow job | `build-and-push` api push | `needs: build-and-push` gate | WIRED | Line 560 of `docker-publish.yml`; prevents race where Dockerfile.job FROM resolves before api push |

---

### Data-Flow Trace (Level 4)

Phase 52 delivers entrypoint logic and packaging — not data-rendering components — so Level 4 data-flow tracing is not applicable. The entrypoint is a process pipeline (presign → download → verify → analyze → POST), not a UI component rendering dynamic data from a store.

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Happy path: presign → download → verify → analyze → PUT exits 0 | `uv run pytest tests/test_job_runner.py -k happy_path --tb=short -q` | 1 passed | PASS |
| Exit-code matrix: all 5 failure classes map to correct distinct codes | `uv run pytest tests/test_job_runner.py -k exit_code --tb=short -q` | 5 passed (10/10/11/12/13) | PASS |
| TLS CA verify: client built with verify=<baked CA>, never False | `uv run pytest tests/test_job_runner.py -k ca_verify --tb=short -q` | 1 passed | PASS |
| Source guard: no MonoLoader, windowed analyze_file wired | `uv run pytest tests/test_job_runner.py -k no_monoloader --tb=short -q` | 1 passed | PASS |
| Import boundary: no phaze.database/sqlalchemy.ext.asyncio after import | `uv run pytest tests/test_task_split.py -k job_runner --tb=short -q` | 1 passed | PASS |
| Static deployment guards: Dockerfile.job + workflow structure | `uv run pytest tests/test_deployment/ -k job --tb=short -q` | 5 passed | PASS |
| Presign client: happy-path tuple, 4xx no-retry, 5xx retry, token-safe | `uv run pytest tests/test_services/test_agent_client.py -k "presign or download_url" --tb=short -q` | 5 passed | PASS |

---

### Probe Execution

No probe scripts declared or conventionally located (`scripts/*/tests/probe-*.sh`) for this phase. Phase 52 is verified through the unit/static-guard test suite above. Phase 52 explicitly scopes to being "built and tested independently of any live cluster or bucket" — no probe execution required or expected.

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| KJOB-01 | Plan 03 | Job-runner image FROM api base, zero new deps, one-shot CMD, published to GHCR | SATISFIED | Dockerfile.job + build-job-runner workflow job + 4 static guards all verified |
| KJOB-02 | Plans 01, 02 | One-shot: presign → download → sha256-verify → analyze → POST result → exit | SATISFIED | job_runner.py run(), request_download_url, PresignDownloadResponse, analysis_wire converters all wired; happy-path test passes |
| KJOB-03 | Plan 02 | Windowed/streaming analyze_file; no whole-file MonoLoader; memory safe | SATISFIED | MonoLoader grep = 0; analyze_file count = 10; no_monoloader source guard passes |
| KJOB-04 | Plan 02 | Honest exit-code contract: distinct non-zero per failure class; never 0 on failure | SATISFIED | EXIT_DOWNLOAD=10, EXIT_INTEGRITY=11, EXIT_ANALYSIS=12, EXIT_CALLBACK=13; single sys.exit(0); exit-code matrix test passes all 5 scenarios |
| KJOB-05 | Plans 02, 03 | Internal CA baked into image; no verify=False anywhere in entrypoint path | SATISFIED | Dockerfile.job COPYs phaze-ca.crt; ENV PHAZE_AGENT_CA_FILE set; verify=False grep = 0; ca_verify test passes |

No orphaned requirements. REQUIREMENTS.md maps KJOB-01 through KJOB-05 exclusively to Phase 52 — all 5 are accounted for.

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | None | — | — |

Scan performed on: `src/phaze/job_runner.py`, `src/phaze/services/analysis_wire.py`, `src/phaze/services/agent_client.py`, `src/phaze/schemas/agent_analysis.py`, `Dockerfile.job`, `tests/test_job_runner.py`, `tests/test_deployment/test_job_image.py`.

No TBD/FIXME/XXX/TODO/HACK/PLACEHOLDER markers found. No empty return stubs. No verify=False. No MonoLoader. No hardcoded empty data flowing to rendering.

The `# pragma: no cover` annotations on `job_runner.py` lines 86-88 (real deferred-essentia import body) and lines 236-237 (`__main__` guard) are legitimate — they are intentionally excluded from the test-coverage gate and are not stubs (the deferred import is exercised in production; the `__main__` guard is a standard CLI pattern).

---

### Human Verification Required

None. All must-haves for Phase 52 are verifiable programmatically:

- The image contract is proven by static file guards (not a live registry push) — this is the stated verification scope for this phase.
- The entrypoint flow is proven by respx unit tests against a fake control plane.
- The CA trust is proven by a source grep + test asserting `verify=<path>` (not False).
- The import boundary is proven by a subprocess isolation test.

No visual rendering, no live cluster, no external service integration is under test in Phase 52 — all deferred correctly to Phases 53-56.

---

### Coverage Summary

| Module | Coverage | Gate |
|--------|----------|------|
| `src/phaze/job_runner.py` | 90.91% | PASS (≥85%) |
| `src/phaze/schemas/agent_analysis.py` | 100% | PASS |
| `src/phaze/services/analysis_wire.py` | 100% | PASS |
| Combined (phase-52 new modules) | 94.57% | PASS |

Uncovered lines in `job_runner.py`: lines 86-88 (`# pragma: no cover`, real essentia import body behind deferred-import seam — not testable without the platform-gated wheel), lines 149-150/153-155 (defensive config-guard branches for `cfg not AgentSettings` / invalid UUID — pod always runs `PHAZE_ROLE=agent`), lines 236-237 (`__main__` guard, standard CLI pattern).

---

## Gaps Summary

None. All 5 KJOB requirements are satisfied by substantive, wired, tested implementations. The phase goal is achieved.

---

_Verified: 2026-06-27_
_Verifier: Claude (gsd-verifier)_
