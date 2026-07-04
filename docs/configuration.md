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
| `PHAZE_ENABLE_SAQ_UI` (or `enable_saq_ui`) | No | `true`                                          | Mount SAQ's built-in queue-monitoring dashboard at `/saq` in the `phaze-api` app (reusing the lifespan SAQ queues; no second Redis pool, no extra port). Set `false` to skip the mount entirely. See [api.md](api.md) → SAQ Monitoring UI. |

## Worker / task queue settings (all roles)

| Variable                       | Required | Default | Description                                          |
|--------------------------------|----------|---------|------------------------------------------------------|
| `WORKER_MAX_JOBS`              | No       | `8`     | Concurrent SAQ jobs per worker.                      |
| `WORKER_JOB_TIMEOUT`          | No       | `600`   | Per-job timeout in seconds.                          |
| `WORKER_MAX_RETRIES`          | No       | `4`     | Max attempts per job (1 initial + 3 retries).        |
| `WORKER_PROCESS_POOL_SIZE`    | No       | `4`     | CPU-bound process pool size.                         |
| `WORKER_HEALTH_CHECK_INTERVAL`| No       | `60`    | SAQ health-check interval in seconds.                |
| `WORKER_KEEP_RESULT`          | No       | `3600`  | Seconds SAQ retains a finished job's result.         |
| `PHAZE_SCAN_STALL_SECONDS` (or `SCAN_STALL_SECONDS`) | No | `600` | Seconds with no progress before a RUNNING scan is reaped as stalled by the control worker's every-minute cron. Lives on `BaseSettings`, so both roles parse it, but only the control worker runs the reaper. The admin UI flips a RUNNING scan to an amber "stalled?" indicator at **half** this threshold, before the hard reap. |

## Backend registry (`backends.toml`)

**As of 2026.7.1 (Phase 67, REG-01/04/05, D-11/D-12) the typed backend registry is the SOLE cloud config surface.** It replaces the flat `PHAZE_CLOUD_TARGET` selector and the flat `PHAZE_S3_*` / `PHAZE_KUBE_*` / compute-scratch env vars, which were **removed with no back-compat shim**. Instead of one global cloud target, you declare a *registry* of backends (and their staging buckets) in a TOML file.

**Loading + zero-config default.** The registry is loaded from a TOML file pointed at by `PHAZE_BACKENDS_CONFIG_FILE` (default `/etc/phaze/backends.toml`). If the file is **absent**, the control plane synthesizes an **implicit single `kind=local` backend** — an all-local deploy needs **zero** config edits. The registry is sourced **only** from the TOML file (it is deliberately not an env var), and a present-but-empty `backends = []` fails fast at startup rather than silently booting with no backend.

**`[[backends]]` — the analysis backends.** An array-of-tables; each entry is a discriminated union on `kind`:

| Field | Applies to | Description |
|-------|-----------|-------------|
| `id` | all | Unique backend identifier (used in logs + bucket refs). |
| `kind` | all | `local` \| `compute` \| `kueue`. Selects the variant + its required config. |
| `rank` | all | Cost-tier ordering; lower ranks are preferred by the scheduler. |
| `cap` | all | Concurrency cap for this backend (replaces the old flat in-flight window). |
| `scratch_dir` | `compute` | Remote scratch dir the rsync push lands in (was the flat compute-scratch mirror). |
| `[backends.kube]` | `kueue` | Nested Kueue cluster config: API URL, namespace, local-queue, Job image/resources, workload apiVersion, CA/ConfigMap/Secret names, and inline `kubeconfig_file` / `sa_token_file` secret pointers. |
| `buckets` | `kueue` | List of `[[buckets]]` `id`s this Kueue backend stages through. |

**`[[buckets]]` — the S3 staging-bucket registry (REG-05).** An array-of-tables of the S3-compatible staging buckets Kueue backends reference:

| Field | Description |
|-------|-------------|
| `id` | Unique bucket identifier referenced by a backend's `buckets` list. |
| `scope` | `shared` (any number of Kueue backends may reference it) or `cluster-specific` (**at most one** Kueue backend may reference it — a cardinality invariant enforced at startup). |
| `endpoint` | S3-compatible endpoint URL (validated as a well-formed http(s) URL). |
| `region`, `addressing_style` | Optional S3 connection tuning. |
| `access_key_id_file`, `secret_access_key_file` | Inline `*_file` secret pointers (control-plane only; never sent to the agent or pod). |

Whole-registry invariants (enforced by the `_validate_registry` model validator at startup): non-empty registry; every Kueue backend's `buckets` ids resolve to a declared bucket and the resolved set is non-empty; a `cluster-specific` bucket is referenced by at most one Kueue backend. The resolved registry is logged **secret-free** at boot as an `{id, kind, rank, cap}` projection.

The global tuning knobs below (route threshold, retry budgets, S3 presign/lifecycle/part-size) are **not** per-backend and remain env vars on `ControlSettings`.

## Cloud-burst settings

> **Superseded in 2026.7.1 (Phase 67):** the flat `cloud_target` / `cloud_max_in_flight` / compute-scratch and flat `s3_*` / `kube_*` knobs in the tables below were **removed with no shim** — backend selection, caps, cluster config, and bucket config now come from the **[Backend registry](#backend-registry-backendstoml)** above. The rows are retained only as a historical field reference; the **global** knobs still marked as kept (`cloud_route_threshold_sec`, `push_max_attempts`, `cloud_submit_max_attempts`, the `s3_presign_*` / `s3_lifecycle_ttl_days` / `s3_multipart_part_size_bytes` knobs, and the agent-side `cloud_scratch_dir` / push-SSH fields) remain live env vars.

Cloud burst (Phase 49/50/51, v5.0) offloads **long** audio sets (duration ≥ the route threshold) to a free OCI A1 arm64 **compute agent** over Tailscale via an rsync push — instead of letting them time out on the local file server. The full feature walkthrough, runbook, and smoke test live in [cloud-burst.md](cloud-burst.md); this section is the canonical knob reference.

Descriptions are sourced from the `Field(...)` text in [`src/phaze/config.py`](../src/phaze/config.py). The `Class` column is the role the field lives on (`ControlSettings` = the application server that owns routing; `AgentSettings` = the compute agent). All knobs use the `PHAZE_*` (or bare-name) dual form described above unless noted.

| Knob | Env var (alias) | Class | Default | `_FILE`? | Description |
|------|-----------------|-------|---------|----------|-------------|
| `cloud_target` | `PHAZE_CLOUD_TARGET` (or `cloud_target`) | Control | `local` | no | **Master routing selector for the whole feature** (`local` \| `a1` \| `k8s`). `local` (default) reverts to all-local analysis with no other change; `a1` routes long files to the v5.0 OCI A1 compute agent (rsync push); `k8s` stages to S3 and submits a Kueue Job. See *Cloud target* below. **Renamed in v6.0** — replaces the old cloud-burst on/off boolean. |
| `cloud_route_threshold_sec` | `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC` (or `cloud_route_threshold_sec`) | Control | `5400` | no | Duration threshold (seconds) at/above which a file is routed to a cloud compute agent. Default 5400 (90 min); bounded `gt=0, lt=86400` (out-of-range fails fast at startup). |
| `cloud_max_in_flight` | `PHAZE_CLOUD_MAX_IN_FLIGHT` (or `cloud_max_in_flight`) | Control | `2` | no | Max cloud files staged-or-in-flight (`PUSHING`+`PUSHED`); the load-bearing ≤N window — the only backpressure keeping an unbounded backlog off the single compute agent. Bounded `gt=0, lt=100`. |
| `push_max_attempts` | `PHAZE_PUSH_MAX_ATTEMPTS` (or `push_max_attempts`) | Control | `3` | no | Max push attempts before a sha256-mismatched file is marked `ANALYSIS_FAILED`. Bounded `gt=0, lt=20`. |
| `compute_scratch_dir` | `PHAZE_COMPUTE_SCRATCH_DIR` (or `compute_scratch_dir`) | Control | `None` | no | Control-side mirror of the compute agent's scratch directory; the push callback builds the `process_file` scratch path from it. **MUST match `cloud_scratch_dir`** on the compute agent (a drift surfaces as a sha256/transfer failure). |
| `cloud_scratch_dir` | `PHAZE_CLOUD_SCRATCH_DIR` (or `cloud_scratch_dir`) | Agent | `None` | no | Remote scratch directory on the compute agent where pushed files land and are later read by `process_file`. **MUST match `compute_scratch_dir`** on the control plane; it is also the cloud-agent compose's named-volume mount path. |
| `push_ssh_host` | `PHAZE_PUSH_SSH_HOST` (or `push_ssh_host`) | Agent | `None` | no | Hostname/IP of the rsync-over-SSH push target (the compute agent). Operator-provisioned in Phase 51. |
| `push_ssh_user` | `PHAZE_PUSH_SSH_USER` (or `push_ssh_user`) | Agent | `None` | no | SSH username for the rsync push target. |
| `push_timeout_sec` | `PHAZE_PUSH_TIMEOUT_SEC` (or `push_timeout_sec`) | Agent | `600` | no | rsync I/O-stall timeout (seconds) for a single `push_file` transfer; MUST stay below the SAQ `push_file` job timeout so the kill is deterministic. Bounded `gt=0, lt=86400`. |
| `push_connect_timeout_sec` | `PHAZE_PUSH_CONNECT_TIMEOUT_SEC` (or `push_connect_timeout_sec`) | Agent | `30` | no | SSH connect-handshake timeout (seconds) for the rsync push. Bounded `gt=0, lt=3600`. |
| `push_ssh_key` | `PHAZE_PUSH_SSH_KEY` (or `push_ssh_key`) | Agent | `None` | **YES** (whitespace **preserved**) | SSH identity private key for the rsync push, file-mounted via `PHAZE_PUSH_SSH_KEY_FILE`. Never logged. |
| `push_known_hosts` | `PHAZE_PUSH_KNOWN_HOSTS` (or `push_known_hosts`) | Agent | `None` | **YES** (whitespace **preserved**) | Pinned `known_hosts` for strict SSH host-key checking of the push target, file-mounted via `PHAZE_PUSH_KNOWN_HOSTS_FILE`. Must be re-provisioned with the compute agent's host key after it comes up. Never logged. |
| `agent_token` | `PHAZE_AGENT_TOKEN` (or `AGENT_TOKEN`) | Agent | required | **YES** | Bearer token the compute agent authenticates with (same field as any agent). File-mount via `PHAZE_AGENT_TOKEN_FILE`. |
| `worker_max_jobs` | `WORKER_MAX_JOBS` | all | `8` | no | **Agent concurrency** — concurrent SAQ jobs per worker. On the 12 GB Always-Free A1, set this to **`1`**: a single concurrent analysis is RAM-bound on that shape. |
| *n/a (raw env var)* | `PHAZE_AGENT_QUEUE` (or `AGENT_QUEUE`) | Agent | required | no | **Cloud queue name** the compute agent consumes (`phaze-agent-<agent_id>`). ⚠️ This is the single structural exception below — it is **not** a pydantic-settings field. |

### Kube submit/reconcile settings (Phase 54, v6.0)

Phase 54 (v6.0 Kubernetes Burst) adds a third routing target: instead of an rsync push to a single A1, the control plane submits **suspended Kueue Jobs** via the kube API, watches them to completion, and reconciles their status. These knobs are the kube client surface the submit seam, submit task, and reconcile cron read. They are selected by `PHAZE_CLOUD_TARGET=k8s`: when the target is `k8s`, `kube_api_url`, `kube_namespace`, and `kube_local_queue` are **required** and fail fast at startup if unset (the per-target validator added in v6.0); for any other target they stay optional. Kube credentials live on the **control plane only** (the agent and pod never receive them) and honor the `_FILE` convention.

| Knob | Env var (alias) | Class | Default | `_FILE`? | Description |
|------|-----------------|-------|---------|----------|-------------|
| `cloud_submit_max_attempts` | `PHAZE_CLOUD_SUBMIT_MAX_ATTEMPTS` (or `cloud_submit_max_attempts`) | Control | `3` | no | Max kube Job **submit** attempts before a file is marked `ANALYSIS_FAILED` (Phase 54, D-08). A **distinct** budget from `push_max_attempts` (the rsync leg). Bounded `gt=0, lt=20`. |
| `kube_api_url` | `PHAZE_KUBE_API_URL` (or `kube_api_url`) | Control | `None` | no | Kubernetes API server URL the control plane submits/watches Jobs against. **Required when `cloud_target=k8s`** (fail-fast at startup); optional otherwise. |
| `kube_namespace` | `PHAZE_KUBE_NAMESPACE` (or `kube_namespace`) | Control | `None` | no | Namespace the Kueue Jobs are submitted into. **Required when `cloud_target=k8s`** (fail-fast at startup); optional otherwise. |
| `kube_local_queue` | `PHAZE_KUBE_LOCAL_QUEUE` (or `kube_local_queue`) | Control | `None` | no | Kueue LocalQueue name stamped on submitted Jobs (`kueue.x-k8s.io/queue-name` label). **Required when `cloud_target=k8s`** (fail-fast at startup); optional otherwise. |
| `kube_job_image` | `PHAZE_KUBE_JOB_IMAGE` (or `kube_job_image`) | Control | `None` | no | Container image the submitted analysis Job runs. Optional in Phase 54. |
| `kube_job_cpu_request` | `PHAZE_KUBE_JOB_CPU_REQUEST` (or `kube_job_cpu_request`) | Control | `None` | no | CPU resource request stamped on the submitted Job's pod spec (e.g. `2`). Optional in Phase 54. |
| `kube_job_memory_request` | `PHAZE_KUBE_JOB_MEMORY_REQUEST` (or `kube_job_memory_request`) | Control | `None` | no | Memory resource request stamped on the submitted Job's pod spec (e.g. `4Gi`). Optional in Phase 54. |
| `kube_workload_api_version` | `PHAZE_KUBE_WORKLOAD_API_VERSION` (or `kube_workload_api_version`) | Control | `kueue.x-k8s.io/v1beta1` | no | apiVersion of the Kueue Workload/Job resources the control plane submits and reconciles. |
| `kube_ca_secret_name` | `PHAZE_KUBE_CA_SECRET_NAME` (or `kube_ca_secret_name`) | Control | `phaze-internal-ca` | no | Name of the **operator-created** `core/v1` Secret (key `phaze-ca.crt`) holding the internal CA cert. The suspended Job mounts it read-only at `/certs` and sets `PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt`, so the one-shot pod verifies the control-plane TLS chain. The CA is **not baked** into the Job image (KDEPLOY-06, reversing KJOB-05); rotation is a Secret update + re-submit, no rebuild. phaze references it by name only — see [k8s-burst.md §6](k8s-burst.md). |
| `kube_kubeconfig` | `PHAZE_KUBE_KUBECONFIG` (or `kube_kubeconfig`) | Control | `None` | **YES** | Kubeconfig contents for the control plane's kube client, file-mounted via `PHAZE_KUBE_KUBECONFIG_FILE`. Never logged. |
| `kube_sa_token` | `PHAZE_KUBE_SA_TOKEN` (or `kube_sa_token`) | Control | `None` | **YES** | ServiceAccount bearer token for the control plane's kube client, file-mounted via `PHAZE_KUBE_SA_TOKEN_FILE`. Never logged. |

### S3 object-staging settings (Phase 53, v6.0)

Phase 53 (v6.0) adds the **S3 object-staging leg** the `k8s` target needs: an ephemeral Kueue Job pod has no persistent local disk, so the control plane presigns a multipart **PUT** (the file-server agent uploads the long file's bytes over the presigned URL — it never sees bucket credentials) and a just-in-time **GET** (the pod downloads at startup), then deletes the staged object on every terminal outcome with a bucket-lifecycle TTL backstop. Works against **any** S3-compatible backend (MinIO / Backblaze / AWS / …) via an explicit `endpoint_url`. The control plane is the **only** holder of bucket credentials (the agent and pod are credential-free; KSTAGE-02 / T-53-01), so both credential fields honor the `_FILE` convention. These knobs are selected by `PHAZE_CLOUD_TARGET=k8s`: when the target is `k8s`, `s3_bucket` and `s3_endpoint_url` are **required** and fail fast at startup if unset (the `_enforce_s3_config_when_k8s` per-target validator); for any other target (`local` / `a1`) they stay optional (`a1` uses rsync, not S3).

All bounded knobs (`gt`/`ge`/`lt`) reject an out-of-range operator value at startup so a misconfig never reaches the presign/upload code path (T-53-03).

| Knob | Env var (alias) | Class | Default | `_FILE`? | Description |
|------|-----------------|-------|---------|----------|-------------|
| `s3_endpoint_url` | `PHAZE_S3_ENDPOINT_URL` (or `s3_endpoint_url`) | Control | `None` | no | S3-compatible endpoint URL (e.g. `https://s3.us-west-1.amazonaws.com` or a MinIO/Backblaze URL). Must be a well-formed **http(s)** URL with a host — a scheme-less or non-http value is rejected at construction (`_validate_s3_endpoint_url`; T-53-02 SSRF surface). **Required when `cloud_target=k8s`** (fail-fast at startup). |
| `s3_bucket` | `PHAZE_S3_BUCKET` (or `s3_bucket`) | Control | `None` | no | Operator-created bucket used for ephemeral `file_id`-scoped staging objects. **Required when `cloud_target=k8s`** (fail-fast at startup). |
| `s3_region` | `PHAZE_S3_REGION` (or `s3_region`) | Control | `None` | no | S3 region (e.g. `us-west-1`). Optional for many S3-compatible backends. |
| `s3_addressing_style` | `PHAZE_S3_ADDRESSING_STYLE` (or `s3_addressing_style`) | Control | `path` | no | S3 addressing style. `path` (default) maximizes S3-compatible-backend support; `virtual` for AWS virtual-hosted-style. |
| `s3_access_key_id` | `PHAZE_S3_ACCESS_KEY_ID` (or `s3_access_key_id`) | Control | `None` | **YES** | S3 access key id (control-plane only; file-mount via `PHAZE_S3_ACCESS_KEY_ID_FILE`). KSTAGE-02 / T-53-01. Never logged. |
| `s3_secret_access_key` | `PHAZE_S3_SECRET_ACCESS_KEY` (or `s3_secret_access_key`) | Control | `None` | **YES** | S3 secret access key (control-plane only; file-mount via `PHAZE_S3_SECRET_ACCESS_KEY_FILE`). KSTAGE-02 / T-53-01. Never logged. |
| `s3_presign_put_ttl_sec` | `PHAZE_S3_PRESIGN_PUT_TTL_SEC` (or `s3_presign_put_ttl_sec`) | Control | `3600` | no | TTL (seconds) for the presigned multipart PUT/part URLs minted for the upload leg. Bounded `gt=0, lt=86400`. |
| `s3_presign_get_ttl_sec` | `PHAZE_S3_PRESIGN_GET_TTL_SEC` (or `s3_presign_get_ttl_sec`) | Control | `900` | no | TTL (seconds) for the just-in-time presigned GET URL minted at pod startup. Default 900 (short — minted post-admission so it never expires during a Kueue wait). Bounded `gt=0, lt=86400`. |
| `s3_lifecycle_ttl_days` | `PHAZE_S3_LIFECYCLE_TTL_DAYS` (or `s3_lifecycle_ttl_days`) | Control | `2` | no | Bucket lifecycle TTL (days) — the backstop that deletes any staged object the inline callback delete missed (KSTAGE-04, D-02). Bounded `gt=0, lt=30`. |
| `s3_multipart_part_size_bytes` | `PHAZE_S3_MULTIPART_PART_SIZE_BYTES` (or `s3_multipart_part_size_bytes`) | Control | `67108864` | no | Multipart upload part size (bytes) the agent streams over presigned part URLs. Default 67108864 (64 MiB); bounded to the S3 `[5 MiB, 5 GiB)` part-size range (`ge=5242880, lt=5368709120`). |

### Fail-fast startup validators vs. the non-fatal runtime LocalQueue probe

Two **distinct** guard layers protect the `k8s` path, and it is worth keeping them apart:

- **Startup fail-fast (config completeness).** When `cloud_target=k8s`, the already-shipped `ControlSettings` model validators reject an incomplete config at construction — the controller worker + api refuse to start:
  - `_enforce_s3_config_when_k8s` — requires `s3_bucket` and `s3_endpoint_url` (the S3→Kueue byte path).
  - `_enforce_kube_config_when_k8s` — requires `kube_api_url`, `kube_namespace`, and `kube_local_queue` (where/how the Job is submitted).
  - These are deliberately kept as **three separate** per-target validators (the third, `_enforce_compute_scratch_dir_when_a1`, guards the `a1` path) so collapsing them into one `!= "local"` gate can't silently change `a1`'s fail-fast semantics.
- **Runtime non-fatal LocalQueue admission probe (warn + surface).** A correctly-*configured* `kube_local_queue` can still point at a LocalQueue/ClusterQueue that the cluster admin has mis-set so Kueue never **admits** the Job. That is a *cluster-side* condition phaze cannot detect at startup, so it is handled at runtime, **non-fatally**: the `*/5` `reconcile_cloud_jobs` cron maps an `Inadmissible` Workload condition to a warning log and an **Inadmissible** operator-alert card on the pipeline dashboard (it clears when admission recovers). It never crashes the controller — the value was *present* (so startup passed); only the cluster's admission of it is wrong.

In short: **missing K8s/S3 config → startup crash**; **present-but-unadmittable LocalQueue → live dashboard warning**. Cluster-side setup of the Kueue ResourceFlavor / ClusterQueue / LocalQueue, the namespaced RBAC Role, and the `_FILE`-mounted Secret lives in [k8s-burst.md](k8s-burst.md).

### ⚠️ `PHAZE_AGENT_QUEUE` is the one knob NOT configurable via pydantic-settings

Every cloud-burst parameter above is a [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) field **except `PHAZE_AGENT_QUEUE`**. The agent worker (`phaze.tasks.agent_worker`) must hand SAQ a `Queue` object at **module import time**, which is **before** `get_settings()` constructs the settings instance (Phase 26 D-16). So the queue name is read as a **raw `os.environ` lookup at SAQ import time**, not through a settings field — it therefore cannot honor `_FILE` resolution or a settings alias, and it remains a **required operator env var**. This is intentional and structural (moving it into the settings class would fight the import-time ordering), not an omission. By convention it MUST equal `phaze-agent-<PHAZE_AGENT_ID>`; the worker asserts this against the agent_id resolved from its token at startup and exits non-zero on mismatch. Use the exact value `phaze agents add` prints (see [deployment.md](deployment.md) Step 3).

### Cloud target (`PHAZE_CLOUD_TARGET`)

> **Renamed in v6.0 (breaking).** The old single on/off cloud-burst boolean is **removed**. It is replaced by the `cloud_target` selector below. If you kept the old enable boolean in your live env it is now **ignored** and cloud silently stays `local` on redeploy — delete it and set `PHAZE_CLOUD_TARGET` instead.

`cloud_target` is the **single routing selector** that chooses where long audio sets are analyzed (CLOUDDEPLOY-04). One of `local` / `a1` / `k8s`:

- **`local` (the default) = all-local, no other change.** Every file — short and long alike — routes to the local file-server queue exactly as it did before cloud burst existed. No file is held `AWAITING_CLOUD`, the staging cron no-ops, and backfill-to-cloud is rejected. (Long files may then time out locally and fail cleanly as `ANALYSIS_FAILED`.) A fresh deploy ships **dormant** this way until the operator provisions a cloud target and opts in.
- **`a1` = OCI A1 compute agent.** Long files (duration ≥ `cloud_route_threshold_sec`) route to the v5.0 OCI Ampere A1 compute agent via an rsync-over-SSH push. Requires `compute_scratch_dir` (fail-fast at startup when unset).
- **`k8s` = Kubernetes (Kueue).** Long files stage to S3 and the control plane submits a suspended Kueue Job. Requires `kube_api_url`, `kube_namespace`, and `kube_local_queue` (plus the S3 staging knobs `s3_bucket` / `s3_endpoint_url`); all fail fast at startup when unset.
- **Changing the target requires a control-plane restart.** The value is read from the import-time settings singleton, so setting the env var on a running controller does nothing until the controller worker + api are restarted (it is a startup-read, like every other knob).
- **In-flight work drains.** Switching back to `local` only stops *new* cloud work; files already `PUSHING`/`PUSHED` finish, and any held `AWAITING_CLOUD` rows release once a cloud target is selected again.

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

## Agent role settings (`PHAZE_ROLE=agent`)

These fields exist only on `AgentSettings` (the file server). When `PHAZE_ROLE=agent`, a model validator fails fast at startup if any **required** field is missing.

### Required agent fields

| Variable                                      | Required | Default | Description                                                                 |
|-----------------------------------------------|----------|---------|-----------------------------------------------------------------------------|
| `PHAZE_AGENT_API_URL` (or `AGENT_API_URL`)    | **Yes**  | (empty) | Base URL of the application server (e.g., `http://api:8000` in Compose). In `production` mode this must be `https://`. |
| `PHAZE_AGENT_TOKEN` (or `AGENT_TOKEN`)        | **Yes**  | (empty) | Bearer token (`SecretStr`) issued at agent registration. Must match the stored hash in the `agents` table. Format: `phaze_agent_<32 urlsafe-base64 bytes>`. |
| `PHAZE_AGENT_SCAN_ROOTS` (or `SCAN_ROOTS`)    | **Yes**  | (empty) | Comma-separated list of absolute paths the agent may read/write, used for path-traversal containment (e.g., `/data/music,/data/concerts`). |
| `PHAZE_AGENT_QUEUE` (or `AGENT_QUEUE`)        | **Yes**  | (empty) | SAQ queue the agent worker consumes. By convention it MUST equal `phaze-agent-<PHAZE_AGENT_ID>`. There is **no queue column** on the `agents` table: both the control plane and the agent worker derive the queue name from the agent_id. At startup `phaze.tasks.agent_worker` resolves the agent_id from the token via `/whoami` and asserts `PHAZE_AGENT_QUEUE == f"phaze-agent-{agent_id}"`, exiting non-zero on mismatch. Use the exact value printed by `phaze agents add` (see [deployment.md](deployment.md) Step 3). |

### Optional agent fields

| Variable                                          | Required | Default              | Description                                                                 |
|---------------------------------------------------|----------|----------------------|-----------------------------------------------------------------------------|
| `PHAZE_AGENT_ENV` (or `AGENT_ENV`)                | No       | `dev`                | Deployment mode: `dev` or `production`. `production` enforces `https://` agent URL and a passworded Redis URL. |
| `PHAZE_AGENT_CA_FILE` (or `AGENT_CA_FILE`)        | No       | `/certs/phaze-ca.crt`| Path to the operator-distributed CA cert the agent's HTTP client uses to verify the app-server TLS endpoint. |
| `PHAZE_WATCHER_SETTLE_SECONDS` (or `WATCHER_SETTLE_SECONDS`) | No | `10` | Seconds a file's mtime must be stable before the watcher posts it.          |
| `PHAZE_WATCHER_MAX_PENDING_SECONDS` (or `WATCHER_MAX_PENDING_SECONDS`) | No | `3600` | Stuck-file cap; pending entries older than this are evicted without posting.|
| `PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS` (or `WATCHER_SWEEP_INTERVAL_SECONDS`) | No | `2` | How often the watcher's sweep task checks for settled files.               |
| `PHAZE_WATCHER_POLLING_MODE` (or `WATCHER_POLLING_MODE`) | No | `false` | Use watchdog's `PollingObserver` instead of native inotify. Required for macOS Docker bind mounts where inotify events do not propagate. |
| `PHAZE_SCAN_CHUNK_SIZE` (or `SCAN_CHUNK_SIZE`)    | No       | `500`                | Number of file-upsert rows per chunk in `scan_directory`.                   |

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
- **Redis exposure** — keep `REDIS_BIND_IP=127.0.0.1` in dev; set it to the app server's LAN IP in production so agents on other hosts can reach Redis.
- **TLS SANs** — extend `PHAZE_API_TLS_SANS` with the app server's production LAN hostname / IP so agents can verify the TLS handshake.
- **Migrations** — set `PHAZE_AUTO_MIGRATE=false` in production to run Alembic migrations manually during a maintenance window.
- **Agent images** — `docker-compose.agent.yml` pulls `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}`; pin `PHAZE_IMAGE_TAG` (e.g., `2026.7.0`) per deployment.
