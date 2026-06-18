---
phase: 43
slug: analyze-throughput-fix
status: planned
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-17
---

# Phase 43 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Detailed sampling/measurement design lives in `43-RESEARCH.md` (## Validation Architecture).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x + pytest-asyncio (`asyncio_mode="auto"`) + respx |
| **Config file** | pyproject.toml (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60–120 seconds |

---

## Sampling Rate

- **After every task commit:** Run the targeted test module for the touched area (`uv run pytest tests/test_<area>.py -q`)
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing` (≥85% gate)
- **Before `/gsd:verify-work`:** Full suite green + `uv run mypy .` + `uv run ruff check .` clean + `pre-commit run --all-files`
- **Max feedback latency:** ~120 seconds

---

## Per-Task Verification Map

> Populated by the planner — each task maps to an automated check. essentia is heavy and x86-only, so
> analysis is validated at MOCKABLE boundaries (window-list math, pool kill behavior, payload/coverage
> shapes, state transitions, timeout-terminal classification) — NO real essentia in CI. The one
> real-process test (43-01-T3) uses a trivial picklable sleeper fn through pebble, not essentia.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 43-01-T1 | 01 | 1 | ANALYZE-KILL-ON-TIMEOUT | T-43-SC | pebble legitimacy verified before install (blocking-human) | manual gate | (checkpoint — pypi.org/project/Pebble) | n/a | ⬜ pending |
| 43-01-T2 | 01 | 1 | ANALYZE-CONFIG-KNOBS | T-43-02 | inner timeout < SAQ net; PHAZE_* aliases | unit | `uv run pytest tests/test_tasks/test_pool.py -q` | ✅ exists | ⬜ pending |
| 43-01-T3 | 01 | 1 | ANALYZE-KILL-ON-TIMEOUT, ANALYZE-INNER-TIMEOUT | T-43-01 | runaway child SIGKILLed + slot reclaimed on inner timeout | unit (real pebble, dummy fn) | `uv run pytest tests/test_tasks/test_pool.py -k timeout -q` | ✅ exists | ⬜ pending |
| 43-02-T1 | 02 | 1 | ANALYZE-BOUND-COST | T-43-03 | cost bounded; even stride across whole file; idx preserved | unit (pure fn) | `uv run pytest tests/test_services/test_analysis.py -k stride -q` | ✅ exists | ⬜ pending |
| 43-02-T2 | 02 | 1 | ANALYZE-BOUND-COST, ANALYZE-COVERAGE-EMIT | T-43-04 | coverage + sampled emitted; aggregates valid under sampling | unit (mocked essentia) | `uv run pytest tests/test_services/test_analysis.py -q` | ✅ exists | ⬜ pending |
| 43-03-T1 | 03 | 2 | ANALYZE-COVERAGE-PERSIST, ANALYZE-STATE-MACHINE | — | nullable columns; enum is code-only (no enum migration) | migration round-trip | `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` | ✅ creates 021 | ⬜ pending |
| 43-03-T2 | 03 | 2 | ANALYZE-STATE-MACHINE, ANALYZE-COVERAGE-PERSIST | T-43-07 | coverage to columns not JSONB; non-empty PUT → ANALYZED | integration (DB) | `uv run pytest tests/test_schemas/test_agent_analysis.py tests/test_routers/test_agent_analysis.py -q` | ✅ exists | ⬜ pending |
| 43-03-T3 | 03 | 2 | ANALYZE-FAILED-ENDPOINT | T-43-05, T-43-06 | agent auth; file_id path-only; bounded error; extra=forbid | integration (DB + respx) | `uv run pytest tests/test_routers/test_agent_analysis.py tests/test_services/test_agent_client_endpoints.py -q` | ✅ exists | ⬜ pending |
| 43-04-T1 | 04 | 3 | ANALYZE-RETRY-POLICY | T-43-10 | timeout=7200; retries=2 survives defaults hook | unit | `uv run pytest tests/test_services/test_analysis_enqueue.py -q` | ✅ exists | ⬜ pending |
| 43-04-T2 | 04 | 3 | ANALYZE-TIMEOUT-TERMINAL, ANALYZE-WORKER-WIRING | T-43-08, T-43-09 | TimeoutError/ProcessExpired terminal (no retry); non-retryable reported; coverage forwarded | unit (AsyncMock) | `uv run pytest tests/test_tasks/test_functions.py -q` | ✅ exists | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

All Wave-0 test infrastructure already exists in the repo (no scaffolding needed):

- [x] Cap+even-stride downsampler assertions → `tests/test_services/test_analysis.py` (exists; add `-k stride` cases) — deterministic, no essentia.
- [x] Killable-pool proof a runaway child is SIGKILLed on inner timeout → `tests/test_tasks/test_pool.py` (exists; **rewrite** off `ProcessPoolExecutor._max_workers` to a real-pebble timeout test with a trivial sleeper fn).
- [x] Fixtures for the control-API coverage + `analysis/{file_id}/failed` endpoints → `tests/test_routers/test_agent_analysis.py` + `tests/test_services/test_agent_client_endpoints.py` (both exist; DB + respx fixtures established).

*No new test files or frameworks are required; only `pebble` is added to deps.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real long-set bounded runtime on the homelab | ANALYZE-BOUND-COST | Requires real essentia + a multi-hour file (not in CI) | After redeploy to nox, confirm a known >4h set now completes in minutes, `analysis.sampled=true`, and the analyze counter advances |
| Kill-on-timeout reclaims a real pool slot | ANALYZE-KILL-ON-TIMEOUT | Requires a real runaway essentia child | After redeploy, confirm worker CPU/slot frees when a job hits the inner timeout (no 483% CPU / 28.6 GiB pin) |
| Backlog re-enqueue reaches the 11356 in-flight jobs | ANALYZE-RETRY-POLICY | Old jobs carry baked timeout=14400 (RESEARCH Runtime State Inventory) | Operator purge + re-enqueue (or homelab redeploy prompt) — out of phase code; flag at deploy |

---

## Validation Sign-Off

- [x] All tasks have automated verify or a documented manual/checkpoint reason (43-01-T1 is the blocking-human supply-chain gate)
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covered by existing infrastructure (noted above)
- [x] No watch-mode flags
- [x] Feedback latency < 120s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** planner-complete (pending execution)
