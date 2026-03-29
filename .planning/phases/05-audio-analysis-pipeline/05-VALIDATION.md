---
phase: 5
slug: audio-analysis-pipeline
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-28
validated: 2026-03-28
---

# Phase 5 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio |
| **Config file** | `pyproject.toml` [tool.pytest.ini_options] |
| **Quick run command** | `uv run pytest tests/test_services/test_analysis.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_services/test_analysis.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 05-01-01 | 01 | 1 | ANL-01 | unit (mock essentia) | `uv run pytest tests/test_services/test_analysis.py::test_detect_bpm -x` | ✅ | ✅ green |
| 05-01-02 | 01 | 1 | ANL-01 | unit (DB) | `uv run pytest tests/test_services/test_analysis.py::test_bpm_stored -x` | ✅ | ✅ green |
| 05-01-03 | 01 | 1 | ANL-02 | unit (mock essentia) | `uv run pytest tests/test_services/test_analysis.py::test_classify_mood -x` | ✅ | ✅ green |
| 05-01-04 | 01 | 1 | ANL-02 | unit (mock essentia) | `uv run pytest tests/test_services/test_analysis.py::test_classify_style -x` | ✅ | ✅ green |
| 05-01-05 | 01 | 1 | ANL-02 | unit (DB) | `uv run pytest tests/test_services/test_analysis.py::test_analysis_result_stored -x` | ✅ | ✅ green |
| 05-02-01 | 02 | 2 | ANL-01+02 | unit (mock) | `uv run pytest tests/test_tasks/test_functions.py::test_process_file_analysis -x` | ✅ | ✅ green |
| 05-02-02 | 02 | 2 | ANL-01+02 | unit | `uv run pytest tests/test_tasks/test_functions.py::test_process_file_retry -x` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/test_services/test_analysis.py` — stubs for ANL-01, ANL-02 (analysis service unit tests)
- [x] Update `tests/test_tasks/test_functions.py` — update process_file tests for real analysis logic
- [x] Mock strategy: mock at the `analyze_file` boundary (the sync function passed to `run_in_process_pool`) so tests don't need actual model files or essentia installed

*Testing strategy note: essentia-tensorflow is 291MB with native C++ extensions. Unit tests mock at the analysis function boundary. Integration tests marked `@pytest.mark.slow` and skipped in CI unless model files are available.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Model files download correctly | ANL-01+02 | Requires network + 200-300MB download | Run `uv run python -m phaze.services.models download` and verify files exist |
| Actual BPM/mood accuracy on real audio | ANL-01+02 | Requires real audio files + essentia installed | Run analysis on a known file and check results match prototype output |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 30s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** nyquist-auditor validated 2026-03-28 — 7/7 tasks green
