# Project Retrospective

*A living document updated after each milestone. Lessons feed forward into future planning.*

## Milestone: v1.0 — MVP

**Shipped:** 2026-03-30
**Phases:** 11 | **Plans:** 24 | **Tasks:** 43

### What Was Built
- Full music collection pipeline: scan -> analyze -> propose -> approve -> execute
- Docker Compose stack with FastAPI API, arq workers, PostgreSQL, Redis
- Audio analysis via essentia-tensorflow (34 models for BPM, key, mood, style)
- LLM-powered filename proposals via litellm with batch processing
- Admin web UI (HTMX + Tailwind) with approve/reject, bulk actions, keyboard shortcuts, SSE progress
- Copy-verify-delete execution with append-only audit log
- 282 tests, 7,975 lines of Python

### What Worked
- GSD workflow kept 11 phases organized with clear planning -> execution -> verification gates
- Parallel executor agents (worktree isolation) cut execution time significantly for independent plans
- TDD approach caught integration issues early (e.g., MagicMock truthiness bug in execution tests)
- Milestone audit after Phase 8 surfaced real integration gaps (scan->analyze, analyze->propose triggers) that Phase 9 closed
- Phase branching with PRs kept main clean and provided review checkpoints
- Pre-commit hooks with frozen SHAs caught formatting/linting issues before they accumulated

### What Was Inefficient
- Phase 10 and 11 were gap-closure phases created after the audit — earlier integration testing could have caught these during Phase 8/9
- Some VERIFICATION.md files showed gaps_found but the gaps were already closed by successor phases — verification status should update automatically
- SUMMARY frontmatter requirements-completed fields were inconsistently populated across early phases — establishing the convention earlier would have avoided Phase 11 cleanup
- Phase 10 VERIFICATION gaps (config.json EOF, INF-03 checkbox) were trivial items that shouldn't have required a separate phase

### Patterns Established
- justfile as command runner for all dev tasks (replicated in CI via `just` delegation)
- Phase branching strategy with PRs per phase for code review
- Nyquist validation (VALIDATION.md) for test coverage verification per phase
- 3-source cross-reference for requirements (VERIFICATION + SUMMARY + REQUIREMENTS.md)
- Milestone audit before completion to surface tech debt early

### Key Lessons
1. Run integration checks after every pipeline-connecting phase, not just at milestone end
2. Establish SUMMARY frontmatter conventions (requirements-completed, tech-stack) from Phase 1
3. Trivial doc fixes should be batched into the phase that creates them, not deferred to cleanup phases
4. The milestone audit -> gap closure -> re-audit cycle is effective but adds 2-3 phases — bake integration testing into earlier phases to reduce this
5. Pre-commit hook validation in CI (via pre-commit/action) is more reliable than manual runs

### Cost Observations
- Model mix: ~60% opus (execution), ~30% sonnet (verification, integration checks), ~10% haiku (quick lookups)
- Notable: Parallel worktree agents are the most token-efficient approach for independent plans — each gets a fresh context window without polluting the orchestrator

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Phases | Plans | Key Change |
|-----------|--------|-------|------------|
| v1.0 | 11 | 24 | Established GSD workflow, branching strategy, Nyquist validation |

### Cumulative Quality

| Milestone | Tests | LOC | Phases |
|-----------|-------|-----|--------|
| v1.0 | 282 | 7,975 | 11 |

### Top Lessons (Verified Across Milestones)

1. Integration testing at pipeline boundaries catches gaps that unit tests miss
2. Documentation conventions established early save cleanup phases later
