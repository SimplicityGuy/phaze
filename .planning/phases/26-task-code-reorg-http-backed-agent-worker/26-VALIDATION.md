---
phase: 26
slug: task-code-reorg-http-backed-agent-worker
status: approved
nyquist_compliant: true
wave_0_complete: true
created: 2026-05-12
revised: 2026-05-12
---

# Phase 26 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio for async; respx for httpx mocking) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest -q --no-cov` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~45-60 seconds (with real Postgres + Redis via docker-compose) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/<changed>` for the directly touched module
- **After every plan wave:** Run `uv run pytest -q --no-cov` (full unit suite, no slow integration unless tagged)
- **Before `/gsd-verify-work`:** Full suite must be green; coverage ≥ 85%
- **Max feedback latency:** 60 seconds

---

## Per-Task Verification Map

> Populated 2026-05-12 (revision iteration 2 per checker B4). Each row corresponds to a task across the 13 plans. REQ-ID column references the plan's `requirements` frontmatter. Threat Ref links to the plan's `<threat_model>` STRIDE register.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|
| 01-T1 | 26-01 | 1 | TASK-01, DIST-03 | — | Role-aware settings factory; validator fail-fast | unit | `uv run pytest tests/test_config_role_split.py -k role_split -x` | ✅ created same task |
| 01-T2 | 26-01 | 1 | TASK-01 | — | `tenacity>=8.4` + `respx>=0.21` deps land; mypy override block | static | `uv run mypy src/phaze/services/agent_client.py` (post Plan 02) + `uv run python -c "import tenacity, respx"` | ✅ created same task |
| 01-T3 | 26-01 | 1 | TASK-01, D-28 | — | `ProposalStatus.{EXECUTED, FAILED}` + `FileState.{MOVED, UNCHANGED}` exist | unit | `uv run python -c "from phaze.models.proposal import ProposalStatus; from phaze.models.file import FileState; assert ProposalStatus.EXECUTED and ProposalStatus.FAILED and FileState.MOVED and FileState.UNCHANGED"` | ✅ created same task |
| 02-T1 | 26-02 | 1 | TASK-02, DIST-03 | T-26-02-S, T-26-02-I | PhazeAgentClient: bearer header injection, 4xx no-retry, 5xx-retry, token never logged | unit (respx) | `uv run pytest tests/test_services/test_agent_client.py -x -q --no-cov` | ✅ created same task |
| 02-T2 | 26-02 | 1 | TASK-02 | — | Per-endpoint method tests | unit (respx) | `uv run pytest tests/test_services/test_agent_client_endpoints.py -x -q --no-cov` | ✅ created same task |
| 03-T1 | 26-03 | 2 | TASK-02, TASK-03 | T-26-03-T, T-26-03-D | 4 schemas with extra='forbid'; validator for moved-without-path; tracks max_length=2000 | unit | `uv run pytest tests/test_schemas/test_agent_schemas.py -x` + smoke validator script | ✅ created same task |
| 03-T2 | 26-03 | 2 | TASK-02 | T-26-03-T | 5 task payloads + ExecuteBatchProposalItem; no current_path; ExecuteApprovedBatchPayload.proposals capped at 500 | unit | `uv run python -c "from phaze.schemas.agent_tasks import *; import uuid"` + roundtrip smoke | ✅ created same task |
| 04-T1 | 26-04 | 3 | TASK-02 | T-26-04-T | AgentTaskRouter: per-agent queue cache; enqueue calls correct queue | unit | `uv run pytest tests/test_services/test_agent_task_router.py -x` | ✅ created same task |
| 05-T1 | 26-05 | 3 | TASK-02, TASK-03, OPS-01 | T-26-05-S, T-26-05-I | GET /whoami returns AgentIdentity for authenticated bearer; 401/403 paths | router unit | `uv run pytest tests/test_routers/test_agent_whoami.py -x` | ✅ created same task |
| 06-T1 | 26-06 | 3 | TASK-03 | T-26-06-S | RED contract tests for PUT /analysis happy + replay + partial + empty + 422 + 401/403 | router integration | `uv run pytest tests/test_routers/test_agent_analysis.py --collect-only` (RED via ModuleNotFoundError) | ✅ created same task |
| 06-T2 | 26-06 | 3 | TASK-03 | T-26-06-T | PUT /analysis idempotent upsert via pg_insert + on_conflict; CR-01 partial-PUT | router integration | `uv run pytest tests/test_routers/test_agent_analysis.py -x -q --no-cov` | ✅ created same task |
| 06-T3 | 26-06 | 3 | TASK-03 (W6) | — | `_summarize_dict_to_string` deterministic alphabetical tiebreak + 50-char cap | unit (parametrized) | `uv run pytest tests/test_routers/test_summarize_dict_to_string.py -x -q --no-cov` | ✅ created same task (W6) |
| 07-T1 | 26-07 | 3 | TASK-03 | — | RED integration tests for POST /tracklists (real Redis + Postgres) + W7 too-many-tracks | router integration | `uv run pytest tests/test_routers/test_agent_tracklists.py --collect-only` | ✅ created same task |
| 07-T2 | 26-07 | 3 | TASK-03 | T-26-07-T, T-26-07-DoS | Redis idempotency (fast-path + concurrent-writer + owner-path); tracks max_length=2000 enforced | router integration | `uv run pytest tests/test_routers/test_agent_tracklists.py -x -q --no-cov -m integration` (7 tests) | ✅ created same task |
| 08-T1 | 26-08 | 3 | TASK-03 | T-26-08-T, T-26-08-S2 | RED contract tests: joint-update, idempotent no-op, illegal transitions, cross-agent 403 (W1) | router integration | `uv run pytest tests/test_routers/test_agent_proposals.py --collect-only` | ✅ created same task |
| 08-T2 | 26-08 | 3 | TASK-03 | T-26-08-S2, T-26-08-T | PATCH /proposals/{id}/state state-machine + single-commit joint update + W1 cross-tenant guard | router integration | `uv run pytest tests/test_routers/test_agent_proposals.py -x -q --no-cov` (11 tests) | ✅ created same task |
| 09-T1 | 26-09 | 4 | TASK-01, OPS-01 | T-26-09-S, T-26-09-E | Controller settings module: fileless functions only; no agent-side imports; ctx["queue"] stashed (W4) | unit | `uv run python -c "import phaze.tasks.controller; ..."` + grep no file-bound imports | ✅ created same task |
| 09-T2 | 26-09 | 4 | OPS-01 (W2) | — | Controller startup banner: role=control + queue=controller + ctx["queue"] invariant | unit (caplog) | `uv run pytest tests/test_tasks/test_controller_startup_banner.py -x -q --no-cov` | ✅ created same task (W2) |
| 10-T1 | 26-10 | 5 | TASK-01, DIST-03 | T-26-10-E | Subprocess import-boundary test — phaze.database NEVER in agent_worker import chain | subprocess | `uv run pytest tests/test_task_split.py -x -q --no-cov` | ✅ created same task |
| 10-T2 | 26-10 | 5 | TASK-01, DIST-03 | T-26-10-S, T-26-10-I | agent_worker 6-step startup; B1 fingerprint_orchestrator; queue mismatch RuntimeError; token preview | static + integration | `uv run mypy src/phaze/tasks/agent_worker.py` + grep invariants + import-boundary test | ✅ created same task |
| 10-T3 | 26-10 | 5 | OPS-01 (W2) | T-26-10-I | Agent worker startup banner: role=agent + agent_id + token preview = first-12 + "..." (D-13) | unit (caplog) | `uv run pytest tests/test_tasks/test_agent_startup_banner.py -x -q --no-cov` | ✅ created same task (W2) |
| 11-T1 | 26-11 | 4 | TASK-01 | T-26-11-T | process_file + extract_file_metadata: no DB imports; ctx["api_client"] only | static + unit | grep no DB imports + `uv run mypy src/phaze/tasks/{functions,metadata_extraction}.py` | ✅ created same task |
| 11-T2 | 26-11 | 4 | TASK-01 | T-26-11-T | fingerprint_file + scan_live_set: no DB imports; uuid5 idempotent request_id; ctx["fingerprint_orchestrator"] | static + unit | grep no DB imports + `uv run mypy` | ✅ created same task |
| 11-T3 | 26-11 | 4 | TASK-01 (B2 Option A) | T-26-11-S1, T-26-11-T2 | execute_approved_batch FULL impl: per-proposal copy+verify+delete; path-traversal guard; failure isolation | unit (mock) | `uv run pytest tests/test_tasks/test_execute_approved_batch.py -x -q --no-cov` (4 tests: happy, partial, escape, sha-mismatch) | ✅ created same task (B2) |
| 11-T4 | 26-11 | 4 | TASK-01, DIST-03 | T-26-10-E | Import-boundary test reaches GREEN after Plans 10 + 11 merge | subprocess | `uv run pytest tests/test_task_split.py -x -q --no-cov` | ❌ depends on 10-T1 |
| 12-T1 | 26-12 | 5 | TASK-03 | — | main.py wires 4 new routers + app.state.redis lifespan | integration | `uv run pytest tests/test_main_app.py -x -q --no-cov` + smoke `curl /openapi.json` includes new endpoints | ✅ created same task |
| 13-T1 | 26-13 | 6 | OPS-01, D-04 | T-26-09-E | docker-compose.yml command points to controller / agent_worker; legacy worker.py + session.py DELETED | static | `! test -f src/phaze/tasks/worker.py` + `! test -f src/phaze/tasks/session.py` + `grep "phaze.tasks.controller.settings" docker-compose.yml` | ✅ created same task |

**Status legend:** ⬜ pending · ✅ green · ❌ red · ⚠️ flaky

---

## Wave 0 Requirements

> Validation infrastructure that MUST exist before any wave-1+ task can run its `<automated>` block.

- [x] `tests/conftest.py` — extended in Plan 02 (agent_client_mock fixture); reuses authenticated_client from Phase 25
- [x] `tests/test_task_split.py` — scaffolded in Plan 10 Task 1 (subprocess import-boundary)
- [x] Model enum extensions — Plan 01 Task 3 (`ProposalStatus.{EXECUTED, FAILED}` + `FileState.{MOVED, UNCHANGED}`)
- [x] `tenacity>=8.4` + `respx>=0.21` deps — Plan 01 Task 1 (`uv add tenacity` + `uv add --dev respx`)
- [x] mypy `[[tool.mypy.overrides]]` for `phaze.services.agent_client` + `phaze.services.agent_task_router` — Plan 01 Task 1
- [x] `AgentSettings.scan_roots` — Plan 01 Task 1 (used by Plan 11 Task 3 for path-traversal containment check)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| docker-compose worker boots cleanly with `PHAZE_ROLE=control` | OPS-01 | Compose role-selection logic is an env+CMD interaction, not unit-testable | `docker compose up -d worker && docker compose logs worker \| grep -i "controller.settings"` — must see SAQ startup with controller module path |
| Same image with `PHAZE_ROLE=agent` + `PHAZE_AGENT_QUEUE` boots as agent | OPS-01, DIST-03 | Requires a running application server + Redis at known endpoints; integration smoke only | Manually export env, run `uv run saq phaze.tasks.agent_worker.settings`, verify `/whoami` succeeds and queue name matches |
| Agent worker fails fast (RuntimeError) when `agent_api_url` missing | D-14 | Lifecycle failure — requires container exit-code inspection | Drop `AGENT_API_URL` from env, attempt to start agent worker, confirm non-zero exit |
| scan_live_set tracklist UI artist/title gap (W5 — chose Option (b)) | TASK-03 | UI-visual regression not catchable by unit tests | After Phase 26 deployment, navigate to v3.0 tracklist review UI for a fingerprint-sourced tracklist; expect empty artist/title fields. Document as known limitation in 26-13-SUMMARY + ROADMAP.md Phase 26 entry. Resolution scheduled for Phase 27/28 controller-side enrichment. |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies (filled 2026-05-12)
- [x] Sampling continuity: no 3 consecutive tasks without automated verify (verified — every Wave 1+ task has a command)
- [x] Wave 0 covers all MISSING references (enum extensions, fixtures, dependencies, mypy overrides, scan_roots)
- [x] No watch-mode flags
- [x] Feedback latency < 60s (each automated command < 30s; full suite < 60s)
- [x] Import-boundary test (D-25) lands in Wave 5 via Plan 10 Task 1 so every subsequent commit validates the boundary
- [x] State-machine transition coverage: every allowed transition + every rejected transition has a contract test (Plan 08 — 11 tests including W1 cross-agent 403)
- [x] `nyquist_compliant: true` set in frontmatter (per-task verify map populated)

**Approval:** approved 2026-05-12
