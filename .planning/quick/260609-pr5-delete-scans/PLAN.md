---
phase: quick-260609-pr5-delete-scans
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/services/scan_deletion.py
  - tests/test_services/test_scan_deletion.py
  - src/phaze/routers/pipeline_scans.py
  - src/phaze/routers/pipeline.py
  - tests/test_routers/test_pipeline_scans.py
  - src/phaze/templates/pipeline/partials/recent_scans_table.html
  - docs/api.md
  - README.md
autonomous: true
requirements: []
user_setup: []

must_haves:
  truths:
    - "Operator can delete a terminal (completed/failed) scan from the Recent Scans table"
    - "Deleting a scan removes the ScanBatch row and EVERY row in the DB that hangs off its files"
    - "Deleting a scan NEVER touches data belonging to any other batch"
    - "The `live` watcher sentinel batch can never be deleted (409)"
    - "A running scan cannot be deleted (409); no delete control renders for it"
    - "After delete, the Recent Scans table re-renders without the deleted row (HTMX swap)"
  artifacts:
    - path: "src/phaze/services/scan_deletion.py"
      provides: "delete_scan_cascade(session, batch_id) ordered transactional cascade"
      contains: "async def delete_scan_cascade"
    - path: "src/phaze/routers/pipeline_scans.py"
      provides: "DELETE /pipeline/scans/{batch_id} endpoint + build_recent_scans helper"
      contains: "async def delete_scan"
    - path: "src/phaze/templates/pipeline/partials/recent_scans_table.html"
      provides: "per-row delete control on terminal rows + Actions column"
      contains: "hx-delete"
  key_links:
    - from: "recent_scans_table.html delete button"
      to: "DELETE /pipeline/scans/{batch_id}"
      via: "hx-delete + hx-target=#recent-scans + hx-swap=outerHTML"
      pattern: "hx-delete=\"/pipeline/scans/"
    - from: "routers/pipeline_scans.py::delete_scan"
      to: "services/scan_deletion.py::delete_scan_cascade"
      via: "service call inside one transaction, then session.commit()"
      pattern: "delete_scan_cascade\\("
---

<objective>
PR5 of 5 (final): give the single-user admin UI a way to delete a recent scan and
remove ALL database data associated with it, in one transaction, with zero
collateral damage to other batches.

Purpose: Closes the scan-lifecycle loop. After PR1–4 (structlog, last_progress_at
heartbeat, stall reaper, activity indicator) the operator can see and triage scans
but cannot remove a bad/stale one. This PR adds deletion + an ordered,
application-level cascade across the full FK graph that hangs off a ScanBatch.

Output:
- `services/scan_deletion.py::delete_scan_cascade` — ordered, set-based, transactional cascade
- `DELETE /pipeline/scans/{batch_id}` HTMX endpoint with live/running guards
- delete control in `recent_scans_table.html` (terminal rows only)
- full test coverage (service cascade scoping, endpoint guards, UI render) + docs

Scope is delete-scans + cascade ONLY. No FK schema changes, no migration.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@CLAUDE.md

<interfaces>
<!-- Verified FK dependency DAG (read directly from the models — DO NOT trust the
     PR brief's summary, which was wrong about the tracklist chain). Every FK that
     transitively hangs off a ScanBatch, with its real tablename, FK column, and
     ondelete rule: -->

scan_batches.id  (ScanBatch — models/scan_batch.py)
└── files.batch_id → scan_batches.id          (FileRecord, models/file.py)        [no ondelete]
    ├── metadata.file_id → files.id            (FileMetadata, models/metadata.py, UNIQUE)        [no ondelete]
    ├── analysis.file_id → files.id            (AnalysisResult, models/analysis.py, UNIQUE)      [no ondelete]
    ├── fingerprint_results.file_id → files.id (FingerprintResult, models/fingerprint.py)        [no ondelete]
    ├── tag_write_log.file_id → files.id       (TagWriteLog, models/tag_write_log.py)            [no ondelete]
    ├── proposals.file_id → files.id           (RenameProposal, models/proposal.py)             [no ondelete]
    │   └── execution_log.proposal_id → proposals.id  (ExecutionLog, models/execution.py)       [no ondelete]
    ├── file_companions.media_id → files.id    (FileCompanion, models/file_companion.py)  [ondelete=CASCADE]
    │   file_companions.companion_id → files.id                                          [ondelete=CASCADE]
    └── tracklists.file_id → files.id          (Tracklist, models/tracklist.py, file_id NULLABLE) [no ondelete]
        └── tracklist_versions.tracklist_id → tracklists.id   (TracklistVersion)         [no ondelete]
            └── tracklist_tracks.version_id → tracklist_versions.id  (TracklistTrack)    [no ondelete]
                └── discogs_links.track_id → tracklist_tracks.id   (DiscogsLink)         [no ondelete]

<!-- CRITICAL CORRECTION vs the PR brief:
  - There is an intermediate `tracklist_versions` table. tracklist_tracks hang off
    a VERSION (tracklist_tracks.version_id → tracklist_versions.id), NOT directly off
    a tracklist. The chain is 4 levels deep: tracklists → tracklist_versions →
    tracklist_tracks → discogs_links.
  - Tracklist.latest_version_id is a plain nullable UUID column with NO ForeignKey
    constraint, so it imposes no delete-ordering requirement.
  - tracklists.file_id is NULLABLE: scraped-but-unmatched tracklists have file_id=NULL
    and MUST NOT be deleted. Scoping by `file_id IN (this batch's files)` handles that.
  - file_companions already CASCADE on both sides, but delete the rows explicitly
    anyway (scoped) so behavior does not silently depend on DB-engine cascade. -->

Existing reusable helpers in routers/pipeline_scans.py (import, do not re-derive):
  _TERMINAL_STATUSES = frozenset({ScanStatus.COMPLETED, ScanStatus.FAILED})
  def elapsed_seconds(batch) -> int
  def seconds_since_progress(batch) -> int
  def is_scan_stalled(batch) -> bool
  logger = structlog.get_logger(__name__)

ScanStatus (models/scan_batch.py): RUNNING / COMPLETED / FAILED / LIVE (StrEnum; batch.status is the str value).

Dashboard's Recent Scans query + transient-attr attachment (routers/pipeline.py::dashboard,
lines ~142-162): selects last 10 ScanBatch where status != 'live', desc by created_at,
then attaches `_agent_name`, `_elapsed_seconds`, `_seconds_since_progress`, `_is_stalled`.
This block is what the delete endpoint must reproduce to re-render the table — extract it.
</interfaces>

Test fixtures (tests/conftest.py): real-Postgres. `session` (AsyncSession against
TEST_DATABASE_URL, create_all schema, seeds `legacy-application-server` Agent), and
the smoke-app pattern in tests/test_routers/test_pipeline_scans.py
(`_make_smoke_app` + `smoke` fixture mounting pipeline_scans + pipeline routers with
an AsyncMock task_router/queue). Service helper `_make_file(...)` pattern lives in
tests/test_services/test_companion.py.
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Ordered transactional cascade service</name>
  <files>src/phaze/services/scan_deletion.py, tests/test_services/test_scan_deletion.py</files>
  <behavior>
    delete_scan_cascade(session, batch_id) -> dict[str, int] (table -> rows deleted):
    - Seeds a batch with files + metadata + analysis + fingerprint_results + proposals
      + execution_log + tracklists + tracklist_versions + tracklist_tracks + discogs_links
      + tag_write_log + file_companions; after the call EVERY one of those rows is gone
      and the scan_batches row is gone.
    - A SECOND independent batch seeded with the full graph is 100% INTACT afterward
      (no collateral deletion). Assert exact surviving counts per table.
    - Cross-batch companion: a file in batch A linked via file_companions to a file in
      batch B. Deleting batch A removes the file_companions JOIN row but the batch-B
      FILE survives.
    - Tracklist with file_id=NULL (scraped/unmatched) is NOT deleted when an unrelated
      batch is deleted.
    - Returns a counts dict whose values match the seeded cardinality.
  </behavior>
  <action>
    Create delete_scan_cascade as an async function taking (session: AsyncSession,
    batch_id: uuid.UUID) -> dict[str, int]. Implement an explicit ordered cascade using
    SQLAlchemy Core `delete()` statements with `.where(col.in_(select(...)))` subqueries
    and `execution_options(synchronize_session=False)` for set-based bulk deletes
    (scans can hold tens of thousands of files — do NOT load rows into the identity map).
    Do NOT commit inside the service; the caller owns the transaction (keeps it composable
    and lets the endpoint commit atomically).

    Delete in this verified child->parent order, every statement scoped to THIS batch's
    files (let F = select(FileRecord.id).where(FileRecord.batch_id == batch_id)):
      1. discogs_links   WHERE track_id   IN (tracklist_tracks of versions of tracklists whose file_id IN F)
      2. tracklist_tracks WHERE version_id IN (tracklist_versions of tracklists whose file_id IN F)
      3. tracklist_versions WHERE tracklist_id IN (tracklists whose file_id IN F)
      4. tracklists      WHERE file_id IN F
      5. execution_log   WHERE proposal_id IN (proposals whose file_id IN F)
      6. proposals       WHERE file_id IN F
      7. fingerprint_results WHERE file_id IN F
      8. analysis        WHERE file_id IN F
      9. metadata        WHERE file_id IN F
      10. tag_write_log  WHERE file_id IN F
      11. file_companions WHERE media_id IN F OR companion_id IN F
      12. files          WHERE batch_id == batch_id
      13. scan_batches   WHERE id == batch_id

    Each delete subquery must reference only tables not yet deleted at that step (verified:
    step 5 references proposals which is deleted at step 6; step 1 references tracklist_tracks
    deleted at step 2; etc.). Build nested `select()` subqueries — do NOT pre-fetch id lists
    into Python (avoid huge IN-lists / N round-trips). Capture `result.rowcount` per statement
    into the counts dict keyed by tablename. Emit a structlog INFO at the end with batch_id and
    the counts dict. Use full type hints; mypy-strict clean; line length 150; double quotes.

    Write tests FIRST (RED). Use the real-Postgres `session` fixture. Build a seed helper that
    constructs the full graph for a given batch (reuse the FileRecord-builder style from
    tests/test_services/test_companion.py). Assert per-table counts via
    `select(func.count()).select_from(Model)` within the same transaction (uncommitted deletes
    are visible transaction-locally). Assert the second batch's full graph survives and the
    cross-batch companion file survives.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr5-delete-scans && uv run pytest tests/test_services/test_scan_deletion.py -x</automated>
  </verify>
  <done>delete_scan_cascade removes the batch + all descendant rows in one transaction, scoped strictly to the batch's files; a sibling batch and cross-batch companion files are provably untouched; returns per-table counts. Tests green.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: DELETE endpoint + shared recent-scans helper</name>
  <files>src/phaze/routers/pipeline_scans.py, src/phaze/routers/pipeline.py, tests/test_routers/test_pipeline_scans.py</files>
  <behavior>
    DELETE /pipeline/scans/{batch_id}:
    - completed scan -> 200, recent_scans_table.html returned WITHOUT the deleted row;
      delete_scan_cascade was invoked and the row is gone from the DB.
    - unknown batch_id -> 404.
    - status == 'live' -> 409 (live sentinel can NEVER be deleted), no rows touched.
    - status == 'running' -> 409 (only terminal scans deletable), no rows touched.
    - failed scan -> 200, deleted (terminal).
  </behavior>
  <action>
    Add `async def delete_scan(request, batch_id: uuid.UUID, session)` decorated with
    `@router.delete("/{batch_id}", response_class=HTMLResponse)` in routers/pipeline_scans.py.
    Logic: `session.get(ScanBatch, batch_id)`; None -> raise HTTPException 404. If
    `batch.status == ScanStatus.LIVE.value` -> raise HTTPException 409 "live watcher batch
    cannot be deleted". If `batch.status not in _TERMINAL_STATUSES` (i.e. RUNNING) -> raise
    HTTPException 409 "cannot delete a running scan; wait for it to complete or fail". This
    server-side recheck is authoritative defense-in-depth: the reaper may have flipped a row
    to FAILED, or a stale button may target a now-running row. On a deletable row: call
    `await delete_scan_cascade(session, batch_id)`, then `await session.commit()`, then
    `logger.info("scan deleted", batch_id=str(batch_id), **counts)`. Finally return the
    re-rendered Recent Scans section for the HTMX outerHTML swap.

    To re-render without duplicating the dashboard's query+attr logic (gap-14 lesson: a
    duplicated elapsed-seconds copy crashed this very table once), extract a module-level
    `async def build_recent_scans(session) -> list[ScanBatch]` that runs the
    `status != 'live'` / desc / limit(10) query and attaches `_agent_name`,
    `_elapsed_seconds`, `_seconds_since_progress`, `_is_stalled` exactly as
    pipeline.dashboard does today. Refactor pipeline.dashboard to call this helper (no
    behavior change). The delete endpoint calls it and returns
    templates.TemplateResponse(name="pipeline/partials/recent_scans_table.html",
    context={"request": request, "recent_scans": rows}). Decide helper location: put it in
    pipeline_scans.py and import into pipeline.py (pipeline.py already imports the
    elapsed/stall helpers from pipeline_scans, so no new import cycle). Full type hints,
    mypy-strict, ruff clean.

    Tests (RED first) extend tests/test_routers/test_pipeline_scans.py using the existing
    `smoke` fixture: assert 200 + deleted-row-absent for a completed batch; 404 for a random
    uuid; 409 for a live batch; 409 for a running batch; confirm the DB row count drops only
    for the deleted batch.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr5-delete-scans && uv run pytest tests/test_routers/test_pipeline_scans.py -x</automated>
  </verify>
  <done>DELETE endpoint returns 200 + re-rendered table for terminal scans, 404 unknown, 409 for live and running; cascade + commit + structlog INFO fire on success; dashboard re-renders via the shared build_recent_scans helper with no regression.</done>
</task>

<task type="auto">
  <name>Task 3: Recent Scans delete control + docs</name>
  <files>src/phaze/templates/pipeline/partials/recent_scans_table.html, tests/test_routers/test_pipeline_scans.py, docs/api.md, README.md</files>
  <action>
    Add an "Actions" column to recent_scans_table.html: a `<th scope="col" class="px-4 py-3">Actions</th>`
    header after "Elapsed", and a trailing `<td>` per row. Render a delete control ONLY when
    `batch.status in ['completed', 'failed']` (terminal). The control is a small trash/✕ button
    matching the existing dark-mode Tailwind aesthetic (e.g. red-tinted icon button), with:
      hx-delete="/pipeline/scans/{{ batch.id }}"
      hx-confirm="Delete this scan and all associated data? This cannot be undone."
      hx-target="#recent-scans"
      hx-swap="outerHTML"
      aria-label="Delete scan {{ batch.scan_path }}"
    Running rows render an empty actions cell (no button); live rows never appear in this table.
    Bump the failed-error `<tr>` colspan from 6 to 7 to account for the new column. Keep the
    existing PR4 activity-indicator markup intact.

    Add a UI render assertion to test_pipeline_scans.py: render the table (or hit /pipeline/)
    with a completed batch and a running batch seeded; assert `hx-delete` is present for the
    completed row and absent for the running row.

    Docs: in docs/api.md under "## Pipeline Scans (`/pipeline/scans`)", add a table row:
    `| DELETE | `/pipeline/scans/{batch_id}` | Delete a terminal scan + all associated DB data (HTMX) |`
    and a short note that `live`/`running` scans are not deletable (409). Update the relevant
    Recent Scans description in README.md to mention the delete capability. Keep README badges
    on one line; do not re-add removed badges.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr5-delete-scans && uv run pytest tests/test_routers/test_pipeline_scans.py -x && uv run ruff check . && uv run ruff format --check .</automated>
  </verify>
  <done>Terminal rows show an accessible delete button wired to DELETE with hx-confirm and #recent-scans outerHTML swap; running/live rows show none; error-row colspan is 7; docs/api.md and README document the new endpoint.</done>
</task>

</tasks>

<verification>
Full gate before PR:

```bash
cd /Users/Robert/Code/public/phaze-pr5-delete-scans
uv run pytest --cov --cov-report=term-missing   # >= 85% coverage
uv run ruff check .
uv run ruff format --check .
uv run mypy .
pre-commit run --all-files                        # frozen hooks; never --no-verify
```

Cascade-scoping is the load-bearing invariant: the service test MUST prove a sibling
batch's full graph and a cross-batch companion FILE survive deletion.
</verification>

<success_criteria>
- Operator deletes a terminal scan from Recent Scans; row vanishes via HTMX swap.
- Every descendant row (metadata, analysis, fingerprint_results, proposals,
  execution_log, tracklists -> versions -> tracks -> discogs_links, tag_write_log,
  file_companions, files) for that batch is removed in ONE transaction.
- No other batch's data is touched; cross-batch companion files survive (only the
  join row dies).
- `live` and `running` batches return 409 and are never deletable; only terminal rows
  expose a delete control.
- No migration added (application-level cascade).
- >= 85% coverage; ruff/mypy/pre-commit clean; many small atomic commits
  (service; endpoint+helper; UI+docs).
</success_criteria>

<output>
Create `.planning/quick/260609-pr5-delete-scans/SUMMARY.md` when done.
</output>
