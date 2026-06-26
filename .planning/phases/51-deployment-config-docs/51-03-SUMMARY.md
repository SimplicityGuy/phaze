---
phase: 51-deployment-config-docs
plan: 03
subsystem: docs
tags: [cloud-burst, documentation, configuration, deployment, runbook]
requires:
  - "51-01: cloud_burst_enabled toggle in src/phaze/config.py"
  - "51-02: docker-compose.cloud-agent.yml + justfile recipes"
  - "51-04: 51-HOMELAB-CHANGE-PROMPT.md"
provides:
  - "docs/cloud-burst.md: cloud-burst feature home (walkthrough + runbook + smoke test)"
  - "docs/configuration.md: full cloud-burst config knob table + _FILE rows"
  - "docs/deployment.md: cloud-agent deployment target + pointer"
  - "docs/README.md: Cloud Burst Operations index entry"
affects:
  - docs/configuration.md
  - docs/cloud-burst.md
  - docs/deployment.md
  - docs/README.md
tech-stack:
  added: []
  patterns:
    - "gsd-doc-writer line-1 marker on every doc"
    - "numbered ## Step N operator-walkthrough headings (mirror deployment.md)"
    - "config knob table sourced from config.py Field(...) descriptions"
key-files:
  created:
    - docs/cloud-burst.md
  modified:
    - docs/configuration.md
    - docs/deployment.md
    - docs/README.md
decisions:
  - "Documented PHAZE_AGENT_QUEUE as the single structural exception to CLOUDDEPLOY-02's pydantic-settings coverage (raw os.environ read at SAQ import time, before get_settings())"
  - "Master-toggle semantics documented in both configuration.md (canonical) and cloud-burst.md (operator context): OFF=all-local, flip requires control-plane restart, in-flight drains"
  - "Runbook reference copies (Tailscale ACL JSON, phaze_broker PG SQL, OCI A1 OpenTofu spec) live in cloud-burst.md; deployment.md only points (D-13, keeps deployment.md from bloating)"
metrics:
  duration: ~12 min
  completed: 2026-06-26
  tasks: 3
  files: 4
---

# Phase 51 Plan 03: Cloud-burst documentation Summary

Documented the entire cloud-burst feature surface: the full config-knob table in
`docs/configuration.md`, a new single feature-home `docs/cloud-burst.md` (compose walkthrough +
homelab runbook + smoke test), and pointers from `docs/deployment.md` and the `docs/README.md`
Operations index. Satisfies CLOUDDEPLOY-02 (config docs), CLOUDDEPLOY-03 (runbook reference
copies), and CLOUDDEPLOY-04 (master-toggle semantics).

## What was built

### Task 1 — `docs/configuration.md` cloud-burst knob table (commit 7ee0cad)
- New `## Cloud-burst settings` section with a 6-column table (Knob / Env var / Class / Default /
  `_FILE`? / Description) covering all 13 cloud knobs plus the two criterion-named knobs
  `WORKER_MAX_JOBS` (agent concurrency; documented `=1` on the 12 GB A1) and `PHAZE_AGENT_QUEUE`
  (cloud queue name).
- Extended the existing `_FILE` table with `push_ssh_key` and `push_known_hosts`, flagged as the
  only **whitespace-preserved** `_FILE` secrets (the other cloud `_FILE` rows — `queue_url`,
  `redis_url`, `agent_token` — were already present).
- Added an explicit callout that `PHAZE_AGENT_QUEUE` is the **single structural exception** to
  CLOUDDEPLOY-02's "all params via pydantic-settings" coverage: it is read as a raw `os.environ`
  lookup at SAQ module-import time, before `get_settings()` constructs — so it cannot be a
  settings field. Contains "import time" as the verifier-checked phrase.
- Added a `### Master toggle` subsection documenting OFF=all-local + restart-required + in-flight
  drains.
- Descriptions sourced verbatim-in-spirit from the `Field(...)` text in `src/phaze/config.py`
  (verified against the real fields, including the new `cloud_burst_enabled` from Plan 01).

### Task 2 — `docs/cloud-burst.md` feature home (commit e1fbd2f)
- New doc, line-1 `gsd-doc-writer` marker, structured as a 7-step operator walkthrough mirroring
  `deployment.md`: OpenTofu apply → Tailscale ACL → `phaze_broker` role SQL → `-arm64` release →
  compose up → flip toggle + restart → smoke test.
- Reference copies embedded (phaze is source-of-truth spec): the Tailscale **grants ACL JSON**
  (`tag:cloud-agent`), the least-privilege **`phaze_broker` PG role SQL** (with the load-bearing
  `GRANT USAGE, CREATE ON SCHEMA public` finding + explanatory comment + DIST-04 probe), and the
  **OCI A1 OpenTofu** spec (2 OCPU / 12 GB).
- Compose guidance for `docker-compose.cloud-agent.yml`: worker-only, `-arm64`, named scratch,
  `network_mode: host` tailscaled, the full production `.env` (broker DSN, https API, passworded
  redis, `kind=compute`, `WORKER_MAX_JOBS=1`, scratch-dir match), and the two production guards
  (https + passworded redis).
- Smoke-test checklist (agent registers, long file routes cloud, push transfers, compute drains
  `process_file`, scratch cleaned).
- Toggle + runtime-state notes (OFF=dormant, restart-required, `AWAITING_CLOUD` release-on-enable,
  nox `PHAZE_PUSH_KNOWN_HOSTS` re-provision). Links to `configuration.md` for the canonical knob
  table rather than duplicating it. Placeholders only — no real secrets/IPs.

### Task 3 — Pointers + index (commit a962706)
- `docs/deployment.md`: added the `docker-compose.cloud-agent.yml` row to the Deployment Targets
  table, a `## Cloud-burst compute agent` pointer section, and See-also bullets — the vendor
  runbook stays out of deployment.md (D-13).
- `docs/README.md`: added the `Cloud Burst` row to the Operations index with the ☁️ emoji-prefixed
  purpose, mirroring the Deployment Guide row.

## Verification
- All `grep` acceptance checks pass (Task 1/2/3 automated checks all returned `grep-ok`).
- `pre-commit run --files <each doc>` exits clean for all four files (whitespace/EOF/large-file/
  secret-scan hooks Passed; language-specific hooks correctly skipped for markdown).
- All four docs retain their line-1 `<!-- generated-by: gsd-doc-writer -->` marker.

## Deviations from Plan

None — plan executed exactly as written. All facts (config field names, env-var aliases, the
compose service shape, the `_FILE`-preserved-whitespace set, the deploy ordering) were verified
against the real `src/phaze/config.py`, `docker-compose.cloud-agent.yml`, and
`51-HOMELAB-CHANGE-PROMPT.md` rather than invented.

## Known Stubs

None. All four docs are complete operator-facing content with no placeholders beyond the
intentional secret placeholders (`<strong-unique-password>`, `100.x.x.x` tailnet IPs) that the
threat model (T-51-11) requires.

## Self-Check: PASSED
- FOUND: docs/cloud-burst.md
- FOUND: docs/configuration.md (modified)
- FOUND: docs/deployment.md (modified)
- FOUND: docs/README.md (modified)
- FOUND commit: 7ee0cad (Task 1)
- FOUND commit: e1fbd2f (Task 2)
- FOUND commit: a962706 (Task 3)
