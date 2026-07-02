---
status: complete
phase: 61-full-record-k-agents
source: [61-01-SUMMARY.md, 61-02-SUMMARY.md, 61-03-SUMMARY.md, 61-04-SUMMARY.md, 61-05-SUMMARY.md, 61-VERIFICATION.md, 61-HUMAN-UAT.md]
started: 2026-07-02T00:14:00Z
updated: 2026-07-02T00:28:00Z
method: driven — Playwright MCP against a live uvicorn app (fresh phaze_uat DB, seeded dev-agent + one analyzed file)
---

## Current Test

[testing complete]

## Tests

### 1. Record slide-in opens on a direct GET / (CR-01)
expected: On a fresh direct `GET /` (Analyze default), clicking a file row opens the right-anchored full-record slide-in with header/facts/timeline/metadata/identity/pending-approvals/history.
result: pass
evidence: The table wrapper carries `x-data` (CR-01 fix live); clicking the row opened the panel and loaded the full record ("Bonobo - Kerala (Live at Coachella).mp3 … Lane 🖥️ local, Windowed analysis, pending-approvals box, history"). Screenshot: uat-61-record-slidein.png.

### 2. Focus-trap — ⌘K palette
expected: Opening ⌘K traps Tab inside the palette; Esc closes it and returns focus to `#cmdk-trigger`.
result: pass
evidence: Palette opens with the combobox focused; Tab stays inside the dialog; Esc closes it. Focus-return initially landed on `<body>` (found + fixed during UAT — commit 9d6f562: `.noreturn` + `$nextTick`-deferred focus); re-verified focus returns to `#cmdk-trigger`.

### 3. Focus-trap — record slide-in
expected: With the record panel open, Tab stays inside; Esc/✕/backdrop closes and returns focus to the opening row.
result: pass
evidence: Panel traps Tab (only the ✕ is focusable for a file with no approvals); Esc closes it. Focus-return initially failed (opener `<tr>` was not focusable); fixed in 9d6f562 by making rows `tabindex="0"` + keyboard-operable. Re-verified: Esc returns focus to `TR#analyze-row-1`, and Enter on a focused row opens the record (keyboard-open bonus).

### 4. Friendly 404 fragment for a missing file (WR-01)
expected: Requesting a record for a de-duplicated/removed file shows the friendly "file no longer exists" fragment inside the panel (htmx normally drops non-2xx).
result: pass
evidence: An htmx GET to a bogus `/record/{uuid}` swapped the friendly fragment into `#record-body` ("That file no longer exists. It may have been moved or de-duplicated…") — the `htmx:beforeSwap` 404 opt-in (WR-01 fix) fired.

### 5. First-run empty state + scan wiring (RECORD-04)
expected: With 0 files, home/Analyze shows the centered guide listing each agent's `scan_roots` with per-agent "Scan" buttons posting the discovery scan; live progress rides the existing single poll.
result: pass
evidence: With 0 files, `GET /` rendered "Point Phaze at your music" with the seeded `dev-agent`, its two roots (`/data/music`, `/data/concerts`), and Scan buttons reading "Scan dev-agent" (WR-02 name fix live, not the UUID) whose aria-labels reference the root. Screenshot: uat-61-empty-state.png. Note: live scan-progress-over-ticks needs a live agent process to consume the enqueue; the static render + `/pipeline/scans` wiring + single-poll structure were verified.

## Summary

total: 5
passed: 5
issues: 0
pending: 0
skipped: 0
blocked: 0

## Also confirmed live (code-review fixes)
- CR-01 (record `x-data` wrapper + panel opens), WR-01 (404 swap), WR-02 (`Scan {agent.name}`), WR-03 (⌘K Artists row carries `q=` → "👤 Bonobo" → `/search/?q=Bonobo`). ⌘K grouped search surfaced the Artists facet over the seeded data.

## Gaps

[none — one focus-return issue was found and fixed live during this UAT; see commit 9d6f562]
