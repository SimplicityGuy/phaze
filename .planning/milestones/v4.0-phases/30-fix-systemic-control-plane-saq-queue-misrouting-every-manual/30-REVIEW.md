---
phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual
reviewed: 2026-06-09T00:00:00Z
depth: standard
files_reviewed: 18
files_reviewed_list:
  - src/phaze/main.py
  - src/phaze/services/enqueue_router.py
  - src/phaze/services/agent_task_router.py
  - src/phaze/services/ingestion.py
  - src/phaze/routers/pipeline.py
  - src/phaze/routers/tracklists.py
  - src/phaze/routers/scan.py
  - src/phaze/routers/agent_exec_batches.py
  - src/phaze/templates/pipeline/partials/trigger_response.html
  - src/phaze/templates/tracklists/partials/scan_progress.html
  - tests/test_services/test_enqueue_router.py
  - tests/test_no_default_queue_producers.py
  - tests/test_routers/test_pipeline.py
  - tests/test_routers/test_pipeline_fingerprint.py
  - tests/test_routers/test_tracklists.py
  - tests/test_routers/test_scan.py
  - tests/test_main_lifespan.py
  - tests/test_phase04_gaps.py
findings:
  critical: 0
  warning: 4
  info: 3
  total: 7
status: resolved
fixes_applied:
  date: 2026-06-10
  scope: critical+warning
  resolved: [WR-01, WR-02, WR-03, WR-04]
  deferred_info: [IN-01, IN-02, IN-03]
  commits: [2c9ac7c, 84c9256, b127a4e, c72f3e3]
  verification: 128 tests pass, ruff + mypy clean
---

# Phase 30: Code Review Report

> **Resolution (2026-06-10):** All 4 Warning findings fixed via `/gsd:code-review 30 --fix`
> (commits 2c9ac7c WR-01, 84c9256 WR-02, b127a4e WR-04, c72f3e3 WR-03). Info findings
> IN-01/02/03 deferred (not in `--fix` default scope). 128 phase-touched tests green.

**Reviewed:** 2026-06-09
**Depth:** standard
**Files Reviewed:** 18
**Status:** issues_found

## Summary

Phase 30 eliminates the v4.0.6 default-queue stranding incident by introducing a
single routing chokepoint (`resolve_queue_for_task`), wiring a named `controller`
queue in the API lifespan, and replacing every `app.state.queue` reference across
all control-plane endpoints. The core routing logic — CONTROLLER_TASKS /
AGENT_TASKS frozensets, active-agent selection, NoActiveAgentError propagation —
is correct and well-tested. The SAQ cron-function dispatch concern (whether
`refresh_tracklists` is reachable when only in `cron_jobs`) was verified against
the installed SAQ source: SAQ's `Worker.__init__` adds every `CronJob.function` to
`self.functions` at lines 157-160 of `worker.py`, so `refresh_tracklists` is in the
dispatch table and the CONTROLLER_TASKS entry is sound.

Four warnings are filed. None blocks correct queue routing; all degrade
maintainability or create silent mis-behavior edge cases.

---

## Warnings

### WR-01: Static guard has a false-negative for two-step attribute access

**File:** `tests/test_no_default_queue_producers.py:75-79`

**Issue:** `_ProducerVisitor.visit_Attribute` only flags the exact AST pattern
`<expr>.state.queue` — i.e., an `Attribute` node whose `attr` is `"queue"` and
whose `value` is itself an `Attribute` node with `attr="state"`. A developer who
writes a two-step form escapes detection:

```python
# NOT caught by the current visitor:
s = request.app.state
queue = s.queue          # Attribute node: s.queue — value is a Name, not an Attribute
```

Because `s` is a `Name` node (not an `Attribute`), the `isinstance(node.value,
ast.Attribute)` check at line 77 is False and the offending access is silently
passed. The meta-test (`test_static_guard_would_catch_a_reintroduced_producer`)
only exercises the direct single-expression form and would not catch this variant.

**Fix:** Extend the visitor to also flag any `Attribute` node whose `attr` is
`"queue"` and whose `value` resolves to the name `"state"` (i.e., also accept a
`Name` node with `id == "state"`), or use a two-pass approach: first record all
assignments `<name> = <expr>.state`, then flag `<name>.queue`.

A simpler partial improvement is to add the direct `Name` case:

```python
def visit_Attribute(self, node: ast.Attribute) -> None:
    if node.attr == "queue":
        val = node.value
        # Direct: *.state.queue
        if isinstance(val, ast.Attribute) and val.attr == "state":
            self.default_refs.append((node.lineno, "*.state.queue attribute access"))
        # Indirect: state.queue  (where `state` was assigned from *.app.state)
        elif isinstance(val, ast.Name) and val.id == "state":
            self.default_refs.append((node.lineno, "state.queue attribute access (possible indirect)"))
    self.generic_visit(node)
```

The indirect form introduces a false-positive risk (any local variable named
`state` with a `.queue` attribute), but the scanned trees are small enough that a
manual review of any flag is cheap.

---

### WR-02: `scan_status` does not include `no_active_agent` in its context, creating an implicit schema gap

**File:** `src/phaze/routers/tracklists.py:288-300`
**Related:** `src/phaze/templates/tracklists/partials/scan_progress.html:3`

**Issue:** The template `scan_progress.html` uses the defensive check
`{% if no_active_agent is defined and no_active_agent %}`. This works because
Jinja2's default `Undefined` is falsy. However, the `scan_status` endpoint never
passes `no_active_agent` to the template (lines 288-300), while `trigger_scan`'s
no-active-agent branch does (with `True`) and the normal success branch does not
either. This means the same template is rendered with three different context
schemas:

1. `trigger_scan` no-agent branch: `{..., done: True, no_active_agent: True}`
2. `trigger_scan` success branch: `{..., done: False}` — key absent
3. `scan_status` response: `{..., done: bool}` — key absent

The `is defined` guard in the template exists specifically to handle the absent key
from paths (2) and (3), which means the template's own safe rendering depends on a
Jinja2 behavior detail (undefined-is-falsy) rather than an explicit contract. If
the project ever switches to `StrictUndefined` (common practice to catch typos), or
a future maintainer adds `no_active_agent` logic to the template without realising
`scan_status` never populates it, the gap silently breaks the done-state rendering.

**Fix:** Add `no_active_agent: False` explicitly to the `scan_status` context and
to the `trigger_scan` success branch, then drop the `is defined` guard from the
template:

```python
# scan_status (tracklists.py ~line 288):
context={
    "request": request,
    "job_ids": job_ids,
    "agent_id": agent_id,
    "total": total,
    "completed": completed,
    "done": done,
    "tracklists_created": tracklists_created,
    "errors": errors,
    "no_active_agent": False,   # add this
}
```

```html
<!-- scan_progress.html line 3: simplify to -->
{% if no_active_agent %}
```

---

### WR-03: Test double infrastructure duplicated across five test files

**Files:**
- `tests/test_services/test_enqueue_router.py:73-82`
- `tests/test_no_default_queue_producers.py:149-158`
- `tests/test_routers/test_pipeline.py:37-64`
- `tests/test_routers/test_pipeline_fingerprint.py:22-49`
- `tests/test_routers/test_tracklists.py:18-67`
- `tests/test_routers/test_scan.py:29-63`

**Issue:** `_FakeQueue`, `_FakeTaskRouter` / `_FakeRouter`, `_seed_active_agent`,
`_stub_app_state`, and `_wire_fakes` are independently defined in all five test
files. The implementations differ in minor ways (e.g., `_FakeQueue.enqueue` returns
`None` in `test_pipeline.py` but a `MagicMock` job in `test_tracklists.py`; the
`_seed_active_agent` helpers differ in whether they call `session.commit()` or
`session.flush()`). Any change to the fake queue's interface (e.g., making
`enqueue` return a typed object) requires tracking down all five copies.

**Fix:** Centralise the shared doubles in `tests/conftest.py` or a dedicated
`tests/fakes.py` module, and import them in each test file. The
`test_tracklists.py` variant (with `_FakeQueue.job = AsyncMock`) is the richest
and should be the canonical implementation. The `_seed_active_agent` helper that
calls `session.commit()` (used in most files) is the correct one for creating a
committed agent row — prefer it over the `flush()`-only variant in
`test_tracklists.py`.

---

### WR-04: `scan_status` renders `done=True` and "Scan complete" when `job_ids` is empty

**File:** `src/phaze/routers/tracklists.py:265`

**Issue:** The `scan_status` endpoint accepts `job_ids: str = Query(...)`. If the
client sends `job_ids=` (an empty string), the list comprehension at line 265
produces `ids = []`. With `total = len(ids) = 0` and `completed = 0`, the
expression `done = completed >= total` evaluates to `True` (0 >= 0). The endpoint
returns the `done=True` branch with `tracklists_created=0`, which renders "Scan
complete. No matching tracks found" — a misleading success state when no scan
actually ran.

This edge case is hard to reach in the normal HTMX polling flow (the job_ids come
from `trigger_scan`'s real enqueue loop), but it is reachable by a direct GET
request or a programming error that constructs the poll URL incorrectly.

**Fix:** Guard against an empty ID list before computing `done`:

```python
ids = [jid.strip() for jid in job_ids.split(",") if jid.strip()]
if not ids:
    raise HTTPException(status_code=422, detail="job_ids must be a non-empty comma-separated list")
```

Alternatively, require `job_ids` to match a format pattern at the query param
level:
```python
job_ids: str = Query(..., min_length=1)
```

---

## Info

### IN-01: `agent_id` query parameter in `scan_status` lacks format validation

**File:** `src/phaze/routers/tracklists.py:255`

**Issue:** `agent_id: str = Query(...)` accepts any string. The value is passed to
`task_router.queue_for(agent_id)`, which constructs a Redis queue named
`phaze-agent-{agent_id}`. The Phase 26 D-18 slug contract (`^[a-z0-9]+(-[a-z0-9]+)*$`)
is never enforced at the HTTP boundary. A malformed or very long `agent_id` produces
a queue name that will never match any running worker, so `queue.job()` returns
`None` for every key — the poll silently drains as "complete". The SUMMARY
acknowledges this as accepted residual T-30-03.

**Fix:** Add a `pattern` constraint to make the Phase 26 contract explicit and
surface invalid values as 422 rather than a silent wrong-queue poll:

```python
agent_id: str = Query(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128)
```

---

### IN-02: `no_active_agent: False` hardcoded in `trigger_proposals_ui` without explanatory comment

**File:** `src/phaze/routers/pipeline.py:251`

**Issue:** `trigger_proposals_ui` passes `no_active_agent: False` to the template
context unconditionally (line 251) and has no try/except around the
`resolve_queue_for_task("generate_proposals", ...)` call (line 243). This is
correct: `generate_proposals` is in `CONTROLLER_TASKS`, so `resolve_queue_for_task`
never calls `select_active_agent` and can never raise `NoActiveAgentError`. But the
omission of a try/except differs from all six per-agent HTMX handlers in the same
file and will puzzle the next maintainer who adds a try/except "for consistency"
and wonders why it never fires.

**Fix:** Add a one-line comment:

```python
# generate_proposals is a controller task; resolve_queue_for_task never raises
# NoActiveAgentError for controller tasks (no agent selection needed).
routed = await enqueue_router.resolve_queue_for_task("generate_proposals", request.app.state, session)
```

---

### IN-03: Background enqueue tasks silently swallow Redis exceptions (pre-existing)

**File:** `src/phaze/routers/pipeline.py:78-80, 118-119, 202-204, 287-289, 313-315, 372-374`
**File:** `src/phaze/routers/scan.py:71-73`

**Issue:** Background enqueue tasks are created with
`asyncio.create_task(_enqueue_*_jobs(...))` and the only done callback is
`_background_tasks.discard`. If `queue.enqueue(...)` raises (Redis connection
refused, network error), the exception propagates out of the background coroutine,
asyncio logs "Task exception was never retrieved" to stderr, and the exception is
never captured in structlog. The calling endpoint has already returned
`{"enqueued": N}` to the user, so N jobs are reported as enqueued but none
actually landed in Redis.

This pattern predates Phase 30 and is not worsened by the routing fix. Addressing
it (e.g., with a structured exception-logging callback) is deferred work, not a
Phase 30 regression.

**Fix when addressed:**
```python
def _log_task_exception(task: asyncio.Task[None]) -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and (exc := task.exception()):
        logger.error("background enqueue failed", error=str(exc), exc_info=exc)

task.add_done_callback(_log_task_exception)
```

---

_Reviewed: 2026-06-09_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
