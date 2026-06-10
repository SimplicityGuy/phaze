---
phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual
verified: 2026-06-09T00:00:00Z
status: passed
score: 3/3
overrides_applied: 0
re_verification: false
---

# Phase 30: Fix Systemic Control-Plane SAQ Queue Misrouting — Verification Report

**Phase Goal:** Every control-plane (UI/API) enqueue lands on a queue an actual worker consumes. Route the misrouted sites (pipeline.py, tracklists.py, scan.py/ingestion.py) through a shared helper: controller-bound tasks → `controller` queue, per-agent tasks → `AgentTaskRouter` with active-agent selection. The `default` queue ends with no producers. Regression tests assert correct queue targeting.

**Verified:** 2026-06-09
**Status:** PASSED
**Score:** 3/3 requirements verified
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|---------|
| 1 | Every control-plane enqueue targets a consumed queue; default queue has no producers (QR-01) | VERIFIED | `app.state.controller_queue = Queue.from_url(..., name="controller")` in main.py:98; grep confirms 0 `app.state.queue` references across pipeline.py, tracklists.py, scan.py; `resolve_queue_for_task` raises `ValueError` for unknown task names (confirmed in enqueue_router.py:142) |
| 2 | Per-agent routing uses active-agent selection; 0-agent surfaces a clear error (QR-02) | VERIFIED | `select_active_agent` filters `revoked_at.is_(None)` AND `last_seen_at.is_not(None)`, orders by `last_seen_at DESC LIMIT 1`; 6 per-agent pipeline handlers catch `NoActiveAgentError`; scan.py raises HTTP 503; tracklists.py renders no-agent fragment; scan_status polls per-agent queue via threaded `agent_id` |
| 3 | Regression + guard tests assert queue targeting and prevent recurrence (QR-03) | VERIFIED | `tests/test_no_default_queue_producers.py` exists and 6 tests pass; static AST guard scans routers/services; meta-test proves guard is not vacuously green; per-site test suites assert named-queue targeting (phaze-agent-nox / controller) |

**Score:** 3/3 truths verified

---

## Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/enqueue_router.py` | Routing helper with task map, select_active_agent, resolve_queue_for_task, NoActiveAgentError, RoutedQueue | VERIFIED | All five entities present; `CONTROLLER_TASKS` (5 tasks) and `AGENT_TASKS` (6 tasks) defined as frozensets; fully typed; substantive implementation |
| `src/phaze/main.py` | Lifespan wires `controller_queue`; unnamed default queue removed | VERIFIED | Lines 98-101: `_app.state.controller_queue = Queue.from_url(..., name="controller")` then `register_before_enqueue(apply_project_job_defaults)`; shutdown line 111: `await _app.state.controller_queue.disconnect()`; no unnamed Queue construction present |
| `src/phaze/services/agent_task_router.py` | Public `queue_for(agent_id)` accessor | VERIFIED | Line 68: `def queue_for(self, agent_id: str) -> Queue: return self._queue_for(agent_id)` |
| `src/phaze/routers/pipeline.py` | 8 handlers routed through `resolve_queue_for_task`; no `app.state.queue` | VERIFIED | `grep -c "app.state.queue"` = 0; `grep -c "resolve_queue_for_task"` = 8; 6 per-agent handlers have `except NoActiveAgentError` branches |
| `src/phaze/routers/tracklists.py` | 4 enqueue sites fixed; scan_status polls per-agent queue; no `app.state.queue` | VERIFIED | `grep -c "app.state.queue"` = 0; `resolve_queue_for_task` called 5 times; `task_router.queue_for(agent_id)` on scan_status line 264; `agent_id` threaded through |
| `src/phaze/routers/scan.py` | `trigger_scan` routes `extract_file_metadata` per-agent; 503 on no-agent | VERIFIED | `grep -c "app.state.queue"` = 0; `resolve_queue_for_task` called; `HTTPException(status_code=503, ...)` raised on `NoActiveAgentError` |
| `src/phaze/services/ingestion.py` | `run_scan` queue param documented as per-agent only; no logic change | VERIFIED | Lines 136-141: docstring updated to state queue must be a consumed per-agent queue; `queue.enqueue("extract_file_metadata", ...)` loop unchanged |
| `src/phaze/templates/tracklists/partials/scan_progress.html` | `agent_id` threaded into poll URL | VERIFIED | Line 28: `hx-get="/tracklists/scan/status?job_ids={{ job_ids }}&amp;agent_id={{ agent_id }}"` |
| `tests/test_no_default_queue_producers.py` | Static guard test; fails on default-queue reintroduction | VERIFIED | AST-based `_ProducerVisitor` scans routers+services; meta-test proves guard catches both offense classes; 6 tests pass |
| `tests/test_services/test_enqueue_router.py` | Unit tests for routing logic | VERIFIED | 11 tests pass covering all routing branches |
| `tests/test_routers/test_pipeline.py` + `test_pipeline_fingerprint.py` | Named-queue targeting assertions | VERIFIED | 32 tests pass; asserts `phaze-agent-nox` for process_file/extract/fingerprint, `controller` for generate_proposals, 0-agent branch for per-agent handlers |
| `tests/test_routers/test_tracklists.py` | Named-queue targeting assertions; agent_id poll round-trip | VERIFIED | 63 tests pass; asserts `controller` for scrape/search/match, `phaze-agent-nox` for scan_live_set, no-agent empty-state |
| `tests/test_routers/test_scan.py` | Named-queue assertion; 503 for no-agent | VERIFIED | 8 tests pass; asserts `phaze-agent-nox` / `extract_file_metadata`; `assert response.status_code == 503` on no-agent path |
| `tests/test_main_lifespan.py` | Asserts `controller_queue` present; `queue` absent | VERIFIED | Lines 106-108: `assert hasattr(app.state, "controller_queue")` and `assert not hasattr(app.state, "queue")` |
| `README.md` | "Task Queue Routing" subsection documenting routing model | VERIFIED | Lines 108-117: subsection covers controller-bound tasks, per-agent tasks, active-agent selection, fail-loud, guard test, and operational cleanup note; line 1 `<!-- generated-by: gsd-doc-writer -->` unchanged |

---

## Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `enqueue_router.py` | `phaze.models.agent.Agent` | `select_active_agent` SELECT WHERE `revoked_at.is_(None)` | WIRED | Lines 105-109: `Agent.revoked_at.is_(None)`, `Agent.last_seen_at.is_not(None)`, `order_by(Agent.last_seen_at.desc())` |
| `main.py` | `apply_project_job_defaults` | `controller_queue.register_before_enqueue` | WIRED | Lines 99-101: `register_before_enqueue(apply_project_job_defaults)` directly after queue construction |
| `pipeline.py` | `enqueue_router.py` | `resolve_queue_for_task` per trigger handler | WIRED | 8 call sites confirmed via grep count |
| `tracklists.py trigger_scan` | `tracklists.py scan_status` | `agent_id` threaded through `scan_progress.html` into poll URL | WIRED | `agent_id` in template context (line 242); poll URL includes `&agent_id={{ agent_id }}`; `scan_status` accepts `agent_id: str = Query(...)` and calls `task_router.queue_for(agent_id)` |
| `scan.py trigger_scan` | `ingestion.py run_scan` | `queue=routed.queue` argument is per-agent queue | WIRED | `run_scan(..., queue=routed.queue)` passes resolved queue through |

---

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| No default-queue producers in routers/services | `uv run pytest tests/test_no_default_queue_producers.py -q` | 6 passed in 0.25s | PASS |
| Enqueue routing logic correct | `uv run pytest tests/test_services/test_enqueue_router.py -q` | 11 passed in 1.05s | PASS |
| Lifespan wires controller_queue, removes default | `uv run pytest tests/test_main_lifespan.py -q` | 1 passed | PASS |
| Pipeline named-queue targeting | `uv run pytest tests/test_routers/test_pipeline.py tests/test_routers/test_pipeline_fingerprint.py -q` | 32 passed in 5.36s | PASS |
| Tracklists named-queue targeting + agent_id threading | `uv run pytest tests/test_routers/test_tracklists.py -q` | 63 passed in 10.70s | PASS |
| Scan named-queue targeting + 503 on no-agent | `uv run pytest tests/test_routers/test_scan.py -q` | 8 passed in 1.34s | PASS |

---

## Requirements Coverage

| Requirement | Description | Status | Evidence |
|-------------|-------------|--------|---------|
| QR-01 | Every control-plane enqueue targets a consumed queue; default queue has no producers | SATISFIED | Named `controller` queue in lifespan; 0 `app.state.queue` references in routers/services; `resolve_queue_for_task` raises on unknown; lifespan test asserts absence of `app.state.queue` |
| QR-02 | Per-agent routing uses active-agent selection; 0-agent surfaces a clear error | SATISFIED | `select_active_agent` with correct filters and ordering; 6 pipeline handlers return `{"enqueued": 0, "message": ...}` on `NoActiveAgentError`; scan.py returns HTTP 503; tracklists.py renders no-agent fragment; scan_status polls correct per-agent queue via threaded `agent_id` |
| QR-03 | Regression + guard tests assert queue targeting and prevent recurrence | SATISFIED | `tests/test_no_default_queue_producers.py` committed; AST guard non-vacuous (meta-test confirms); per-site suites (pipeline: 32, tracklists: 63, scan: 8) assert named-queue targeting; guard covers the direct `*.state.queue` access pattern |

**Note on REQUIREMENTS.md:** No separate `.planning/REQUIREMENTS.md` file exists. Requirements QR-01, QR-02, QR-03 are defined inline in `.planning/ROADMAP.md` line 115 under Phase 30. All three are accounted for.

---

## Anti-Patterns Found

| File | Pattern | Severity | Impact |
|------|---------|----------|--------|
| `tests/test_no_default_queue_producers.py:75-79` | Static guard has false-negative for two-step attribute access (`s = req.app.state; s.queue` — value is `ast.Name`, not `ast.Attribute`) | WARNING (WR-01 from code review) | Guard misses this uncommon variant. No such pattern exists anywhere in the current codebase. The meta-test only verifies the direct form. This is a guard-robustness gap, not a production routing gap; the goal of QR-03 (prevent silent recurrence of the common form) is still met. |
| `src/phaze/routers/tracklists.py:288-300` | `scan_status` never passes `no_active_agent` to template context; `scan_progress.html` uses `{% if no_active_agent is defined and no_active_agent %}` to handle the absent key | WARNING (WR-02 from code review) | Works today due to Jinja2's undefined-is-falsy; would silently break if project switches to `StrictUndefined`. Does not affect queue routing correctness. |
| `tests/test_services/test_enqueue_router.py`, `test_no_default_queue_producers.py`, `test_pipeline.py`, `test_pipeline_fingerprint.py`, `test_tracklists.py`, `test_scan.py` | `_FakeQueue`, `_FakeTaskRouter`, `_seed_active_agent`, `_stub_app_state` independently defined in 5-6 test files with minor implementation divergences | WARNING (WR-03 from code review) | Maintenance burden only; no routing correctness impact. |
| `src/phaze/routers/tracklists.py:265` | `scan_status` with `job_ids=""` evaluates `done = 0 >= 0 = True` and renders misleading "Scan complete" | WARNING (WR-04 from code review) | Hard to reach via normal HTMX flow; reachable by direct GET or programming error. Not a queue routing regression. |

### Guard Robustness Judgment (WR-01)

WR-01 does NOT materially undermine QR-03. Rationale:

1. **No such pattern exists today.** A comprehensive `grep -rn "\.state\b" src/phaze/` scan finds zero instances where `state` is aliased before `.queue` is accessed.
2. **The guard catches the incident pattern.** The v4.0.6 bug was the direct `request.app.state.queue` form; the guard correctly flags this (confirmed by the meta-test and by a planted regression in pipeline.py during development).
3. **The review suggests a fix.** Adding `elif isinstance(val, ast.Name) and val.id == "state":` to `visit_Attribute` would close the gap at low cost. This is a follow-up hardening item, not a blocker.

---

## Human Verification Required

None. All phase-30 behaviors are fully verifiable programmatically.

---

## Gaps Summary

No gaps. All three requirements are verified by codebase evidence. The four code-review warnings (WR-01 through WR-04) are maintainability observations that do not affect the correctness of the routing fix or the goal achievement. The known environmental constraint (local Redis down) does not affect the 127 phase-30-touched tests, which all pass without Redis.

---

_Verified: 2026-06-09_
_Verifier: Claude (gsd-verifier)_
