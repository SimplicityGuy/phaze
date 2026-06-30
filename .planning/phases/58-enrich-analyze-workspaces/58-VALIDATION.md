---
phase: 58
slug: enrich-analyze-workspaces
status: ready
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-30
updated: 2026-06-30
---

# Phase 58 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Task IDs bound to the created plans (58-01..58-04) on 2026-06-30.

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

> Task IDs are `{plan}-{task}`. The Phase-58 test file + seed helpers are created in Wave 0
> (task 58-01-00); the four workspace tests start as xfail stubs there and are converted to
> real assertions by the task listed below.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 58-02-03 | 58-02 | 2 | WORK-01 | T-57-01 | Discover fragment renders recent-scans table + discovered/not-yet-enriched sub-count; SCAN/RECOVER present; `dag-seed-notYetEnriched` placeholder present; stage name stays whitelisted (no path splice) | route/render | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_discover_workspace -x` | ✅ 58-01-00 | ⬜ pending |
| 58-03-02 | 58-03 | 3 | WORK-02 | T-58-ENQ | Metadata/Fingerprint workspaces render queue + ALL button posting to existing `/pipeline/extract-metadata` and `/pipeline/fingerprint`; no `EXTRACT SELECTED`/checkbox (D-02); `count`/`no_active_agent` branches of `trigger_response.html` | route/render | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_metadata_trigger_all_wired -x` | ✅ 58-01-00 | ⬜ pending |
| 58-04-02 | 58-04 | 4 | WORK-03 | T-58-ALERT, T-58-SEED | All 3 lane cards always render; `not configured` (no cloud_target) vs `offline` (no agent / localqueue_unreachable) labels + 0 capacity; Inadmissible carries `role="alert"`, admission card does NOT; `dag-seed-computeOnline` placeholder present so A1 numeral seeds | render + state | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_lane_cards_states -x` | ✅ 58-01-00 | ⬜ pending |
| 58-04-03 | 58-04 | 4 | WORK-04 | T-58-XSS | In-flight row shows lane badge + `running` + the mid-flight `N/M windows` indicator (partial `fine_windows_analyzed`<`fine_windows_total`, 57.1 PR #184); completed row shows full `window {analyzed}/{total}` from aggregate; per-file lane derived from cloud_job; paths autoescaped. A render emitting only `running` MUST fail | render | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_analyze_file_table_lane_and_windows -x` | ✅ 58-01-00 | ⬜ pending |
| 58-01-01 | 58-01 | 1 | WORK-05 | T-58-POLL | Shell fires exactly one `/pipeline/stats` poll from persistent chrome + `visibilitychange` shed; workspace fragments contain NO `hx-trigger="every"`/`setInterval`; live values update via OOB | structural assert | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_single_poll_discipline -x` | ✅ 58-01-00 | ⬜ pending |
| 58-01-00 | 58-01 | 1 (Wave 0) | R-5 | T-57-01 | Workspace fragments are bare (no `<html>`/`extends`/second skip-link); dead-template AST guard green | structural | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_stage_fragment_is_bare -x` | ✅ 58-01-00 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/test_enrich_analyze_workspaces.py` — covers WORK-01..05 + R-5 (created in task 58-01-00 as the single Phase-58 test file; four workspace tests land as xfail stubs, converted by 58-02-03 / 58-03-02 / 58-04-02 / 58-04-03)
- [x] Reuse existing `conftest.py` fixtures (`AsyncClient`, `session`, `seed_test_agent`); 58-01-00 adds module-level async seed helpers (`_seed_analysis(session, file_id, fine_done, fine_total)`, `_seed_cloud_job(session, file_id, cloud_phase)`) for `cloud_job` (cloud_phase variants) + `analysis` aggregate rows (incl. partial-window in-flight rows for the WORK-04 mid-flight assertion).
- [x] Single-poll structural assertion helper — `test_single_poll_discipline` (58-01-00) greps the rendered shell + each fragment for `hx-trigger="every"` / `setInterval`.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live OOB refresh visibly updates lane capacity + windowed bars without manual reload | WORK-05 | End-to-end browser timing/visibility behavior; the structural single-poll assertion covers the no-second-loop guarantee, but observable live refresh is a UAT check | Open `/s/analyze` with files in-flight; confirm in the network tab exactly one `/pipeline/stats` request per 5s, values refresh in place (incl. the mid-flight N/M windows count), and polling pauses when the tab is backgrounded |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (test file + seed helpers in 58-01-00)
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** validated against plans 58-01..58-04 (planner revision 2026-06-30); per-task green status set during execution.
