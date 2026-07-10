---
status: partial
phase: 85-executed-gate-revival
source: [85-01-SUMMARY.md, 85-02-SUMMARY.md, 85-03-SUMMARY.md, 85-04-SUMMARY.md]
started: 2026-07-10T00:00:00Z
updated: 2026-07-10T00:00:00Z
---

## Current Test

[testing complete — 1 item blocked on homelab deploy]

## Tests

### 1. Tag-write operator workspace lists actually-applied files
expected: The Tag-write review workspace, previously permanently empty (gated on the dead
`state == EXECUTED`), now lists actually-applied files and each row's APPROVE wires to
`POST /tags/{id}/write` (not a proposals PATCH); the header bulk button posts the bulk tag-write.
result: pass
evidence: "Ran the operator-flow test `test_review_apply_workspaces.py::test_tagwrite_workspace_apply_and_bulk_wiring`
  against the live ephemeral DB — PASSED. The applied file (state='moved' + executed proposal) renders
  `hx-post=\"/tags/{file.id}/write\"` and never routes through `/proposals/`."

### 2. Writing tags to an applied file mutates the file on disk + logs COMPLETED
expected: Triggering a tag write on an actually-applied file (the guard that previously ALWAYS failed)
now reads the current tags, writes the proposed tags to the audio file on disk, and records a
`TagWriteLog` with status `completed`.
result: pass
evidence: "End-to-end driver over the REAL `execute_tag_write` path on a real MP3: BEFORE on disk
  `artist=OLD ARTIST, title=OLD TITLE` → AFTER `artist=NEW ARTIST, title=NEW TITLE, album=Live 2026`;
  `is_applied(file)=True` (state on disk = 'moved', gated by the executed proposal, NOT files.state);
  `TagWriteLog.status = completed`. This is the milestone's behavior revival, observed directly."

### 3. CUE operator workspace lists eligible applied files + generation writes a real .cue
expected: The CUE review workspace lists eligible applied files (approved tracklist + timestamped
track), and generating a CUE produces a real `.cue` file on disk with valid REM/FILE/TRACK/INDEX
structure.
result: pass
evidence: "`test_cue_gate_and_preview` PASSED (eligible applied set surfaces, ineligible one does not).
  End-to-end driver over the REAL `generate_cue_content` + `write_cue_file`: wrote `set.cue` containing
  `FILE \"set.mp3\" MP3`, TRACK 01 `INDEX 01 00:00:00`, TRACK 02 `INDEX 01 05:12:00` (312s). Also
  `test_generate_cue_admits_applied_file_not_executed_state` PASSED — the route admits a state='moved'
  applied file."

### 4. Operator review lists are bounded (won't blow up at 200K scale)
expected: The `get_tagwrite_review_rows` / `get_cue_review_cards` builders return at most
`_MAX_REVIEW_ROWS` (=2000) rows even when a large applied backlog qualifies (D-03 bound).
result: pass
evidence: "`test_get_tagwrite_review_rows_bounded_by_cap` PASSED — with `_MAX_REVIEW_ROWS` monkeypatched
  to 3 and >3 applied files seeded, the builder returned exactly 3. NOTE: the known limit-before-filter
  starvation nuance (WR-01) is tracked as accepted follow-up debt; the DoS bound itself holds."

### 5. Non-applied files are still rejected (no stray writes)
expected: A file that is NOT applied (e.g. `state='moved'` but no executed proposal, or a pending/failed
proposal) is rejected by both the tag-write and CUE guards — no filesystem mutation occurs.
result: pass
evidence: "End-to-end driver: a moved-but-no-executed-proposal file returns `is_applied()=False` and
  `execute_tag_write` RAISED `Only executed files can have tags written`. Route tests
  `test_non_applied_file_raises` and `test_generate_cue_file_not_applied` PASSED."

### 6. Live 200K-corpus tag/CUE write end-to-end on homelab
expected: On a real homelab deploy against the ~200K applied-file corpus, the Tags/CUE operator lists
populate with real applied files, an operator triggers one manual tag-write, and the tags are written
to the file on disk with a persisted `TagWriteLog` COMPLETED row.
result: blocked
blocked_by: release-build
reason: "Requires a homelab deployment against the real applied-file corpus + filesystem; cannot be
  driven from the local dev environment. The behavior path is proven locally (tests 1–5, incl. real
  filesystem mutation); this is the production-scale confirmation only. Tracked in 85-HUMAN-UAT.md for
  the next rollout."

## Summary

total: 6
passed: 5
issues: 0
pending: 0
skipped: 0
blocked: 1

## Gaps

[none — 0 issues; the 1 blocked item is a deploy gate, not a code defect]
