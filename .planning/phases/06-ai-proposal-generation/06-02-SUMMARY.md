---
phase: 06-ai-proposal-generation
plan: 02
subsystem: ai
tags: [litellm, arq, redis, rate-limiting, structured-output, proposal-storage]

# Dependency graph
requires:
  - phase: 06-ai-proposal-generation
    plan: 01
    provides: FileProposalResponse, BatchProposalResponse, load_prompt_template, clean_companion_content, build_file_context, Settings LLM fields
  - phase: 04-task-queue-worker-infrastructure
    provides: arq worker patterns, WorkerSettings, process_file, _get_session
provides:
  - ProposalService class with generate_batch calling litellm acompletion
  - Redis-based rate limiting with INCR/EXPIRE pattern
  - store_proposals creating immutable RenameProposal records with context_used JSONB
  - load_companion_contents for reading and cleaning companion files
  - generate_proposals arq batch job function
  - WorkerSettings wired with generate_proposals and ProposalService startup
affects: [admin-ui, approval-workflow]

# Tech tracking
tech-stack:
  added: []
  patterns: [Redis INCR/EXPIRE rate limiting, litellm acompletion with response_format for structured output, immutable proposal records with context_used JSONB]

key-files:
  created:
    - src/phaze/tasks/proposal.py
    - tests/test_tasks/test_proposal.py
  modified:
    - src/phaze/services/proposal.py
    - src/phaze/tasks/worker.py
    - tests/test_services/test_proposal.py

key-decisions:
  - "FileRecord moved from TYPE_CHECKING to runtime import in proposal service since store_proposals and load_companion_contents use it in select() queries"
  - "AsyncSession moved to TYPE_CHECKING since from __future__ import annotations makes it work for type hints"
  - "arq provides ctx[redis] automatically as ArqRedis -- no extra pool creation needed"
  - "Retry backoff uses job_try * 10 seconds (10s, 20s, 30s) for LLM failures"

patterns-established:
  - "Redis rate limiting: INCR key, set 60s EXPIRE on count==1, DECR and sleep(2s) when over limit"
  - "Immutable proposals: RenameProposal records store context_used JSONB with all extracted metadata plus input_context"
  - "Batch job pattern: load file context -> rate limit -> call LLM -> store proposals -> commit"

requirements-completed: [AIP-01, AIP-02]

# Metrics
duration: 6min
completed: 2026-03-28
---

# Phase 6 Plan 02: Proposal Service and Batch Job Summary

**ProposalService calling litellm acompletion with structured output, Redis rate limiting with configurable RPM, immutable proposal storage, and generate_proposals arq batch job wired into WorkerSettings**

## Performance

- **Duration:** 6 min
- **Started:** 2026-03-28T22:58:14Z
- **Completed:** 2026-03-28T23:04:14Z
- **Tasks:** 2 (TDD: RED + GREEN each)
- **Files modified:** 5

## Accomplishments
- ProposalService with generate_batch calling litellm acompletion with response_format=BatchProposalResponse for structured LLM output
- Redis-based rate limiting using INCR/EXPIRE pattern with configurable max RPM and 2-second backoff when over limit
- store_proposals creates immutable RenameProposal records with context_used JSONB containing extracted metadata and input context, transitions file state to PROPOSAL_GENERATED
- load_companion_contents queries FileCompanion join table, reads files from disk, cleans content via clean_companion_content
- generate_proposals arq batch job orchestrates the full pipeline: load context, rate limit, call LLM, store proposals, retry with backoff on failure
- WorkerSettings updated with generate_proposals in functions list and ProposalService initialized in startup hook
- 40 tests passing across service and task test suites

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: Failing tests for ProposalService** - `5b38c96` (test)
2. **Task 1 GREEN: ProposalService implementation** - `6a6e733` (feat)
3. **Task 2 RED: Failing tests for generate_proposals** - `87c5f79` (test)
4. **Task 2 GREEN: generate_proposals and WorkerSettings wiring** - `89c68af` (feat)

_Note: TDD tasks have RED + GREEN commits each._

## Files Created/Modified
- `src/phaze/services/proposal.py` - Added ProposalService class, check_rate_limit, store_proposals, load_companion_contents
- `src/phaze/tasks/proposal.py` - New arq batch job function generate_proposals
- `src/phaze/tasks/worker.py` - Updated WorkerSettings with generate_proposals, startup with ProposalService init
- `tests/test_services/test_proposal.py` - Extended with 17 new tests (34 total) for service layer
- `tests/test_tasks/test_proposal.py` - New test file with 6 tests for arq job and worker wiring

## Decisions Made
- FileRecord moved from TYPE_CHECKING to runtime import in proposal service since store_proposals and load_companion_contents need it in SQLAlchemy select() queries
- AsyncSession moved to TYPE_CHECKING block since `from __future__ import annotations` allows annotation-only usage
- arq provides `ctx["redis"]` automatically as ArqRedis instance -- no extra pool creation needed in startup
- Retry backoff for LLM failures uses `job_try * 10` seconds (10s, 20s, 30s) -- slower backoff than analysis (job_try * 5) since LLM rate limits are more likely to need longer waits

## Deviations from Plan

None - plan executed exactly as written.

## Known Stubs

None - all functions are fully implemented with real logic, not placeholders.

## Issues Encountered

None.

## User Setup Required

None - API keys (ANTHROPIC_API_KEY) will be needed at runtime but are already configured as optional Settings fields from Plan 01.

## Next Phase Readiness
- Full proposal generation pipeline is operational: arq job -> ProposalService -> litellm -> immutable DB records
- Ready for admin UI integration to display and approve/reject proposals
- Rate limiting prevents exceeding configured LLM RPM via Redis counter

---
*Phase: 06-ai-proposal-generation*
*Completed: 2026-03-28*
