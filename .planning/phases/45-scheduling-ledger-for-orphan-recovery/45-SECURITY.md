---
phase: 45
slug: scheduling-ledger-for-orphan-recovery
status: verified
threats_open: 0
asvs_level: 1
created: 2026-06-20
---

# Phase 45 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Reconstructed from the six PLAN `<threat_model>` blocks (State B) and verified
> against shipped code by gsd-security-auditor on 2026-06-20. **19/19 threats
> closed (17 mitigate + 2 accept), 0 open.** block_on: high — no high/blocker gaps.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| control-plane producer → `before_enqueue` hook → Postgres ledger | every keyed enqueue upserts a control-side ledger row at the single chokepoint (`apply_deterministic_key`) | `<function>:<natural_id>` key + trusted control-side JSON payload |
| SAQ worker → `after_process` hook → Postgres ledger | controller-stage terminal outcome clears its ledger row (gated on `TERMINAL_STATUSES`) | ledger key (delete-by-key) |
| `_shared` deterministic_key.py → phaze.database | agent worker imports `_shared`; the DB handle stays behind a runtime `getattr` + function-local lazy import, never a module import (L-05) | none in the agent process (Postgres-free) |
| agent worker → control API callback / `/failed` ack → Postgres ledger | the agent's terminal/success outcome becomes control-visible only here; the ledger clear runs control-side | PATH `file_id` (+ auth-token-bound agent identity) |
| agent-supplied request → ledger clear key | clear key derives from a trusted natural id (PATH `file_id`, or `body.file_id` on create_tracklist), agent identity from the auth token (AUTH-01) — never an attacker-chosen redirect field | `file_id` |
| SAQ-owned `saq_jobs` blob → control-side backfill → ledger | startup backfill reads the JSON job blob (read-only SAVEPOINT probe) to seed the ledger; a malformed blob skips that row | JSON job blob (function/kwargs/key) |
| Alembic migration 022 → `saq_jobs` | FORBIDDEN — backfill is runtime control-side code, never a migration data step (020 banner); 022 is additive DDL for `scheduling_ledger` only | none |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation (verified evidence) | Status |
|-----------|----------|-----------|-------------|--------------------------------|--------|
| T-45-01 | Tampering | `job.kwargs` persisted into `ledger.payload` | accept | Payload stored as JSONB, never executed (`models/scheduling_ledger.py:54`); upstream `extra="forbid"` validation (`schemas/agent_tasks.py:31,49,60,70`) | closed |
| T-45-02 | Elevation/Boundary | `deterministic_key.py` dragging phaze.database into agent import graph | mitigate | `getattr(job.queue,"ledger_sessionmaker",None)` + function-local lazy import (`_shared/deterministic_key.py:125-135,178-183`); TYPE_CHECKING-only sessionmaker (`queue_factory.py:40-45,100-101`); `tests/test_task_split.py` green | closed |
| T-45-03 | Denial of Service | ledger upsert/clear failure blocking enqueue/teardown | mitigate | Both hook sites try/except log-never-raise (`deterministic_key.py:137-140` WRITE, `:188-190` CLEAR) — cache_redis best-effort discipline | closed |
| T-45-04 | Tampering | migration 022 touching SAQ-owned `saq_jobs` | mitigate | Additive DDL only (`alembic/versions/022_add_scheduling_ledger.py:49-68`); sole `saq_jobs` token is the banner comment; `tests/test_migrations/test_022.py` green | closed |
| T-45-05 | Spoofing | agent redirecting the ledger clear to another file's key | mitigate | Clear key from PATH/`body.file_id` + fixed function name; agent identity from auth token (AUTH-01): `agent_analysis.py:196,225`, `agent_metadata.py:73,101`, `agent_fingerprint.py:54,85`, `agent_tracklists.py:168,203` | closed |
| T-45-06 | Repudiation/Correctness | clearing/acking a still-retryable job prematurely | accept | ack gated `job is not None and not job.retryable` (`scan.py:110,150`, `metadata_extraction.py:73`, `fingerprint.py:64`) | closed |
| T-45-07 | Denial of Service | ledger clear failing the callback that recorded the domain result | mitigate | clear rides the result-write txn (clear-before-commit): `agent_analysis.py:196-198`, `agent_metadata.py:73-74`, `agent_fingerprint.py:54-55`, `agent_tracklists.py:168-170`; ack endpoint sole DB op (`agent_tracklists.py:203-204`) | closed |
| T-45-08 | Denial of Service (queue detonation) | recovery sweeping never-scheduled work | mitigate | recovery reads ONLY ledger rows (`reenqueue.py:266,270`); a never-scheduled DISCOVERED file has no row (module banner `:20-39`); incident regression test | closed |
| T-45-09 | Denial of Service (queue doubling) | replaying a still-live item | mitigate | `get_live_job_keys` exclusion (`reenqueue.py:267,270`) + deterministic-key dedup backstop in `_replay_row` (`:217-222`) | closed |
| T-45-10 | Tampering | replayed payload bypassing schema validation | mitigate | replay via keyed producer with `key=row.key` (`reenqueue.py:218`); dequeue re-validates `extra="forbid"` (`scan.py:89`, `metadata_extraction.py:34`, `fingerprint.py:33`) → dead-letter | closed |
| T-45-11 | Elevation/Boundary | `reenqueue.py` leaking into the agent import graph | mitigate | control-only banner (`reenqueue.py:1-9`); `tests/test_task_split.py` green | closed |
| T-45-12 | Tampering | unparseable/malicious `saq_jobs` blob in backfill | mitigate | `_parse_job_blob` isolated, returns None on non-JSON/non-dict, row skipped (`reenqueue.py:327-338,377-379`) — one bad row never aborts backfill | closed |
| T-45-13 | Correctness | backfill overwriting a fresher hook-written row | mitigate | `insert_ledger_if_absent` ON CONFLICT DO NOTHING (`scheduling_ledger.py:91`), called by backfill (`reenqueue.py:392`) — hook row wins | closed |
| T-45-14 | Availability | backfill aborting controller boot | mitigate | startup try/except (`controller.py:142-148`) + internal SAVEPOINT degrade-to-empty on missing `saq_jobs` (`reenqueue.py:368-373`) | closed |
| T-45-15 | Tampering/Coupling | reading `saq_jobs` from an Alembic data migration | mitigate | backfill is a runtime startup reconcile (`reenqueue.py:341`); migration 022 has no data step; `saq_jobs` read only in runtime SQL (`reenqueue.py:324`) | closed |
| T-45-16 | Denial of Service (recovery loop) | `scan_live_set` no-match leaking `scan_live_set:<file_id>` on a controller-down ack | mitigate | no-match ack guarded: re-raise on retryable, swallow+log on terminal (`scan.py:106-114`) — CR-01 / plan 45-05 | closed |
| T-45-17 | Denial of Service (recovery loop) | a stage with no reliable clear re-enqueuing forever | mitigate | TOTAL per-stage predicate `_DOMAIN_COMPLETED_STAGES` + `is_domain_completed` (`reenqueue.py:107-120,182-205`); totality asserted vs `_KEY_BUILDERS` (`_ALL_KEYED_FUNCTIONS:305`) | closed |
| T-45-18 | Denial of Service (recovery loop) | terminally-failed metadata/fingerprint re-enqueuing (no callback cleared the row) | mitigate | new `POST /{file_id}/failed` clears the deterministic row (`agent_metadata.py:78-105`, `agent_fingerprint.py:59-89`); worker acks (`metadata_extraction.py:78`, `fingerprint.py:69`) — CR-02 / plan 45-06; recovery regression test | closed |
| T-45-SC | Tampering (supply chain) | npm/pip/cargo installs | mitigate | zero changes to `pyproject.toml`/`uv.lock` on the branch vs `main` (git diff empty) — no new packages to audit | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

### Post-plan hardening note

The residual double-failure-masking concern on the bare ack-in-`except` blocks
(code-review WR-01, affecting the realization of T-45-16 / T-45-06) was hardened
by quick task `260620-jvu`. The nested swallow+log guard is verified present in
all three required sites: `scan.py:155-158` (match-failure), `metadata_extraction.py:77-80`,
and `fingerprint.py:68-71` — each re-raises the original error after swallowing an
ack failure on the terminal attempt.

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-45-01 | T-45-01 | `job.kwargs` is persisted verbatim into `ledger.payload` as JSONB. The kwargs are already validated by the keyed producers' `extra="forbid"` schemas before enqueue (the ledger stores the same trusted control-side payload), and JSONB is stored, never executed. Residual risk is negligible for a single-user, private-network deployment. | Robert Wlodarczyk | 2026-06-20 |
| AR-45-02 | T-45-06 | A scan/metadata/fingerprint ack fires only on the retries-exhausted terminal attempt (`job is not None and not job.retryable`); a retryable attempt does not ack, so the ledger row deliberately survives until the genuine terminal attempt. The accepted residual is a one-recovery-cycle window where a row for a job between its last retry and terminal classification could be replayed — bounded and idempotent (deterministic-key dedup). | Robert Wlodarczyk | 2026-06-20 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-20 | 19 | 19 | 0 | gsd-security-auditor (opus) — State B, verify-mitigations mode |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-20
