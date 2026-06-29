---
phase: 56
slug: deployment-runbook-config-docs
status: verified
threats_open: 0
asvs_level: 2
created: 2026-06-28
---

# Phase 56 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Register authored at plan time across all 7 PLAN `<threat_model>` blocks; verified in
> *verify-mitigations* mode by gsd-security-auditor (no retroactive STRIDE needed).

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| control-plane → kube API | Probe + submit/reconcile GET/POST the Kueue LocalQueue/Jobs over the operator-provided mesh; failures must degrade, not crash | LocalQueue/Job/Workload metadata; SA bearer token |
| operator → runbook YAML | Operator-applied manifests; the RBAC floor must resist privilege drift | RBAC Role verbs, Secret refs |
| controller process → api process | The LocalQueue-unreachable signal crosses via shared Redis, not in-memory state | boolean reachability flag |
| Redis cache → SSR render | Dashboard reads the boot flag; a read failure must degrade silently | boolean flag |
| k8s pod → callback auth | One-shot pod authenticates with a bearer token; its Agent row must classify "never", never "dead" | bearer token, agent identity |
| operator → kube Secret (CA) | CA cert rides an operator-created K8s Secret; phaze references it by name only, never authors or logs it | private CA bytes (by reference) |
| operator → config (env/_FILE) | Documented knobs; secrets flagged for `_FILE` mounting, never inline | credentials (S3, Anthropic, agent token, CA) |
| static template strings → browser | Jinja autoescape; no operator free-text in alert/note copy | static UI strings |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-56-RBAC | Elevation of Privilege | runbook RBAC Role verbs | mitigate | `docs/k8s-burst.md:203-244` namespaced `kind: Role` (not ClusterRole), exact verb floor (jobs create/get/delete; workloads get/watch/list; localqueues get); "no cluster-wide grants" — enforced by `test_rbac_covers_call_graph` | closed |
| T-56-DOS | Denial of Service | controller.startup probe | mitigate | `controller.py:167-197` gated on `cloud_target=="k8s"`; kube GET, Redis persistence (CR-01), and off-k8s stale-flag clear (WR-01) each in separately-guarded try/except — none re-raises; boot cannot abort | closed |
| T-56-LOG | Information Disclosure | probe WARNING | mitigate | `controller.py:177-180,188,197` — static message names only `PHAZE_KUBE_LOCAL_QUEUE`; no token / kube DSN interpolated | closed |
| T-56-POLL | Denial of Service | get_localqueue_unreachable + dashboard render | mitigate | `pipeline.py:844-861` returns False on missing/erroring Redis (never raises); both render paths seed the flag (`routers/pipeline.py:507,589`); 5s poll never 500s | closed |
| T-56-XSS | Tampering | localqueue_card.html + agents.html | mitigate | `localqueue_card.html:21-31` + `admin/agents.html:17-23` static copy through Jinja autoescape; no operator string interpolated | closed |
| T-56-DEAD | Spoofing/Integrity | Agents UI liveness | mitigate | `agent_liveness.py:79-80` returns `'never'` when `last_seen_at is None` before any threshold math; one-shot pod never heartbeats — proven by `test_classify_never_not_dead_when_last_seen_at_none` | closed |
| T-56-TOKEN | Information Disclosure | bearer-token Secret | mitigate | `docs/k8s-burst.md:272` token via `PHAZE_AGENT_TOKEN_FILE`; `config.py:705 agent_token: SecretStr` (masked), in `SECRET_FILE_FIELDS` | closed |
| T-56-NAME | Tampering | kube object names | mitigate | `kube_staging.py:61-69` job name `phaze-analyze-{file_id}` (UUID, DNS-1123 safe); manifest `metadata.name` uses it; no operator free-text | closed |
| T-56-VER | Integrity/Availability | apiVersion drift | mitigate | `config.py:564-568 kube_workload_api_version` default `kueue.x-k8s.io/v1beta1`, consumed via `new_class(version=...)`; `docs/k8s-burst.md:344-366` lockstep rule + v1beta2 upgrade note | closed |
| T-56-SECRETS | Information Disclosure | documented credentials | mitigate | `docs/configuration.md:27-40,123-125,139-140` knob table flags every credential with its `_FILE` sibling; `config.py:48-49` SecretStr fields stay masked | closed |
| T-56-REVERT | Availability | revert procedure | mitigate | `docs/deployment.md:66-86` single-toggle `PHAZE_CLOUD_TARGET=local` + restart, "no other change", no teardown | closed |
| T-56-06-CA-EMPTY | Spoofing/TLS | mounted CA | mitigate | `agent_bootstrap.py:61-69` raises on missing/`st_size==0` CA; passes `verify=cfg.agent_ca_file`, never `verify=False` | closed |
| T-56-06-CA-LEAK | Information Disclosure | public GHCR image | mitigate | `Dockerfile.job` bakes no CA (mounted at runtime); CI `docker-publish.yml:625-674` passes only `BASE_IMAGE`, no CA secret | closed |
| (kube_ca mount) | Integrity/TLS | CA Secret volume mount | mitigate | `kube_staging.py:171-185` — `phaze-ca` volume from operator Secret `cfg.kube_ca_secret_name`, mounted `readOnly` at `/certs`; container sets `PHAZE_AGENT_CA_FILE=/certs/phaze-ca.crt` | closed |
| T-56-SC | Tampering | npm/pip/cargo installs | accept | Zero new external packages (`Dockerfile.job:6-7`) | closed |
| T-56-FILTER | Availability | agents table | accept | No `kind=compute` filter added (would hide the v5.0 A1 agent); accept the 'never' pill + ephemeral note | closed |
| T-56-SSRF | Tampering/SSRF | s3_endpoint_url doc | accept | Already validated http(s)+netloc at construction (`config.py:587-603 _validate_s3_endpoint_url`); doc only restates | closed |
| T-56-06-SECRET-NAME | Tampering | kube_ca_secret_name | accept | Plain object name (`config.py:569`), referenced by name, mounted `readOnly`; phaze never authors the Secret | closed |
| T-56-06-SC | Tampering | npm/pip/cargo installs | accept | Zero new external packages; `build_job_manifest` returns a plain dict | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-56-01 | T-56-SC / T-56-06-SC | Zero new external packages introduced this phase; supply-chain surface unchanged | Phase 56 plan author | 2026-06-28 |
| AR-56-02 | T-56-FILTER | A `kind=compute` filter would hide the legitimate v5.0 A1 agent; the 'never' liveness pill + ephemeral note is the correct UX | Phase 56 plan author (56-RESEARCH Pitfall 4) | 2026-06-28 |
| AR-56-03 | T-56-SSRF | `s3_endpoint_url` is already scheme/netloc-validated at construction; the doc only restates the existing constraint | Phase 56 plan author | 2026-06-28 |
| AR-56-04 | T-56-06-SECRET-NAME | `kube_ca_secret_name` is a plain object name, not a credential; the Secret is operator-authored and mounted read-only | Phase 56 plan author | 2026-06-28 |

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-28 | 19 | 19 | 0 | gsd-security-auditor (opus), verify-mitigations mode |

**Advisory (non-blocking, follow-up):** `.github/workflows/docker-publish.yml:556` carries a stale
comment claiming "the internal CA is baked from a repo secret (KJOB-05)" that contradicts the actual
phase-56 behavior. The real build step (L656-674) bakes no CA and the authoritative note (L625-630)
reverses KJOB-05, so the T-56-06-CA-LEAK mitigation holds. Comment corrected in this phase (see commit
trail) to remove the misleading reference.

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-28
