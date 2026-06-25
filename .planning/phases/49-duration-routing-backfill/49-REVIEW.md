---
phase: 49-duration-routing-backfill
reviewed: 2026-06-25T00:00:00Z
depth: standard
files_reviewed: 22
files_reviewed_list:
  - src/phaze/config.py
  - src/phaze/models/file.py
  - src/phaze/routers/pipeline.py
  - src/phaze/services/enqueue_router.py
  - src/phaze/services/pipeline.py
  - src/phaze/tasks/controller.py
  - src/phaze/tasks/release_awaiting_cloud.py
  - src/phaze/templates/pipeline/dashboard.html
  - src/phaze/templates/pipeline/partials/awaiting_cloud_card.html
  - src/phaze/templates/pipeline/partials/backfill_response.html
  - src/phaze/templates/pipeline/partials/dag_canvas.html
  - src/phaze/templates/pipeline/partials/stats_bar.html
  - src/phaze/templates/pipeline/partials/trigger_response.html
  - tests/_queue_fakes.py
  - tests/test_config/test_cloud_route_threshold.py
  - tests/test_dag_canvas_render.py
  - tests/test_routers/test_pipeline.py
  - tests/test_services/test_enqueue_router.py
  - tests/test_services/test_pipeline.py
  - tests/test_tasks/test_controller_reenqueue.py
  - tests/test_tasks/test_recovery.py
  - tests/test_tasks/test_release_awaiting_cloud.py
findings:
  critical: 1
  warning: 3
  info: 2
  total: 6
status: issues_found
resolution:
  cr_01: resolved   # commit a55482d — kind-aware recovery (Option A) + 3 regression tests
  wr_03: resolved   # commit a55482d — corrected stale release_awaiting_cloud docstring
  wr_01: deferred    # cosmetic HTMX count display; tracked follow-up
  wr_02: deferred    # deepen_analysis is pre-existing, outside Phase-49 scope; tracked follow-up
  in_01: deferred    # verify file_metadata.file_id unique constraint; tracked follow-up
  in_02: deferred    # controller.startup forward-ref cleanup; tracked follow-up
---

> **Resolution (2026-06-25):** CR-01 (Critical) fixed via Option A — `recover_orphaned_work`
> now routes held `AWAITING_CLOUD` `process_file` rows to a compute agent only (skips for the
> release cron when none online), closing the CLOUDROUTE-02 violation. WR-03 (stale docstring)
> corrected in the same commit (`a55482d`). WR-01, WR-02, IN-01, IN-02 are non-blocking and
> tracked as follow-ups (WR-02's `deepen_analysis` predates Phase 49 and is outside its scope).


# Phase 49: Code Review Report

**Reviewed:** 2026-06-25
**Depth:** standard
**Status:** issues_found

## Summary

Phase 49 adds per-file duration routing (short→fileserver, long→compute), an `AWAITING_CLOUD` held state, a `*/5` release cron, and an operator "Backfill to cloud" action. The per-file router (`_route_discovered_by_duration`), the release cron, and the dashboard wiring are individually correct and well-tested, and the queue-routing-never-default invariant holds at every new enqueue site I traced.

The serious problem is a **cross-module interaction**: the backfill path (`trigger_backfill_cloud`) seeds a scheduling-ledger row for held `AWAITING_CLOUD` files (D-09), but the Phase-45 recovery producer (`recover_orphaned_work`) selects its agent **kind-agnostically** and will replay those held rows onto a fileserver queue — violating the load-bearing CLOUDROUTE-02 invariant that a long file is *never* analyzed locally. This is directly contradicted by `release_awaiting_cloud`'s own module docstring, which asserts held files carry no ledger row. No test covers the routing of a held file through recovery, so the gap is invisible to the suite.

## Critical Issues

### CR-01: Backfill-held files are recoverable and get routed to a fileserver agent, breaking CLOUDROUTE-02

**File:** `src/phaze/routers/pipeline.py:672-687` (ledger seed) + `src/phaze/tasks/reenqueue.py:283-308` (replay)
**Issue:**
`trigger_backfill_cloud` holds long files in `AWAITING_CLOUD` and, for the held branch only, seeds a scheduling-ledger row via `insert_ledger_if_absent(... function="process_file" ...)`, which stamps `routing="agent"` (confirmed by `test_backfill_no_compute_holds_awaiting_cloud_with_ledger_row`).

`recover_orphaned_work` then treats such a row as orphaned:
- `is_domain_completed` is `False` for an `AWAITING_CLOUD` file (analyze done-set is `{ANALYZED, ANALYSIS_FAILED}` only — see `test_awaiting_cloud_file_stays_pending_in_recovery`), and
- the key is not live (the held file was never enqueued).

So the row is partitioned into `agent_rows` and replayed via:
```python
agent = await select_active_agent(session)          # reenqueue.py:299 — NO kind filter
agent_queue = ctx["task_router"].queue_for(agent.id)
```
`select_active_agent` with no `kind` returns the most-recently-seen agent of **any** kind. The exact condition under which the file was held is "no compute agent online" — i.e., typically only the fileserver is online — so recovery enqueues `process_file:<id>` (with the stored `agent_id=""` payload) onto the **fileserver** queue. The fileserver worker then analyzes the long file locally, which is precisely what Phase 49 exists to prevent (the 4h-timeout incident; CLOUDROUTE-02 / T-49-03 "NEVER silently analyzed locally"). It is reachable any time the operator clicks the global "Recover orphaned work" button (`force=True`) — or on a queue-loss boot — while a fileserver is online and compute is not.

The release cron correctly scopes its selection with `select_active_agent(session, kind="compute")` (`release_awaiting_cloud.py:69`); recovery does not, and recovery is now reachable for held files only because backfill gave them a ledger row.

**Fix:** Do not make held files recoverable through the kind-agnostic recovery path. Two viable options:

Option A — make recovery kind-aware for `process_file` rows whose file is `AWAITING_CLOUD` (route only to a compute agent, else skip).

Option B (simpler, matches the release module's documented invariant) — do **not** seed a ledger row for held backfill files at all; let the state-driven `release_awaiting_cloud` cron be the sole drain for held files (exactly as it is for "Run Analysis"-held files, which have no ledger row). Remove the held-branch ledger seed and `_held_backfill_ledger_payload`. This eliminates the divergence between the two held-file producers and removes the recovery foot-gun entirely.

Option B is preferable: it restores the single, consistent held-file recovery mechanism and makes `release_awaiting_cloud.py`'s docstring true again (see WR-03).

## Warnings

### WR-01: HTMX "Run Analysis" response hides the awaiting-cloud / skipped counts when both agent kinds are absent

**File:** `src/phaze/routers/pipeline.py:592-606` + `src/phaze/templates/pipeline/partials/trigger_response.html:1-11`
**Issue:** `trigger_analysis_ui` passes `no_active_agent=True` together with `split_counts=True` and the real `awaiting`/`skipped` numbers, but the template's first branch `{% if no_active_agent %}` wins and renders only "...0 files enqueued for analysis." When both kinds are absent yet long files were just committed to `AWAITING_CLOUD`, the operator is told nothing was done. The JSON endpoint does surface `awaiting_cloud` in this case, so the two paths are inconsistent. The held count only re-appears on the next 5s `awaiting_cloud_card` poll.
**Fix:** In `trigger_response.html`, when `no_active_agent` and `awaiting` is truthy, append the held count, or branch on `split_counts` before `no_active_agent` when `awaiting > 0`.

### WR-02: `deepen_analysis` routes `process_file` kind-agnostically — a long file can land on a fileserver

**File:** `src/phaze/routers/pipeline.py:739-749`
**Issue:** `deepen_analysis` resolves the queue via `resolve_queue_for_task("process_file", ...)`, which calls `select_active_agent(session)` with no `kind`. "Deepen" re-runs a single file at the *unbounded* window budget. If the deepened file is long, routing it to a fileserver re-creates exactly the unbounded long-file analysis that Phase 49 (and Phase 43) exist to keep off the fileserver. The endpoint does not consult `cloud_route_threshold_sec` or the file's duration at all.
**Fix:** Apply the same duration gate as `_route_discovered_by_duration`. NOTE: `deepen_analysis` is a pre-existing endpoint outside Phase 49's declared scope; track as a follow-up rather than a Phase-49 regression.

### WR-03: `release_awaiting_cloud` docstring asserts an invariant that backfill violates

**File:** `src/phaze/tasks/release_awaiting_cloud.py:13-15`
**Issue:** The module docstring states held files "carry NO scheduling-ledger row and `recover_orphaned_work`'s ledger-driven replay structurally cannot see them." This is false for backfill-held files, which `trigger_backfill_cloud` explicitly gives a ledger row (D-09). The stale invariant is what makes CR-01 easy to miss.
**Fix:** Resolve CR-01 via Option B (stop seeding the ledger row) so the docstring becomes true again; or correct the docstring under Option A.

## Info

### IN-01: Duration join relies on FileMetadata being 1:1 with FileRecord at the DB level

**File:** `src/phaze/services/pipeline.py:788-802, 819-855`
**Issue:** `get_discovered_files_with_duration` (LEFT OUTER JOIN) and `_backfill_candidates_stmt` (INNER JOIN) emit one `(FileRecord, duration)` tuple per joined metadata row. The ORM relationship is `uselist=False`, but no DB-level unique constraint on `FileMetadata.file_id` was seen in the reviewed files. Actual double-enqueue is prevented by the deterministic `process_file:<id>` key.
**Fix:** Confirm a unique constraint exists on `file_metadata.file_id`; if not, de-duplicate by `FileRecord.id` after the join.

### IN-02: `controller.startup` references the module-level `queue` defined later in the file

**File:** `src/phaze/tasks/controller.py:105, 113, 178`
**Issue:** `startup` reads/mutates the module-global `queue` defined textually later. Works because `startup` runs after import completes, but the forward reference is fragile.
**Fix:** Stash the queue on `ctx` from the already-constructed `settings["queue"]`, or move construction above the hooks.

---

_Reviewed: 2026-06-25_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
