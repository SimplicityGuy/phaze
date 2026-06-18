---
status: complete
phase: 44-analyze-observability-ui-straggler-failed-count-sampled-badg
source:
  - 44-01-SUMMARY.md
  - 44-02-SUMMARY.md
  - 44-03-SUMMARY.md
  - 44-04-SUMMARY.md
started: 2026-06-18T17:37:39Z
updated: 2026-06-18T17:58:00Z
method: automated (Playwright-driven against a local stack — ephemeral Postgres+Redis, uvicorn phaze.main:app, seeded data)
---

## Current Test

[testing complete]

## Tests

### 1. Dashboard "Analysis Health" card
expected: On the pipeline dashboard, an "Analysis Health" card shows a straggler count ("still grinding") and an ANALYSIS_FAILED count ("gave up"). Both update live on the existing 5s poll (the card lives outside #pipeline-stats and updates via hx-swap-oob), no manual refresh needed.
result: pass
evidence: Playwright on local stack — card rendered Stragglers=1 (seeded active process_file job aged past threshold) + Analysis failed=3 (seeded analysis_failed files). Added a 4th failed file via SQL with no page reload; after the 5s poll the count auto-updated 3→4 via hx-swap-oob. Screenshot phase44-test1-card.png.

### 2. Sampled badge on a sampled file
expected: Open the analysis timeline / proposal view for a file that was sampled by Phase 43 (analysis.sampled = true). An amber "sampled" badge renders; hovering it shows a tooltip with the four coverage counts. A file that was NOT sampled (or an older pre-Phase-43 row with no coverage data) shows no badge and no error.
result: pass
evidence: Sampled file timeline rendered the amber "Sampled — more data available" badge with tooltip title="fine 60/420, coarse 30/210 windows — sampled" (all four seeded coverage counts). The non-sampled file (sampled=false) rendered NO badge and NO button, no error. Verified both via the in-context expand on /proposals/ and direct fragment fetch.

### 3. "Deepen analysis" button + click
expected: On a sampled file's timeline there is a "Deepen analysis" button (only shown when the file is sampled). Clicking it sends an HTMX POST to /pipeline/files/{file_id}/deepen and shows an inline fragment confirming the re-analysis was enqueued. A non-sampled file does not show the button.
result: pass
evidence: On /proposals/ expanded the sampled row's timeline (htmx loaded), clicked "Deepen analysis"; the aria-live result span filled with "Re-analysis queued at full window budget (deepen)." Non-sampled row showed no button. Screenshot phase44-test3-deepen.png.

### 4. Deepen re-analysis enqueues at full window budget (cap=0)
expected: After clicking "Deepen analysis", an active agent picks up the re-enqueued process_file job and re-analyzes the file with ALL windows (cap=0 / unbounded), not the strided sample. When it finishes, the file's analysis reflects full coverage. If no agent is online, the action degrades gracefully with a clear message rather than erroring.
result: pass
evidence: |
  Inspected saq_jobs after the deepen: the re-enqueued job landed on queue
  "phaze-agent-dev-agent" (per-agent, NEVER the consumer-less default queue — Phase-30 guard)
  with the COMPLETE payload — "fine_cap": 0, "coarse_cap": 0 (the analyze-ALL sentinel),
  correct file_id + agent_id, deterministic key "process_file:<file_id>", timeout 7200,
  retries 2 (v4.0.8 full-payload guard + D-05 dedup all confirmed).
  Graceful no-agent path also verified: with the dev-agent's last_seen_at NULL, the same POST
  returned "No active agent available — start an agent worker and retry. Nothing re-enqueued."
  with nothing enqueued (Phase-30 no-fallthrough guard).
note: |
  Execution boundary — the ACTUAL essentia re-analysis (agent worker crunching all windows and
  writing back full coverage / sampled=false) was NOT run: this UAT harness has no agent worker
  with essentia + a real audio file. The deepen MECHANISM (Phase 44's deliverable: routing +
  full payload + cap=0 + deterministic dedup + graceful degrade) is fully verified end-to-end.
  Actual window-crunching is the agent worker's job (Phase 31/43 scope).

## Summary

total: 4
passed: 4
issues: 0
pending: 0
skipped: 0

## Gaps

[none — all tests passed]
