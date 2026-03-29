---
phase: 6
slug: ai-proposal-generation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-28
---

# Phase 6 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio |
| **Config file** | `pyproject.toml` [tool.pytest.ini_options] |
| **Quick run command** | `uv run pytest tests/test_services/test_proposal.py tests/test_tasks/test_generate.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_services/test_proposal.py tests/test_tasks/test_generate.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 06-01-01 | 01 | 1 | AIP-01 | unit (mock litellm) | `uv run pytest tests/test_services/test_proposal.py -x` | ❌ W0 | ⬜ pending |
| 06-01-02 | 01 | 1 | AIP-01 | unit | `uv run pytest tests/test_services/test_proposal.py::test_prompt_template -x` | ❌ W0 | ⬜ pending |
| 06-01-03 | 01 | 1 | AIP-01 | unit (mock DB) | `uv run pytest tests/test_services/test_proposal.py::test_build_context -x` | ❌ W0 | ⬜ pending |
| 06-01-04 | 01 | 1 | AIP-02 | unit (mock DB) | `uv run pytest tests/test_services/test_proposal.py::test_store_proposal -x` | ❌ W0 | ⬜ pending |
| 06-01-05 | 01 | 1 | AIP-02 | unit (mock DB) | `uv run pytest tests/test_services/test_proposal.py::test_state_transition -x` | ❌ W0 | ⬜ pending |
| 06-01-06 | 01 | 1 | AIP-01 | unit (mock Redis) | `uv run pytest tests/test_services/test_proposal.py::test_rate_limit -x` | ❌ W0 | ⬜ pending |
| 06-02-01 | 02 | 2 | AIP-01 | unit (mock all) | `uv run pytest tests/test_tasks/test_generate.py -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_services/test_proposal.py` — covers AIP-01, AIP-02 (proposal service unit tests)
- [ ] `tests/test_tasks/test_generate.py` — covers AIP-01 batch job integration
- [ ] Mock strategy: mock litellm `acompletion()` at the service boundary so tests don't need real API keys or LLM calls

*Testing strategy note: litellm calls are mocked — no real LLM API calls in unit tests. Integration tests that actually call the LLM can be marked `@pytest.mark.slow` and skipped in CI.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| LLM produces sensible filenames for real files | AIP-01 | Requires actual LLM API call + human judgement on naming quality | Run `generate_proposals` on a sample batch and review proposals in DB |
| Prompt template markdown renders naming rules correctly | AIP-01 | Template authoring quality is subjective | Read the rendered prompt and verify it matches naming decisions from CONTEXT.md |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
