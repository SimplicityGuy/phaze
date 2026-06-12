---
status: partial
phase: 35-pipeline-determinism-idempotency-per-job-type-observability
source: [35-VERIFICATION.md]
started: 2026-06-12
updated: 2026-06-12
---

## Current Test

[awaiting human testing]

## Tests

### 1. SVG DAG visual layout and edge rendering
expected: The 9-node pipeline DAG canvas renders on `/pipeline/` with bézier edges drawn from node anchor points (not overlapping/overflowing). Edges honestly show only Metadata→Proposals and Analyze→Proposals converging (NO Fingerprint→Proposals, NO tracklist→Proposals). Dark mode renders correctly.
result: [pending]

### 2. Alpine.js reactive gating (disabled triggers when upstream empty)
expected: With `discovered === 0`, the Analyze/Fingerprint/etc. node trigger buttons show the gated/`opacity-60` state, a WAITING pill, and a "No files discovered" button label. Buttons enable once upstream counts are non-zero.
result: [pending]

### 3. Responsive `<ol>` fallback at < sm viewport
expected: At a mobile (< sm) viewport width the SVG canvas hides and the stacked `<ol>` list of 9 stages appears in topological order. The `<ol>` is also the screen-reader text equivalent (sm:sr-only).
result: [pending]

### 4. End-to-end metadata + fingerprint trigger (no dead-letter)
expected: Clicking the DAG "Extract Metadata" trigger (and "Fingerprint") against a live agent worker enqueues jobs that the worker accepts and processes — they do NOT dead-letter on payload validation. (Code + regression tests confirm the full ExtractMetadataPayload/FingerprintFilePayload are built and validate against the strict schema; this item is the live-worker confirmation of CR-01/CR-02.)
result: [pending]

## Summary

total: 4
passed: 0
issues: 0
pending: 4
skipped: 0
blocked: 0

## Gaps
