---
phase: 58
slug: enrich-analyze-workspaces
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-30
---

# Phase 58 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio + httpx `AsyncClient` (85% min coverage) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_shell_routes.py tests/test_enrich_analyze_workspaces.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_shell_routes.py tests/test_enrich_analyze_workspaces.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing` (≥85%)
- **Before `/gsd:verify-work`:** Full suite green + `uv run ruff check . && uv run mypy .`
- **Max feedback latency:** ~60 seconds

---

## Per-Task Verification Map

> Task IDs are assigned during planning. Rows below derive from the RESEARCH Phase Requirements → Test Map and bind each phase requirement to its validating behavior; the planner/executor maps these onto concrete task IDs.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | TBD | TBD | WORK-01 | T-57-01 | Discover fragment renders recent-scans table + discovered/not-yet-enriched sub-count; SCAN/RECOVER present; stage name stays whitelisted (no path splice) | route/render | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_discover_workspace -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | WORK-02 | — | Metadata/Fingerprint workspaces render queue + ALL button posting to existing `/pipeline/extract-metadata` and `/pipeline/fingerprint`; `count`/`no_active_agent` branches of `trigger_response.html` | route/render | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_metadata_trigger_all_wired -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | WORK-03 | — | All 3 lane cards always render; `not configured` (no cloud_target) vs `offline` (no agent / localqueue_unreachable) labels; Inadmissible carries `role="alert"`, admission card does NOT | render + state | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_lane_cards_states -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | WORK-04 | T-58-XSS | In-flight row shows lane badge + `running` + N/M windows from mid-flight signal; completed row shows `window {analyzed}/{total}` from aggregate; per-file lane derived from cloud_job; paths autoescaped | render | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_analyze_file_table_lane_and_windows -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | WORK-05 | — | Workspace fragments contain NO `hx-trigger="every"`/`setInterval`; live values update via OOB; exactly one `/pipeline/stats` request per cycle; `visibilitychange` guard sheds polling when backgrounded | structural assert | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_single_poll_discipline -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | R-5 | — | Workspace fragments are bare (no `<html>`/`extends`/second skip-link); dead-template AST guard green | structural | reuse Phase-57 fragment-bareness assertion + AST guard | ✅ exists (extend) | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_enrich_analyze_workspaces.py` — covers WORK-01..05 (new file; or extend `tests/test_shell_routes.py`)
- [ ] Reuse existing `conftest.py` fixtures (`AsyncClient`, seeded files/agents/`cloud_job` rows). Confirm fixtures can seed `cloud_job` (cloud_phase variants) + `analysis` aggregate rows for lane/window assertions.
- [ ] Single-poll structural assertion helper (grep rendered fragment for `hx-trigger="every"` / `setInterval` → must be absent).

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live OOB refresh visibly updates lane capacity + windowed bars without manual reload | WORK-05 | End-to-end browser timing/visibility behavior; the structural single-poll assertion covers the no-second-loop guarantee, but observable live refresh is a UAT check | Open `/s/analyze` with files in-flight; confirm in the network tab exactly one `/pipeline/stats` request per 5s, values refresh in place, and polling pauses when the tab is backgrounded |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
