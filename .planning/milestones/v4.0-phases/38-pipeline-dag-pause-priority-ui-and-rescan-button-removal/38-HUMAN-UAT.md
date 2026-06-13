---
status: partial
phase: 38-pipeline-dag-pause-priority-ui-and-rescan-button-removal
source: [38-VERIFICATION.md]
started: 2026-06-13
updated: 2026-06-13
---

## Current Test

[awaiting human testing — requires homelab deployment with Phase 37 backend live in a real browser]

## Tests

### 1. Pause/Resume button flip
expected: In the pipeline dashboard, clicking Pause on a stage chip hides the amber Pause button and shows the green Resume button (and the reverse), no reload required.
why_human: Alpine x-show toggling driven by `$store.pipeline.{stage}Paused` requires a real browser; not observable via grep/pytest.
result: [pending]

### 2. Priority stepper increments/decrements + boundary disablement
expected: ▲ Higher decrements the displayed number by 10 (e.g. 50→40) and disables at 0; ▼ Lower increments and disables at 100. Number updates flicker-free from the server JSON response.
why_human: `x-text` binding to `$store.pipeline.{stage}Priority` + Alpine `:disabled` requires a live browser + real Phase-37 backend.
result: [pending]

### 3. 5s poll does not revert a just-applied pause (authoritative non-regression)
expected: After pausing a stage, the next 5s `/pipeline/stats` poll re-pushes the correct paused state via OOB dag-seed paragraphs; no flicker or revert.
why_human: Observing the racing-poll vs in-flight-click non-regression requires a live browser with network activity.
result: [pending]

### 4. Mobile `<ol>` text equivalent shows paused/priority annotations
expected: At mobile width (< sm), the sr-only `<ol>` lists "— paused" / "— priority N" for the 3 agent stages (metadata/analyze/fingerprint) only.
why_human: CSS breakpoint + Alpine x-if/x-text rendering require a real browser viewport.
result: [pending]

### 5. No chip overlap / clean SVG edge landing
expected: At sm+ width, no agent chip overlaps the chip below it; the 3 control-bearing chips are separated by a visible gap; SVG bezier edges land on each chip midpoint.
why_human: Rendered pixel height vs NODE_LAYOUT gutter requires a real browser; the automated test checks y-coordinates only.
result: [pending]

## Summary

total: 5
passed: 0
issues: 0
pending: 5
skipped: 0
blocked: 0

## Gaps

[none — all 5 are deployment-gated visual/interactive checks, not code gaps]
