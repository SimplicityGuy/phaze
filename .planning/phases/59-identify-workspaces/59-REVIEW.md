---
phase: 59-identify-workspaces
reviewed: 2026-07-01T00:10:07Z
depth: standard
files_reviewed: 5
files_reviewed_list:
  - src/phaze/routers/shell.py
  - src/phaze/services/pipeline.py
  - src/phaze/templates/pipeline/partials/trackid_workspace.html
  - src/phaze/templates/pipeline/partials/tracklist_workspace.html
  - tests/test_identify_workspaces.py
findings:
  critical: 0
  warning: 2
  info: 4
  total: 6
status: issues_found
---

# Phase 59: Code Review Report

**Reviewed:** 2026-07-01T00:10:07Z
**Depth:** standard
**Files Reviewed:** 5
**Status:** issues_found

## Summary

Phase 59 adds two read-only stage workspaces (Track-ID + Tracklist) as HTMX fragments over
existing endpoints/models, plus two new service helpers (`get_trackid_stage_files`,
`get_tracklist_set_rows`) and a pure badge-mapper (`_trackid_engine_badge`).

The phase's stated correctness invariants hold up under adversarial checking:

- **T-57-01** — every `STAGE_PARTIALS` value is a static string literal; `stage` is
  whitelisted (`if stage not in STAGE_PARTIALS: 404`) and never spliced into a template path.
- **XSS boundary** — all DB-sourced cell text flows through `_file_table.html`'s
  `{{ cell.text }}` / `title="{{ cell.title }}"` autoescape; no `| safe` on DB data. The
  `subcount` `x-text` expressions carry only static `$store.pipeline.*` keys (no DB data) and
  match the Phase-58 pattern.
- **Fingerprint vocabulary** — `_trackid_engine_badge` correctly keys "done" on `"success"`
  (the value the engine adapters actually persist), tolerating `"completed"` defensively.
  Verified against the writer path (`services/fingerprint.py` → `tasks/fingerprint.py:48` →
  `routers/agent_fingerprint.py` `pg_insert`): only `"success"`/`"failed"` are ever stored.
- **Read-only + degrade-safe** — both new helpers are pure SELECTs wrapped in
  `session.begin_nested()` returning `[]` on error; no enqueue/commit/add/flush/DDL.
- **No new poll / store key** — templates carry no `hx-trigger="every"` / `setInterval`; the
  `$store.pipeline` keys used (`fingerprintDone`, `tracklistDone`, `searchBusy`, `scrapeBusy`,
  `matchBusy`) all already exist in `base.html` / `shell.html`. No new OOB seed, no chain
  endpoint.

No blockers. Findings below are two correctness warnings (one adjacent-pre-existing, one new)
and four informational items.

## Warnings

### WR-01: `get_tracklist_set_rows` per-set track coverage double-counts multi-version tracklists

**File:** `src/phaze/services/pipeline.py:988-997`
**Issue:** The `track_counts_subq` joins `TracklistVersion` → `TracklistTrack` and groups by
`TracklistVersion.tracklist_id`, summing `COUNT(id)` / `COUNT(confidence)` across **every**
version of a tracklist:

```python
track_counts_subq = (
    select(
        TracklistVersion.tracklist_id.label("tracklist_id"),
        func.count(TracklistTrack.id).label("total"),
        func.count(TracklistTrack.confidence).label("confident"),
    )
    .select_from(TracklistVersion)
    .join(TracklistTrack, TracklistTrack.version_id == TracklistVersion.id)
    .group_by(TracklistVersion.tracklist_id)
)
```

The `Tracklist` model carries `latest_version_id` and `TracklistVersion.version_number`
(models/tracklist.py:38,60) — multiple versions per tracklist are a first-class state (a
re-scrape appends a new version). For any re-scraped set the D-07 "N/M" coverage in the
Tracklist workspace is inflated (e.g. a 10-track set scraped twice renders `20/20` or `18/20`
instead of `10/10`). The docstring acknowledges "summed across the tracklist's versions" but
the rendered coverage is then simply wrong for the multi-version case.
**Fix:** Scope the track count to the tracklist's latest version only, e.g. join on
`Tracklist.latest_version_id == TracklistTrack.version_id` (or a per-tracklist
`max(version_number)` correlated subquery) so the N/M reflects a single version:
```python
track_counts_subq = (
    select(
        TracklistTrack.version_id.label("version_id"),
        func.count(TracklistTrack.id).label("total"),
        func.count(TracklistTrack.confidence).label("confident"),
    )
    .group_by(TracklistTrack.version_id)
    .subquery()
)
# ...outerjoin on track_counts_subq.c.version_id == Tracklist.latest_version_id
```

### WR-02: `get_stage_progress` fingerprint done-count filters on never-persisted `"completed"` status

**File:** `src/phaze/services/pipeline.py:371` (pre-existing; Phase 35 — but in a reviewed
file and directly contradicts the vocabulary this phase correctly handles)
**Issue:** The fingerprint DAG node counts:

```python
select(func.count(distinct(FingerprintResult.file_id))).where(FingerprintResult.status == "completed")
```

`FingerprintResult.status` is only ever written `"success"` or `"failed"` (verified via
`services/fingerprint.py:102/153`, `tasks/fingerprint.py:48`, `routers/agent_fingerprint.py`
`pg_insert`). `"completed"` is never persisted, so `fingerprint.done` is structurally always
`0`. Phase 59 correctly documented and fixed this exact vocabulary trap in
`_trackid_engine_badge` (Pitfall 1) but left the identical bug untouched one function up in the
same reviewed file. This helper is also loaded on the Tracklist render path
(`shell.py:163` → `get_stage_progress`), though the fingerprint key itself isn't shown there.
**Fix:** Align with the persisted vocabulary:
```python
select(func.count(distinct(FingerprintResult.file_id))).where(FingerprintResult.status == "success")
```
(and update the docstring at pipeline.py:309). If "any completed engine counts as done" is the
intent, `.where(FingerprintResult.status.in_(("success", "failed")))` may be more appropriate —
but `"completed"` is unambiguously dead.

## Info

### IN-01: Candidate confidence is a system-wide value applied to every unmatched file

**File:** `src/phaze/services/pipeline.py:895-904, 951-953`
**Issue:** For any file with a fingerprint but no linked tracklist, the `"candidate"` branch
surfaces `best_candidate` — the single highest `match_confidence` across **all** unlinked
tracklists system-wide — and `has_candidate` is a global existence check. Every unmatched file
therefore renders the identical candidate percentage regardless of any relationship to that
tracklist, which is misleading per-file data. The docstring flags this as the literal D-04
reading and defers refinement, so this is informational, not a defect against the plan.
**Fix:** If UI-SPEC later requires per-file candidates, tie the candidate to the file via the
fingerprint/AcoustID linkage rather than a global `max()`; otherwise consider rendering
candidate confidence as `—` to avoid implying a per-file score.

### IN-02: Tracklist workspace render is not fully degrade-safe (3 of 5 reads can raise)

**File:** `src/phaze/routers/shell.py:163-167`
**Issue:** The `tracklist` branch calls `get_stage_progress` (internally `_safe_count`-guarded)
and the two new SAVEPOINT-wrapped helpers, but also `get_untracked_files`,
`get_scrape_pending_tracklists`, and `get_match_pending_tracklists` — pre-existing plain SELECTs
with no try/except. A DB hiccup during a direct `/s/tracklist` navigation would 500 the page.
This matches the established Phase-58 pattern (Discover/Metadata branches call the same class of
unguarded helpers) and only affects direct nav, not the 5s poll, so it is informational.
**Fix:** If robustness parity with the new helpers is desired, wrap these three pending-count
reads in the same degrade-safe SAVEPOINT idiom or a shared `_safe_list` helper.

### IN-03: Subcount copy overstates the bound metric ("sets matched" / "with a tracklist match")

**File:** `src/phaze/templates/pipeline/partials/tracklist_workspace.html:28`,
`src/phaze/templates/pipeline/partials/trackid_workspace.html:26`
**Issue:** Both subcounts bind to `$store.pipeline.tracklistDone`, which (per `dag_canvas.html`)
is the count of tracklists *discovered* (the union of both producer edges), not the count of
sets *matched* to a discogs release. The copy "`… sets matched`" and "`… with a tracklist
match`" therefore labels a discovery count as a match count. Cosmetic, but the numeral will read
higher than a user expects for "matched".
**Fix:** Either soften the wording (e.g. "sets with a tracklist") or bind to a match-specific
store key if one is desired (no new key is in scope this phase).

### IN-04: Stale test docstring — "xfail stubs" are now filled assertions

**File:** `tests/test_identify_workspaces.py:15-20`
**Issue:** The module docstring states the four workspace behavior tests "are `xfail` stubs that
COLLECT cleanly now," but `test_trackid_table_signals`,
`test_trackid_success_renders_done`, `test_tracklist_step_cards_and_triggers`, and
`test_tracklist_per_set_coverage` are all fully-implemented real assertions with no `xfail`
marker (Plans 02/03 converted them). The stale docstring could mislead a future reader into
thinking these are non-enforcing.
**Fix:** Update the docstring to reflect that the workspace tests are now active assertions.

---

_Reviewed: 2026-07-01T00:10:07Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
