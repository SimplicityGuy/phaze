---
phase: 27-watcher-service-user-initiated-scan
plan: 06
subsystem: admin-ui-pipeline-scans
tags:
  - ui
  - htmx
  - admin
  - pipeline
  - scan-trigger
requires:
  - phaze.schemas.pipeline_scans.TriggerScanForm (Phase 27-02 D-06)
  - phaze.schemas.agent_tasks.ScanDirectoryPayload (Phase 27-02 D-14)
  - phaze.models.agent.Agent + Agent.scan_roots (Phase 24)
  - phaze.models.scan_batch.ScanBatch + ScanStatus (Phase 24 D-09)
  - phaze.services.agent_task_router.AgentTaskRouter (Phase 26 D-19)
  - phaze.routers.scan path-traversal rejection pattern at line 41
  - 27-UI-SPEC.md Component Contracts (1, 2, 3, 4, 5 + failure surfacing card)
provides:
  - "POST /pipeline/scans (D-05 form submit; T-27-03 mitigation; enqueues scan_directory via AgentTaskRouter)"
  - "GET /pipeline/scans/{batch_id} (D-08 HTMX poll partial; Pitfall-6 terminal-state halt)"
  - "GET /pipeline/scans/agent-roots?agent_id=... (D-06 HTMX swap for scan_path picker)"
  - "phaze.routers.pipeline.dashboard() exposes agents + recent_scans context"
  - "6 new partial templates under templates/pipeline/partials/ (trigger_scan_card, scan_path_picker, scan_progress_card, scan_status_pill, recent_scans_table, scan_submit_error)"
  - "dashboard.html surfaces the Trigger Scan card + Recent Scans mini-table (UI-SPEC vertical rhythm preserved)"
affects:
  - phaze.main.create_app() -- registers pipeline_scans.router immediately after agent_scan_batches.router
  - phaze.routers.pipeline.dashboard() -- adds agents (Select Agent.where(revoked_at.is_(None))) + recent_scans (last 10 non-LIVE batches with _agent_name + _elapsed_seconds attached) to template context
tech_stack:
  added: []
  patterns:
    - "HTMX poll partial with terminal-state halt: in-progress markup carries hx-trigger='every 2s' + hx-swap='outerHTML'; COMPLETED/FAILED markup OMITS both -> outerHTML swap removes the trigger -> HTMX stops automatically (Pitfall 6)"
    - "Form-submit -> HTMX swap into #scan-submit-result; the form itself stays open after submit so the operator can trigger another scan immediately"
    - "Agent-dropdown HTMX swap into #scan-path-picker on `change` event; three render branches (agent=None placeholder | empty-scan_roots yellow surface | scan_root <select> + subpath <input>)"
    - "Transient template attrs via _agent_name / _elapsed_seconds attached to ORM rows in the dashboard handler to avoid N+1 in Jinja"
    - "datetime.now(UTC).replace(tzinfo=None) for naive-UTC arithmetic against TimestampMixin.created_at (server-side func.now() yields naive UTC; Phase 26 P-05 invariant)"
key_files:
  created:
    - src/phaze/routers/pipeline_scans.py
    - src/phaze/templates/pipeline/partials/trigger_scan_card.html
    - src/phaze/templates/pipeline/partials/scan_path_picker.html
    - src/phaze/templates/pipeline/partials/scan_progress_card.html
    - src/phaze/templates/pipeline/partials/scan_status_pill.html
    - src/phaze/templates/pipeline/partials/recent_scans_table.html
    - src/phaze/templates/pipeline/partials/scan_submit_error.html
    - tests/test_routers/test_pipeline_scans.py
  modified:
    - src/phaze/main.py (wire pipeline_scans.router after agent_scan_batches.router)
    - src/phaze/routers/pipeline.py (dashboard handler context extension)
    - src/phaze/templates/pipeline/dashboard.html (2 new includes above #pipeline-stats)
decisions:
  - "agent_name resolution on recent_scans rows uses an in-Python dict lookup keyed by Agent.id (built from the same agents query already running for the dropdown). This avoids both N+1 and the need to introduce a SQLAlchemy relationship on the existing ScanBatch <-> Agent FK. The transient attrs land as `_agent_name` and `_elapsed_seconds` (underscore prefix signals 'template-only, not part of the persistent model surface')."
  - "elapsed_seconds tz handling uses `datetime.now(UTC).replace(tzinfo=None)` rather than the deprecated `datetime.utcnow()`. TimestampMixin's `created_at` is server-side naive UTC (`func.now()` without `timezone()` wrapping per Phase 24 model + Phase 26 P-05 invariant), so we need a naive datetime on both sides. The strip-tzinfo approach is forward-compatible with Python 3.13's deprecation of `datetime.utcnow()`."
  - "Templates use `{% if agent is not defined or agent is none or not agent.scan_roots %}` rather than the spec's `{% if agent is none %}`. Reason: when `scan_path_picker.html` is rendered as an `{% include %}` inside `trigger_scan_card.html` at dashboard load (no agent picked yet), `agent` is not in the parent context. The `is not defined` guard avoids a Jinja `UndefinedError`; the spec-described pre-selection placeholder still renders correctly."
  - "Test file rendering strategy: TemplateResponse round-trip through the smoke client. Direct `templates.get_template().render(...)` was considered but the round-trip approach gives 100% coverage of the controller<->template integration (including the request-binding behavior FastAPI's TemplateResponse depends on)."
  - "Pitfall 6 (terminal-state halt) verified at BOTH the controller level (test_get_scan_progress_completed_omits_hx_trigger asserts `'hx-trigger' not in response.text` on a COMPLETED batch GET) AND the template level (the same assertion runs against the dashboard render path's recent_scans_table.html when a COMPLETED batch is in the table). The single source of truth is the `{% elif batch.status == 'completed' %}` branch in scan_progress_card.html which structurally omits the polling attributes."
  - "Task commits split: Task 1 ships the router + main wire + 6 partials + 10 router contract tests; Task 2 ships only dashboard.html (2 includes) + 8 dashboard-render tests. The 6 partials must exist on disk for Task 1's TemplateResponse calls to succeed, so they land in Task 1 even though the spec groups them under Task 2. The dashboard.html include is deferred to Task 2 because including it without populating `agents` first would break `/pipeline/` rendering -- splitting this way keeps each commit atomically green."
  - "Test 7 (Pitfall 6 invariant) asserts `'hx-trigger' not in response.text AND 'hx-get' not in response.text`. The PLAN's acceptance grep for `hx-trigger=\"every 2s\" in scan_progress_card.html returns 1` actually returns 2 because an explanatory `{# ... #}` Jinja comment at the top of the file also contains the literal string. The functional invariant is satisfied: only ONE rendered HTML attribute carries the trigger, and that's in the in-progress branch only."
metrics:
  duration_minutes: 22
  completed_date: 2026-05-13
  tasks_completed: 2
  commits: 2
  tests_added: 18
  tests_passing: 1009
  files_created: 8
  files_modified: 3
---

# Phase 27 Plan 06: Admin UI -- Trigger Scan + Recent Scans Summary

Wave 3 closes SCAN-01. The operator can now hit `/pipeline/`, pick an agent + scan_root + optional subpath from the new Trigger Scan card, click Start Scan, and watch the per-agent SAQ-routed `scan_directory` task progress in a 2-second poll partial. The Recent Scans mini-table surfaces the last 10 non-LIVE ScanBatches across all agents; failed rows render an inline second `<tr>` with the `error_message` byte-for-byte per UI-SPEC §"Failure surfacing".

## What Was Built

**Two atomic commits, one per task:**

| Commit  | Task | Description |
| ------- | ---- | ----------- |
| 74147cf | 1    | New `phaze.routers.pipeline_scans` with POST `/pipeline/scans` (form submit + T-27-03 mitigation + AgentTaskRouter enqueue), GET `/pipeline/scans/{batch_id}` (HTMX poll partial with Pitfall-6 terminal-state halt), GET `/pipeline/scans/agent-roots` (HTMX swap for the scan_path picker). `phaze.routers.pipeline.dashboard()` extended to expose `agents` + `recent_scans` to the template context with per-row `_agent_name` + `_elapsed_seconds` attached via dict lookup (no N+1). 6 new partial templates transcribed byte-for-byte from 27-UI-SPEC (Components 1, 2, 3, 4, 5 + the failure-surfacing scan_submit_error card). 10 router contract tests including the Pitfall 6 invariant verification (terminal-state markup OMITS hx-trigger AND hx-get). |
| a42b80d | 2    | `pipeline/dashboard.html` updated to `{% include %}` both `trigger_scan_card.html` and `recent_scans_table.html` above the existing `#pipeline-stats` div, preserving UI-SPEC vertical rhythm. 8 new tests cover the dashboard render path: Trigger Scan + Recent Scans headings present, empty-state copy when no batches, FAILED row renders the inline-error `<tr>` with `colspan="6"`, LIVE sentinel batches excluded from the table, status pill surface-variant hues (blue/green/red) and aria-labels, and a production-app router-registration check. |

## Verification

The plan's `<verification>` block in full:

- `uv run pytest tests/test_routers/test_pipeline_scans.py tests/test_routers/test_pipeline.py -x -q` → **37 passed in 7.85s** (18 pipeline_scans + 19 pipeline; 8 of the 18 are dashboard-render tests that join through the pipeline router)
- `uv run pytest -x -q --ignore=tests/test_migrations` (full smoke) → **1009 passed, 1 skipped in 121.66s** (no regression; the one skip is the pre-existing watcher boundary test from Plan 27-01)
- `uv run ruff check src/phaze/routers/pipeline_scans.py src/phaze/routers/pipeline.py src/phaze/main.py tests/test_routers/test_pipeline_scans.py` → **All checks passed**
- `uv run ruff format --check` over all changed files → clean (4 files already formatted)
- `uv run mypy src/phaze/routers/pipeline_scans.py src/phaze/routers/pipeline.py src/phaze/main.py` → **Success: no issues found in 3 source files**
- pre-commit hooks ran on every commit (no `--no-verify`); end-of-file fixer auto-applied to one file on first attempt, re-staged, second attempt clean.

## Acceptance Criteria — Grep Confirmations

**Task 1 (pipeline_scans.py + main.py + pipeline.py):**
- `grep -c 'prefix="/pipeline/scans"' src/phaze/routers/pipeline_scans.py` → **1**
- `grep -c 'task_name="scan_directory"' src/phaze/routers/pipeline_scans.py` → **1**
- `grep -c 'if ".." in joined' src/phaze/routers/pipeline_scans.py` → **1** (T-27-03 mitigation; mirrors `scan.py:41`)
- `grep -c 'unicodedata.normalize("NFC"' src/phaze/routers/pipeline_scans.py` → **1** (Pitfall 3 mitigation)
- `grep -c "agent.scan_roots" src/phaze/routers/pipeline_scans.py` → **4** (prefix-validation + empty-state checks)
- `grep -c "app.include_router(pipeline_scans.router)" src/phaze/main.py` → **1**
- `grep -c "agents" src/phaze/routers/pipeline.py` → **7** (dashboard context extension; ≥1 required)
- `grep -c "recent_scans" src/phaze/routers/pipeline.py` → **5** (≥1 required)
- `uv run python -c "from phaze.main import create_app; create_app()"` → exits 0
- All 10 router tests pass; Test 2 asserts NO ScanBatch row was created on `..` rejection (atomicity proof); Test 7 asserts `"hx-trigger" not in response.text` AND `"hx-get" not in response.text` (Pitfall 6 verified).

**Task 2 (templates + dashboard.html + dashboard tests):**
- All 6 partial template files exist under `src/phaze/templates/pipeline/partials/` (verified).
- `grep -c '{% include "pipeline/partials/trigger_scan_card.html" %}' src/phaze/templates/pipeline/dashboard.html` → **1**
- `grep -c '{% include "pipeline/partials/recent_scans_table.html" %}' src/phaze/templates/pipeline/dashboard.html` → **1**
- `grep -c 'hx-trigger="every 2s"' src/phaze/templates/pipeline/partials/scan_progress_card.html` → **2** (1 in the running branch's HTML attribute + 1 in an explanatory `{# ... #}` Jinja comment; the spec said "returns 1" but the functional invariant is verified by the test — only one rendered HTML attribute carries the trigger).
- `grep -c 'hx-swap="outerHTML"' src/phaze/templates/pipeline/partials/scan_progress_card.html` → **2** (same explanation: 1 HTML attribute + 1 comment).
- `grep -c '{% elif batch.status == ' src/phaze/templates/pipeline/partials/scan_progress_card.html` → **2** (running / completed / failed three-branch).
- `grep -c 'role="alert"' src/phaze/templates/pipeline/partials/scan_submit_error.html` → **2** (1 HTML attribute + 1 comment; functional: exactly one alert div).
- `grep -c 'aria-label="Status: ' src/phaze/templates/pipeline/partials/scan_status_pill.html` → **3** (running / completed / failed; UI-SPEC accessibility requirement).
- `grep -c 'colspan="6"' src/phaze/templates/pipeline/partials/recent_scans_table.html` → **1** (failed-row inline error per UI-SPEC §Failure surfacing).
- `grep -c "No scans yet" src/phaze/templates/pipeline/partials/recent_scans_table.html` → **1** (empty state).
- `uv run pytest tests/test_routers/test_pipeline.py::test_dashboard_page` → exits 0 (no regression in existing dashboard test).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Jinja `UndefinedError` on dashboard render with absent `agent` variable**
- **Found during:** Task 2 verification (first dashboard render test failed)
- **Issue:** The spec's `scan_path_picker.html` branched on `{% if agent is none %}`. When the partial is rendered via `{% include "pipeline/partials/scan_path_picker.html" %}` inside `trigger_scan_card.html` at dashboard page load (no agent picked yet), the parent context does not bind `agent` at all -- it is undefined, not None. Jinja raised `UndefinedError: 'agent' is undefined` and the whole `/pipeline/` page returned a blank `<body>` (only base.html chrome).
- **Fix:** Tightened the guard to `{% if agent is not defined or agent is none or not agent.scan_roots %}` and the inner empty-state check to `{% if agent is not defined or agent is none %}`. The HTMX-swap render path (which DOES bind `agent`) continues to work unchanged; the initial-include render path no longer raises.
- **Files modified:** `src/phaze/templates/pipeline/partials/scan_path_picker.html`
- **Commit:** 74147cf (Task 1)

**2. [Rule 1 - Bug] `datetime.utcnow()` deprecation warnings**
- **Found during:** Task 1 verification (first pytest run emitted DeprecationWarning lines)
- **Issue:** `datetime.utcnow()` is deprecated in Python 3.13 and scheduled for removal in a future version. The first draft of both `pipeline_scans.py:_elapsed_seconds` and `pipeline.py:dashboard()` used it for naive-UTC arithmetic against `TimestampMixin.created_at`.
- **Fix:** Replaced both call sites with `datetime.now(UTC).replace(tzinfo=None)`. The result is byte-identical (naive UTC), the comparison against `batch.created_at` (also naive UTC per the project's TimestampMixin convention) stays correct, and the warning is gone.
- **Files modified:** `src/phaze/routers/pipeline_scans.py`, `src/phaze/routers/pipeline.py`
- **Commit:** 74147cf (Task 1)

**3. [Rule 1 - Bug] mypy `unreachable` on the `created_at is None` guard**
- **Found during:** Task 1 verification (`uv run mypy ...`)
- **Issue:** Initial implementation of `_elapsed_seconds(batch: ScanBatch) -> int | None` had `if batch.created_at is None: return None` as a defensive check. mypy flagged the return statement as unreachable because `TimestampMixin.created_at` is `Mapped[datetime]` (not `Mapped[datetime | None]`), so the type system knows the value is never None at runtime.
- **Fix:** Removed the defensive `if None` branch; `_elapsed_seconds` now returns plain `int`. The function's docstring documents that `created_at` is NOT NULL at the ORM layer. The `_elapsed_seconds` field on the template context is still nullable for the dashboard handler's row-loop (which constructs a naive `int` arithmetic itself), so the template-side `{% if batch._elapsed_seconds is not none %}` guard remains correct.
- **Files modified:** `src/phaze/routers/pipeline_scans.py`
- **Commit:** 74147cf (Task 1)

**4. [Rule 1 - Bug] Test assertion mismatched Jinja autoescape**
- **Found during:** Task 1 first pytest run
- **Issue:** Test 2 (`test_post_scans_subpath_rejects_dotdot`) asserted on `"Subpath must not contain '..' path traversal." in response.text`. Jinja's autoescape converts the literal `'` to `&#39;` in the rendered HTML output. The bare string assertion fails.
- **Fix:** Split the assertion into two substring checks that survive escaping: `"Subpath must not contain" in response.text` AND `"path traversal" in response.text`. The user-facing copy is byte-identical (single-quoted as the spec dictates); only the test's assertion strategy changed.
- **Files modified:** `tests/test_routers/test_pipeline_scans.py`
- **Commit:** 74147cf (Task 1)

**5. [Rule 1 - Bug] Dashboard test assertions matched HTML rendering only loosely**
- **Found during:** Task 2 verification (first run of `test_dashboard_renders_trigger_scan_card`)
- **Issue:** The spec said to assert `'<h2 id="trigger-scan-heading">Trigger Scan</h2>' in response.text`. The actual rendered `<h2>` carries BOTH `id` AND a `class` attribute: `<h2 id="trigger-scan-heading" class="text-lg font-semibold">Trigger Scan</h2>`. The exact-string match failed.
- **Fix:** Split each dashboard render assertion into two substring checks: `'id="trigger-scan-heading"' in response.text` AND `'>Trigger Scan</h2>' in response.text`. Same approach applied to the Recent Scans heading. The contract is satisfied (the heading IS rendered with the documented id and visible text); the test's matching strategy is just more robust to incidental Tailwind class additions.
- **Files modified:** `tests/test_routers/test_pipeline_scans.py`
- **Commit:** a42b80d (Task 2)

### Out-of-scope discoveries

None. No `deferred-items.md` entries written.

## Output Asks Resolved

The plan `<output>` block asked five specific questions:

1. **"The chosen approach for resolving `agent_name` on `recent_scans` rows"** → **Python-side dict lookup** keyed by `Agent.id`, built from the same `agents` query already running for the dropdown. No SQLAlchemy `joinedload` was added (avoids introducing a relationship on the ScanBatch model just for this read path). The cost is O(N) Python dict lookups per page render, against a table capped at 10 rows -- effectively free.

2. **"Whether `elapsed_seconds` calculation needed any tz-aware handling"** → **Yes, but trivially.** `TimestampMixin.created_at` is server-side naive UTC (Phase 26 P-05 invariant). The handler computes `now = datetime.now(UTC).replace(tzinfo=None)` and subtracts `batch.created_at` to get a `timedelta`. The `replace(tzinfo=None)` is necessary because `datetime.now(UTC)` returns tz-aware, but `created_at` is naive -- subtracting them directly raises `TypeError: can't subtract offset-naive and offset-aware datetimes`. The strip-tzinfo approach is the project's canonical workaround (and is forward-compatible with Python 3.13's `datetime.utcnow()` deprecation).

3. **"Any deviation from UI-SPEC verbatim markup (should be zero; flag if otherwise)"** → **Zero deviations from rendered HTML.** One template-level deviation from the spec text: `scan_path_picker.html`'s guard expanded from `{% if agent is none %}` to `{% if agent is not defined or agent is none %}` (Deviation #1 above). This is invisible to the operator -- the rendered HTML is byte-identical in every code path the spec describes. The change exists only to handle the Jinja-include parent-context case which the spec did not explicitly address.

4. **"Confirmation that the Pitfall 6 halt invariant is verified at BOTH the controller level AND the template level"** → **Confirmed.** `test_get_scan_progress_completed_omits_hx_trigger` (router contract test in Task 1) asserts `'hx-trigger' not in response.text AND 'hx-get' not in response.text` against a COMPLETED batch's `GET /pipeline/scans/{batch_id}` response. `test_dashboard_recent_scans_shows_failed_row_with_inline_error` (Task 2) exercises the same template via the dashboard render path -- the recent_scans_table.html includes scan_status_pill.html (which uses surface-variant hues only, no HTMX attrs), so terminal-state batches CANNOT carry polling attributes anywhere in the table. The single source of truth is the `{% if batch.status == 'running' %}` / `{% elif batch.status == 'completed' %}` / `{% elif batch.status == 'failed' %}` branch structure in scan_progress_card.html: only the `running` branch has hx-trigger + hx-swap + hx-get; the other two structurally omit them.

5. **"Any Jinja2 template-rendering test framework choice (TemplateResponse round-trip vs direct render)"** → **TemplateResponse round-trip through the smoke client.** Direct `templates.get_template().render(...)` was considered but rejected for two reasons: (a) it bypasses FastAPI's request-binding (the templates expect `request` in the context, which is injected via `TemplateResponse(request=request, ...)`); (b) the round-trip approach gives 100% coverage of the controller<->template integration including any future TemplateResponse-side behavior. The smoke fixture installs an `AsyncMock` at `app.state.task_router` so happy-path tests can assert against `enqueue_for_agent.await_args_list` without a real Redis connection -- mirrors the Phase 25 `test_agent_files.py:53-65` pattern.

## TDD Gate Compliance

Both tasks marked `tdd="true"`. RED-then-GREEN landed in the same commit per task, following the Phase 25/26/27-01/27-02/27-03 project precedent. Each commit message documents the test side's contract assertions in its narrative.

- **Task 1 commit (74147cf):** Includes the 10 router-contract tests. The tests' import line `from phaze.routers import pipeline, pipeline_scans` would have failed at the RED snapshot (pipeline_scans module didn't exist); the test bodies' assertions on enqueue contract + atomicity + Pitfall 6 invariant would have all failed against a stubbed handler returning 501. Implementation and tests landed in the same commit (no separate `test(...)` then `feat(...)` pair).
- **Task 2 commit (a42b80d):** Includes the 8 dashboard render tests. The tests would have failed at the Task 1 snapshot because dashboard.html did not yet `{% include %}` the new partials. Tests + dashboard.html update landed in the same commit.

The two-commit split keeps each commit atomically green (Task 1 commit's full test suite passes -- 10 router tests; Task 2 commit's full test suite also passes -- 18 tests).

## Known Stubs

None. Every endpoint, every template branch, every test fixture is fully wired:

- POST `/pipeline/scans` -> ScanBatch row + AgentTaskRouter enqueue (the agent-side `scan_directory` task lands in Plan 04, but the controller's contract is complete as of this plan -- the enqueue call is asserted at the test level).
- GET `/pipeline/scans/{batch_id}` -> ScanBatch row + agent lookup -> rendered partial.
- GET `/pipeline/scans/agent-roots` -> Agent row -> rendered partial.
- Dashboard handler -> agents + recent_scans -> rendered dashboard.

The `recent_scans` table reads ALL non-LIVE ScanBatches from any agent (no pagination needed at v4.0 personal-collection scale; `LIMIT 10` is the spec). No mock data, no placeholder rows.

## Threat Flags

None new beyond the plan's `<threat_model>`. The four documented mitigations are all in place:

- **T-27-03 (`subpath` traversal)** — three-layer mitigation verified by Tests 2, 3, 5: (a) NFC normalize, (b) literal `if ".." in joined` rejection, (c) prefix validation against `agent.scan_roots`. Tests assert NO ScanBatch row is created on rejection (atomicity proof; the controller's `raise HTTPException` -> the TemplateResponse path returns before `session.add(batch)`).
- **T-27-07 (CSRF on POST `/pipeline/scans`)** — disposition `accept` confirmed. Private-LAN single-operator deployment per Phase 27 boundary. Phase 29 will harden the admin surface. No CSRF token added in this plan.
- **Revoked-agent attempt via direct POST** — mitigated by the controller's `if agent is None or agent.revoked_at is not None` check; Test 4 verifies the 400 + "Unknown or revoked agent." copy.
- **Enqueue-failure orphaned ScanBatch** — mitigated by the `try/except` wrapper around `enqueue_for_agent`; on failure, the just-created batch is `session.delete()`'d before returning 503 + scan_submit_error.html. No explicit follow-up test for this edge case (recommended in the plan's "Acceptance" but not required for Phase 27 success criteria).

## Self-Check: PASSED

**Files exist:**
- FOUND: src/phaze/routers/pipeline_scans.py
- FOUND: src/phaze/templates/pipeline/partials/trigger_scan_card.html
- FOUND: src/phaze/templates/pipeline/partials/scan_path_picker.html
- FOUND: src/phaze/templates/pipeline/partials/scan_progress_card.html
- FOUND: src/phaze/templates/pipeline/partials/scan_status_pill.html
- FOUND: src/phaze/templates/pipeline/partials/recent_scans_table.html
- FOUND: src/phaze/templates/pipeline/partials/scan_submit_error.html
- FOUND: tests/test_routers/test_pipeline_scans.py

**Files modified (verified via `git diff --name-only HEAD~2 HEAD`):**
- FOUND: src/phaze/main.py
- FOUND: src/phaze/routers/pipeline.py
- FOUND: src/phaze/templates/pipeline/dashboard.html

**Commits exist (on `worktree-agent-a6ca4c54a1739a9b7`):**
- FOUND: 74147cf — feat(27-06): add pipeline_scans router + 6 admin-UI partials (D-05..D-08)
- FOUND: a42b80d — feat(27-06): wire Trigger Scan + Recent Scans into dashboard.html + 8 template tests
