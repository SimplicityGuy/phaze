---
quick_id: 260615-cyp
slug: fix-dag-canvas-xdata-quote
date: 2026-06-15
---

# Quick Task: Fix DAG canvas x-data double-quote rendering bug

## Problem

The live pipeline dashboard rendered the entire Alpine `nodes` getter expression as
visible page text instead of a working graph. Root cause: in
`src/phaze/templates/pipeline/partials/dag_canvas.html`, a JS comment inside the
parent `#pipeline-dag` `x-data="..."` attribute used **double quotes**:

```js
// where 0 == "no online agent" (fail-safe default). NO stage_controls ...
```

The `x-data` attribute is opened with `"`, so the browser terminates the attribute at
the first `"` of `"no online agent"` and dumps the remainder of the expression
(fingerprint_scan → proposals → scrape → match → `}; } }">`) into the DOM as text.
Introduced in Phase 40 (the `fingerprint_scan` node comment).

## Fix

1. Change the comment's `"no online agent"` → `'no online agent'` (single quotes). The
   whole getter otherwise uses single quotes for every JS string.
2. Add a regression guard
   (`test_xdata_getter_has_no_unescaped_double_quotes`) that extracts the
   `#pipeline-dag` `x-data` attribute value and asserts it contains zero literal `"`
   characters — any double-quote inside the attribute prematurely closes it.

## Verification

- New test fails (2 double-quotes) if the bug is reintroduced; passes (0) with the fix.
- `uv run pytest tests/test_dag_canvas_render.py` — 30 non-DB tests pass (4 `integration`
  tests error only on absent local Postgres).
- `pre-commit run --files ...` — all hooks clean.

## Scope

One template line + one test. NODE_LAYOUT / EDGES / canvas dimensions UNCHANGED.
