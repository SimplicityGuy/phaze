---
phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq
plan: 04
subsystem: docs
tags: [docs, homelab, deployment, saq, postgres, redis, queue, secrets]

# Dependency graph
requires:
  - phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq
    plan: 01
    provides: PHAZE_QUEUE_URL config field (libpq DSN, SECRET_FILE_FIELDS) + build_pipeline_queue factory
  - phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq
    plan: 02
    provides: all four queue-construction sites on PostgresQueue; per-queue pool sizing (control 2/8, agent 1/4); producer pools opened
provides:
  - "36-HOMELAB-CHANGE-PROMPT.md: paste-ready operator change request for the homelab repo agent (Step D deliverable)"
  - "In-repo docs (README, deployment, configuration, .env.example) describe Postgres-as-broker / Redis-as-cache and document the secret-backed PHAZE_QUEUE_URL"
affects: [homelab-redeploy, operator-runbook]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Docs steer PHAZE_QUEUE_URL (DB-credential secret) to the <VAR>_FILE convention, mirroring DATABASE_URL"
    - "Operator-facing change prompt enumerates the agent→Postgres:5432 firewall edge + connection budget vs max_connections"

key-files:
  created:
    - .planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-HOMELAB-CHANGE-PROMPT.md
  modified:
    - README.md
    - docs/deployment.md
    - docs/configuration.md
    - .env.example

key-decisions:
  - "Documented PHAZE_QUEUE_URL as the raw libpq form everywhere (postgresql://, never +asyncpg) — matches the config.py default + _strip_sqlalchemy_driver validator"
  - "Surfaced the agent→Postgres:5432 firewall edge as an intentional relaxation of Phase-26 D-25, with the optional production credential-guard note (RESEARCH Open Q1, still deferred)"
  - "Framed the change prompt as the migration-specific portion only, with final consolidation deferred to after Phase 38"

requirements-completed: [REQ-36-1]

# Metrics
duration: 22min
completed: 2026-06-12
---

# Phase 36 Plan 04: Homelab Change-Prompt + Doc Migration Summary

**Produced the Step D homelab change-prompt (`36-HOMELAB-CHANGE-PROMPT.md`) covering the full Redis→Postgres broker cutover, and brought README/deployment/configuration/.env.example in line with the new reality: PostgreSQL is the SAQ broker, Redis is cache-only, and `PHAZE_QUEUE_URL` is the secret-backed libpq broker DSN.**

## Performance

- **Duration:** ~22 min
- **Completed:** 2026-06-12
- **Tasks:** 2
- **Files modified:** 5 (1 created, 4 modified)

## Accomplishments

- **Task 1 — Homelab change-prompt:** A paste-ready operator change request for the homelab repo agent covering all seven required topics: (1) `PHAZE_QUEUE_URL` (libpq DSN, `<VAR>_FILE` secret) on BOTH control and agent compose services; (2) `saq[redis]`→`saq[postgres]` image dep swap + rebuild with the libpq-on-slim-base verification note; (3) `saq_jobs`/`saq_stats`/`saq_versions` first-boot advisory-lock auto-DDL + the DB-role grants (CREATE in own schema + LISTEN/NOTIFY); (4) the agent→Postgres:5432 firewall edge (relaxing D-25) + the optional production credential-guard note; (5) the per-queue connection budget (control 2/8, agent 1/4) vs Postgres `max_connections`; (6) Redis demoted to cache-only; (7) control-first → agents redeploy ordering with the Phase-32 boot re-enqueue self-heal (no job-data migration). Placeholders only — no real DSN/password inlined.
- **Task 2 — In-repo docs:** README services table + Mermaid node labels (`PG` = "DB + SAQ broker", `REDIS` = "cache only") + key-features + tech-stack now show Postgres-as-broker; `docs/deployment.md` redis row, the DIST-04 agent-network paragraph, prerequisites firewall list, agent env vars, production-critical table, and the `<VAR>_FILE` secrets list all updated; `docs/configuration.md` documents the `PHAZE_QUEUE_URL` core setting + its `_FILE` sibling + defaults + per-environment override; `.env.example` gains a libpq `PHAZE_QUEUE_URL` block with a `_FILE` sibling, mirroring the `DATABASE_URL` block.

## Task Commits

1. **Task 1: Homelab change-prompt deliverable** — `5703a63` (docs)
2. **Task 2: README/deployment/configuration/.env.example doc migration** — `77f7e92` (docs)

## Deviations from Plan

None — plan executed as written. `scripts/update-project.sh` was confirmed to need no manual edit (it auto-syncs dependency floors; the saq/redis floors require no cap), as the plan anticipated.

## Issues Encountered

- **Transient `ENOSPC` (host disk near-full, ~180Mi free):** the first write to `docs/configuration.md` failed with "no space left on device". Verified the file was not corrupted (still intact at 250 lines, prior README/deployment edits preserved), then retried the same edit successfully. This is a host-environment condition, not a content problem; all subsequent writes and both commits (with full pre-commit hooks) succeeded cleanly.

## Authentication Gates

None.

## Threat Surface

- **T-36-03 (Elevation/Spoofing — agent→Postgres:5432 edge):** mitigated in docs — the homelab prompt + `deployment.md` require firewalling agents→Postgres on the private LAN, a least-privilege DB role (CREATE only in its own schema + LISTEN/NOTIFY), and flag the optional production credential-guard analog (RESEARCH Open Q1).
- **T-36-02 (Information Disclosure — `PHAZE_QUEUE_URL` credentials):** mitigated in docs — every surface steers to the `<VAR>_FILE` secret convention; no real password is inlined in the prompt or `.env.example` (placeholders / the existing `phaze:phaze` dev default only).
- **T-36-10 (Tampering — first-boot DDL under shared schema):** mitigated in docs — the prompt documents the advisory-lock-guarded idempotent `init_db()` and that the role should CREATE only in its own schema.

No new security surface introduced (docs-only plan).

## Known Stubs

None.

## Verification

- Task 1 grep gate: `PHAZE_QUEUE_URL` + `saq[postgres]` + `5432|firewall` + `saq_jobs` all present → PASS
- Task 2 grep gate: `PHAZE_QUEUE_URL` in `.env.example` + `deployment.md` + `configuration.md`, and the legacy "Task-queue broker and cache" phrase removed from README → PASS
- No real credentials in any touched file (only the existing `phaze:phaze` dev placeholder + `<user>:<password>` placeholders)
- Both commits passed the full pre-commit hook suite (no `--no-verify`)

## Next Phase Readiness

- The migration's operator deliverable is complete: the homelab agent has a paste-ready prompt, and the in-repo docs no longer call Redis the broker. Final env/secret consolidation is deferred to after Phase 38 per the ROADMAP Step D note.
- Blockers: none.

## Self-Check: PASSED

- `36-HOMELAB-CHANGE-PROMPT.md` exists on disk (verified)
- Commits `5703a63` + `77f7e92` present in git log (verified)

---
*Phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq*
*Completed: 2026-06-12*
