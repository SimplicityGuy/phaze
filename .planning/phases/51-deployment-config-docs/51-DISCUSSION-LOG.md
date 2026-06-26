# Phase 51: Deployment, config & docs - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-26
**Phase:** 51-deployment-config-docs
**Areas discussed:** Master toggle behavior, Cloud-agent compose & Tailscale, Runbook depth & format, Config-docs surface

---

## Master toggle behavior (CLOUDDEPLOY-04)

### OFF behavior for long files

| Option | Description | Selected |
|--------|-------------|----------|
| Route long files local | OFF = pure pre-Phase-49: all files (incl. long) route local; may time out cleanly (bounded 4h + retries=1) | ✓ |
| Hold long files (AWAITING_CLOUD) | OFF still holds long files unanalyzed | |
| Skip long files entirely | OFF analyzes only short files; long left DISCOVERED | |

**User's choice:** Route long files local — honest "all-local analysis with no other change."

### Gate scope

| Option | Description | Selected |
|--------|-------------|----------|
| Gate every cloud entry point | OFF short-circuits routing + staging cron + release_awaiting_cloud cron | ✓ |
| Gate routing only | OFF only changes routing; crons keep running | |

**User's choice:** Gate every cloud entry point.

### In-flight handling on flip-OFF

| Option | Description | Selected |
|--------|-------------|----------|
| Let in-flight drain | PUSHING/PUSHED finish; OFF only stops new cloud work | ✓ |
| Hard stop + reclaim | Abort in-flight pushes, clean scratch, re-route local | |

**User's choice:** Let in-flight drain.

### Naming + default

| Option | Description | Selected |
|--------|-------------|----------|
| cloud_burst_enabled, default false | PHAZE_CLOUD_BURST_ENABLED, off-by-default (safe; opt-in after provisioning) | ✓ |
| cloud_burst_enabled, default true | Same name, on out of the box (riskier) | |
| Other name | Different name/default | |

**User's choice:** cloud_burst_enabled, default false.

---

## Cloud-agent compose & Tailscale (CLOUDDEPLOY-01)

### Tailscale connectivity

| Option | Description | Selected |
|--------|-------------|----------|
| Host-installed tailscaled | tailscaled on the A1 host; compose uses host TS connectivity | ✓ |
| Tailscale sidecar container | tailscale/tailscale sidecar with TS_AUTHKEY; shared netns | |

**User's choice:** Host-installed tailscaled.

### Compose shape + scratch

| Option | Description | Selected |
|--------|-------------|----------|
| Worker-only + named scratch volume | Just the compute worker; no watcher/fingerprint/media; named volume scratch | ✓ |
| Worker + watcher | Include the watcher (unnecessary — no scan roots) | |
| Bind-mount scratch | Worker-only but host bind-mount scratch | |

**User's choice:** Worker-only + named scratch volume.

### arm64 image tag

| Option | Description | Selected |
|--------|-------------|----------|
| ${PHAZE_IMAGE_TAG:-latest} | Existing agent-compose convention; default latest, pin via env | ✓ |
| Pin a fixed v5.0 tag | Hard-code a version tag | |

**User's choice:** ${PHAZE_IMAGE_TAG:-latest} — **corrected** to carry the mandatory `-arm64` suffix (the Phase 47 image is a separate `-arm64` tag, not multi-arch; `docs/arm64-agent-image.md:189-194`). Final: `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64`.

---

## Runbook depth & format (CLOUDDEPLOY-03)

### Runbook depth

| Option | Description | Selected |
|--------|-------------|----------|
| Copy-paste prescriptive | Exact tailscale up flags, full ACL JSON, role SQL, .env layout | ✓ |
| Conceptual + references | Explain shape, link to vendor docs | |

**User's choice:** Copy-paste prescriptive.

### OCI provisioning

| Option | Description | Selected |
|--------|-------------|----------|
| Console click-path | OCI web console walkthrough | (superseded) |
| OCI CLI commands | oci compute instance launch ... | |
| Terraform / IaC | Terraform module | |

**User's choice:** Initially console click-path — **superseded mid-discussion** by the user directive: "in the homelab repo, we're going to use opentofu to deploy the OCI infrastructure."

### PG queue-broker role

| Option | Description | Selected |
|--------|-------------|----------|
| Full SQL in the runbook | Exact CREATE ROLE + minimal GRANTs for saq_jobs (Postgres queue backend) | ✓ |
| Reference existing config docs | Keep role SQL minimal/abstract | |

**User's choice:** Full SQL (carried as the spec in the homelab change prompt, copied into phaze docs for reference).

### Infra split (follow-up after OpenTofu directive)

| Option | Description | Selected |
|--------|-------------|----------|
| Homelab change-prompt deliverable | Phase 51 emits a ready-to-paste homelab prompt specifying the OpenTofu OCI A1 module + ACL + PG role; phaze docs reference it | ✓ |
| Full OpenTofu authored in phaze | Write .tf in phaze (violates workspace boundary) | |
| Just reference homelab, no change-prompt | Mention only, no spec handed over | |

**User's choice:** Homelab change-prompt deliverable.

### ACL + PG role authoring location

| Option | Description | Selected |
|--------|-------------|----------|
| Both in homelab, spec'd by phaze | ACL + role applied in homelab; phaze carries exact JSON/SQL as the authoritative spec | ✓ |
| ACL + role documented in phaze docs only | Apply manually from phaze docs | |

**User's choice:** Both in homelab, spec'd by phaze.

---

## Config-docs surface (CLOUDDEPLOY-02)

### Doc layout

| Option | Description | Selected |
|--------|-------------|----------|
| One new cloud-burst doc + cross-links | New docs/cloud-burst.md (runbook + compose/deploy); config subsection in configuration.md; pointer from deployment.md | ✓ |
| Fold into existing docs | Runbook+compose into deployment.md, knobs into configuration.md | |
| Separate runbook + separate config doc | Two new files | |

**User's choice:** One new cloud-burst doc + cross-links.

### Config knob documentation

| Option | Description | Selected |
|--------|-------------|----------|
| Full table: knob, env, default, _FILE | Table of every cloud-burst knob + _FILE secrets + master-toggle semantics, sourced from config.py Field descriptions | ✓ |
| Prose summary + link to config.py | Brief prose, point to source | |

**User's choice:** Full table.

---

## Claude's Discretion

- PG-role grant-timing (CREATE-on-first-boot vs pre-create table).
- Exact compose env var / `.env` layout within the worker-only + no-media + named-scratch + `-arm64` constraints.
- Toggle read-once-at-startup vs per-tick (prefer per-tick).
- Exact wording/structure of the homelab change prompt.
- Smoke-test step as doc checklist vs scripted check.

## Deferred Ideas

- Cost/throughput-aware routing (CLOUDROUTE-05) — out of scope.
- Dynamic multi-compute-agent discovery — static single-A1 sufficient.
- Hard-stop + reclaim of in-flight cloud work — drain-only chosen.
- Tailscale sidecar packaging — host-installed chosen.
- Terraform/OCI-CLI provisioning — superseded by homelab OpenTofu.
