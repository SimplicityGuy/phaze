---
phase: 69
slug: tiered-drain-scheduler
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-04
---

# Phase 69 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

Register authored at plan-time across all 5 plans (69-01…69-05). Auditor mode: **verify mitigations exist** (not scan for new threats). All 14 `mitigate` threats verified present in the implementation with file:line evidence; all 6 `accept` rationales confirmed. No unregistered `## Threat Flags` in any SUMMARY.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| operator config → ControlSettings | new bounded int knob crosses at startup | `cloud_spill_to_local_after_seconds` (int) |
| overlapping cron ticks → cloud_job count-and-claim | concurrent drains race for per-backend slots | in-flight slot counts |
| reconcile row-mutation vs drain claim | both mutate cloud_job in-flight counts; must be mutually exclusive per-row | cloud_job status/attempts |
| agent callback (bearer-authed) → control DB | compute/kueue agents POST failure callbacks that mutate FileState/cloud_job | file_id (path UUID), FileState |
| ledger recovery vs backend reconcile | both could claim ownership of a cloud file's re-drive | cloud_job ownership |
| drain tick → file.state | `stage_cloud_window` mutates `files.state` under the advisory lock; a wrong/late flip → cross-backend double-dispatch | FileState |
| local agent liveness → recovery ledger | a LOCAL_ANALYZING file whose local agent dies must remain a recoverable orphan | scheduling-ledger row |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-69-01-01 | Tampering (input-val) | `cloud_spill_to_local_after_seconds` | mitigate | Bounded pydantic Field(gt=0, lt=86400) — `config.py:608-614` | closed |
| T-69-01-02 | DoS (retry storm) | attempt-exclusion in select_backend | mitigate | Keys off bounded `cloud_submit_max_attempts` (gt=0,lt=20); exhausted → forced local — `backend_selection.py:97-99` | closed |
| T-69-01-03 | Info disclosure | selection logging | accept | Pure fn logs nothing sensitive; only `{id,kind,rank,cap}` downstream | closed |
| T-69-02-01 | DoS (over-dispatch) | per-backend cap enforcement | mitigate | Once-per-tick snapshot + local `remaining[]` decrement in single `pg_advisory_xact_lock(5_000_504)` txn, one commit — `release_awaiting_cloud.py:74,127,133-140,202,207` | closed |
| T-69-02-02 | DoS (probe storm) | is_available/in_flight_count snapshot | mitigate | Probed once per backend per tick; never re-probed in candidate loop — `release_awaiting_cloud.py:134-140` | closed |
| T-69-02-03 | Tampering (SQLi) | advisory lock + candidate claim | mitigate | `pg_advisory_xact_lock(:lock_key)` bound param; ORM throughout, no f-string SQL — `release_awaiting_cloud.py:127` | closed |
| T-69-02-04 | Info disclosure | drain logging | accept | Logs only `{id,cap}` + tally counts; no secrets | closed |
| T-69-03-01 | DoS (cap overshoot race) | reconcile per-row lock | mitigate | Per-row `pg_advisory_xact_lock(5_000_504)`; reconcile only decrements (never claims) — `backends.py:385`, `reconcile_cloud_jobs.py:170-202` | closed |
| T-69-03-02 | DoS (retry storm) | at-cap spill-back | mitigate | Keys off bounded `cloud_submit_max_attempts`; at cap forced local — `backends.py:359`, `reconcile_cloud_jobs.py:170` | closed |
| T-69-03-03 | Tampering (SQLi) | per-row lock + backend-scoped query | mitigate | Bound lock key + `CloudJob.backend_id == self.id` bound param; no f-string SQL — `backends.py:368,385` | closed |
| T-69-03-04 | Info disclosure | reconcile logging | accept | Logs `file_id`/`cloud_job_id`/attempt/cap only; no secrets | closed |
| T-69-04-01 | Elevation/DoS (double-recovery) | recover_orphaned_work orphan set | mitigate | Excludes files with in-flight cloud_job — single owner per kind (SCHED-05) — `reenqueue.py:204-219,343` | closed |
| T-69-04-02 | DoS (unbounded cloud retry) | callback at-cap spill | mitigate | Spill sets `cloud_job.attempts = cloud_submit_max_attempts` → select_backend forces local — `agent_push.py:188,203`, `agent_s3.py:175,185` | closed |
| T-69-04-03 | Tampering (SQLi) | callback UPDATEs + in-flight query | mitigate | ORM `update().values()` + bound params; `.status.in_([...])` bound list; no f-string SQL — `agent_push.py:201-203`, `agent_s3.py:183-185`, `reenqueue.py:219` | closed |
| T-69-04-04 | Spoofing | agent callbacks | accept | `Depends(get_authenticated_agent)` bearer-token dep (AUTH-01, untouched); `file_id` is path UUID — `agent_push.py:69,157`, `agent_s3.py:65,146` | closed |
| T-69-04-05 | Info disclosure | callback logging | accept | Logs file_id/agent_id/attempt/cap only; no secrets | closed |
| T-69-05-01 | Tampering/State | LocalBackend.dispatch state flip | mitigate | Flip in same session as enqueue, before it, no intra-method commit; atomic under drain's single post-loop commit — `backends.py:217-224` | closed |
| T-69-05-02 | Denial (leaked slot) | cross-backend double-dispatch | mitigate | `FileState.LOCAL_ANALYZING` excludes file from `get_cloud_staging_candidates` (CR-01 fix) — `pipeline.py:1255`, `file.py:63` | closed |
| T-69-05-03 | Denial (lost work) | local agent death mid-analysis | accept | LOCAL_ANALYZING in neither done-predicate + no cloud_job → ledger re-drives lost process_file — `reenqueue.py:179,189` | closed |
| T-69-05-SC | Supply-chain | npm/pip/cargo installs | mitigate | No new dependencies; `pyproject.toml`/`uv.lock` untouched since Phase 65 (git log) | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-69-01 | T-69-01-03, T-69-02-04, T-69-03-04, T-69-04-05 | Info-disclosure via logs — call sites emit only `{id,kind,rank,cap}`/`file_id`/`cloud_job_id`/`agent_id`/`attempt`/`cap`; no SecretStr, `*_file` path, or token logged (Phase-68 logging discipline) | Robert Wlodarczyk | 2026-07-04 |
| AR-69-02 | T-69-04-04 | Agent-callback spoofing — mitigated by the pre-existing bearer-token agent auth dependency (AUTH-01); `file_id` is a path UUID, no free-text; Phase 69 did not touch auth | Robert Wlodarczyk | 2026-07-04 |
| AR-69-03 | T-69-05-03 | Local agent death mid-analysis — existing scheduling-ledger recovery covers it (LOCAL_ANALYZING is not a done-state and carries no cloud_job, so a lost process_file re-drives); no new control required | Robert Wlodarczyk | 2026-07-04 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-04 | 20 | 20 | 0 | gsd-security-auditor (opus) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-04
