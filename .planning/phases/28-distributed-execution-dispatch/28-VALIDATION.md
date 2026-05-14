---
phase: 28
slug: distributed-execution-dispatch
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-14
---

# Phase 28 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio (already configured) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_routers/test_agent_exec_batches.py tests/test_services/test_execution_dispatch_grouping.py -x` |
| **Full suite command** | `uv run pytest -x --cov=src --cov-report=term-missing` |
| **Estimated runtime** | ~90 seconds (full suite); ~5 seconds (quick) |

---

## Sampling Rate

- **After every task commit:** Run the quick command for the touched module
- **After every plan wave:** Run the full suite command
- **Before `/gsd-verify-work`:** Full suite must be green (≥85% coverage gate)
- **Max feedback latency:** 90 seconds

---

## Per-Task Verification Map

> Populated by the planner. Each Phase 28 task must point at one of these test entry points
> (or be a Wave 0 stub that establishes one).

| Test ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 28-V-01 | TBD | 1 | EXEC-01 | — | Group APPROVED proposals by `FileRecord.agent_id` | unit | `uv run pytest tests/test_services/test_execution_dispatch_grouping.py::test_groups_by_agent_id -x` | ❌ W0 | ⬜ pending |
| 28-V-02 | TBD | 1 | EXEC-01 | — | Skip revoked agents and surface a count | unit | `uv run pytest tests/test_services/test_execution_dispatch_grouping.py::test_revoked_agent_filtered_with_count -x` | ❌ W0 | ⬜ pending |
| 28-V-03 | TBD | 1 | EXEC-01 | — | Chunk per-agent groups at 500 | unit | `uv run pytest tests/test_services/test_execution_dispatch_grouping.py::test_1000_proposals_split_into_2_chunks -x` | ❌ W0 | ⬜ pending |
| 28-V-04 | TBD | 2 | EXEC-01 | — | `start_execution` enqueues one job per (agent, chunk) | integration | `uv run pytest tests/test_routers/test_execution_dispatch.py::test_multi_agent_dispatch_enqueues_per_chunk -x` | ❌ W0 | ⬜ pending |
| 28-V-05 | TBD | 2 | EXEC-01 | — | Dispatch INFO log + `dispatch_summary` field in Redis hash | integration | `uv run pytest tests/test_routers/test_execution_dispatch.py::test_dispatch_summary_in_redis_hash -x` | ❌ W0 | ⬜ pending |
| 28-V-06 | TBD | 2 | EXEC-02 | — | Agent posts one progress per successful proposal at terminal step | unit | `uv run pytest tests/test_tasks/test_execute_approved_batch_progress.py::test_success_emits_one_deleted_progress_post -x` | ❌ W0 | ⬜ pending |
| 28-V-07 | TBD | 2 | EXEC-02 | — | Agent posts one failed progress with `failed_at_step` on failure | unit | `uv run pytest tests/test_tasks/test_execute_approved_batch_progress.py::test_failure_emits_failed_progress_post -x` | ❌ W0 | ⬜ pending |
| 28-V-08 | TBD | 2 | EXEC-02 | — | `sub_batch_terminal` set on last item only | unit | `uv run pytest tests/test_tasks/test_execute_approved_batch_progress.py::test_sub_batch_terminal_set_on_last_item -x` | ❌ W0 | ⬜ pending |
| 28-V-09 | TBD | 2 | EXEC-02 | — | ExecutionLog write-ahead invariant preserved (POST→PATCH chain regression) | integration | `uv run pytest tests/test_tasks/test_execute_approved_batch.py -x` | ✅ | ⬜ pending |
| 28-V-10 | TBD | 1 | EXEC-03 | T-AUTH | New endpoint 401 without token | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_unauthenticated_401 -x` | ❌ W0 | ⬜ pending |
| 28-V-11 | TBD | 1 | EXEC-03 | T-TENANT | New endpoint 403 on `agent_id` mismatch (BEFORE state machine) | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_cross_tenant_agent_id_mismatch_403 -x` | ❌ W0 | ⬜ pending |
| 28-V-12 | TBD | 1 | EXEC-03 | — | New endpoint 404 on missing batch | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_unknown_batch_404 -x` | ❌ W0 | ⬜ pending |
| 28-V-13 | TBD | 1 | EXEC-03 | T-TENANT | New endpoint 403 on agent not in dispatch | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_non_participating_agent_403 -x` | ❌ W0 | ⬜ pending |
| 28-V-14 | TBD | 1 | EXEC-03 | — | Idempotent duplicate (`request_id`) → 200 + no HINCRBY | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_duplicate_request_id_does_not_re_increment -x` | ❌ W0 | ⬜ pending |
| 28-V-15 | TBD | 1 | EXEC-03 | — | Counter math across all 4 `terminal_step` × 3 `failed_at_step` branches | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py -k counter_math -x` | ❌ W0 | ⬜ pending |
| 28-V-16 | TBD | 1 | EXEC-03 | — | `sub_batch_terminal=true` triggers terminal status promotion | contract | `uv run pytest tests/test_routers/test_agent_exec_batches.py::test_sub_batch_terminal_promotes_status_complete -x` | ❌ W0 | ⬜ pending |
| 28-V-17 | TBD | 1 | EXEC-03 | — | Schema-layer: `failed_at_step` required iff `terminal_step="failed"` | unit | `uv run pytest tests/test_schemas/test_agent_exec_batches.py -x` | ❌ W0 | ⬜ pending |
| 28-V-18 | TBD | 2 | EXEC-04 | — | SSE emits aggregate counts | integration | `uv run pytest tests/test_routers/test_execution_dispatch.py::test_sse_emits_aggregate_progress -x` | ❌ W0 | ⬜ pending |
| 28-V-19 | TBD | 2 | EXEC-04 | — | SSE emits per-agent breakdown | integration | `uv run pytest tests/test_routers/test_execution_dispatch.py::test_sse_emits_agents_table -x` | ❌ W0 | ⬜ pending |
| 28-V-20 | TBD | 2 | EXEC-04 | — | SSE closes on `complete_with_errors` | integration | `uv run pytest tests/test_routers/test_execution_dispatch.py::test_sse_closes_on_complete_with_errors -x` | ❌ W0 | ⬜ pending |
| 28-V-21 | TBD | 2 | EXEC-04 | — | `agents_table.html` renders empty / single / multi / errors states | template | `uv run pytest tests/test_template_helpers/test_progress_partial.py -x` | ❌ W0 | ⬜ pending |
| 28-V-22 | TBD | 1 | TASK-04 | — | Config-validator rejects non-localhost `audfprint_url` | unit | `uv run pytest tests/test_services/test_fingerprint_locality.py::test_audfprint_url_rejects_external_host -x` | ❌ W0 | ⬜ pending |
| 28-V-23 | TBD | 1 | TASK-04 | — | Config-validator rejects non-localhost `panako_url` | unit | `uv run pytest tests/test_services/test_fingerprint_locality.py::test_panako_url_rejects_external_host -x` | ❌ W0 | ⬜ pending |
| 28-V-24 | TBD | 2 | TASK-04 | — | Cross-FS fingerprint banner partial renders and dismisses | template | `uv run pytest tests/test_template_helpers/test_cross_fs_fingerprint_notice.py -x` | ❌ W0 | ⬜ pending |
| 28-V-25 | TBD | 2 | EXEC-02 | — | `PhazeAgentClient.post_exec_batch_progress` — happy + 4xx-no-retry + 5xx-with-retry | unit | `uv run pytest tests/test_services/test_agent_client_exec_batch_progress.py -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

> The planner MUST land a Wave 0 plan that creates these test files (as stubs returning
> `pytest.skip("Wave 0 stub")` if implementation is not yet present) plus the shared
> fixtures. This is what unblocks Nyquist sampling for every later task.

- [ ] `tests/test_routers/test_agent_exec_batches.py` — contract tests for the new POST endpoint (auth, cross-tenant, idempotency, counter math, terminal promotion)
- [ ] `tests/test_routers/test_execution_dispatch.py` — integration tests for the rewritten `start_execution` and SSE stream
- [ ] `tests/test_services/test_execution_dispatch_grouping.py` — unit tests for grouping / revoked-filter / chunking helpers
- [ ] `tests/test_services/test_fingerprint_locality.py` — unit tests for the new config field validators
- [ ] `tests/test_services/test_agent_client_exec_batch_progress.py` — unit tests for `PhazeAgentClient.post_exec_batch_progress` (respx mock, tenacity retry semantics)
- [ ] `tests/test_schemas/test_agent_exec_batches.py` — unit tests for `ExecBatchProgressPayload` cross-field validator
- [ ] `tests/test_tasks/test_execute_approved_batch_progress.py` — unit tests for agent-side terminal-step progress POST + `sub_batch_terminal`
- [ ] `tests/test_template_helpers/test_progress_partial.py` — template render tests for `agents_table.html`
- [ ] `tests/test_template_helpers/test_cross_fs_fingerprint_notice.py` — template render + Alpine dismiss attribute presence
- [ ] `tests/conftest.py` — extend (if needed) with a `_make_smoke_app` style helper for `agent_exec_batches.router` mirroring Phase 27's `tests/test_routers/test_agent_scan_batches.py:34-44`

*Framework install: not needed — pytest, pytest-asyncio, respx, fakeredis-py, httpx are already in `pyproject.toml`.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| End-to-end multi-agent execution against two live file servers with a real Redis + two SAQ workers | EXEC-01..04 | Requires Docker Compose stack with two agent containers; not automated in the unit/integration tiers | Run `just compose-up`, approve a duplicate batch that spans both servers in the admin UI, watch the SSE stream show `dispatch_summary`, aggregate counters incrementing, and per-agent breakdown converging to `complete`. Confirm `ExecutionLog` rows match agent-side filesystem changes. |
| Banner dismissal persists across reload | TASK-04 | Alpine.js `localStorage` interaction is browser-only | Open `/duplicates`, dismiss the cross-FS-fingerprint banner, reload page, confirm it stays dismissed (or returns per the chosen persistence policy in CONTEXT.md D-14). |

---

## Validation Sign-Off

- [ ] All Phase 28 plans cite at least one of `28-V-01..28-V-25` in their `<automated>` or `<acceptance_criteria>` blocks
- [ ] Sampling continuity: no 3 consecutive tasks without an automated verify command
- [ ] Wave 0 plan creates the test files listed above and they are reachable from CI
- [ ] No `--watch` or interactive-mode flags in any task's automated commands
- [ ] Feedback latency < 90s on the full suite
- [ ] `nyquist_compliant: true` set in this file's frontmatter after Wave 0 plan lands

**Approval:** pending
