---
phase: 51-deployment-config-docs
plan: 04
subsystem: deployment-spec
tags: [cloud-burst, oci-a1, opentofu, tailscale, postgres-broker, homelab-change-prompt, dist-04]
requires:
  - "51-RESEARCH.md: OCI A1 OpenTofu spec, Tailscale grants ACL, least-privilege Postgres role SQL, deploy ordering"
  - "Phase 36 HOMELAB-CHANGE-PROMPT.md: change-prompt structure (line-1 marker, paste blockquote, numbered sections, done-when checklist)"
provides:
  - "51-HOMELAB-CHANGE-PROMPT.md: ready-to-paste homelab spec for OCI A1 + Tailscale ACL + phaze_broker role (CLOUDDEPLOY-03)"
affects:
  - "homelab repo (OpenTofu OCI A1 module, tailnet grants policy, lux Postgres role) — applied there, specced here"
tech-stack:
  added: []
  patterns:
    - "Cross-repo change prompt: phaze is source-of-truth spec, homelab applies live infra (D-09/D-10 workspace boundary)"
    - "Placeholder discipline: var.* / 100.x.x.x / <strong-unique-password> — never real secrets (Phase 36 precedent)"
key-files:
  created:
    - ".planning/phases/51-deployment-config-docs/51-HOMELAB-CHANGE-PROMPT.md"
  modified: []
decisions:
  - "Spec the A1 at 2 OCPU / 12 GB (current June-2026 Always-Free limit), not 4/24; WORKER_MAX_JOBS=1"
  - "Broker role gets CREATE ON SCHEMA public (empirically unavoidable: SAQ init_db unconditional CREATE TABLE IF NOT EXISTS saq_versions, PG checks schema-CREATE before IF-NOT-EXISTS) + DML on 3 SAQ tables + sequence USAGE + ZERO app-ORM grants"
  - "Dedicated-saq-schema hardening documented as optional, not default (relocates live queue table — riskier homelab change)"
metrics:
  duration: ~10m
  completed: 2026-06-26
  tasks: 2
  files: 1
---

# Phase 51 Plan 04: Homelab Change Prompt (OCI A1 + Tailscale + broker role) Summary

Authored the cross-repo deliverable that lets the homelab repo provision the live v5.0 Cloud Burst infrastructure (CLOUDDEPLOY-03): a single ready-to-paste change prompt carrying the OpenTofu OCI Always-Free A1 module spec, the exact default-deny Tailscale grants ACL JSON, and the empirically-verified least-privilege `phaze_broker` Postgres role SQL — with phaze as source-of-truth spec and all live infra applied in homelab (D-09/D-10).

## What Was Built

`.planning/phases/51-deployment-config-docs/51-HOMELAB-CHANGE-PROMPT.md` — line-1 `generated-by: gsd-executor` HTML-comment marker, paste-the-section blockquote, a `## Context for the homelab agent` summary, then four numbered change sections plus a done-when checklist:

- **§1 Provision the OCI Always-Free A1 (OpenTofu)** — `hcl` block (`oci_core_instance`, `VM.Standard.A1.Flex`, `ocpus = 2` / `memory_in_gbs = 12`, Canonical Ubuntu 24.04 Minimal aarch64 image OCID var, `assign_public_ip = true`, `ssh_authorized_keys` var) plus the VCN/subnet/IGW/route-table/security-list note, the **2 OCPU/12 GB June-2026 capacity gotcha** (retry across ADs/regions on Out of Capacity), and the cloud-init `tailscaled` + `rsync` install. Security list = outbound-mostly (Tailscale is the real ACL).
- **§2 Apply the Tailscale grants ACL** — `jsonc` default-deny grants block verbatim from RESEARCH: `tagOwners` `tag:cloud-agent`, `hosts` lux/nox, grants `A1 → lux` on `tcp:{5432,6379,8000}` + `nox → A1` on `tcp:22`. Placeholder tailnet IPs (`100.x.x.x`).
- **§3 Create the least-privilege phaze_broker Postgres role (lux)** — `sql` block verbatim: `CREATE ROLE phaze_broker`; `GRANT USAGE, CREATE ON SCHEMA public` (with the load-bearing rationale that SAQ `init_db` runs an unconditional `CREATE TABLE IF NOT EXISTS saq_versions` and PG checks schema-CREATE *before* the IF-NOT-EXISTS short-circuit — empirically verified on live PG18); DML on `saq_jobs`/`saq_stats`/`saq_versions`; `saq_jobs_lock_key_seq` USAGE; the DIST-04 `SELECT * FROM files LIMIT 1` probe that MUST ERROR; full-role-boots-first prerequisite; PG15+ schema-CREATE note; optional dedicated-saq-schema hardening (document-don't-default).
- **§4 Deploy ordering** — 7-step sequence (OpenTofu apply → tailnet ACL → broker role SQL → v5.0.x `-arm64` GHCR release → A1 `.env` + `docker compose -f docker-compose.cloud-agent.yml up -d` → `PHAZE_CLOUD_BURST_ENABLED=true` + control-plane restart → smoke test), via `datum@nox` / `datum@lux`. Closing notes: off-by-default ships dormant, toggle flip requires a control-plane restart (startup-read, Pitfall 6), nox `PHAZE_PUSH_KNOWN_HOSTS` re-provision with the A1 host key.

## Task Commits

| Task | Name | Commit | Files |
| ---- | ---- | ------ | ----- |
| 1 | OpenTofu OCI A1 module spec + Tailscale grants ACL sections | 914d1b5 | 51-HOMELAB-CHANGE-PROMPT.md |
| 2 | Least-privilege broker role SQL + deploy ordering + verification | ceb9bc8 | 51-HOMELAB-CHANGE-PROMPT.md |

## Verification

- Task 1 grep: `VM.Standard.A1.Flex` + `tcp:5432` + `tag:cloud-agent` → ok
- Task 2 grep: `CREATE ON SCHEMA public` + `saq_jobs_lock_key_seq` + `PHAZE_CLOUD_BURST_ENABLED=true` → ok
- DIST-04: no `GRANT … (files|metadata|agents)` present (confirmed via regex) — broker role carries zero app-ORM grants
- Line 1 is the `gsd-executor` generated-by HTML-comment marker
- Placeholders only — no real IPs/OCIDs/passwords (`100.x.x.x`, `var.*`, `<strong-unique-password>`)
- `pre-commit run --files 51-HOMELAB-CHANGE-PROMPT.md` → all hooks Passed/Skipped (clean)

## Deviations from Plan

None - plan executed exactly as written.

## Threat Model Coverage

- **T-51-07 (broker info disclosure):** mitigated — role limited to SAQ-table DML + sequence USAGE + `CREATE ON SCHEMA public`; zero app-ORM grants; DIST-04 probe included.
- **T-51-08 (broker over-broad EoP):** accepted-with-note — `CREATE ON SCHEMA public` is empirically unavoidable; residual risk documented, optional dedicated-saq-schema hardening offered.
- **T-51-09 (Tailscale ACL scope):** mitigated — default-deny grants give exactly `lux:{5432,6379,8000}` outbound + `nox:22` inbound.
- **T-51-10 (secrets in prompt):** mitigated — placeholders only.
- **T-51-SC (package installs):** accept — spec/markdown deliverable only, no installs.

## Self-Check: PASSED

- FOUND: .planning/phases/51-deployment-config-docs/51-HOMELAB-CHANGE-PROMPT.md
- FOUND commit: 914d1b5
- FOUND commit: ceb9bc8
