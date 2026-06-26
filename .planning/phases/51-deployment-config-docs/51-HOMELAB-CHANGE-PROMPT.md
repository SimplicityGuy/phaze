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
