---
phase: 88-lane-agent-drill-in
reviewed: 2026-07-11T17:02:49Z
depth: standard
files_reviewed: 13
files_reviewed_list:
  - src/phaze/routers/admin_agents.py
  - src/phaze/routers/pipeline.py
  - src/phaze/routers/shell.py
  - src/phaze/services/backends.py
  - src/phaze/services/pipeline.py
  - src/phaze/templates/admin/agents.html
  - src/phaze/templates/admin/partials/_agent_activity.html
  - src/phaze/templates/admin/partials/agents_table.html
  - src/phaze/templates/pipeline/partials/_detail_pane.html
  - src/phaze/templates/pipeline/partials/_lane_card.html
  - src/phaze/templates/pipeline/partials/_lane_detail.html
  - src/phaze/templates/pipeline/partials/analyze_workspace.html
  - src/phaze/templates/shell/shell.html
findings:
  critical: 2
  warning: 2
  info: 2
  total: 6
status: resolved
resolved_in: 3665d328
---

> **Resolution (commit `3665d328`):** CR-01, CR-02, WR-01/WR-02, and IN-01 fixed with
> mutation-verified regression tests (each new/changed test fails on the pre-fix code).
> IN-02 required no code change (spec-confirmed DRILL-02 semantics). See the commit body
> for the fix-by-finding mapping.

# Phase 88: Code Review Report

**Reviewed:** 2026-07-11T17:02:49Z
**Depth:** standard
**Files Reviewed:** 13
**Status:** issues_found

## Summary

Phase 88 adds two HTMX drill-in endpoints (`GET /pipeline/lanes/{backend_id}`,
`GET /admin/agents/{agent_id}/_activity`) rendering non-modal detail-pane bodies, plus
degrade-safe DB aggregate helpers (`_agent_stage_buckets`, `get_agent_lane_depths`,
`get_agent_recent_scans`, `get_lane_recent_completions`, `get_lane_queue_depths`) and
poll-survival `?lane=`/`?agent=` seeding.

**Security posture is sound.** All new SQL uses SQLAlchemy ORM with bound parameters
(`FileRecord.agent_id == agent_id`, `CloudJob.backend_id == backend_id`); no raw string
interpolation was introduced (the only `text()` SQL in `services/pipeline.py` is the
pre-existing static `_STAGE_BUSY_SQL` family with no operand interpolation). No new template
uses `|safe` or `|tojson`; `agent.id` / `agent.name` / `lane.id` / `lane.kind` stay
Jinja-autoescaped, and the raw hostile `agent_id`/`backend_id` are never reflected on the
not-found branches. Path params are validated by lookup-in-known-set, and `Agent.id` /
`CloudJob.backend_id` are `String` columns so `session.get(Agent, agent_id)` returns `None`
(not a UUID-cast DBAPIError 500) for an unknown id.

**Two correctness defects escaped, both in the "never 500 / degrade-safe" and dismiss
contracts the phase explicitly claims.** The agent-activity degrade path can 500 the pane on a
DB hiccup, and the agent-activity pane cannot be dismissed (its own self-poll re-opens it).
Both are grounded in the phase's own sibling code, which does it correctly for the lane path.

## Critical Issues

### CR-01: `_agent_stage_buckets` degrade path expires the loaded `agent` ORM object → 500 on render

**File:** `src/phaze/services/pipeline.py:402-408` (called from `src/phaze/routers/admin_agents.py:219`)

**Issue:** `agent_activity` loads the `agent` ORM object first (`admin_agents.py:205`), then
runs `_agent_stage_buckets` six times, then renders `_agent_activity.html` which reads
`agent.name` / `agent.id` / `agent.kind` / `agent.last_seen_at` (and the `_kind_badge.html` /
`_status_pill.html` includes). On any bucket-query failure, `_agent_stage_buckets` executes a
**plain `await session.rollback()`**:

```python
except Exception:
    logger.warning("agent_stage_bucket_degraded", ...)
    try:
        await session.rollback()   # <-- expires ALL ORM objects, incl. the loaded `agent`
    except Exception:
        ...
```

A plain `session.rollback()` expires every instance in the identity map (independent of
`expire_on_commit`), including the already-loaded `agent`. The subsequent synchronous Jinja
render then accesses an expired mapped attribute, triggering an async lazy-load outside a
greenlet context → `MissingGreenlet` → **HTTP 500**. This directly defeats the endpoint's
documented D-00b/T-88-07 guarantee ("Every read is bounded + degrade-safe … NEVER an
HTTPException / JSON / 500").

This is the *exact* hazard the sibling helper added in this same phase already guards against:
`get_agent_recent_scans` (`services/pipeline.py:447-464`) uses `session.begin_nested()`
specifically because — per its own docstring — "a plain `session.rollback()` would expire it
and 500 the render on the next lazy load." `_agent_stage_buckets` runs on the same `agent`
object, after it is loaded and before render, but was left on the plain-rollback path. (It is a
clone of `_safe_bucket_counts`, which is only *safe* because its callers load ORM objects
*after* it runs — the reverse of the ordering here.)

**Fix:** Wrap the read in a SAVEPOINT so the degrade path recovers the aborted transaction
without expiring the caller's `agent`, mirroring `get_agent_recent_scans` / `get_stage_busy_counts`:

```python
async def _agent_stage_buckets(session, agent_id, stage) -> dict[str, int]:
    out = {s.value: 0 for s in Status}
    status_subq = (
        select(stage_status_case(stage).label("status"))
        .where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))
        .where(FileRecord.agent_id == agent_id)
        .subquery()
    )
    stmt = select(status_subq.c.status, func.count()).group_by(status_subq.c.status)
    try:
        async with session.begin_nested():
            rows = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("agent_stage_bucket_degraded", stage=stage.value, agent_id=agent_id, exc_info=True)
        return out
    for status_label, n in rows:
        if status_label in out:
            out[status_label] = int(n)
    return out
```

### CR-02: Agent-activity detail pane cannot be dismissed — its 5s self-poll re-opens it forever

**File:** `src/phaze/templates/admin/partials/_agent_activity.html:51-56` (interacts with
`src/phaze/templates/pipeline/partials/_detail_pane.html:49-67,91-93`)

**Issue:** The agent-activity body's ROOT element carries the own-tick poll directly:

```html
<div id="agent-activity-body"
     hx-get="/admin/agents/{{ agent.id }}/_activity"
     hx-trigger="every 5s"
     hx-target="#detail-pane"
     hx-swap="innerHTML"
     ...>
```

Every 5s this swaps `#detail-pane`'s innerHTML, which fires the shell's
`hx-on::after-swap="… onLoaded()"` (`_detail_pane.html:93`). `onLoaded()` unconditionally sets
`this.open = true` (`_detail_pane.html:52`). So after the operator clicks ✕ (or presses Esc) →
`hide()` sets `open=false` and clears `?agent=` — but within 5s the still-running self-poll
re-swaps the body, re-runs `onLoaded()`, and **re-opens the pane**. The Close button and Esc
are effectively inert, and a runaway background poll to `/admin/agents/{id}/_activity` continues
indefinitely.

The lane pane does NOT have this bug: `_lane_detail.html:109-115` puts the own-tick on a
*separate* `<div>` that removes itself on dismiss —
`x-effect="if (armed && !open && window.htmx) window.htmx.remove($el)"`. `_agent_activity.html`
omits this stop mechanism entirely. The asymmetry between the two wave-2 bodies is the defect.

**Fix:** Mirror the lane pane's self-removing own-tick. Move the `hx-trigger="every 5s"` off the
body root onto a dedicated child element scoped to the shell's `open` flag so it stops on
dismiss:

```html
{# own-tick that removes itself when the pane is dismissed (matches _lane_detail.html) #}
<div hx-get="/admin/agents/{{ agent.id }}/_activity"
     hx-trigger="every 5s"
     hx-target="#detail-pane"
     hx-swap="innerHTML"
     x-data="{ armed: false }"
     x-init="$nextTick(() => { armed = true })"
     x-effect="if (armed && !open && window.htmx) window.htmx.remove($el)"></div>
```

(and drop `hx-trigger`/`hx-get`/`hx-target`/`hx-swap` from `#agent-activity-body`).

## Warnings

### WR-01: `agent_activity` returns `status_code=404` for an unknown agent, but the /admin/agents page has no htmx 404 opt-in — the friendly empty fragment is discarded

**File:** `src/phaze/routers/admin_agents.py:206-214` (interacts with
`src/phaze/templates/admin/agents.html`)

**Issue:** For an unknown/stale/hostile `agent_id`, `agent_activity` returns the
`_agent_activity.html` "Agent not found" fragment with `status_code=404`. htmx does **not** swap
non-2xx responses by default; it fires `htmx:responseError` and leaves the target untouched. The
`record.py idiom` cited in the docstring only works because `shell.html:252-262` explicitly
opts the 404 back in (`d.shouldSwap = true`) — but ONLY for `d.target.id === 'record-body'`.
The `/admin/agents` page extends `base.html` (not `shell.html`) and has **no** `htmx:beforeSwap`
handler for `#detail-pane` (confirmed: `agents.html` and `base.html` contain none). Result: the
"friendly empty fragment" never renders into the pane — the click/self-poll silently no-ops, and
an agent revoked while the pane is open leaves stale content on screen (compounding CR-02).

This is inconsistent with the sibling `lane_detail`, which returns the offline fragment at
**200** (`pipeline.py:811-823`) so it always swaps.

**Fix:** Either return the not-found fragment at status 200 (matching `lane_detail`'s
never-error posture), or add a `htmx:beforeSwap` opt-in for a 404 targeting `#detail-pane` on
the agents page (mirroring `shell.html:252-262`). Returning 200 is the smaller, more consistent
change.

### WR-02: `agent_activity` self-poll 404 stream is unbounded when an agent is revoked mid-view

**File:** `src/phaze/routers/admin_agents.py:181-214` (interacts with CR-02)

**Issue:** Because the agent pane's self-poll never stops (CR-02) and a revoked agent yields a
404 that htmx drops (WR-01), a pane left open on an agent that is subsequently revoked will
issue a `/admin/agents/{id}/_activity` request every 5s **forever**, each returning 404, with no
UI feedback and no termination. Even after CR-02 is fixed (self-removing tick), a robust design
should surface the "agent gone" state. Fixing CR-02 + WR-01 together resolves this; noted
separately because the unbounded-404 behavior is the observable symptom and should be verified
in a regression test (open pane → revoke agent → assert poll stops / shows gone-state, no 500).

**Fix:** After CR-02 (self-removing tick) + WR-01 (200 not-found fragment), ensure the
not-found fragment does NOT re-arm an own-tick (it has no `agent.id` to poll), so the loop
terminates. Add a test covering the revoke-while-open transition.

## Info

### IN-01: Inconsistent lane-dict key access between the two poll-seed sites

**File:** `src/phaze/routers/pipeline.py:758` vs `src/phaze/routers/shell.py:199`

**Issue:** `pipeline_stats_partial` resolves the selected lane with subscript
(`any(one["id"] == lane for one in lanes)`) while `shell._render_stage` uses `.get`
(`any(one.get("id") == lane_param ...)`). `get_backend_lane_snapshot` always populates `"id"`,
so the subscript is safe today, but the two copies of the same lookup should match. Prefer
`.get("id")` in both for defensive symmetry.

**Fix:** Use `one.get("id")` in `pipeline.py:758`.

### IN-02: `_agent_stage_buckets` counts only music/video files for the propose/review/apply columns

**File:** `src/phaze/services/pipeline.py:391-396` (rendered by
`_agent_activity.html:40-50`)

**Issue:** The per-agent matrix scopes every stage — including `propose`/`review`/`apply` — to
`FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)`, and `_mv_total` (the "owns no files yet" gate) is
derived from the metadata bucket sum. This is internally consistent and matches the dashboard
denominator convention, but means an agent owning only non-media files renders "This agent owns
no files yet." Confirm this matches the intended DRILL-02 semantics (it appears to); no code
change required, flagged for spec confirmation only.

---

_Reviewed: 2026-07-11T17:02:49Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
