# Phase 52: Job-runner image & one-shot entrypoint - Pattern Map

**Mapped:** 2026-06-27
**Files analyzed:** 7 (5 new, 2 modified)
**Analogs found:** 7 / 7

All analogs below were read from live source and excerpts verified (the RESEARCH.md
citations were re-checked; a few line numbers had drifted and are corrected here).

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/job_runner.py` (NEW) | entrypoint / orchestrator | request-response (one-shot batch) | `src/phaze/tasks/functions.py::process_file` + `src/phaze/entrypoint.py` | exact (process_file = flow; entrypoint = process model) |
| `src/phaze/services/analysis_wire.py` (OPTIONAL NEW) | utility | transform | `src/phaze/tasks/functions.py::_features_to_mood_dict/_features_to_style_dict` | exact (relocate verbatim) |
| `Dockerfile.job` (NEW) | config / image | n/a | `Dockerfile` (api base) + `Dockerfile.agent-arm64` (layout only) | role-match (api FROM; arm64 = CA/user concept) |
| `.github/workflows/docker-publish.yml` (MODIFIED) | config / CI | n/a | `parity-golden-x86` job (`needs: build-and-push`) | exact (needs-gated job precedent) |
| `tests/test_job_runner.py` (NEW) | test | request-response | `tests/test_services/test_agent_client.py` (respx) + `process_file` tests | role-match |
| `tests/test_task_split.py` (MODIFIED) | test | n/a | `test_agent_worker_does_not_import_phaze_database` | exact (clone the case) |
| `PhazeAgentClient.request_download_url` (MODIFIED method) | client method | request-response | `PhazeAgentClient.put_analysis` / `report_pushed` | exact (one-method-per-endpoint) |

## Pattern Assignments

### `src/phaze/job_runner.py` (entrypoint, one-shot request-response)

This is the single genuinely-new module. It fuses two analogs: the **flow** comes from
`process_file`, the **process model** (PID-1, import-boundary, `__main__` guard) comes
from `entrypoint.py`. The critical divergence (D-01): `process_file` reports failure via
callback then `return`s a dict so SAQ marks the job complete; the one-shot must instead
`sys.exit(<distinct code>)`.

**Analog A — flow + helpers:** `src/phaze/tasks/functions.py` (`process_file`, lines 146-297)

**Core analyze→convert→PUT pattern** (functions.py:222-272) — mirror this, but call
`analyze_file` directly (no pebble pool — RESEARCH Alternatives, A4) and map exceptions to
exit codes instead of `report_analysis_failed`+return:
```python
analysis = await run_in_process_pool(ctx, _load_analyze_file(), read_path, payload.models_path,
                                     timeout=cfg.analysis_inner_timeout_sec, fine_cap=fine_cap, coarse_cap=coarse_cap)
# one-shot: result = analyze_file(read_path, models_dir, fine_cap=..., coarse_cap=...)
features = analysis.get("features", {}) if isinstance(analysis, dict) else {}
mood_dict = _features_to_mood_dict(features)
style_dict = _features_to_style_dict(features)
windows = [AnalysisWindowPayload(**w) for w in analysis.get("windows", [])]
await api.put_analysis(payload.file_id, AnalysisWritePayload(
    bpm=analysis.get("bpm"), musical_key=analysis.get("musical_key"),
    mood=mood_dict, style=style_dict, danceability=analysis.get("danceability"),
    energy=analysis.get("energy"),
    fine_windows_analyzed=analysis.get("fine_windows_analyzed"), fine_windows_total=analysis.get("fine_windows_total"),
    coarse_windows_analyzed=analysis.get("coarse_windows_analyzed"), coarse_windows_total=analysis.get("coarse_windows_total"),
    sampled=analysis.get("sampled"), windows=windows))
```

**sha256 verify off the event loop** (functions.py:185-211) — the integrity gate, but a
mismatch maps to `sys.exit(EXIT_INTEGRITY)` instead of `report_push_mismatch`+return:
```python
actual_sha256 = await asyncio.to_thread(compute_sha256, Path(payload.scratch_path))
if actual_sha256 != payload.expected_sha256:
    Path(payload.scratch_path).unlink(missing_ok=True)
    ...  # one-shot: sys.exit(EXIT_INTEGRITY)
```

**Temp-file cleanup `finally`** (functions.py:290-297) — the one-shot downloads to `/tmp`,
so reuse the unlink-on-exit discipline (V12; RESEARCH Security Domain):
```python
finally:
    if payload.scratch_path and cleanup_scratch:
        Path(payload.scratch_path).unlink(missing_ok=True)
```

**Module-level import-boundary docstring** (functions.py:7-8) — copy this banner so the
new module advertises the Postgres-free invariant:
```python
# This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
# Enforced by tests/test_task_split.py.
```

**Reusable helpers to call (do NOT reimplement):**
- `_features_to_mood_dict` / `_features_to_style_dict` — functions.py:94, :119 (relocate to `analysis_wire.py`, see next section, so both `functions.py` and `job_runner.py` import them)
- `compute_sha256(Path)` — `src/phaze/services/hashing.py:10` (chunked 64 KB; never whole-file)
- `analyze_file(path, models_dir, *, fine_cap, coarse_cap, ...)` — `src/phaze/services/analysis.py:520` (streaming windowed; returns the exact dict shape consumed above)

**Analog B — process model:** `src/phaze/entrypoint.py` (whole file, 77 lines)

**`__main__` guard + clean `main()`** (entrypoint.py:33, :76-77) — but the one-shot does
NOT `os.execvp`; it runs the flow and `sys.exit(code)` (RESEARCH Stack note):
```python
def main() -> None:
    ...
if __name__ == "__main__":  # pragma: no cover  # CLI invocation guard
    main()
```

**Import-boundary banner** (entrypoint.py:18-22) — adapt to the job_runner ban list:
```python
# IMPORT-BOUNDARY INVARIANT: MUST NOT import phaze.database, phaze.tasks.session,
# sqlalchemy.ext.asyncio. Verified by tests/test_task_split.py.
```

**Logging — call once, first** (D-03): `configure_logging()` from `src/phaze/logging_config.py:71`
is keyword-only + env-fallback + import-safe; call bare at the top of `main()`. It renders
JSON when stdout is not a TTY (the pod case) — logging_config.py:56-68.

**Signal/SIGTERM (Pitfall 6):** run as clean PID-1 (no shell wrapper); do NOT trap SIGTERM
into exit 0 — default Python SIGTERM→143 is correctly "not success".

**Exit-code mapping (D-01, the new surface):**
```python
EXIT_OK = 0; EXIT_DOWNLOAD = 10; EXIT_INTEGRITY = 11; EXIT_ANALYSIS = 12; EXIT_CALLBACK = 13
```

---

### `src/phaze/services/analysis_wire.py` (utility, transform) — OPTIONAL NEW

**Analog:** `src/phaze/tasks/functions.py:81-143` (`_MOOD_SET_NAMES`, `_features_to_mood_dict`,
`_features_to_style_dict`). Move these three verbatim into a shared module so both the SAQ
`process_file` and the new `job_runner` import them (RESEARCH "Don't Hand-Roll"). They are
already correct + tested — relocation only. Keep the functions Postgres-free (stdlib + typing
only) so neither import path violates the boundary.

---

### `src/phaze/services/agent_client.py` — ADD `request_download_url` method

**Analog:** the existing one-method-per-endpoint pattern, esp. `put_analysis` (agent_client.py:271-280)
and `report_analysis_failed` (:282-297). The new method routes through the same `_request`
funnel (free tenacity retry + 4xx-no-retry + exception taxonomy):
```python
async def put_analysis(self, file_id: uuid.UUID, payload: AnalysisWritePayload) -> AnalysisWriteResponse:
    from phaze.schemas.agent_analysis import AnalysisWriteResponse  # noqa: PLC0415
    response = await self._request("PUT", f"/api/internal/agent/analysis/{file_id}",
                                   json=payload.model_dump(mode="json", exclude_unset=True))
    return AnalysisWriteResponse.model_validate(response.json())
```

**New method shape** (RESEARCH Q3 — server side is Phase 53; mock here): a minimal
`request_download_url(file_id) -> (url, expected_sha256)`, lazily importing its response
schema, hitting `POST /api/internal/agent/.../presign` with the same bearer. The presign
response MUST carry `expected_sha256` (Pitfall 3 — pod has no DB; mirrors v5.0
`ProcessFilePayload.expected_sha256` pinned from `FileRecord.sha256_hash`).

**Note on retry granularity (D-02):** `_request` already wraps EVERY call in
`AsyncRetrying(stop_after_attempt(3), wait_exponential_jitter(initial=0.5, max=4.0))`
retrying 5xx+transport, never 4xx (agent_client.py:186-216). So `put_analysis` gets bounded
callback retries "for free." The presign/download/verify/analyze steps must fail-fast — do
NOT add an extra retry loop around `analyze_file`.

---

### `Dockerfile.job` (config / image) — NEW

**Primary analog:** `Dockerfile` (the api base, 48 lines). The Job image is `FROM` the
published api image — it already carries Python 3.14 + essentia + every native lib + the
`phaze` package + uv. Reuse its `uv run` CMD form (Dockerfile:48):
```dockerfile
CMD ["uv", "run", "uvicorn", "phaze.main:app", "--host", "0.0.0.0", "--port", "8000"]
# job image: CMD ["uv", "run", "python", "-m", "phaze.job_runner"]
```

**Layout-only analog:** `Dockerfile.agent-arm64`. Borrow ONLY these two concepts:
- non-root `uid/gid 1000` user (Dockerfile.agent-arm64:170-171 / Dockerfile:44-45)
- the CA-trust idea — but **baked, not mounted**. The cloud-agent mounts `${CA_PATH}:/certs:ro`
  (docker-compose.cloud-agent.yml:60); Phase 52 instead `COPY phaze-ca.crt /etc/phaze/phaze-ca.crt`
  and sets `PHAZE_AGENT_CA_FILE` to that baked path (KJOB-05).

**What does NOT transfer from arm64** (RESEARCH "State of the Art"): the entire source-build
chain (git clone essentia, `waf`, the 4 TF/OpenMP fixes, `--system`/`--ignore-requires-python`
bare-pip, and the bare `python3 -m` CMD at line 180). Phase 52 is x86 — the wheel is already
installed; use `uv run` (Pitfall 5: verify `uv run` propagates the child exit code unchanged).

**Guard (RESEARCH Package Legitimacy):** `Dockerfile.job` must contain NO `pip install` /
`uv add` / `uv pip install` line (zero new deps, KJOB-01).

**Models (Pitfall 2 / D-05):** the api image does NOT bake the 34 essentia `.pb` models.
Phase 52 entrypoint reads `models_dir` from env (`PHAZE_MODELS_DIR`, default `/models` —
mirrors `AgentSettings.models_path = "/models"`, config.py:227) and passes it to
`analyze_file`. Actual cluster provisioning (RO PVC) is Phase 54 — do NOT bake models here.

---

### `.github/workflows/docker-publish.yml` (config / CI) — MODIFIED

**Analog:** the `parity-golden-x86` job (docker-publish.yml:314-405), which already uses
`needs: build-and-push` to depend on the freshly-pushed api image. This is the exact
build-ordering fix for Pitfall 1 (the Job image `FROM` the api tag cannot be a sibling matrix
row — matrix entries have no ordering).

**Pattern to clone** (docker-publish.yml:314-318):
```yaml
parity-golden-x86:
  runs-on: ubuntu-latest
  timeout-minutes: 30
  needs: build-and-push          # <-- gates on the api image being pushed first
```
Add a new `build-job-runner` job with `needs: build-and-push`, resolve the api tag via the
same `docker/metadata-action` bare-repo URL convention (Phase 29 D-15: `image_suffix: ""`,
docker-publish.yml:31-35, :104), pass the resolved tag as `ARG BASE_IMAGE`, and push the Job
image tags off the same annotated `v`-tag (D-04). Reuse the metadata tag block at
docker-publish.yml:108-115 and the login/buildx/build-push step shapes (:80-145).

---

### `tests/test_task_split.py` — ADD `test_job_runner_does_not_import_phaze_database`

**Analog:** `test_agent_worker_does_not_import_phaze_database` (test_task_split.py:33-106) and
the simpler `test_cert_bootstrap_stays_postgres_free` (:233-267) — the latter is the cleaner
template (no SAQ-broker positive assertion needed). RESEARCH calls this "the single
highest-leverage test in the phase."

**Subprocess skeleton to clone** (test_task_split.py:247-267):
```python
script = textwrap.dedent("""
    import sys
    import phaze.job_runner  # noqa: F401
    forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")
    present = [m for m in forbidden if m in sys.modules]
    if present:
        for m in present:
            sys.stderr.write(f"BANNED MODULE IMPORTED: {m}\\n")
        sys.exit(1)
    sys.exit(0)
""")
result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20, check=False)  # noqa: S603
assert result.returncode == 0, f"job_runner import contaminated sys.modules:\n{result.stderr}"
```
Set the minimal env (`PHAZE_ROLE=agent`, `PHAZE_AGENT_API_URL`, `PHAZE_AGENT_TOKEN`,
`PHAZE_AGENT_SCAN_ROOTS=/tmp`, `PHAZE_REDIS_URL`) per the agent_worker case (:71-77) if
`job_runner` calls `get_settings()` at import.

---

### `tests/test_job_runner.py` (test, request-response) — NEW

**Analogs:** respx-based HTTP mocking (the existing `tests/test_services/test_agent_client.py`
suite referenced in agent_client.py:101) + the `process_file` exit/outcome tests.

**Coverage matrix** (RESEARCH Test Map):
- happy path: respx fake control plane (presign + PUT) + fixture audio → exit 0 (KJOB-02)
- exit-code matrix: parametrized presign-fail→10, sha-mismatch→11, analyze-raise→12, PUT-fail→13 (KJOB-04)
- `verify=<ca>` is threaded (assert `PhazeAgentClient(verify=...)` got the baked path); grep guard for `verify=False` (KJOB-05)
- no whole-file `MonoLoader` (AST/grep guard; KJOB-03)
- Fixture: reuse `scripts/parity/reference.wav` if present (RESEARCH Wave 0); a models-stub or fine-tier-only path keeps the unit test off the GB models.

## Shared Patterns

### Structured JSON logging (D-03)
**Source:** `src/phaze/logging_config.py:71` (`configure_logging`)
**Apply to:** `job_runner.main()` — first call, bare (env-driven).
```python
configure_logging()  # JSON when stdout is not a TTY (pod); import-safe (stdlib + structlog only)
```
Use `structlog.get_logger(__name__)` for per-step events carrying `file_id` + step outcome +
timing. NEVER log the bearer token (D-13; agent_client.py:124-130 keeps it header-only).

### HTTPS client with baked CA (KJOB-05)
**Source:** `src/phaze/tasks/_shared/agent_bootstrap.py:46-70` (`construct_agent_client`)
**Apply to:** `job_runner` client construction.
```python
ca_path = Path(cfg.agent_ca_file)
if not ca_path.exists() or ca_path.stat().st_size == 0:
    raise RuntimeError(f"CA file empty or unreadable: {cfg.agent_ca_file}")
return PhazeAgentClient(base_url=cfg.agent_api_url, token=cfg.agent_token.get_secret_value(),
                        timeout=30.0, verify=cfg.agent_ca_file)  # never verify=False
```
The one-shot can call `construct_agent_client` directly (it is already Postgres-free,
test-enforced at test_task_split.py:270) — only the CA path differs (baked vs mounted).

### `_FILE`-convention secrets (v4.0.1)
**Source:** `src/phaze/config.py:69-148` (`BaseSettings._resolve_secret_files` + `SECRET_FILE_FIELDS`)
**Apply to:** the pod's `PHAZE_AGENT_TOKEN` / control-plane URL / file_id arrive via env or
`<VAR>_FILE` siblings. `AgentSettings` already resolves `PHAZE_AGENT_TOKEN_FILE` (config.py:473)
and exposes `agent_api_url` (:481), `agent_token` (:485), `agent_ca_file` (:601),
`models_path` (:227). No new env-parsing — reuse `get_settings()` → `AgentSettings`.

### Callback wire schema
**Source:** `src/phaze/schemas/agent_analysis.py` — `AnalysisWritePayload` (:48), `AnalysisWindowPayload`
(:22), `AnalysisFailurePayload` (:82). All `extra="forbid"` with `ge`/`le` bounds and
`max_length=50000` on windows (V5 input validation). Build these exactly as `process_file`
does (functions.py:253-271). Endpoints (verified, agent_client.py:271-297):
```
PUT  /api/internal/agent/analysis/{file_id}          -> AnalysisWriteResponse
POST /api/internal/agent/analysis/{file_id}/failed   -> AnalysisFailureResponse
```

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| (none) | — | — | Every capability has a verified codebase analog. The only genuinely-new logic is the exit-code mapping in `job_runner.main()` and the presign-request shape — both are small and derived from `process_file` outcome handling + the existing one-method-per-endpoint client pattern. |

## Metadata

**Analog search scope:** `src/phaze/` (tasks, services, schemas, config, entrypoint, logging),
`tests/`, root Dockerfiles, `.github/workflows/`, `docker-compose.cloud-agent.yml`.
**Files scanned:** 12 source files read + verified.
**Pattern extraction date:** 2026-06-27
**Line-number corrections vs RESEARCH.md:** `analyze_file` confirmed at analysis.py:520;
`process_file` at functions.py:146; `configure_logging` at logging_config.py:71; client `verify`
threading at agent_client.py:132-161; `_request` retry funnel at :171-220 (RESEARCH said
171-216/220 — confirmed). All cited analogs exist as described.
