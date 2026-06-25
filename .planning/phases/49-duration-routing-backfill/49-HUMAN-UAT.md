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

### 2. "Run analysis" with NO agents online surfaces the held count (WR-01, cosmetic)
expected: When BOTH agent kinds are offline and long files are held, the HTMX trigger response ideally shows the awaiting-cloud count. Currently the `{% if no_active_agent %}` branch wins and shows "0 files enqueued"; the Awaiting-cloud card corrects the number within 5s. Confirm this is acceptable, or file WR-01 as a follow-up to surface the held count inline.
result: [pending]

## Summary

total: 2
passed: 0
issues: 0
pending: 2
skipped: 0
blocked: 0

## Gaps
