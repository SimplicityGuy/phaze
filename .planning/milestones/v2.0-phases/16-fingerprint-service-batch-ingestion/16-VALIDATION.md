---
phase: 16
slug: fingerprint-service-batch-ingestion
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-04-01
---

# Phase 16 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/test_services/test_fingerprint*.py tests/test_tasks/test_fingerprint*.py -x -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~20 seconds |

---

## Sampling Rate

- **After every task commit:** Run quick command
- **After every plan wave:** Run full suite
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 20 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 16-01-01 | 01 | 1 | FPRINT-01 | unit | `uv run pytest tests/test_services/test_fingerprint.py -x -q` | ❌ W0 | ⬜ pending |
| 16-01-02 | 01 | 1 | FPRINT-01 | integration | `docker compose up -d audfprint panako && curl localhost:*/health` | ❌ W0 | ⬜ pending |
| 16-02-01 | 02 | 2 | FPRINT-02 | unit | `uv run pytest tests/test_tasks/test_fingerprint.py -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_services/test_fingerprint.py` — stubs for fingerprint service client tests
- [ ] `tests/test_tasks/test_fingerprint.py` — stubs for batch ingestion task tests

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Fingerprint containers start and respond | FPRINT-01 | Docker container health | `docker compose up audfprint panako`, verify health endpoints respond |
| Fingerprint DB persists across restart | FPRINT-02 | Docker volume persistence | Ingest a file, restart container, query — same result |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 20s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
