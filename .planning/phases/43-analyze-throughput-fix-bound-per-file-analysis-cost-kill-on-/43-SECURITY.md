---
phase: 43
slug: analyze-throughput-fix-bound-per-file-analysis-cost-kill-on
status: verified
threats_open: 0
asvs_level: 2
created: 2026-06-18
---

# Phase 43 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

Analyze-throughput fix: bound per-file essentia cost, kill-on-timeout via a
killable pebble ProcessPool, terminal-failure classification, and a coverage
contract. This audit VERIFIES the plan-time threat register against the
implemented code — every `mitigate` threat was confirmed by a code match in the
cited file, and the single `accept` threat's rationale was re-confirmed against
the config validator.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Agent worker → essentia child process | `run_in_process_pool` schedules CPU-bound analysis in a `pebble.ProcessPool` (`max_tasks=1`, per-task SIGKILL timeout) | local file path, models path, window caps |
| Agent worker → control plane (HTTP) | `PhazeAgentClient.put_analysis` / `report_analysis_failed` POST/PUT under bearer-token agent auth | analysis results, coverage counts, bounded failure detail |
| External producer → agent worker (SAQ/PostgresQueue) | `enqueue_process_file` emits a deterministic-key job with bounded outer timeout + retries | `ProcessFilePayload` (server-generated UUID, paths, optional caps) |
| Untrusted body → `/analysis/{file_id}/failed` | FastAPI route; identity from token, target from path | `AnalysisFailurePayload` (Literal reason + bounded error string) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-43-SC | Tampering | pebble supply chain | mitigate | Blocking-human verify checkpoint pre-`uv add` (43-01-SUMMARY "Checkpoint: Task 1", resume-signal "approved"); pinned `pebble>=5.2.0` (pyproject.toml:32), resolved `pebble==5.2.0` pure-Python (uv.lock:979-980) | closed |
| T-43-01 | DoS | runaway essentia child pinning a pool slot | mitigate | `ProcessPool(max_workers=..., max_tasks=1)` (pool.py:26); per-task `timeout` SIGKILLs child → `TimeoutError` (pool.py:44-51); `max_tasks=1` recycles worker per task | closed |
| T-43-02 | DoS | unbounded inner timeout (misconfig) | accept | `analysis_inner_timeout_sec` default 6600, `gt=0, lt=7200` validator (config.py:460-466) — inner < outer 7200 is enforced, not convention; `PHAZE_*` override documented; single-user homelab | closed |
| T-43-03 | DoS | unbounded per-file window count O(duration) | mitigate | `_stride_to_cap` even-stride downsampler bounds to ≤cap (analysis.py:391-415); caps applied pre-decode-loop in fine/coarse passes (analysis.py:453, 484); defaults 60/30 (config.py:467-478) | closed |
| T-43-04 | Tampering | sampled aggregates misreported as complete | mitigate | Five-field coverage emitted: `fine_windows_analyzed/total`, `coarse_windows_analyzed/total`, `sampled` (analysis.py:583-587) | closed |
| T-43-05 | Spoofing/Tampering | forged file_id/agent_id in failure report | mitigate | `Depends(get_authenticated_agent)` — agent from token, not body (agent_analysis.py:198); UPDATE scoped to PATH `file_id` only (agent_analysis.py:213); body `extra="forbid"` blocks smuggled ids (agent_analysis.py:92) | closed |
| T-43-06 | DoS | oversized error string in failure payload | mitigate | `AnalysisFailurePayload.error max_length=2000` (agent_analysis schema:95); `extra="forbid"` (schema:92); `reason` is `Literal["timeout","crashed","error"]` (schema:94) | closed |
| T-43-07 | Tampering | coverage fields leaking into features JSONB | mitigate | Five coverage names in `_ANALYSIS_COLUMN_FIELDS` (router agent_analysis.py:71-75); dedicated columns on `AnalysisResult` (models/analysis.py:28-32); overflow-funnel pops only non-column keys (router:140) | closed |
| T-43-08 | DoS | blind-retry of deterministically-too-long file | mitigate | `except TimeoutError`/`except ProcessExpired` → `report_analysis_failed` + normal return → SAQ COMPLETE, no retry (functions.py:169-178); transient retry once via `retries=2` (analysis_enqueue.py:99) | closed |
| T-43-09 | Tampering | unbounded error string forwarded from worker | mitigate | `str(exc)[:_ERROR_DETAIL_MAX]` (=2000) before send (functions.py:38, 187); control-side `max_length=2000` is authoritative bound (schema:95) | closed |
| T-43-10 | DoS | unbounded outer job timeout pinning a slot | mitigate | `process_file` enqueue `timeout=7200` (was 14400) (analysis_enqueue.py:94); inner pebble timeout 6600 kills first (config.py:460-466, threaded at functions.py:165) | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-43-01 | T-43-02 | A misconfigured inner timeout cannot disable the deterministic kill: `analysis_inner_timeout_sec` is validated `gt=0, lt=7200` (config.py:463), so it is structurally guaranteed to fall below the 7200s SAQ `process_file` net regardless of `PHAZE_ANALYSIS_INNER_TIMEOUT_SEC` override. The residual risk (an operator setting it to just under 7200, slightly delaying the kill) is bounded and acceptable for a single-user homelab deployment. | Robert Wlodarczyk | 2026-06-18 |

*Accepted risks do not resurface in future audit runs.*

---

## Unregistered Flags

None. SUMMARY `## Threat Flags` (43-03) explicitly records "None — no security surface
beyond the planned threat register". Plans 43-01/02/04 record no new attack surface
beyond the registered T-43-* threats. No new entry points appeared during
implementation that lack a threat mapping.

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-18 | 11 | 11 | 0 | gsd-security-auditor (Claude Opus 4.8) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-18
