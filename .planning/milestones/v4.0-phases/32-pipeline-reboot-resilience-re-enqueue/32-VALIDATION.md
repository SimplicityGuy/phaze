---
phase: 32
slug: pipeline-reboot-resilience-re-enqueue
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-11
---

# Phase 32 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) |
| **Config file** | pyproject.toml ([tool.pytest.ini_options]) |
| **Quick run command** | `uv run pytest tests/test_services/test_reenqueue.py tests/test_tasks/ -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60–120 seconds (full suite) |

---

## Sampling Rate

- **After every task commit:** quick run command
- **After every plan wave:** full suite command
- **Before `/gsd:verify-work`:** full suite green + coverage ≥85%
- **Max feedback latency:** ~120 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 32-00-01 | 00 | 0 | harness | — | dedup-aware FakeQueue: 2nd enqueue of an in-flight key returns None | unit | `uv run pytest tests/_queue_fakes_test.py -k dedup -q` | ❌ W0 | ⬜ pending |
| 32-01-01 | 01 | 1 | shared key+payload | — | `process_file_job_key(file_id)` == `f"process_file:{file_id}"`; both producers use it | unit | `uv run pytest tests/test_services/test_reenqueue.py -k key -q` | ❌ W0 | ⬜ pending |
| 32-01-02 | 01 | 1 | complete payload | — | enqueue carries full ProcessFilePayload (5 fields) + timeout=14400 + retries=2 | unit | `uv run pytest tests/test_services/test_reenqueue.py -k payload -q` | ❌ W0 | ⬜ pending |
| 32-01-03 | 01 | 1 | dashboard parity | — | routers/pipeline.py `_enqueue_analysis_jobs` now sets the same deterministic key | unit | `uv run pytest tests/test_routers/test_pipeline.py -k key -q` | ❌ W0 | ⬜ pending |
| 32-02-01 | 02 | 2 | re-enqueue service | — | re-enqueues all DISCOVERED onto active agent's queue; returns count | unit | `uv run pytest tests/test_services/test_reenqueue.py -k discovered -q` | ❌ W0 | ⬜ pending |
| 32-02-02 | 02 | 2 | zero-agent skip | — | NoActiveAgentError → logged warning, count 0, no raise | unit | `uv run pytest tests/test_services/test_reenqueue.py -k no_agent -q` | ❌ W0 | ⬜ pending |
| 32-02-03 | 02 | 2 | dedup no-op | — | re-enqueue of an in-flight-keyed file is a no-op (dedup fake or @integration) | unit/integration | `uv run pytest tests/test_services/test_reenqueue.py -k dedup -q` | ❌ W0 | ⬜ pending |
| 32-03-01 | 03 | 3 | startup hook | — | controller startup calls re-enqueue once (count surfaced/logged) | unit | `uv run pytest tests/test_tasks/test_controller_reenqueue.py -k startup -q` | ❌ W0 | ⬜ pending |
| 32-03-02 | 03 | 3 | cron registration | — | CronJob(reenqueue, cron=...) present in controller settings functions/cron list | unit | `uv run pytest tests/test_tasks/test_controller_reenqueue.py -k cron -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/_queue_fakes.py` — add dedup modeling to `FakeQueue`: track in-flight keys; a 2nd `enqueue` with a live key returns `None` (so the dedup no-op behavior is unit-testable without real Redis). If a dedup-aware fake is deemed too lossy, the dedup test falls back to a real-Redis `@pytest.mark.integration` test per RESEARCH §Q5.
- [ ] Confirm `FakeQueue.enqueue` already captures `key` (RESEARCH: yes, via captured_policy) — assert against it for the shared-key-format test.

*Existing pytest infrastructure otherwise covers all phase requirements.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real reboot self-heal | reboot resilience | Requires a real host reboot with the live corpus + agent worker | After redeploy: reboot the homelab host; confirm DISCOVERED files re-enqueue automatically (no manual "Run Analysis") and analysis resumes |
| Real-Redis dedup under restart | dedup | Container restart with surviving Redis | Restart controller while jobs are queued; confirm the startup re-enqueue does NOT duplicate in-flight jobs (deterministic-key dedup) |

*Unit/integration tests cover the dedup primitive and routing; the full reboot loop is inherently a manual/integration check.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (dedup-aware FakeQueue)
- [ ] No watch-mode flags
- [ ] Feedback latency < 120s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
