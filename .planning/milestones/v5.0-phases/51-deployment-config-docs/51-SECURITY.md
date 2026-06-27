---
phase: 51
slug: deployment-config-docs
status: verified
threats_open: 0
asvs_level: 2
created: 2026-06-26
---

# Phase 51 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> v5.0 Cloud-Burst deployment surface (master toggle, arm64 cloud-agent compose,
> homelab provisioning spec, docs). Retroactive audit of merged PR #159.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| operator config → control-plane routing | `cloud_burst_enabled` gates whether any file leaves the local trust boundary for a cloud target | file-routing decision (none when OFF) |
| compute agent (A1) → lux Postgres | Compute agent reaches ONLY `saq_jobs`/`saq_stats`/`saq_versions` via `PHAZE_QUEUE_URL`; never the app ORM (DIST-04) | queue rows (no app data) |
| OCI A1 host → tailnet | Host `tailscaled` is the network trust boundary; container uses `network_mode: host` under a default-deny grants ACL | saq_jobs broker, Redis cache, HTTP API traffic |
| nox → A1 | rsync-over-SSH push channel; only `nox:22` inbound to A1 | pushed long-file media to ephemeral scratch |
| documentation/spec → operator action | Docs + homelab spec instruct secret handling; must not leak real secrets or encourage insecure config | placeholder credentials only |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-51-01 | Tampering / EoP | master toggle OFF state | mitigate | OFF gates all 3 cloud entry points: routing seam `is_long = cloud_enabled and ...` (pipeline.py:311, fed by settings.cloud_burst_enabled at :372/:606); staging cron early no-op (release_awaiting_cloud.py:125); backfill early-return (pipeline.py:669) | closed |
| T-51-02 | Repudiation / scope drift | backfill path | mitigate | Explicit master-toggle early-return BEFORE the candidate query — mutates ZERO `file.state` rows when disabled (pipeline.py:665-674); not routing-seam-only | closed |
| T-51-03 | Insecure default | new toggle | accept | `cloud_burst_enabled` defaults `False` (config.py:392-396) — ships dormant; safe state is the default | closed |
| T-51-04 | Information disclosure | cloud compose env | mitigate | No `DATABASE_URL`/`POSTGRES_*`/postgres/redis service in compose (docker-compose.cloud-agent.yml:43-60); asserted by test_cloud_agent_compose_has_no_postgres_env (test:72-91) | closed |
| T-51-05 | Tampering (wrong arch) | worker image tag | mitigate | Image `...:${PHAZE_IMAGE_TAG:-latest}-arm64` (compose:45); asserted ends `-arm64` by test_worker_image_is_arm64_ghcr_pinned (test:118) | closed |
| T-51-06 | Information disclosure (secrets-in-compose) | _FILE secrets | mitigate | No plaintext secrets inlined; env only `PHAZE_ROLE`/`PHAZE_AGENT_KIND` + `env_file: .env` (compose:39-41,48-51); `*_FILE` resolution via SECRET_FILE_FIELDS (config.py:79,452) | closed |
| T-51-07 | Information disclosure | phaze_broker DB role | mitigate | Role granted USAGE+CREATE ON SCHEMA public + DML on 3 saq tables + sequence USAGE only, ZERO app-ORM grants; `SELECT * FROM files` probe MUST ERROR (cloud-burst.md:163-195; 51-HOMELAB:174-211) | closed |
| T-51-08 | Elevation of privilege | broker role over-broad | accept (with note) | `CREATE ON SCHEMA public` empirically unavoidable (SAQ init_db unconditional `CREATE TABLE IF NOT EXISTS`); residual risk documented + optional dedicated-saq-schema hardening offered (cloud-burst.md:184-202; 51-HOMELAB:163-219) | closed |
| T-51-09 | Spoofing / lateral movement | Tailscale ACL scope | mitigate | Default-deny grants: A1→lux `tcp:{5432,6379,8000}` + nox→A1 `tcp:22`, nothing else (51-HOMELAB:110-148; cloud-burst.md:113-143) | closed |
| T-51-10 | Information disclosure | secrets in change prompt | mitigate | Placeholders only — `100.x.x.x`/`100.y.y.y`, `<strong-unique-password>`, `var.*`, image-OCID vars; real-IP/real-password scan returned zero hits (51-HOMELAB.md) | closed |
| T-51-11 | Information disclosure | secrets in docs | mitigate | All ACL/SQL/.env examples use placeholders; config table flags `_FILE`-secret-bearing fields (cloud-burst.md, configuration.md:81-107); scan returned zero real secrets/IPs | closed |
| T-51-12 | Tampering / misconfiguration | runbook completeness | mitigate | Runbook carries CREATE ON SCHEMA finding (cloud-burst.md:156-186), production guards https+passworded-redis (cloud-burst.md:246-249; configuration.md:288-289), and scratch-dir-match warning (cloud-burst.md:251-253; configuration.md:93-94) | closed |
| T-51-SC | Tampering (supply chain) | package installs | accept | No package installs this phase (config field + compose YAML + docs/spec only); references an already-published GHCR `-arm64` image | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-51-03 | T-51-03 | Master toggle defaults `False`; feature ships dormant. Safe state is the default — no residual risk (config.py:392-396). | Phase 51 plan (51-01) | 2026-06-26 |
| AR-51-08 | T-51-08 | `CREATE ON SCHEMA public` empirically unavoidable: SAQ `init_db()` runs an unconditional `CREATE TABLE IF NOT EXISTS saq_versions` on every connect, and PostgreSQL checks schema-CREATE privilege before the IF-NOT-EXISTS short-circuit. Residual risk: broker can create new objects in `public`. Optional dedicated-`saq`-schema hardening documented for strict lockdown. | Phase 51 plan (51-04) | 2026-06-26 |
| AR-51-SC | T-51-SC | No package installs in this phase (config/compose/docs/spec only). Compose references an already-published GHCR image; OpenTofu/oci-provider/Tailscale tooling live in the homelab repo, not phaze. | Phase 51 plans (51-01..04) | 2026-06-26 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-26 | 13 | 13 | 0 | gsd-security-auditor |

Note (non-security, tracked separately): a deployment-correctness audit flagged that
`docker-compose.cloud-agent.yml:46` adds `command: uv run saq ...` which overrides the
Dockerfile CMD and may prevent the arm64 container from starting. This is a deployment bug,
not a security threat, and is out of scope for this audit — it does not affect the T-51-05
arm64-tag mitigation (the image tag invariant holds independent of the `command:` line).

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-26
