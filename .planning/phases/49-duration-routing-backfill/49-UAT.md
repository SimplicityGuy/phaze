---
status: complete
phase: 49-duration-routing-backfill
source: [49-01-SUMMARY.md, 49-02-SUMMARY.md, 49-03-SUMMARY.md, 49-04-SUMMARY.md]
started: 2026-06-25T00:00:00Z
updated: 2026-06-25T00:00:00Z
method: live-server (uvicorn against ephemeral test DB on :5433/:6380, real Jinja/HTMX render, curl-driven)
---

## Current Test

[testing complete]

## Tests

### 1. "Awaiting cloud" held-file count card renders on the dashboard
expected: GET /pipeline/ renders an "Awaiting cloud" card showing the count of files in AWAITING_CLOUD.
result: pass
evidence: With 1 held file seeded, the card rendered `<p ...text-sky-700...>1</p>` under "Awaiting cloud" / "held — no compute agent online". After two more holds (Tests 3 + 5), the card read 3, exactly matching the DB count (`AWAITING_CLOUD: 3`) — the card tracks DB truth.

### 2. Awaiting-cloud card stays live via the 5s OOB poll
expected: The dashboard's `#pipeline-stats` polls `/pipeline/stats` every 5s; the card (which lives outside that target) is re-pushed out-of-band so its count updates without re-rendering the DAG.
result: pass
evidence: GET /pipeline/stats emitted `<section id="awaiting-cloud-card" ... hx-swap-oob="true" ...>` with the live count (1). The dashboard carries `hx-get="/pipeline/stats" hx-trigger="every 5s"` on `#pipeline-stats`; the OOB fragment is the same `#awaiting-cloud-card` id — standard HTMX OOB swap (the production-proven Phase-44 straggler-card pattern). Unit-asserted by `test_stats_partial_emits_awaiting_cloud_card_oob`.

### 3. Run analysis routes by duration; no-agent case surfaces the held count (WR-01)
expected: POST /pipeline/analyze routes each DISCOVERED file by duration. With no compute agent online, a long file is held in AWAITING_CLOUD; the response reports the held count rather than "0 files enqueued".
result: pass
evidence: With no online agent (auto-seeded dev-agent had a NULL last_seen), the response rendered "No compute agent online — 1 held awaiting cloud, 1 skipped (no local agent). Held files release automatically when a compute agent connects." — the WR-01 fix working live (long held, short skipped, held count surfaced).

### 4. Run analysis full split with a fileserver + compute agent online
expected: With both a seen fileserver and a seen compute agent, long files route to the compute queue and short/null files to the fileserver; the response reports the split counts.
result: pass
evidence: After seeding a seen fileserver + compute agent and a fresh long+short pair, POST /pipeline/analyze rendered "Enqueued 2 local, 1 cloud, 0 awaiting cloud for analysis." — long→compute (CLOUDROUTE-01), shorts→fileserver (CLOUDROUTE-03), nothing held (compute online).

### 5. Backfill to cloud selects timed-out long files, resets, and holds when no compute
expected: POST /pipeline/backfill-cloud selects exactly ANALYSIS_FAILED ∧ duration≥threshold files, resets them to DISCOVERED, and routes them (compute if online, else AWAITING_CLOUD).
result: pass
evidence: With one ANALYSIS_FAILED long file and no compute agent, the response rendered "Backfilled 1 long files: 0 cloud, 1 awaiting cloud." The file moved ANALYSIS_FAILED → DISCOVERED → AWAITING_CLOUD (DB `analysis_failed: 0` afterward).

### 6. Backfill double-click is a no-op (no over-enqueue)
expected: A second backfill click finds no candidates (the first reset them out of ANALYSIS_FAILED) and enqueues nothing.
result: pass
evidence: The second POST /pipeline/backfill-cloud rendered "No timed-out long files to backfill." — the explicit filter + reset close the over-enqueue class (CLOUDROUTE-04 / D-10).

## Summary

total: 6
passed: 6
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps

[none]
