---
phase: 06-ai-proposal-generation
plan: 01
subsystem: ai
tags: [litellm, pydantic, llm, prompt-template, structured-output]

# Dependency graph
requires:
  - phase: 04-task-queue-worker-infrastructure
    provides: arq worker patterns, Settings with pydantic-settings
  - phase: 05-audio-analysis-pipeline
    provides: AnalysisResult model with BPM, mood, style, key, features
provides:
  - litellm dependency installed and version-pinned
  - LLM configuration fields in Settings (model, API keys, rate limits, batch size)
  - FileProposalResponse and BatchProposalResponse Pydantic models for structured LLM output
  - Naming prompt template with live set and album track format rules
  - clean_companion_content helper for stripping ASCII art and truncating NFO files
  - build_file_context helper for assembling per-file LLM context dicts
  - load_prompt_template helper for loading markdown prompts from disk
affects: [06-02-PLAN, ai-proposal-generation]

# Tech tracking
tech-stack:
  added: [litellm>=1.82.6,<1.82.7]
  patterns: [TYPE_CHECKING imports for ORM models in service layer, prompt-as-markdown-file, Pydantic models without numeric Field constraints for Anthropic compatibility]

key-files:
  created:
    - src/phaze/services/proposal.py
    - src/phaze/prompts/naming.md
    - src/phaze/prompts/__init__.py
    - tests/test_services/test_proposal.py
  modified:
    - pyproject.toml
    - src/phaze/config.py

key-decisions:
  - "No Field(ge=, le=) constraints on confidence float due to litellm Anthropic bug (GitHub #21016)"
  - "Prompt template stored as markdown file at src/phaze/prompts/naming.md loaded at runtime"
  - "Companion content truncated to 3000 chars with ASCII art stripping"
  - "Default LLM model set to claude-sonnet-4-20250514"

patterns-established:
  - "Prompt-as-markdown: prompt templates stored in src/phaze/prompts/ as .md files, loaded via load_prompt_template()"
  - "Pydantic response models without numeric constraints for Anthropic compatibility"
  - "TYPE_CHECKING block for ORM model imports in service modules"

requirements-completed: [AIP-01]

# Metrics
duration: 5min
completed: 2026-03-28
---

# Phase 6 Plan 01: LLM Contracts and Data Structures Summary

**litellm dependency pinned, Settings extended with 5 LLM config fields, Pydantic response models for structured output, naming prompt template with live set and album track rules, and companion cleaning + context building helpers tested**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-28T22:47:39Z
- **Completed:** 2026-03-28T22:52:39Z
- **Tasks:** 1 (TDD: RED + GREEN + REFACTOR)
- **Files modified:** 6

## Accomplishments
- litellm >=1.82.6,<1.82.7 added to pyproject.toml and installed via uv sync
- Settings extended with llm_model, anthropic_api_key, llm_max_rpm, llm_batch_size, llm_max_companion_chars
- FileProposalResponse and BatchProposalResponse Pydantic models defined (no Field constraints per Pitfall 2)
- Naming prompt template at src/phaze/prompts/naming.md with live set format, album track format, date rules, confidence guidance, metadata extraction instructions, and {files_json} placeholder
- clean_companion_content() strips ASCII art and truncates to 3000 chars
- build_file_context() assembles FileRecord + AnalysisResult + companions into LLM-ready dict
- 17 tests passing, ruff clean, mypy clean

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: Failing tests** - `54df1f4` (test)
2. **Task 1 GREEN: Implementation** - `aff2f82` (feat)
3. **Task 1 REFACTOR: Clean up test imports** - `48a3726` (refactor)

## Files Created/Modified
- `pyproject.toml` - Added litellm>=1.82.6,<1.82.7 dependency
- `src/phaze/config.py` - Extended Settings with 5 LLM configuration fields
- `src/phaze/services/proposal.py` - FileProposalResponse, BatchProposalResponse, load_prompt_template, clean_companion_content, build_file_context
- `src/phaze/prompts/__init__.py` - Package init for prompts directory
- `src/phaze/prompts/naming.md` - Naming prompt template with format rules and {files_json} placeholder
- `tests/test_services/test_proposal.py` - 17 unit tests for all models and helpers

## Decisions Made
- No Field(ge=, le=) constraints on confidence float due to litellm Anthropic bug (GitHub #21016) -- post-parse clamping should be applied by the caller in Plan 02
- Prompt template stored as markdown file at src/phaze/prompts/naming.md, loaded at runtime via load_prompt_template()
- Companion content truncated to 3000 chars max with ASCII art line stripping (regex matching 10+ repeated non-alphanumeric chars)
- Default LLM model set to claude-sonnet-4-20250514 (configurable via LLM_MODEL env var)
- ORM model imports placed in TYPE_CHECKING block to avoid import-time side effects in service layer

## Deviations from Plan

None - plan executed exactly as written.

## Known Stubs

None - all functions are fully implemented with real logic, not placeholders.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required for this plan. API keys will be needed when Plan 02 implements actual LLM calling.

## Next Phase Readiness
- All contracts and data structures are defined for Plan 02 to implement the actual LLM calling, batch processing, and proposal storage
- FileProposalResponse and BatchProposalResponse are importable from phaze.services.proposal
- Prompt template is on disk and loadable
- Settings has all required LLM config fields

---
*Phase: 06-ai-proposal-generation*
*Completed: 2026-03-28*
