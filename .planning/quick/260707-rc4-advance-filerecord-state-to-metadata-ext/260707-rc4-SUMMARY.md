---
phase: quick-260707-rc4
plan: 01
subsystem: agent-internal-api
tags: [file-state-machine, metadata, fingerprint-gate, pipeline-ui]
requires:
  - phaze.models.file.FileRecord
  - phaze.models.file.FileState
provides:
  - "Guarded DISCOVERED -> METADATA_EXTRACTED advance in put_metadata"
affects:
  - fingerprint stage (get_files_by_state gate)
  - pipeline UI (metadata_workspace.html State column)
tech-stack:
  added: []
  patterns:
    - "Guarded single-transaction state advance (mirrors agent_push.py:126 WR-02)"
key-files:
  created: []
  modified:
    - src/phaze/routers/agent_metadata.py
    - tests/metadata/routers/test_agent_metadata.py
decisions:
  - "State advance fires on every success PUT (NOT gated on the dumped dict) so an empty-body success PUT still unblocks the file"
  - "Guard on state == DISCOVERED so a parallel/late fingerprint or analyze callback is never downgraded"
metrics:
  duration: ~15m
  completed: 2026-07-07
  tasks: 2
  files: 2
requirements_completed: [RC4-STATE-ADVANCE]
---

# Phase quick-260707-rc4 Plan 01: Advance FileRecord state to METADATA_EXTRACTED Summary

Metadata PUT now guardedly advances a file DISCOVERED -> METADATA_EXTRACTED in the same transaction as the FileMetadata upsert, unblocking the fingerprint stage and making the pipeline UI reflect reality.

## What Was Done

### Task 1 — Guarded state advance in put_metadata (commit `b9c65660`)
- Added `from sqlalchemy import update` and `from phaze.models.file import FileRecord, FileState` (placed per the project's force-sort-within-sections isort config).
- Inserted a guarded `update(FileRecord).where(FileRecord.id == file_id, FileRecord.state == FileState.DISCOVERED).values(state=FileState.METADATA_EXTRACTED)` between the upsert `session.execute(stmt)` and `clear_ledger_entry` + `session.commit()`, so it runs in the same transaction.
- Guard on `state == FileState.DISCOVERED` mirrors agent_push.py:126 (WR-02): a parallel/late fingerprint or analyze callback that already advanced the file is never downgraded back to METADATA_EXTRACTED.
- Advance is NOT gated on the `dumped` dict — an empty-body success PUT still means extraction ran and must unblock the file.
- Updated the docstring with a "State advance (260707-rc4)" note explaining the guard rationale.

### Task 2 — Regression + guard tests (commit `377a8c16`)
- Extended the existing `tests/metadata/routers/test_agent_metadata.py` (did not create a new module).
- `test_metadata_put_advances_discovered_to_extracted`: seed DISCOVERED file, PUT `{"artist": "A"}`, assert `state == METADATA_EXTRACTED` and the FileMetadata row exists.
- `test_metadata_put_does_not_downgrade_later_state`: seed file, advance to ANALYZED, PUT metadata, assert state stays ANALYZED (not downgraded) and the metadata row is still upserted.
- Added `update` to the existing `from sqlalchemy import select` line for the guard-test state seed.

## Verification
- `uv run ruff check` — clean on both touched files.
- `uv run ruff format --check` — both files already formatted.
- `uv run mypy src/phaze/routers/agent_metadata.py` — no issues.
- `uv run pytest tests/metadata/routers/test_agent_metadata.py -q` — 15 passed (13 existing + 2 new), run against the ephemeral test DB (`just test-db`, ports 5433/6380).

## Deviations from Plan
None — plan executed as written. Note: the plan's verify command was a bare `uv run pytest`, but the DB-backed tests require the ephemeral test Postgres/Redis (`just test-db`); started it, ran the module green, then tore it down (`just test-db-down`). No code deviation.

## Self-Check: PASSED
- FOUND: src/phaze/routers/agent_metadata.py (modified, contains guarded `update(FileRecord)`)
- FOUND: tests/metadata/routers/test_agent_metadata.py (two new tests)
- FOUND commit: b9c65660 (Task 1)
- FOUND commit: 377a8c16 (Task 2)
