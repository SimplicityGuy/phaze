---
phase: 48
slug: compute-agent-type
status: draft
nyquist_compliant: false
wave_0_complete: false
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
| TBD | TBD | TBD | CLOUDAGENT-01 | T-48-02 (bad kind) | `Agent.kind` defaults `'fileserver'`; DB CHECK rejects values outside `{fileserver,compute}` | unit | `uv run pytest tests/test_models/ -k kind -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | CLOUDAGENT-01 | — | migration 024 up backfills existing rows → `'fileserver'`; down drops column | unit | `uv run pytest tests/test_migrations/ -k 024 -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | CLOUDAGENT-01 | T-48-02 | `agents add --kind compute` inserts row with `kind='compute'` + empty scan_roots, no error; `--kind fileserver` still requires scan roots | unit | `uv run pytest tests/test_cli/test_agents_add.py -k compute -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | CLOUDAGENT-01 | T-48-02 | `AgentSettings(kind='compute', scan_roots=[])` validates (no ValueError); fileserver still rejects empty | unit | `uv run pytest tests/test_config -k compute -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | CLOUDAGENT-02 | T-48-01 (EoP) | `agent_worker` import graph excludes `phaze.database`/`.session`/`sqlalchemy.ext.asyncio`; Postgres broker present | unit (subprocess) | `uv run pytest tests/test_task_split.py -x` | ✅ exists — reaffirm/extend | ⬜ pending |
| TBD | TBD | TBD | CLOUDAGENT-02 | — | compute agent uses same `phaze-agent-<id>` queue + `PUT /api/internal/agent/analysis/{file_id}` path (no new mechanic) | unit | covered by existing `AgentTaskRouter` + `agent_analysis` tests | ✅ exists | ⬜ pending |
| TBD | TBD | TBD | CLOUDAGENT-03 | T-48-02 | `agents_table.html` renders Kind badge for `kind='compute'` rows alongside liveness pill + queue depth | unit (template render) | `uv run pytest tests/test_routers -k "admin_agents and kind" -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_models/` — `Agent.kind` default `'fileserver'` + CHECK constraint test (CLOUDAGENT-01)
- [ ] `tests/test_migrations/` — migration 024 up/down + backfill test (CLOUDAGENT-01)
- [ ] `tests/test_cli/test_agents_add.py` — `--kind compute` with empty scan roots; assert `--kind fileserver` still requires scan roots (CLOUDAGENT-01)
- [ ] `tests/test_config/` — `AgentSettings` compute path accepts empty scan roots; fileserver still rejects (CLOUDAGENT-01)
- [ ] `tests/test_routers/` — admin agents table renders kind badge for a compute row (CLOUDAGENT-03)
- [ ] Extend `tests/test_task_split.py` to explicitly cover the compute-agent invariant (CLOUDAGENT-02)
- Framework install: none — pytest + pytest-asyncio already present.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Compute agent (running the Phase 47 arm64 image) appears on the live Agents admin page with a kind badge, liveness, and queue depth | CLOUDAGENT-03 | End-to-end requires a real registered agent draining a real queue against a live deployment | Register via `agents add --kind compute`, start the agent container, enqueue an analysis job, observe the Agents admin page shows the compute agent with kind badge + green liveness + queue depth |

*Automated tests cover model/migration/CLI/config/template-render layers; live capacity visibility is the only manual leg.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 40s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
