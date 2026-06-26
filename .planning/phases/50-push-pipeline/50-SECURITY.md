---
phase: 50
slug: push-pipeline
status: verified
threats_open: 0
asvs_level: 1
created: 2026-06-26
---

# Phase 50 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Push pipeline: rsync-over-Tailscale transfer from the file-server agent to the compute
> agent (OCI A1) for analysis, with sha256 integrity verification and token-authed callbacks.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Operator → ControlSettings/AgentSettings | Env / `<VAR>_FILE` config at startup | SSH key + known_hosts (SecretStr), bounded numeric knobs |
| File-server agent → Compute agent | rsync-over-SSH (Tailscale) push of a media file | Media file bytes to `<scratch_dir>/<file_id>.<ext>` |
| Agent (Postgres-free) → Control plane | Token-authed internal-API callbacks | `file_id` (URL path), push outcome; no body identity |
| Control plane → Compute queue | `process_file` enqueue after push | ORM-pinned `expected_sha256`, control-derived `scratch_path` |
| Compute scratch dir → analysis | Ephemeral pushed copy read by `process_file` | sha256-verified bytes; deleted in `finally` |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-50-00-01 | Tampering | test scaffolding | mitigate | Wave-0 stubs use `pytest.skip` (never assert-pass), replaced by real assertions in 50-03/04/06. `tests/test_push_pipeline.py` (33 asserts, 0 skips), `test_process_file_scratch.py` (34), `test_staging_cron.py` (37), `test_routing_seam.py` (22) | closed |
| T-50-secret-leak | Info Disclosure | config.py / tasks/push.py | mitigate | `config.py:452` SECRET_FILE_FIELDS ∋ push_ssh_key/push_known_hosts; `:623,:628` SecretStr; `push.py:144-147` materialized to 0600 temp, `:181-185` shredded in finally; stderr bounded `:45,:178` | closed |
| T-50-config-oob | Tampering | config.py | mitigate | `config.py:403-409` cloud_max_in_flight gt=0,lt=100; `:413-418` push_max_attempts gt=0,lt=20; `:608-613` push_timeout_sec gt=0,lt=86400; `:615-620` push_connect_timeout_sec gt=0,lt=3600 — out-of-range fails at startup | closed |
| T-50-scratch-skew | Tampering | config.py / routers/agent_push.py | mitigate | `config.py:420-429` compute_scratch_dir must-match invariant documented; `agent_push.py:121` scratch_path = `{compute_scratch_dir}/{file_id}.{file_type}` (UUID-derived, never untrusted filename) | closed |
| T-50-payload-inject | Tampering | schemas/agent_tasks.py, schemas/agent_push.py | mitigate | `agent_tasks.py:31,:62` + `agent_push.py:32,:46,:59` `extra="forbid"`; file_id `uuid.UUID`; scratch_path derived control-side (`agent_push.py:121`) | closed |
| T-50-double-enqueue | Tampering | deterministic_key.py / release_awaiting_cloud.py / agent_push.py | mitigate | `deterministic_key.py:85` `push_file: str(k["file_id"])`; `release_awaiting_cloud.py:71-78,:101-103` push_file_job_key + keyed enqueue; `agent_push.py:216` `key=ledger_key` — SAQ incomplete-set dedup | closed |
| T-50-orphan-leak | DoS | tasks/reenqueue.py | mitigate | `reenqueue.py:109-114` push_file in `_DOMAIN_COMPLETED_STAGES`; `:181-187` done-set {PUSHED,ANALYZED,ANALYSIS_FAILED}; `:235-236` predicate re-drives still-PUSHING | closed |
| T-50-misroute | EoP | tasks/reenqueue.py / services/enqueue_router.py | mitigate | `reenqueue.py:339-340` push_file rows partitioned; `:365` `select_active_agent(kind="fileserver")`; `:366-368` NoActiveAgentError → WARNING skip, never compute enqueue/raise | closed |
| T-50-injection | Tampering/EoP | tasks/push.py | mitigate | `push.py:154` `create_subprocess_exec(*argv)` (no shell); `:100-109` fixed-list argv with `--` terminator; remote_dest UUID `:99`; nosec justified inline `:152-153` | closed |
| T-50-spoof | Spoofing/Tampering | tasks/push.py | mitigate | `push.py:95-97` `StrictHostKeyChecking=yes` + `UserKnownHostsFile=<pinned>` + `BatchMode=yes` | closed |
| T-50-scratch-dos | DoS | tasks/agent_worker.py / functions.py | mitigate | `agent_worker.py:95-103` compute-only `_maybe_sweep_scratch` (gated kind=="compute" + cloud_scratch_dir), `:158` startup sweep; `functions.py:278-285` per-job `finally` unlink | closed |
| T-50-hang | DoS | tasks/push.py | mitigate | `push.py:103` rsync `--timeout`; `:97` ssh `ConnectTimeout`; `:167` `asyncio.wait_for` outer bound; `:96` `BatchMode=yes` blocks auth-prompt hang | closed |
| T-50-no-fallback | Tampering | tasks/push.py | mitigate | `push.py:159-164` `FileNotFoundError` (rsync/ssh absent) → terminal RuntimeError naming binary; never local analysis | closed |
| T-50-corrupt | Tampering | tasks/functions.py | mitigate | `functions.py:185-199` off-loop `to_thread(compute_sha256, ...)` before analysis; mismatch → unlink + `report_push_mismatch` + no-analyze (`status="push_mismatch"`) | closed |
| T-50-loop | DoS | routers/agent_push.py | mitigate | `agent_push.py:172-186` `next_attempt > push_max_attempts` → `FileState.ANALYSIS_FAILED` + clear ledger | closed |
| T-50-spoofed-callback | Spoofing | routers/agent_push.py | mitigate | `agent_push.py:66,:145` `Depends(get_authenticated_agent)` token dep; file_id from URL path; identity never from body (AUTH-01) | closed |
| T-50-integrity-pin | Tampering | routers/agent_push.py | mitigate | `agent_push.py:127` `expected_sha256=file.sha256_hash` read control-side from ORM `FileRecord.sha256_hash`; agent never supplies it | closed |
| T-50-bypass | Tampering | routers/pipeline.py / services/enqueue_router.py | mitigate | `pipeline.py` `grep -c cloud_files == 0`; `:315` `_route_discovered_by_duration` always sets `AWAITING_CLOUD`; `:291-292` `cloud` always 0 — no direct compute enqueue path | closed |
| T-50-cron-raise | DoS | tasks/release_awaiting_cloud.py | mitigate | `release_awaiting_cloud.py:137-141` (compute) + `:156-160` (fileserver) `try/except NoActiveAgentError` → clean `{"staged": 0}` no-op | closed |
| T-50-poll-500 | DoS | services/pipeline.py | mitigate | `pipeline.py:819-847` `get_pushing_count`/`get_pushed_count` via `_safe_count`; `:272-289` rollback + return 0 — poll never 500s | closed |
| T-50-SC | Tampering | dependencies | accept | No new packages this phase (`tech-stack.added: []` across all 8 plans); supply-chain gate N/A | closed |
| T-50-stale-count | Info Disclosure | UI count cards | accept | Per-card counts observational (`pipeline.py:826,:842` documented); load-bearing window enforced by cron via `get_cloud_window_count` from committed FileState | closed |
| T-50-runaway-cron | DoS | tasks/controller.py / release_awaiting_cloud.py | accept | `stage_cloud_window` bounded top-up gated on online compute agent; Phase-42 no-general-auto-advance guard comment retained (50-06) | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-50-01 | T-50-SC | No new dependencies introduced in Phase 50; the litellm/supply-chain gate has no surface to act on this phase. Re-evaluate when packages are added. | Phase 50 plan author | 2026-06-26 |
| AR-50-02 | T-50-stale-count | The "Staged (pushing)"/"Analyzing (cloud)" dashboard cards are observational and degrade-safe (`_safe_count` → 0). The ≤N backpressure that actually bounds the window is enforced by the cron reading committed FileState via `get_cloud_window_count`, which is intentionally NOT degrade-safe. A stale card cannot over-stage. | Phase 50 plan author | 2026-06-26 |
| AR-50-03 | T-50-runaway-cron | `stage_cloud_window` is a single bounded `*/5` top-up gated on an online compute agent and capped at `cloud_max_in_flight`; both agent-absent paths are clean no-ops. The Phase-42 no-general-auto-advance guard remains the structural backstop. | Phase 50 plan author | 2026-06-26 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-26 | 23 | 23 | 0 | gsd-security-auditor |

Unregistered flags: none. All `## Threat Flags` sections in 50-00..50-07 SUMMARY.md report "None"; the new attack surface (rsync transport, push callbacks, scratch dir) maps entirely to the plan-time register above.

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-26
