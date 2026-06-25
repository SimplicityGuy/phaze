---
phase: 49
slug: duration-routing-backfill
status: verified
threats_open: 0
asvs_level: 2
created: 2026-06-25
---

# Phase 49 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| operator env → ControlSettings | `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC` parsed into a bounded pydantic int Field | int config value |
| operator → POST /pipeline/analyze | triggers per-file duration routing; no operator free-text (file ids are server UUIDs) | HTTP request (no body params) |
| operator → POST /pipeline/backfill-cloud | candidate set is a server-side `ANALYSIS_FAILED ∧ duration≥threshold` query; no free-text | HTTP request (no body params) |
| control plane → compute/fileserver queues | per-agent named queues only (`task_router.queue_for(agent.id)`); never the consumer-less default queue | SAQ job payloads (process_file) |
| controller cron → compute queue | scheduled state-driven `release_awaiting_cloud`; no external input | SAQ job payloads (process_file) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-49-01 | Tampering | `cloud_route_threshold_sec` config | mitigate | Bounded pydantic `Field(default=5400, gt=0, lt=86400)` on `ControlSettings` (`src/phaze/config.py:365-371`); out-of-range fails at construction, never reaches the SQL compare | closed |
| T-49-02 | Injection | duration / backfill queries | mitigate | ORM with bound params (`src/phaze/services/pipeline.py:797-798, 827-829`); `duration >= threshold` is a bound int via column comparison, no string interpolation | closed |
| T-49-03 | Denial of Service | held long files silently analyzed locally and time out | mitigate | CLOUDROUTE-02: `≥threshold ∧ no compute` → `AWAITING_CLOUD`, never the fileserver queue (`src/phaze/routers/pipeline.py:307-322`). Recovery-path leak (CR-01) fixed — held `process_file` rows route via `select_active_agent(kind="compute")` only, else skip for the release cron (`src/phaze/tasks/reenqueue.py:312-332`) | closed |
| T-49-04 | Tampering | enqueue targeting the consumer-less default queue | mitigate | Queues obtained only via `task_router.queue_for(agent.id)` at every new site (`routers/pipeline.py:298-299`, `tasks/release_awaiting_cloud.py:78`, `tasks/reenqueue.py:330`); never an unnamed Queue (Phase-30 invariant) | closed |
| T-49-05 | Denial of Service | backfill double-click detonates the queue | mitigate | Explicit `ANALYSIS_FAILED ∧ duration≥threshold` filter (not a backlog sweep) + reset-to-DISCOVERED so a second click finds zero candidates + `process_file:<id>` deterministic-key dedup (`src/phaze/routers/pipeline.py:648-662`) | closed |
| T-49-06 | Injection | backfill candidate query | mitigate | ORM bound-param query `_backfill_candidates_stmt` with a bound int `threshold_sec` from `settings.cloud_route_threshold_sec` (`src/phaze/services/pipeline.py:819-829`); no operator free-text reaches the query | closed |
| T-49-07 | Denial of Service | release cron over-enqueues held files | mitigate | Bounded state scan (`get_files_by_state(AWAITING_CLOUD)`) + `process_file:<id>` deterministic-key dedup + state reset to DISCOVERED so a released file leaves the scanned set (`src/phaze/tasks/release_awaiting_cloud.py:68, 81-88`); no whole-backlog sweep | closed |
| T-49-08 | Elevation of Privilege | release routes a long file to a media-less compute agent only | accept | Intended — the load-bearing `kind="compute"` filter (`tasks/release_awaiting_cloud.py:73`, `tasks/reenqueue.py:323`) targets the correct agent class for long files (Phase 48); no unintended privilege change. See Accepted Risks Log. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-49-01 | T-49-08 | Held long files are routed to `kind="compute"` agents only. This is the intended design (Phase 48): compute agents are the correct, media-less target for long-file analysis. The kind filter is load-bearing for CLOUDROUTE-02, not a privilege escalation — the compute agent receives only the job payload it is built to process. | Robert (operator), via gsd-secure-phase audit | 2026-06-25 |

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-25 | 8 | 8 | 0 | gsd-security-auditor (opus), State-B from plan-time register |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-25

---

## Non-Threat-Register Follow-Ups (carried from 49-REVIEW.md)

Not part of the Phase-49 threat register, but tracked here because they touch the same routing-safety class:

- **WR-02** — `deepen_analysis` routes `process_file` kind-agnostically and ignores duration, so a long file could be analyzed on a fileserver (same class as T-49-03, but on a pre-existing endpoint outside Phase-49 scope). Recommend a follow-up to apply the duration gate. Non-blocking.
- **IN-01** — confirm a unique constraint on `file_metadata.file_id` so the duration join cannot double-count. Non-blocking (deterministic key prevents double-enqueue).
