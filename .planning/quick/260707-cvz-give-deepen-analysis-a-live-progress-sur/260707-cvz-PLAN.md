---
phase: quick-260707-cvz
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/templates/pipeline/partials/deepen_progress.html
  - src/phaze/templates/pipeline/partials/deepen_response.html
  - src/phaze/routers/pipeline.py
  - tests/shared/routers/test_pipeline.py
autonomous: true
requirements: [DEEPEN-PROGRESS-01]

must_haves:
  truths:
    - "Clicking 'Deepen analysis' returns a fragment that polls in place and shows live 'N/M windows' progress"
    - "Progress surface reaches a terminal 'done' state and stops polling when the deepen re-run completes"
    - "not_found / no_active_agent branches remain static one-liners (no polling, no enqueue change)"
    - "A stale pre-click sampled result is never shown as 'complete' (timestamp-gated terminal state)"
  artifacts:
    - path: "src/phaze/templates/pipeline/partials/deepen_progress.html"
      provides: "Three-state (queued/running/complete) self-polling HTMX fragment with terminal-halt"
    - path: "src/phaze/routers/pipeline.py"
      provides: "GET /pipeline/files/{file_id}/deepen-progress poll endpoint + since wiring in deepen POST"
  key_links:
    - from: "deepen_response.html (success branch)"
      to: "/pipeline/files/{file_id}/deepen-progress?since={epoch}"
      via: "hx-get + hx-trigger load,every 2s + hx-swap=outerHTML"
    - from: "deepen_progress GET endpoint"
      to: "AnalysisResult.analysis_completed_at / fine_windows_analyzed / fine_windows_total"
      via: "select on file_id, timestamp-gated completion predicate"
---

<objective>
Give the "Deepen analysis" action a live, in-place progress surface. Today the deepen
success path returns a single static line ("Re-analysis queued at full window budget") and
the operator sees no progress. After this change the `#deepen-result-{file_id}` anchor shows a
self-polling fragment that renders the same `N/M windows` idiom used in analyze_workspace.html
("Re-analyzing · 34/62 windows"), then a terminal "Deepen complete" state that stops polling.

Purpose: answer the user's question "where do I see progress after clicking Deepen?" in-place.
Output: one new poll endpoint, one new fragment, a wired success branch, and route/render tests.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md

<key_facts>
Completion contract (verified in code, load-bearing):
- `agent_analysis.post_analysis_progress` (routers/agent_analysis.py:259) is a COUNTER-ONLY upsert.
  On the deepen re-run's START call it writes `(fine_windows_analyzed=0, fine_windows_total=N)` and
  overwrites ONLY those two columns via `on_conflict_do_update` — it does NOT touch
  `analysis_completed_at`. So during a re-deepen of an already-ANALYZED file, `analysis_completed_at`
  keeps its OLD (pre-click) value until completion. DO NOT gate "running" on completed_at being NULL.
- `agent_analysis.put_analysis` (routers/agent_analysis.py:241) is the ONLY writer that stamps
  `analysis_completed_at = func.now()` (in the same tx it flips FileState.ANALYZED). This is the
  single monotonic completion signal.
- `AnalysisResult` (models/analysis.py): `fine_windows_analyzed`/`fine_windows_total` (both nullable),
  `analysis_completed_at` (nullable, tz-aware), `sampled` (nullable bool).
- Existing `N/M` idiom to mirror: analyze_workspace.html:79-90 — "running · %s/%s windows".

Established HTMX self-poll idiom to REUSE (three-state terminal-halt, Pitfall 6):
- scan_progress_card.html + pipeline_scans.scan_progress (routers/pipeline_scans.py:200): the
  in-progress branch carries `hx-get` + `hx-trigger="every 2s"` + `hx-swap="outerHTML"`; the terminal
  branches OMIT all three, so the outerHTML swap removes the trigger and HTMX halts automatically.

Router facts:
- `router = APIRouter(tags=["pipeline"])` — NO prefix; routes are full paths (e.g. the deepen POST is
  `/pipeline/files/{file_id}/deepen`, pipeline.py:877).
- Deepen button + anchor: analysis_timeline.html:9-16 — button POSTs to the deepen endpoint with
  `hx-target="#deepen-result-{{ file_id }}"`, `hx-swap="innerHTML"`; anchor is
  `<span id="deepen-result-{{ file_id }}" aria-live="polite">`.
</key_facts>

<completion_predicate>
COMPLETION PREDICATE (exact — use verbatim in the endpoint):

    requested_at = datetime.fromtimestamp(since, tz=UTC)   # `since` = deepen-click epoch seconds
    complete = (analysis is not None
                and analysis.analysis_completed_at is not None
                and analysis.analysis_completed_at > requested_at)

Rationale: `since` is captured at click time and threaded through the poll URL. A stale pre-click
sampled result has completed_at <= requested_at ⇒ NOT complete (kills the misleading-complete edge).
A fresh put_analysis stamps func.now() > requested_at ⇒ complete. Robust against "not started yet".

STATE MACHINE for the fragment (evaluated in order):
1. file missing (FileRecord is None)                      -> terminal "gone", no poll.
2. complete (predicate above true)                         -> terminal "Deepen complete", no poll.
3. fine_total truthy AND fine_analyzed < fine_total        -> RUNNING "Re-analyzing · {a}/{t} windows", poll.
4. otherwise (stale/equal counts, job not started yet)     -> "Queued — starting deepen…", poll.
Counts are numeric-only (autoescaped ints, XSS-safe). Guard None counts to 0 for display.
</completion_predicate>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add the three-state self-polling deepen_progress fragment + wire the success branch</name>
  <files>src/phaze/templates/pipeline/partials/deepen_progress.html, src/phaze/templates/pipeline/partials/deepen_response.html</files>
  <action>
Create `deepen_progress.html` — the poll target fragment. Mirror scan_progress_card.html's
terminal-halt idiom EXACTLY: non-terminal branches carry
`hx-get="/pipeline/files/{{ file_id }}/deepen-progress?since={{ since }}"`,
`hx-trigger="every 2s"`, `hx-swap="outerHTML"`; terminal branches OMIT all three. Root element
in every branch is a `<span>` (the anchor is an inline span) with `aria-live="polite"`. Render the
four states from the context flags the endpoint supplies (booleans `gone`, `complete`, `running` +
ints `fine_done`, `fine_total`):
  - gone: red text "File no longer available — deepen cannot be tracked." (terminal, no poll)
  - complete: green text "Deepen complete — reload to see the updated analysis." (terminal, no poll)
  - running: blue text "Re-analyzing · {{ fine_done }}/{{ fine_total }} windows" (poll)
  - else (queued/starting): amber text "Queued — starting deepen…" (poll)
Render ONLY numeric ints into the counts (never essentia strings); no raw/unescaped output.
NOTE (planner's call, per required_outcome #2): chart auto-refresh is intentionally deferred — the
poll endpoint has file_id only, but analysis_timeline is keyed by proposal_id, so wiring a refetch
would thread proposal_id through the poll (higher risk). The clear "reload to see updated analysis"
message is the accepted low-risk terminal per the task's own fallback clause.

Edit `deepen_response.html`: keep the `not_found` and `no_active_agent` branches EXACTLY as-is
(static one-liners). Replace ONLY the `{% else %}` (success) branch body with a bootstrap poller —
a self-replacing `<span>` that fires the first fetch on load and then hands off to deepen_progress:
`hx-get="/pipeline/files/{{ file_id }}/deepen-progress?since={{ since }}"`,
`hx-trigger="load, every 2s"`, `hx-swap="outerHTML"`, `aria-live="polite"`, initial text
"Queued — starting deepen…". On first swap the bootstrap span is replaced by deepen_progress.html,
which then owns the single poll loop (no double-poll). This requires `file_id` and `since` in the
deepen POST context (added in Task 2).
  </action>
  <verify>
    <automated>uv run python -c "from jinja2 import Environment, FileSystemLoader; e=Environment(loader=FileSystemLoader('src/phaze/templates')); e.get_template('pipeline/partials/deepen_progress.html'); e.get_template('pipeline/partials/deepen_response.html'); print('templates parse OK')"</automated>
  </verify>
  <done>Both templates parse; deepen_progress renders 4 states with hx-trigger present only on running/queued; deepen_response success branch is the bootstrap poller, not_found/no_active_agent unchanged.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Add the deepen-progress GET endpoint and thread `since` into the deepen POST</name>
  <files>src/phaze/routers/pipeline.py</files>
  <behavior>
    - GET /pipeline/files/{file_id}/deepen-progress?since=<epoch>: unknown file_id -> gone=True fragment, no poll trigger.
    - completed_at is None OR <= requested_at, counts stale/equal -> queued fragment (poll present).
    - completed_at is None, fine_analyzed < fine_total -> running fragment "{a}/{t} windows" (poll present).
    - completed_at > requested_at -> complete fragment (no poll trigger).
    - POST /pipeline/files/{file_id}/deepen success path -> context carries `file_id` + numeric `since`; response includes the bootstrap poller.
  </behavior>
  <action>
Add imports to pipeline.py: `from datetime import UTC, datetime` and
`from phaze.models.analysis import AnalysisResult`.

In `deepen_analysis` (pipeline.py:877): compute `since = datetime.now(UTC).timestamp()` (a float)
BEFORE the enqueue block, and add `"file_id": file_id, "since": since` to the TemplateResponse
context. Do NOT change any guard, the enqueue/dedup/routing logic, or the not_found/no_active_agent
branches — `since`/`file_id` are only consumed by the success branch's bootstrap poller.

Add a new endpoint `@router.get("/pipeline/files/{file_id}/deepen-progress", response_class=HTMLResponse)`
`async def deepen_progress(request, file_id: uuid.UUID, since: float, session=Depends(get_session))`.
`since` is a required numeric query param (float) — FastAPI coerces/validates it (a non-numeric value
is a 422, XSS-safe). Body:
  1. Load FileRecord by id; if None -> render deepen_progress.html with `{"gone": True, "complete":
     False, "running": False, "fine_done": 0, "fine_total": 0, "file_id": file_id, "since": since}`.
  2. Load `AnalysisResult` for file_id (scalar_one_or_none).
  3. `requested_at = datetime.fromtimestamp(since, tz=UTC)`.
  4. Apply the COMPLETION PREDICATE verbatim (see <completion_predicate>): compute `complete`.
  5. `fine_done = analysis.fine_windows_analyzed or 0`; `fine_total = analysis.fine_windows_total or 0`
     (None-guarded). `running = (not complete) and fine_total > 0 and fine_done < fine_total`.
  6. Render deepen_progress.html with `{gone: False, complete, running, fine_done, fine_total,
     file_id, since}`.
Place the endpoint adjacent to `deepen_analysis`. Keep mypy-clean (annotate return `-> HTMLResponse`).
  </action>
  <verify>
    <automated>just test-db up >/dev/null 2>&1; uv run pytest tests/shared/routers/test_pipeline.py -k "deepen" -x -q; uv run ruff check src/phaze/routers/pipeline.py; uv run mypy src/phaze/routers/pipeline.py</automated>
  </verify>
  <done>Endpoint returns correct fragment per state; deepen POST success context carries file_id+since; ruff+mypy clean; existing deepen tests still pass.</done>
</task>

<task type="auto">
  <name>Task 3: Route/render tests for the poll endpoint states and the polling success path</name>
  <files>tests/shared/routers/test_pipeline.py</files>
  <action>
Add tests alongside the existing deepen tests (grep `deepen` in this file for fixtures/pattern —
reuse the app/client + async_session + DB-seeding fixtures already there; DB rides the ephemeral
PG/Redis on 5433/6380 via `just test-db`).

Cover the GET /pipeline/files/{file_id}/deepen-progress?since=<epoch> endpoint:
  - queued/starting: seed an AnalysisResult with a pre-click `analysis_completed_at` (<= since) and
    equal counts (e.g. 20/20); assert response contains "Queued" and DOES carry `hx-trigger`.
  - running: seed fine_windows_analyzed < fine_windows_total (e.g. 34/62), completed_at pre-click or
    NULL; assert body contains "34/62 windows" and carries `hx-trigger` (poll active).
  - complete: seed `analysis_completed_at` strictly AFTER the `since` value passed in the query;
    assert body contains "Deepen complete" and does NOT contain `hx-trigger` (poll halted).
  - gone: request with a random unknown file_id (well-formed uuid); assert "no longer available" and
    no `hx-trigger`.
Cover the success path: POST the deepen endpoint for a file WITH an active agent (reuse the existing
success-path fixture) and assert the response body contains the bootstrap poller — `deepen-progress`
in an `hx-get` AND `hx-trigger="load, every 2s"` — proving the success branch now polls (not the old
static "Re-analysis queued" line). Keep the not_found/no_active_agent assertions from existing tests
intact (they must still return the static one-liners).
Choose `since` values as explicit epoch floats and set seeded `analysis_completed_at` relative to them
(tz-aware UTC datetimes) so the boundary (> vs <=) is deterministic.
  </action>
  <verify>
    <automated>just test-db up >/dev/null 2>&1; uv run pytest tests/shared/routers/test_pipeline.py -k "deepen" -q; uv run pytest --cov=src/phaze/routers/pipeline --cov-report=term-missing -q tests/shared/routers/test_pipeline.py | tail -5</automated>
  </verify>
  <done>All four poll-state tests + the polling success-path test pass; pipeline router coverage stays >=90%; no regression in existing deepen tests.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| browser → GET deepen-progress | `file_id` (uuid path) and `since` (float query) cross from client |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-cvz-01 | Tampering | `since` query param | mitigate | Typed `float` param — FastAPI 422s non-numeric; used only in a datetime compare, never rendered raw |
| T-cvz-02 | Information disclosure | window counts in fragment | mitigate | Only numeric ints (None-guarded to 0) rendered; no essentia strings, no raw HTML |
| T-cvz-03 | DoS | 2s self-poll loop | accept | Single-user admin tool; terminal-state outerHTML swap halts the loop (Pitfall 6); gone-state also halts on deleted file |
| T-cvz-04 | Elevation | forged file_id | accept | Read-only progress on an admin-only surface; unknown id returns benign "gone" fragment, never a 500 |
</threat_model>

<success_criteria>
- Clicking "Deepen analysis" returns a self-polling fragment showing live `N/M windows`.
- Poll reaches a terminal "Deepen complete" state and stops (no `hx-trigger` in terminal markup).
- Stale pre-click sampled result never shows "complete" (timestamp-gated predicate).
- not_found / no_active_agent branches and the enqueue/dedup/routing logic are unchanged.
- ruff + mypy + pre-commit clean; pipeline router coverage >=90%; no `--no-verify`.
</success_criteria>

<output>
Create `.planning/quick/260707-cvz-give-deepen-analysis-a-live-progress-sur/260707-cvz-SUMMARY.md` when done.
</output>
