---
status: partial
phase: 61-full-record-k-agents
source: [61-VERIFICATION.md]
started: 2026-07-01T23:55:00Z
updated: 2026-07-01T23:55:00Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. CR-01 — record slide-in opens on a fresh direct load
expected: On a fresh direct `GET /` (Analyze default, NOT after a rail swap), clicking an Analyze file row opens the right-anchored full-record slide-in. The `x-data` wrapper fix is present in the HTML; only a browser exercises Alpine's init timing.
result: [pending]

### 2. Focus-trap — ⌘K palette
expected: Opening ⌘K traps Tab focus inside the palette; Esc closes it and returns focus to `#cmdk-trigger`.
result: [pending]

### 3. Focus-trap — record slide-in
expected: With the record panel open, Tab stays inside the panel; Esc / ✕ / backdrop click closes it and returns focus to the opening row.
result: [pending]

### 4. WR-01 — friendly 404 fragment for a deleted file
expected: Clicking a row whose file was de-duplicated/removed shows the "file no longer exists" friendly fragment inside the panel (the `htmx:beforeSwap` handler opts the 404 body into the swap for `#record-body`). Confirm via browser Network + panel content.
result: [pending]

### 5. Empty state — live scan progress on the single poll
expected: On a first-run (0 files) home/Analyze, clicking "Scan {agent}" shows live progress via the existing `/pipeline/stats` poll with NO second request loop in the Network tab.
result: [pending]

## Summary

total: 5
passed: 0
issues: 0
pending: 5
skipped: 0
blocked: 0

## Gaps
