---
quick_id: 260615-cyp
slug: fix-dag-canvas-xdata-quote
date: 2026-06-15
status: complete
commit: 928d229
pr: 137
---

# Summary: Fix DAG canvas x-data double-quote rendering bug

## What changed

- `src/phaze/templates/pipeline/partials/dag_canvas.html` — the `fingerprint_scan`
  comment `// where 0 == "no online agent"` now uses single quotes
  (`'no online agent'`). The double quotes were closing the parent `#pipeline-dag`
  `x-data="..."` attribute early, so the browser rendered the rest of the Alpine
  `nodes` getter as visible page text.
- `tests/test_dag_canvas_render.py` — added
  `test_xdata_getter_has_no_unescaped_double_quotes`, which extracts the
  `#pipeline-dag` x-data attribute value (opening `x-data="` to the first `">`) and
  asserts it contains zero literal `"` characters.

## Verification

- Guard reads `0` double-quotes on the fix, `2` if the bug is reintroduced.
- `uv run pytest tests/test_dag_canvas_render.py` — 30 pass; 4 `test_integration_*`
  error only because local Postgres (:5432) is absent (environmental, unrelated).
- `pre-commit run --files ...` — all hooks clean (ruff/ruff-format/bandit/mypy).

## Notes

- Live incident: observed on the homelab dashboard (nox/lux, v4.2.0). The fix ships
  in the next image build/redeploy — no DB or data migration involved.
- Layout untouched: `NODE_LAYOUT`, `EDGES`, and canvas dimensions are unchanged.
- Sibling finding from the same review (NOT in this PR): a stale SAQ
  `cron:reenqueue_discovered` job fired once post-v4.2.0-restart and dead-lettered
  (`KeyError: 'reenqueue_discovered'` — function removed in Phase 42). It did not
  recur (cron no longer registered); benign, no code change required.
