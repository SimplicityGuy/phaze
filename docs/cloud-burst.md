<!-- generated-by: gsd-doc-writer -->
# Cloud Burst — OCI A1 compute agent (v5.0)

**Cloud burst** offloads **long** audio sets to a free, always-on **OCI Ampere A1 (arm64)
compute agent** so they no longer time out on the local file server. The control plane routes
any file whose duration is at/above `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC` to the compute agent: the
file server **pushes** it over **rsync-over-SSH across Tailscale** (Phase 50), the A1 worker
analyzes it, and results reconcile by `file_id`. There is **NO object storage** — the transport
is a direct rsync push to an ephemeral scratch volume that the agent deletes after analysis.

This document is the single home for deploying and operating cloud burst: the compose
walkthrough, the homelab provisioning runbook (OCI A1 + Tailscale ACL + Postgres broker role),
the deploy ordering, and the smoke test. For the canonical per-knob config reference, see
[configuration.md → Cloud-burst settings](configuration.md#cloud-burst-settings) — this page does
not duplicate that table.

> **Renamed in v6.0 (breaking).** The old single on/off cloud-burst boolean is **removed** and
> replaced by the `PHAZE_CLOUD_TARGET` selector (`local` \| `a1` \| `k8s`). If you kept the old
> enable boolean in your live env it is now **ignored** — cloud silently stays `local` on redeploy.
> Delete it and set `PHAZE_CLOUD_TARGET` instead. See *Cloud target & runtime-state semantics* below.

> **The feature ships OFF by default.** `PHAZE_CLOUD_TARGET=local` (the default) means a fresh
> deploy behaves **all-local** with zero cloud activity. Provision the infrastructure below first,
> then set the target and restart the control plane (Step 6). Use `PHAZE_CLOUD_TARGET=a1` for the
> OCI A1 compute agent documented on this page, or `PHAZE_CLOUD_TARGET=k8s` for the Kubernetes
> (Kueue) target — documented in its own runbook, [k8s-burst.md](k8s-burst.md).

> **Superseded in 2026.7.1 (Phase 67).** The single `PHAZE_CLOUD_TARGET` selector this page
> describes was **removed** in favor of the declarative **[backend registry](configuration.md#backend-registry-backendstoml)**
> (`backends.toml`): a `compute` backend is now one `[[backends]] kind="compute"` entry among
> N cost-ranked backends the scheduler drains across simultaneously. This page remains the A1
> host-provisioning + rsync-transport reference; for the config model and the trivial
> `cloud_target`→`backends` mapping, see
> [configuration.md → Cloud target](configuration.md#cloud-target-removed-in-phase-67).

## Architecture at a glance

```mermaid
flowchart LR
  %% Tailscale tailnet (default-deny grants ACL)
  subgraph nox["nox (file server)"]
    noxc["docker-compose.agent.yml<br/>(worker+watcher+fprint+media)"]
  end
  subgraph a1["OCI A1 (compute agent)"]
    a1c["docker-compose.cloud-agent.yml<br/>worker (kind=compute)<br/>no media, scratch volume<br/>-arm64 image"]
  end
  subgraph lux["lux (application server)"]
    luxapi["api(:8000) · Postgres(:5432 app ORM + saq_jobs broker) · Redis(:6379)"]
    luxctl["controller worker (stage_cloud_window cron)"]
    luxbroker["broker role 'phaze_broker' → saq_jobs ONLY (least-privilege)"]
  end
  noxc -->|"rsync over SSH (nox → A1:22)"| a1c
  a1c -->|"HTTP API + saq_jobs + cache (A1 → lux:{5432,6379,8000})"| luxapi
```

_PHAZE_CLOUD_TARGET=local ⇒ long files route LOCAL, staging cron no-ops, backfill rejected, A1 idle. (all-local)_

Key invariants:

- **Worker-only compose.** The cloud-agent stack runs **only** the agent SAQ worker
  (`PHAZE_ROLE=agent`, `PHAZE_AGENT_KIND=compute`). No watcher, no `audfprint`/`panako`
  fingerprint sidecars, no media mount — a compute agent owns no scan roots.
- **arm64-only image.** The image is published as a **separate `-arm64` tag** (there is no
  multi-arch manifest); the `-arm64` suffix is mandatory or the pull resolves the x86 image,
  which will not run on the Ampere A1 (see [arm64-agent-image.md](arm64-agent-image.md)).
- **No `DATABASE_URL` (DIST-04).** The compute agent reaches Postgres **only** via
  `PHAZE_QUEUE_URL` for the `saq_jobs` broker, plus the application server's HTTP API. It never
  touches the app ORM tables.
- **Host Tailscale.** `tailscaled` runs on the **A1 host** (not a sidecar); the compose uses
  `network_mode: host` to inherit the host's tailnet connectivity + MagicDNS.

## Step 1 — Provision the OCI A1 (homelab OpenTofu)

The OCI A1 instance, its VCN/subnet/security-list, and the boot volume are authored as
**OpenTofu IaC in the homelab repo** (workspace boundary — phaze emits the spec, homelab applies
it). The full ready-to-paste change request is in
[`51-HOMELAB-CHANGE-PROMPT.md`](../.planning/phases/51-deployment-config-docs/51-HOMELAB-CHANGE-PROMPT.md);
the reference spec is reproduced here.

> **⚠️ Capacity gotcha (load-bearing).** As of **June 2026** the OCI Always-Free Ampere A1 limit
> was reduced to **2 OCPU / 12 GB total** (previously 4 OCPU / 24 GB). Spec the A1 at **2 OCPU /
> 12 GB** and set `WORKER_MAX_JOBS=1` on the compute agent (a single concurrent analysis is
> RAM-bound on 12 GB). Always-Free A1 capacity is region-constrained — "Out of Capacity" is
> common; retry across availability domains / regions.

```hcl
# Provider: oracle/oci. Resources the homelab module must create:
resource "oci_core_instance" "phaze_compute_a1" {
  availability_domain = <pick an AD with A1 capacity>
  compartment_id      = var.compartment_id
  display_name        = "phaze-cloud-agent"
  shape               = "VM.Standard.A1.Flex"        # Always-Free eligible (Ampere arm64)
  shape_config {
    ocpus         = 2                                 # current Always-Free limit (June 2026)
    memory_in_gbs = 12
  }
  source_details {
    source_type = "image"
    source_id   = <Canonical Ubuntu 24.04 Minimal aarch64 image OCID for the region>
  }
  create_vnic_details {
    subnet_id        = oci_core_subnet.phaze_public.id
    assign_public_ip = true                           # for outbound + tailscale bootstrap
  }
  metadata = { ssh_authorized_keys = var.ssh_public_key }
}
# Plus: oci_core_vcn, oci_core_subnet, oci_core_internet_gateway, oci_core_route_table,
#       and a security list / NSG. Boot volume defaults are Always-Free within the 200GB total.
```

Tailscale (Step 2) is the real access control, so the OCI security list only needs to allow
**outbound** (for `tailscaled` to reach the tailnet relays/DERP) and may keep inbound tightly
closed except first-boot SSH bootstrap. Install `tailscaled` **and** `rsync` on the A1 (the host
must be able to receive rsync-over-SSH pushes):

```bash
# cloud-init (or manual on first boot):
apt-get update && apt-get install -y tailscale rsync
tailscale up --advertise-tags=tag:cloud-agent     # tag must match the ACL tagOwners (Step 2)
```

## Step 2 — Apply the Tailscale grants ACL (homelab)

Apply this **default-deny** tailnet policy (Tailscale's current `grants` form). Once any policy
exists, the A1 (tagged `tag:cloud-agent`) gets **only** what is granted: `A1 → lux` on
`tcp:{5432,6379,8000}` and `nox → A1` on `tcp:22`, and nothing else. This is a reference copy;
homelab is the source of the live policy.

```jsonc
{
  // The A1 must come up tagged 'tag:cloud-agent' (tailscale up --advertise-tags=tag:cloud-agent,
  // or auth-key with the tag). 'hosts' map lux/nox to their tailnet IPs (or use tags if preferred).
  "tagOwners": {
    "tag:cloud-agent": ["autogroup:admin"]
  },
  "hosts": {
    "lux": "100.x.x.x",   // application server tailnet IP (Postgres queue + Redis cache + HTTP API)
    "nox": "100.y.y.y"    // file server tailnet IP (push initiator)
  },
  "grants": [
    // A1 compute agent -> lux: saq_jobs broker (5432), Redis cache (6379), app HTTP API (8000).
    {
      "src": ["tag:cloud-agent"],
      "dst": ["lux"],
      "ip": ["tcp:5432", "tcp:6379", "tcp:8000"]
    },
    // nox file server -> A1: SSH for the rsync-over-SSH push (Phase 50 push_file target).
    {
      "src": ["nox"],
      "dst": ["tag:cloud-agent"],
      "ip": ["tcp:22"]
    }
  ]
}
```

`tcp:5432` is required because the SAQ broker is **Postgres** (Phase 36); the A1 still has **no**
`DATABASE_URL` (DIST-04 — it touches only `saq_jobs`/`saq_stats`/`saq_versions`). Use placeholders
only here; the real lux/nox tailnet IPs live in the homelab repo, never in this spec.

## Step 3 — Create the least-privilege `phaze_broker` Postgres role (homelab, lux)

The compute agent connects to lux Postgres **only** as the SAQ broker. Create a dedicated
`phaze_broker` role with exactly the grants below and **zero** grants on any app-ORM table.

> **Prerequisite (load-bearing).** The **full `phaze` role** (the control plane) must boot
> **FIRST** so SAQ has already created and migrated `saq_jobs` / `saq_stats` / `saq_versions`.
> The broker role then never *owns* or *migrates* the SAQ schema — its `init_db()`
> short-circuits at the version check.

> **Why `CREATE ON SCHEMA public` is unavoidable (empirically verified, live Postgres 18,
> 2026-06-26).** SAQ's `init_db()` runs, on **every** `queue.connect()`, an **unconditional**
> `CREATE TABLE IF NOT EXISTS saq_versions` — and PostgreSQL checks the **schema-CREATE
> privilege _before_ the `IF NOT EXISTS` existence short-circuit**. A broker role lacking
> `CREATE ON SCHEMA public` fails with `ERROR: permission denied for schema public` **even when
> all SAQ tables already exist**. Pre-creating the tables and granting only table-DML therefore
> does **not** work; `CREATE ON SCHEMA public` is required regardless.

```sql
-- Run as the phaze owner / a superuser on the lux Postgres, in the phaze database.
-- The control plane (full 'phaze' role) must boot FIRST so SAQ has already created
-- saq_jobs/saq_stats/saq_versions and migrated them to the current version.

CREATE ROLE phaze_broker LOGIN PASSWORD '<strong-unique-password>';   -- use a secret/_FILE in deploy

-- USAGE to resolve objects; CREATE is REQUIRED by SAQ init_db's unconditional
-- `CREATE TABLE IF NOT EXISTS saq_versions` (PG checks schema-CREATE before IF-NOT-EXISTS).
GRANT USAGE, CREATE ON SCHEMA public TO phaze_broker;

-- Queue table DML (the broker dequeues, sweeps, writes stats).
GRANT SELECT, INSERT, UPDATE, DELETE ON saq_jobs, saq_stats, saq_versions TO phaze_broker;

-- saq_jobs.lock_key is SERIAL -> an INSERT assigns from this sequence.
GRANT USAGE, SELECT ON SEQUENCE saq_jobs_lock_key_seq TO phaze_broker;

-- DIST-04: grant NOTHING on the app ORM tables. Verify the role is blind to app data:
--   SET ROLE phaze_broker; SELECT * FROM files LIMIT 1;  -- must ERROR: permission denied
```

**Postgres-version note.** On PostgreSQL **15+**, the `public` schema no longer grants `CREATE`
to `PUBLIC` by default, so the explicit `GRANT … CREATE ON SCHEMA public` is genuinely required.
On PG **<15** it is redundant. Confirm the lux Postgres major version in the runbook.

**DIST-04 verification probe (run after creating the role).** The broker must be blind to all
app-ORM data:

```sql
SET ROLE phaze_broker;
SELECT * FROM files LIMIT 1;     -- MUST ERROR: permission denied for table files
RESET ROLE;
```

If that `SELECT` returns rows instead of erroring, the role is over-granted — stop and revoke.

**Optional stronger hardening (document, don't default).** A dedicated `saq` schema gives the
broker zero rights in `public` (`CREATE SCHEMA saq; ALTER TABLE … SET SCHEMA saq;` plus
`search_path` changes on both roles). It is tighter but relocates the **live** control-plane
`saq_jobs` table — a riskier homelab change. Defer unless strict public-schema lockdown is wanted.

## Step 4 — Release the `-arm64` image

Ship a CalVer release. The Phase 47 `build-arm64` CI job publishes the native arm64 image as a
separate `-arm64` tag — `ghcr.io/simplicityguy/phaze:2026.7.0-arm64` (and `:latest-arm64` on the
default branch). The cloud-agent compose pins it via `PHAZE_IMAGE_TAG` (see
[arm64-agent-image.md → Tag naming](arm64-agent-image.md)). There is no multi-arch manifest, so
the `-arm64` suffix is mandatory.

## Step 5 — Bring up the compute agent (`docker-compose.cloud-agent.yml`)

On the **A1 host**, the operator runs the standalone worker-only compose:

```bash
docker compose -f docker-compose.cloud-agent.yml up -d
```

The compose file is worker-only: a single `worker` service (no media-bound sidecars),
`network_mode: host`, the `-arm64` image, a **named** `cloud_scratch` volume, the models mount
`rw` (auto-download), and the CA cert mount `ro`. It declares no `postgres`/`redis` service and
no `DATABASE_URL` (DIST-04).

Populate the A1's `.env` with the compute-agent variables. The cloud-burst knobs are documented
in [configuration.md → Cloud-burst settings](configuration.md#cloud-burst-settings); secrets use
the `*_FILE` convention:

```bash
PHAZE_QUEUE_URL=postgresql://phaze_broker:<pw>@lux:5432/phaze   # libpq form; broker role (NOT phaze)
                                                               # prefer PHAZE_QUEUE_URL_FILE
PHAZE_REDIS_URL=redis://:<redis_pw>@lux:6379/0                  # production mode REQUIRES a password
PHAZE_AGENT_API_URL=https://lux:8000                           # production mode REQUIRES https://
PHAZE_AGENT_ENV=production
PHAZE_AGENT_KIND=compute                                       # relaxes the empty-scan-roots gate
PHAZE_AGENT_QUEUE=phaze-agent-<compute_agent_id>               # raw env, read at SAQ import time
PHAZE_AGENT_TOKEN_FILE=/run/secrets/agent_token                # _FILE secret
PHAZE_CLOUD_SCRATCH_DIR=/scratch                               # MUST match control's PHAZE_COMPUTE_SCRATCH_DIR
WORKER_MAX_JOBS=1                                              # single RAM-bound analysis on the 12 GB A1
MODELS_PATH=/models
PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt
PHAZE_IMAGE_TAG=2026.7.0                                      # pulls 2026.7.0-arm64
# NO DATABASE_URL (DIST-04). NO SCAN_PATH / PHAZE_AGENT_SCAN_ROOTS (kind=compute relaxes it).
```

> **Two production guards WILL fire if violated** (`AgentSettings`, with
> `PHAZE_AGENT_ENV=production`): `agent_api_url` must be `https://` and `redis_url` must carry a
> password. Both are non-negotiable on the compute agent — set `PHAZE_AGENT_API_URL=https://lux:8000`
> and a passworded `PHAZE_REDIS_URL`.

**Scratch-dir match.** `PHAZE_CLOUD_SCRATCH_DIR` (A1) **must equal** `PHAZE_COMPUTE_SCRATCH_DIR`
(lux control plane) and the named-volume mount path — a drift surfaces as a sha256/transfer
failure, never silent corruption.

## Step 6 — Select the cloud target and restart the control plane (lux)

On the **lux control plane**, set the routing selector and restart so the new value is read:

```bash
# In the lux .env (A1 / rsync target):
PHAZE_CLOUD_TARGET=a1
# then restart the controller worker + api
```

`cloud_target` is a **startup-read** of the settings singleton — setting the env var on a running
controller does **nothing** until the controller worker + api restart. Once set to `a1`, long files
begin routing to the A1, and any pre-existing `AWAITING_CLOUD` rows held from before the change are
released by the staging cron. For the Kubernetes target set `PHAZE_CLOUD_TARGET=k8s` instead and
provide the kube/S3 knobs — see the dedicated [k8s-burst.md](k8s-burst.md) runbook. See *Cloud
target & runtime-state semantics* below.

## Step 7 — Smoke test

Confirm the end-to-end path with this checklist:

- [ ] **Agent registers.** The compute agent appears on `/admin/agents` and reaches **alive**
      within ~60s of `docker compose -f docker-compose.cloud-agent.yml up -d` (heartbeat OK).
- [ ] **A long file routes to cloud.** Trigger analysis on a set whose duration ≥
      `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC`; confirm the file enters `AWAITING_CLOUD` (not the local
      queue).
- [ ] **The file pushes.** The file server's `push_file` job transfers it via rsync-over-SSH to
      the A1 scratch dir; the file moves through `PUSHING` → `PUSHED` and the sha256 verifies.
- [ ] **The compute agent drains a `process_file`.** The A1 worker reads the pushed file from
      scratch, analyzes it, and posts results that reconcile by `file_id`.
- [ ] **Scratch is cleaned.** The pushed file is deleted from the A1 scratch volume after
      analysis (no scratch leak).
- [ ] **`local` reverts cleanly (optional).** With `PHAZE_CLOUD_TARGET=local` + a control-plane
      restart, a new long file routes **local** and the staging cron no-ops.

## Cloud target & runtime-state semantics

> **Renamed in v6.0 (breaking).** The old single on/off cloud-burst boolean is **removed**. The
> `PHAZE_CLOUD_TARGET` selector (`local` \| `a1` \| `k8s`) replaces it. An operator who kept the old
> enable boolean set in their live env must **delete it** — it is ignored and cloud silently stays
> `local` on redeploy.

`PHAZE_CLOUD_TARGET` (`cloud_target`, `ControlSettings`) is the **single routing selector** for the
whole feature (CLOUDDEPLOY-04):

- **`local` (default) = all-local, no other change.** Every file — short and long — routes
  to the local file-server queue exactly as before cloud burst existed. The routing seam never
  sets `AWAITING_CLOUD`, the staging cron no-ops, and backfill-to-cloud is rejected. Long files
  may then time out locally and fail cleanly as `ANALYSIS_FAILED`. A fresh deploy ships
  **dormant** this way until the operator selects a target and completes Steps 1–6.
- **`a1` = OCI A1 compute agent (this page).** Long files route to the A1 via rsync-over-SSH.
  Requires `compute_scratch_dir` (control plane) matched to the A1's `cloud_scratch_dir`.
- **`k8s` = Kubernetes (Kueue).** Long files stage to S3 and the control plane submits a suspended
  Kueue Job. Requires the kube + S3 knobs. Documented in its own runbook —
  [k8s-burst.md](k8s-burst.md).
- **Changing the target requires a control-plane restart** (startup-read — Pitfall 6). The
  controller worker + api must restart for a change to take effect.
- **In-flight work drains; switching to `local` only stops NEW cloud work.** Files already
  `PUSHING`/`PUSHED` finish across a restart (state is durable in Postgres); no mid-transfer/
  mid-analysis abort, no scratch reclaim. Held `AWAITING_CLOUD` rows from before a `local`→cloud
  change release once a cloud target is selected.
- **`nox`'s `PHAZE_PUSH_KNOWN_HOSTS` must be re-provisioned** with the A1's SSH **host key** after
  the A1 is up (Phase 50 strict known_hosts), or the rsync-over-SSH push fails host verification.

### Selecting the `k8s` target

The Kubernetes (Kueue) target — the kube + S3 config knobs, the cluster-admin runbook
(ResourceFlavor / ClusterQueue / LocalQueue / RBAC / Secret), the apiVersion-lockstep rule, the
transport-agnostic endpoint notes, the deploy ordering, and the submit → reconcile lifecycle —
is documented in its own self-contained runbook:

> **➡ See [k8s-burst.md](k8s-burst.md)** for the full Kubernetes burst setup. This page stays
> A1-specific; the k8s target is **not** folded in here.

See also [configuration.md → Kube submit/reconcile settings](configuration.md#kube-submitreconcile-settings-phase-54-v60)
for the full per-knob reference (defaults, `_FILE` support).

## See also

- [configuration.md → Cloud-burst settings](configuration.md#cloud-burst-settings) — the canonical
  per-knob reference (env var, default, `_FILE` support, description).
- [k8s-burst.md](k8s-burst.md) — the **Kubernetes (Kueue)** burst target (`PHAZE_CLOUD_TARGET=k8s`):
  the cluster-admin runbook, RBAC, and submit → reconcile lifecycle.
- [deployment.md](deployment.md) — the two-host base deployment the cloud agent extends.
- [arm64-agent-image.md](arm64-agent-image.md) — how the `-arm64` image is built and tagged.
- [`51-HOMELAB-CHANGE-PROMPT.md`](../.planning/phases/51-deployment-config-docs/51-HOMELAB-CHANGE-PROMPT.md)
  — the ready-to-apply homelab infrastructure change request (OpenTofu + ACL + broker role).
