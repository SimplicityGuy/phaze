---
status: partial
phase: 90-destructive-migration-writer-removal
source: [90-VERIFICATION.md]
started: 2026-07-13T06:00:54Z
updated: 2026-07-13T06:00:54Z
---

## Current Test

[awaiting human/operator testing]

## Tests

### 1. Migration 039 rehearsal against a real-corpus restore (ROADMAP success-criterion 3)
expected: Restore a real prod snapshot → apply migrations 032→038 → run shadow-compare green on the DRAINED live corpus (`--profile drain`) → run 039 → assert `files_state_archive` populated (one row/file) and `files.state`/`ix_files_state` gone → `downgrade()` → assert verbatim restore of durable states → record ACCESS EXCLUSIVE lock-acquisition + DDL timing. Deploy order: through 038 → shadow-compare green (drained) → THEN 039; 039 is never prod's first migration (prod is at Alembic 031). This is the intentional pre-DEPLOY operator gate — documented in 90-03-PLAN.md's <verification> block and 039's downgrade() docstring; execution is manual, not required for phase code-completion.
result: [pending]

## Summary

total: 1
passed: 0
issues: 0
pending: 1
skipped: 0
blocked: 0

## Gaps
