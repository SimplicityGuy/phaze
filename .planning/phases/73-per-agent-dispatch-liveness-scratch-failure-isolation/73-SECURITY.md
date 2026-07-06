---
phase: 73
slug: per-agent-dispatch-liveness-scratch-failure-isolation
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-05
---

# Phase 73 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Register authored at plan time across all 4 PLAN files (`register_authored_at_plan_time: true`),
> so this run VERIFIED each mitigation exists in the implementation (not retroactive-STRIDE).
> Verification completed by the orchestrator after the `gsd-security-auditor` agent was cut off
> by a provider weekly-limit mid-run; every closure below is backed by a direct file:line read.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| operator → config | `backends.toml` `push_host`/`scratch_dir`/`ssh_user` are operator-provided and later reach the ssh remote spec + rsync operand | non-secret host/path/user tokens |
| control → SAQ payload | `PushFilePayload.dest_*` serialize into the `push_file` job payload JSONB consumed by the fileserver | non-secret destination fields |
| SAQ payload → rsync argv | `payload.dest_*` interpolated into the ssh remote spec + rsync operand on the fileserver | non-secret destination fields |
| fileserver secret custody | `push_ssh_key` + `push_known_hosts` `SecretStr` materialized to 0600 temp files, shredded in `finally` | SSH private key + known_hosts (secret) |
| compute agent → `/mismatch` | token-authed callback; a stale/wrong/duplicate compute agent could report for another agent's file | file_id (path), agent identity (token) |
| fileserver agent → `/pushed` | token-authed callback from the pusher; routing must use the recorded backend, not "the active compute agent" | file_id (path), agent identity (token) |
| drain tick → N backends | one flaky compute backend must not abort the whole tick (DoS on healthy lanes) | per-backend availability + in-flight snapshot |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-73-01 | Tampering | `PushFilePayload.dest_host` / `dest_scratch_dir` / `dest_ssh_user` reaching the ssh remote spec + rsync operand | mitigate | Pydantic field_validators (`schemas/agent_tasks.py:97-123`): `dest_scratch_dir` absolute-path **+ shell-metachar reject** (WR-01 hardening), `dest_host` + `dest_ssh_user` reject whitespace/shell-metachars via `_DEST_HOST_FORBIDDEN`; `extra="forbid"` preserved (`agent_tasks.py:63`) | closed |
| T-73-02 | Information disclosure | SSH key/known_hosts material leaking into the payload | mitigate | `dest_*` are `str \| None` only (`agent_tasks.py:76-78`) — no `SecretStr` fields added; secrets stay agent-side | closed |
| T-73-03 | Tampering | A compute entry with no `push_host` silently building a `None/...` destination | mitigate | id-tagged `_require_dispatch_fields` fail-fast at `ComputeBackend` construction (`config_backends.py:106-115`) | closed |
| T-73-04 | Tampering | `payload.dest_host`/`dest_scratch_dir` smuggling an rsync flag or shell metachar | mitigate | `--` argv terminator before positionals (`tasks/push.py:114`) + list-argv via `create_subprocess_exec` (no shell, `push.py:186`) + T-73-01 validators; remote name is the server UUID `<scratch_dir>/<file_id>.<file_type>` | closed |
| T-73-05 | Information disclosure | SSH key / known_hosts leaking via the retired-config change | mitigate | Secrets materialized to 0600 temp files, child reaped before shred, unlinked in `finally` regardless of outcome (`push.py:178-223`); never logged; `dest_*` remain non-secret | closed |
| T-73-06 | Denial of service | Silent scratch-dir skew if `cloud_scratch_dir` were deleted (compute janitor `AttributeError`) | mitigate | `AgentSettings.cloud_scratch_dir` field kept (Landmine 2 / D-04); only the fileserver's remote-target read was retired | closed |
| T-73-07 | Spoofing / Elevation | `/mismatch` reporter mis-attributing another agent's file (MCOMP-06 security core) | mitigate | `resolve_compute_backend(cloud_job.backend_id)` → `agent.id != backend.agent_ref` → **403 before any mutation** (`routers/agent_push.py:214-224`); never re-stamps `backend_id` from the token. **Hardened by CR-01 fix**: the over-cap spill now carries a `state == PUSHING` CAS guard (`agent_push.py:243-257`, commit `49903419`) so a duplicate/late/unattributed (backend-None, gate-skipped) reporter cannot clobber an already-advanced file to `AWAITING_CLOUD` | closed |
| T-73-08 | Tampering / DoS | Wrong-agent `process_file` routing finds no scratch copy (retry storm) | mitigate | `/pushed` routes `process_file` to the RECORDED `backend.agent_ref` with its `scratch_dir`, not `select_active_agent` (`agent_push.py:99-160`); WR-02 `state == PUSHING` CAS on the PUSHED flip already present | closed |
| T-73-09 | Tampering | `/mismatch` re-drive pushing to a null/empty destination | mitigate | Re-driven payload stamps `dest_*` from the recorded backend; a backend-None (unattributed) file HOLDS rather than enqueuing a destination-less push (`agent_push.py:261-311`). Defense-in-depth: `_build_rsync_argv` fails fast on a `None` `dest_host`/`dest_scratch_dir` (WR-02 fix, `push.py:106-113`) | closed |
| T-73-10 | Information disclosure | `backend_id` / agent identity in logs | mitigate | All callback logs project only `{file_id, reporter/agent_id, expected}` id fields — no `SecretStr`/token (`agent_push.py` log calls) | closed |
| T-73-11 | Denial of service | A flaky compute backend's probe failure cascading to abort the drain tick | mitigate | Per-backend snapshot try/except (`tasks/release_awaiting_cloud.py:151-157`): a raising `is_available`/`in_flight_count` is caught → backend treated as unavailable (0 slots), logged `backend_id`-only (no creds), `continue`; proven by the MCOMP-05 regression | closed |
| T-73-12 | Tampering | `recover_orphaned_work` single-active re-drive mis-routing a recovered held file on N-compute deploys | accept | Documented known limitation (PROV-01 backlog); NOT widened this phase — widening risks the 44.5k over-enqueue incident class. See Accepted Risks AR-73-01 | closed |
| T-73-13 | Denial of service | `push_attempt` ledger read-modify-write has no row lock → concurrent `/mismatch` for one file can lose an increment (a few extra re-drives beyond the cap) — WR-04 | accept | Contained: the deterministic `push_file:<file_id>` job key collapses concurrent re-drives to a single rsync (`agent_push.py:337`, `push.py` dedup); `push_max_attempts` bounded (gt=0, lt=20, `config.py:560`) so no unbounded storm; the D-07 reporter gate limits who can report for an attributed file. Worst case is self-limiting. `SELECT … FOR UPDATE` hardening is backlog. See Accepted Risks AR-73-02 | closed |
| T-73-SC | Tampering (supply chain) | npm/pip/cargo installs | accept | Zero new dependencies this phase; `pyproject.toml` untouched (verified: no diff to dependency sections). See Accepted Risks AR-73-03 | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-73-01 | T-73-12 | `recover_orphaned_work` still re-drives via the single-active-compute path; widening it to N-compute per-agent recovery is deferred to the PROV-01 backlog because a broadened sweep risks the Phase-45 44.5k over-enqueue incident class. On N-compute deploys a recovered held file may re-route via the first eligible compute agent rather than its original — bounded, non-destructive, and recovery-only. | Robert (operator) | 2026-07-05 |
| AR-73-02 | T-73-13 | The `push_attempt` counter RMW lacks a `SELECT … FOR UPDATE` row lock. A lost increment under concurrent `/mismatch` for the same file causes at most a handful of extra re-drives before the cap trips — contained by the deterministic `push_file:<id>` job-key dedup (concurrent re-drives collapse to one rsync), the bounded `push_max_attempts` (< 20), and the D-07 reporter gate (only the dispatched agent passes for an attributed file). Row-lock hardening tracked as backlog; not required to contain the threat. | Robert (operator) | 2026-07-05 |
| AR-73-03 | T-73-SC | Phase 73 adds zero new dependencies (pure application-code refactor over the existing backend/push pipeline); `pyproject.toml`/`uv.lock` dependency sections untouched, so no Package Legitimacy Audit is required (per RESEARCH). | Robert (operator) | 2026-07-05 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-05 | 14 | 14 | 0 | orchestrator (verify-mitigations mode; auditor agent cut off by provider weekly-limit, closures re-derived from direct file:line reads) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter
