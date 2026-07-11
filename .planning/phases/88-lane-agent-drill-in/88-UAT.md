---
status: complete
phase: 88-lane-agent-drill-in
source: [88-01-SUMMARY.md, 88-02-SUMMARY.md, 88-03-SUMMARY.md]
started: 2026-07-11
updated: 2026-07-11
method: automated browser drive (Playwright-MCP) against a live app boot (uvicorn + phaze_uat on test PG 5433 / Redis 6380), seeded corpus
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test
expected: App boots from scratch, migrations run, dashboard + agents pages return live data.
result: pass
note: |
  `uvicorn phaze.main:app` against a fresh `phaze_uat` DB with PHAZE_AUTO_MIGRATE=1 — all Alembic
  migrations ran to 037, startup complete, `/` and `/admin/agents` returned HTTP 200. (Tailwind CSS
  was not built in this headless env — a harmless `/static/css/app.css` 404, styling only.)

### 2. DRILL-01 — Lane drill-in (click a backend-lane card)
expected: Clicking a lane card opens GET /pipeline/lanes/{backend_id} showing queues / in-flight / quota / recent completions in the shared #detail-pane.
result: pass
note: |
  Clicked the local lane card (role=button) on /s/analyze → #detail-pane rendered: "🖥️ LOCAL · local
  RANK 99", 0/1 in-flight/cap, liveness "Available — accepting work.", queue depths (analyze/fingerprint/
  meta/io), "No completions in the last 20", and an "Updated HH:MM:SS" own-tick timestamp. Quota/
  inadmissible correctly ABSENT for a local (non-kueue) lane (D-06 kind-adaptivity). URL pushed `?lane=local`.

### 3. DRILL-02 — Agent drill-in (click an agent row)
expected: Clicking an agent row opens GET /admin/agents/{agent_id}/_activity showing owned files grouped by derived stage_status, recent scan batches, per-lane queue depths, and liveness.
result: pass
note: |
  Clicked the dev-agent row (role=button, 9 seeded files) → #detail-pane rendered the liveness header +
  the per-agent 6-stage matrix with counts that exactly match the seeded corpus: Meta ✓8/✗1, FP ✓2/—7,
  Analyze ✓2/—7, Prop ✓1/—8, Appr(=review) ✓1/—8, Exec(=apply) ✓0/—9 — confirming the Appr=review /
  Exec=apply remap AND the real-PG `GROUP BY stage_status_case` aggregate against live data. Queue depths
  + recent scan batches ("live <watcher> … completed /data/music/…") also rendered. URL pushed `?agent=dev-agent`.

### 4. DRILL-03 — Poll-survival + keyboard-accessible, dismissable pane
expected: The drill-in survives the 5s poll (selection carried via URL param, rendered outside the polled region) and is keyboard-accessible (role=button, Enter/Space, focus ring) and dismissable (✕ / Esc).
result: issue
reported: "Browser: clicking/opening a lane or agent detail leaves the pane UN-dismissable — no ✕ Close button appears, Esc does nothing, and the detail's 5s auto-refresh never runs. Console: 'ReferenceError: onLoaded is not defined'."
severity: major
resolution: fixed
note: |
  ROOT CAUSE: `_detail_pane.html` calls `onLoaded()` from `hx-on::after-swap`, but `onLoaded` is an
  Alpine METHOD on the enclosing `<section x-data>`. HTMX `hx-on` evaluates in the GLOBAL scope, where
  a bare `onLoaded()` is undefined → the swap handler throws, `open` never flips true, so: the ✕ Close
  button (x-show="open") stays hidden, the Esc guard (`if (open …)`) is dead, and the body's self-removing
  own-tick (`x-effect="… !open …"`) removes itself immediately → no 5s auto-refresh. Browser-verified via
  Alpine.$data(pane): open=false, close-button hidden, own-tick gone.

  Why every prior gate missed it: markup/httpx tests assert the string `onLoaded()` is PRESENT (it was),
  the code review read templates statically, and the verifier read source — none EXECUTE the JS. Only a
  live browser (this UAT) surfaced it.

  FIX (commit below): `hx-on::after-swap="… Alpine.$data(this).onLoaded()"` reaches the component scope
  explicitly. Browser re-verified on BOTH panes, mouse + keyboard:
    - lane (Enter on role=button) + agent (click): open=true, ✕ Close visible, own-tick persists in DOM,
      heading focused (D-09), selectedId set (aria-current highlight).
    - Esc: open=false, `?param` cleared via history.replaceState, Close re-hidden, focus RETURNED to the
      originating trigger by stable id.
  Regression guard added: tests/shared/core/test_a11y_guards.py::test_detail_pane_after_swap_reaches_alpine_scope
  (mutation-verified — reverting to bare onLoaded() turns it RED).

  Minor cosmetic note (not fixed, not a success-criterion): on dismiss the resting "No … selected" empty
  state does not reappear (the innerHTML swap replaced it) — the pane shows the last content in a
  deselected state. All functional dismiss behaviors (deselect, clear param, stop tick, hide ✕, return
  focus) work.

## Summary

total: 4
passed: 3
issues: 1 (diagnosed + fixed + regression-guarded inline)
pending: 0
skipped: 0
blocked: 0

## Gaps

- truth: "The drill-in pane is keyboard-accessible and dismissable (DRILL-03, success criterion 3)"
  status: resolved
  reason: "hx-on::after-swap called a bare onLoaded() (Alpine method) in the global hx-on scope → ReferenceError → pane never opened/dismissed and the own-tick self-removed. Fixed via Alpine.$data(this).onLoaded(); browser re-verified on both panes (mouse+keyboard) + mutation-verified markup guard added."
  severity: major
  test: 4
  artifacts: [src/phaze/templates/pipeline/partials/_detail_pane.html, tests/shared/core/test_a11y_guards.py]
  missing: []
