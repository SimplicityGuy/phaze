---
type: quick
slug: pr4-scan-activity-indicator
branch: feat/scan-activity-indicator
worktree: /Users/Robert/Code/public/phaze-pr4-activity
created: 2026-06-09
pr: 4 of 5 (scan-reliability series)
autonomous: true
files_modified:
  - src/phaze/models/scan_batch.py
  - alembic/versions/017_add_scan_batches_last_progress_at.py
  - src/phaze/routers/agent_scan_batches.py
  - src/phaze/routers/pipeline_scans.py
  - src/phaze/services/ingestion.py
  - src/phaze/config.py
  - src/phaze/tasks/scan_reaper.py
  - src/phaze/tasks/controller.py
  - src/phaze/routers/pipeline.py
  - src/phaze/templates/pipeline/partials/recent_scans_table.html
  - src/phaze/templates/pipeline/partials/scan_progress_card.html
  - tests/test_migrations/test_017_upgrade.py
  - tests/test_tasks/test_scan_reaper.py
  - tests/test_routers/test_agent_scan_batches.py
  - tests/test_routers/test_pipeline.py
  - tests/test_routers/test_pipeline_scans.py
  - tests/test_template_helpers/test_progress_partial.py
  - .env.example
  - docs/configuration.md
  - README.md

must_haves:
  truths:
    - "A RUNNING scan that is actually progressing shows a green pulsing dot + '·Ns ago' in both the Recent Scans table and the in-progress poll card."
    - "A RUNNING scan that has gone quiet (past the UI warn threshold) flips to an amber 'stalled?' treatment before the reaper kills it."
    - "Every agent PATCH that advances progress/status stamps last_progress_at; both scan-create paths stamp it on create."
    - "A genuinely-dead RUNNING scan (no progress for scan_stall_seconds) is auto-marked FAILED with a 'stalled' error_message and a frozen completed_at by a control-side cron."
    - "A LIVE sentinel batch is NEVER touched by the reaper."
  artifacts:
    - path: "alembic/versions/017_add_scan_batches_last_progress_at.py"
      provides: "Adds nullable tz-aware last_progress_at column + backfills existing rows to updated_at"
    - path: "src/phaze/tasks/scan_reaper.py"
      provides: "reap_stalled_scans(ctx) control-side SAQ cron handler"
    - path: "src/phaze/models/scan_batch.py"
      provides: "last_progress_at Mapped[datetime | None] column"
  key_links:
    - from: "src/phaze/tasks/controller.py"
      to: "reap_stalled_scans"
      via: "CronJob in settings['cron_jobs']"
      pattern: "CronJob\\(reap_stalled_scans"
    - from: "src/phaze/routers/agent_scan_batches.py"
      to: "batch.last_progress_at"
      via: "stamp on every non-no-op applied PATCH"
      pattern: "last_progress_at = datetime.now\\(UTC\\)"
    - from: "src/phaze/routers/pipeline.py"
      to: "recent_scans_table.html"
      via: "_seconds_since_progress / _is_stalled transient attrs"
      pattern: "_is_stalled"
---

<objective>
PR4 of 5 in the scan-reliability series. Make it visibly obvious whether a
"RUNNING" scan is actually progressing, and auto-fail genuinely-dead scans.

Three mechanisms, one column:
1. A `last_progress_at` heartbeat column on `ScanBatch`, stamped at every point a
   scan makes real progress (agent PATCH, both create paths, terminal updates).
2. A control-side SAQ cron (`reap_stalled_scans`) that marks RUNNING batches with
   no progress for `scan_stall_seconds` as FAILED.
3. A UI activity indicator (pulsing green dot + "·Ns ago", amber "stalled?" when
   quiet) in the Recent Scans table and the in-progress poll card.

Purpose: the operator asked for "some indicator that the running is actually
running" and an auto-fail for dead scans. This closes the "is it working or
hung?" ambiguity and stops orphan RUNNING rows from lingering forever.

Output: migration 017, model column, stamping at all progress points, the reaper
task + config knob + cron registration, UI changes, tests (>=85% cov), docs.

Scope guard: activity indicator + stall reaper + `last_progress_at` ONLY. Do NOT
implement delete-scans (that is PR5).
</objective>

<context>
@.planning/STATE.md
@CLAUDE.md

# Files already read during planning (load only if the executor needs them again):
# - src/phaze/models/scan_batch.py         (TimestampMixin, completed_at, ScanStatus, indexes)
# - src/phaze/routers/agent_scan_batches.py (PATCH state-machine; set_fields/no-op logic at L92-124)
# - src/phaze/routers/pipeline_scans.py     (elapsed_seconds helper; trigger_scan create; scan_progress poll)
# - src/phaze/services/ingestion.py         (run_scan create + terminal completed/failed updates)
# - src/phaze/routers/pipeline.py           (dashboard handler; _agent_name/_elapsed_seconds attach at L153-157)
# - src/phaze/config.py                     (BaseSettings; AliasChoices convention; PHAZE_*/BARE/lower)
# - src/phaze/tasks/controller.py           (control startup wires ctx["async_session"]; cron_jobs list)
# - src/phaze/tasks/discogs.py              (canonical `async with ctx["async_session"]() as session:` task pattern)
# - alembic/versions/015_*.py + 016_*.py    (add_column + data-backfill migration patterns; 016 is current head)
# - tests/test_migrations/test_016_upgrade.py (downgrade-to/upgrade-to migration test harness)
# - tests/test_tasks/test_heartbeat_cron.py (cron handler test via hand-built ctx dict)
# - tests/test_template_helpers/test_progress_partial.py (Jinja2Templates render-in-test pattern)

<interfaces>
ScanStatus (src/phaze/models/scan_batch.py): StrEnum RUNNING="running" COMPLETED="completed" FAILED="failed" LIVE="live".
ScanBatch columns today: id(UUID), agent_id, scan_path, status, total_files, processed_files,
  error_message, completed_at(tz-aware nullable). Inherits TimestampMixin -> created_at/updated_at (tz-aware, NOT NULL).
elapsed_seconds(batch) (src/phaze/routers/pipeline_scans.py): tz-aware-safe; pipeline.py imports it.
Control worker ctx (src/phaze/tasks/controller.py startup): ctx["async_session"] = async_sessionmaker(...).
  Canonical task body: `async with ctx["async_session"]() as session: ...; await session.commit()`.
SAQ cron forms VERIFIED in repo:
  - 5-field standard:        refresh_tracklists cron="0 3 1 * *"   (controller.py:124)
  - 6-field trailing-seconds (croniter): heartbeat_tick cron="* * * * * */30" (agent_worker.py:203)
config.py AliasChoices convention (3 names): ("PHAZE_X", "X", "x") — see database_url L143-146.
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Migration 017 + model column (last_progress_at)</name>
  <files>alembic/versions/017_add_scan_batches_last_progress_at.py, src/phaze/models/scan_batch.py</files>
  <action>
Add `last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)` to
`ScanBatch` (place it next to `completed_at`, with a docstring comment explaining it is the per-progress
heartbeat that drives the UI activity indicator + the stall reaper; tz-aware to match TimestampMixin).

Create migration 017 mirroring the 015 add-column pattern AND the 016 backfill pattern in ONE migration:
revision="017", down_revision="016", Create Date 2026-06-09.
  - upgrade(): `op.add_column("scan_batches", sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True))`
    then `op.execute("UPDATE scan_batches SET last_progress_at = updated_at WHERE last_progress_at IS NULL")`
    so existing rows get a sane non-NULL heartbeat (raw SQL, static literals only — no injection surface).
  - downgrade(): `op.drop_column("scan_batches", "last_progress_at")`.
No NOT NULL constraint. 016 is the current alembic head (verified) so 017 chains cleanly.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr4-activity && uv run alembic heads 2>&1 | grep -q 017 && uv run python -c "import phaze.models.scan_batch as m; assert 'last_progress_at' in m.ScanBatch.__table__.c, 'column missing'" && echo OK</automated>
  </verify>
  <done>`alembic heads` shows a single head 017; ScanBatch model exposes last_progress_at; mypy + ruff clean.</done>
  <commit>feat(scan): add scan_batches.last_progress_at heartbeat column (migration 017)</commit>
</task>

<task type="auto">
  <name>Task 2: Stamp last_progress_at at every progress point</name>
  <files>src/phaze/routers/agent_scan_batches.py, src/phaze/routers/pipeline_scans.py, src/phaze/services/ingestion.py</files>
  <action>
Stamp `last_progress_at = datetime.now(UTC)` (same style as PR2's completed_at) wherever a scan advances:

1. agent_scan_batches.py `patch_scan_batch` — PRIMARY heartbeat (the agent `scan_directory` task PATCHes
   processed_files each chunk). The same-state no-op echo at step 3 (L92-95) returns BEFORE any write and
   MUST NOT stamp. After the `for field, value in set_fields.items()` apply loop (L114-116) and BEFORE the
   completed_at block, stamp `batch.last_progress_at = datetime.now(UTC)` — we only reach there on a real
   applied PATCH (set_fields non-empty / not a same-state no-op). Keep ordering: apply set_fields -> stamp
   last_progress_at -> conditionally stamp completed_at -> commit/refresh. (datetime/UTC imported at L26.)

2. pipeline_scans.py `trigger_scan` POST — when constructing the RUNNING `ScanBatch` (L243-250), pass
   `last_progress_at=datetime.now(UTC)` so a freshly-created batch starts with a heartbeat.

3. ingestion.py `run_scan` (legacy path) — set `last_progress_at=datetime.now(UTC)` on the create
   `ScanBatch(...)` (L149-156), and add `last_progress_at=datetime.now(UTC)` to BOTH terminal update
   .values() blocks (COMPLETED L186-190 and FAILED L205-209), consistent with the existing completed_at
   stamping there.

No fenced code in this plan — follow the existing `datetime.now(UTC)` call sites already in each file.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr4-activity && grep -v '^[[:space:]]*#' src/phaze/routers/agent_scan_batches.py | grep -c "last_progress_at = datetime.now(UTC)" | grep -q 1 && grep -v '^[[:space:]]*#' src/phaze/routers/pipeline_scans.py | grep -q "last_progress_at=datetime.now(UTC)" && grep -c "last_progress_at=datetime.now(UTC)" src/phaze/services/ingestion.py | grep -q 3 && uv run ruff check src/phaze/routers/agent_scan_batches.py src/phaze/routers/pipeline_scans.py src/phaze/services/ingestion.py && echo OK</automated>
  </verify>
  <done>Agent PATCH stamps last_progress_at on real (non-no-op) PATCHes; both create paths stamp on create; run_scan stamps on create + both terminal updates. ruff/mypy clean.</done>
  <commit>feat(scan): stamp last_progress_at on every scan progress point</commit>
</task>

<task type="auto">
  <name>Task 3: scan_stall_seconds config + stall reaper task + cron registration</name>
  <files>src/phaze/config.py, src/phaze/tasks/scan_reaper.py, src/phaze/tasks/controller.py</files>
  <action>
(a) config.py — add to `BaseSettings` (so the control role inherits it): `scan_stall_seconds: int = 600`
    with `validation_alias=AliasChoices("PHAZE_SCAN_STALL_SECONDS", "SCAN_STALL_SECONDS", "scan_stall_seconds")`
    and a description ("Seconds with no progress before a RUNNING scan is reaped as stalled."). Match the
    3-name AliasChoices convention used by database_url/redis_url.

(b) Create src/phaze/tasks/scan_reaper.py — `async def reap_stalled_scans(ctx: dict[str, Any]) -> dict[str, int]`.
    CONTROL-ONLY (needs Postgres via ctx["async_session"]; the agent worker is Postgres-free and CANNOT run
    it). Module docstring must state that boundary. Logic:
      - `cfg = get_settings()`, `threshold = cfg.scan_stall_seconds`, `now = datetime.now(UTC)`,
        `cutoff = now - timedelta(seconds=threshold)`.
      - `async with ctx["async_session"]() as session:` select ScanBatch rows where
        `ScanBatch.status == ScanStatus.RUNNING.value` AND
        `func.coalesce(ScanBatch.last_progress_at, ScanBatch.updated_at, ScanBatch.created_at) < cutoff`.
        NEVER match 'live' — the explicit `status == RUNNING` predicate guarantees LIVE/COMPLETED/FAILED are
        excluded.
      - For each stalled row: `ref = batch.last_progress_at or batch.updated_at or batch.created_at`
        (assume-UTC if tz-naive, mirroring elapsed_seconds), `seconds_since = int((now - ref).total_seconds())`;
        set `batch.status = ScanStatus.FAILED.value`, `batch.error_message = f"stalled: no progress for {threshold}s"`,
        `batch.completed_at = now`. Emit `logger.warning("scan reaped: stalled", batch_id=str(batch.id),
        scan_path=batch.scan_path, seconds_since_progress=seconds_since)` (structlog — project standard).
      - `await session.commit()`; return `{"reaped": len(rows)}`.
    Use `logger = structlog.get_logger(__name__)`.

(c) controller.py — import `reap_stalled_scans` and register it in BOTH `settings["functions"]` (so SAQ can
    execute it) and `settings["cron_jobs"]`: `CronJob(reap_stalled_scans, cron="* * * * *")  # type: ignore[type-var]`
    — every-minute cadence using the VERIFIED 5-field standard cron form (same form as the existing
    refresh_tracklists `"0 3 1 * *"`). Do NOT add this to agent_worker.py (control-vs-agent DB boundary).
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr4-activity && uv run python -c "from phaze.config import get_settings; assert get_settings().scan_stall_seconds == 600" && uv run python -c "import phaze.tasks.scan_reaper as r; assert callable(r.reap_stalled_scans)" && grep -q "CronJob(reap_stalled_scans" src/phaze/tasks/controller.py && grep -q "reap_stalled_scans," src/phaze/tasks/controller.py && uv run ruff check src/phaze/config.py src/phaze/tasks/scan_reaper.py src/phaze/tasks/controller.py && echo OK</automated>
  </verify>
  <done>scan_stall_seconds=600 default + env override works; reaper imports cleanly and uses ctx["async_session"]; registered as a function + every-minute CronJob in controller.py only. ruff/mypy clean.</done>
  <commit>feat(scan): add stall reaper cron + PHAZE_SCAN_STALL_SECONDS knob</commit>
</task>

<task type="auto">
  <name>Task 4: UI activity indicator (handlers + 2 templates)</name>
  <files>src/phaze/routers/pipeline_scans.py, src/phaze/routers/pipeline.py, src/phaze/templates/pipeline/partials/recent_scans_table.html, src/phaze/templates/pipeline/partials/scan_progress_card.html</files>
  <action>
Define the warn threshold ONCE and document it: the UI flips to amber "stalled?" at HALF the reaper's hard
threshold so the operator sees a warning before the reaper kills the scan. e.g. scan_stall_seconds=600 ->
UI warns at 300s.

(a) pipeline_scans.py — add a `seconds_since_progress(batch: ScanBatch) -> int` helper next to
    `elapsed_seconds`, mirroring its tz-aware-safe handling: `ref = batch.last_progress_at or batch.created_at`
    (assume-UTC if tz-naive), return `int((now - ref).total_seconds())`. Add a module-level
    `_UI_STALL_WARN_FRACTION = 0.5` and `is_scan_stalled(batch) -> bool`:
    `batch.status == ScanStatus.RUNNING.value and seconds_since_progress(batch) > int(get_settings().scan_stall_seconds * _UI_STALL_WARN_FRACTION)`.
    (Import get_settings.) Then in BOTH `scan_progress` (poll, L150-159) and `trigger_scan`'s success render
    (L297-306) add to the context: `"seconds_since_progress": seconds_since_progress(batch)` (use 0 in the
    freshly-created trigger_scan case) and `"is_stalled": is_scan_stalled(batch)`.

(b) pipeline.py `dashboard` — in the recent_scans loop (L154-157) also attach per batch:
    `batch._seconds_since_progress = seconds_since_progress(batch)` and `batch._is_stalled = is_scan_stalled(batch)`
    (import both helpers from pipeline_scans alongside the existing elapsed_seconds import). Keep the existing
    _agent_name/_elapsed_seconds attaches.

(c) recent_scans_table.html — in the Status cell (L39-41), keep the existing scan_status_pill.html include
    and, ONLY for `batch.status == 'running'`, render an activity affordance alongside it:
      - progressing (not _is_stalled): a green pulsing dot (e.g.
        `inline-block h-2 w-2 rounded-full bg-green-500 dark:bg-green-400 animate-pulse`, `aria-hidden="true"`)
        plus a small muted "·{{ batch._seconds_since_progress }}s ago" span.
      - stalled (_is_stalled): amber treatment — amber dot + a small `text-amber-600 dark:text-amber-400`
        "stalled?" label.
    Match the existing dark-mode Tailwind aesthetic (text-xs, muted gray for the "ago" text).

(d) scan_progress_card.html RUNNING branch ONLY (L9-25): add a pulsing "live" affordance next to the RUNNING
    pill and a "last activity {{ seconds_since_progress }}s ago" line so a working scan visibly ticks each 2s
    poll; when `is_stalled`, swap to the amber "stalled?" warning treatment. Do NOT touch the completed/failed
    branches — preserve Pitfall-6 (only the running branch carries hx-trigger/hx-get/hx-swap, so terminal
    swaps halt polling). Keep `aria-live="polite"` on the card; the pulse dot is decorative (`aria-hidden`).
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr4-activity && uv run python -c "from phaze.routers.pipeline_scans import seconds_since_progress, is_scan_stalled; print('helpers ok')" && grep -q "animate-pulse" src/phaze/templates/pipeline/partials/recent_scans_table.html && grep -q "_is_stalled" src/phaze/templates/pipeline/partials/recent_scans_table.html && grep -q "animate-pulse" src/phaze/templates/pipeline/partials/scan_progress_card.html && grep -q "is_stalled" src/phaze/templates/pipeline/partials/scan_progress_card.html && uv run ruff check src/phaze/routers/pipeline.py src/phaze/routers/pipeline_scans.py && echo OK</automated>
  </verify>
  <done>RUNNING rows/cards show a green pulsing dot + "·Ns ago"; flip to amber "stalled?" past the half-threshold; terminal branches unchanged (polling still halts). Dashboard + poll + trigger contexts all carry _seconds_since_progress/_is_stalled. ruff/mypy clean.</done>
  <commit>feat(ui): live activity indicator + stalled-scan affordance</commit>
</task>

<task type="auto">
  <name>Task 5: Tests (migration, reaper, stamping, UI) to >=85% coverage</name>
  <files>tests/test_migrations/test_017_upgrade.py, tests/test_tasks/test_scan_reaper.py, tests/test_routers/test_agent_scan_batches.py, tests/test_routers/test_pipeline.py, tests/test_routers/test_pipeline_scans.py, tests/test_template_helpers/test_progress_partial.py</files>
  <behavior>
    - Migration 017: downgrade-to base -> upgrade-to 016, insert a RUNNING row + a row with NULL
      last_progress_at and a known updated_at, upgrade-to 017, assert the column exists and NULL rows are
      backfilled to updated_at; downgrade-to 016 drops the column. Mirror test_016_upgrade.py harness
      (_build_alembic_config / downgrade_to / upgrade_to; cleanup in a finally block).
    - Reaper: stalled RUNNING row (COALESCE(...) older than scan_stall_seconds) -> status FAILED,
      error_message contains "stalled", completed_at set, WARNING logged. Fresh RUNNING row -> untouched.
      LIVE row -> NEVER touched (even if ancient). Boundary: a row exactly at threshold vs just past it.
      Build ctx with ctx["async_session"] = the test sessionmaker (mirror test_heartbeat_cron.py's
      hand-built ctx); reuse the DB fixtures the other test_tasks DB tests use.
    - Stamping: agent PATCH advancing processed_files bumps last_progress_at; a same-state no-op PATCH does
      NOT write/stamp it; trigger_scan create + run_scan create stamp last_progress_at.
    - UI: dashboard handler attaches _seconds_since_progress and _is_stalled with correct values for a fresh
      vs quiet RUNNING batch; recent_scans_table.html renders the green pulse for a progressing running row
      and the amber "stalled?" for a stalled one; scan_progress_card.html RUNNING branch renders the live
      affordance. Render via the Jinja2Templates pattern in test_progress_partial.py (or TestClient).
  </behavior>
  <action>
Write the tests per the behavior block. Extend the existing files where one already covers the target
(test_agent_scan_batches.py, test_pipeline.py, test_pipeline_scans.py, test_progress_partial.py); add new
files for the migration (test_017_upgrade.py) and reaper (test_scan_reaper.py). Reuse existing fixtures and
harnesses — do not invent new DB plumbing. Target >=85% coverage on all changed modules.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr4-activity && uv run pytest tests/test_tasks/test_scan_reaper.py tests/test_routers/test_agent_scan_batches.py tests/test_routers/test_pipeline.py tests/test_routers/test_pipeline_scans.py tests/test_template_helpers/test_progress_partial.py -q && uv run pytest --cov=phaze --cov-report=term-missing -q 2>&1 | tail -5</automated>
  </verify>
  <done>All new/extended tests pass; reaper untouched/stalled/live cases covered; migration up/down verified; stamping + UI attrs/renders asserted; overall coverage >=85%.</done>
  <commit>test(scan): cover last_progress_at, stall reaper, and activity indicator</commit>
</task>

<task type="auto">
  <name>Task 6: Docs — PHAZE_SCAN_STALL_SECONDS + reaper behavior</name>
  <files>.env.example, docs/configuration.md, README.md</files>
  <action>
Document the new knob + reaper behavior (repo rule: docs stay current with code):

1. .env.example — add a commented `# PHAZE_SCAN_STALL_SECONDS=600` near the existing scan/worker block
   (e.g. by `PHAZE_SCAN_CHUNK_SIZE` around L123), with a one-line comment that RUNNING scans with no
   progress for this many seconds are auto-failed as stalled by the control-side reaper cron.

2. docs/configuration.md — add a `PHAZE_SCAN_STALL_SECONDS` (or `SCAN_STALL_SECONDS`) row to the settings
   table (default `600`, No, "Seconds with no progress before a RUNNING scan is reaped as stalled by the
   control worker's every-minute cron."). Note it lives on BaseSettings (both roles parse it, but only the
   control worker runs the reaper). Mention the UI warn threshold = half the hard threshold.

3. README.md — add a brief sentence near the existing scan/logging notes that RUNNING scans show a live
   activity indicator and are auto-failed if they stall (link to the Configuration row). Keep README badges
   on one line; do not re-add removed badges; preserve the GSD marker on line 1 of docs.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr4-activity && grep -q "PHAZE_SCAN_STALL_SECONDS" .env.example && grep -q "SCAN_STALL_SECONDS" docs/configuration.md && grep -qi "stall" README.md && echo OK</automated>
  </verify>
  <done>.env.example, docs/configuration.md, and README.md all document PHAZE_SCAN_STALL_SECONDS + the reaper/indicator behavior. yamllint/pre-commit doc hooks pass.</done>
  <commit>docs(scan): document PHAZE_SCAN_STALL_SECONDS + stall reaper / activity indicator</commit>
</task>

</tasks>

<verification>
Run the full gate before opening the PR (never use --no-verify):

```
cd /Users/Robert/Code/public/phaze-pr4-activity
uv run alembic upgrade head            # 017 applies cleanly on top of 016
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest --cov=phaze --cov-report=term-missing   # >=85%
pre-commit run --all-files             # frozen hooks must pass
```

Manual UI smoke (optional, behind the human-verify equivalent): trigger a scan, confirm the Recent Scans
row + in-progress card show a green pulsing dot and "·Ns ago" while progressing; let one go quiet and
confirm the amber "stalled?" appears before the reaper (default 600s) flips it to FAILED.
</verification>

<success_criteria>
- Migration 017 adds nullable tz-aware `last_progress_at`, backfills existing rows to `updated_at`, and 016->017->016 round-trips.
- `last_progress_at` is stamped on every real agent PATCH (not same-state no-ops), on both scan-create paths, and on run_scan's terminal updates.
- `reap_stalled_scans` marks only RUNNING (never LIVE) batches past `scan_stall_seconds` as FAILED with a "stalled" error_message + frozen completed_at, logs a structlog WARNING, and is wired as an every-minute control-side CronJob (`"* * * * *"`).
- `PHAZE_SCAN_STALL_SECONDS` (default 600) is configurable on BaseSettings via the 3-name AliasChoices convention.
- Recent Scans table + in-progress card show a green pulsing activity dot + "·Ns ago" for progressing RUNNING scans and an amber "stalled?" treatment past the half-threshold; terminal-state polling-halt (Pitfall 6) is preserved.
- Tests cover all of the above; overall coverage >=85%; ruff/mypy/pre-commit clean.
- Scope respected: NO delete-scans work (that is PR5).
</success_criteria>

<risks>
- **SAQ croniter cadence (RESOLVED):** the every-minute reaper uses the 5-field `"* * * * *"` form, which is
  verified in-repo against `refresh_tracklists` (`"0 3 1 * *"`, controller.py:124). The 6-field
  trailing-seconds croniter form (`"* * * * * */30"`, agent_worker.py:203) is only needed for sub-minute
  cadence — not required here. If a faster cadence is ever wanted, use the 6-field form (e.g. `"* * * * * */30"`).
- **Control-vs-agent DB boundary (CRITICAL):** the reaper MUST live on the control role. The agent worker is
  Postgres-free (no `ctx["async_session"]`) and importing/registering the reaper there would fail at runtime.
  Register the CronJob ONLY in controller.py; do not touch agent_worker.py. controller.startup already wires
  `ctx["async_session"]` (verified at controller.py:65).
- **Reaper must never touch LIVE:** the explicit `status == ScanStatus.RUNNING.value` predicate is the guard.
  Do not broaden it to a "not terminal" check, which would sweep up the LIVE watcher sentinel.
- **Agent PATCH stamp placement:** stamp AFTER the same-state no-op early-return (step 3) so an idempotent
  PATCH never bumps last_progress_at (matches the existing completed_at idempotency contract).
- **UI warn threshold < reaper threshold:** intentionally half (`_UI_STALL_WARN_FRACTION = 0.5`) so "looks
  stalled" surfaces before the hard reap. If the operator sets a very small scan_stall_seconds, the warn
  window shrinks proportionally — acceptable.
</risks>

<output>
This is a quick task on worktree branch `feat/scan-activity-indicator`. Execute the tasks in order, committing
at each `<commit>` boundary (many small atomic commits per the repo rule). Open ONE PR for this branch when the
full verification gate passes. Do NOT push to main directly.
</output>
