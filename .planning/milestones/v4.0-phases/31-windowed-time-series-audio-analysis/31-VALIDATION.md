---
phase: 31
slug: windowed-time-series-audio-analysis
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-10
---

# Phase 31 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution. Derived from 31-RESEARCH.md Validation Architecture.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| **Quick run command** | `uv run pytest tests/test_services/test_analysis.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~quick: seconds; full suite: minutes (integration marks may be slow) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_services/test_analysis.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite green + `pre-commit run --all-files` (ruff/mypy/bandit)
- **Max feedback latency:** < 30 seconds (quick run)

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| (spike) | 01 | 0 | ANL-01 | — | N/A (throwaway) | manual | `uv run scripts/spike_windowed_analysis.py` (not committed) | ❌ W0 | ⬜ pending |
| TBD | — | — | ANL-01 | — | windowing boundaries incl. trailing-partial drop (<15 s) | unit | `uv run pytest tests/test_services/test_analysis.py -k window_boundaries` | ❌ W0 | ⬜ pending |
| TBD | — | — | ANL-01 | — | aggregate reductions (median/modal/dominant/mean) | unit | `uv run pytest tests/test_services/test_analysis.py -k aggregate` | ❌ W0 | ⬜ pending |
| TBD | — | — | ANL-01 | — | per-window failure isolation (one raises → others survive) | unit | `uv run pytest tests/test_services/test_analysis.py -k failure_isolation` | ❌ W0 | ⬜ pending |
| TBD | — | — | (new) | — | `AnalysisWindowPayload` (de)serialization round-trip | unit | `uv run pytest tests/test_schemas/test_agent_analysis.py -k window` | ⚠️ extend | ⬜ pending |
| TBD | — | — | (new) | — | `put_analysis` idempotency: re-PUT replaces, not duplicates, child rows | integration | `uv run pytest tests/test_routers -k analysis_window_idempotent -m integration` | ❌ W0 | ⬜ pending |
| TBD | — | — | ANL-01 | — | ≥2 h synthetic file completes without crash/unbounded memory | integration | `uv run pytest tests -k long_file_bounded -m integration` | ❌ W0 | ⬜ pending |
| TBD | — | — | ANL-01 | — | short real fixture → expected window counts + aggregates | integration | `uv run pytest tests -k real_fixture_windows -m integration` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky · Task IDs finalized by planner against PLAN.md.*

---

## Wave 0 Requirements

- [ ] Window-boundary + trailing-partial-drop unit tests (`tests/test_services/test_analysis.py`)
- [ ] Aggregate-reduction unit tests — pure-Python, **no essentia mock needed** (cheapest high-value coverage)
- [ ] Per-window failure-isolation unit test
- [ ] `analysis_window` idempotency integration test (delete-then-insert)
- [ ] ≥2 h synthetic-file bounded-memory integration test (mark `integration`, may be slow)
- [ ] `AnalysisWindowPayload` schema round-trip (extend `tests/test_schemas/test_agent_analysis.py`)
- [ ] Spike script (throwaway, **not committed** to `tests/`) — see Spike Design below

> Existing `tests/test_services/test_analysis.py` mocks `essentia` (`@patch ... mock_es`). New unit tests should mock `EasyLoader`/`RhythmExtractor2013`/`KeyExtractor` the same way.

---

## Spike Design (mandatory first plan task)

Throwaway script (e.g. `scripts/spike_windowed_analysis.py`, run via `uv run`, **not committed**):

| Validates | Method | Pass/Fail Threshold |
|-----------|--------|---------------------|
| (a) per-window decode works | `EasyLoader` loop over a real ≥2 h file at 44.1k | All windows decode; no exception |
| (b) `RhythmExtractor2013` on 30 s buffer | run on each fine window | No `OnsetDetectionGlobal` overflow; BPM returned (conf may be 0 on silence) |
| (c) bounded memory | sample RSS (`resource.getrusage`) every N windows over full file | Peak RSS roughly flat (< 1.5 GB); does NOT grow with `window_index` |
| (d) coarse TF inference time | time 34-model pass per 180 s window; extrapolate to full file | Acceptable for 8× concurrency (record sec/hour-of-audio) |
| (A1) seek cost | log per-window decode time vs `window_index` | Roughly constant (non-quadratic). If linear growth → choose decode+Resample hybrid |

> The spike answers Assumptions A1/A2 with real numbers; paste its output into the plan's decision log to lock the EasyLoader-vs-hybrid choice.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real ≥2 h file bounded memory + acceptable wall time | ANL-01 | Requires a real multi-hour archive file unavailable in CI fixtures | Run spike script against a real 2 h set on the homelab; confirm RSS flat + acceptable per-file time |
| Review-UI sparkline + expandable timeline renders correctly | ANL-01 | Visual/HTMX interaction | Load review list, confirm sparkline; click expand, confirm multi-lane timeline fragment loads |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
