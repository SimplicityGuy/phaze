---
phase: 52
slug: job-runner-image-one-shot-entrypoint
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-27
---

# Phase 52 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio + respx (httpx mocking); `uv run pytest` |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`); coverage gate 85% (CLAUDE.md) |
| **Quick run command** | `uv run pytest tests/test_job_runner.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~30 seconds (unit; no live cluster/bucket) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_job_runner.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing` (≥85%)
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

> Task IDs are assigned by the planner; this map binds each phase requirement to its
> automated proof. Phase 52 is unit-tested against a fake control plane (respx) and a
> fixture models dir — no live cluster or object storage (those arrive in Phases 53/54).

| Requirement | Behavior | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|-------------|----------|------------|-----------------|-----------|-------------------|-------------|--------|
| KJOB-01 | Image builds FROM x86 api base, zero new pip deps | — | N/A | integration (Dockerfile grep/lint) + matrix-entry test | `uv run pytest tests/test_deployment/ -k job` | ❌ W0 | ⬜ pending |
| KJOB-02 | presign→download→verify→analyze→POST→exit happy path | — | N/A | unit (respx fake control plane + fixture audio) | `uv run pytest tests/test_job_runner.py -k happy_path -x` | ❌ W0 | ⬜ pending |
| KJOB-03 | windowed/streaming only — no whole-file MonoLoader decode | — | N/A | unit (assert `analyze_file` path) + grep/AST guard for `MonoLoader` | `uv run pytest tests/test_job_runner.py -k no_monoloader` | ❌ W0 | ⬜ pending |
| KJOB-04 | distinct non-zero exit per failure class; never exit 0 on failure | — | N/A | unit (parametrized exit-code matrix) + container `echo $?` smoke | `uv run pytest tests/test_job_runner.py -k exit_code` | ❌ W0 | ⬜ pending |
| KJOB-05 | callback uses baked CA; no `verify=False` anywhere | T-52-01 | TLS trust via baked CA only; no verify bypass | unit (assert `verify=<ca>` passed) + repo grep guard | `uv run pytest tests/test_job_runner.py -k ca_verify` | ❌ W0 | ⬜ pending |
| (boundary) | entrypoint imports NO `phaze.database` | T-52-02 | HTTP-only agent boundary preserved | subprocess import-boundary | `uv run pytest tests/test_task_split.py -k job_runner` | ❌ W0 (extend existing) | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_job_runner.py` — new test module: happy path, exit-code matrix, no-MonoLoader guard, CA-verify
- [ ] `tests/conftest.py` (or local fixtures) — fixture audio file + fixture `models_dir` + respx fake control plane
- [ ] Extend `tests/test_task_split.py` — import-boundary clone of `test_agent_worker_does_not_import_phaze_database`

*Framework already present (pytest + respx in dev deps) — no install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real multi-hour set analyzes under a hard pod memory limit without OOM | KJOB-03 (success criterion 3) | Requires a real long set + a memory-constrained container run; peak RSS is measured, not unit-asserted | Run the built image against a representative long set under `--memory` (docker) sizing; record peak RSS to size the Job memory request in Phase 54 |

*All other phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
