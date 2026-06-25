---
phase: 48
slug: compute-agent-type
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-25
---

# Phase 48 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `48-RESEARCH.md` § Validation Architecture. Task IDs are filled by the planner.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (project standard) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_cli tests/test_models tests/test_config tests/test_task_split.py -x -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` (≥85% per CLAUDE.md) |
| **Estimated runtime** | ~20-40 seconds (quick) / full suite per project norm |

---

## Sampling Rate

- **After every task commit:** Run the quick run command above
- **After every plan wave:** Run the full suite command
- **Before `/gsd:verify-work`:** Full suite green + `uv run ruff check .` + `uv run mypy .` + `pre-commit run --all-files`
- **Max feedback latency:** ~40 seconds (quick)

---

## Per-Task Verification Map

> Requirement-level map from research. Planner assigns concrete `{48-PP-TT}` task IDs and copies the matching row(s) into each plan.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 48-01-01 | 48-01 | 1 | CLOUDAGENT-01 | T-48-01-A (bad kind) | `Agent.kind` defaults `'fileserver'`; DB CHECK rejects values outside `{fileserver,compute}` | unit | `uv run pytest tests/test_models/test_agent.py -k kind -x` | ✅ exists | ✅ green (3 passed) |
| 48-01-02 | 48-01 | 1 | CLOUDAGENT-01 | — | migration 024 up backfills existing rows → `'fileserver'`; down drops column | unit (DB) | `uv run pytest tests/test_migrations/test_024.py -x` | ✅ exists | ✅ green (integration suite) |
| 48-02-01 | 48-02 | 2 | CLOUDAGENT-01 | T-48-02-A | `agents add --kind compute` inserts row with `kind='compute'` + empty scan_roots, no error; `--kind fileserver` still requires scan roots | unit (DB) | `uv run pytest tests/test_cli/test_agents_add.py -k "compute or kind or fileserver" -x` | ✅ exists | ✅ green (integration suite) |
| 48-02-02 | 48-02 | 2 | CLOUDAGENT-01 | T-48-02-A | `AgentSettings(kind='compute', scan_roots=[])` validates (no ValueError); fileserver still rejects empty | unit | `uv run pytest tests/test_config/test_agent_settings_kind.py -x` | ✅ exists | ✅ green (6 passed) |
| 48-03-02 | 48-03 | 2 | CLOUDAGENT-02 | T-48-03-A (EoP) | `agent_worker` import graph excludes `phaze.database`/`.session`/`sqlalchemy.ext.asyncio`; Postgres broker present | unit (subprocess) | `uv run pytest tests/test_task_split.py -x` | ✅ exists | ✅ green (7 passed) |
| existing | — | — | CLOUDAGENT-02 | — | compute agent uses same `phaze-agent-<id>` queue + `PUT /api/internal/agent/analysis/{file_id}` path (no new mechanic) | unit | covered by existing `AgentTaskRouter` + `agent_analysis` tests | ✅ exists | ✅ green (integration suite) |
| 48-03-01 | 48-03 | 2 | CLOUDAGENT-03 | T-48-03-B | `agents_table.html` renders Kind badge for `kind='compute'` rows alongside liveness pill + queue depth, on both full-page and `/_table` poll paths | unit (template render, DB) | `uv run pytest tests/test_routers/test_admin_agents.py -k kind -x` | ✅ exists | ✅ green (integration suite) |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

> **Coverage note:** All 7 mapped requirement-rows are COVERED and green. No-DB layers (model, config, import-boundary) re-run live during this audit (16 passed). DB-backed layers (migration 024, CLI registration, admin-page render) confirmed green in the full integration suite (`just integration-test` → 2071 passed, 0 failed). No MISSING or PARTIAL gaps — no test generation required.

---

## Wave 0 Requirements

- [x] `tests/test_models/` — `Agent.kind` default `'fileserver'` + CHECK constraint test (CLOUDAGENT-01)
- [x] `tests/test_migrations/` — migration 024 up/down + backfill test (CLOUDAGENT-01)
- [x] `tests/test_cli/test_agents_add.py` — `--kind compute` with empty scan roots; assert `--kind fileserver` still requires scan roots (CLOUDAGENT-01)
- [x] `tests/test_config/` — `AgentSettings` compute path accepts empty scan roots; fileserver still rejects (CLOUDAGENT-01)
- [x] `tests/test_routers/` — admin agents table renders kind badge for a compute row (CLOUDAGENT-03)
- [x] Extend `tests/test_task_split.py` to explicitly cover the compute-agent invariant (CLOUDAGENT-02)
- Framework install: none — pytest + pytest-asyncio already present.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Compute agent (running the Phase 47 arm64 image) appears on the live Agents admin page with a kind badge, liveness, and queue depth | CLOUDAGENT-03 | End-to-end requires a real registered agent draining a real queue against a live deployment | Register via `agents add --kind compute`, start the agent container, enqueue an analysis job, observe the Agents admin page shows the compute agent with kind badge + green liveness + queue depth |

*Automated tests cover model/migration/CLI/config/template-render layers; live capacity visibility is the only manual leg.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 40s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** verified 2026-06-25

---

## Validation Audit 2026-06-25

| Metric | Count |
|--------|-------|
| Requirement-rows mapped | 7 |
| Covered (green) | 7 |
| Partial | 0 |
| Missing | 0 |
| Gaps resolved | 0 (none found) |
| Tests generated | 0 (full coverage already present) |

State A audit (existing VALIDATION.md). Every requirement row maps to an existing,
green automated test. No-DB layers re-run live (16 passed); DB-backed layers green in
the full integration suite (2071 passed, 0 failed). The sole manual-only leg (live
compute-agent admin-page render) is documented above and is the only non-automatable
verification — not a Nyquist gap. Phase is **nyquist_compliant**.
