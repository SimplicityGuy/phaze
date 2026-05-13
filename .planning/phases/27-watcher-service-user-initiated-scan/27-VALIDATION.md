---
phase: 27
slug: watcher-service-user-initiated-scan
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-13
---

# Phase 27 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Source of truth: `27-RESEARCH.md` §"Validation Architecture" (23 named pytest cases).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x + pytest-asyncio + respx (existing — no Wave 0 install) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_agent_watcher tests/test_routers/test_agent_scan_batches.py tests/test_routers/test_agent_files_batch_id.py tests/test_routers/test_pipeline_scans.py tests/test_tasks/test_scan_directory.py -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | quick ~30s · full ~3min |

---

## Sampling Rate

- **After every task commit:** Run quick command for the touched module's tests
- **After every plan wave:** Run quick command across all Phase 27 modules
- **Before `/gsd-verify-work`:** Full suite green, ≥85% coverage on new modules
- **Max feedback latency:** 30 seconds (quick command)

---

## Per-Task Verification Map

> Populated by planner from RESEARCH.md §"Validation Architecture" → "Test Plan (23 cases)".
> Each row maps a plan-task to its automated verify command and the requirement it proves.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | ⬜ pending |

*Planner MUST fill this table from RESEARCH.md §"Validation Architecture" before plans go to checker.*
*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_agent_watcher/__init__.py` — package marker for new test module
- [ ] `tests/test_agent_watcher/conftest.py` — shared fixtures (synthetic watchdog events, asyncio loop, respx mocked PhazeAgentClient)
- [ ] `pyproject.toml` — `watchdog>=4.0` added to `[project].dependencies` (D-23); ensures imports resolve before any agent_watcher test runs
- [ ] `tests/test_task_split.py` — extend existing import-boundary test with a parallel case for `phaze.agent_watcher` (D-22 / D-25 parity)

*Existing infrastructure (pytest-asyncio, respx, the test-router pattern, the `test_task_split.py` harness) covers the rest — no framework install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Browser-rendered "Trigger Scan" card layout, focus ring, color contrast, reduced-motion respect | UI-SPEC.md §Accessibility, §Responsive | Visual contract; automated DOM tests can't cover rendered pixels | Manual: pull up `/pipeline/`, tab through form, check focus rings, run macOS "Reduce Motion" toggle, verify scan_progress_card polling halts on completion |
| End-to-end on real Docker compose with `rsync --inplace` writing a 200MB file into `/data/music` | SCAN-04 settle behavior under real I/O | Synthetic mtime-stability tests cover the state machine; this proves the wall-clock behavior under a real writer pattern | Manual: `docker compose up watcher`, `rsync --inplace` a large file from a host shell, observe no early POST in agent logs, verify single FileRecord appears after settle period |
| Operator dropdown UX with 10+ registered agents | UI-SPEC.md §Trigger Scan card | Layout/wrapping behavior at scale isn't covered by Playwright-less tests | Manual: register 10 dummy agents in dev DB, view `/pipeline/`, verify dropdown remains usable |

*All other phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All planned tasks have `<automated>` verify command OR are in Wave 0
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (watchdog dep, test module skeleton, import-boundary test extension)
- [ ] No watch-mode flags (`--watch`, `--reuse-db`, `-x` in quick command)
- [ ] Feedback latency < 30s for quick command
- [ ] `nyquist_compliant: true` set in frontmatter once planner has filled the per-task map and checker has run

**Approval:** pending
