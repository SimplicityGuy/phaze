---
phase: 30
slug: fix-systemic-control-plane-saq-queue-misrouting-every-manual
status: verified
threats_open: 0
asvs_level: 1
created: 2026-06-10
---

# Phase 30 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| HTTP control-plane (API/UI) → SAQ/Redis | Operator-triggered enqueues cross into the task queue; a wrong queue name strands jobs (the v4.0.6 incident). | Task payloads (file paths, batch ids) |
| API → agents table | Active-agent selection trusts DB state (`revoked_at` / `last_seen_at`) to pick a dispatch target. | Agent identity / liveness |
| `agent_id` query param → per-agent queue lookup | `scan_status` receives `agent_id` to pick the poll queue. | Queue name component (constrained to `Agent.id`) |
| Future code change → CI | A regression reintroducing a default-queue producer must be caught before merge. | Source diffs |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-30-01 | DoS / Data-integrity | Enqueue to consumer-less `default` queue | mitigate | Unnamed default queue removed from `main.py:98` lifespan (only `name="controller"` remains); `resolve_queue_for_task` maps every task via `CONTROLLER_TASKS`/`AGENT_TASKS` frozensets (`enqueue_router.py:44-75`) and raises `ValueError` on unknown names (`enqueue_router.py:142-143`); 0 `*.state.queue` producer refs in routers/services; static AST guard `tests/test_no_default_queue_producers.py` fails CI on reintroduction. | closed |
| T-30-02 | Spoofing / Elevation | Dispatching to a revoked/dead agent | mitigate | `select_active_agent` filters `revoked_at IS NULL` AND `last_seen_at IS NOT NULL` (`enqueue_router.py:106-107`); `legacy-application-server` sentinel inserted with `revoked_at = NOW()` (`alembic/versions/012:57-61`) is excluded by the filter. | closed |
| T-30-03 | Tampering | Attacker-influenceable queue name / `scan_status` `agent_id` param | mitigate (accepted residual) | Queue name derives only from `Agent.id` (DB PK, CHECK `^[a-z0-9]+(-[a-z0-9]+)*$` at `models/agent.py:34-37`) + fixed task allowlist; built as `phaze-agent-{agent_id}` (`agent_task_router.py:87`). `scan_status` echoes `agent_id` from the selected agent; a garbage value resolves to a job-less queue → empty poll, never an enqueue or cross-tenant read. Residual: `agent_id` not slug-validated at the HTTP boundary (see Accepted Risks). | closed |
| T-30-04 | DoS | 0 active agents silently "succeeds" | mitigate | `select_active_agent` raises `NoActiveAgentError` (`enqueue_router.py:114-116`); pipeline handlers surface a visible empty-state (`pipeline.py` 6 handlers + `trigger_response.html`), tracklists `trigger_scan` renders a no-active-agent fragment (`tracklists.py:213-228`), `scan.py` returns HTTP 503 (`scan.py:64-68`). No silent 200. | closed |
| T-30-SC | Tampering (supply chain) | Package installs | accept | No new runtime dependencies introduced — `git diff ab06fe4..HEAD -- pyproject.toml` is empty; all five SUMMARY `tech-stack.added` lists are `[]`. Pure refactor of existing SAQ/SQLAlchemy code. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-30-SC | T-30-SC | No new packages added in Phase 30 (verified empty `pyproject.toml` diff across the phase). No supply-chain delta to review. | Robert (operator) | 2026-06-10 |
| AR-30-03 | T-30-03 (residual, REVIEW IN-01) | `scan_status` `agent_id` query param lacks `pattern=^[a-z0-9]+(-[a-z0-9]+)*$` enforcement at the HTTP boundary. Impact bounded to a silently-empty poll on a single-user private-LAN tool (no enqueue, no cross-agent data read). Suggested hardening: add `Query(..., pattern=..., max_length=128)` if this surface ever becomes multi-tenant or internet-exposed. | Robert (operator) | 2026-06-10 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-10 | 5 | 5 | 0 | gsd-security-auditor (opus), ASVS L1 |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-10
