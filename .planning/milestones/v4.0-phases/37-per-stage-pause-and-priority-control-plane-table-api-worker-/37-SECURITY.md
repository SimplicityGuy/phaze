---
phase: 37
slug: per-stage-pause-and-priority-control-plane-table-api-worker
status: verified
threats_open: 0
asvs_level: 1
created: 2026-06-13
---

# Phase 37 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| operator request → control endpoint | `stage` path param + `delta` body are untrusted input crossing into DB writes | stage name (str), priority delta (int) |
| operator `stage` value → raw SQL | `stage` selects the `key LIKE` prefix in the service helpers — injection surface if interpolated | stage name → SQL filter prefix |
| admin UPDATE ↔ worker dequeue | concurrent contention on the same `saq_jobs` rows (the no-double-pickup guarantee) | job rows (priority/scheduled/status) |
| enqueue hook → agent process | `apply_stage_control` runs inside the agent worker's queue construction; a DB-layer import here breaks the agent import boundary | control-row read (paused/priority) |
| migration author → DDL | migration 020 seeds rows; any interpolated value is a tampering surface | seed stage names |
| client → API | endpoints sit behind reverse-proxy internal-realm auth (no app-layer auth) — same posture as existing `/pipeline/*` and `/saq` | HTTP requests on the private LAN |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-37-01 | Tampering (SQL injection) | migration 020 seed · `services/stage_control.py` UPDATEs · router `stage` path param | mitigate | Bound params only (`sa.text(...)` seed @ `020:61-66`; static module-constant SQL with `:p`/`:s`/`:pfx` @ `services/stage_control.py:46-50`); `stage` validated against `STAGE_TO_FUNCTION` allowlist BEFORE any `key LIKE` prefix is built (`_key_prefix` @ `services:59-61`; router `_validate_stage`→422 @ `routers/pipeline_stages.py:54-57`, called first in all 3 endpoints); hook read uses psycopg3 `%(stage)s` bound param. | closed |
| T-37-02 | Denial of Service (pipeline stall / un-dequeueable priority) | `priority` column · `delta` body · best-effort hook read | mitigate | DB CHECK `priority BETWEEN 0 AND 100` (`models/pipeline_stage_control.py:35` + `020:56`) keeps every priority inside SAQ's 0–32767 dequeue window; endpoint clamps `row.priority+delta` to `[0,100]` (`routers/pipeline_stages.py:50-51,92`); hook is best-effort — a control-read failure logs + enqueues unpaused/default and never blocks the enqueue (`tasks/_shared/stage_control.py:101-107,134-138`). | closed |
| T-37-03 | Tampering (mid-dequeue mutation / double-run / clobbered retry backoff) | concurrent admin UPDATE vs worker dequeue on `saq_jobs`; resume vs retry backoff | mitigate | Every admin UPDATE carries `status='queued'` (`services/stage_control.py:46,47,50`); no app-level lock added — relies on SAQ's dequeue `FOR UPDATE SKIP LOCKED`; resume is sentinel-guarded `AND scheduled = :s` so retry-backoff rows (`now+delay`) are never clobbered; priority-delta read-modify-write serialized via `FOR UPDATE` row lock (`routers/pipeline_stages.py:71,91`, WR-02 fix). Proven by `tests/integration/test_stage_concurrency.py` + `test_stage_resume.py`. | closed |
| T-37-04 | Elevation / agent import-boundary break | `apply_stage_control` hook | mitigate | Hook reads the control table via `job.queue.pool` (psycopg3) only — no `phaze.database` / `sqlalchemy.ext.asyncio` import in `tasks/_shared/stage_control.py`; `saq.Job` under `TYPE_CHECKING`. Enforced by subprocess test `test_stage_control_stays_postgres_free` (`tests/test_task_split.py`, banned-module set under `PHAZE_ROLE=agent`). | closed |
| T-37-04 (infra) | Spoofing / Access control | endpoint exposure | accept | No app-layer auth added; operator-only via the reverse proxy's internal-realm auth on the private LAN, consistent with existing `/pipeline/*` and the `/saq` UI. Documented in `README.md`. | closed (accepted) |
| T-37-SC | Tampering (supply chain) | dependencies | accept | No new packages added this phase (uses pre-existing `alembic`, `saq[postgres]`, `sqlalchemy`); `pyproject.toml` last touched by Phase 36 (`eb570c8`). litellm remains version-capped per the March 2026 incident. | closed (accepted) |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-37-01 | T-37-04 (infra) | Single-user, private-LAN deployment; all `/pipeline/*` + `/saq` sit behind reverse-proxy internal-realm auth. App-layer auth is out of scope and consistent with the existing posture (LOCKED). | Robert (operator) | 2026-06-13 |
| AR-37-02 | T-37-SC | No new dependencies introduced this phase; nothing to vet. | Robert (operator) | 2026-06-13 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-13 | 6 | 6 | 0 | gsd-security-auditor (opus, ASVS L1, block_on: high) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-13
