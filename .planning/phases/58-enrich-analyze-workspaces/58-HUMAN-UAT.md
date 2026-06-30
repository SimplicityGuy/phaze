---
status: partial
phase: 58-enrich-analyze-workspaces
source: [58-VERIFICATION.md]
started: 2026-06-30
updated: 2026-06-30
---

## Current Test

[awaiting human testing — deployment/browser-gated]

## Tests

### 1. Single live-refresh poll discipline (WORK-05 / R-2 / R-3) — browser

expected: With the app running and the shell open (`/`), the browser Network tab shows **exactly one** `GET /pipeline/stats` request per ~5 seconds (no second poll loop). Workspace numerals (Discover not-yet-enriched sub-count, Analyze lane capacity, file-table windowed progress) refresh **in place** without a manual reload. When the tab is backgrounded, polling **stops** (visibilitychange shed); it resumes on foreground. On initial paint there is **no `undefined` flash** in the Discover sub-count or A1 lane capacity (W-1 fix).
result: [pending]

### 2. In-flight windowed progress against the real 57.1 signal (WORK-04) — live analysis

expected: While a file is actively analyzing, its Analyze file-table row shows its lane badge (local/A1/k8s) + `running` **and** a live `N/M windows` count that advances as windows complete (the Phase 57.1 mid-flight signal). Completed files show full `window {a}/{total}` coverage. (Structurally asserted by `test_analyze_file_table_lane_and_windows` via seeded data; this item confirms the real runtime signal end-to-end.)
result: [pending]

## Summary

total: 2
passed: 0
issues: 0
pending: 2
skipped: 0
blocked: 0

## Gaps
