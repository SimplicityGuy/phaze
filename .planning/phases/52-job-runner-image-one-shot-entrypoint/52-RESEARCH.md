# Phase 52: Job-runner image & one-shot entrypoint - Research

**Researched:** 2026-06-27
**Domain:** Container image build + a single-shot pod entrypoint reusing the existing x86 essentia stack; HTTP-only agent boundary; honest exit-code process model
**Confidence:** HIGH (this is almost entirely codebase-grounded; every claim below is `[VERIFIED: repo]` from reading the named source file)

## Summary

Phase 52 is a **grounding-and-assembly** phase, not a greenfield one. Every capability the
one-shot entrypoint needs already exists in the v4.0/v5.0 code and is reused verbatim:
structured logging (`phaze.logging_config.configure_logging`), the HTTP-only callback client
(`PhazeAgentClient`), streaming windowed analysis (`phaze.services.analysis.analyze_file` — already
no-whole-file-decode), chunked sha256 (`phaze.services.hashing.compute_sha256`), the
analysis wire schema (`AnalysisWritePayload` / `AnalysisFailurePayload`), the `_FILE`-secret
config machinery, the internal-CA HTTPS trust pattern (`verify=<ca_file>`), and the
pre-uvicorn entrypoint precedent (`phaze.entrypoint`). The image FROM-target is the existing
published x86 api image (`ghcr.io/simplicityguy/phaze:<tag>`), which already carries Python
3.14 + essentia-tensorflow + every native lib + the `phaze` package + `uv`.

The genuinely **new** code is small and well-bounded: (1) a new entrypoint module that
orchestrates `presign → download → verify → analyze → callback → exit` as a *fire-once*
process (the v5.0 analogue `process_file` is a SAQ task that returns dicts and reports
failures via callback then exits 0 so SAQ marks the job complete — the one-shot must instead
**translate each outcome to a distinct process exit code** per D-01); (2) a new `Dockerfile.job`
that bakes the operator-provided internal CA cert and sets the new CMD; (3) a new matrix entry
in `docker-publish.yml`. Two coordination seams with later phases must be defined here but are
*mocked* for Phase 52's unit tests: the presign-request request/response contract (Phase 53
implements the server side) and the fact that the presign response must carry the expected
sha256 (sourced from `FileRecord.sha256_hash`, exactly as v5.0's push pipeline pins
`expected_sha256` into the `process_file` payload).

**Primary recommendation:** Build `Dockerfile.job` `FROM ghcr.io/simplicityguy/phaze:<tag>`
(the api image == the x86 essentia base), `COPY` the operator-provided `phaze-ca.crt` into the
image, and `CMD ["uv","run","python","-m","phaze.job_runner"]`. The new `phaze.job_runner`
module reuses `configure_logging`, `PhazeAgentClient(verify=<baked-ca-path>)`, `compute_sha256`,
and `analyze_file`, wraps each step in its own try/except that maps to a distinct exit code
(0/10/11/12/13), and retries **only** the final callback POST (D-02). Resolve the two CI/runtime
landmines flagged below (image-build ordering vs. the api image; essentia **models** provisioning
in the pod) before writing plans.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Request fresh presigned GET URL | Job pod (one-shot) | Control plane (Phase 53 mints it) | KSTAGE-03: minted just-in-time at pod start, post-admission |
| Download file bytes | Job pod | Object storage (presigned GET) | Pod is S3-credential-free; pulls over plain HTTPS GET |
| sha256 integrity verify | Job pod | Control plane (supplies expected hash from `FileRecord`) | Pod has no DB; expected hash arrives in the presign response |
| Windowed essentia analysis | Job pod (CPU) | — | CPU-bound; `analyze_file` streams windows, never whole-file decode |
| Persist analysis result | Control plane API | Job pod (POSTs it) | Reconciled by `file_id` via `/api/internal/agent/analysis/{file_id}` |
| Exit-code contract | Job pod (process) | Kueue/Workload (reads pod status) | Unattended Job; per-class code legible from `kubectl get pods` |
| TLS trust for callback | Job image (baked CA) | — | KJOB-05: CA baked in image, no `verify=False` |
| Models (.pb weights) provisioning | **UNRESOLVED — see Open Questions Q1** | — | Pod has no compose volume; coarse pass needs the 34 TF models |

## Standard Stack

**Zero new pip dependencies (KJOB-01, locked).** Everything is already in `pyproject.toml` /
`uv.lock` and present in the api image. No installation step, no Package Legitimacy Audit beyond
the confirmation below.

### Core (all already present in `ghcr.io/simplicityguy/phaze:<tag>`)
| Component | Source in repo | Purpose | Notes |
|-----------|----------------|---------|-------|
| `configure_logging()` | `src/phaze/logging_config.py:71` | Structured JSON to stdout (D-03) | Postgres/`phaze.config`-import-free; JSON when stdout not a TTY (pod case). Call once at entrypoint start. `[VERIFIED: repo]` |
| `PhazeAgentClient` | `src/phaze/services/agent_client.py:118` | HTTPS callback client w/ bearer auth + tenacity retry | `verify` param threads to `httpx.AsyncClient(verify=...)`; accepts a CA file path. `[VERIFIED: repo]` |
| `compute_sha256(Path)` | `src/phaze/services/hashing.py:10` | Chunked (64 KB) sha256 — never loads whole file to memory | Run via `asyncio.to_thread` off the event loop (the `process_file` pattern). `[VERIFIED: repo]` |
| `analyze_file(path, models_dir, *, fine_cap, coarse_cap, ...)` | `src/phaze/services/analysis.py:520` | Two-tier windowed analysis; returns aggregates + `windows` list + 5-field coverage | **Streaming-only** — see KJOB-03 finding below. `[VERIFIED: repo]` |
| `AnalysisWritePayload` / `AnalysisWindowPayload` / `AnalysisFailurePayload` | `src/phaze/schemas/agent_analysis.py` | Callback POST/PUT bodies | Identical wire contract the file-server/compute agents already use. `[VERIFIED: repo]` |
| `BaseSettings` `_FILE`-secret machinery | `src/phaze/config.py:69-145` | `<VAR>_FILE` file-mounted secrets for the Postgres-less pod | `SECRET_FILE_FIELDS` resolver runs `mode="before"`; strips trailing newline. `[VERIFIED: repo]` |
| `uv` runtime launcher | api `Dockerfile:24,48` | `uv run python -m ...` | api image is Python **3.14**, uv-managed venv — use `uv run` (NOT the arm64 image's bare `python3 -m`). `[VERIFIED: repo]` |

### Supporting / reference
| Component | Source | Use |
|-----------|--------|-----|
| `phaze.entrypoint` | `src/phaze/entrypoint.py` | Precedent for a clean container entrypoint (bootstrap → `os.execvp`). The one-shot does NOT execvp uvicorn; it runs the flow then `sys.exit(code)`. Borrow only the import-boundary discipline + PID-1 signal cleanliness. `[VERIFIED: repo]` |
| `process_file` task | `src/phaze/tasks/functions.py:146` | The v5.0 single-file analyze+callback flow to factor from — see "Don't Hand-Roll" + the factoring note below. `[VERIFIED: repo]` |
| `_features_to_mood_dict` / `_features_to_style_dict` | `src/phaze/tasks/functions.py:94,119` | Convert `analyze_file` string outputs → the `dict[str,float]` wire format `AnalysisWritePayload` requires. **Reuse verbatim** (move to a shared helper). `[VERIFIED: repo]` |
| `construct_agent_client(cfg)` | `src/phaze/tasks/_shared/agent_bootstrap.py:46` | Pattern for building the client with `verify=cfg.agent_ca_file` + fail-fast on empty CA. The one-shot needs the same CA-existence guard. `[VERIFIED: repo]` |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `FROM ghcr.io/simplicityguy/phaze:<tag>` (api image) | A fresh `FROM python:3.14-slim` repeating the api `base` apt+uv+deps layers | Duplicates the whole base recipe and drifts; only justified if the CI build-ordering landmine (Q2) can't be solved. Reusing the api image is cleaner and honors "FROM the existing essentia base." |
| New `phaze.job_runner` module orchestrating the flow inline | Reuse the SAQ `process_file` task directly | `process_file` is `ctx`-bound, returns dicts, and *never raises to a non-zero exit* (it reports failures via callback so SAQ marks the job complete). The one-shot's exit-code contract (D-01) is structurally incompatible — a thin new module that *calls shared helpers* is correct. |
| Run `analyze_file` directly in the entrypoint process | Run it in a pebble subprocess (`run_in_process_pool`) | Direct is simpler and a segfault/OOM naturally exits the pod non-zero (Kueue/pod-limit handles the kill). The pebble pool exists for SAQ slot-reclaim, which a fire-once pod doesn't need. Planner's discretion; direct is recommended. |

**Installation:** none — zero new deps (KJOB-01). The image is assembled, not pip-installed.

## Package Legitimacy Audit

**No external packages are installed by this phase.** KJOB-01 mandates **zero new pip
dependencies**; the Job image is `FROM` an already-built phaze image and adds only a `COPY` of
the CA cert and a new `CMD`. slopcheck/registry verification is therefore N/A. The planner must
add a guard that the new `Dockerfile.job` contains **no** `pip install` / `uv add` / `uv pip
install` line (mirror the spirit of the import-boundary tests).

## Architecture Patterns

### System Architecture Diagram

```
  ┌─────────────────────────── Kueue Job Pod (x86, one-shot) ───────────────────────────┐
  │                                                                                      │
  │  [env / _FILE secrets]                                                               │
  │   PHAZE_AGENT_API_URL, PHAZE_AGENT_TOKEN(_FILE),                                     │
  │   PHAZE_JOB_FILE_ID, PHAZE_AGENT_CA_FILE(=baked path), presign endpoint              │
  │        │                                                                             │
  │        ▼                                                                             │
  │  configure_logging()  ── structured JSON → stdout (kubectl logs)                     │
  │        │                                                                             │
  │        ▼                                                                             │
  │  (1) POST request-presign(file_id) ──HTTPS(verify=baked CA)──►  Control plane API    │
  │        │  ◄── { presigned_get_url, expected_sha256 }  (Phase 53 server side)         │
  │        │            fail → exit 10                                                   │
  │        ▼                                                                             │
  │  (2) GET presigned_url ──────────────►  S3-compatible object store                   │
  │        │   stream to /tmp/<file_id>.<ext>   fail → exit 10                           │
  │        ▼                                                                             │
  │  (3) compute_sha256(tmp) == expected_sha256 ?   mismatch → exit 11                   │
  │        ▼                                                                             │
  │  (4) analyze_file(tmp, /models, fine_cap, coarse_cap)   ── windowed/streaming        │
  │        │            crash/OOM/timeout → exit 12                                      │
  │        ▼                                                                             │
  │  (5) PUT /api/internal/agent/analysis/{file_id}  (AnalysisWritePayload)              │
  │        │   ──HTTPS(verify=baked CA)──►  Control plane API  (reconcile by file_id)    │
  │        │   bounded retry (~3× expo backoff, D-02); exhausted → exit 13               │
  │        ▼                                                                             │
  │  (6) sys.exit(0)                                                                     │
  └──────────────────────────────────────────────────────────────────────────────────────┘
```

File-to-implementation mapping (the diagram shows data flow; this table maps to code):

| Stage | Implementation | Reuse vs. new |
|-------|----------------|---------------|
| Logging | `configure_logging()` | reuse |
| Presign request | new client method on `PhazeAgentClient` (or a thin httpx call) | **new** (server side = Phase 53) |
| Download | `httpx` streaming GET → write to `/tmp` | new (small) |
| Verify | `compute_sha256` + comparison | reuse + new compare |
| Analyze | `analyze_file` + `_features_to_*_dict` | reuse |
| Callback | `PhazeAgentClient.put_analysis` | reuse |
| Exit mapping | new `phaze.job_runner.main()` | **new** |

### Recommended Project Structure
```
src/phaze/
├── job_runner.py          # NEW: one-shot orchestrator + exit-code mapping + main()
├── services/
│   └── analysis_wire.py   # OPTIONAL NEW: move _features_to_mood_dict/_features_to_style_dict
│                          #   here so both functions.py (SAQ) and job_runner.py share them
Dockerfile.job             # NEW: FROM phaze:<tag>; COPY CA; CMD uv run python -m phaze.job_runner
tests/
└── test_job_runner.py     # NEW: respx fake control plane + fixture audio; exit-code matrix
```
(Module names are Claude's Discretion per CONTEXT.md — `phaze.job_runner` is a recommendation.)

### Pattern 1: Distinct exit code per failure class (D-01, KJOB-04)
**What:** Each pipeline step is wrapped so its failure raises/returns a *typed* outcome that
`main()` maps to a fixed integer. The success path is the only `sys.exit(0)`.
**When to use:** Always — this is the core of the phase.
**Example (shape, not literal — exact integers are Claude's Discretion):**
```python
# Source: derived from process_file outcome handling (functions.py) + D-01 mapping
EXIT_OK = 0
EXIT_DOWNLOAD = 10     # presign request OR download failure
EXIT_INTEGRITY = 11    # sha256 mismatch
EXIT_ANALYSIS = 12     # analyze_file crash / OOM / inner timeout
EXIT_CALLBACK = 13     # PUT analysis failed after bounded retries

def main() -> None:
    configure_logging(json_logs=cfg.log_json)  # one call, first
    try:
        url, expected = request_presign(...)            # except -> sys.exit(EXIT_DOWNLOAD)
        path = download(url)                            # except -> sys.exit(EXIT_DOWNLOAD)
        verify(path, expected)                          # mismatch -> sys.exit(EXIT_INTEGRITY)
        result = analyze_file(path, models_dir, ...)    # except -> sys.exit(EXIT_ANALYSIS)
        put_analysis_with_retry(file_id, result)        # exhausted -> sys.exit(EXIT_CALLBACK)
    ...
    sys.exit(EXIT_OK)
```
**Contrast with `process_file`:** that task calls `report_analysis_failed(...)` then `return
{... "status": "analysis_failed"}` so SAQ marks the job COMPLETE (functions.py:236-241). The
one-shot must NOT do that — the *process exit code* is the signal Kueue/Workload reads. KJOB-04:
never exit 0 on a failed analysis.

### Pattern 2: Retry the callback only (D-02)
`PhazeAgentClient._request` already wraps every call in `AsyncRetrying(stop_after_attempt(3),
wait_exponential_jitter(initial=0.5, max=4.0))` retrying 5xx + transport errors, never 4xx
(agent_client.py:171-220). **The callback POST already gets bounded retries for free.** Presign/
download/verify/analyze must **fail-fast** (no retry) — the control plane re-drives the whole Job
(KSUBMIT-05). Do not wrap the analyze step in a retry loop.

### Pattern 3: HTTPS trust via baked CA (KJOB-05)
`construct_agent_client` validates `cfg.agent_ca_file` exists + non-empty, then
`PhazeAgentClient(verify=cfg.agent_ca_file)` (agent_bootstrap.py:61-70). The one-shot does the
same, but the CA file is **baked into the image** (`COPY phaze-ca.crt /etc/phaze/phaze-ca.crt`)
rather than bind-mounted (`/certs:ro` as in `docker-compose.cloud-agent.yml:60`). Set
`PHAZE_AGENT_CA_FILE` to the baked path. **No `verify=False` anywhere** — a CI grep guard for
`verify=False` / `verify = False` in the entrypoint path is cheap and worth adding.

### Anti-Patterns to Avoid
- **Importing `phaze.database` / `sqlalchemy.ext.asyncio` in the entrypoint path** — the pod is
  Postgres-less. Add an analogue of `test_agent_worker_does_not_import_phaze_database`
  (test_task_split.py:33) that subprocess-imports `phaze.job_runner` and asserts the banned
  modules are absent from `sys.modules`. **This is the single highest-leverage test in the phase.**
- **Whole-file `MonoLoader` decode** — violates KJOB-03. `analyze_file` already avoids it (see below).
- **Logging the bearer token** — `PhazeAgentClient` stores it only in httpx headers, never as an
  attribute (D-13). Don't reintroduce a leak in the new module.
- **Reusing the stale `:latest` api image as the FROM base in the release build** — see Q2.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| sha256 of a multi-GB file | A `f.read()` + `hashlib` one-liner | `compute_sha256` (chunked 64 KB) | Loading a multi-hour set into memory defeats KJOB-03; the chunked reader already exists. |
| Windowed BPM/key/mood/style | Any new essentia call sequence | `analyze_file(path, models_dir)` | It already streams per-window via `EasyLoader(startTime/endTime)`, applies the 60/30 caps, isolates per-window failures, and emits the exact wire shape. |
| mood/style → `dict[str,float]` | New conversion code | `_features_to_mood_dict` / `_features_to_style_dict` | Already correct + tested; just relocate to a shared module. |
| HTTPS callback w/ auth, retry, error taxonomy | New httpx wrapper | `PhazeAgentClient` | Bearer header, tenacity funnel, 4xx-no-retry, CA verify — all done. |
| File-mounted secrets in a Postgres-less process | New env parsing | `BaseSettings` + `_FILE` convention | `AgentSettings` already resolves `PHAZE_AGENT_TOKEN_FILE` etc. |
| Structured pod logging | New JSON logger | `configure_logging()` | TTY-aware, import-safe, identical to homelab log shape (D-03). |

**Key insight:** The analysis, hashing, callback, logging, and secret-loading are all solved.
The phase's real work is the **process model** (one-shot + exit codes) and the **image**
(FROM + CA bake + matrix entry) — keep the new surface area minimal and lean on the helpers.

## Runtime State Inventory

Not a rename/refactor/migration phase — greenfield image + module. Section omitted by design.
(One adjacent note: the new image is published to GHCR under a new tag/suffix; that is a build
artifact, not pre-existing runtime state. No data migration is involved in Phase 52.)

## Common Pitfalls

### Pitfall 1: CI build-ordering — the Job image FROM the api image races the matrix
**What goes wrong:** `docker-publish.yml` builds `api`/`audfprint`/`panako` as a **parallel
matrix** (docker-publish.yml:24-45). If `Dockerfile.job` does `FROM ghcr.io/.../phaze:<tag>`,
that tag is produced by the *same* run's `api` matrix entry — a parallel job can't depend on a
sibling matrix entry's pushed tag.
**Why it happens:** Matrix entries have no ordering; `FROM` resolves at build start.
**How to avoid (planner decides one):**
  - (a) Make the Job image a **separate job** with `needs: build-and-push` (like `parity-golden-x86`
    at docker-publish.yml:314 already does — it `needs: build-and-push`), passing the resolved tag
    via `ARG BASE_IMAGE`. This still honors D-04 ("same release-tag workflow") while fixing ordering.
  - (b) Keep it a matrix entry but `FROM python:3.14-slim` and repeat the base apt+uv+deps layers
    (duplication; not recommended).
**Warning signs:** `manifest unknown` / pulling a stale `:latest` at build time.
**Recommendation:** Option (a). D-04 says "another target in the same workflow," not strictly
"another matrix row" — a `needs:`-gated job satisfies the intent and is the pattern the workflow
already uses for `parity-golden-x86`.

### Pitfall 2: essentia models (.pb) are NOT in the api image
**What goes wrong:** `analyze_file`'s COARSE pass needs the 34 TF model `.pb`/`.json` files at
`models_dir`. The api `Dockerfile` does **not** `COPY models` (verified — no model layer); v5.0
agents get them via a `/models` compose volume **plus** `ensure_models_present` auto-download from
`essentia.upf.edu` at worker startup (agent_worker.py:151, model_bootstrap.py). A bare Job pod has
neither a compose volume nor (reliably) network egress to upf.edu.
**Why it happens:** Models (~GBs) were deliberately kept out of images to avoid a flaky-download
build dependency (Dockerfile.agent-arm64:110-113 documents this).
**How to avoid (planner decides — see Q1):** options are (i) bake models into `Dockerfile.job`
(large image, but no runtime download — best for an ephemeral pod with uncertain egress), (ii)
call `ensure_models_present` at entrypoint start to download to an `emptyDir` (depends on
upf.edu reachability + adds minutes to every Job), or (iii) mount models from a cluster
PVC/initContainer (pushes work into Phase 54/56). **This must be resolved before plans are
written** — it materially changes the Dockerfile and the entrypoint.
**Warning signs:** coarse windows all log "failed; skipping" (analysis.py:502) → empty mood/style.

### Pitfall 3: presign response must carry the expected sha256
**What goes wrong:** The pod has no DB, so it cannot look up `FileRecord.sha256_hash` to verify
the download. If the presign-request response doesn't include the expected hash, integrity
verification (KJOB-02) is impossible.
**Why it happens:** Easy to design the presign endpoint to return only the URL.
**How to avoid:** Define the Phase 52 client-side contract so the presign response returns
`{ presigned_get_url, expected_sha256 }`. This mirrors v5.0 exactly: the control plane pins
`expected_sha256` from `FileRecord.sha256_hash` into the `ProcessFilePayload`
(agent_tasks.py `ProcessFilePayload.expected_sha256`, set by `report_pushed`). Phase 53 implements
the server side; Phase 52 mocks it and **defines the consumed shape**.

### Pitfall 4: presigned-URL freshness
**What goes wrong:** A presigned GET minted at submit time expires during a long Kueue quota wait.
**Why it happens:** Kueue admission can be arbitrarily delayed behind quota.
**How to avoid:** CONTEXT + KSTAGE-03 lock the design: the pod requests a **fresh** presign at
*pod start* (post-admission), never reuses a submit-time URL. The entrypoint's step 1 is the
presign request for exactly this reason. (Phase 52 owns the request; Phase 53 owns just-in-time minting.)

### Pitfall 5: exit-code propagation through `uv run`
**What goes wrong:** `CMD ["uv","run","python","-m","phaze.job_runner"]` — if `uv run` swallows or
rewrites the child's exit code, the per-class contract (D-01) is lost at the pod boundary.
**Why it happens:** Wrapper launchers can alter exit status.
**How to avoid:** Verify `uv run` propagates the child exit code unchanged (it does for normal
exits; confirm in a test: `docker run <job-image> ...; echo $?`). If any doubt, the arm64 image's
precedent of invoking the interpreter directly (`python3 -m ...`, Dockerfile.agent-arm64:180) is
the fallback — but note the api image is uv-managed 3.14, so `uv run` is the native call here.
Add a test that asserts the container exits with each expected code for each simulated failure.

### Pitfall 6: signal handling for a long analyze
**What goes wrong:** A multi-hour analyze receives SIGTERM (Kueue eviction / pod delete) and the
process doesn't exit promptly or exits 0.
**Why it happens:** Default Python SIGTERM handling + a busy native essentia call.
**How to avoid:** Run as a clean PID-1 (the `phaze.entrypoint` precedent: no shell wrapper, direct
exec). A SIGTERM during analyze should result in a non-zero exit (default Python SIGTERM → 143),
which is correctly "not success." Don't trap SIGTERM into a 0 exit. Eviction re-drive is the
control plane's job (KSUBMIT-05), so the pod only needs to die honestly.

## Code Examples

### Streaming analysis call (the KJOB-03 path)
```python
# Source: src/phaze/services/analysis.py:520 (analyze_file) + functions.py:222-271 (usage)
# analyze_file decodes ONE window at a time via es.EasyLoader(startTime=, endTime=)
# (analysis.py:457, :488) — no whole-file MonoLoader. _probe_duration_sec uses
# es.MetadataReader (header only, analysis.py:358-367), also no full decode.
analysis = analyze_file(
    read_path,                 # local /tmp path of the downloaded file
    models_dir,                # essentia .pb dir  (see Open Question Q1)
    fine_cap=fine_cap,         # default 60; caps cost to O(1) not O(duration)
    coarse_cap=coarse_cap,     # default 30
)
mood = _features_to_mood_dict(analysis["features"])
style = _features_to_style_dict(analysis["features"])
windows = [AnalysisWindowPayload(**w) for w in analysis["windows"]]
await api.put_analysis(file_id, AnalysisWritePayload(
    bpm=analysis["bpm"], musical_key=analysis["musical_key"],
    mood=mood, style=style, danceability=analysis["danceability"],
    fine_windows_analyzed=analysis["fine_windows_analyzed"],
    fine_windows_total=analysis["fine_windows_total"],
    coarse_windows_analyzed=analysis["coarse_windows_analyzed"],
    coarse_windows_total=analysis["coarse_windows_total"],
    sampled=analysis["sampled"], windows=windows,
))
```

### Client construction with baked CA
```python
# Source: src/phaze/tasks/_shared/agent_bootstrap.py:46-70
ca_path = Path(cfg.agent_ca_file)          # baked image path, e.g. /etc/phaze/phaze-ca.crt
if not ca_path.exists() or ca_path.stat().st_size == 0:
    raise RuntimeError(f"CA file empty or unreadable: {cfg.agent_ca_file}")
client = PhazeAgentClient(
    base_url=cfg.agent_api_url,
    token=cfg.agent_token.get_secret_value(),
    verify=cfg.agent_ca_file,              # KJOB-05 — never verify=False
)
```

### Callback endpoints the POST must hit (exact paths)
```
PUT  /api/internal/agent/analysis/{file_id}          body: AnalysisWritePayload   -> 200 AnalysisWriteResponse
POST /api/internal/agent/analysis/{file_id}/failed   body: AnalysisFailurePayload -> 200 AnalysisFailureResponse
GET  /api/internal/agent/whoami                       -> AgentIdentity   (optional startup probe)
```
Source: `src/phaze/routers/agent_analysis.py:54,94,202` + `agent_client.py:271,282`. Auth =
`Authorization: Bearer <token>`; `agent_id` is derived from the token server-side, never from the
body (AUTH-01). The PUT is an idempotent upsert keyed on `file_id` and advances the file to
`ANALYZED` (agent_analysis.py:188-189) and clears the scheduling ledger (agent_analysis.py:196).

## State of the Art

| Old (v5.0) approach | v6.0 / Phase 52 approach | Why it changed |
|---------------------|--------------------------|----------------|
| Persistent SAQ-draining compute agent (`python3 -m saq agent_worker.settings`) | One-shot fire-once process (`uv run python -m phaze.job_runner`) | Kueue Job per file is ephemeral; no queue drain |
| Failure reported via callback, task returns 0 (SAQ marks complete) | Failure → distinct process exit code (Kueue/Workload reads pod status) | Unattended Job has no SAQ to interpret a return dict |
| File arrives via rsync-over-Tailscale to a scratch dir; `scratch_path` + `expected_sha256` pinned in the SAQ payload | File arrives via presigned S3 GET; pod requests fresh presign at start, expected sha256 in presign response | No persistent pod disk / no mesh assumption (transport-agnostic) |
| arm64 source-built image (`Dockerfile.agent-arm64`, 3.13, `python3 -m`, `--system` install, CA `/certs:ro` mount) | x86 image `FROM` the existing api image (3.14, `uv run`, deps already installed, CA **baked**) | Cluster is x64 → reuse the x86 essentia wheel; no source build; CA baked because ConfigMap CA is deferred (KDEPLOY-06) |

**Explicitly NOT applicable from `Dockerfile.agent-arm64`:** the entire source-build chain
(git clone essentia, `waf configure`, the 4 TF/OpenMP fixes, `--system`/`--ignore-requires-python`
bare-pip, `python3 -m` CMD). Those exist only because no aarch64 essentia wheel exists. Phase 52
is x86 and the wheel is already installed in the api image — none of that transfers. What DOES
transfer conceptually: the `uid/gid 1000` non-root user, the runtime native-lib set
(libatomic1/ffmpeg/libsndfile1/libchromaprint-tools — already in the api base), and the CA-trust
idea (but baked, not mounted).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `uv run` propagates the child process exit code unchanged in the api image | Pitfall 5 | Exit-code contract (D-01/KJOB-04) silently broken at the pod boundary — must be test-verified, not assumed |
| A2 | The presign-request response is the right place to return `expected_sha256` (mirrors v5.0 pinning) | Pitfall 3 | If Phase 53 designs the contract differently, the Phase 52 client interface needs rework — define jointly |
| A3 | Baking models into `Dockerfile.job` is the preferred provisioning route for an egress-uncertain pod | Q1 | Wrong route → either a multi-GB image nobody wanted, or coarse analysis silently empty in the pod |
| A4 | Running `analyze_file` directly (no pebble subprocess) is acceptable for the one-shot | Stack/Alternatives | A native segfault would crash the pod (exit non-zero) which is *correct*, but loses the chance to emit a clean exit 12 with a `report_analysis_failed` callback first — planner should decide |

All other claims are `[VERIFIED: repo]` against the cited source file.

## Open Questions

1. **Where do essentia models come from in the Job pod?** (HIGH priority — blocks Dockerfile + entrypoint design)
   - What we know: api image does NOT bake models; v5.0 uses a `/models` volume + `ensure_models_present` upf.edu download; the pod has no compose volume.
   - What's unclear: bake (i) vs runtime-download (ii) vs cluster PVC/initContainer (iii).
   - Recommendation: **bake models into `Dockerfile.job`** for a self-contained, egress-independent
     ephemeral pod (accept the larger image), OR explicitly defer model-mount to Phase 54/56 and
     have Phase 52's entrypoint accept a `models_dir` that's provisioned externally. Decide in
     `/gsd:discuss-phase` follow-up or first planning task — it is the one true blocker.

2. **Job image build-ordering vs the api image** (resolved-with-recommendation — Pitfall 1)
   - Recommendation: separate `needs: build-and-push` job with `ARG BASE_IMAGE`, not a parallel
     matrix row. Satisfies D-04's "same workflow" intent.

3. **Presign-request endpoint shape** (coordination with Phase 53)
   - What we know: pod calls it at startup with `file_id`; expects `{url, expected_sha256}`.
   - What's unclear: exact path/verb/auth (likely `POST /api/internal/agent/.../presign` with the
     same bearer). Phase 52 should add a `PhazeAgentClient` method + mock it; Phase 53 implements it.
   - Recommendation: define a minimal `request_download_url(file_id) -> (url, sha256)` client method
     now, respx-mocked in tests; flag the server contract as Phase 53's.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Existing x86 api image on GHCR | `FROM` base for `Dockerfile.job` | ✓ | published per release tag | — |
| Python 3.14 + essentia-tensorflow (cp314 x86) | analysis in pod | ✓ (in api image) | per `uv.lock` | — |
| native libs (libatomic1, ffmpeg, libsndfile1, libchromaprint-tools) | essentia/decode | ✓ (api Dockerfile:20) | base-image snapshot | — |
| essentia model `.pb` files | coarse analysis pass | ✗ (NOT in api image) | — | **Q1 — bake / download / mount (unresolved)** |
| Operator internal CA cert (`phaze-ca.crt`) | KJOB-05 baked trust | ✗ at build unless provided | — | operator supplies at build (build context / secret) |
| Object storage + presign endpoint | download | ✗ (Phase 53) | — | **mocked** for Phase 52 unit tests (respx) |
| Live Kueue cluster | running the Job | ✗ (Phase 54) | — | not needed — Phase 52 is unit-tested against a fake control plane |

**Missing with no fallback (blocking):** essentia models provisioning (Q1) — must be decided.
**Missing with fallback:** object storage + presign (mocked via respx); CA (operator build input);
cluster (out of scope for this phase).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio + respx (httpx mocking); `uv run pytest` |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`); coverage gate 85% (CLAUDE.md) |
| Quick run command | `uv run pytest tests/test_job_runner.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| KJOB-01 | Image builds FROM api base, zero new pip deps | integration (Dockerfile lint/grep) + matrix-entry test | `uv run pytest tests/test_deployment/ -k job` | ❌ Wave 0 |
| KJOB-02 | presign→download→verify→analyze→POST→exit happy path | unit (respx fake control plane + fixture audio) | `uv run pytest tests/test_job_runner.py -k happy_path -x` | ❌ Wave 0 |
| KJOB-03 | windowed/streaming only — no whole-file MonoLoader | unit (assert `analyze_file` path; AST/grep guard for `MonoLoader`) | `uv run pytest tests/test_job_runner.py -k no_monoloader` | ❌ Wave 0 |
| KJOB-04 | distinct non-zero exit per failure class; never 0 on failure | unit (parametrized exit-code matrix) + container `echo $?` smoke | `uv run pytest tests/test_job_runner.py -k exit_code` | ❌ Wave 0 |
| KJOB-05 | callback uses baked CA; no `verify=False` anywhere | unit (assert `verify=<ca>` passed) + grep guard | `uv run pytest tests/test_job_runner.py -k ca_verify` | ❌ Wave 0 |
| (boundary) | entrypoint imports NO `phaze.database` | subprocess import-boundary | `uv run pytest tests/test_task_split.py -k job_runner` | ❌ Wave 0 (extend existing file) |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_job_runner.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` (≥85%)
- **Phase gate:** full suite green + `ruff check` + `ruff format --check` + `uv run mypy .` before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_job_runner.py` — covers KJOB-02/03/04/05 (respx fake control plane, small fixture audio clip, exit-code matrix)
- [ ] Extend `tests/test_task_split.py` — subprocess import-boundary case for `phaze.job_runner` (no `phaze.database` / `sqlalchemy.ext.asyncio`)
- [ ] `tests/test_deployment/` — assert `docker-publish.yml` produces the Job image tag(s); assert `Dockerfile.job` contains no `pip install`/`uv add`
- [ ] Fixture: a short committed reference clip (or reuse `scripts/parity/reference.wav` if present) so `analyze_file` runs in-test without GB models — may need a models-stub or a fine-tier-only path for the unit test

## Security Domain

> `security_enforcement` not present in `.planning/config.json` → treated as enabled.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | Bearer token via `Authorization` header; token from `_FILE` secret; `agent_id` derived server-side, never from body (AUTH-01) — `PhazeAgentClient` + `agent_auth.get_authenticated_agent` |
| V3 Session Management | no | Stateless one-shot; no sessions |
| V4 Access Control | yes | `file_id` rides the URL path only; PUT/POST scoped to path `file_id`, body cannot redirect to another file (agent_analysis.py:171-196) |
| V5 Input Validation | yes | `AnalysisWritePayload`/`AnalysisWindowPayload` are `extra="forbid"` pydantic with `ge`/`le` bounds + `max_length=50000` on windows |
| V6 Cryptography | yes | sha256 integrity via stdlib `hashlib` (`compute_sha256`); TLS via baked internal CA (`verify=<ca_file>`), **never** `verify=False`; never log the bearer (D-13) |
| V12 Files/Resources | yes | Stream download to `/tmp`, bounded; delete the temp file on exit (mirror `process_file`'s `finally` unlink, functions.py:296) |

### Known Threat Patterns for {x86 one-shot pod ↔ control-plane HTTPS}
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Corrupt/partial download analyzed as truth | Tampering | sha256 verify vs expected hash → exit 11 (KJOB-02/04) |
| TLS MITM on callback | Spoofing/Info | Baked internal CA pin via `verify=<ca>`; reject any other cert (KJOB-05) |
| Bearer token leak in logs | Info disclosure | Token only in httpx headers, never logged (D-13); `_FILE` secret strip; structured logs carry `file_id` not creds |
| Forged `agent_id`/`file_id` in body | Elevation/Tampering | `extra="forbid"` payloads; server derives `agent_id` from token; `file_id` from path only |
| Pod reaches Postgres directly | Elevation | Import-boundary test bans `phaze.database` in the entrypoint path |
| Presigned URL replay after expiry | Spoofing | Fresh presign minted at pod start, short TTL (KSTAGE-03; server side Phase 53) |
| Stale `:latest` base image with old code shipped | Tampering/Supply chain | Build Job image FROM the freshly-built release tag, not `:latest` (Pitfall 1) |

## Sources

### Primary (HIGH confidence — repo, `[VERIFIED: repo]`)
- `src/phaze/services/analysis.py` — `analyze_file`, `_iter_windows`, `_analyze_fine_windows` (EasyLoader streaming), `_analyze_coarse_windows`, `_probe_duration_sec` (MetadataReader, no decode)
- `src/phaze/tasks/functions.py` — `process_file` (the v5.0 single-file flow), `_features_to_mood_dict`/`_features_to_style_dict`, terminal/retryable outcome handling
- `src/phaze/services/agent_client.py` — `PhazeAgentClient`, `verify` threading, tenacity retry funnel, `put_analysis`/`report_analysis_failed`
- `src/phaze/routers/agent_analysis.py` + `src/phaze/schemas/agent_analysis.py` — callback endpoints, payload schemas, state advance, ledger clear
- `src/phaze/logging_config.py` — `configure_logging` (import-safe, TTY-aware JSON)
- `src/phaze/entrypoint.py` — pre-uvicorn shim (PID-1/exec precedent, import-boundary invariant)
- `src/phaze/services/hashing.py` — `compute_sha256` (chunked)
- `src/phaze/config.py` — `_FILE`-secret machinery, `AgentSettings.agent_api_url/agent_token/agent_ca_file/models_path/cloud_scratch_dir`
- `src/phaze/tasks/_shared/agent_bootstrap.py` — `construct_agent_client` (CA guard + verify)
- `src/phaze/schemas/agent_tasks.py` — `ProcessFilePayload` (`expected_sha256`/`scratch_path` precedent)
- `Dockerfile` — x86 base: python:3.14-slim, native libs, uv, USER phaze, `uv run` CMD, no models layer
- `Dockerfile.agent-arm64` — v5.0 image precedent (and what does NOT transfer)
- `.github/workflows/docker-publish.yml` — matrix (api/audfprint/panako), `image_suffix`/bare-repo URL (Phase 29 D-15), `needs: build-and-push` precedent (`parity-golden-x86`), metadata/cache steps
- `docker-compose.cloud-agent.yml` — `/models:rw` + `/certs:ro` mount precedent
- `tests/test_task_split.py` — `test_agent_worker_does_not_import_phaze_database` (the boundary test to clone)
- `src/phaze/tasks/_shared/model_bootstrap.py` + `src/phaze/scripts/download_models.py` — model provisioning reality (upf.edu, not baked)

### Secondary / config
- `.planning/phases/52-.../52-CONTEXT.md` (D-01..D-04 + discretion), `.planning/REQUIREMENTS.md` (KJOB-01..05), `.planning/PROJECT.md` (CPU-only, CA, entrypoint-shim Key Decisions), `.planning/config.json` (nyquist_validation true)

### Tertiary (LOW confidence)
- None — no WebSearch was required; the phase is fully codebase-grounded (consistent with the
  ROADMAP note that Phase 52 needs no research-phase and directly parallels v5.0 Phase 47).

## Metadata

**Confidence breakdown:**
- Standard stack (reused helpers): HIGH — every component read directly from the cited file
- Architecture (one-shot + exit codes): HIGH — derived from `process_file` + D-01/D-02; the new
  surface is small and the precedent is explicit
- Pitfalls: HIGH for #1/#3/#4/#5/#6 (grounded), HIGH for #2 (models) as a *risk* though its
  *resolution* (Q1) is an open decision
- Open questions: Q1 (models) is the one true blocker; Q2 resolved-with-recommendation; Q3 is a
  Phase 53 coordination seam

**Research date:** 2026-06-27
**Valid until:** ~2026-07-27 (stable — internal codebase; no fast-moving external deps since zero new pip deps)
</content>
</invoke>
