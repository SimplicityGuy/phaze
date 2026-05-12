---
phase: 26
slug: task-code-reorg-http-backed-agent-worker
nyquist_audit: true
audited: 2026-05-12
verdict: NEEDS_GAPS_FILLED
gaps_found: 5
gaps_critical: 2
---

# Phase 26 — Nyquist Coverage Audit

Adversarial audit of the Phase 26 test surface. All claims checked against
disk reality, not VALIDATION.md assertions. Two test files marked ✅ in the
verification map do not exist on disk. Three behavioral behaviors have no
test that can fail.

---

## Audit Method

For each task row in 26-VALIDATION.md: (1) verify the referenced test file
exists, (2) read the test, (3) compare assertions against the implementation
surface, (4) flag any behavior the implementation exhibits that no assertion
would catch if it regressed.

---

## What Passes (density adequate)

| Coverage Area | Test File | Count | Quality |
|---------------|-----------|-------|---------|
| 4xx/5xx retry semantics | `test_services/test_agent_client.py` | 9 | Strong — call_count verified per scenario |
| Bearer header injection | same | 1 | Verified via `route.calls.last.request.headers` |
| PATCH /proposals state-machine | `test_routers/test_agent_proposals.py` | 11 | Complete — all allowed transitions + 4 illegal + cross-tenant 403 + auth surface |
| PUT /analysis upsert + CR-01 | `test_routers/test_agent_analysis.py` | 8 | Complete — idempotent replay, partial PUT, empty-body noop |
| GET /whoami auth surface | `test_routers/test_agent_identity.py` | 4 | Complete — happy, 401, 403, revocation |
| execute_approved_batch | `test_tasks/test_execute_approved_batch.py` | 6 | Strong — happy/partial/path-escape/sha256-mismatch/empty-scan-roots |
| process_file (HTTP rewrite) | `test_tasks/test_functions.py` | 6 | Strong — analyzed/skipped/pool-fail/http-fail/extra-kwargs |
| extract_file_metadata | `test_tasks/test_metadata_extraction.py` | 5 | Strong |
| fingerprint_file | `test_tasks/test_fingerprint.py` | 5 | Strong |
| scan_live_set | `test_tasks/test_scan.py` | 6 | Strong — stable request_id + W5 artist=None confirmed |
| D-25 import boundary | `test_task_split.py` | 1 | Strong — subprocess, catches real import contamination |
| D-13 startup banner + token preview | `test_tasks/test_agent_startup_banner.py` | 1 | Secret bytes verified absent from caplog |
| OPS-01 controller banner | `test_tasks/test_controller_startup_banner.py` | 1 | role + queue + ctx["queue"] stashed |
| Payload schemas (agent_tasks) | `test_schemas/test_agent_tasks.py` | 22 | Complete — extra=forbid, 500-proposal cap |
| Proposal schema + moved-path validator | `test_schemas/test_agent_proposals.py` | 9 | Complete |
| Tracklist schema + 2000-track cap | `test_schemas/test_agent_tracklists.py` | covers cap | Complete |
| AgentTaskRouter queue isolation | `test_services/test_agent_task_router.py` | 4 (integration) | Correct — skip is pre-existing D-3 |
| POST /tracklists idempotency | `test_routers/test_agent_tracklists.py` | 7 (integration) | Correct — skip is pre-existing D-3 |

---

## Gaps Found

### GAP-1 — CRITICAL: `test_config_role_split.py` does not exist

**Validation map claim:** 01-T1 row marks `tests/test_config_role_split.py` ✅ "created same task."
**Reality:** File does not exist on disk. The automated command `uv run pytest tests/test_config_role_split.py -k role_split -x` would immediately error with "no such file."

**Behaviors with zero test coverage:**

| Behavior | Implementation Location | Risk If Regressed |
|----------|------------------------|-------------------|
| `get_settings()` returns `AgentSettings` when `PHAZE_ROLE=agent` | `config.py:154-156` | Agent workers silently get `ControlSettings`; Postgres credentials injected into agent env |
| `AgentSettings` raises `ValueError` when `PHAZE_AGENT_API_URL` missing | `config.py:137-138` | Agent starts without knowing the app server; every HTTP call fails at runtime instead of boot |
| `AgentSettings` raises `ValueError` when `PHAZE_AGENT_TOKEN` missing | `config.py:139-140` | Agent starts unauthenticated |
| `AgentSettings` raises `ValueError` when `scan_roots` empty | `config.py:141-142` | Path-traversal guard in `execute_approved_batch` throws `RuntimeError` rather than failing fast at boot |
| Comma-split `PHAZE_AGENT_SCAN_ROOTS` validator produces list from `"a,b"` | `config.py:121-133` | Multi-root deployments silently get single-string scan_root `"a,b"` that never matches any path |

**Recommended test:** `tests/test_config_role_split.py` — 5 parametrized unit tests, no DB or Redis required.

```
uv run pytest tests/test_config_role_split.py -x -q --no-cov
```

---

### GAP-2 — HIGH: `test_agent_client_endpoints.py` does not exist

**Validation map claim:** 02-T2 row marks `tests/test_services/test_agent_client_endpoints.py` ✅ "created same task."
**Reality:** File does not exist on disk.

**Behaviors uncovered for Phase-26-new methods** (existing `test_agent_client.py` only tests `put_analysis` + `whoami`):

| Method | Endpoint | Gap |
|--------|----------|-----|
| `create_tracklist` | `POST /api/internal/agent/tracklists` | No respx test — URL construction, payload serialization, response model parsing |
| `patch_proposal_state` | `PATCH /api/internal/agent/proposals/{id}/state` | No respx test — `exclude_unset=True` serialization; response model parsing |
| `post_execution_log` | `POST /api/internal/agent/execution-log` | No respx test |
| `patch_execution_log` | `PATCH /api/internal/agent/execution-log/{id}` | No respx test |
| `heartbeat` | `POST /api/internal/agent/heartbeat` | No respx test (204 No Content return path) |

Note: the retry/error-class invariants are thoroughly tested against `put_analysis` in `test_agent_client.py`. The gap here is per-method URL correctness and response model parsing for the 5 Phase-26-new methods.

**Recommended test:** `tests/test_services/test_agent_client_endpoints.py` — 5 respx happy-path tests (one per method), verifying URL construction and response model type.

```
uv run pytest tests/test_services/test_agent_client_endpoints.py -x -q --no-cov
```

---

### GAP-3 — MEDIUM: Token never logged in `_request` WARNING path (D-13 partial)

**Claim:** D-13 — bearer token must never appear in logs.

**Current coverage:** `test_agent_startup_banner.py` asserts the secret portion is absent from `caplog` during `startup()`. This covers the banner path.

**Gap:** `PhazeAgentClient._request()` emits a `logger.warning(...)` on every 4xx/5xx/network failure (lines 164, 176 of `agent_client.py`). No test captures `caplog` from `_request` and asserts the token string is absent. The implementation looks correct (the warning only logs `method`, `path`, `status_code`, `type(e).__name__`), but the invariant is asserted nowhere in the test suite for the HTTP-client warning path.

**Regression scenario:** A future developer adds `e.request.headers` to the warning message for debuggability, inadvertently logging the full `Authorization: Bearer <token>` header. No test would catch this.

**Recommended test:** One caplog test added to `test_agent_client.py` — call `put_analysis` with a mocked 500 response, capture `caplog` at WARNING level, assert `_TOKEN` does not appear in any log record message.

---

### GAP-4 — MEDIUM: `original_path` escape from `scan_roots` not tested

**Current coverage:** `test_execute_approved_batch_path_escape_rejected` tests that a `proposed_path` of `/etc/passwd` is rejected. The test docstring explicitly says "proposed_path escapes scan_root."

**Gap:** `_resolve_and_check_containment` is called for both `item.original_path` AND `item.proposed_path` (execution.py lines 112-113). A malicious or corrupted payload with `original_path="/etc/shadow"` and a valid `proposed_path` would be caught by the same guard — but this path through the code has no test. If the guard were refactored to only check `proposed_path`, the existing test would still pass.

**Recommended test:** One additional case in `test_execute_approved_batch.py` where `original_path="/etc/shadow"` and `proposed_path` is inside the scan root. Assert `error_count == 1` and the original untouched.

---

### GAP-5 — LOW: Queue/token mismatch `RuntimeError` in `agent_worker.startup()` not tested

**Implementation:** `agent_worker.py` lines 133-142 raise `RuntimeError` when `PHAZE_AGENT_QUEUE` env suffix does not match `identity.agent_id` returned by `/whoami`.

**Current coverage:** Zero. The startup banner test mocks `whoami` to return `agent_id="test-id"` and the env is `PHAZE_AGENT_QUEUE=phaze-agent-test-id` — so the guard is bypassed correctly, but a mismatch scenario is never exercised.

**Risk:** This is the primary anti-misconfiguration guard per "Pitfall 1" (one agent consuming another agent's queue). If the check were accidentally removed or the string-comparison logic changed, no test would catch it.

**Recommended test:** Add a second test in `test_tasks/test_agent_startup_banner.py` (or a new file) where `monkeypatch` sets `PHAZE_AGENT_QUEUE=phaze-agent-wrong-id` while `whoami` returns `agent_id="correct-id"`. Assert `startup()` raises `RuntimeError` matching `"queue/token mismatch"`.

---

## Summary

| # | Gap | Priority | Behaviors Uncovered | File Missing? |
|---|-----|----------|---------------------|---------------|
| 1 | `test_config_role_split.py` absent | CRITICAL | 5 fail-fast behaviors in config role dispatch | Yes |
| 2 | `test_agent_client_endpoints.py` absent | HIGH | 5 Phase-26 client methods unverified end-to-end | Yes |
| 3 | D-13 token-never-logged in `_request` warning path | MEDIUM | Token leak in warning logs undetected | No |
| 4 | `original_path` path escape not tested | MEDIUM | Guard regression on original path goes undetected | No |
| 5 | Queue/token mismatch `RuntimeError` untested | LOW | Primary anti-misconfiguration guard unverified | No |

---

## Coverage Verdict

**NEEDS GAPS FILLED**

The bulk of Phase 26's critical surface is well-covered: the D-25 import-boundary test is strong and structural; the 11-test proposal state-machine suite is exhaustive; all 5 file-bound task bodies have behavioral unit tests with mocked HTTP clients; the 4 new router contracts each have dedicated test files. The overall test count for Phase 26 surface is approximately 100+ passing tests.

The two missing files (GAP-1, GAP-2) are the actionable blockers: both are listed ✅ in the verification map but do not exist on disk. The automated CI command for each would fail immediately. GAP-1 in particular is critical — the config role-split is the foundation of the entire DIST-03 requirement, and its fail-fast validators are completely untested.

GAP-3 through GAP-5 are medium/low hardening gaps; they do not block the phase ship but represent unverified invariants that future refactoring could silently break.

---

## Recommended Next Action

1. Create `tests/test_config_role_split.py` covering `get_settings()` dispatch + all three `AgentSettings` fail-fast validators + comma-split (GAP-1, CRITICAL).
2. Create `tests/test_services/test_agent_client_endpoints.py` with one respx happy-path test per new Phase-26 client method (GAP-2, HIGH).
3. Add one `caplog` test to `test_agent_client.py` asserting token absent from WARNING logs (GAP-3, MEDIUM).
4. Add one `original_path` escape case to `test_execute_approved_batch.py` (GAP-4, MEDIUM).
5. Add queue/token mismatch test to `test_agent_startup_banner.py` or a new file (GAP-5, LOW).

_Audited: 2026-05-12 by Nyquist adversarial test coverage audit_
