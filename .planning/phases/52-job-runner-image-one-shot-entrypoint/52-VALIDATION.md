---
phase: 52
slug: job-runner-image-one-shot-entrypoint
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-27
validated: 2026-06-27
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
| KJOB-01 | Image builds FROM x86 api base, zero new pip deps | — | N/A | integration (Dockerfile grep/lint) + matrix-entry test | `uv run pytest tests/test_deployment/ -k job` | ✅ `test_job_image.py` | ✅ green (5) |
| KJOB-02 | presign→download→verify→analyze→POST→exit happy path | — | N/A | unit (respx fake control plane + fixture audio) | `uv run pytest tests/test_job_runner.py -k happy_path -x` | ✅ `test_job_runner.py` | ✅ green (1) |
| KJOB-03 | windowed/streaming only — no whole-file MonoLoader decode | — | N/A | unit (assert `analyze_file` path) + grep/AST guard for `MonoLoader` | `uv run pytest tests/test_job_runner.py -k no_monoloader` | ✅ `test_job_runner.py` | ✅ green (1) |
| KJOB-04 | distinct non-zero exit per failure class; never exit 0 on failure | — | N/A | unit (parametrized exit-code matrix) + container `echo $?` smoke | `uv run pytest tests/test_job_runner.py -k exit_code` | ✅ `test_job_runner.py` | ✅ green (7) |
| KJOB-05 | callback uses baked CA; no `verify=False` anywhere | T-52-01 | TLS trust via baked CA only; no verify bypass | unit (assert `verify=<ca>` passed) + repo grep guard | `uv run pytest tests/test_job_runner.py -k ca_verify` | ✅ `test_job_runner.py` | ✅ green (1) |
| (boundary) | entrypoint imports NO `phaze.database` | T-52-02 | HTTP-only agent boundary preserved | subprocess import-boundary | `uv run pytest tests/test_task_split.py -k job_runner` | ✅ `test_task_split.py` | ✅ green (1) |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/test_job_runner.py` — new test module: happy path, exit-code matrix, no-MonoLoader guard, CA-verify
- [x] `tests/conftest.py` (or local fixtures) — fixture audio file + fixture `models_dir` + respx fake control plane (`job_env` fixture)
- [x] Extend `tests/test_task_split.py` — import-boundary clone of `test_agent_worker_does_not_import_phaze_database`

*Framework already present (pytest + respx in dev deps) — no install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real multi-hour set analyzes under a hard pod memory limit without OOM | KJOB-03 (success criterion 3) | Requires a real long set + a memory-constrained container run; peak RSS is measured, not unit-asserted | Run the built image against a representative long set under `--memory` (docker) sizing; record peak RSS to size the Job memory request in Phase 54 |

*All other phase behaviors have automated verification.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 30s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** validated 2026-06-27 — all 5 KJOB requirements + the import boundary have green automated proofs; one peak-RSS check remains documented manual-only.

---

## Validation Audit 2026-06-27

| Metric | Count |
|--------|-------|
| Requirements audited | 6 (KJOB-01..05 + import boundary) |
| COVERED (green automated) | 6 |
| Gaps found | 0 |
| Resolved | 0 (no gaps) |
| Escalated to manual-only | 0 (1 pre-existing manual item retained) |

State A audit: the plan-time Wave-0 test deliverables were all created during execution and run green. Per-requirement evidence (live run): KJOB-01 → 5 passed (`test_job_image.py`); KJOB-02 → 1 (`happy_path`); KJOB-03 → 1 (`no_monoloader`); KJOB-04 → 7 (`exit_code` matrix, incl. the WR-01/WR-02 `analyze_non_dict_result`/`analyze_bad_window_key` cases added during code-review fixes); KJOB-05 → 1 (`ca_verify`); import boundary → 1 (`test_task_split.py`). No `nyquist-auditor` spawn needed — zero gaps to fill.
