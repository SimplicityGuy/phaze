---
phase: 38-pipeline-dag-pause-priority-ui-and-rescan-button-removal
reviewed: 2026-06-13T00:00:00Z
depth: standard
files_reviewed: 4
files_reviewed_list:
  - src/phaze/templates/pipeline/partials/dag_canvas.html
  - src/phaze/templates/base.html
  - src/phaze/services/pipeline.py
  - src/phaze/routers/pipeline.py
findings:
  critical: 1
  warning: 1
  info: 2
  total: 4
status: resolved
resolution: "CR-01 (form-encode endpoint) + WR-01 (parse guard) fixed in commits f28280c/76a1a13; IN-01/IN-02 declined (seed-to-0 is a deliberate tested invariant; aria-label change cosmetic). Full suite green 1750."
---

# Phase 38: Code Review Report

**Reviewed:** 2026-06-13
**Depth:** standard
**Files Reviewed:** 4
**Status:** issues_found

## Summary

Phase 38 adds per-stage pause/resume and priority-stepper controls to the three agent nodes on
the DAG canvas, removes the dead "Rescan Files" anchor, extends `_build_dag_context` with six
new int store keys, and adds `get_stage_controls` as a degrade-safe DB reader.

The architecture is sound: the two-button `x-show` approach (static `hx-post`, RESEARCH A4
resolved), authoritative-only store update via `@htmx:after-request`, 5s OOB poll propagation,
`int()`-coerced `paused` values, and the nested-try degrade pattern all match the spec. Dark
mode coverage, layout gutter, canvas height, edge anchors, `<ol>` text-equivalent seeding, and
XSS mitigations are all correct.

**One blocker breaks the priority stepper entirely:** HTMX's `hx-vals` sends
`application/x-www-form-urlencoded` by default, but `POST /pipeline/stages/{stage}/priority`
expects a JSON body (`StagePriorityDelta` is a plain Pydantic `BaseModel`). Every click on
▲ Higher and ▼ Lower will receive a 422 response. Pause/Resume are unaffected — those
endpoints have no body parameters.

---

## Critical Issues

### CR-01: Priority stepper always 422 — `hx-vals` sends form data; endpoint expects JSON body

**File:** `src/phaze/templates/pipeline/partials/dag_canvas.html:147,154`

**Issue:** `hx-vals='{"delta": -10}'` and `hx-vals='{"delta": 10}'` cause HTMX to merge `delta`
into a FormData object and POST with `Content-Type: application/x-www-form-urlencoded`. The body
arriving at the Phase 37 endpoint is `delta=-10` (URL-encoded). FastAPI, seeing
`body: StagePriorityDelta` (a plain `BaseModel`), calls `await request.json()` on that
URL-encoded string, which raises `json.JSONDecodeError`, and returns 422. The
`@htmx:after-request` handler then sees `$event.detail.successful === false`, sets `error = true`,
and displays "Couldn't update. Retry." on every click. The Pause/Resume endpoints are not
affected because `POST /pipeline/stages/{stage}/pause` and `.../resume` have no body parameters.

`StagePriorityDelta` (confirmed in `src/phaze/schemas/pipeline_stages.py`):
```python
class StagePriorityDelta(BaseModel):
    delta: int
```

HTMX 2.x only sends a JSON request body when the `json-enc` extension
(`htmx-ext-json-enc`) is active. Without it, `hx-vals` always serialises to form data.

**Fix — Option A (preferred, no Phase 37 change):** Load the `htmx-ext-json-enc` extension in
`base.html` alongside the existing SSE extension, then add `hx-ext="json-enc"` to the two
priority buttons:

```html
<!-- base.html: add next to the SSE extension -->
<script src="https://cdn.jsdelivr.net/npm/htmx-ext-json-enc@2.0.1/json-enc.js"
        integrity="sha384-<verified-hash>"
        crossorigin="anonymous"></script>
```

```html
<!-- dag_canvas.html stage_controls macro, ▲ Higher button -->
<button type="button"
        hx-post="/pipeline/stages/{{ stage }}/priority"
        hx-vals='{"delta": -10}'
        hx-ext="json-enc"
        hx-swap="none" hx-disabled-elt="this"
        ...>▲ Higher</button>

<!-- ▼ Lower button — same addition -->
<button type="button"
        hx-post="/pipeline/stages/{{ stage }}/priority"
        hx-vals='{"delta": 10}'
        hx-ext="json-enc"
        hx-swap="none" hx-disabled-elt="this"
        ...>▼ Lower</button>
```

**Fix — Option B (no new JS dependency):** Change the endpoint to accept `delta` as a form
field. Replace `body: StagePriorityDelta` with a `Form()` parameter in `pipeline_stages.py`:

```python
from fastapi import APIRouter, Depends, Form, HTTPException

@router.post("/pipeline/stages/{stage}/priority")
async def set_priority(
    stage: str,
    delta: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _validate_stage(stage)
    row = await _load_control_row(session, stage, lock=True)
    new_priority = max(_PRIORITY_MIN, min(_PRIORITY_MAX, row.priority + delta))
    row.priority = new_priority
    await set_stage_priority(session, stage, new_priority)
    await session.commit()
    return _response(row)
```

If Option B is chosen, `StagePriorityDelta` can be removed (or retained for the API-level
`/api/v1/` endpoint if one exists). Update any tests that POST JSON to the endpoint to POST
form data instead.

---

## Warnings

### WR-01: `JSON.parse` in `@htmx:after-request` has no error boundary — silent stale state on malformed response

**File:** `src/phaze/templates/pipeline/partials/dag_canvas.html:131`

**Issue:** The handler:
```javascript
if ($event.detail.successful) {
    const r = JSON.parse($event.detail.xhr.response);
    $store.pipeline.{{ stage }}Priority = r.priority;
    $store.pipeline.{{ stage }}Paused = r.paused ? 1 : 0;
    error = false;
} else { error = true; }
```
If `JSON.parse` throws (e.g., an upstream proxy injects an HTML error page on a 2xx, or Phase 37
is mis-deployed to return a non-JSON body), the exception escapes the handler: `error` is never
set to `true`, the store is not updated, and the UI shows no error message. The operator sees a
successful-looking click with stale displayed values and no "Couldn't update. Retry." feedback.

The Phase 37 endpoints reliably return JSON on 2xx in normal operation, making this an
edge-case risk rather than a daily occurrence. The concern is elevated because CR-01 means
priority POSTs currently return 422 (which does trigger `error = true` correctly). Once CR-01
is fixed, this becomes the next robustness gap.

**Fix:** Wrap the parse in a try/catch so any parse failure surfaces as an error state:
```javascript
@htmx:after-request="
  if ($event.detail.successful) {
    try {
      const r = JSON.parse($event.detail.xhr.response);
      $store.pipeline.{{ stage }}Priority = r.priority;
      $store.pipeline.{{ stage }}Paused = r.paused ? 1 : 0;
      error = false;
    } catch (_) {
      error = true;
    }
  } else {
    error = true;
  }"
```

---

## Info

### IN-01: `base.html` seeds `*Priority` store keys at `0` — semantic mismatch with the actual default (50)

**File:** `src/phaze/templates/base.html:120-122`

**Issue:**
```javascript
metadataPriority: 0, analyzePriority: 0, fingerprintPriority: 0
```
Priority `0` means "maximum urgency / runs first" in the Phase 37 semantics; the actual default
in `_DEFAULT_CONTROLS` and migration 020 is `50`. Before Alpine's `x-init` seeds fire (via the
`dag.items()` loop in `dag_canvas.html`), the ▲ Higher buttons appear disabled because
`:disabled="$store.pipeline.{{ stage }}Priority <= 0"` evaluates `0 <= 0 = true`.

In practice this is sub-millisecond: Alpine processes `x-init` on the seed `<p>` elements
(which appear earlier in DOM order) before it evaluates the `:disabled` binding on the buttons.
No visible flash occurs. However, a developer reading the store initialisation without this
context would expect `0` to match the runtime default and could be misled when debugging.

**Fix:** Seed the three priority keys at `0` if that is the intended "pre-poll placeholder" per
the RESEARCH doc, or change to `50` to match the semantic default (the in-page `x-init` will
overwrite either value immediately):
```javascript
metadataPaused: 0, metadataPriority: 50,
analyzePaused: 0,  analyzePriority: 50,
fingerprintPaused: 0, fingerprintPriority: 50
```

### IN-02: Dynamic `:aria-label` on `x-show`-gated static buttons is redundant — simpler static strings would suffice

**File:** `src/phaze/templates/pipeline/partials/dag_canvas.html:137,142`

**Issue:** Both the Pause and Resume buttons carry the identical Alpine binding:
```html
:aria-label="($store.pipeline.{{ stage }}Paused ? 'Resume' : 'Pause') + ' {{ stage }} stage'"
```
Because the Pause button is always hidden (`display:none`) when `paused=1` and always visible
when `paused=0`, its aria-label always evaluates to `"Pause {{ stage }} stage"`. The Resume
button is symmetric. The dynamic binding is correct but unnecessarily complex given two-button
static structure mandated by RESEARCH A4 / UI-SPEC §Component Inventory.

**Fix:** Replace both `:aria-label` bindings with static equivalents matching the UI-SPEC
Copywriting Contract:
```html
<!-- Pause button -->
aria-label="Pause {{ stage }} stage"

<!-- Resume button -->
aria-label="Resume {{ stage }} stage"
```
This also removes a reactive dependency on the store from elements whose visibility already
encodes the state.

---

_Reviewed: 2026-06-13_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
