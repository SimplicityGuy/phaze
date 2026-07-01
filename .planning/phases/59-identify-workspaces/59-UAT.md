---
status: complete
phase: 59-identify-workspaces
source: [59-01-SUMMARY.md, 59-02-SUMMARY.md, 59-03-SUMMARY.md]
started: 2026-07-01T01:03:14Z
updated: 2026-07-01T01:08:00Z
method: driven — local uvicorn (phaze_uat DB + ephemeral test Redis) + Playwright, seeded representative data
---

## Current Test

[testing complete]

## Tests

### 1. Track-ID workspace renders the combined per-file identity table
expected: `/s/trackid` shows a `TRACK-ID` workspace with ONE table, columns File · audfprint · Panako · Tracklist · Confidence; read-only (no trigger button).
result: pass
observed: heading `TRACK-ID`, single table with the 5 columns, 3 file rows, no action button in header (read-only, D-01/inert).

### 2. Track-ID per-engine fingerprint status words (Pitfall-1)
expected: each row shows audfprint + Panako as status words done/failed/pending, keyed on the real `success` value.
result: pass
observed: Coachella row = done/done; Live Set B = done/failed; Unknown C = done/pending — exactly the seeded fingerprint_results (success→done, failed→failed, no-row→pending).

### 3. Track-ID tracklist match state + confidence (D-04)
expected: linked file shows matched + its match_confidence %; unlinked shows candidate/no-match.
result: pass
observed: Coachella (linked, mc=88) → matched 88%. Files B/C (no linked tracklist) → candidate 72%.
note: B/C have NO tracklist of their own yet show "candidate 72%" — the D-04 system-wide best-candidate fallback (schema has no per-file candidate link; code-review Info item, an accepted documented tradeoff — not a defect).

### 4. Tracklist workspace — three step cards (D-05)
expected: `/s/tracklist` shows three sequential step cards Search · Scrape · Match (NOT a stepper), each with a done/total count.
result: pass
observed: `1 · 🔎 SEARCH` (1/—), `2 · 📄 SCRAPE` (2/2), `3 · 🔗 MATCH` (0/2), rendered as a 3-card grid.

### 5. Tracklist per-step ALL triggers with R-4 guard (D-06)
expected: each card has an ALL trigger (SEARCH/SCRAPE/MATCH ALL) wired to the existing bulk endpoint, guarded by hx-confirm + disable-while-busy; no single chain button.
result: pass
observed: `hx-post` to `/pipeline/{search,scrape,match}-tracklists`, `hx-confirm="Enqueue…?"`, `:disabled="$store.pipeline.*Busy > 0"` on each; no run-chain button. (Verified in served HTML — triggers not fired to avoid external 1001Tracklists calls.)

### 6. Tracklist per-set N/M coverage table, latest-version scoped (D-07/D-08, WR-01)
expected: table below the cards, one row per set, showing N/M confident tracks scoped to the latest tracklist version.
result: pass
observed: Peggy Gou (candidate) 2/3; Coachella (matched) 1/2 — matches seeded latest-version tracks; WR-01 fix confirmed (no cross-version inflation).

### 7. Single-poll discipline + bare fragments (WORK-05/R-2, R-5)
expected: both workspaces ride the ONE existing /pipeline/stats poll (no second loop); HX responses are bare fragments.
result: pass
observed: exactly 1 `hx-trigger="…every…"` on the full page (the shell's poll); fragments add none. HX response to `/s/tracklist` contains 0 `<html>` tags (bare fragment).

## Summary

total: 7
passed: 7
issues: 0
pending: 0
skipped: 0

## Gaps

[none]
