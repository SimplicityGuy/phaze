---
phase: 06-ai-proposal-generation
verified: 2026-03-28T23:15:00Z
status: passed
score: 9/9 must-haves verified
re_verification: false
gaps: []
human_verification:
  - test: "Submit a real LLM call via arq job against a live Anthropic API key"
    expected: "BatchProposalResponse parsed from LLM, RenameProposal records written to DB, file state transitions to PROPOSAL_GENERATED"
    why_human: "Requires live Anthropic API key, running Redis, and running PostgreSQL — cannot exercise end-to-end LLM path programmatically without infrastructure"
---

# Phase 6: AI Proposal Generation Verification Report

**Phase Goal:** The system uses an LLM to propose new filenames for files, storing proposals as immutable records
**Verified:** 2026-03-28T23:15:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (from ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | The system sends file metadata, analysis results, and companion file content to an LLM and receives proposed filenames | VERIFIED | `ProposalService.generate_batch` calls `litellm.acompletion` with `response_format=BatchProposalResponse`; `build_file_context` assembles metadata+analysis+companions into the prompt via `{files_json}` substitution |
| 2 | Each proposal is stored as an immutable record in PostgreSQL (not regenerated on the fly) | VERIFIED | `store_proposals` creates `RenameProposal` records with `status=ProposalStatus.PENDING`; no UPDATE path exists; model lacks any regeneration trigger |
| 3 | Proposals include the original filename, proposed filename, and the metadata context used to generate them | VERIFIED | `RenameProposal` stores `proposed_filename`, `context_used` JSONB contains `input_context` (the full file context dict with `original_filename`) plus all extracted metadata fields |
| 4 | Batch prompting processes multiple files per LLM call for cost efficiency | VERIFIED | `generate_proposals` arq job accepts `file_ids: list[str]` and assembles `files_context` list before a single `generate_batch` call; `llm_batch_size: int = 10` in Settings |

**Plan 01 additional truths:**

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 5 | litellm is installed and version-pinned to >=1.82.6,<1.82.7 | VERIFIED | `pyproject.toml`: `"litellm>=1.82.6,<1.82.7"` |
| 6 | LLM configuration is available via environment variables | VERIFIED | Settings has `llm_model`, `anthropic_api_key`, `openai_api_key`, `llm_max_rpm`, `llm_batch_size`, `llm_max_companion_chars` — all pydantic-settings fields loadable from env |
| 7 | Prompt template exists as a markdown file loadable at runtime | VERIFIED | `src/phaze/prompts/naming.md` (93 lines); `load_prompt_template()` reads from `Path(__file__).parent.parent / "prompts" / f"{name}.md"` |
| 8 | Pydantic response models validate LLM structured output | VERIFIED | `FileProposalResponse` and `BatchProposalResponse` defined with no `Field(ge=, le=)` constraints (Anthropic compatibility preserved) |
| 9 | Rate limiting prevents exceeding configured RPM via Redis counter | VERIFIED | `check_rate_limit` uses Redis `INCR`/`EXPIRE(60)`/`DECR`+`sleep(2.0)` pattern keyed at `"phaze:llm:rpm"` |

**Score:** 9/9 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `pyproject.toml` | litellm dependency | VERIFIED | `"litellm>=1.82.6,<1.82.7"` present |
| `src/phaze/config.py` | LLM configuration fields | VERIFIED | `llm_model`, `anthropic_api_key`, `llm_max_rpm`, `llm_batch_size`, `llm_max_companion_chars` — single `openai_api_key` field, no duplicate |
| `src/phaze/services/proposal.py` | Pydantic response models, prompt loader, companion cleaner, context builder, ProposalService, rate limiter, proposal storage | VERIFIED | 333 lines, all exports present and substantive |
| `src/phaze/prompts/naming.md` | Prompt template with naming rules | VERIFIED | 93 lines (minimum 40); contains `{files_json}`, `YYYY.MM.DD`, `Live @`, confidence guidance, metadata extraction instructions |
| `src/phaze/prompts/__init__.py` | Package marker | VERIFIED | Exists, 0 lines (correct for a package init) |
| `src/phaze/tasks/proposal.py` | generate_proposals arq job function | VERIFIED | 92 lines; all required patterns present |
| `src/phaze/tasks/worker.py` | WorkerSettings updated with generate_proposals and ProposalService startup | VERIFIED | `generate_proposals` in `functions` list; `ProposalService` instantiated in `startup` |
| `tests/test_services/test_proposal.py` | Unit tests for all service layer components | VERIFIED | 645 lines (minimum 60); 34 tests covering all behaviors |
| `tests/test_tasks/test_proposal.py` | Unit tests for generate_proposals arq job | VERIFIED | 208 lines (minimum 40); 6 tests covering happy path, empty, retry, rate limit, worker wiring |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/phaze/services/proposal.py` | `src/phaze/prompts/naming.md` | `load_prompt_template` reads from `prompts/` directory | WIRED | `_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"` — path resolves to `src/phaze/prompts/`; test confirms file loads and returns `{files_json}` and `YYYY.MM.DD` |
| `src/phaze/services/proposal.py` | `src/phaze/config.py` | imports Settings fields for LLM config | WIRED | `from phaze.config import settings` confirmed in `src/phaze/tasks/proposal.py`; `proposal.py` service uses config indirectly via caller |
| `src/phaze/tasks/proposal.py` | `src/phaze/services/proposal.py` | `generate_proposals` calls `ProposalService.generate_batch` | WIRED | `proposal_service: ProposalService = ctx["proposal_service"]` then `await proposal_service.generate_batch(files_context)` |
| `src/phaze/services/proposal.py` | litellm | `acompletion()` for async LLM calls | WIRED | `from litellm import acompletion`; used in `generate_batch` as `await acompletion(model=..., response_format=BatchProposalResponse)` |
| `src/phaze/services/proposal.py` | `src/phaze/models/proposal.py` | Creates `RenameProposal` records | WIRED | `from phaze.models.proposal import ProposalStatus, RenameProposal`; `RenameProposal(...)` instantiated in `store_proposals` |
| `src/phaze/tasks/worker.py` | `src/phaze/tasks/proposal.py` | `WorkerSettings.functions` includes `generate_proposals` | WIRED | `from phaze.tasks.proposal import generate_proposals`; `functions: ClassVar[list[Any]] = [process_file, generate_proposals]` |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `generate_proposals` arq job | `files_context` | SQLAlchemy `select(FileRecord)` + `select(AnalysisResult)` queries | Yes — queries DB by UUID, falls back gracefully when no record found | FLOWING |
| `store_proposals` | `RenameProposal` records | `batch_response.proposals` from LLM response | Yes — iterates LLM proposals, creates one `RenameProposal` per proposal | FLOWING |
| `check_rate_limit` | Redis `incr` count | Live Redis `INCR` on `"phaze:llm:rpm"` key | Yes — reads and mutates live Redis counter; 60s TTL set on first increment | FLOWING |
| `load_companion_contents` | companion file text | `select(FileCompanion)` + `Path.read_text` | Yes — queries join table, reads files from disk; OSError skipped gracefully | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| `ProposalService.generate_batch` calls `acompletion` with `response_format=BatchProposalResponse` | `uv run pytest tests/test_services/test_proposal.py::TestGenerateBatch -v` | PASSED | PASS |
| `check_rate_limit` blocks when over RPM then retries | `uv run pytest tests/test_services/test_proposal.py::TestCheckRateLimit -v` | PASSED | PASS |
| `store_proposals` creates immutable records with clamped confidence and context_used JSONB | `uv run pytest tests/test_services/test_proposal.py::TestStoreProposals -v` | PASSED | PASS |
| `generate_proposals` arq job full pipeline with retry-on-failure | `uv run pytest tests/test_tasks/test_proposal.py -v` | PASSED (6/6) | PASS |
| `WorkerSettings.functions` contains `generate_proposals` | `uv run pytest tests/test_tasks/test_proposal.py::test_worker_settings_contains_generate_proposals` | PASSED | PASS |
| Full test suite coverage | `uv run pytest --cov` | 92.88% total (threshold: 85%) | PASS |
| Ruff lint clean | `uv run ruff check src/phaze/services/proposal.py src/phaze/tasks/proposal.py ...` | No issues | PASS |
| Mypy type check clean | `uv run mypy src/phaze/services/proposal.py src/phaze/tasks/proposal.py ...` | No issues found | PASS |

All 40 tests pass across both test suites (34 service tests + 6 task tests).

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| AIP-01 | 06-01-PLAN, 06-02-PLAN | System uses LLM to propose a new filename for each file based on available metadata, analysis results, and companion file content where available | SATISFIED | `ProposalService.generate_batch` sends `build_file_context` dicts (containing `original_filename`, `analysis`, `companions`) to litellm `acompletion`; `BatchProposalResponse` is parsed from response |
| AIP-02 | 06-02-PLAN | Proposals are stored as immutable records in PostgreSQL (not regenerated on the fly) | SATISFIED | `store_proposals` creates `RenameProposal` ORM records with `status=ProposalStatus.PENDING`; no update or regeneration path exists; `context_used` JSONB stores full input context as part of the immutable record |

No orphaned requirements — AIP-01 and AIP-02 are the only requirements mapped to Phase 6 in REQUIREMENTS.md, and both are claimed in plan frontmatter.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/phaze/services/proposal.py` | 275 | `session.add(record)` where `record` is a `RenameProposal` mock — test warning about unawaited coroutine | Info | Test warning only; not a production code issue. Mock `session.add` is synchronous in SQLAlchemy but the test mock generates a coroutine. No production impact. |

No blockers. No stubs. No placeholder implementations. No hardcoded empty returns in production paths. The three RuntimeWarnings in `store_proposals` tests are test infrastructure artifacts (mock `session.add` returning a coroutine), not production code defects.

### Human Verification Required

#### 1. End-to-End LLM Call Through Live Infrastructure

**Test:** Set `ANTHROPIC_API_KEY` in environment, start Docker Compose (PostgreSQL + Redis + worker), ingest a test music file, enqueue a `generate_proposals` job via arq with the file's UUID, wait for completion.
**Expected:** `RenameProposal` record created in `proposals` table with `status=pending`, `proposed_filename` reflecting the file's name, `context_used` containing the extracted metadata fields, file's `state` in `files` table updated to `proposal_generated`.
**Why human:** Requires live Anthropic API key, running Redis, and running PostgreSQL containers — cannot exercise the real `acompletion` call or verify actual DB record creation without the full infrastructure stack.

### Gaps Summary

No gaps. All automated checks pass. The phase goal is fully achieved: the system has a working LLM-powered filename proposal pipeline, proposals are stored as immutable `RenameProposal` records with full `context_used` JSONB, rate limiting is enforced via Redis, and the arq batch job is wired into `WorkerSettings`. The only item routed to human verification is the live end-to-end test requiring infrastructure.

---

_Verified: 2026-03-28T23:15:00Z_
_Verifier: Claude (gsd-verifier)_
