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

## Milestone: v2.0 — Metadata Enrichment & Tracklist Integration

**Shipped:** 2026-04-02
**Phases:** 6 | **Plans:** 16 | **Tasks:** 31

### What Was Built
- Audio tag extraction (mutagen) populating FileMetadata with artist, title, album, year, genre, track number, duration, bitrate, raw JSONB dump
- AI destination path proposals with collision detection, directory tree preview, and execution gate
- Duplicate resolution UI with auto-scoring (bitrate > tags > path), side-by-side comparison, resolve/undo workflow
- 1001Tracklists integration: async scraper, fuzzy matcher (rapidfuzz), monthly refresh cron
- Dual fingerprint service (audfprint + Panako) as Docker containers with HTTP APIs and batch ingestion
- Live set scanning with tracklist review: inline editing, approve/reject, bulk reject, confidence badges

### What Worked
- Milestone audit after all phases caught only cosmetic/process tech debt — no functional gaps, proving v1.0 lesson about integration testing paid off
- Phase branching with PRs continued to keep main clean (PRs #16-#22)
- Research phases before planning (especially Phase 16 fingerprint architecture) prevented major rework
- HTMX + server-rendered templates kept UI delivery fast without frontend build complexity
- Parallel-capable phases (13, 14, 15 all depend only on 12) gave scheduling flexibility

### What Was Inefficient
- Phase 12 REQUIREMENTS.md checkboxes got lost during branch merge — needed manual sync in tech debt cleanup
- Nyquist VALIDATION.md frontmatter was never toggled to `true` after execution across all 6 phases — process step consistently skipped
- Phase 12 showed as "Not started" in ROADMAP.md progress table despite being complete — merge artifact from phase branch
- Some MILESTONES.md accomplishments are raw summary one-liners rather than curated highlights

### Patterns Established
- Dual fingerprint engine architecture with Protocol-based adapters and weighted orchestrator
- HTMX OOB swaps for inline updates (undo toasts, status transitions) — reusable pattern across all admin pages
- Alpine.js for client-side state that HTMX doesn't handle (filter tabs, radio selection highlighting, scan panel toggle)
- Convergence gate pattern: dual exists() subquery checks before advancing pipeline stage

### Key Lessons
1. Nyquist VALIDATION.md frontmatter finalization should be automated (hook or post-execution step) — manually toggling 6 files is error-prone
2. REQUIREMENTS.md checkbox sync needs to happen on main after PR merge, not just on the phase branch
3. Research phases for unfamiliar domains (fingerprinting, scraping) are high-ROI — Phase 16 research prevented audfprint/Panako integration surprises
4. MILESTONES.md accomplishment extraction should be curated (4-6 highlights), not raw dump of all 16 summary one-liners

### Cost Observations
- Model mix: ~70% opus (execution + planning), ~20% sonnet (verification), ~10% haiku (quick checks)
- Notable: 6 phases in 3 days with 538 tests — velocity improved from v1.0 due to established patterns and infrastructure

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Phases | Plans | Key Change |
|-----------|--------|-------|------------|
| v1.0 | 11 | 24 | Established GSD workflow, branching strategy, Nyquist validation |
| v2.0 | 6 | 16 | Research phases before planning, dual-service architecture, HTMX patterns matured |

### Cumulative Quality

| Milestone | Tests | LOC | Phases |
|-----------|-------|-----|--------|
| v1.0 | 282 | 7,975 | 11 |
| v2.0 | 538 | 5,966 | 6 |

### Top Lessons (Verified Across Milestones)

1. Integration testing at pipeline boundaries catches gaps that unit tests miss (v1.0 audit gaps, v2.0 clean audit)
2. Documentation conventions established early save cleanup phases later (v1.0 SUMMARY frontmatter, v2.0 Nyquist frontmatter)
3. Research phases for unfamiliar domains prevent rework (v2.0 fingerprint architecture research)
