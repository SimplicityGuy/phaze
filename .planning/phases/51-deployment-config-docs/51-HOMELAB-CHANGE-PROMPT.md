<!-- generated-by: gsd-executor -->
# Homelab Change Prompt — Phaze v5.0 Cloud Burst (OCI A1 + Tailscale + broker role)

> **Paste the section below into the homelab repo agent.** It is a ready-to-apply
> change request for the homelab deployment of Phaze. It carries the **infrastructure
> spec** for the v5.0 Cloud Burst feature: the OpenTofu OCI Always-Free A1 module, the
> exact Tailscale grants ACL, and the empirically-verified least-privilege Postgres
> queue-broker role SQL. Phaze is the **source-of-truth spec**; all live infra (OpenTofu
> `.tf`, the tailnet policy, the lux Postgres role) lives in the **homelab** repo and is
> applied there (workspace boundary, D-09/D-10). Use placeholders only — never commit a
> real password, OCID, or tailnet IP.

---

## Context for the homelab agent

Phaze v5.0 adds **Cloud Burst**: long audio sets (≥ the cloud-route threshold) are pushed
off-server and analyzed on a **free OCI Ampere A1** instance running the **arm64 compute
agent** over **Tailscale**, instead of timing out on the local fileserver. The compute
agent is a worker-only Docker Compose service (no media mount, no watcher, scratch volume,
`-arm64` image) that:

- drains its per-agent SAQ queue from the **lux Postgres** broker (`saq_jobs` only — it has
  **no** `DATABASE_URL` and never touches the app ORM tables, DIST-04),
- receives pushed files via **rsync-over-SSH** from **nox** (the file server), and
- reaches the lux HTTP API (`:8000`) and Redis cache (`:6379`) over the tailnet.

Phaze emits the spec; **homelab applies it**. The three pieces below are exactly what the
homelab repo must author/apply:

1. **OpenTofu OCI A1 module** — provision the Always-Free Ampere A1 (this prompt, §1).
2. **Tailscale grants ACL** — default-deny network policy scoping the A1 (this prompt, §2).
3. **Least-privilege `phaze_broker` Postgres role** on lux — `saq_jobs` access only (§3).

The feature ships **off by default** (`PHAZE_CLOUD_BURST_ENABLED=false`); after the infra
below is provisioned, the operator flips the toggle and restarts the lux control plane (§4).

Apply the changes below to the homelab Phaze deployment (`datum@nox` and `datum@lux`, plus
the new OCI A1 host).

---

## 1. Provision the OCI Always-Free A1 (OpenTofu)

Author the OpenTofu module in the homelab repo (provider `oracle/oci`). Phaze specifies the
shape and networking; the homelab `.tf` declares the resources. The A1 must come up as
**Ubuntu 24.04 arm64**, on the tailnet, with `rsync` available to receive pushes.

> **⚠️ Capacity/limit gotcha (load-bearing — verify at provision time).** As of **June 2026**
> the OCI Always-Free Ampere A1 limit was reduced to **2 OCPU / 12 GB total** (previously
> 4 OCPU / 24 GB). **Spec the A1 at 2 OCPU / 12 GB** and set `WORKER_MAX_JOBS=1` on the
> compute agent (single concurrent, RAM-bound analysis on 12 GB). Always-Free A1 capacity is
> region-constrained — **"Out of Capacity"** is common; retry across availability domains /
> regions, or use a small provisioning retry loop.

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

**Networking / security-list note.** Tailscale provides the actual A1↔lux / nox→A1 access
control (the grants ACL in §2), so the OCI security list only needs to allow **outbound**
(for `tailscaled` to reach the tailnet relays / DERP) and may keep **inbound tightly closed**
except what the operator needs for first-boot SSH bootstrap (or bootstrap entirely over
Tailscale once `tailscaled` is up). The security list is **outbound-mostly** — Tailscale is
the real ACL.

**Cloud-init / first-boot note.** Install `tailscaled` **and** `rsync` on the A1 (via
cloud-init or the runbook) so the host can join the tailnet and receive rsync-over-SSH
pushes:

```bash
# cloud-init (or manual on first boot):
apt-get update && apt-get install -y tailscale rsync
tailscale up --advertise-tags=tag:cloud-agent     # tag must match §2 tagOwners
```

The A1 **host** (not the container) runs `tailscaled` (D-05). The compute-agent compose uses
`network_mode: host` to inherit the host's tailnet connectivity + MagicDNS.

---

## 2. Apply the Tailscale grants ACL

Apply the following **default-deny** tailnet policy (Tailscale's current `grants` form;
generally available, coexists with any legacy `acls`/`ports`). Once any policy exists, the
A1 (tagged `tag:cloud-agent`) gets **only** what is explicitly granted: `A1 → lux` on
`tcp:{5432,6379,8000}` and `nox → A1` on `tcp:22`, and nothing else.

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

**Notes.**

- With **default-deny**, the A1 cannot reach anything else on the tailnet, and nothing else
  can reach the A1 except `nox:22`.
- `tcp:5432` is required because the SAQ broker is **Postgres** (Phase 36); the A1 still has
  **NO** `DATABASE_URL` (DIST-04 holds — it touches only `saq_jobs`/`saq_stats`/`saq_versions`).
- If `port 22` collides with a Tailscale-SSH policy, use a normal `sshd` — a plain `tcp:22`
  grant is sufficient; Tailscale SSH `ssh` rules are a separate construct and are **not**
  needed here.
- **Placeholders only** — replace `100.x.x.x` / `100.y.y.y` with the real lux/nox tailnet
  IPs in the homelab repo; never commit real IPs into this spec.

---

## 3. Create the least-privilege phaze_broker Postgres role (lux)

The compute agent connects to the lux Postgres **only** as the SAQ broker — it dequeues,
sweeps, and writes stats on the three SAQ tables and nothing else (DIST-04). Create a
dedicated `phaze_broker` role with exactly the grants below.

> **Prerequisite (load-bearing).** The **full `phaze` role** (the control plane) must boot
> **FIRST** so SAQ has already created and migrated `saq_jobs` / `saq_stats` / `saq_versions`
> to the current version. The broker role then never *owns* or *migrates* the SAQ schema —
> its `init_db()` short-circuits at the version check.

> **Why `CREATE ON SCHEMA public` is unavoidable (empirically verified, live Postgres 18,
> 2026-06-26).** SAQ's `init_db()` runs, on **every** `queue.connect()`, an **unconditional**
> `CREATE TABLE IF NOT EXISTS saq_versions` — and PostgreSQL checks the **schema-CREATE
> privilege _before_ the `IF NOT EXISTS` existence short-circuit**. A broker role lacking
> `CREATE ON SCHEMA public` therefore fails with `ERROR: permission denied for schema public`
> **even when all SAQ tables already exist**. So pre-creating the tables and granting only
> table-DML does **not** work — `CREATE ON SCHEMA public` is required regardless. The safe
> least-privilege synthesis is below: pre-create the tables as the full role **AND** grant
> the broker `CREATE ON SCHEMA public` + table-DML + sequence USAGE, with **zero** grants on
> any app-ORM table.

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

**Postgres-version note.** On PostgreSQL **15+**, the `public` schema no longer grants
`CREATE` to `PUBLIC` by default, so the explicit `GRANT … CREATE ON SCHEMA public` is
genuinely required and meaningful. On PG **<15**, `PUBLIC` already had `CREATE` on `public`,
so the grant is redundant there. Confirm the lux Postgres major version in the runbook.

**DIST-04 verification probe (must run after creating the role).** The broker must be blind
to all app-ORM data:

```sql
SET ROLE phaze_broker;
SELECT * FROM files LIMIT 1;     -- MUST ERROR: permission denied for table files
RESET ROLE;
```

If that `SELECT` returns rows instead of erroring, the role is over-granted — stop and
revoke. The role must carry **no** grant on any app ORM table (`files`, `metadata`,
`agents`, etc.).

**Optional stronger hardening (document, don't default).** A dedicated `saq` schema gives the
broker zero rights in `public`: `CREATE SCHEMA saq; ALTER TABLE saq_jobs/saq_stats/saq_versions
SET SCHEMA saq;` then `ALTER ROLE phaze SET search_path = saq, public;` and
`ALTER ROLE phaze_broker SET search_path = saq;` with `GRANT USAGE, CREATE ON SCHEMA saq` (and
no rights in `public`). This is genuinely tighter but relocates the **live** control-plane
`saq_jobs` table and changes the full role's `search_path` — a riskier homelab change. Defer
unless strict public-schema lockdown is wanted.

---

## 4. Deploy ordering

Apply in this order (Phase 36 "Step D" precedent; SSH targets `datum@nox` / `datum@lux`).
Placeholders only — never inline real secrets.

1. **homelab:** OpenTofu apply → A1 up (Ubuntu 24.04 arm64, 2 OCPU / 12 GB); cloud-init
   installs `tailscaled` + `rsync`; `tailscale up --advertise-tags=tag:cloud-agent` (§1).
2. **homelab:** apply the tailnet **grants ACL** (A1 ↔ lux + nox → A1) (§2).
3. **homelab (`datum@lux` Postgres):** run the `phaze_broker` role SQL — the control plane
   (full `phaze` role) must already have booted so the SAQ tables exist (§3).
4. **phaze release:** ship **v5.0.x** → GHCR publishes `ghcr.io/simplicityguy/phaze:v5.0.0-arm64`.
5. **A1:** populate `.env` (broker `PHAZE_QUEUE_URL`, agent token, scratch dir,
   `WORKER_MAX_JOBS=1`), then `docker compose -f docker-compose.cloud-agent.yml up -d`.
6. **`datum@lux` control plane:** set `PHAZE_CLOUD_BURST_ENABLED=true`; **restart** the
   controller worker + api (the toggle is startup-read).
7. **smoke test:** trigger an analysis on a long set, confirm it routes to the A1, the file
   pushes (rsync over SSH), the compute agent analyzes it, and results reconcile by `file_id`.

**Closing notes.**

- **Off-by-default ships the cloud feature dormant.** A fresh v5.0 deploy is **all-local**
  until steps 1–6 are done; long files route to the local queue (and may time out cleanly as
  `ANALYSIS_FAILED`) until cloud is enabled. Pre-existing `AWAITING_CLOUD` rows release once
  the toggle is on.
- **Flipping `PHAZE_CLOUD_BURST_ENABLED` requires a control-plane restart** (startup-read of
  the settings singleton — Pitfall 6). Setting the env without restarting changes nothing.
- **nox's `PHAZE_PUSH_KNOWN_HOSTS` must be re-provisioned** with the A1's SSH **host key**
  after the A1 is up (Phase 50 strict known_hosts), or the rsync-over-SSH push will fail host
  verification.

---

## Done-when checklist

- [ ] OpenTofu OCI A1 module authored in homelab; A1 up as Ubuntu 24.04 arm64, 2 OCPU / 12 GB, on the tailnet with `rsync` present
- [ ] Tailscale **default-deny** grants ACL applied: A1 → lux `tcp:{5432,6379,8000}` + nox → A1 `tcp:22`, tagged `tag:cloud-agent`
- [ ] `phaze_broker` role created with `GRANT USAGE, CREATE ON SCHEMA public` + DML on the 3 SAQ tables + `saq_jobs_lock_key_seq` USAGE, and **zero** app-ORM grants
- [ ] DIST-04 probe run: `SET ROLE phaze_broker; SELECT * FROM files LIMIT 1;` **ERRORs** (permission denied)
- [ ] Deploy order followed: OpenTofu → ACL → broker SQL → v5.0.x `-arm64` release → A1 compose up → `PHAZE_CLOUD_BURST_ENABLED=true` + control-plane restart → smoke test
- [ ] nox `PHAZE_PUSH_KNOWN_HOSTS` re-provisioned with the A1 host key

---

*Infrastructure spec for Phase 51 (v5.0 Cloud Burst: OCI A1 + Tailscale + least-privilege
broker role). Phaze is source-of-truth; the homelab repo authors the OpenTofu `.tf`, the
tailnet policy, and the lux Postgres role.*
