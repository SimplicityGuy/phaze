---
phase: 6
slug: ai-proposal-generation
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-28
audited: 2026-03-29
---

# Phase 6 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio |
| **Config file** | `pyproject.toml` [tool.pytest.ini_options] |
| **Quick run command** | `uv run pytest tests/test_services/test_proposal.py tests/test_tasks/test_proposal.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~3 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_services/test_proposal.py tests/test_tasks/test_proposal.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 06-01-01 | 01 | 1 | AIP-01 | unit (mock litellm) | `uv run pytest tests/test_services/test_proposal.py::TestGenerateBatch -x` | ✅ | ✅ green |
| 06-01-02 | 01 | 1 | AIP-01 | unit | `uv run pytest tests/test_services/test_proposal.py::TestLoadPromptTemplate -x` | ✅ | ✅ green |
| 06-01-03 | 01 | 1 | AIP-01 | unit (mock DB) | `uv run pytest tests/test_services/test_proposal.py::TestBuildFileContext -x` | ✅ | ✅ green |
| 06-01-04 | 01 | 1 | AIP-02 | unit (mock DB) | `uv run pytest tests/test_services/test_proposal.py::TestStoreProposals -x` | ✅ | ✅ green |
| 06-01-05 | 01 | 1 | AIP-02 | unit (mock DB) | `uv run pytest tests/test_services/test_proposal.py::TestStoreProposals::test_creates_rename_proposal_records -x` | ✅ | ✅ green |
| 06-01-06 | 01 | 1 | AIP-01 | unit (mock Redis) | `uv run pytest tests/test_services/test_proposal.py::TestCheckRateLimit -x` | ✅ | ✅ green |
| 06-02-01 | 02 | 2 | AIP-01 | unit (mock all) | `uv run pytest tests/test_tasks/test_proposal.py -x` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/test_services/test_proposal.py` — covers AIP-01, AIP-02 (34 tests for proposal service)
- [x] `tests/test_tasks/test_proposal.py` — covers AIP-01 batch job integration (6 tests)
- [x] Mock strategy: litellm `acompletion()` mocked at service boundary, Redis mocked for rate limiting

*Testing strategy note: litellm calls are mocked — no real LLM API calls in unit tests. Integration tests that actually call the LLM can be marked `@pytest.mark.slow` and skipped in CI.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| LLM produces sensible filenames for real files | AIP-01 | Requires actual LLM API call + human judgement on naming quality | Run `generate_proposals` on a sample batch and review proposals in DB |
| Prompt template markdown renders naming rules correctly | AIP-01 | Template authoring quality is subjective | Read the rendered prompt and verify it matches naming decisions from CONTEXT.md |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 30s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved

---

## Validation Audit 2026-03-29

| Metric | Count |
|--------|-------|
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

All 7 tasks have automated test coverage. 40 tests passing across 2 test files. No gaps detected.
