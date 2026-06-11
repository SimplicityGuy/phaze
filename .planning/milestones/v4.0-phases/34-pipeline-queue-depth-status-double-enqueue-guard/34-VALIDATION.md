---
phase: 34
slug: pipeline-queue-depth-status-double-enqueue-guard
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-10
---

# Phase 34 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) |
| **Config file** | pyproject.toml ([tool.pytest.ini_options]) |
| **Quick run command** | `uv run pytest tests/test_services/test_pipeline.py tests/test_routers/test_pipeline.py -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60–120 seconds (full suite) |

---

## Sampling Rate

- **After every task commit:** Run the quick run command
- **After every plan wave:** Run the full suite command
- **Before `/gsd:verify-work`:** Full suite green + coverage ≥85%
- **Max feedback latency:** ~120 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 34-00-01 | 00 | 0 | harness | — | `FakeQueue.count(kind)` returns int per kind | unit | `uv run pytest tests/_queue_fakes_test.py -q` | ❌ W0 | ⬜ pending |
| 34-01-01 | 01 | 1 | queue-depth read | — | `get_queue_activity` sums agent_queued/active across all non-revoked agents | unit | `uv run pytest tests/test_services/test_pipeline.py -k queue_activity -q` | ❌ W0 | ⬜ pending |
| 34-01-02 | 01 | 1 | scheduled excluded | — | controller cron jobs not counted (queued+active only) | unit | `uv run pytest tests/test_services/test_pipeline.py -k scheduled -q` | ❌ W0 | ⬜ pending |
| 34-01-03 | 01 | 1 | failure isolation | — | missing app.state attrs / redis error → all-zero, no raise | unit | `uv run pytest tests/test_services/test_pipeline.py -k degrade -q` | ❌ W0 | ⬜ pending |
| 34-02-01 | 02 | 2 | stats surface | — | `/pipeline/stats` context + initial `dashboard()` carry agent_busy/controller_busy | unit | `uv run pytest tests/test_routers/test_pipeline.py -k busy -q` | ❌ W0 | ⬜ pending |
| 34-03-01 | 03 | 2 | processing card | — | card renders bar+counts when agent_busy>0, empty when idle; denom guard | unit | `uv run pytest tests/test_routers/test_pipeline.py -k processing_card -q` | ❌ W0 | ⬜ pending |
| 34-03-02 | 03 | 2 | progress math | — | percent = analyzed/(analyzed+agent_busy); 0/0 → empty | unit | `uv run pytest tests/test_routers/test_pipeline.py -k progress -q` | ❌ W0 | ⬜ pending |
| 34-04-01 | 04 | 2 | 4 buttons + disable | — | 4 buttons render; agentBusy/controllerBusy disable bindings present | unit | `uv run pytest tests/test_routers/test_pipeline.py -k stage_cards -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/_queue_fakes.py` — add `async def count(self, kind)` to `FakeQueue`; ensure `FakeTaskRouter.queue_for` returns countable fakes with seedable per-kind depths
- [ ] Existing `conftest.py` `wire_fakes`/`install_fake_queues` fixtures cover app.state faking (no new fixtures expected)

*Existing pytest infrastructure otherwise covers all phase requirements.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Refresh-survival of the live progress card on the real stack | observability | Requires live SAQ queue with real in-flight jobs (nox agent worker) | After homelab redeploy: trigger Run Analysis, refresh `/pipeline/`, confirm progress card + "N queued" persists and Run Analysis is disabled until the queue drains |

*All unit-testable behaviors have automated verification; the live refresh-survival check is inherently integration/manual.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (FakeQueue.count)
- [ ] No watch-mode flags
- [ ] Feedback latency < 120s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
