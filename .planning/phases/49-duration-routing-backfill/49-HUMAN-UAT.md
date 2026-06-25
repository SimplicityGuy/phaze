---
status: partial
phase: 49-duration-routing-backfill
source: [49-VERIFICATION.md]
started: 2026-06-25T00:00:00Z
updated: 2026-06-25T00:00:00Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. "Awaiting cloud" count card updates live on the 5s poll
expected: With files held in AWAITING_CLOUD, the pipeline dashboard's "Awaiting cloud" count card renders the held count and refreshes via the 5s HTMX OOB poll (the count drops as the release cron drains held files once a compute agent is online).
result: [pending]

### 2. "Run analysis" with NO agents online surfaces the held count (WR-01)
expected: When BOTH agent kinds are offline and long files are held, the HTMX trigger response shows the awaiting-cloud count (e.g. "1 held awaiting cloud").
result: RESOLVED — fixed in commit e603c3c; the no-agent branch now renders the held count inline (regression test test_analyze_ui_no_agents_surfaces_held_count). Live-browser confirmation optional.

## Summary

total: 2
passed: 1
issues: 0
pending: 1
skipped: 0
blocked: 0

## Gaps
