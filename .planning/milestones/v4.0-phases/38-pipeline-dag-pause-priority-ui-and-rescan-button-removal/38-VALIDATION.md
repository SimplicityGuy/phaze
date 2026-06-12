---
phase: 38
slug: pipeline-dag-pause-priority-ui-and-rescan-button-removal
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-06-12
---

# Phase 38 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`uv run pytest`) |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_dag_canvas_render.py tests/test_pipeline_dag_context.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~45s (pure-Jinja render + DB-backed client fixture) |

> The DAG UI is tested two ways already — mirror these exactly: **pure-Jinja render tests** (`test_dag_canvas_render.py` — render the partial with a fake context, assert markup/topology/copy) and **DB-backed integration tests** via the shared `client` fixture (GET `/pipeline/` and `/pipeline/stats`, assert OOB seeds). Store-literal text assertions live in `test_pipeline_dag_context.py`.

---

## Sampling Rate

- **After every task commit:** `uv run pytest tests/test_dag_canvas_render.py tests/test_pipeline_dag_context.py -x`
- **After every plan wave:** `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** full suite green + ≥85% coverage on touched modules
- **Max feedback latency:** ~45 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 38-rescan | 01 | 1 | REQ-38-3 | — | "Rescan Files" anchor gone; no `href="#trigger-scan-heading"` Rescan link in canvas | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ add to existing | ⬜ pending |
| 38-toggle | 02 | 2 | REQ-38-1 | T-38-OOB | Pause/Resume two `x-show`-gated buttons post to `/pipeline/stages/{stage}/pause\|resume` with `hx-swap="none"` | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ add | ⬜ pending |
| 38-stepper | 02 | 2 | REQ-38-2 | T-38-DELTA | ▲/▼ steppers post `/pipeline/stages/{stage}/priority` `{delta:-10}`/`{delta:10}`, value bound to `<stage>Priority`; ▲ disabled at 0, ▼ disabled at 100 | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ add | ⬜ pending |
| 38-notgated | 02 | 2 | REQ-38-1, REQ-38-2 | — | controls are NOT gated by `agentBusy` (no `nodes.<node>.blocked` on control markup) | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ add | ⬜ pending |
| 38-context | 03 | 1 | REQ-38-4 | T-38-XSS | `_build_dag_context` returns the 6 `<stage>Paused`/`<stage>Priority` int keys; `base.html` store seeds all 6 to 0 | DB-backed + store-text | `uv run pytest tests/test_pipeline_dag_context.py -x` | ⚠️ extend `_NEW_STORE_KEYS` | ⬜ pending |
| 38-stats-oob | 03 | 1 | REQ-38-4 | T-38-OOB | `GET /pipeline/stats` emits an OOB `dag-seed-<key>` paragraph for each of the 6 new keys | integration | `uv run pytest tests/test_pipeline_dag_context.py -x` | ⚠️ extend | ⬜ pending |
| 38-degrade | 03 | 1 | REQ-38-4 | T-38-DEGRADE | poll degrades to 200 (defaults) when the control table is unreadable | integration | `uv run pytest tests/test_pipeline_dag_context.py -x` | ⚠️ add (mirror `test_stats_poll_degrades_to_200_without_counter_source`) | ⬜ pending |
| 38-overlap | 02 | 2 | guard | — | overlap regression passes with recomputed `NODE_LAYOUT` + taller chips | pure render | `uv run pytest tests/test_dag_canvas_render.py::test_topology_column_one_chips_do_not_overlap -x` | ⚠️ update `min_chip_height` | ⬜ pending |
| 38-hxpost4 | 02 | 2 | guard | — | the "exactly 4 hx-post" test updated to include the new stage-control posts | pure render | `uv run pytest tests/test_dag_canvas_render.py -x` | ⚠️ update existing | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] Extend `tests/test_dag_canvas_render.py`: Rescan-removed assertion; control-fragment render (toggle + steppers per agent node, hx targets, `hx-swap=none`, disabled bounds, not-`agentBusy`-gated); update the exact-4-hx-post test; update the overlap test's `min_chip_height`.
- [ ] Extend `tests/test_pipeline_dag_context.py`: add the 6 keys to `_NEW_STORE_KEYS`; assert `_build_dag_context` returns them as ints; assert OOB seeds for them; add a control-table-unreadable degrade test.
- [ ] `services/pipeline.py::get_stage_controls` degrade-safe reader (depends on Phase 37's `PipelineStageControl` model — import lands when Phase 37 ships).
- [ ] No new test *file* needed — extend the two existing DAG test files (mirror their structure).

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live pause/priority reflected on the DAG across the 5s poll on homelab | REQ-38-4 | Requires the running dashboard + a real Phase 37 backend + browser | After redeploy: open `/pipeline/`, click Pause on a stage, confirm the chip flips to Resume and the stage's paused state persists across the next poll; step priority, confirm the number updates without flicker |
| Visual: taller agent chips do not overlap in the rendered canvas | guard | Pixel layout is render-and-measure | Load `/pipeline/` in a browser, confirm the 3 agent chips + Discovery node render without visual overlap (the recomputed `NODE_LAYOUT` gutter) |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
