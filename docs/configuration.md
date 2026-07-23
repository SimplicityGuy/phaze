<!-- generated-by: gsd-doc-writer -->
# Configuration

All configuration is via environment variables (or a `.env` file). See [`.env.example`](../.env.example) for the operator-facing defaults.

The canonical source of truth is [`src/phaze/config.py`](../src/phaze/config.py), a [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) hierarchy.

## How settings are loaded

Phaze splits settings into a shared `BaseSettings` class plus two role-specific subclasses, selected at process boot by the `PHAZE_ROLE` env var:

| `PHAZE_ROLE` | Settings class    | Role                                                                 |
|--------------|-------------------|---------------------------------------------------------------------|
| `control` (default) | `ControlSettings` | Application server: LLM proposals, Discogs matching, fileless tasks |
| `agent`      | `AgentSettings`   | File server: HTTP client to the app server, file-bound SAQ tasks    |

`get_settings()` (cached via `lru_cache`) is the single dispatch point. A module-level `settings = ControlSettings()` singleton is preserved for back-compat with existing `from phaze.config import settings` call sites; agent entry points call `get_settings()` / `AgentSettings()` directly.

**Env var binding:** most fields bind to the uppercased field name (e.g., `scan_path` ← `SCAN_PATH`). Several fields are bound to an explicit `PHAZE_*` alias via `validation_alias=AliasChoices(...)`, in which case the `PHAZE_*` form is the documented operator-facing name and the bare name still works for in-process / test convenience. Both forms are listed below where they differ.

## Secrets via files (`_FILE` convention)

Every **secret-bearing** setting also accepts a `<VAR>_FILE` sibling that points at a file containing the secret — the same convention used by the official Postgres/Redis images and our sibling service `discogsography`. This lets a deployment share a single Docker/Swarm secret (`/run/secrets/...`), a Kubernetes secret mount, or a SOPS-decrypted file with Phaze without inlining the cleartext into an env var.

The secret-bearing fields and their `_FILE` siblings:

| Field | Roles | `_FILE` variables (any one works) |
|-------|-------|-----------------------------------|
| `anthropic_api_key` | control | `ANTHROPIC_API_KEY_FILE` |
| `openai_api_key`    | control | `OPENAI_API_KEY_FILE` |
| `database_url`      | all     | `PHAZE_DATABASE_URL_FILE`, `DATABASE_URL_FILE` |
| `redis_url`         | all     | `PHAZE_REDIS_URL_FILE`, `REDIS_URL_FILE` |
| `queue_url`         | all     | `PHAZE_QUEUE_URL_FILE` |
| `agent_token`       | agent   | `PHAZE_AGENT_TOKEN_FILE`, `AGENT_TOKEN_FILE` |
| `push_ssh_key`      | agent   | `PHAZE_PUSH_SSH_KEY_FILE` (whitespace **preserved**) |
| `push_known_hosts`  | agent   | `PHAZE_PUSH_KNOWN_HOSTS_FILE` (whitespace **preserved**) |

> **Removed in 2026.7.1 (Phase 67, REG-04):** the flat control-plane S3 and kube
> secret env vars are gone with no shim. Per-backend secrets now live as inline
> `*_file` pointers inside `backends.toml` (each S3 bucket's access/secret key and
> each Kueue backend's kubeconfig / SA token), resolved by the shared secret-file
> helper. Only the control-plane secrets above (LLM keys + `database_url` /
> `redis_url` / `queue_url`) remain on the env `<VAR>_FILE` path. See
> **[Backend registry (`backends.toml`)](#backend-registry-backendstoml)** below.

Semantics (implemented by the shared `_resolve_secret_files` validator in `config.py`, which derives the `_FILE` names from each field's existing aliases):

- **One `_FILE` per accepted env name.** A field bound to both `PHAZE_DATABASE_URL` and `DATABASE_URL` honors `PHAZE_DATABASE_URL_FILE` **and** `DATABASE_URL_FILE`.
- **Precedence:** an explicitly-set direct env var always wins over its `_FILE` sibling. The file is read only when the direct var is unset.
- **Newline stripping:** surrounding whitespace and trailing newlines are stripped (`.strip()`). This is critical for `PHAZE_AGENT_TOKEN` — the *entire* wire string (prefix included) is hashed by `phaze.routers.agent_auth.hash_token`, so a stray `\n` from a heredoc/`echo`-created secret file would otherwise make the hash never match (a permanent 401).
- **Whitespace-preserved exceptions:** `push_ssh_key` and `push_known_hosts` are the **only** `_FILE` secrets kept **verbatim** (NOT stripped), because OpenSSH requires the trailing newline on key material / known_hosts lines — stripping it makes `ssh` reject the key (`invalid format` / `error in libcrypto`). They are members of `SECRET_FILE_PRESERVE_WHITESPACE` in `config.py`.
- **Fail-fast:** if a `_FILE` var is set but the path is missing or unreadable, startup raises a `ValidationError` naming the variable and path — it never silently falls back to an empty secret.
- Resolution runs **before** the required-field and production guards (`_enforce_required_agent_fields`, the HTTPS/Redis-password validators), so a `_FILE`-sourced `PHAZE_AGENT_TOKEN` satisfies the required-field guard. `SecretStr` fields stay `SecretStr` (masked in logs/reprs) after resolution.

Example (Docker secret mounted at `/run/secrets/anthropic_api_key`):

```bash
ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic_api_key   # no ANTHROPIC_API_KEY needed
```

Resolution order for a single secret-bearing field (`_resolve_secret_files`):

```mermaid
flowchart TD
    A[Direct env var set?<br/>e.g. ANTHROPIC_API_KEY] -->|yes| B[Use env value verbatim<br/>_FILE sibling ignored]
    A -->|no| C[_FILE sibling set?<br/>e.g. ANTHROPIC_API_KEY_FILE]
    C -->|yes| D[Read file at path]
    D -->|readable| E{push_ssh_key /<br/>push_known_hosts?}
    E -->|yes| F[Keep verbatim<br/>trailing newline preserved]
    E -->|no| G[.strip whitespace/newline]
    D -->|missing / unreadable| H[Fail fast:<br/>ValidationError names var + path]
    C -->|no| I[Field default]
```

## Core settings (all roles)

| Variable                          | Required | Default                                                  | Description                                                                 |
|-----------------------------------|----------|----------------------------------------------------------|-----------------------------------------------------------------------------|
| `PHAZE_ROLE`                      | No       | `control`                                                | Selects the settings subclass: `control` or `agent`.                        |
| `PHAZE_DATABASE_URL` (or `DATABASE_URL`) | No | `postgresql+asyncpg://phaze:phaze@postgres:5432/phaze`    | PostgreSQL connection string. Use `localhost` when running on the host instead of in Compose. |
| `PHAZE_REDIS_URL` (or `REDIS_URL`)| No       | `redis://redis:6379/0`                                    | Redis connection string. **Cache / rate-limit / counters only** — no longer the SAQ broker (see `PHAZE_QUEUE_URL`). In production agent mode, a password is required (see Per-environment overrides). |
| `PHAZE_QUEUE_URL` (or `queue_url`)| No       | `postgresql://phaze:phaze@postgres:5432/phaze`            | SAQ Postgres broker DSN (Phase 36). Must be the **raw libpq** form (`postgresql://…`), NOT the SQLAlchemy `postgresql+asyncpg://` dialect — psycopg3's pool cannot parse the `+driver` suffix (an `+asyncpg`/`+psycopg` value is auto-normalized). Carries DB credentials, so it is secret-bearing (`PHAZE_QUEUE_URL_FILE`). On agent hosts it points at the app-server Postgres LAN IP:5432 — agents open a psycopg3 pool to it (new firewall edge, relaxes D-25). |
| `DEBUG`                           | No       | `false`                                                  | Enable debug mode.                                                          |
| `API_HOST`                        | No       | `0.0.0.0`                                                | API server bind address.                                                    |
| `API_PORT`                        | No       | `8000`                                                   | API server port.                                                            |
| `SCAN_PATH`                       | No       | `/data/music`                                            | Music directory mounted for scanning.                                       |
| `MODELS_PATH`                     | No       | `/models` (config default; `.env.example` uses `./models`) | Essentia audio-analysis model directory. Run `just download-models` to populate. |
| `OUTPUT_PATH`                     | No       | `/data/output`                                           | Destination directory for executed file moves.                             |
| `PHAZE_ENABLE_SAQ_UI` (or `enable_saq_ui`) | No | `true`                                          | Mount SAQ's built-in queue-monitoring dashboard at `/saq` in the `phaze-api` app (reusing the lifespan SAQ `PostgresQueue` instances; no extra broker, no extra port). Set `false` to skip the mount entirely. See [api.md](api.md) → SAQ Monitoring UI. |
| `PHAZE_SCRAPER_CONTACT_URL` (or `scraper_contact_url`) | No | `https://github.com/SimplicityGuy/phaze` | Contact URL embedded in the honest 1001Tracklists scraper User-Agent (phaze-hu8v). Lives on `BaseSettings`. |

## Worker / task queue settings (all roles)

| Variable                       | Required | Default | Description                                          |
|--------------------------------|----------|---------|------------------------------------------------------|
| `WORKER_MAX_JOBS`              | No       | `8`     | Concurrent SAQ jobs per worker.                      |
| `WORKER_JOB_TIMEOUT`          | No       | `600`   | Per-job timeout in seconds.                          |
| `WORKER_MAX_RETRIES`          | No       | `4`     | Max attempts per job (1 initial + 3 retries).        |
| `WORKER_PROCESS_POOL_SIZE`    | No       | `4`     | Concurrency bound (`asyncio.Semaphore`) on in-flight essentia analysis subprocesses per agent worker. |
| `WORKER_HEALTH_CHECK_INTERVAL`| No       | `60`    | SAQ health-check interval in seconds.                |
| `WORKER_KEEP_RESULT`          | No       | `3600`  | Seconds SAQ retains a finished job's result.         |
| `PHAZE_ABORTING_REAP_SECONDS` (or `aborting_reap_seconds`) | No | `900` | Seconds past a job's frozen `started` timestamp before a `saq_jobs` row stuck in `status='aborting'` is reaped (deleted, releasing its deterministic key). The bound is on `started`, not `touched` — SAQ's sweeper bumps `touched` on every abort pass, so a touched-based bound would never trigger (phaze-qmc2.1). MUST exceed `WORKER_JOB_TIMEOUT`; default 900 = 600s timeout + 300s slack. |
| `PHAZE_SCAN_STALL_SECONDS` (or `SCAN_STALL_SECONDS`) | No | `86400` (24h) | Seconds with no progress before a RUNNING scan is reaped as stalled by the control worker's every-minute cron. Lives on `BaseSettings`, so both roles parse it, but only the control worker runs the reaper. `scan_directory` is an unbounded (`timeout=0`) BULK job, so this progress-based reaper is its sole liveness guard — the generous 24h default keeps a healthy-but-slow scan (e.g. SHA-256 hashing a multi-GB concert video on a network mount) from being falsely reaped. The admin UI flips a RUNNING scan to an amber "stalled?" indicator at **half** this threshold (12h), before the hard reap. `.env.example` ships only a commented example, so the effective default is the field default. |

### Per-lane agent-worker concurrency

Only the agent lane worker (started with `PHAZE_AGENT_LANE` set) reads these; they live on `BaseSettings` so both roles parse cleanly.

| Variable                       | Required | Default | Description                                          |
|--------------------------------|----------|---------|------------------------------------------------------|
| `PHAZE_LANE_ANALYZE_CONCURRENCY` (or `lane_analyze_concurrency`) | No | `4` | Concurrency of the analyze lane worker (`process_file`; in-process essentia, CPU-bound). |
| `PHAZE_LANE_FINGERPRINT_CONCURRENCY` (or `lane_fingerprint_concurrency`) | No | `2` | Concurrency of the fingerprint lane worker (`fingerprint_file`; via panako/audfprint, CPU-bound). |
| `PHAZE_LANE_META_CONCURRENCY` (or `lane_meta_concurrency`) | No | `2` | Concurrency of the meta lane worker (extract/scan/execute; light/fast). |
| `PHAZE_LANE_IO_CONCURRENCY` (or `lane_io_concurrency`) | No | `4` | Concurrency of the io lane worker (`s3_upload`/`push_file`; network-bound, off CPU budget). |
| `PHAZE_AGENT_HEARTBEAT` (or `agent_heartbeat_enabled`) | No | `true` | Whether this agent worker launches the liveness heartbeat background task. Compose sets this `true` on exactly one lane worker (analyze) and `false` on the other three, so an agent reports one authoritative `last_seen`, never N duplicate heartbeats. |

### Database connection pool tuning (all roles)

These size the SQLAlchemy engine pool shared by the api and control-worker engines, plus the control-side per-`(agent, lane)` dispatch queues in `services/agent_task_router.py`. Defaults are deliberately conservative because phaze reaches Postgres through PgBouncer in session mode, where every client connection pins one upstream server connection for its lifetime.

| Variable                       | Required | Default | Description                                          |
|--------------------------------|----------|---------|------------------------------------------------------|
| `PHAZE_DB_POOL_SIZE` (or `db_pool_size`) | No | `5` | SQLAlchemy engine `pool_size` for the api + control-worker engines. |
| `PHAZE_DB_MAX_OVERFLOW` (or `db_max_overflow`) | No | `5` | SQLAlchemy engine `max_overflow` for the api + control-worker engines. |
| `PHAZE_DB_POOL_TIMEOUT` (or `db_pool_timeout`) | No | `10` | Seconds to wait for a pooled connection before failing fast. |
| `PHAZE_DB_POOL_RECYCLE` (or `db_pool_recycle`) | No | `1800` | Recycle a pooled connection after this many seconds (30 min) so idle server slots are freed rather than pinned indefinitely. |
| `PHAZE_DB_POOL_PRE_PING` (or `db_pool_pre_ping`) | No | `true` | Validate a pooled connection before checkout, dropping dead server connections instead of handing one out. |
| `PHAZE_DISPATCH_QUEUE_MIN_SIZE` (or `dispatch_queue_min_size`) | No | `0` | psycopg3 `min_size` for each control-side per-(agent,lane) dispatch queue. `0` keeps zero idle server connections pinned. |
| `PHAZE_DISPATCH_QUEUE_MAX_SIZE` (or `dispatch_queue_max_size`) | No | `2` | psycopg3 `max_size` for each control-side per-(agent,lane) dispatch queue, capping the enqueue burst. |

## Backend registry (`backends.toml`)

**As of 2026.7.1 (Phase 67, REG-01/04/05, D-11/D-12) the typed backend registry is the SOLE cloud config surface.** It replaces the flat `PHAZE_CLOUD_TARGET` selector and the flat `PHAZE_S3_*` / `PHAZE_KUBE_*` / compute-scratch env vars, which were **removed with no back-compat shim**. Instead of one global cloud target, you declare a *registry* of backends (and their staging buckets) in a TOML file.

**Loading + zero-config default.** The registry is loaded from a TOML file pointed at by `PHAZE_BACKENDS_CONFIG_FILE` (default `/etc/phaze/backends.toml`). If the file is **absent**, the control plane synthesizes an **implicit single `kind=local` backend** — an all-local deploy needs **zero** config edits. The registry is sourced **only** from the TOML file (it is deliberately not an env var), and a present-but-empty `backends = []` fails fast at startup rather than silently booting with no backend.

```mermaid
flowchart TD
    A[PHAZE_BACKENDS_CONFIG_FILE] --> B{File present?}
    B -->|no| C[Implicit single kind=local backend<br/>id=local, rank=99, cap=1<br/>zero-config all-local]
    B -->|yes| D[Parse TOML — authoritative]
    D --> E{backends = empty?}
    E -->|yes| F[Fail fast:<br/>refuse to boot with no backend]
    E -->|no| G[_validate_registry model validator]
    G --> H{Per-variant + whole-registry<br/>invariants hold?}
    H -->|no| I[Fail fast:<br/>id-tagged ValidationError<br/>compute→agent_ref+scratch_dir,<br/>kueue→[kube]+non-empty buckets,<br/>cluster-specific bucket ≤1 ref]
    H -->|yes| J[Registry live<br/>logged secret-free as id/kind/rank/cap]
```

**`[[backends]]` — the analysis backends.** An array-of-tables; each entry is a discriminated union on `kind`:

| Field | Applies to | Description |
|-------|-----------|-------------|
| `id` | all | Unique backend identifier (used in logs + bucket refs). |
| `kind` | all | `local` \| `compute` \| `kueue`. Selects the variant + its required config. |
| `rank` | all | Cost-tier ordering; lower ranks are preferred by the scheduler. |
| `cap` | all | Concurrency cap for this backend (replaces the old flat in-flight window). |
| `agent_ref` | `compute`, `kueue` | On `compute`: **REQUIRED** — names the compute agent node the rsync push dispatches to; construction **fails fast** (id-tagged `ValueError`) if absent (REG-02, D-13). On `kueue`: **optional** (default unset, phaze-ifcr) — binds the `kind="compute"` Agent row provisioned for this cluster's one-shot `job_runner` callbacks so the COMPUTE-01 admin-page dedupe has an authoritative key; unset falls back to the id/name-coincidence dedupe path. |
| `scratch_dir` | `compute` | **REQUIRED** — remote scratch dir the rsync push lands in (was the flat compute-scratch mirror). Construction fails fast if absent (a missing value would build a literal `"None/<file_id>"` push path). |
| `push_host` | `compute` | **REQUIRED** (Phase 73, D-01) — the rsync/ssh push destination host. Construction fails fast if absent (a missing value would build a literal `"None:..."` remote spec); rejected if it contains whitespace or shell metacharacters. |
| `ssh_user` | `compute` | Optional ssh login user for the push. Omitted ⇒ the push falls back to the fileserver's configured user; rejected if it contains whitespace or shell metacharacters. |
| `[backends.kube]` | `kueue` | Nested Kueue cluster config: API URL, namespace, per-cluster kubeconfig `context`, local-queue, Job image/resources, workload apiVersion, CA/ConfigMap/Secret names, and inline `kubeconfig_file` / `sa_token_file` secret pointers. |
| `buckets` | `kueue` | List of `[[buckets]]` `id`s this Kueue backend stages through. |

> **Worked multi-compute scenario.** For a worked mixed arm64/x86 cost-tiered example — two `kind="compute"` backends (free arm64 preferred, paid x86 spill) plus the local catch, with the per-agent compose recipe and the rank-tiered drain — see [multi-compute.md](multi-compute.md). This section stays the canonical field reference.

`[backends.kube]` keys of note: `api_url`, `namespace`, `local_queue` (all **required** on a `kind="kueue"` backend), plus `context` — the **per-cluster kubeconfig context** name (MKUE-01) that selects among N clusters; when omitted the client uses the kubeconfig's current-context. `context` is a plain kubeconfig context name, **not** a secret. Further optional keys: `env_configmap_name` (default `phaze-agent-env`) and `env_secret_name` (default `phaze-agent-token`) — the operator-created ConfigMap/Secret names the submitted Job's pod pulls its agent env and token from (referenced by name only, like `ca_secret_name`); and `active_deadline_seconds` (default `10800`, bounded `gt=0`) — the Job-level wall-clock bound emitted as `spec.activeDeadlineSeconds` (phaze-1b39). The deadline is load-bearing, not cosmetic: `job_runner` delegates **all** wall-clock bounding to it (the analyze stage runs with no inner timeout), so without it an admitted-but-stalled pod (ImagePullBackOff, hung analyze, black-holed callback) would occupy its burst-lane cap slot forever; k8s SIGTERMs the pod at the deadline and the Failed Job routes into the normal re-drive/spill recovery.

`[backends.kube].models_pvc_name` (optional) names an **operator-provisioned** `ReadOnlyMany` PersistentVolumeClaim pre-populated with the essentia weights. When set, `build_job_manifest` mounts that claim **read-only** at `/models` (which **must** equal the agent-env ConfigMap's `PHAZE_MODELS_DIR`) so the analyze pod reads its weights from provisioned storage — instead of a fat image or a runtime download. phaze **creates no PV/PVC** and references the claim **by name only** (same posture as the LocalQueue / Secret / ConfigMap it references by name); the PVC carries **only** model weights, never secrets or certs. Like `context`, `models_pvc_name` is a plain Kubernetes object name, **not** a secret. Unset ⇒ no models volume/mount is emitted (the Job manifest is byte-identical to the CA-only form). See [k8s-burst.md](k8s-burst.md) "Models provisioning".

**`[[buckets]]` — the S3 staging-bucket registry (REG-05).** An array-of-tables of the S3-compatible staging buckets Kueue backends reference:

| Field | Description |
|-------|-------------|
| `id` | **REQUIRED** — unique **registry key** referenced by a backend's `buckets` list. This is *not* the S3 bucket name (see `bucket`). |
| `bucket` | **REQUIRED** — the actual **S3 bucket name** the staging objects are written to (distinct from `id`, which is only the registry key). |
| `scope` | **REQUIRED** — `shared` (any number of Kueue backends may reference it) or `cluster-specific` (**at most one** Kueue backend may reference it — a cardinality invariant enforced at startup). |
| `endpoint_url` | **REQUIRED** — S3-compatible endpoint URL (validated as a well-formed http(s) URL with a host at registry construction; a scheme-less or non-http value is rejected — SSRF guard). |
| `region`, `addressing_style` | Optional S3 connection tuning (`addressing_style` defaults to `path`). |
| `access_key_id_file`, `secret_access_key_file` | Inline `*_file` secret pointers (control-plane only; never sent to the agent or pod). |

Whole-registry invariants (enforced by the `_validate_registry` model validator at startup): non-empty registry; every Kueue backend's `buckets` ids resolve to a declared bucket and the resolved set is non-empty; a `cluster-specific` bucket is referenced by at most one Kueue backend. The resolved registry is logged **secret-free** at boot as an `{id, kind, rank, cap}` projection.

The global tuning knobs below (route threshold, retry budgets, S3 presign/lifecycle/part-size) are **not** per-backend and remain env vars on `ControlSettings`.

## Cloud-burst settings

> **Superseded in 2026.7.1 (Phase 67):** the flat `cloud_target` / `cloud_max_in_flight` / compute-scratch and flat `s3_*` / `kube_*` knobs in the tables below were **removed with no shim** — backend selection, caps, cluster config, and bucket config now come from the **[Backend registry](#backend-registry-backendstoml)** above. The rows are retained only as a historical field reference; the **global** knobs still marked as kept (`cloud_route_threshold_sec`, `push_max_attempts`, `cloud_submit_max_attempts`, `cloud_spill_to_local_after_seconds`, `cloud_uploading_stale_after_sec` / `cloud_uploaded_stale_after_sec`, the `s3_presign_*` / `s3_lifecycle_ttl_days` / `s3_multipart_part_size_bytes` / `s3_client_timeout_sec` knobs, and the agent-side `cloud_scratch_dir` / push-SSH fields) remain live env vars.

Cloud burst (Phase 49/50/51, v5.0) offloads **long** audio sets (duration ≥ the route threshold) to a free OCI A1 arm64 **compute agent** over Tailscale via an rsync push — instead of letting them time out on the local file server. The full feature walkthrough, runbook, and smoke test live in [cloud-burst.md](cloud-burst.md); this section is the canonical knob reference.

Descriptions are sourced from the `Field(...)` text in [`src/phaze/config.py`](../src/phaze/config.py). The `Class` column is the role the field lives on (`ControlSettings` = the application server that owns routing; `AgentSettings` = the compute agent). All knobs use the `PHAZE_*` (or bare-name) dual form described above unless noted.

| Knob | Env var (alias) | Class | Default | `_FILE`? | Description |
|------|-----------------|-------|---------|----------|-------------|
| `cloud_target` | ~~`PHAZE_CLOUD_TARGET`~~ (removed) | Control | *n/a* | no | **REMOVED in 2026.7.1 (Phase 67, REG-01/04) — no shim.** This flat selector no longer exists; backend selection now comes from the [Backend registry](#backend-registry-backendstoml). Nothing in `src/phaze/` reads it, and because `model_config` is `extra="ignore"` a stale `PHAZE_CLOUD_TARGET` env var left in a live `.env` is **silently dropped** (not honored) — it does **not** change routing. Delete it. See the 1:1 `cloud_target`→`backends` equivalence in *[Cloud target](#cloud-target-removed-in-phase-67)* below. |
| `cloud_route_threshold_sec` | `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC` (or `cloud_route_threshold_sec`) | Control | `5400` | no | Duration threshold (seconds) at/above which a file is routed to a cloud compute agent. Default 5400 (90 min); bounded `gt=0, lt=86400` (out-of-range fails fast at startup). |
| `cloud_max_in_flight` | ~~`PHAZE_CLOUD_MAX_IN_FLIGHT`~~ (removed) | Control | *n/a* | no | **REMOVED in 2026.7.1 (Phase 67) — no shim.** This flat ≤N backpressure window no longer exists as a settings field; per-backend concurrency is now the `cap` field on each `[[backends]]` entry in [`backends.toml`](#backend-registry-backendstoml). A stale `PHAZE_CLOUD_MAX_IN_FLIGHT` env var is silently dropped (`extra="ignore"`), not honored. |
| `push_max_attempts` | `PHAZE_PUSH_MAX_ATTEMPTS` (or `push_max_attempts`) | Control | `3` | no | Max push re-drives of a sha256-mismatched file before it **spills back to `AWAITING_CLOUD`** (Phase 69 SCHED-03: at the cap the file no longer hard-fails — its cloud budget is marked spent so the next drain tick routes it to local). Bounded `gt=0, lt=20`. |
| `cloud_spill_to_local_after_seconds` | `PHAZE_CLOUD_SPILL_TO_LOCAL_AFTER_SECONDS` (or `cloud_spill_to_local_after_seconds`) | Control | `900` | no | **Live (Phase 69, D-02) — the tiered-drain staleness gate.** Seconds a long file waits in `AWAITING_CLOUD` while higher-rank backends are online-but-**FULL** before slow local (rank 99) becomes an eligible spill target. Offline backends spill to local immediately (D-03, not staleness-gated). Bounded `gt=0, lt=86400`. |
| `cloud_uploading_stale_after_sec` | `PHAZE_CLOUD_UPLOADING_STALE_AFTER_SEC` (or `cloud_uploading_stale_after_sec`) | Control | `21600` | no | **Live (phaze-ul2v) — stranded-upload reaper.** Seconds a `cloud_job` may sit `UPLOADING` with no timestamp movement before the reconcile reaper spills it back to awaiting. Generous default (6h) because a multi-GB multipart upload is legitimately slow and bumps no timestamp while it transfers; MUST exceed the largest `s3_upload` SAQ net. Bounded `gt=0, lt=604800`. |
| `cloud_uploaded_stale_after_sec` | `PHAZE_CLOUD_UPLOADED_STALE_AFTER_SEC` (or `cloud_uploaded_stale_after_sec`) | Control | `900` | no | **Live (phaze-ul2v) — lost-submit reaper.** Seconds a `cloud_job` may sit `UPLOADED` with no submit enqueued before the reconcile reaper spills it back to awaiting. Much tighter than the `UPLOADING` bound: `report_uploaded` enqueues `submit_cloud_job` in the same transaction, so nothing legitimately dwells here. Bounded `gt=0, lt=604800`. |
| `compute_scratch_dir` | ~~`PHAZE_COMPUTE_SCRATCH_DIR`~~ (removed) | Control | *n/a* | no | **REMOVED in Phase 67 (superseded), accessor retired in Phase 73/MCOMP-03.** This flat control-side scratch-dir mirror no longer exists as a settings field; the control plane now reads each compute backend's `scratch_dir` from [`backends.toml`](#backend-registry-backendstoml) directly. **MUST match `cloud_scratch_dir`** on the compute agent (a drift surfaces as a sha256/transfer failure). |
| `cloud_scratch_dir` | `PHAZE_CLOUD_SCRATCH_DIR` (or `cloud_scratch_dir`) | Agent | `None` | no | Remote scratch directory on the compute agent where pushed files land and are later read by `process_file`. **MUST match the control-plane compute backend's `scratch_dir` in [`backends.toml`](#backend-registry-backendstoml)** (the flat control-side `compute_scratch_dir` field was **removed in Phase 67**); it is also the cloud-agent compose's named-volume mount path. |
| `push_ssh_host` | `PHAZE_PUSH_SSH_HOST` (or `push_ssh_host`) | Agent | `None` | no | Hostname/IP of the rsync-over-SSH push target (the compute agent). Operator-provisioned in Phase 51. |
| `push_ssh_user` | `PHAZE_PUSH_SSH_USER` (or `push_ssh_user`) | Agent | `None` | no | SSH username for the rsync push target. |
| `push_timeout_sec` | `PHAZE_PUSH_TIMEOUT_SEC` (or `push_timeout_sec`) | Agent | `600` | no | rsync I/O-stall timeout (seconds) for a single `push_file` transfer; MUST stay below the SAQ `push_file` job timeout so the kill is deterministic. Bounded `gt=0, lt=86400`. |
| `push_connect_timeout_sec` | `PHAZE_PUSH_CONNECT_TIMEOUT_SEC` (or `push_connect_timeout_sec`) | Agent | `30` | no | SSH connect-handshake timeout (seconds) for the rsync push. Bounded `gt=0, lt=3600`. |
| `push_ssh_key` | `PHAZE_PUSH_SSH_KEY` (or `push_ssh_key`) | Agent | `None` | **YES** (whitespace **preserved**) | SSH identity private key for the rsync push, file-mounted via `PHAZE_PUSH_SSH_KEY_FILE`. Never logged. |
| `push_known_hosts` | `PHAZE_PUSH_KNOWN_HOSTS` (or `push_known_hosts`) | Agent | `None` | **YES** (whitespace **preserved**) | Pinned `known_hosts` for strict SSH host-key checking of the push target, file-mounted via `PHAZE_PUSH_KNOWN_HOSTS_FILE`. Must be re-provisioned with the compute agent's host key after it comes up. Never logged. |
| `agent_token` | `PHAZE_AGENT_TOKEN` (or `AGENT_TOKEN`) | Agent | required | **YES** | Bearer token the compute agent authenticates with (same field as any agent). File-mount via `PHAZE_AGENT_TOKEN_FILE`. |
| `worker_max_jobs` | `WORKER_MAX_JOBS` | all | `8` | no | **Agent concurrency** — concurrent SAQ jobs per worker. On the 12 GB Always-Free A1, set this to **`1`**: a single concurrent analysis is RAM-bound on that shape. |
| *n/a (raw env var)* | `PHAZE_AGENT_QUEUE` | Agent | required | no | **Cloud queue name** the compute agent consumes (`phaze-agent-<agent_id>`). ⚠️ This is the single structural exception below — it is **not** a pydantic-settings field, so it has **no alias** and no `_FILE` support. |

### Kube submit/reconcile settings (Phase 54, v6.0)

Phase 54 (v6.0 Kubernetes Burst) adds Kueue-cluster analysis: the control plane submits **suspended Kueue Jobs** via the kube API, watches them to completion, and reconciles their status. These knobs are the kube client surface the submit seam, submit task, and reconcile cron read.

> **Superseded in 2026.7.1 (Phase 67).** In v6.0 these were flat `PHAZE_KUBE_*` env vars gated by `PHAZE_CLOUD_TARGET=k8s`; that selector and the flat `kube_*` knobs (plus the `_enforce_kube_config_when_k8s` validator) were **removed with no shim**. A Kueue backend now declares this config under its `[[backends]] kind="kueue"` → `[backends.kube]` submodel in [`backends.toml`](#backend-registry-backendstoml), and the discriminated-union submodel requires the API URL / namespace / local-queue **per backend** at startup (via `_validate_registry`). The rows below are retained as a **historical field reference**; kube credentials live on the **control plane only** (the agent and pod never receive them) as inline `*_file` secret pointers in `backends.toml`.

| Knob | Env var (alias) | Class | Default | `_FILE`? | Description |
|------|-----------------|-------|---------|----------|-------------|
| `cloud_submit_max_attempts` | `PHAZE_CLOUD_SUBMIT_MAX_ATTEMPTS` (or `cloud_submit_max_attempts`) | Control | `3` | no | Max kube Job **submit** attempts before a file is marked `ANALYSIS_FAILED` (Phase 54, D-08). A **distinct** budget from `push_max_attempts` (the rsync leg). Bounded `gt=0, lt=20`. |
| `kube_api_url` | `[backends.kube]` `api_url` | Control | `None` | no | Kubernetes API server URL the control plane submits/watches Jobs against. **Required on a `kind="kueue"` backend** (its `[backends.kube]` submodel validates it at startup). |
| `kube_namespace` | `[backends.kube]` `namespace` | Control | `None` | no | Namespace the Kueue Jobs are submitted into. **Required on a `kind="kueue"` backend** (submodel-validated at startup). |
| `kube_local_queue` | `[backends.kube]` `local_queue` | Control | `None` | no | Kueue LocalQueue name stamped on submitted Jobs (`kueue.x-k8s.io/queue-name` label). **Required on a `kind="kueue"` backend** (submodel-validated at startup). |
| `kube_job_image` | `[backends.kube]` `job_image` | Control | `None` | no | Container image the submitted analysis Job runs. Optional. |
| `kube_job_cpu_request` | `[backends.kube]` `cpu_request` | Control | `None` | no | CPU resource request stamped on the submitted Job's pod spec (e.g. `2`). Optional. |
| `kube_job_memory_request` | `[backends.kube]` `memory_request` | Control | `None` | no | Memory resource request stamped on the submitted Job's pod spec (e.g. `4Gi`). Optional. |
| `kube_workload_api_version` | `[backends.kube]` `workload_api_version` | Control | `kueue.x-k8s.io/v1beta1` | no | apiVersion of the Kueue Workload/Job resources the control plane submits and reconciles. |
| `kube_ca_secret_name` | `[backends.kube]` `ca_secret_name` | Control | `phaze-internal-ca` | no | Name of the **operator-created** `core/v1` Secret (key `phaze-ca.crt`) holding the internal CA cert. The suspended Job mounts it read-only at `/certs` and sets `PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt`, so the one-shot pod verifies the control-plane TLS chain. The CA is **not baked** into the Job image (KDEPLOY-06, reversing KJOB-05); rotation is a Secret update + re-submit, no rebuild. phaze references it by name only — see [k8s-burst.md §6](k8s-burst.md). |
| `models_pvc_name` | `[backends.kube]` `models_pvc_name` only (no env var) | Control | `None` | no | **Optional.** Name of an **operator-provisioned** `ReadOnlyMany` PersistentVolumeClaim pre-populated with the essentia weights. When set, the suspended Job mounts it **read-only** at `/models` (which **must** equal the agent-env ConfigMap's `PHAZE_MODELS_DIR`), so the analyze pod reads its weights from provisioned storage — never a fat image or a runtime download (the Job image stays weights-free). phaze **creates no PV/PVC** and references the claim **by name only**; the PVC carries **only** model weights, never secrets/certs. A plain Kubernetes object name, **not** a secret. Unset ⇒ no models volume/mount (byte-identical manifest). See [k8s-burst.md](k8s-burst.md) "Models provisioning". |
| `kube_kubeconfig` | `[backends.kube]` `kubeconfig` (inline `kubeconfig_file` pointer) | Control | `None` | no (TOML `*_file`) | Kubeconfig contents for the control plane's kube client (`SecretStr`), loaded via the inline `kubeconfig_file` secret pointer in `backends.toml` — no env var. Never logged. |
| `kube_sa_token` | `[backends.kube]` `sa_token` (inline `sa_token_file` pointer) | Control | `None` | no (TOML `*_file`) | ServiceAccount bearer token for the control plane's kube client (`SecretStr`), loaded via the inline `sa_token_file` secret pointer in `backends.toml` — no env var. Never logged. |

### S3 object-staging settings (Phase 53, v6.0)

Phase 53 (v6.0) adds the **S3 object-staging leg** a Kueue backend needs: an ephemeral Kueue Job pod has no persistent local disk, so the control plane presigns a multipart **PUT** (the file-server agent uploads the long file's bytes over the presigned URL — it never sees bucket credentials) and a just-in-time **GET** (the pod downloads at startup), then deletes the staged object on every terminal outcome with a bucket-lifecycle TTL backstop. Works against **any** S3-compatible backend (MinIO / Backblaze / AWS / …) via an explicit `endpoint`. The control plane is the **only** holder of bucket credentials (the agent and pod are credential-free; KSTAGE-02 / T-53-01), so both credential fields honor the `_FILE` convention.

> **Superseded in 2026.7.1 (Phase 67, REG-05).** In v6.0 these were flat `PHAZE_S3_*` env vars gated by `PHAZE_CLOUD_TARGET=k8s` (with the `_enforce_s3_config_when_k8s` validator). That selector, the flat `s3_*` knobs, and the single-shared-bucket assumption were **removed with no shim**. Staging buckets now live in the **[`[[buckets]]` registry](#backend-registry-backendstoml)** (REG-05) — one or more S3-compatible buckets a Kueue backend references by `id`, each with its own inline `*_file` credential pointers. The rows below are retained as a **historical field reference** for the per-bucket fields.

All bounded knobs (`gt`/`ge`/`lt`) reject an out-of-range operator value at startup so a misconfig never reaches the presign/upload code path (T-53-03).

| Knob | Env var (alias) | Class | Default | `_FILE`? | Description |
|------|-----------------|-------|---------|----------|-------------|
| `endpoint` | `[[buckets]]` `endpoint_url` | Control | `None` | no | S3-compatible endpoint URL (e.g. `https://s3.us-west-1.amazonaws.com` or a MinIO/Backblaze URL). Must be a well-formed **http(s)** URL with a host — a scheme-less or non-http value is rejected at registry validation (T-53-02 SSRF surface). **Required on each `[[buckets]]` entry** — the current TOML key is `endpoint_url`. |
| `id` (bucket name) | `[[buckets]]` `id` + `bucket` | Control | `None` | no | The old flat `s3_bucket` splits into two required `[[buckets]]` keys: `id` (the **registry key** a Kueue backend's `buckets` list references) and `bucket` (the **actual S3 bucket name** staging objects are written to). Both required on each `[[buckets]]` entry. |
| `s3_region` | `[[buckets]]` `region` | Control | `None` | no | S3 region (e.g. `us-west-1`). Optional for many S3-compatible backends. |
| `s3_addressing_style` | `[[buckets]]` `addressing_style` | Control | `path` | no | S3 addressing style. `path` (default) maximizes S3-compatible-backend support; `virtual` for AWS virtual-hosted-style. |
| `s3_access_key_id` | `[[buckets]]` `access_key_id` (inline `access_key_id_file` pointer) | Control | `None` | no (TOML `*_file`) | Per-bucket S3 access key id (`SecretStr`, control-plane only), loaded via the inline `access_key_id_file` secret pointer in `backends.toml` — no env var. KSTAGE-02 / T-53-01. Never logged. |
| `s3_secret_access_key` | `[[buckets]]` `secret_access_key` (inline `secret_access_key_file` pointer) | Control | `None` | no (TOML `*_file`) | Per-bucket S3 secret access key (`SecretStr`, control-plane only), loaded via the inline `secret_access_key_file` secret pointer in `backends.toml` — no env var. KSTAGE-02 / T-53-01. Never logged. |
| `s3_presign_put_ttl_sec` | `PHAZE_S3_PRESIGN_PUT_TTL_SEC` (or `s3_presign_put_ttl_sec`) | Control | `3600` | no | TTL (seconds) for the presigned multipart PUT/part URLs minted for the upload leg. Bounded `gt=0, lt=86400`. |
| `s3_presign_get_ttl_sec` | `PHAZE_S3_PRESIGN_GET_TTL_SEC` (or `s3_presign_get_ttl_sec`) | Control | `900` | no | TTL (seconds) for the just-in-time presigned GET URL minted at pod startup. Default 900 (short — minted post-admission so it never expires during a Kueue wait). Bounded `gt=0, lt=86400`. |
| `s3_lifecycle_ttl_days` | `PHAZE_S3_LIFECYCLE_TTL_DAYS` (or `s3_lifecycle_ttl_days`) | Control | `2` | no | Bucket lifecycle TTL (days) — the backstop that deletes any staged object the inline callback delete missed (KSTAGE-04, D-02). Bounded `gt=0, lt=30`. |
| `s3_multipart_part_size_bytes` | `PHAZE_S3_MULTIPART_PART_SIZE_BYTES` (or `s3_multipart_part_size_bytes`) | Control | `67108864` | no | Multipart upload part size (bytes) the agent streams over presigned part URLs. Default 67108864 (64 MiB); bounded to the S3 `[5 MiB, 5 GiB)` part-size range (`ge=5242880, lt=5368709120`). |
| `s3_client_timeout_sec` | `PHAZE_S3_CLIENT_TIMEOUT_SEC` (or `s3_client_timeout_sec`) | Control | `30` | no | **Live (phaze-1v37).** Explicit connect + read timeout (seconds) bounding every control-side S3 SDK call (complete/abort/delete multipart). Without it botocore's minute-scale defaults + retries let a wedged/blackholed S3 endpoint pin the calling connection for minutes. Bounded `gt=0, lt=600`. |

### Fail-fast startup validators vs. the non-fatal runtime LocalQueue probe

Two **distinct** guard layers protect the Kueue path, and it is worth keeping them apart:

- **Startup fail-fast (config completeness).** The `_validate_registry` model validator rejects an incomplete `backends.toml` at construction — the controller worker + api refuse to start. For each `kind="kueue"` backend the discriminated-union submodel requires its `[backends.kube]` API URL / namespace / local-queue and a non-empty resolved `[[buckets]]` set (the S3→Kueue byte path); a `kind="compute"` backend requires its `scratch_dir`. (In v6.0 these were three flat per-`cloud_target` validators — `_enforce_s3_config_when_k8s`, `_enforce_kube_config_when_k8s`, `_enforce_compute_scratch_dir_when_a1` — all removed in Phase 67 when the flat selector went away.)
- **Runtime non-fatal LocalQueue admission probe (warn + surface).** A correctly-*configured* local-queue can still point at a LocalQueue/ClusterQueue that the cluster admin has mis-set so Kueue never **admits** the Job. That is a *cluster-side* condition phaze cannot detect at startup, so it is handled at runtime, **non-fatally**: the `*/5` `reconcile_cloud_jobs` cron maps an `Inadmissible` Workload condition to a warning log and an **Inadmissible** operator-alert card on the pipeline dashboard (it clears when admission recovers). It never crashes the controller — the value was *present* (so startup passed); only the cluster's admission of it is wrong.

In short: **incomplete backend config → startup crash**; **present-but-unadmittable LocalQueue → live dashboard warning**. Cluster-side setup of the Kueue ResourceFlavor / ClusterQueue / LocalQueue, the namespaced RBAC Role, and the `_FILE`-mounted Secret lives in [k8s-burst.md](k8s-burst.md).

### ⚠️ `PHAZE_AGENT_QUEUE` is the one knob NOT configurable via pydantic-settings

Every cloud-burst parameter above is a [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) field **except `PHAZE_AGENT_QUEUE`**. The agent worker (`phaze.tasks.agent_worker`) must hand SAQ a `Queue` object at **module import time**, which is **before** `get_settings()` constructs the settings instance (Phase 26 D-16). So the queue name is read as a **raw `os.environ` lookup at SAQ import time**, not through a settings field — it therefore cannot honor `_FILE` resolution or a settings alias, and it remains a **required operator env var**. This is intentional and structural (moving it into the settings class would fight the import-time ordering), not an omission. By convention it MUST equal `phaze-agent-<PHAZE_AGENT_ID>`; the worker asserts this against the agent_id resolved from its token at startup and exits non-zero on mismatch. Use the exact value `phaze agents add` prints (see [deployment.md](deployment.md) Step 3).

### Cloud target (removed in Phase 67)

> **Removed in 2026.7.1 (Phase 67, REG-01/04) — no back-compat shim.** The flat `PHAZE_CLOUD_TARGET` selector was **removed outright**. Nothing in `src/phaze/` reads it; because `model_config` is `extra="ignore"`, a stale `PHAZE_CLOUD_TARGET=…` left in a live `.env` is **silently dropped** (not honored) — it does **not** pin routing. Delete it. Backend selection, caps, and per-cluster/bucket config now come from the declarative **[Backend registry](#backend-registry-backendstoml)** (`backends.toml`).

**Why it changed.** One global `local|a1|k8s` selector could name only a **single** target. The registry lets you declare *N* backends at once (local + one-or-more Kueue clusters + one-or-more compute agents), each with a cost `rank` and concurrency `cap`, and the tiered scheduler drains long files across them by rank (top-ranked/cheapest first, spilling to the next when a lane is at `cap`). See [cloud-burst.md](cloud-burst.md) / [k8s-burst.md](k8s-burst.md).

**1:1 `cloud_target`→`backends` equivalence.** For anyone reading an old config, each former `cloud_target` value maps to a trivial one-entry registry:

| Old `cloud_target` | Equivalent `backends.toml` |
|--------------------|-----------------------------|
| `local` (default)  | **No file needed** — an absent `backends.toml` synthesizes an implicit single `kind="local"` backend (all-local). |
| `a1`               | One `[[backends]]` with `kind="compute"` (its `scratch_dir` replaces the old flat compute-scratch knob). |
| `k8s`              | One `[[backends]]` with `kind="kueue"` (its `[backends.kube]` submodel carries the former flat `kube_*` knobs) **plus** the `[[buckets]]` entry it stages through (the former flat `s3_*` knobs). |

There is **no migration runbook and no removal date** — the flat knobs are already gone; this table exists only to read an old config at a glance.

**Reverting all routing to local (incident response).** This no longer means editing a selector and restarting. The pipeline header carries a **force-local** master toggle (BEUI-02) that writes a durable `route_control` row and gates both the drain and the duration router **live** — reversible, no redeploy. Files already held `AWAITING_CLOUD` when force-local engages **stay held** (the drain no-ops); new long files route local. See [runbook.md → Force-local incident revert](runbook.md#force-local-incident-revert).

## Logging / observability (all roles)

Phaze routes every process's logs — native app logs plus foreign stdlib / uvicorn / SAQ
logs — through a single [structlog](https://www.structlog.org/) pipeline configured once per
OS process. Both knobs live on `BaseSettings`, so they apply identically to the api, the SAQ
workers (control + agent), the watcher, and the CLI/scripts.

| Variable          | Required | Default                          | Description                                                                                          |
|-------------------|----------|----------------------------------|------------------------------------------------------------------------------------------------------|
| `PHAZE_LOG_LEVEL` | No       | `INFO`                           | Root log level: `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. Set `DEBUG` for verbose per-file / intermediate detail. |
| `PHAZE_LOG_JSON`  | No       | auto (JSON when stdout is not a TTY) | `true` = one JSON object per line (production / Docker); `false` = human-friendly console; unset = auto. |

INFO proves work is happening — model downloads, scans (`scan started` / `scan progress` /
`scan completed`), fingerprints, metadata extraction, executions, Discogs/tracklist matching,
and per-agent task enqueues all emit at INFO. `DEBUG` adds per-file (`file discovered`,
`model ok`) and intermediate detail; the 30-second agent heartbeat background task stays at
DEBUG so it never floods INFO. To watch a running scan in detail: `PHAZE_LOG_LEVEL=DEBUG`.

## Fingerprint service settings (all roles)

The fingerprint sidecars are validated to live on the agent's local Compose network only — `audfprint_url`/`panako_url` must resolve to `localhost`, `127.0.0.1`, `audfprint`, or `panako`. Cross-file-server fingerprint matching is not supported in v4.0.

| Variable        | Required | Default                 | Description                            |
|-----------------|----------|-------------------------|----------------------------------------|
| `AUDFPRINT_URL` | No       | `http://audfprint:8001` | Audfprint fingerprint service endpoint.|
| `PANAKO_URL`    | No       | `http://panako:8002`    | Panako fingerprint service endpoint.   |

## Internal agent API settings (all roles)

| Variable               | Required | Default          | Description                                                   |
|------------------------|----------|------------------|---------------------------------------------------------------|
| `AGENT_TOKEN_PREFIX`   | No       | `phaze_agent_`   | Required prefix for agent bearer tokens.                      |
| `AGENT_FILE_CHUNK_MAX` | No       | `1000`           | Max file records per chunk in the internal agent API.         |

## Bring-up settings (all roles)

| Variable                  | Required | Default | Description                                                                                   |
|---------------------------|----------|---------|-----------------------------------------------------------------------------------------------|
| `PHAZE_AUTO_MIGRATE`      | No       | `true`  | Run `alembic upgrade head` in the api lifespan startup. Set `false` in production to gate migrations behind a maintenance window. |
| `PHAZE_DEV_SEED_AGENT`    | No       | `false` | On a fresh `agents` table, seed a single dev-agent row so the watcher can authenticate on first start. Keep `false` in production. |
| `PHAZE_DEV_AGENT_TOKEN`   | No       | (random)| Optional fixed bearer for the dev-seeded agent. If unset, the api generates a random one and logs it at INFO. Format: `phaze_agent_<32 urlsafe-base64 bytes>`. |

## HTTPS / internal CA settings (Phase 29)

The application server generates a self-signed CA + leaf certificate pair into the certs directory on first startup (idempotent). The pre-uvicorn entrypoint ([`src/phaze/entrypoint.py`](../src/phaze/entrypoint.py)) reads three env vars directly (it must not load `phaze.config`):

| Variable             | Required | Default                       | Description                                                                 |
|----------------------|----------|-------------------------------|-----------------------------------------------------------------------------|
| `PHAZE_CERTS_DIR`    | No       | `/certs`                      | Directory the cert bootstrap writes to and uvicorn loads TLS material from (bind-mount target). |
| `PHAZE_API_HOST`     | No       | `localhost`                   | CN baked into the auto-generated leaf certificate.                          |
| `PHAZE_API_TLS_SANS` | No       | `localhost,127.0.0.1,api`     | Comma-separated SAN list for the leaf cert. Production should add the app server's LAN hostname / IP. |

`PHAZE_API_TLS_SANS` is also a `BaseSettings` field (`api_tls_sans`) so other parts of the app can read the same value.

## Control role settings (`PHAZE_ROLE=control`)

These fields exist only on `ControlSettings` (the application server).

### Deployment mode

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PHAZE_CONTROL_ENV` (or `control_env`) | No | `dev` | Control-plane deployment mode: `dev` or `production`. `production` refuses passwordless Redis URLs on the api/controller (phaze-hti8; mirrors the agent-side `PHAZE_AGENT_ENV` guard). `dev` permits passwordless Redis so a fresh clone works without extra ceremony. |

### LLM / litellm settings

| Variable                  | Required | Default                      | Description                                       |
|---------------------------|----------|------------------------------|---------------------------------------------------|
| `LLM_MODEL`               | No       | `claude-sonnet-4-20250514`   | LLM model used for filename/path proposals.       |
| `ANTHROPIC_API_KEY`       | No*      | (none)                       | Anthropic API key (`SecretStr`). Required only if using an Anthropic model. |
| `OPENAI_API_KEY`          | No*      | (none)                       | OpenAI API key (`SecretStr`). Required only if using an OpenAI model. |
| `LLM_MAX_RPM`             | No       | `30`                         | Max LLM requests per minute.                      |
| `LLM_BATCH_SIZE`          | No       | `10`                         | Files per LLM batch call.                         |
| `LLM_MAX_COMPANION_CHARS` | No       | `3000`                       | Max characters of companion-file content sent per file. |

\* Neither key is required by the config schema, but at least one matching the selected `LLM_MODEL` provider is needed to generate proposals at runtime.

### Discogs settings

| Variable                    | Required | Default                       | Description                          |
|-----------------------------|----------|-------------------------------|--------------------------------------|
| `DISCOGSOGRAPHY_URL`        | No       | `http://discogsography:8000`  | Discogsography service endpoint.     |
| `DISCOGS_MATCH_CONCURRENCY` | No       | `5`                           | Concurrent Discogs match tasks.      |

### Pipeline dashboard settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PHAZE_STRAGGLER_THRESHOLD_SEC` (or `straggler_threshold_sec`) | No | `6600` | Running-age threshold (seconds) above which an in-flight `process_file` analyze job is flagged a **straggler** on the pipeline dashboard (Phase 44) — still grinding, distinct from `ANALYSIS_FAILED` which gave up. Default 6600 mirrors the agent's `analysis_inner_timeout_sec` (a job past the inner-timeout horizon is by definition overdue). Bounded `gt=0, lt=86400`. |

## Agent role settings (`PHAZE_ROLE=agent`)

These fields exist only on `AgentSettings` (the file server). When `PHAZE_ROLE=agent`, a model validator fails fast at startup if any **required** field is missing.

### Required agent fields

| Variable                                      | Required | Default | Description                                                                 |
|-----------------------------------------------|----------|---------|-----------------------------------------------------------------------------|
| `PHAZE_AGENT_API_URL` (or `AGENT_API_URL`)    | **Yes**  | (empty) | Base URL of the application server (e.g., `http://api:8000` in Compose). In `production` mode this must be `https://`. |
| `PHAZE_AGENT_TOKEN` (or `AGENT_TOKEN`)        | **Yes**  | (empty) | Bearer token (`SecretStr`) issued at agent registration. Must match the stored hash in the `agents` table. Format: `phaze_agent_<32 urlsafe-base64 bytes>`. |
| `PHAZE_AGENT_SCAN_ROOTS` (or `SCAN_ROOTS`)    | **Yes**  | (empty) | Comma-separated list of absolute paths the agent may read/write, used for path-traversal containment (e.g., `/data/music,/data/concerts`). |
| `PHAZE_AGENT_QUEUE` (no alias)                | **Yes**  | (empty) | SAQ queue the agent worker consumes. ⚠️ Not a pydantic-settings field — a raw `os.environ` read at SAQ import time, so no bare-name alias and no `_FILE` support (see the cloud-burst section's structural-exception note). By convention it MUST equal `phaze-agent-<PHAZE_AGENT_ID>`. There is **no queue column** on the `agents` table: both the control plane and the agent worker derive the queue name from the agent_id. At startup `phaze.tasks.agent_worker` resolves the agent_id from the token via `/whoami` and asserts `PHAZE_AGENT_QUEUE == f"phaze-agent-{agent_id}"`, exiting non-zero on mismatch. Use the exact value printed by `phaze agents add` (see [deployment.md](deployment.md) Step 3). |

### Optional agent fields

| Variable                                          | Required | Default              | Description                                                                 |
|---------------------------------------------------|----------|----------------------|-----------------------------------------------------------------------------|
| `PHAZE_AGENT_KIND` (or `kind`)                    | No       | `fileserver`         | Agent capability marker: `fileserver` (owns media + scan roots) or `compute` (cloud agent — owns no media/scan roots, which relaxes the empty-`scan_roots` required-field gate). Phase 48; middle layer of the 3-layer kind defense (CLI `choices=` + DB CHECK bracket it). |
| `PHAZE_AGENT_ENV` (or `AGENT_ENV`)                | No       | `dev`                | Deployment mode: `dev` or `production`. `production` enforces `https://` agent URL and a passworded Redis URL. |
| `PHAZE_AGENT_CA_FILE` (or `AGENT_CA_FILE`)        | No       | `/certs/phaze-ca.crt`| Path to the operator-distributed CA cert the agent's HTTP client uses to verify the app-server TLS endpoint. |
| `PHAZE_WATCHER_SETTLE_SECONDS` (or `WATCHER_SETTLE_SECONDS`) | No | `10` | Seconds a file's mtime must be stable before the watcher posts it.          |
| `PHAZE_WATCHER_MAX_PENDING_SECONDS` (or `WATCHER_MAX_PENDING_SECONDS`) | No | `3600` | Stuck-file cap; pending entries older than this are evicted without posting.|
| `PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS` (or `WATCHER_SWEEP_INTERVAL_SECONDS`) | No | `2` | How often the watcher's sweep task checks for settled files.               |
| `PHAZE_WATCHER_POLLING_MODE` (or `WATCHER_POLLING_MODE`) | No | `false` | Use watchdog's `PollingObserver` instead of native inotify. Required for macOS Docker bind mounts where inotify events do not propagate. |
| `PHAZE_SCAN_CHUNK_SIZE` (or `SCAN_CHUNK_SIZE`)    | No       | `500`                | Number of file-upsert rows per chunk in `scan_directory`.                   |

### Agent analysis tuning (windowed analysis — Phase 31/43/57.1)

The agent worker reads these to size the per-window decode loop in `services/analysis.py::analyze_file` (windowed time-series audio analysis).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PHAZE_ANALYSIS_FINE_WINDOW_SEC` (or `analysis_fine_window_sec`) | No | `30` | Fine-tier (BPM/key) window length in seconds (Phase 31). |
| `PHAZE_ANALYSIS_COARSE_WINDOW_SEC` (or `analysis_coarse_window_sec`) | No | `180` | Coarse-tier (mood/style/danceability) window length in seconds (Phase 31). |
| `PHAZE_ANALYSIS_FINE_MIN_SEC` (or `analysis_fine_min_sec`) | No | `15` | Minimum audio length for a trailing FINE window; shorter trailing windows are dropped except window 0 (Phase 31). |
| `PHAZE_ANALYSIS_INNER_TIMEOUT_SEC` (or `analysis_inner_timeout_sec`) | No | `6600` | Inner per-analysis timeout (kill-on-timeout). The Phase 101 subprocess driver (`services/analysis_exec.py`) `SIGKILL`s the analysis child past this bound (retired the earlier pebble ProcessPool from Phase 43). MUST stay below the 7200s SAQ `process_file` net so the kill is deterministic; enforced `gt=0, lt=7200`. |
| `PHAZE_ANALYSIS_FINE_CAP` (or `analysis_fine_cap`) | No | `60` | Max FINE-tier (BPM/key) windows `analyze_file` decodes per file. Bounded `ge=2` (even-stride keeps first+last) (Phase 43). |
| `PHAZE_ANALYSIS_COARSE_CAP` (or `analysis_coarse_cap`) | No | `30` | Max COARSE-tier (mood/style/danceability) windows `analyze_file` decodes per file. Bounded `ge=2` (Phase 43). |
| `PHAZE_ANALYSIS_PROGRESS_INTERVAL_SEC` (or `analysis_progress_interval_sec`) | No | `5.0` | Minimum seconds between mid-flight analyze-progress POSTs; the final count is always flushed regardless, and `0` disables throttling. Bounded `ge=0.0` (Phase 57.1 D-04). |

## Docker Compose-only variables

These are consumed by the Compose stack (`docker-compose.yml`, `docker-compose.agent.yml`), not by `phaze.config`.

| Variable           | Required | Default       | Description                                                                 |
|--------------------|----------|---------------|-----------------------------------------------------------------------------|
| `POSTGRES_USER`    | No       | `phaze`       | PostgreSQL superuser for the `postgres` service.                            |
| `POSTGRES_PASSWORD`| No       | `phaze`       | PostgreSQL password for the `postgres` service.                            |
| `POSTGRES_DB`      | No       | `phaze`       | PostgreSQL database name created on first boot.                            |
| `REDIS_PASSWORD`   | **Yes**  | (none)        | Password for `redis-server --requirepass`. Compose fails at parse time if unset (`${REDIS_PASSWORD:?...}`). `.env.example` ships a `changeme` placeholder for dev. |
| `REDIS_BIND_IP`    | No       | `127.0.0.1`   | Host interface to bind Redis `:6379` on. Production overrides to a LAN IP so off-host agents can connect. |
| `UID`              | No       | `1000`        | Host user ID for volume permissions.                                       |
| `GID`              | No       | `1000`        | Host group ID for volume permissions.                                      |
| `CA_PATH`          | No       | `./certs`     | Host path bind-mounted read-only to `/certs` in agent containers (operator-distributed CA cert). |
| `PHAZE_IMAGE_TAG`  | No       | `latest`      | GHCR image tag pulled by `docker-compose.agent.yml` (e.g., `2026.7.0`).      |

## Config file format

Phaze has no JSON/YAML/TOML application config file. All runtime configuration flows through environment variables (loaded from a `.env` file via pydantic-settings, `env_file=".env"`). Unknown env vars are ignored (`extra="ignore"`).

A minimal `.env` for a single-host dev bring-up:

```bash
# Database + queue broker + Redis cache (Docker service names)
DATABASE_URL=postgresql+asyncpg://phaze:phaze@postgres:5432/phaze
PHAZE_QUEUE_URL=postgresql://phaze:phaze@postgres:5432/phaze   # libpq form, NOT +asyncpg
REDIS_URL=redis://redis:6379/0
REDIS_PASSWORD=changeme

# App
SCAN_PATH=/data/music
MODELS_PATH=./models

# Dev agent bring-up (so the watcher can authenticate on a fresh DB)
PHAZE_DEV_SEED_AGENT=true
PHAZE_AGENT_API_URL=http://api:8000
PHAZE_AGENT_TOKEN=phaze_agent_<token from `docker compose logs api`>
PHAZE_AGENT_SCAN_ROOTS=/data/music
```

## Required vs optional settings

Almost every field has a safe default so a fresh clone runs with `docker compose up`. The settings that cause a **fail-fast at startup** if missing or misconfigured:

- **Agent role (`PHAZE_ROLE=agent`)** — `PHAZE_AGENT_API_URL`, `PHAZE_AGENT_TOKEN`, and `PHAZE_AGENT_SCAN_ROOTS` are all required. The `_enforce_required_agent_fields` model validator raises `ValueError` at construction if any is empty.
- **Redis password (Compose)** — `REDIS_PASSWORD` must be set or `docker compose` aborts at parse time (`${REDIS_PASSWORD:?REDIS_PASSWORD required}`).
- **Fingerprint URLs** — `AUDFPRINT_URL` / `PANAKO_URL` are rejected unless their host is `localhost`, `127.0.0.1`, `audfprint`, or `panako`.

## Defaults

Defaults are defined in `src/phaze/config.py`. Highlights:

- `database_url` → `postgresql+asyncpg://phaze:phaze@postgres:5432/phaze`
- `queue_url` → `postgresql://phaze:phaze@postgres:5432/phaze` (libpq form for the SAQ Postgres broker)
- `redis_url` → `redis://redis:6379/0`
- `api_host` → `0.0.0.0`, `api_port` → `8000`
- `scan_path` → `/data/music`, `output_path` → `/data/output`, `models_path` → `/models`
- `worker_max_jobs` → `8`, `worker_job_timeout` → `600`, `worker_max_retries` → `4`
- `llm_model` → `claude-sonnet-4-20250514`, `llm_max_rpm` → `30`, `llm_batch_size` → `10`
- `agent_env` → `dev`, `agent_ca_file` → `/certs/phaze-ca.crt`
- `watcher_settle_seconds` → `10`, `watcher_sweep_interval_seconds` → `2`, `scan_chunk_size` → `500`

## Per-environment overrides

There are no `.env.development` / `.env.production` files; environment selection is explicit:

- **Host vs container connection strings** — `.env.example` defaults to the Docker service names `postgres` / `redis`. When running a service directly on the host with `uv run`, switch `DATABASE_URL`, `PHAZE_QUEUE_URL`, and `REDIS_URL` to `localhost` (or an SSH tunnel to the home server).
- **Agent dev vs production** — set `PHAZE_AGENT_ENV=production` on agents. This activates two guards:
  - `_enforce_https_in_production` — `agent_api_url` must start with `https://`, otherwise the bearer token travels in cleartext.
  - `_enforce_redis_password_in_production` — `redis_url` must contain a password, paired with the server-side `--requirepass` + LAN-bound port hardening. `dev` (default) permits passwordless Redis so a fresh clone works without extra ceremony.
- **Control plane dev vs production** — set `PHAZE_CONTROL_ENV=production` on the api/controller. This activates the control-plane twin of the Redis guard (phaze-hti8): `control_env=production` refuses a passwordless `redis_url` at startup. Setting only `PHAZE_AGENT_ENV=production` does **not** guard the control plane — the two knobs are independent and must both be set in production.
- **Redis exposure** — keep `REDIS_BIND_IP=127.0.0.1` in dev; set it to the app server's LAN IP in production so agents on other hosts can reach Redis.
- **TLS SANs** — extend `PHAZE_API_TLS_SANS` with the app server's production LAN hostname / IP so agents can verify the TLS handshake.
- **Migrations** — set `PHAZE_AUTO_MIGRATE=false` in production to run Alembic migrations manually during a maintenance window.
- **Agent images** — `docker-compose.agent.yml` pulls `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}`; pin `PHAZE_IMAGE_TAG` (e.g., `2026.7.0`) per deployment.
