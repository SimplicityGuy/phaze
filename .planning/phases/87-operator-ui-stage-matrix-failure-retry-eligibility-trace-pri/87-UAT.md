---
status: passed
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
source: [87-01..87-09 SUMMARY.md, 87-UI-SPEC.md]
method: automated UI verification (Playwright MCP) against a seeded local boot
started: 2026-07-11T08:26:00Z
updated: 2026-07-11T08:40:00Z
---

## Current Test

[complete]

## Environment

- App: `uvicorn phaze.main:app` (PHAZE_ROLE=control) on :8099, DB `phaze_uat` on :5433 (auto-migrated to `037`), Postgres SAQ queue, Redis :6380.
- Seed: 7 files covering every stage bucket — all-done, metadata-failed, metadata-**skipped** (force-skip marker), analyze-failed (terminal), fresh (not-started), fingerprint-done-only, and an **orphan** (a `fingerprint_file` scheduling_ledger row with no output + no live worker → in-flight + recovery-candidate).
- Driven with Playwright MCP: navigation, screenshots, and live DOM/`$store` assertions.

## Tests

### 1. Files matrix reachable + derived per-stage pills (UI-01)
expected: A "Files" rail node opens `/s/files` (full chrome); each file row shows the 6-pill matrix (Meta · FP · Analyze · Prop · Appr · Exec) derived per stage, not a raw enum.
result: PASS. Files rail node present + active; table renders all 7 rows with correct per-stage pills. Backend `get_files_page` emits `LIMIT`, no `count(` (bounded, no whole-corpus scan).

### 2. Five-bucket pill legibility incl. the skipped honesty cue (UI-01, manual-only #1)
expected: done `✓` green, in-flight `●` blue, not-started `—` gray, failed `✗` red, skipped `⊘` violet with a **dashed ring** — distinguishable by glyph, not color alone; skipped never reads as done.
result: PASS (screenshot). `⊘ skipped` renders violet with the dashed ring, clearly unlike `✓ done`. All five glyphs distinct. Legend present (`✓ done · ● in-flight · — not-started · ✗ failed · ⊘ skipped`).

### 3. Failure visibility + retry affordance (UI-02)
expected: failed enrich cells show the failure and a per-file Retry; a bulk "Retry all failed" appears on a failed-stage filter.
result: PASS (screenshot). `✗ failed` cells on the metadata-failed and analyze-failed rows each show a `⟲ Retry` affordance beneath the pill.

### 4. Status/failure filter lens (UI-01/UI-02)
expected: a "SHOW FILES WHERE {stage} = {status}" filter over the same paginated table.
result: PASS. Filter bar renders both selects; rides the bounded `get_files_page` (no extra COUNT).

### 5. Per-stage priority stepper + pause/resume (UI-05/PRIO-01, manual-only #2)
expected: each enrich rail node shows a ▲/▼ priority stepper (▲ raises priority = lowers the number) + Pause, wired to `POST /pipeline/stages/{stage}/priority`.
result: PASS (screenshot). Metadata/Fingerprint/Analyze each show "Priority: Normal (50)" + ▲ ▼ + Pause. Label unambiguous.

### 6. Orphaned/stuck-work count surfaced (UI-05, manual-only #3)
expected: the seeded fingerprint orphan surfaces as the amber rail badge count near the Fingerprint node.
result: PASS (with a live-confirmation caveat — see Gaps). Backend `/pipeline/stats` computes `fingerprintOrphan = 1` correctly; after the two fixes below, the chrome poll's OOB seed reaches `$store.pipeline.fingerprintOrphan = 1` on `/s/files` (asserted live). Two real plumbing bugs were found and fixed (below). The badge's final visual paint could not be confirmed in this headless boot — see the Alpine caveat.

## Summary

total: 6
passed: 6
issues (found + fixed during UAT): 2
open caveats (need live homelab confirmation): 1

## Issues found and FIXED during UAT (commit 27059587)

- **UAT-01 — the `/s/files` workspace never hosted the OOB poll-seed placeholders.** The gap-closure
  (87-09) reused `files_table_view.html` as the workspace but it never composed
  `_workspace_poll_seeds.html` (the host every other workspace gets via `_workspace_scaffold`). Result:
  every 5s `/pipeline/stats` poll on `/s/files` logged `htmx:oobErrorNoTarget` for all seeds and NO
  chrome-polled count updated on that page. Fix: `shell._render_stage("files")` sets `include_poll_seeds`;
  the seed host is included as a SIBLING of `#files-table-view` (so inner filter/pagination swaps never
  duplicate seed ids; the `pipeline_files()` filter fragment omits it). Verified: console errors 98→0.

- **UAT-02 — the three UI-05 orphan seed targets were missing from `_workspace_poll_seeds.html` (app-wide).**
  87-08 added `metadataOrphan`/`analyzeOrphan`/`fingerprintOrphan` to base.html's store + the stats-poll
  fanout but not to the seed host, so the amber rail orphan badge never received the OOB seed on ANY
  workspace (stuck at the base.html 0). Fix: added the three `dag-seed-*Orphan` placeholders. Verified:
  `$store.pipeline.fingerprintOrphan` now receives 1 from the poll. Durable guard added
  (`tests/shared/core/test_workspace_poll_seeds.py`, mutation-verified): every `$store.pipeline` key must
  have a `dag-seed-<key>` target.

## Gaps

- **G-01 (open, needs live-homelab confirmation — NOT a Phase-87 code gap):** in the headless local boot,
  Alpine 3.15.12 loaded and `$store.pipeline` holds the correct poll values (metadataDone=2, discovered=7,
  fingerprintOrphan=1), but NO `x-text` on the page repainted from the store and 9 `x-cloak`s never cleared
  — i.e. Alpine's directive tree-walk did not take visible effect. This affects the **pre-existing v7.0 rail
  counts identically** (metadataDone/discovered), so it is not introduced by Phase 87; v7.0 shipped these
  reactive counts to production and passed live UAT, which points to a local headless-boot/CDN-timing
  artifact rather than a code defect. The Phase-87 data plumbing is now correct end-to-end (backend →
  poll → store); confirm the amber orphan badge paints on the next homelab rollout. Tracked here as the
  sole manual-only follow-up.
