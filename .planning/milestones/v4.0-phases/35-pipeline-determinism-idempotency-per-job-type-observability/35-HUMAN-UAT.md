---
status: complete
phase: 35-pipeline-determinism-idempotency-per-job-type-observability
source: [35-VERIFICATION.md]
started: 2026-06-12
updated: 2026-06-12
method: playwright-mcp automated browser verification (local uvicorn against ephemeral Postgres+Redis, seeded 16 files across states + active agent)
---

## Current Test

[testing complete]

## Tests

### 1. SVG DAG visual layout and edge rendering
expected: 9-node DAG with anchor-derived bézier edges; only Metadata→Proposals and Analyze→Proposals converge into Proposals (no Fingerprint/tracklist→Proposals); no layout overflow.
result: issue → fixed
reported: "some boxes overlap in the rendering" (user, with screenshot — Metadata/Fingerprint trigger buttons painted over by the chip below)
severity: major
fix: "commit 88881ab — NODE_LAYOUT h/y recomputed to real chip heights with 28px gutters; canvas grown 500→720px. Re-verified in-browser: 0 overlaps, 0 clipped buttons, edges anchored. Regression test test_topology_column_one_chips_do_not_overlap added."
edge_topology: "verified honest — 9 edges, metadata+analyze→proposals only; no fingerprint/tracklist edge into proposals"

### 2. Alpine.js reactive gating (disabled triggers when upstream empty)
expected: With discovered === 0, Metadata/Analyze chips show disabled state (opacity-60, WAITING pill, "No files discovered" button label).
result: pass
note: "Driving Alpine.store('pipeline').discovered = 0 flipped both chips reactively: button disabled=true, label 'No files discovered', opacity-60, pill WAITING."

### 3. Responsive <ol> fallback at < sm viewport
expected: At < sm width the SVG canvas hides and the stacked <ol> (screen-reader/text equivalent) appears.
result: pass
note: "At 375px the canvas [role=group] computed display:none; the <ol> is display:block, visible (343px), and reactively reflects live counts."

### 4. End-to-end metadata + fingerprint trigger (no dead-letter)
expected: The DAG trigger endpoints enqueue jobs a worker accepts (no payload-validation dead-letter).
result: pass (code + endpoint verified; live-agent consumption not exercised in dev)
note: "CR-01/CR-02 fixes confirmed: enqueue helpers build the COMPLETE ExtractMetadataPayload/FingerprintFilePayload, asserted valid against the strict schemas by regression tests. The 5s poll /pipeline/stats returns 200 and emits all 17 dag-seed OOB paragraphs with correct live DB-truth counts (metadataDone=8, analyzeDone=3). No live agent worker is connected in the dev environment, so end-to-end job consumption was not exercised; payload completeness is proven by tests."

## Summary

total: 4
passed: 3
issues: 1
pending: 0
skipped: 0
blocked: 0
note: "The 1 issue (chip overlap) was fixed during this session (commit 88881ab) and re-verified; all 4 items now satisfied."

## Gaps

[none — the single issue found was fixed and re-verified in-session]
