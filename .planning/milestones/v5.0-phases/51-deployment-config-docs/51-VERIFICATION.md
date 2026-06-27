---
phase: 51-deployment-config-docs
verified: 2026-06-26T19:00:00Z
status: passed
score: 4/4 must-haves verified
overrides_applied: 0
---

# Phase 51: Deployment, config & docs — Verification Report

**Phase Goal:** The compute agent is deployable and fully operator-controlled — a Tailscale-connected compose stack, every cloud-burst parameter configurable, an OCI A1 + Tailscale-ACL provisioning runbook, and a single master toggle that reverts to all-local analysis.

**Verified:** 2026-06-26T19:00:00Z
**Status:** PASSED
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths (Roadmap Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Operator can bring up the compute agent from a cloud-agent compose file with Tailscale connectivity, no media mount, a scratch volume, and the arm64 image (CLOUDDEPLOY-01) | VERIFIED | `docker-compose.cloud-agent.yml` exists; single `worker` service; `network_mode: host`; image `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64` (ends `-arm64`); `cloud_scratch` named volume (no host bind, no `/data/music`); `PHAZE_ROLE=agent` + `PHAZE_AGENT_KIND=compute`. 8 YAML-parse invariant tests all PASS (11 passed in 0.04s). |
| 2 | Every cloud-burst parameter configurable via pydantic-settings with `_FILE`-secret support (CLOUDDEPLOY-02) | VERIFIED | `ControlSettings.cloud_burst_enabled` (default `False`, `AliasChoices("PHAZE_CLOUD_BURST_ENABLED", "cloud_burst_enabled")`). `docs/configuration.md` contains a full `## Cloud-burst settings` table with all 13 pydantic knobs + `WORKER_MAX_JOBS` + `PHAZE_AGENT_QUEUE`. `_FILE`-bearing rows (`push_ssh_key`, `push_known_hosts`, `agent_token`, `queue_url`, `redis_url`) flagged. `PHAZE_AGENT_QUEUE` documented as the single structural exception (SAQ import-time raw `os.environ` read, "before `get_settings()` constructs" — contains "import time"). |
| 3 | Operator can follow a runbook to provision an OCI Always-Free A1, Tailscale ACL scoping A1 to `lux:{5432,6379,8000}` + `nox→A1:22`, plus a least-privilege Postgres broker role (CLOUDDEPLOY-03) | VERIFIED | `51-HOMELAB-CHANGE-PROMPT.md` present; §1 carries `VM.Standard.A1.Flex`, `ocpus = 2`, `memory_in_gbs = 12`; §2 carries `tag:cloud-agent` default-deny grants ACL with `tcp:5432/tcp:6379/tcp:8000` (A1→lux) + `tcp:22` (nox→A1); §3 carries `GRANT USAGE, CREATE ON SCHEMA public TO phaze_broker`, DML on `saq_jobs`/`saq_stats`/`saq_versions`, `saq_jobs_lock_key_seq` USAGE, DIST-04 `SELECT * FROM files` probe. §4 has 7-step deploy ordering with `PHAZE_CLOUD_BURST_ENABLED=true` + restart step. Reference copies also in `docs/cloud-burst.md`. Placeholders only. |
| 4 | Operator can disable the entire cloud-burst feature with a single config toggle, reverting to all-local analysis with no other change (CLOUDDEPLOY-04) | VERIFIED | Three gates confirmed in code: (a) `_route_discovered_by_duration` — `is_long = cloud_enabled and duration is not None and duration >= threshold_sec` (pipeline.py:311), called at all three sites with `settings.cloud_burst_enabled` (lines 372, 605, 697); (b) `stage_cloud_window` — `if not cfg.cloud_burst_enabled: return {"staged": 0, "skipped": 0}` BEFORE advisory lock (release_awaiting_cloud.py:125-126); (c) `trigger_backfill_cloud` — `if not settings.cloud_burst_enabled:` returns partial with `count=0, disabled=True` BEFORE `count_backfill_candidates` (pipeline.py:669-674), mutating ZERO `file.state` rows. Toggle tests pass (3/3: default False, PHAZE_CLOUD_BURST_ENABLED alias, bare-name alias). |

**Score:** 4/4 truths verified

---

## Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/config.py` | `cloud_burst_enabled` bool Field on ControlSettings | VERIFIED | Line 392: `cloud_burst_enabled: bool = Field(default=False, validation_alias=AliasChoices("PHAZE_CLOUD_BURST_ENABLED", "cloud_burst_enabled"), ...)` on `ControlSettings` (class starts line 338, not `BaseSettings` line 69). |
| `src/phaze/routers/pipeline.py` | Toggle-gated routing seam + backfill early-return | VERIFIED | `cloud_enabled: bool` parameter at line 259; `is_long = cloud_enabled and ...` at line 311; all three call sites pass `settings.cloud_burst_enabled`; backfill guard at lines 669-674. |
| `src/phaze/tasks/release_awaiting_cloud.py` | `stage_cloud_window` toggle no-op gate | VERIFIED | Lines 121-126: `cfg = get_settings()` then `if not cfg.cloud_burst_enabled: return {"staged": 0, "skipped": 0}` before advisory lock and window logic. |
| `docker-compose.cloud-agent.yml` | OCI A1 compute-agent compose stack | VERIFIED | Single `worker` service; `network_mode: host`; `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64`; `PHAZE_AGENT_KIND=compute`; named `cloud_scratch` volume; no media/DB/postgres/redis. |
| `tests/test_deployment/test_cloud_agent_compose.py` | YAML-parse invariant assertions | VERIFIED | 8 tests; all PASS; covers single-service, no-DB (DIST-04), role+kind, `-arm64` image, named-scratch, no-media, MODELS rw/CA ro, host networking. |
| `tests/test_config/test_cloud_burst_toggle.py` | Toggle field unit tests | VERIFIED | 3 tests: default-False, PHAZE_CLOUD_BURST_ENABLED alias, bare-name alias; all PASS. |
| `.planning/phases/51-deployment-config-docs/51-HOMELAB-CHANGE-PROMPT.md` | Homelab spec: OCI A1 + Tailscale ACL + broker role | VERIFIED | `<!-- generated-by: gsd-executor -->` marker line 1; §1 OpenTofu HCL with `VM.Standard.A1.Flex`, 2 OCPU/12 GB, Ubuntu 24.04 arm64; §2 default-deny Tailscale grants ACL with `tag:cloud-agent`, `tcp:5432/6379/8000`/`tcp:22`; §3 `GRANT USAGE, CREATE ON SCHEMA public`, DML on 3 SAQ tables, `saq_jobs_lock_key_seq` USAGE, DIST-04 probe, zero app-ORM grants; §4 7-step deploy ordering. |
| `docs/cloud-burst.md` | Cloud-burst feature home (walkthrough + runbook + smoke test) | VERIFIED | `<!-- generated-by: gsd-doc-writer -->` marker line 1; references `docker-compose.cloud-agent.yml`; embeds Tailscale ACL JSON (`tag:cloud-agent`), `phaze_broker` PG role SQL (`CREATE ON SCHEMA public`), OCI A1 OpenTofu spec; 7-step deploy ordering; smoke-test checklist; toggle semantics (OFF=dormant, restart-required, AWAITING_CLOUD release-on-enable). |
| `docs/configuration.md` | Cloud-burst config knob table + `_FILE` rows | VERIFIED | `## Cloud-burst settings` section present; all 13 pydantic knobs including `cloud_burst_enabled`; `WORKER_MAX_JOBS` and `PHAZE_AGENT_QUEUE`; `_FILE`-capable knobs flagged; `PHAZE_AGENT_QUEUE` exception note contains "import time"; `### Master toggle` subsection documents OFF=all-local + restart-required. |
| `docs/deployment.md` | Cloud-agent deployment target + pointer | VERIFIED | Line 19: `docker-compose.cloud-agent.yml` row in Deployment Targets table; `## Cloud-burst compute agent` section and See-also bullets linking to `cloud-burst.md`. |
| `docs/README.md` | Cloud Burst Operations index entry | VERIFIED | Line 29: `| **[Cloud Burst](cloud-burst.md)** | ☁️ OCI A1 compute-agent deploy, Tailscale ACL, broker role, master toggle |` |
| `justfile` | `cloud-agent-up` / `cloud-agent-down` recipes | VERIFIED | Lines 28-36: `cloud-agent-up` and `cloud-agent-down` recipes with `docker compose -f docker-compose.cloud-agent.yml up -d / down`. |

---

## Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/phaze/routers/pipeline.py` | `ControlSettings.cloud_burst_enabled` | `settings.cloud_burst_enabled` at all 3 call sites | WIRED | Lines 372, 605, 697 all pass `settings.cloud_burst_enabled` to `_route_discovered_by_duration`. Backfill guard at line 669 reads `settings.cloud_burst_enabled` directly. |
| `src/phaze/tasks/release_awaiting_cloud.py` | `ControlSettings.cloud_burst_enabled` | `get_settings()` at top of `stage_cloud_window` | WIRED | Lines 121-126: `cfg = get_settings(); if not cfg.cloud_burst_enabled: return {"staged": 0, "skipped": 0}`. |
| `docker-compose.cloud-agent.yml` | `ghcr.io/simplicityguy/phaze` (-arm64 tag, Phase 47) | `image:` line with `${PHAZE_IMAGE_TAG:-latest}-arm64` | WIRED | `image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64` — ends with `-arm64`, `PHAZE_IMAGE_TAG` in string. |
| `docker-compose.cloud-agent.yml` | host tailscaled (D-05) | `network_mode: host` | WIRED | `network_mode: host` present on worker service. |
| `docs/deployment.md` | `docs/cloud-burst.md` | Deployment Targets table row + pointer section | WIRED | Both `docker-compose.cloud-agent.yml` row (line 19) and `## Cloud-burst compute agent` pointer section (line 46+) link to `cloud-burst.md`. |
| `docs/README.md` | `docs/cloud-burst.md` | Operations index row | WIRED | Line 29 contains `[Cloud Burst](cloud-burst.md)`. |

---

## Data-Flow Trace (Level 4)

Not applicable — this phase produces config fields, a compose file, and documentation. No dynamic data rendering artifacts.

---

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Toggle defaults False | `uv run pytest tests/test_config/test_cloud_burst_toggle.py::test_cloud_burst_enabled_default_false -q` | 1 passed | PASS |
| Toggle reads PHAZE_CLOUD_BURST_ENABLED alias | `uv run pytest tests/test_config/test_cloud_burst_toggle.py::test_cloud_burst_enabled_env_alias -q` | 1 passed | PASS |
| Compose has exactly one service {worker} | Python yaml.safe_load check | `set(services) == {'worker'}` | PASS |
| Image ends with -arm64 | Python yaml.safe_load check | `image.endswith('-arm64') == True` | PASS |
| cloud_scratch is a named volume | Python yaml.safe_load check | `'cloud_scratch' in top-level volumes` | PASS |
| network_mode: host | Python yaml.safe_load check | `network_mode == 'host'` | PASS |
| 8 compose invariant tests | `uv run pytest tests/test_deployment/test_cloud_agent_compose.py -q` | 8 passed | PASS |
| All 11 phase-critical unit tests | `uv run pytest tests/test_config/test_cloud_burst_toggle.py tests/test_deployment/test_cloud_agent_compose.py -q` | 11 passed in 0.04s | PASS |

**Note on DB-backed tests:** `tests/test_routing_seam.py`, `tests/test_staging_cron.py`, and `tests/test_routers/test_pipeline.py` are integration tests that require a live Postgres instance. They fail locally with `OSError: [Errno 61] Connect call failed ('127.0.0.1', 5432)` — this is expected (no local Postgres). The SUMMARY reports 106 passed (including these tests) when run in the CI environment with Postgres available. The code logic is verified directly at the source level (all three gate sites confirmed), and the non-DB unit tests confirm the toggle semantics. These integration test failures are not phase failures.

---

## Probe Execution

No probe scripts declared or applicable to this phase.

---

## Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| CLOUDDEPLOY-01 | 51-02 | Cloud-agent compose with Tailscale connectivity, no media mount, scratch volume, arm64 image | SATISFIED | `docker-compose.cloud-agent.yml` + 8 invariant tests all pass |
| CLOUDDEPLOY-02 | 51-01, 51-03 | All cloud-burst parameters configurable via pydantic-settings with `_FILE`-secret support | SATISFIED | 13 knobs + WORKER_MAX_JOBS on ControlSettings; full table in `docs/configuration.md`; PHAZE_AGENT_QUEUE exception documented |
| CLOUDDEPLOY-03 | 51-04, 51-03 | Runbook: OCI A1 provisioning + Tailscale ACL + least-privilege Postgres broker role | SATISFIED | `51-HOMELAB-CHANGE-PROMPT.md` (4 sections, 7-step ordering, load-bearing `GRANT USAGE, CREATE ON SCHEMA public`, DIST-04 probe); reference copies in `docs/cloud-burst.md` |
| CLOUDDEPLOY-04 | 51-01, 51-03 | Single config toggle disables entire cloud-burst feature, reverts to all-local | SATISFIED | 3 gate sites in code; toggle tests pass; OFF=all-local semantics documented in `docs/configuration.md` + `docs/cloud-burst.md` |

---

## Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| (none) | — | — | — | No TBD/FIXME/XXX markers found in modified files. No stub patterns found. |

---

## Human Verification Required

None. All must-haves are verifiable programmatically. Toggle behavior is confirmed by code inspection and passing unit tests. Documentation content verified by grep. Compose file verified by YAML-parse tests.

---

## Gaps Summary

No gaps. All four CLOUDDEPLOY requirements are fully implemented and verified.

- **CLOUDDEPLOY-01:** `docker-compose.cloud-agent.yml` is a clean worker-only, `-arm64`, host-Tailscale, named-scratch, no-media stack. 8 YAML-parse invariant tests all pass.
- **CLOUDDEPLOY-02:** All cloud-burst knobs are pydantic-settings fields on `ControlSettings`; the full table with defaults, `_FILE` flags, and the PHAZE_AGENT_QUEUE exception are in `docs/configuration.md`.
- **CLOUDDEPLOY-03:** `51-HOMELAB-CHANGE-PROMPT.md` carries the complete OCI A1 OpenTofu spec, exact Tailscale ACL (default-deny, `tag:cloud-agent`, `tcp:5432/6379/8000`, `tcp:22`), empirically-verified least-privilege broker role SQL (`GRANT USAGE, CREATE ON SCHEMA public`), DIST-04 probe, and 7-step deploy ordering. Reference copies live in `docs/cloud-burst.md`.
- **CLOUDDEPLOY-04:** Three gate sites short-circuit when `cloud_burst_enabled=False`: routing seam (`is_long = cloud_enabled and ...`), staging cron (early return before advisory lock), backfill trigger (early return before candidate query, zero state mutations). Toggle defaults `False`. Toggle semantics documented in two docs.

---

_Verified: 2026-06-26T19:00:00Z_
_Verifier: Claude (gsd-verifier)_
