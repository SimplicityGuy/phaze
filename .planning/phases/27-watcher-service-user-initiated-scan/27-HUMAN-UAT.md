---
status: partial
phase: 27-watcher-service-user-initiated-scan
source: [27-VERIFICATION.md]
started: 2026-05-13T23:27:39Z
updated: 2026-05-13T23:27:39Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. End-to-end file drop → FileRecord under LIVE batch
expected: Start docker compose with the watcher service and drop a new music file (.mp3) into the watched root. After the settle period (10s), a new FileRecord appears in Postgres under the agent's LIVE ScanBatch with (agent_id, original_path) as the natural key. Re-dropping the same file produces no duplicate rows.
result: [pending]

### 2. Admin UI scan trigger → progress polling → terminal halt
expected: Navigate to /pipeline/ admin UI. Select an agent and a path under its scan_roots. Trigger a scan. The card returns the scan_progress_card partial with RUNNING state and hx-trigger='every 2s'; the card auto-updates every 2s; when scan completes the card transitions to COMPLETED state and polling halts (no hx-trigger AND no hx-get in completed markup).
result: [pending]

### 3. Visual layout verification of admin UI
expected: /pipeline/ dashboard renders Trigger Scan card above stats panel with agent dropdown, scan_root select, and subpath input. All UI-SPEC components (trigger_scan_card, scan_path_picker, recent_scans_table, scan_status_pill, scan_submit_error) render correctly per the UI-SPEC markup. Status pill colors match design tokens.
result: [pending]

## Summary

total: 3
passed: 0
issues: 0
pending: 3
skipped: 0
blocked: 0

## Gaps
